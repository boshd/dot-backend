import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const sdkPath = fileURLToPath(new URL("./sdk/app-runtime.tsx", import.meta.url));
const tempDirectory = await mkdtemp(join(tmpdir(), "dot-runtime-types-"));

try {
  const options = {
    declaration: true,
    emitDeclarationOnly: true,
    esModuleInterop: true,
    jsx: ts.JsxEmit.ReactJSX,
    lib: ["lib.es2022.d.ts", "lib.dom.d.ts", "lib.dom.iterable.d.ts"],
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
    outDir: tempDirectory,
    skipLibCheck: true,
    strict: true,
    stripInternal: true,
    target: ts.ScriptTarget.ES2022,
  };
  const host = ts.createCompilerHost(options, true);
  const program = ts.createProgram({ rootNames: [sdkPath], options, host });
  const emit = program.emit();
  const diagnostics = [...ts.getPreEmitDiagnostics(program), ...emit.diagnostics];
  if (diagnostics.length) {
    process.stderr.write(ts.formatDiagnosticsWithColorAndContext(diagnostics, host));
    process.exitCode = 1;
  } else {
    process.stdout.write(await readFile(join(tempDirectory, "app-runtime.d.ts"), "utf8"));
  }
} finally {
  await rm(tempDirectory, { recursive: true, force: true });
}
