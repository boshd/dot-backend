import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, posix } from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";
import ts from "typescript";

const require = createRequire(import.meta.url);
const SDK_VERSION = "2";
const MAX_INPUT_BYTES = 700_000;
const MAX_FILE_BYTES = 256_000;
const MAX_SOURCE_BYTES = 512_000;
const MAX_JAVASCRIPT_BYTES = 2_750_000;
const MAX_CSS_BYTES = 350_000;
const SOURCE_EXTENSIONS = [".ts", ".tsx", ".css"];
const USER_PACKAGES = new Set([
  "react",
  "date-fns",
  "lucide-react",
  "motion/react",
  "recharts",
]);
const INTERNAL_PACKAGES = new Set(["react/jsx-runtime", "react-dom/client"]);

const compilerDirectory = dirname(fileURLToPath(import.meta.url));
const sdkSources = {
  "@dot/app-runtime": await readFile(new URL("./sdk/app-runtime.tsx", import.meta.url), "utf8"),
  "@dot/ui": await readFile(new URL("./sdk/ui.tsx", import.meta.url), "utf8"),
  "@dot/ui/styles.css": await readFile(new URL("./sdk/ui.css", import.meta.url), "utf8"),
  "dot:entry": await readFile(new URL("./sdk/entry.tsx", import.meta.url), "utf8"),
};

class CompilerIssue extends Error {
  constructor(code, message, path = undefined) {
    super(`[${code}] ${message}`);
    this.code = code;
    this.detail = message;
    this.path = path;
  }
}

function respond(value, exitCode = 0) {
  process.stdout.write(JSON.stringify(value));
  process.exitCode = exitCode;
}

async function readRequest() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) {
      throw new CompilerIssue("compiler_input_too_large", "compiler input exceeds 700 KB");
    }
    chunks.push(chunk);
  }
  let value;
  try {
    value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new CompilerIssue("invalid_compiler_input", "compiler input must be valid JSON");
  }
  if (!value || typeof value !== "object" || value.protocol_version !== 1) {
    throw new CompilerIssue("invalid_compiler_input", "unsupported compiler protocol");
  }
  return value;
}

function validateSourceTree(request) {
  if (!Array.isArray(request.files) || request.files.length < 1 || request.files.length > 32) {
    throw new CompilerIssue("invalid_source_tree", "source tree must contain between 1 and 32 files");
  }
  const files = new Map();
  let totalBytes = 0;
  for (const file of request.files) {
    if (!file || typeof file.path !== "string" || typeof file.contents !== "string") {
      throw new CompilerIssue("invalid_source_file", "every source file needs a path and contents");
    }
    const normalized = posix.normalize(file.path);
    const extension = posix.extname(normalized);
    if (
      normalized !== file.path ||
      normalized.startsWith("/") ||
      !normalized.startsWith("src/") ||
      normalized.includes("\0") ||
      !SOURCE_EXTENSIONS.includes(extension)
    ) {
      throw new CompilerIssue(
        "invalid_source_path",
        "source paths must be normalized .ts, .tsx, or .css files below src/",
        file.path,
      );
    }
    if (files.has(normalized)) {
      throw new CompilerIssue("duplicate_source_path", "source path is duplicated", normalized);
    }
    const bytes = Buffer.byteLength(file.contents);
    if (bytes > MAX_FILE_BYTES) {
      throw new CompilerIssue("file_too_large", "source file exceeds 256 KB", normalized);
    }
    totalBytes += bytes;
    files.set(normalized, file.contents);
  }
  if (totalBytes > MAX_SOURCE_BYTES) {
    throw new CompilerIssue("source_too_large", "source tree exceeds 512 KB");
  }
  if (typeof request.entrypoint !== "string" || !files.has(request.entrypoint)) {
    throw new CompilerIssue("missing_entrypoint", "entrypoint is not present in the source tree");
  }
  if (!request.entrypoint.endsWith(".ts") && !request.entrypoint.endsWith(".tsx")) {
    throw new CompilerIssue("invalid_entrypoint", "entrypoint must be a TypeScript module");
  }
  return files;
}

