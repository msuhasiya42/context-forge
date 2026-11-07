import type { ApiClient } from './api/client';
import { ChatPane } from './components/ChatPane/ChatPane';
import { PaneContent } from './components/ChatPane/PaneContent';
import { ChatSidebar } from './components/ChatSidebar/ChatSidebar';
import { SidebarToggle } from './components/SidebarToggle/SidebarToggle';
import { useChatApp } from './hooks/useChatApp';
import { useDisclosure } from './hooks/useDisclosure';
import { DEFAULT_POLL_INTERVAL_MS } from './hooks/useMessages';
import { chatDisplayName } from './utils/chats';
import styles from './App.module.css';

const SIDEBAR_ID = 'chat-sidebar';
const APP_NAME = 'ContextForge';

export interface AppProps {
  client: ApiClient;
  /** How often to refresh a chat while a request is running (stand-in for SSE). */
  pollIntervalMs?: number;
}

export function App({ client, pollIntervalMs = DEFAULT_POLL_INTERVAL_MS }: AppProps) {
  const app = useChatApp(client, pollIntervalMs);
  const sidebar = useDisclosure();
  const { activeChat, messages } = app;

  return (
    <div className={styles.layout}>
      <ChatSidebar
        id={SIDEBAR_ID}
        isOpen={sidebar.isOpen}
        onClose={sidebar.close}
        chats={app.chats.state}
        selectedChatId={activeChat?.chatId ?? null}
        onSelect={(chatId) => {
          app.selectChat(chatId);
          sidebar.close();
        }}
        onNewChat={() => {
          sidebar.close();
          void app.startNewChat();
        }}
        isCreating={app.actions.isCreating}
        actionError={app.actions.actionError}
        onRetry={() => void app.chats.reload()}
      />
      <ChatPane
        title={activeChat ? chatDisplayName(activeChat) : APP_NAME}
        leading={
          <SidebarToggle sidebarId={SIDEBAR_ID} isOpen={sidebar.isOpen} onToggle={sidebar.open} />
        }
      >
        <PaneContent
          activeChat={activeChat}
          chatsLoaded={app.chats.state.status === 'success'}
          messages={messages.state}
          onRetryMessages={() => void messages.reload()}
          onSend={app.sendMessage}
          isSending={messages.isSending}
          sendError={messages.sendError}
        />
      </ChatPane>
    </div>
  );
}
