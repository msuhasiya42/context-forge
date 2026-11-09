import { forwardRef, type ReactNode } from 'react';
import controls from '../../styles/controls.module.css';

interface IconButtonProps {
  /** Accessible name; the icon itself is decorative. */
  label: string;
  icon: ReactNode;
  onClick: () => void;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, onClick },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      className={controls.iconButton}
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      {icon}
    </button>
  );
});
