import { expect, it } from 'vitest';
import { ConversationUI } from './conversation_ui';

it('allows provider boot callbacks before conversation dependencies are initialized', () => {
  expect(() => ConversationUI.updateNotice()).not.toThrow();
  expect(() => ConversationUI.updateSearch()).not.toThrow();
  expect(() => ConversationUI.renderChat()).not.toThrow();
});
