import { expect, it } from 'vitest';
import './conversation_ui';

it('allows provider boot callbacks before conversation dependencies are initialized', () => {
  expect(() => window.ConversationUI.updateNotice()).not.toThrow();
  expect(() => window.ConversationUI.updateSearch()).not.toThrow();
  expect(() => window.ConversationUI.renderChat()).not.toThrow();
});
