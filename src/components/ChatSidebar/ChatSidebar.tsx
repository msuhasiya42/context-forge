import type { Ref } from 'react';
import type { Chat } from '../../api/types';
import controls from '../../styles/controls.module.css';
import type { AsyncState } from '../../utils/asyncState';
import { classNames } from '../../utils/classNames';
import { AsyncContent } from '../AsyncContent/AsyncContent';
import { SidebarToggle } from '../SidebarToggle/SidebarToggle';
import { ChatList } from './ChatList';
import styles from './ChatSidebar.module.css';

interface ChatSidebarProps {
  id: string;
  isOpen: boolean;
  onClose: () => void;
  /** Receives focus when the narrow-screen overlay opens. */
  closeButtonRef: Ref<HTMLButtonElement>;
  chats: AsyncState<Chat[]>;
  selectedChatId: string | null;
  onSelect: (chatId: string) => void;
  onNewChat: () => void;
  isCreating: boolean;
  actionError: string | null;
  onRetry: () => void;
}

export function ChatSidebar(props: ChatSidebarProps) {
  return (
    <aside id={props.id} className={classNames(styles.sidebar, props.isOpen && styles.open)}>
      <header className={styles.header}>
        <h2 className={styles.title}>Chats</h2>
        <button
          type="button"
          className={controls.primary}
          onClick={props.onNewChat}
          disabled={props.isCreating}
        >
          New chat
        </button>
        <SidebarToggle
          ref={props.closeButtonRef}
          sidebarId={props.id}
          isOpen={props.isOpen}
          action="hide"
          onClick={props.onClose}
        />
      </header>
      {props.actionError && (
        <div className={styles.errorSlot}>
          <p role="alert" className={controls.errorBanner}>
            {props.actionError}
          </p>
        </div>
      )}
      <AsyncContent
        state={props.chats}
        loadingText="Loading chats…"
        emptyText="No chats yet"
        onRetry={props.onRetry}
      >
        {(chats) => (
          <ChatList chats={chats} selectedChatId={props.selectedChatId} onSelect={props.onSelect} />
        )}
      </AsyncContent>
    </aside>
  );
}
