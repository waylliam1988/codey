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

describe('protocol decoders', () => {
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
    expect(() => decodeRunState({ run_id: 42 })).toThrow('run_id');
    expect(() => decodeRunState({ research_restore_runs: [42] })).toThrow('research_restore_runs');
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
