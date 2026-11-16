// Shared bits for the ⌘K chat palette and the sidebar's hint for it.

export const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent);

/** ⌘K on a Mac. Elsewhere Ctrl+K — but on a Mac Ctrl+K is the agent's own "kill line", so it's left alone there. */
export const PALETTE_KEY_LABEL = IS_MAC ? '⌘K' : 'Ctrl K';

export const isPaletteShortcut = (e: KeyboardEvent): boolean =>
  (IS_MAC ? e.metaKey : e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k';

/** A terminal that produced output within this many seconds is "working" (claude's TUI animates
 *  ~1/sec while busy); quieter than that and it's likely waiting on you. */
export const WORKING_WITHIN_S = 6;
