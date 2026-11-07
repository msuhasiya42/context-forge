import type { Chat } from '../api/types';
import { timestampOf, upsertBy } from './collections';

export const UNTITLED_CHAT_NAME = 'New chat';

export function chatDisplayName(chat: Chat): string {
  return chat.title.trim() || UNTITLED_CHAT_NAME;
}

export function isUntitled(chat: Chat): boolean {
  return chat.title.trim() === '';
}

export function sortChatsByRecent(chats: readonly Chat[]): Chat[] {
  return [...chats].sort((a, b) => timestampOf(b.updatedAt) - timestampOf(a.updatedAt));
}

export function upsertChat(chats: readonly Chat[], chat: Chat): Chat[] {
  return upsertBy(chats, chat, (value) => value.chatId);
}

/** The explicitly selected chat, or the most recent one when nothing valid is selected. */
export function pickActiveChat(
  sortedChats: readonly Chat[],
  selectedId: string | null,
): Chat | null {
  return sortedChats.find((chat) => chat.chatId === selectedId) ?? sortedChats[0] ?? null;
}
