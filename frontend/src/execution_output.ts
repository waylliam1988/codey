export interface OutputSnapshot {
  stdout: string;
  total_bytes: number;
  omitted_bytes: number;
  truncated: boolean;
}

export interface ExecutionSnapshot {
  revision: number;
  execution_state: 'running' | 'settled' | 'unknown';
  stdout: string;
  stderr?: string;
}

export interface ExecutionOutputScope {
  execution_id: string;
  session_id: string;
  run_id: string;
}

interface RetainedExecutionOutput extends ExecutionSnapshot {
  truncated: boolean;
  exit_code?: number | null;
}

function outputText(value: unknown): string {
  if (typeof value === 'string') return value;
  return value && typeof value === 'object' && 'text' in value && typeof value.text === 'string' ? value.text : '';
}

export async function fetchExecutionOutput(
  scope: ExecutionOutputScope,
  afterRevision = -1,
): Promise<RetainedExecutionOutput | undefined> {
  const query = new URLSearchParams({ session_id: scope.session_id, run_id: scope.run_id });
  if (afterRevision >= 0) query.set('after_revision', String(afterRevision));
  const response = await fetch(`/api/executions/${encodeURIComponent(scope.execution_id)}/output?${query}`);
  if (response.status === 304) return undefined;
  if (!response.ok) throw new Error('Execution output unavailable');
  const data: unknown = await response.json();
  if (!data || typeof data !== 'object' || !('revision' in data) || typeof data.revision !== 'number')
    throw new Error('Execution output invalid');
  const stdout = 'stdout' in data ? data.stdout : undefined;
  const stderr = 'stderr' in data ? data.stderr : undefined;
  const state = 'execution_state' in data ? data.execution_state : undefined;
  const exit = 'exit_code' in data ? data.exit_code : undefined;
  return {
    revision: data.revision,
    execution_state: state === 'settled' || state === 'unknown' ? state : 'running',
    stdout: outputText(stdout),
    stderr: outputText(stderr),
    truncated: [stdout, stderr].some(
      (value) => !!value && typeof value === 'object' && 'truncated' in value && value.truncated === true,
    ),
    ...(typeof exit === 'number' || exit === null ? { exit_code: exit } : {}),
  };
}

export class BoundedOutput {
  private readonly decoder = new TextDecoder('utf-8', { fatal: false });
  private readonly chunks: string[] = [];
  private total = 0;
  constructor(private readonly limit: number) {}
  append(bytes: Uint8Array): void {
    this.total += bytes.byteLength;
    this.chunks.push(this.decoder.decode(bytes, { stream: true }));
    const text = this.chunks.join('');
    if (text.length > this.limit * 2)
      this.chunks.splice(0, this.chunks.length, text.slice(0, this.limit) + text.slice(-this.limit));
  }
  snapshot(): OutputSnapshot {
    const text = this.chunks.join('');
    const visible =
      text.length <= this.limit
        ? text
        : text.slice(0, Math.floor(this.limit / 2)) + text.slice(-Math.ceil(this.limit / 2));
    return {
      stdout: visible,
      total_bytes: this.total,
      omitted_bytes: Math.max(0, this.total - new TextEncoder().encode(visible).byteLength),
      truncated: visible !== text,
    };
  }
}

export function mergeOutputSnapshot(current: ExecutionSnapshot, incoming: ExecutionSnapshot): ExecutionSnapshot {
  if (current.execution_state === 'settled' && incoming.execution_state !== 'settled') return current;
  if (incoming.revision <= current.revision) return current;
  return { ...incoming, stderr: incoming.stderr || '' };
}
