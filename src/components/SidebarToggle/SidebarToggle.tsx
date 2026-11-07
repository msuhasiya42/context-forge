import { classNames } from '../../utils/classNames';
import controls from '../../styles/controls.module.css';
import styles from './SidebarToggle.module.css';

interface SidebarToggleProps {
  sidebarId: string;
  isOpen: boolean;
  onToggle: () => void;
}

/** Shows or hides the chat list; only rendered visibly on narrow screens. */
export function SidebarToggle({ sidebarId, isOpen, onToggle }: SidebarToggleProps) {
  return (
    <button
      type="button"
      className={classNames(controls.button, styles.toggle)}
      aria-controls={sidebarId}
      aria-expanded={isOpen}
      onClick={onToggle}
    >
      {isOpen ? 'Hide chats' : 'Show chats'}
    </button>
  );
}
