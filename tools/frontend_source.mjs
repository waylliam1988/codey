// Erase TypeScript syntax for legacy source assertions without rewriting JS.
// Use the compiler parser: regular expressions cannot safely erase TS types.
import ts from '../frontend/node_modules/typescript/lib/typescript.js';
import fs from 'node:fs';
import { build } from '../frontend/node_modules/esbuild/lib/main.js';
import path from 'node:path';

export function eraseTypes(text, fileName = 'source.ts') {
  return ts.transpileModule(text, {
    fileName,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, removeComments: false },
  }).outputText.replace(/^import\s+(?:type\s+)?[^;]+;\s*/gm, '').replace(/^export\s*\{\};?\s*$/gm, '');
}

if (process.argv[2] === '--bundle') {
  const result = await build({
    entryPoints: [process.argv[3]],
    bundle: true,
    write: false,
    format: 'iife',
    globalName: 'fixture',
    loader: { '.css': 'empty' },
  });
  process.stdout.write(result.outputFiles[0].text);
} else if (process.argv[2]) {
  const root = process.argv[2];
  const result = {};
  for (const entry of fs.readdirSync(root, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith('.ts') || entry.name.endsWith('.d.ts')) continue;
    const filename = path.join(entry.parentPath, entry.name);
    result[path.relative(root, filename).split(path.sep).join('/')] = eraseTypes(fs.readFileSync(filename, 'utf8'), entry.name);
  }
  process.stdout.write(JSON.stringify(result));
}
