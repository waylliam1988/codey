import { afterEach, expect, it, vi } from 'vitest';
import { UserQuestions } from './user_questions';

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});
const question = {
  id: 'q',
  session_id: 'chat',
  run_id: 'run_source',
  revision: 1,
  state: 'pending',
  question: '检查范围？',
  options: ['只收集', '运行测试'],
  descriptions: ['不执行测试', '随后单独审批'],
  allow_free_text: true,
};

it('keeps a focused answer field when an unrelated question changes', () => {
  const questions = new UserQuestions({ observe: vi.fn(), changed: vi.fn() });
  questions.apply(question);
  const card = questions.card('q');
  document.body.append(card);
  const field = card.querySelector('textarea')!;
  questions.apply({ ...question, id: 'other', session_id: 'other' });
  expect(card.querySelector('textarea')).toBe(field);
});

it('submits the selected answer once and does not authorize a command', async () => {
  let finish: (value: unknown) => void = () => {};
  const send = vi.fn((_url: string, _init: RequestInit) => new Promise((resolve) => (finish = resolve)));
  vi.stubGlobal('fetch', send);
  const questions = new UserQuestions({ observe: vi.fn(), changed: vi.fn() });
  questions.apply(question);
  const card = questions.card('q');
  document.body.append(card);
  card.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  expect(card.querySelector<HTMLButtonElement>('[type=submit]')!.disabled).toBe(true);
  card.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  expect(send).toHaveBeenCalledTimes(1);
  const body = JSON.parse(String(send.mock.calls[0][1].body));
  expect(body.answer).toBe('只收集');
  expect(body.approved).toBeUndefined();
  finish({
    ok: true,
    json: async () => ({ question: { ...question, state: 'answered', revision: 3, answer: '只收集' } }),
  });
  await vi.waitFor(() => expect(card.textContent).toContain('Answered · 只收集'));
});

it('locks an unconfirmed answer and checks status instead of resending it', async () => {
  const send = vi
    .fn()
    .mockRejectedValueOnce(new TypeError('reply lost'))
    .mockResolvedValueOnce({
      ok: true,
      json: async () => ({ question: { ...question, state: 'answered', revision: 3, answer: '只收集' } }),
    });
  vi.stubGlobal('fetch', send);
  const questions = new UserQuestions({ observe: vi.fn(), changed: vi.fn() });
  questions.apply(question);
  const card = questions.card('q');
  document.body.append(card);
  card.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(card.textContent).toContain('Answer could not be confirmed'));
  card.querySelector<HTMLButtonElement>('[data-check-question]')!.click();
  await vi.waitFor(() => expect(card.textContent).toContain('Answered'));
  expect(send.mock.calls[1][0]).toBe('/api/questions?id=q');
});
