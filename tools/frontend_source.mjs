// Erase TypeScript syntax for legacy source assertions without rewriting JS.
// Use the compiler parser: regular expressions cannot safely erase TS types.
import ts from '../frontend/node_modules/typescript/lib/typescript.js';
import fs from 'node:fs';
import path from 'node:path';

export function eraseTypes(text, fileName = 'source.ts') {
  return ts.transpileModule(text, {
    fileName,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, removeComments: false },
  }).outputText.replace(/^import\s+(?:type\s+)?[^;]+;\s*/gm, '').replace(/^export\s*\{\};?\s*$/gm, '');
}

if (process.argv[2]) {
  const root = process.argv[2];
  const result = {};
  for (const name of fs.readdirSync(root).filter(name => name.endsWith('.ts'))) {
    result[name] = eraseTypes(fs.readFileSync(path.join(root, name), 'utf8'), name);
  }
  process.stdout.write(JSON.stringify(result));
}

