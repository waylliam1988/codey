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