function loaderFor(path) {
  if (path.endsWith(".tsx")) return "tsx";
  if (path.endsWith(".ts")) return "ts";
  return "css";
}

function resolveRelative(specifier, importer, files) {
  const candidate = posix.normalize(posix.join(posix.dirname(importer), specifier));
  if (!candidate.startsWith("src/") || candidate.includes("..")) {
    throw new CompilerIssue("invalid_import_path", "relative import leaves the generated source tree", importer);
  }
  const candidates = [
    candidate,
    ...SOURCE_EXTENSIONS.map((extension) => `${candidate}${extension}`),
    ...SOURCE_EXTENSIONS.map((extension) => posix.join(candidate, `index${extension}`)),
  ];
  const resolved = candidates.find((path) => files.has(path));
  if (!resolved) {
    throw new CompilerIssue("unresolved_import", `could not resolve ${JSON.stringify(specifier)}`, importer);
  }
  return resolved;
}

function packagePath(specifier) {
  try {
    return require.resolve(specifier);
  } catch {
    throw new CompilerIssue("dependency_unavailable", `approved dependency ${specifier} is unavailable`);
  }
}

function generatedSourcePlugin(files, entrypoint) {
  const resolveControlled = (args, sdk = false) => {
    const specifier = args.path;
    if (specifier === "__dot_user_entry__" && sdk) {
      return { path: entrypoint, namespace: "dot-generated" };
    }
    if (specifier === "@dot/app-runtime" || specifier === "@dot/ui" || specifier === "@dot/ui/styles.css") {
      return { path: specifier, namespace: "dot-sdk" };
    }
    if (specifier.startsWith("./") || specifier.startsWith("../")) {
      if (sdk) throw new CompilerIssue("invalid_sdk_import", "Dot SDK contains an invalid relative import");
      return { path: resolveRelative(specifier, args.importer, files), namespace: "dot-generated" };
    }
    if (USER_PACKAGES.has(specifier) || (sdk && INTERNAL_PACKAGES.has(specifier)) || specifier === "react/jsx-runtime") {
      return { path: packagePath(specifier), namespace: "file" };
    }
    throw new CompilerIssue(
      "dependency_not_allowed",
      `dependency ${JSON.stringify(specifier)} is not in the approved catalog`,
      sdk ? undefined : args.importer,
    );
  };

  return {
    name: "dot-generated-source",
    setup(build) {
      build.onResolve({ filter: /^dot:entry$/ }, () => ({ path: "dot:entry", namespace: "dot-sdk" }));
      build.onResolve({ filter: /.*/, namespace: "dot-generated" }, (args) => resolveControlled(args));
      build.onResolve({ filter: /.*/, namespace: "dot-sdk" }, (args) => resolveControlled(args, true));
      build.onLoad({ filter: /.*/, namespace: "dot-generated" }, (args) => ({
        contents: files.get(args.path),
        loader: loaderFor(args.path),
        resolveDir: "/dot-generated",
      }));
      build.onLoad({ filter: /.*/, namespace: "dot-sdk" }, (args) => ({
        contents: sdkSources[args.path],
        loader: args.path.endsWith(".css") ? "css" : "tsx",
        resolveDir: compilerDirectory,
      }));
    },
  };
}

async function validateSyntax(files) {
  for (const [path, contents] of files) {
    try {
      await esbuild.transform(contents, {
        loader: loaderFor(path),
        jsx: "automatic",
        target: "es2022",
        legalComments: "none",
      });
    } catch (error) {
      if (error?.errors) throw error;
      throw new CompilerIssue("typescript_compile_error", String(error), path);
    }
  }
}

