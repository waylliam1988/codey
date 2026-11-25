import { beforeEach, expect, it, vi } from 'vitest';
import { ResearchDrawer } from './research_drawer';
import type { ResearchRunView } from './research_drawer';

let run: ResearchRunView;
let tab: string;
let copyText: ReturnType<typeof vi.fn>;

beforeEach(() => {
  document.body.innerHTML = `
    <aside id="research-drawer"><span id="research-subtitle"></span>
    <button id="research-restore"></button><div id="research-body"></div></aside>`;
  run = {
    notesCreated: [],
    notesUpdated: [],
    sourceUrls: [],
    evidenceItems: [],
    counterpoints: [],
    qualityWarnings: [],
    citationMap: [],
    openedSources: [],
    coverage: {},
    queries: [],
    searchResults: [],
  };
  tab = 'evidence';
  copyText = vi.fn(async () => true);
  ResearchDrawer.init({
    openDocument: vi.fn(),
    getResearchGraphDepth: () => 1,
    setResearchGraphDepth: vi.fn(),
    getResearchNoteFocusId: () => '',
    setResearchNoteFocusId: vi.fn(),
    $: (id: string) => document.getElementById(id)!,
    getCurrentRun: () => run,
    getActiveId: () => 'chat',
    getResearchDrawerTab: () => tab,
    setResearchDrawerTab: (value: string) => {
      tab = value;
    },
    copyText,
  });
});

it('keeps evidence stance, excerpt, and locator distinct, with a keyboard-accessible source', () => {
  run.evidenceItems = [
    {
      claim: 'A result',
      stance: 'contradicts',
      page: 12,
      source_url: 'https://example.org/report.pdf',
      excerpt: 'Exact passage',
    },
    { claim: 'Legacy record', source_url: 'javascript:alert(1)' },
  ];
  run.counterpoints = ['Limited sample'];
  run.qualityWarnings = ['Missing metadata'];
  ResearchDrawer.render('chat');

  const [result, legacy] = document.querySelectorAll('.research-card');
  expect(result.querySelector('.research-stance')?.textContent).toBe('Contradicts');
  expect(result.querySelector('blockquote')?.textContent).toBe('Exact passage');
  expect(result.querySelector('.research-card-meta span')?.textContent).toBe('p.12');
  expect(result.querySelector('a')?.href).toBe('https://example.org/report.pdf');
  expect(legacy.querySelector('.research-stance')?.textContent).toBe('Stance unknown');
  expect(legacy.querySelector('a')).toBeNull();
  expect(document.querySelector('#research-body')?.textContent).toContain('Counter-evidence / limitations');
  expect(document.querySelector('#research-body')?.textContent).toContain('Quality warnings');
});

it('groups source status using redirect aliases and keeps URL-only records unclassified', () => {
  tab = 'sources';
  run.citationMap = [{ number: 1, title: 'Cited paper', url: 'https://example.org/preprint' }];
  run.openedSources = [
    { requested_url: 'https://example.org/preprint', final_url: 'https://example.org/final.pdf', content_kind: 'pdf' },
    { title: 'Background', final_url: 'https://example.net/background' },
  ];
  ResearchDrawer.render('chat');

  const [cited, opened] = document.querySelectorAll<HTMLElement>('.research-source-section');
  expect(cited.querySelector('h3')?.textContent).toBe('Cited in report');
  expect(cited.querySelector('a')?.href).toBe('https://example.org/final.pdf');
  expect(cited.textContent).toContain('PDF');
  expect(opened.querySelector('h3')?.textContent).toBe('Opened only');
  expect(opened.querySelectorAll('a')).toHaveLength(1);
  expect(opened.textContent).toContain('Background');
  cited.querySelector<HTMLButtonElement>('[aria-label="Copy citation"]')?.click();
  expect(copyText).toHaveBeenCalledWith('[1] Cited paper - https://example.org/final.pdf');

  run.citationMap = [];
  run.openedSources = [];
  run.sourceUrls = ['https://example.org/legacy'];
  ResearchDrawer.render('chat');
  expect(document.querySelector('.research-source-section h3')?.textContent).toBe('Recorded links');
});
