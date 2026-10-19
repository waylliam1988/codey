import { expect, it, vi } from 'vitest';
import './render';
import './process';
import './messages';
import './research_runs';
import type { Session } from './types/domain';

it('edits a user message inline and submits the edited text', async () => {
  const editMessage = vi.fn().mockResolvedValue(true);
  window.Composer = { editMessage } as unknown as typeof window.Composer;
  window.Messages.init({ getActiveId: () => 'chat', getRunningSessionId: () => null } as Parameters<
    typeof window.Messages.init
  >[0]);
  const host = document.createElement('div');
  window.Messages.append(host, { type: 'user', id: 'u', text: '原始请求' });
  host.querySelector<HTMLButtonElement>('.msg-edit')!.click();
  const input = host.querySelector<HTMLTextAreaElement>('.msg-edit-input')!;
  input.value = '修改后的请求';
  host.querySelector<HTMLButtonElement>('.msg-edit-send')!.click();
  await vi.waitFor(() => expect(editMessage).toHaveBeenCalledWith('chat', '修改后的请求'));
  expect(host.querySelector('.msg-edit-input')).not.toBeNull();
});

it('cancels inline message editing without changing the original', () => {
  window.Composer = { editMessage: vi.fn() } as unknown as typeof window.Composer;
  window.Messages.init({ getActiveId: () => 'chat', getRunningSessionId: () => null } as Parameters<
    typeof window.Messages.init
  >[0]);
  const host = document.createElement('div');
  window.Messages.append(host, { type: 'user', id: 'u', text: '原始请求' });
  host.querySelector<HTMLButtonElement>('.msg-edit')!.click();
  host.querySelector<HTMLButtonElement>('.msg-edit-editor .text-btn')!.click();
  expect(host.querySelector('.body')?.textContent).toBe('原始请求');
  expect(host.querySelector('.msg-edit-input')).toBeNull();
});

it('retains an interrupted response locally and removes that notice on final settlement', () => {
  window.Messages.init({
    getActiveId: () => 'chat',
    getRunningSessionId: () => null,
    currentProjectPath: () => '',
  } as Parameters<typeof window.Messages.init>[0]);
  const host = document.createElement('div');
  window.Messages.append(host, {
    type: 'asst',
    runId: 'r',
    streamId: 's',
    streamState: 'interrupted',
    text: 'retained partial',
  });
  expect(host.textContent).toContain('Response interrupted · partial text retained');
  expect(host.querySelector('.body')?.textContent).toBe('retained partial');
  expect([...host.querySelectorAll('button')].some((button) => /retry/i.test(button.textContent || ''))).toBe(false);
  window.Messages.updateReply(host, {
    type: 'asst',
    runId: 'r',
    streamId: 's',
    streamState: 'complete',
    finalAnswer: true,
    text: 'complete answer',
  });
  expect(host.querySelector('.reply-stream-note')).toBeNull();
  expect(host.querySelectorAll('.msg-copy')).toHaveLength(1);
});

it('opens the research report belonging to the message run', () => {
  const sessions: Session[] = [
    {
      id: 'chat',
      title: 'Chat',
      draft: { text: '', start: 0, end: 0, direction: 'none', revision: 0 },
      messages: [],
      researchRuns: [
        {
          runId: 'run_a',
          synthesisId: 'note_a',
          restoreable: false,
          notesCreated: [],
          notesUpdated: [],
          sourceUrls: [],
          queries: [],
          searchResults: [],
          openedSources: [],
          coverage: {},
          citationMap: [],
          evidenceItems: [],
          counterpoints: [],
          qualityWarnings: [],
        },
        {
          runId: 'run_b',
          synthesisId: 'note_b',
          restoreable: false,
          notesCreated: [],
          notesUpdated: [],
          sourceUrls: [],
          queries: [],
          searchResults: [],
          openedSources: [],
          coverage: {},
          citationMap: [],
          evidenceItems: [],
          counterpoints: [],
          qualityWarnings: [],
        },
      ],
    },
  ];
  const openDocument = vi.fn();
  window.ResearchRuns.init({ getSessions: () => sessions });
  window.Messages.init({
    getActiveId: () => 'chat',
    getRunningSessionId: () => null,
    openDocument,
    attachCurrentChatToPickedProject: vi.fn(),
    statusRow: () => document.createElement('div'),
    currentProjectPath: () => '',
    openChangesDrawer: vi.fn(),
    approveCommand: vi.fn(),
    continueTask: vi.fn(),
    resumeTeaching: vi.fn(),
  } as Parameters<typeof window.Messages.init>[0]);
  const host = document.createElement('div');

  window.Messages.append(host, { type: 'research_done', sessionId: 'chat', runId: 'run_a', text: 'Research A' });
  host.querySelector<HTMLButtonElement>('.artifact-open')!.click();

  expect(openDocument).toHaveBeenCalledWith('chat', { kind: 'research', note_id: 'note_a' });
});
