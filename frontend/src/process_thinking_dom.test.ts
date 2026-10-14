import { beforeEach, describe, expect, it, vi } from 'vitest';

describe('process thinking DOM', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="chat"></div>';
    window.Render = {
      renderMarkdown: vi.fn((element: HTMLElement, text: string) => {
        element.textContent = text;
      }),
      addMessageCopyButton: vi.fn((element: HTMLElement) => {
        const button = document.createElement('button');
        button.className = 'copy-message';
        element.append(button);
      }),
    } as unknown as typeof window.Render;
  });

  it('renders summary, thinking body, and copy button inside the process details', async () => {
    await import('./process');
    const chat = document.querySelector<HTMLElement>('#chat')!;
    window.Process.append(chat, { type: 'thinking', runId: 'run-1', reasoning: 'Inspect the source.' }, vi.fn());

    const details = chat.querySelector<HTMLDetailsElement>('.thinking');
    expect(details).not.toBeNull();
    expect(details?.querySelector('summary')?.textContent).toBe('Thinking');
    expect(details?.querySelector('.thinking-body')?.textContent).toContain('Inspect the source.');
    expect(details?.querySelector('.copy-message')).not.toBeNull();
  });
});