function validateTypes(files, entrypoint) {
  const sdkFiles = new Map([
    ["/dot-sdk/app-runtime.tsx", sdkSources["@dot/app-runtime"]],
    ["/dot-sdk/ui.tsx", sdkSources["@dot/ui"]],
  ]);
  const sourceFiles = new Map(
    [...files.entries()].map(([path, contents]) => [`/dot-generated/${path}`, contents]),
  );
  const roots = [...sourceFiles.keys()].filter((path) => path.endsWith(".ts") || path.endsWith(".tsx"));
  const options = {
    allowJs: false,
    esModuleInterop: true,
    jsx: ts.JsxEmit.ReactJSX,
    lib: ["lib.es2022.d.ts", "lib.dom.d.ts", "lib.dom.iterable.d.ts"],
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    noEmit: true,
    noImplicitAny: true,
    skipLibCheck: true,
    strict: true,
    target: ts.ScriptTarget.ES2022,
    typeRoots: [posix.join(compilerDirectory, "node_modules/@types")],
    types: ["react", "react-dom"],
  };
  const host = ts.createCompilerHost(options, true);
  const originalFileExists = host.fileExists;
  const originalReadFile = host.readFile;
  const originalGetSourceFile = host.getSourceFile;
  const resolveRelativeType = (specifier, importer) => {
    const importerPath = importer.replace(/^\/dot-generated\//, "");
    const resolved = resolveRelative(specifier, importerPath, files);
    return `/dot-generated/${resolved}`;
  };
  const aliases = new Map([
    ["@dot/app-runtime", "/dot-sdk/app-runtime.tsx"],
    ["@dot/ui", "/dot-sdk/ui.tsx"],
  ]);

  host.fileExists = (path) => sourceFiles.has(path) || sdkFiles.has(path) || originalFileExists(path);
  host.readFile = (path) => sourceFiles.get(path) ?? sdkFiles.get(path) ?? originalReadFile(path);
  host.getSourceFile = (path, languageVersion, onError, shouldCreateNewSourceFile) => {
    const content = host.readFile(path);
    if (content !== undefined && (sourceFiles.has(path) || sdkFiles.has(path))) {
      return ts.createSourceFile(path, content, languageVersion, true, ts.ScriptKind.TSX);
    }
    return originalGetSourceFile(path, languageVersion, onError, shouldCreateNewSourceFile);
  };
  host.resolveModuleNames = (moduleNames, containingFile) => moduleNames.map((specifier) => {
    let resolved;
    if (specifier.endsWith(".css")) {
      return {
        resolvedFileName: posix.join(compilerDirectory, "sdk/style-module.d.ts"),
        extension: ts.Extension.Dts,
        isExternalLibraryImport: false,
      };
    }
    if (aliases.has(specifier)) resolved = aliases.get(specifier);
    else if (specifier.startsWith("./") || specifier.startsWith("../")) {
      if (containingFile.startsWith("/dot-generated/")) {
        resolved = resolveRelativeType(specifier, containingFile);
      }
    }
    if (resolved) {
      return { resolvedFileName: resolved, extension: ts.Extension.Tsx, isExternalLibraryImport: false };
    }
    const direct = ts.resolveModuleName(specifier, containingFile, options, host).resolvedModule;
    if (direct) return direct;
    // Generated files live at a virtual path. Approved third-party packages that are not
    // already provided by the React type roots need one fallback beside real node_modules.
    return ts.resolveModuleName(
      specifier,
      posix.join(compilerDirectory, "generated-entry.tsx"),
      options,
      host,
    ).resolvedModule;
  });

  const program = ts.createProgram({ rootNames: roots, options, host });
  const diagnostics = ts.getPreEmitDiagnostics(program).filter((diagnostic) => {
    const file = diagnostic.file?.fileName;
    return !file || file.startsWith("/dot-generated/") || file.startsWith("/dot-sdk/");
  });
  if (diagnostics.length) {
    const issues = diagnostics.slice(0, 25).map((diagnostic) => {
      const file = diagnostic.file?.fileName;
      const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, " ").slice(0, 800);
      return {
        code: "typescript_type_error",
        message,
        path: file?.startsWith("/dot-generated/")
          ? file.replace(/^\/dot-generated\//, "")
          : undefined,
      };
    });
    const error = new Error("generated source failed TypeScript type checking");
    error.dotIssues = issues;
    throw error;
  }
  if (!sourceFiles.has(`/dot-generated/${entrypoint}`)) {
    throw new CompilerIssue("missing_entrypoint", "entrypoint is not present in typecheck roots");
  }
}

function normalizeEsbuildIssues(error) {
  if (error instanceof CompilerIssue) {
    return [{ code: error.code, message: error.detail, path: error.path }];
  }
  if (Array.isArray(error?.dotIssues)) return error.dotIssues;
  const errors = Array.isArray(error?.errors) ? error.errors : [];
  if (!errors.length) {
    return [{ code: "compiler_failed", message: String(error?.message || error).slice(0, 800) }];
  }
  return errors.slice(0, 25).map((item) => {
    const text = String(item.text || "TypeScript compilation failed");
    const tagged = /^\[([a-z0-9_]+)\]\s*(.*)$/i.exec(text);
    return {
      code: tagged?.[1] || "typescript_compile_error",
      message: (tagged?.[2] || text).slice(0, 800),
      path: item.location?.file?.startsWith("src/") ? item.location.file : undefined,
    };
  });
}

function normalizeCssForPolicy(css) {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return withoutComments.replace(
    /\\(?:([0-9a-f]{1,6})(?:\s)?|([^\r\n\f]))/gi,
    (_match, hexadecimal, escaped) => {
      if (!hexadecimal) return escaped || "";
      const codePoint = Number.parseInt(hexadecimal, 16);
      if (!Number.isFinite(codePoint) || codePoint === 0 || codePoint > 0x10ffff) {
        return "\ufffd";
      }
      return String.fromCodePoint(codePoint);
    },
  );
}

function validateCompiledCss(css) {
  const normalized = normalizeCssForPolicy(css);
  const forbidden = [
    ["@import", /@import\b/i],
    ["url()", /\burl\s*\(/i],
    ["image-set()", /(?:^|[^a-z0-9_])(?:-webkit-)?image-set\s*\(/i],
    ["src()", /\bsrc\s*\(/i],
    ["expression()", /\bexpression\s*\(/i],
    ["behavior", /(?:^|[;{])\s*behavior\s*:/i],
    ["external URL", /(?:https?:)?\/\//i],
    ["javascript URL", /\bjavascript\s*:/i],
  ];
  const match = forbidden.find(([, pattern]) => pattern.test(normalized));
  if (match) {
    throw new CompilerIssue(
      "compiled_css_network_access",
      `compiled CSS contains forbidden ${match[0]} network or active-content syntax`,
    );
  }
}

async function compile(request) {
  const files = validateSourceTree(request);
  await validateSyntax(files);
  const result = await esbuild.build({
    absWorkingDir: compilerDirectory,
    entryPoints: ["dot:entry"],
    bundle: true,
    write: false,
    outdir: "out",
    entryNames: "dot-app",
    format: "iife",
    platform: "browser",
    target: ["safari16.4", "chrome109", "firefox115"],
    jsx: "automatic",
    minify: true,
    treeShaking: true,
    sourcemap: false,
    legalComments: "none",
    charset: "utf8",
    define: { "process.env.NODE_ENV": '"production"' },
    plugins: [generatedSourcePlugin(files, request.entrypoint)],
    logLevel: "silent",
  });
  validateTypes(files, request.entrypoint);
  const javascriptFile = result.outputFiles.find((file) => file.path.endsWith(".js"));
  const cssFile = result.outputFiles.find((file) => file.path.endsWith(".css"));
  if (!javascriptFile) throw new CompilerIssue("missing_browser_bundle", "compiler emitted no JavaScript");
  const javascript = javascriptFile.text;
  const css = cssFile?.text ?? "";
  if (Buffer.byteLength(javascript) > MAX_JAVASCRIPT_BYTES) {
    throw new CompilerIssue("browser_bundle_too_large", "compiled JavaScript exceeds 2.75 MB");
  }
  if (Buffer.byteLength(css) > MAX_CSS_BYTES) {
    throw new CompilerIssue("browser_bundle_too_large", "compiled CSS exceeds 350 KB");
  }
  validateCompiledCss(css);
  const sha256 = createHash("sha256").update(javascript).update("\0").update(css).digest("hex");
  return {
    format: "iife",
    javascript,
    css,
    sha256,
    sdk_version: SDK_VERSION,
    compiler: "esbuild",
    compiler_version: esbuild.version,
  };
}

try {
  const request = await readRequest();
  respond({ ok: true, bundle: await compile(request) });
} catch (error) {
  respond({ ok: false, issues: normalizeEsbuildIssues(error) }, 1);
}
