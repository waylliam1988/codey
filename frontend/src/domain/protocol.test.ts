import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  decodeChangeFilePreview,
  decodeChangesData,
  decodeProviderCatalog,
  decodeProviderConfig,
  decodeProviderStatus,
  decodeRunState,
  decodeServerEvent,
} from './protocol';

const fixtureRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../../tests/fixtures/protocol');
const fixture = <T>(name: string): T => JSON.parse(readFileSync(resolve(fixtureRoot, name), 'utf8')) as T;

describe('protocol decoders', () => {
  it('validates output truncation as a boolean and preserves a true flag', () => {
    expect(() => decodeServerEvent({ type: 'shell_result', truncated: 'false' })).toThrow();
    expect(decodeServerEvent({ type: 'shell_result', truncated: true }).truncated).toBe(true);
  });
  it('decodes the shared task and research event fixtures without dropping fields', () => {
    const taskDone = fixture<Record<string, any>>('task_done.json');
    const research = fixture<Record<string, Record<string, any>>>('research.json');
    expect(decodeServerEvent(taskDone)).toMatchObject({
      type: 'task_done',
      summary: taskDone.summary,
      receipt: taskDone.receipt,
      final_delivery: 'success',
    });
    for (const mode of ['pure', 'hybrid'] as const) {
      const event = decodeServerEvent(research[mode]);
      expect(event).toMatchObject({ type: 'task_done', mode: research[mode].mode, research: research[mode].research });
    }
  });

  it('decodes the shared approval, shell result, and run state fixtures', () => {
    const approval = fixture<Record<string, any>>('approval.json');
    const shellResult = fixture<Record<string, any>>('shell_result.json');
    const runState = fixture<Record<string, any>>('run_state.json');
    expect(decodeServerEvent(approval)).toMatchObject({
      type: 'shell_request',
      id: 'approval-1',
      command: approval.command,
    });
    expect(decodeServerEvent(shellResult)).toMatchObject({ type: 'shell_result', approved: true, exit_code: 0 });
    expect(decodeRunState(runState)).toMatchObject({
      busy: true,
      run_id: runState.run_id,
      run_status: 'awaiting_approval',
      research_restore_runs: ['run-research-pure-1'],
      pending_event: { type: 'shell_request', id: 'approval-1' },
    });
  });

  it('decodes the shared list, truncated, and file preview changes fixtures', () => {
    const changes = fixture<Record<string, any>>('changes.json');
    expect(decodeChangesData(changes.list)).toMatchObject({
      mode: 'git',
      changed_count: 2,
      files: changes.list.files,
    });
    expect(decodeChangesData(changes.truncated)).toMatchObject({ truncated: true, changed_count: 3 });
    expect(decodeChangeFilePreview(changes.file_preview)).toEqual(changes.file_preview);
  });

  it('normalizes a dynamic provider catalog without hardcoded providers', () => {
    expect(
      decodeProviderCatalog({
        providers: [
          { id: 'local', label: 'Local' },
          { id: 'custom', label: 'Custom' },
        ],
        default: 'custom',
      }),
    ).toEqual({
      providers: [
        { id: 'local', label: 'Local' },
        { id: 'custom', label: 'Custom' },
      ],
      labels: { local: 'Local', custom: 'Custom' },
      defaultProvider: 'custom',
    });
  });

  it('rejects malformed protocol input', () => {
    expect(() => decodeProviderCatalog({ providers: [] })).toThrow('no providers');
    expect(() => decodeServerEvent({ type: '' })).toThrow('invalid');
  });

  it('rejects event fields with invalid scalar and nested types', () => {
    for (const data of [
      { type: 'turn', turn: 'one' },
      { type: 'task_start', session_id: {} },
      { type: 'reply', text: [] },
      { type: 'tool', changed: 'false' },
      { type: 'task_done', provider_failure: { action: [] } },
      { type: 'providers', providers: [{ id: 'local', available: 'false' }] },
    ])
      expect(() => decodeServerEvent(data)).toThrow();
  });

  it('rejects malformed research payloads before a terminal event can be applied', () => {
    expect(() =>
      decodeServerEvent({
        type: 'task_done',
        research: { notes_created: ['note-1', 42] },
      }),
    ).toThrow('research notes_created is invalid');
  });

  it('accepts a null exit code for events without a process exit status', () => {
    expect(decodeServerEvent({ type: 'task_done', exit_code: null }).exit_code).toBeNull();
  });

  it('decodes provider config and rejects malformed status payloads', () => {
    expect(decodeProviderConfig({ providers: [{ id: 'local', label: 'Local' }], recommended: 'local' })).toMatchObject({
      defaultProvider: 'local',
      recommendedProvider: 'local',
    });
    expect(decodeProviderStatus([{ id: 'local', available: true, state: 'ready' }])).toEqual([
      { id: 'local', available: true, state: 'ready' },
    ]);
    expect(() => decodeProviderStatus([{ id: 'local', available: 'yes' }])).toThrow('availability');
    expect(() => decodeProviderStatus([null])).toThrow('item');
  });

  it('accepts null optional run state events in idle and completed snapshots', () => {
    expect(
      decodeRunState({ busy: false, last_shell_result: null, last_terminal_event: null, pending_event: null }),
    ).toEqual({
      busy: false,
    });
    expect(decodeRunState({ busy: true, run_id: 'run-1', session_id: 'session-1', pending_event: null })).toMatchObject(
      {
        busy: true,
        run_id: 'run-1',
      },
    );
  });

  it('keeps explicit research restore availability at the protocol boundary', () => {
    expect(decodeServerEvent({ type: 'task_done', research: { restoreable: false } }).research?.restoreable).toBe(
      false,
    );
    expect(decodeServerEvent({ type: 'task_done', research: { restoreable: true } }).research?.restoreable).toBe(true);
  });

  it('rejects present run state fields with invalid types instead of treating them as idle', () => {
    expect(() => decodeRunState({ busy: 'false' })).toThrow('busy');
    expect(() => decodeRunState({ busy: false, run_id: 42 })).toThrow('run_id');
    expect(() => decodeRunState({ busy: false, research_restore_runs: [42] })).toThrow('research_restore_runs');
  });

  it('requires an explicit busy flag and run scope for busy snapshots', () => {
    expect(() => decodeRunState({})).toThrow('busy is required');
    expect(() => decodeRunState({ busy: true, run_id: '', session_id: '' })).toThrow('run and session IDs');
    expect(decodeRunState({ busy: false, run_id: '', session_id: '' })).toMatchObject({ busy: false });
  });

  it('preserves changes mode and metadata required by the drawer', () => {
    expect(
      decodeChangesData({
        ok: true,
        mode: 'git',
        changed_count: 1,
        project: 'demo',
        diff: 'diff --git a/a b/a',
        files: [{ path: 'a', status: 'M', additions: 1, deletions: 0 }],
      }),
    ).toMatchObject({ mode: 'git', changed_count: 1, project: 'demo' });
  });

  it('preserves aggregate truncation and explicit per-file preview states', () => {
    expect(decodeChangesData({ ok: true, diff: '', files: [], truncated: true })).toMatchObject({ truncated: true });
    expect(decodeChangeFilePreview({ ok: true, path: 'bundle.js', status: 'too_large', size: 184324 })).toEqual({
      ok: true,
      path: 'bundle.js',
      status: 'too_large',
      size: 184324,
    });
  });
});
