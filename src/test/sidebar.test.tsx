import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createMockClient } from '../api/client';
import { renderApp, twoChatSeed } from './fixtures';

const chatList = () => screen.findByRole('navigation', { name: 'Chat list' });
const chatNames = async () =>
  within(await chatList())
    .getAllByRole('button')
    .map((button) => button.textContent);
const selectedChat = async () =>
  within(await chatList())
    .getAllByRole('button')
    .find((button) => button.getAttribute('aria-current') === 'true')?.textContent;

describe('chat sidebar', () => {
  it('lists chats most recent first and moves the highlight on selection', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));

    expect(await chatNames()).toEqual(['Budget', 'Travel']);
    expect(await selectedChat()).toBe('Budget');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Budget');

    await user.click(screen.getByRole('button', { name: 'Travel' }));

    expect(await selectedChat()).toBe('Travel');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Travel');
  });

  it('shows the empty state, then creates and selects a new chat', async () => {
    const { user } = renderApp(createMockClient({ seed: { chats: [], messages: [] } }));

    expect(await screen.findByText('No chats yet')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'New chat' }));

    expect(await selectedChat()).toBe('New chat');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('New chat');
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

  it('puts a new chat at the top of an existing list', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed() }));
    await chatList();

    await user.click(screen.getByRole('button', { name: 'New chat' }));

    expect(await selectedChat()).toBe('New chat');
    expect(await chatNames()).toEqual(['New chat', 'Budget', 'Travel']);
  });
});
