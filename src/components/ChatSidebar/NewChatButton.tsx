import { useId } from 'react';
import controls from '../../styles/controls.module.css';

const ALREADY_NEW_HINT = "You're already in a new chat";

interface NewChatButtonProps {
  onClick: () => void;
  isCreating: boolean;
  /** The selected chat has no messages yet, so another empty chat is not offered. */
  isInNewChat: boolean;
}

export function NewChatButton({ onClick, isCreating, isInNewChat }: NewChatButtonProps) {
  const hintId = useId();
  return (
    <>
      <button
        type="button"
        className={controls.primary}
        onClick={isInNewChat ? undefined : onClick}
        disabled={isCreating}
        aria-disabled={isInNewChat || undefined}
        aria-describedby={isInNewChat ? hintId : undefined}
        title={isInNewChat ? ALREADY_NEW_HINT : undefined}
      >
        New chat
      </button>
      {isInNewChat && (
        <span id={hintId} className={controls.visuallyHidden}>
          {ALREADY_NEW_HINT}
        </span>
      )}
    </>
  );
}
