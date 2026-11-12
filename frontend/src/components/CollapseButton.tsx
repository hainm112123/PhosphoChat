import { useState } from "react";

/** "‹" to collapse a side column, "›" (the same chevron, flipped) to bring it back. */
export function CollapseButton({ collapsed, onToggle, label, className = "" }: {
  collapsed: boolean;
  onToggle: () => void;
  label: string; // what is shown or hidden, e.g. "navigation"
  className?: string;
}) {
  const title = `${collapsed ? "Show" : "Hide"} ${label}`;
  return (
    <button type="button" className={`collapse-btn${collapsed ? " collapsed" : ""} ${className}`}
      onClick={onToggle} title={title} aria-label={title} aria-expanded={!collapsed}>
      <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
        <path d="M10 3 5 8l5 5" fill="none" stroke="currentColor" strokeWidth="1.8"
          strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </button>
  );
}

/** An open/closed flag remembered in this browser (falls back to `initial` when storage is unavailable). */
export function useRememberedToggle(key: string, initial: boolean): [boolean, () => void] {
  const [open, setOpen] = useState(() => {
    try {
      const saved = localStorage.getItem(key);
      return saved == null ? initial : saved === "1";
    } catch {
      return initial;
    }
  });
  const toggle = () => setOpen((o) => {
    try {
      localStorage.setItem(key, o ? "0" : "1");
    } catch {
      /* private window or blocked storage: just don't remember */
    }
    return !o;
  });
  return [open, toggle];
}
