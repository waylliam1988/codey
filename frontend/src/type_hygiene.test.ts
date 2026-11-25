import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';

describe('frontend type boundary hygiene', () => {
  const root = join(process.cwd(), 'src');
  it('keeps production modules free of explicit any, runtime registration and direct fetch', () => {
    const offenders: string[] = [];
    const inspect = (directory: string): void => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) inspect(path);
        else if (path.endsWith('.ts') && !path.endsWith('.test.ts')) {
          const source = ts.createSourceFile(path, readFileSync(path, 'utf8'), ts.ScriptTarget.Latest);
          const visit = (node: ts.Node): void => {
            if (
              node.kind === ts.SyntaxKind.AnyKeyword ||
              (ts.isImportDeclaration(node) &&
                ts.isStringLiteral(node.moduleSpecifier) &&
                /(^|\/)runtime$/.test(node.moduleSpecifier.text)) ||
              (entry.name !== 'transport.ts' &&
                ts.isCallExpression(node) &&
                ts.isIdentifier(node.expression) &&
                node.expression.text === 'fetch')
            )
              offenders.push(path + ':' + node.getStart(source));
            ts.forEachChild(node, visit);
          };
          visit(source);
        }
      }
    };
    inspect(root);
    expect(offenders).toEqual([]);
  });
  const interfaceBody = (source: string, name: string): string => {
    const start = source.indexOf(`interface ${name}`);
    if (start < 0) return '';
    const next = source.indexOf('\nexport interface ', start + 1);
    return source.slice(start, next < 0 ? source.length : next);
  };
  it('does not reintroduce unchecked record any types', () => {
    const offenders: string[] = [];
    const visit = (directory: string): void => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) visit(path);
        else if (['sse.ts', 'research_runs.ts', 'research_drawer.ts', 'research_graph.ts'].includes(entry.name)) {
          const source = readFileSync(path, 'utf8');
          if (/Record<string,\s*any>/.test(source)) offenders.push(path);
        }
      }
    };
    visit(root);
    expect(offenders).toEqual([]);
  });

  it('does not use the broad Function compatibility dependency type', () => {
    const domain = readFileSync(join(root, 'types', 'domain.ts'), 'utf8');
    expect(domain).not.toMatch(/\bModuleDeps\b|Record<string,\s*Function>/);
    for (const name of [
      'providers/provider_ui.ts',
      'workspace/local_context_drawer.ts',
      'research/research_drawer.ts',
      'providers/model_settings.ts',
    ]) {
      expect(readFileSync(join(root, name), 'utf8')).not.toMatch(/\bModuleDeps\b|\bFunction\b/);
    }
  });

  it('does not expose Codey-prefixed browser globals', () => {
    const offenders: string[] = [];
    const visit = (directory: string): void => {
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        const path = join(directory, entry.name);
        if (entry.isDirectory()) visit(path);
        else if (path.endsWith('.ts') && !path.endsWith('.test.ts')) {
          const source = readFileSync(path, 'utf8');
          if (/window\.Codey[A-Z]/.test(source)) offenders.push(path);
        }
      }
    };
    visit(root);
    expect(offenders).toEqual([]);
  });

  it('keeps arbitrary message fields unknown at the domain boundary', () => {
    const domain = readFileSync(join(root, 'types', 'domain.ts'), 'utf8');
    expect(interfaceBody(domain, 'Message')).not.toMatch(/\[key: string\]/);
    const message: import('./types/domain').Message = { type: 'info', payload: { unsafe: true } };
    // @ts-expect-error Dynamic fields require narrowing before string use.
    const text: string = message.payload;
    void text;
    expect(message.type).toBe('info');
  });

  it('keeps named runtime contracts free of catch-all index signatures', () => {
    const domain = readFileSync(join(root, 'types', 'domain.ts'), 'utf8');
    const research = readFileSync(join(root, 'research/research_drawer.ts'), 'utf8');
    const graph = readFileSync(join(root, 'research/research_graph.ts'), 'utf8');
    expect(interfaceBody(domain, 'UiDeps')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(domain, 'ResearchRun')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(domain, 'ResearchPayload')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(research, 'ResearchSource')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(research, 'ResearchRunView')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(graph, 'GraphNode')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(graph, 'RawGraphNode')).not.toMatch(/\[key: string\]/);
  });

  it('routes provider catalog and status boundaries through decoders', () => {
    const provider = readFileSync(join(root, 'providers/provider_ui.ts'), 'utf8');
    expect(provider).toMatch(/decodeProviderCatalog\(data\)/);
    expect(provider).toMatch(/decodeProviderStatus\(rawProviders\)/);
    expect(provider).not.toMatch(/function extractCatalog\(/);
  });

  it('boots the application runtime without the legacy bridge name', () => {
    const main = readFileSync(join(root, 'main.ts'), 'utf8');
    expect(main).toContain("'./app/app_runtime'");
    expect(main).not.toContain("'./legacy-runtime'");
  });
});
