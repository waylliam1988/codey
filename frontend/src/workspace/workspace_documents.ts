import { requestResponse } from '../api/transport';
import { isRecord } from '../domain/protocol';
import { Render } from '../render/render';
import { DocumentPane, type DocumentRef } from './document_pane';
import { hydrateDocumentContent } from './document_content';
import type { Workspace } from './workspace';

export function createWorkspaceDocuments(workspace: Workspace, activeSessionId: () => string) {
  const eventElement = (event: Event): Element | null => (event.target instanceof Element ? event.target : null);
  const documentPane = new DocumentPane({
    docked: true,
    visibility: (visible) => (visible ? workspace.reveal('files') : workspace.panelClosed('files')),
    read: async (sessionId, ref, workspace) => {
      const response = await requestResponse('/api/documents/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sessionId, expected_workspace: workspace, ...ref }),
      });
      const data: unknown = await response.json();
      if (!isRecord(data)) throw new Error('Invalid document response');
      if (!response.ok || data.ok !== true)
        throw new Error(
          typeof data.message === 'string'
            ? data.message
            : typeof data.error === 'string'
              ? data.error
              : 'Could not load document',
        );
      const { content, path, version, title, format, workspace: scope } = data;
      if (
        typeof content !== 'string' ||
        typeof path !== 'string' ||
        typeof version !== 'string' ||
        typeof title !== 'string' ||
        typeof format !== 'string' ||
        typeof scope !== 'string'
      )
        throw new Error('Invalid document response');
      return { content, path, version, title, format, workspace: scope };
    },
    markdown: (container, text, scope) => {
      Render.renderMarkdown(container, text);
      if (scope)
        void hydrateDocumentContent(container, {
          sessionId: scope.sessionId,
          workspace: scope.workspace.startsWith('research:') ? undefined : scope.workspace,
          parent: scope.path.split('/').slice(0, -1).join('/'),
        });
    },
    copy: (text) => Render.copyText(text),
  });
  workspace.register('files', 'Files', documentPane.element, () => documentPane.show(activeSessionId()));
  function openDocument(sessionId: string, ref: DocumentRef): void {
    void documentPane.open(sessionId, ref);
  }
  function previewHtml(sessionId: string, path: string, project: string): void {
    if (sessionId === activeSessionId()) {
      void documentPane.open(
        sessionId,
        { kind: 'workspace', path },
        { workspace: project || undefined, preview: true },
      );
    }
  }
  document.addEventListener('click', (event) => {
    const link = eventElement(event)?.closest<HTMLElement>('[data-document-path]');
    if (!link) return;
    event.preventDefault();
    const path = link.dataset.documentPath;
    if (path) {
      const line = Number(link.dataset.documentLine) || undefined;
      openDocument(
        activeSessionId(),
        link.closest('.document-pane') ? documentPane.fileLink(path, line) : { kind: 'workspace', path, line },
      );
    }
  });

  return { documentPane, openDocument, previewHtml };
}
