import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const MAX_INPUT_BYTES = 700_000;

async function readRequest() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error("normalizer input exceeds 700 KB");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || !Array.isArray(value.files)) throw new Error("normalizer needs source files");
  return value;
}

function jsxNameText(name) {
  return ts.isIdentifier(name) ? name.text : undefined;
}

function normalizeTypeScript(file) {
  const kind = file.path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(file.path, file.contents, ts.ScriptTarget.Latest, true, kind);
  const edits = [];
  const counts = {
    normalized_react_types: 0,
    normalized_value_handlers: 0,
    stripped_class_names: 0,
    stripped_inline_styles: 0,
    stripped_style_elements: 0,
  };

  function addEdit(node, replacement) {
    edits.push({ start: node.getStart(source), end: node.end, replacement });
  }

  function visit(node) {
    if (
      ts.isJsxElement(node)
      && jsxNameText(node.openingElement.tagName)?.toLowerCase() === "style"
    ) {
      addEdit(node, "<></>");
      counts.stripped_style_elements += 1;
      return;
    }
    if (
      ts.isJsxSelfClosingElement(node)
      && jsxNameText(node.tagName)?.toLowerCase() === "style"
    ) {
      addEdit(node, "<></>");
      counts.stripped_style_elements += 1;
      return;
    }
    if (ts.isJsxAttribute(node)) {
      const name = jsxNameText(node.name);
      if (name === "style" || name === "className") {
        addEdit(node, "");
        counts[name === "style" ? "stripped_inline_styles" : "stripped_class_names"] += 1;
        return;
      }
      if (
        name === "onChange"
        && node.initializer
        && ts.isJsxExpression(node.initializer)
        && node.initializer.expression
        && ts.isIdentifier(node.initializer.expression)
        && /^set[A-Z][A-Za-z0-9_]*$/.test(node.initializer.expression.text)
      ) {
        addEdit(node.name, "onValueChange");
        counts.normalized_value_handlers += 1;
      }
    }
    if (
      ts.isTypeReferenceNode(node)
      && ts.isQualifiedName(node.typeName)
      && ts.isIdentifier(node.typeName.left)
      && node.typeName.left.text === "JSX"
      && node.typeName.right.text === "Element"
    ) {
      addEdit(node.typeName, "React.ReactElement");
      counts.normalized_react_types += 1;
      return;
    }
    ts.forEachChild(node, visit);
  }

  visit(source);
  let contents = file.contents;
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    contents = contents.slice(0, edit.start) + edit.replacement + contents.slice(edit.end);
  }
  return { file: { ...file, contents }, counts };
}

function mergeCounts(total, next) {
  for (const [key, value] of Object.entries(next)) total[key] = (total[key] ?? 0) + value;
}

try {
  const request = await readRequest();
  const counts = {};
  const files = request.files.map((file) => {
    if (
      !file
      || typeof file.path !== "string"
      || typeof file.contents !== "string"
      || (!file.path.endsWith(".ts") && !file.path.endsWith(".tsx"))
    ) return file;
    const normalized = normalizeTypeScript(file);
    mergeCounts(counts, normalized.counts);
    return normalized.file;
  });
  process.stdout.write(JSON.stringify({ ok: true, files, counts }));
} catch (error) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error?.message ?? error) }));
  process.exitCode = 1;
}
