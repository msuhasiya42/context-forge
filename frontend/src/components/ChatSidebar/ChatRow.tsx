import { useRef } from 'react';
import type { Chat } from '../../api/types';
import type { InlineRename } from '../../hooks/useInlineRename';
import { chatDisplayName } from '../../utils/chats';
import { classNames } from '../../utils/classNames';
import { IconButton } from '../IconButton/IconButton';
import { PencilIcon, TrashIcon } from '../IconButton/icons';
import { RenameField } from './RenameField';
import styles from './ChatSidebar.module.css';

interface ChatRowProps {
  chat: Chat;
  isSelected: boolean;
  rename: InlineRename;
  onSelect: () => void;
  onRequestDelete: () => void;
}

export function ChatRow({ chat, isSelected, rename, onSelect, onRequestDelete }: ChatRowProps) {
  const renameButtonRef = useRef<HTMLButtonElement>(null);
  const name = chatDisplayName(chat);
  const isEditing = rename.draft?.chatId === chat.chatId;
  // After a keyboard save or cancel, focus returns to the control that started the edit.
  const refocus = (done: boolean) => done && renameButtonRef.current?.focus();

  return (
    <li className={classNames(styles.row, isSelected && styles.selected)}>
      {isEditing && rename.draft ? (
        <RenameField
          draft={rename.draft}
          onChange={rename.change}
          onSubmit={() => void rename.save().then(refocus)}
          onCancel={() => {
            rename.cancel();
            refocus(true);
          }}
          onBlur={rename.saveIfChanged}
        />
      ) : (
        <button
          type="button"
          className={styles.item}
          aria-current={isSelected ? 'true' : undefined}
          onClick={onSelect}
        >
          {name}
        </button>
      )}
      <span className={styles.rowActions}>
        <IconButton
          ref={renameButtonRef}
          label={`Rename ${name}`}
          icon={<PencilIcon />}
          onClick={() => rename.start(chat)}
        />
        <IconButton label={`Delete ${name}`} icon={<TrashIcon />} onClick={onRequestDelete} />
      </span>
    </li>
  );
}
