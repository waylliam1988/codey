import { isRecord } from '../domain/display_values';

export class ApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

export type Decoder<T> = (value: unknown) => T;

/** Keep status and streaming bodies available to endpoints that need them. */
export function requestResponse(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  return fetch(input, { credentials: 'same-origin', ...init });
}

export async function readJsonObject(response: Response): Promise<Record<string, unknown>> {
  const payload: unknown = await response.json();
  if (!isRecord(payload)) throw new Error('Invalid response object');
  return payload;
}

async function failure(response: Response): Promise<ApiError> {
  let payload: unknown = null;
  const text = await response.text();
  try {
    if (text) payload = JSON.parse(text);
  } catch {
    payload = text;
  }
  const detail =
    typeof payload === 'object' && payload !== null && 'error' in payload && typeof payload.error === 'string'
      ? payload.error
      : `HTTP ${response.status}`;
  return new ApiError(detail, response.status, payload);
}

export async function requestJson<T>(input: RequestInfo | URL, decode: Decoder<T>, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  const response = await requestResponse(input, { cache: 'no-store', ...init, headers });
  if (!response.ok) throw await failure(response);
  const payload: unknown = await response.json();
  return decode(payload);
}

export async function requestEmpty(input: RequestInfo | URL, init?: RequestInit): Promise<void> {
  const response = await requestResponse(input, init);
  if (!response.ok) throw await failure(response);
}

export function jsonBody(value: unknown): BodyInit {
  return JSON.stringify(value);
}
