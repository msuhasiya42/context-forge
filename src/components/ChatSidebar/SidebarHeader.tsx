import type { Ref } from 'react';
import { SidebarToggle } from '../SidebarToggle/SidebarToggle';
import { NewChatButton } from './NewChatButton';
import styles from './ChatSidebar.module.css';

interface SidebarHeaderProps {
  sidebarId: string;
  isOpen: boolean;
  onClose: () => void;
  closeButtonRef: Ref<HTMLButtonElement>;
  onNewChat: () => void;
  isCreating: boolean;
  isInNewChat: boolean;
}

export function SidebarHeader(props: SidebarHeaderProps) {
  return (
    <header className={styles.header}>
      <h2 className={styles.title}>Chats</h2>
      <NewChatButton
        onClick={props.onNewChat}
        isCreating={props.isCreating}
        isInNewChat={props.isInNewChat}
      />
      <SidebarToggle
        ref={props.closeButtonRef}
        sidebarId={props.sidebarId}
        isOpen={props.isOpen}
        action="hide"
        onClick={props.onClose}
      />
    </header>
  );
}
