declare module 'markdown-it-task-lists' {
  import type MarkdownIt from 'markdown-it';
  const taskLists: (parser: MarkdownIt, options?: { enabled?: boolean }) => void;
  export default taskLists;
}
