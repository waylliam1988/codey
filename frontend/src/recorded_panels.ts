import { requestJson } from './api/transport';
import { commandCard } from './command_card';
import { iconButton } from './icon_button';
import type { Message } from './types/domain';
import type { DocumentRef } from './document_pane';

export interface RecordedPanelDeps {
  sessionId(): string;
  messages(): Message[];
  openDocument(session: string, ref: DocumentRef): void;
  copy(text: string): Promise<boolean>;
}
export class RecordedPanels {
  readonly review = document.createElement('section');
  readonly outputs = document.createElement('section');
  private generation = 0;
  constructor(private deps: RecordedPanelDeps) {
    this.review.className = this.outputs.className = 'recorded-panel';
  }
  async showReview(target: Record<string, string>): Promise<void> {
    const generation = ++this.generation;
    const session = this.deps.sessionId();
    const run = target.runId || [...this.deps.messages()].reverse().find((message) => message.runId)?.runId || '';
    const body = document.createElement('div');
    body.className = 'workspace-content';
    body.textContent = run ? 'Loading recorded review…' : 'No recorded review for this conversation.';
    this.review.replaceChildren(body);
    if (!run) return;
    try {
      const data = await requestJson<{
        ok: boolean;
        available: boolean;
        review?: {
          verdict?: string;
          status?: string;
          summary?: string;
          scope?: unknown;
          findings?: Array<{
            path?: string;
            new_line?: number;
            old_line?: number;
            issue?: string;
            suggested_fix?: string;
          }>;
        };
      }>(`/api/run_review?${new URLSearchParams({ session_id: session, run_id: run })}`);
      if (generation !== this.generation || this.deps.sessionId() !== session) return;
      if (!data.ok) throw new Error('Could not load recorded review');
      body.replaceChildren();
      const refresh = iconButton('Refresh recorded review', 'reload', () => {
        void this.showReview({ runId: run });
      });
      body.append(refresh);
      const heading = document.createElement('h3');
      heading.textContent = 'Recorded review';
      body.append(heading);
      if (!data.available || !data.review) {
        body.append(document.createTextNode('No review was recorded for this run.'));
        return;
      }
      const review = data.review;
      const status = document.createElement('p');
      status.textContent = [review.status, review.verdict].filter(Boolean).join(' · ');
      const summary = document.createElement('p');
      summary.textContent = review.summary || 'No summary recorded.';
      body.append(status, summary);
      for (const finding of review.findings || []) {
        const row = document.createElement('section');
        row.className = 'review-finding';
        const path = document.createElement('button');
        path.type = 'button';
        const line = finding.new_line;
        path.textContent = `${finding.path || 'Unknown location'}${line ? `:${line}` : ''}`;
        path.disabled = !finding.path;
        path.onclick = () =>
          this.deps.openDocument(session, { kind: 'workspace', path: finding.path!, ...(line ? { line } : {}) });
        const issue = document.createElement('p');
        issue.textContent = finding.issue || '';
        row.append(path, issue);
        if (finding.suggested_fix) {
          const fix = document.createElement('p');
          fix.textContent = finding.suggested_fix;
          row.append(fix);
        }
        body.append(row);
      }
      if (review.scope) {
        const details = document.createElement('details');
        const title = document.createElement('summary');
        title.textContent = 'Recorded scope';
        const scope = document.createElement('pre');
        scope.textContent = JSON.stringify(review.scope, null, 2);
        details.append(title, scope);
        body.append(details);
      }
    } catch (error) {
      if (generation !== this.generation || this.deps.sessionId() !== session) return;
      body.textContent = error instanceof Error ? error.message : 'Recorded review unavailable';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = 'Retry';
      retry.onclick = () => {
        void this.showReview({ runId: run });
      };
      body.append(retry);
    }
  }
  showOutputs(): void {
    const body = document.createElement('div');
    body.className = 'workspace-content';
    const title = document.createElement('h3');
    title.textContent = 'Command output';
    body.append(
      title,
      iconButton('Refresh command output', 'reload', () => this.showOutputs()),
    );
    const commands = this.deps
      .messages()
      .filter((message) => message.type === 'shell_result' || (message.executionId && message.command));
    const latest = new Map<string, Message>();
    commands
      .slice(-100)
      .forEach((message, index) =>
        latest.set(
          message.executionId ? `${message.runId}:${message.executionId}` : message.id || String(index),
          message,
        ),
      );
    for (const message of latest.values()) {
      body.append(commandCard({ ...message, sessionId: message.sessionId || this.deps.sessionId() }, this.deps.copy));
    }
    if (!commands.length) body.append(document.createTextNode('No command output recorded for this conversation.'));
    this.outputs.replaceChildren(body);
  }
}
