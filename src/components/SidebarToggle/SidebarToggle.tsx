import { forwardRef } from 'react';
import { classNames } from '../../utils/classNames';
import controls from '../../styles/controls.module.css';
import styles from './SidebarToggle.module.css';

interface SidebarToggleProps {
  sidebarId: string;
  isOpen: boolean;
  action: 'show' | 'hide';
  onClick: () => void;
}

/** Shows or hides the chat list; only rendered visibly on narrow screens. */
export const SidebarToggle = forwardRef<HTMLButtonElement, SidebarToggleProps>(
  function SidebarToggle({ sidebarId, isOpen, action, onClick }, ref) {
    return (
      <button
        ref={ref}
        type="button"
        className={classNames(controls.button, styles.toggle)}
        aria-controls={sidebarId}
        aria-expanded={isOpen}
        onClick={onClick}
      >
        {action === 'show' ? 'Show chats' : 'Hide chats'}
      </button>
    );
  },
);
