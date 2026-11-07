import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createMockClient } from '../api/client';
import { renderApp, twoChatSeed } from './fixtures';

const chatList = () => screen.findByRole('navigation', { name: 'Chat list' });
// The first button of each row selects the chat; the others are its row actions.
const chatButtons = async () =>
  within(await chatList())
    .getAllByRole('listitem')
    .map((row) => within(row).getAllByRole('button')[0]);
const chatNames = async () => (await chatButtons()).map((button) => button?.textContent);
const selectedChat = async () =>
  (await chatButtons()).find((button) => button?.getAttribute('aria-current') === 'true')
    ?.textContent;
const heading = () => screen.getByRole('heading', { level: 1 });

describe('chat sidebar', () => {
  it('lists chats most recent first and moves the highlight on selection', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));

    expect(await chatNames()).toEqual(['Budget', 'Travel']);
    expect(await selectedChat()).toBe('Budget');
    expect(heading()).toHaveTextContent('Budget');

    await user.click(screen.getByRole('button', { name: 'Travel' }));

    expect(await selectedChat()).toBe('Travel');
    expect(heading()).toHaveTextContent('Travel');
  });

  it('shows the empty state, then creates and selects a new chat', async () => {
    const { user } = renderApp(createMockClient({ seed: { chats: [], messages: [] } }));

    expect(await screen.findByText('No chats yet')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'New chat' }));

    expect(await selectedChat()).toBe('New chat');
    expect(heading()).toHaveTextContent('New chat');
    expect(await screen.findByText('Start the conversation')).toBeInTheDocument();
  });

  it('moves focus into the narrow-screen sidebar, and Escape closes it and restores focus', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));
    await chatList();
    const main = screen.getByRole('main');
    const showChats = screen.getByRole('button', { name: 'Show chats' });

    await user.click(showChats);
    expect(screen.getByRole('button', { name: 'Hide chats' })).toHaveFocus();
    expect(main).toHaveAttribute('inert');

    await user.keyboard('{Escape}');
    expect(showChats).toHaveFocus();
    expect(showChats).toHaveAttribute('aria-expanded', 'false');
    expect(main).not.toHaveAttribute('inert');
  });

  it('keeps one empty new chat: New chat twice yields one chat and a disabled button', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));
    await chatList();
    const newChat = screen.getByRole('button', { name: 'New chat' });

    await user.click(newChat);
    expect(await selectedChat()).toBe('New chat');
    await screen.findByText('Start the conversation');
    await user.click(newChat);

    expect(await chatNames()).toEqual(['New chat', 'Budget', 'Travel']);
    expect(newChat).toHaveAttribute('aria-disabled', 'true');
    expect(newChat).toHaveAccessibleDescription("You're already in a new chat");

    await user.click(screen.getByRole('button', { name: 'Budget' }));
    await user.click(newChat);
    expect(await selectedChat()).toBe('New chat');
    expect(await chatNames()).toHaveLength(3);
  });

  it('renames a chat from the keyboard and rejects an empty name', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));
    await chatList();

    screen.getByRole('button', { name: 'Rename Budget' }).focus();
    await user.keyboard('{Enter}');
    const input = screen.getByRole('textbox', { name: 'Chat name' });
    expect(input).toHaveValue('Budget');
    await user.clear(input);
    await user.keyboard('{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a name for the chat.');
    await user.type(input, 'Household budget{Enter}');

    expect(await selectedChat()).toBe('Household budget');
    expect(heading()).toHaveTextContent('Household budget');
    expect(screen.getByRole('button', { name: 'Rename Household budget' })).toHaveFocus();
  });

  it('deletes a chat after confirmation and selects the next most recent chat', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));
    await chatList();

    await user.click(screen.getByRole('button', { name: 'Delete Budget' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete chat?' });
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Delete Budget' }));
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await chatNames()).toEqual(['Travel']);
    expect(await selectedChat()).toBe('Travel');
    expect(heading()).toHaveTextContent('Travel');
  });
});
