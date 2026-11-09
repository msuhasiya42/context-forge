import type { Ref } from 'react';
import type { Chat } from '../../api/types';
import { useDeleteConfirmation } from '../../hooks/useDeleteConfirmation';
import controls from '../../styles/controls.module.css';
import type { AsyncState } from '../../utils/asyncState';
import { chatDisplayName } from '../../utils/chats';
import { classNames } from '../../utils/classNames';
import { AsyncContent } from '../AsyncContent/AsyncContent';
import { ConfirmDialog } from '../ConfirmDialog/ConfirmDialog';
import { ChatList } from './ChatList';
import { SidebarHeader } from './SidebarHeader';
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
  isInNewChat: boolean;
  onRename: (chatId: string, title: string) => Promise<boolean>;
  onArchive: (chatId: string) => Promise<boolean>;
  actionError: string | null;
  onRetry: () => void;
}

export function ChatSidebar(props: ChatSidebarProps) {
  const deletion = useDeleteConfirmation(props.onArchive);
  return (
    <aside id={props.id} className={classNames(styles.sidebar, props.isOpen && styles.open)}>
      <SidebarHeader
        sidebarId={props.id}
        isOpen={props.isOpen}
        onClose={props.onClose}
        closeButtonRef={props.closeButtonRef}
        onNewChat={props.onNewChat}
        isCreating={props.isCreating}
        isInNewChat={props.isInNewChat}
      />
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
          <ChatList
            chats={chats}
            selectedChatId={props.selectedChatId}
            onSelect={props.onSelect}
            onRename={props.onRename}
            onRequestDelete={deletion.request}
          />
        )}
      </AsyncContent>
      {deletion.pending && (
        <ConfirmDialog
          title="Delete chat?"
          message={`"${chatDisplayName(deletion.pending)}" will be removed from your chats.`}
          confirmLabel="Delete"
          isBusy={deletion.isDeleting}
          onConfirm={() => void deletion.confirm()}
          onCancel={deletion.cancel}
        />
      )}
    </aside>
  );
}
