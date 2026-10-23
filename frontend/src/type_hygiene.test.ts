import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

describe('frontend type boundary hygiene', () => {
  const root = join(process.cwd(), 'src');
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
    expect(domain).not.toMatch(/ModuleDeps|Record<string,\s*Function>/);
    for (const name of ['provider_ui.ts', 'local_context_drawer.ts', 'research_drawer.ts', 'model_settings.ts']) {
      expect(readFileSync(join(root, name), 'utf8')).not.toMatch(/ModuleDeps|\bFunction\b/);
    }
  });

  it('gives the SSE global a callable contract', () => {
    const declarations = readFileSync(join(root, 'types', 'window.d.ts'), 'utf8');
    expect(declarations).toMatch(/Sse:\s*SseApi;/);
    expect(declarations).not.toMatch(/Sse:\s*any/);
  });

  it('keeps composer and request globals behind named contracts', () => {
    const declarations = readFileSync(join(root, 'types', 'window.d.ts'), 'utf8');
    expect(declarations).toMatch(/Composer:\s*import\('\.\.\/composer'\)\.ComposerApi;/);
    expect(declarations).not.toMatch(/Composer:\s*any/);
    expect(readFileSync(join(root, 'requests.ts'), 'utf8')).toMatch(/interface RequestsDeps/);
    expect(declarations).toMatch(/Requests:\s*import\('\.\.\/requests'\)\.RequestsApi;/);
    expect(declarations).toMatch(/ProviderUI:\s*import\('\.\.\/provider_ui'\)\.ProviderUiApi;/);
    expect(declarations).toMatch(/UiState:\s*import\('\.\.\/ui_state'\)\.UiStateApi;/);
    expect(declarations).toMatch(/ConversationUI:\s*import\('\.\.\/conversation_ui'\)\.ConversationUiApi;/);
    expect(declarations).toMatch(/Messages:\s*import\('\.\.\/messages'\)\.MessagesApi;/);
    expect(declarations).toMatch(/Process:\s*import\('\.\.\/process'\)\.ProcessApi;/);
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
    const research = readFileSync(join(root, 'research_drawer.ts'), 'utf8');
    const graph = readFileSync(join(root, 'research_graph.ts'), 'utf8');
    expect(interfaceBody(domain, 'UiDeps')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(domain, 'ResearchRun')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(domain, 'ResearchPayload')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(research, 'ResearchSource')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(research, 'ResearchRunView')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(graph, 'GraphNode')).not.toMatch(/\[key: string\]/);
    expect(interfaceBody(graph, 'RawGraphNode')).not.toMatch(/\[key: string\]/);
  });

  it('routes provider catalog and status boundaries through decoders', () => {
    const provider = readFileSync(join(root, 'provider_ui.ts'), 'utf8');
    expect(provider).toMatch(/decodeProviderCatalog\(data\)/);
    expect(provider).toMatch(/decodeProviderStatus\(rawProviders\)/);
    expect(provider).not.toMatch(/function extractCatalog\(/);
  });

  it('boots the application runtime without the legacy bridge name', () => {
    const main = readFileSync(join(root, 'main.ts'), 'utf8');
    expect(main).toContain("'./app_runtime'");
    expect(main).not.toContain("'./legacy-runtime'");
  });
});
