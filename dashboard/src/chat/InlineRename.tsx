import { useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useChatActions, type Chat } from '../api';

/**
 * Rename a chat's sidebar name: optimistically patch every cached chat list (so the new name
 * shows at once; the open chat's header follows via the sidebar's title sync), then persist it
 * via `PATCH /chats/{id}` (the same overlay `name` that `loom chats rename` writes).
 * Empty / unchanged names are ignored — a blank never wipes a chat's name.
 */
export function useRenameChat() {
  const qc = useQueryClient();
  const { patch } = useChatActions();
  return (id: string, name: string) => {
    const next = name.trim();
    if (!next) return;
    qc.setQueriesData<Chat[]>({ queryKey: ['chats'] }, (list) =>
      Array.isArray(list) ? list.map((c) => (c.id === id ? { ...c, name: next } : c)) : list,
    );
    patch.mutate({ id, patch: { name: next } });
  };
}

/**
 * Inline text field for renaming in place: Enter (or clicking away) saves, Escape cancels.
 * Click / double-click / drag events are swallowed so editing inside a clickable or draggable
 * row never opens, drags, or re-triggers anything underneath.
 */
export function InlineRename({
  value,
  onCommit,
  onCancel,
  className = '',
}: {
  value: string;
  onCommit: (name: string) => void;
  onCancel: () => void;
  className?: string;
}) {
  const [draft, setDraft] = useState(value);
  const done = useRef(false); // Enter/Escape unmount the input, which fires blur — don't commit twice
  const finish = (save: boolean) => {
    if (done.current) return;
    done.current = true;
    const next = draft.trim();
    if (save && next && next !== value) onCommit(next);
    else onCancel();
  };
  return (
    <input
      autoFocus
      value={draft}
      maxLength={120}
      onFocus={(e) => e.currentTarget.select()}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        e.stopPropagation(); // keep row / global shortcuts out of the text field
        if (e.key === 'Enter') finish(true);
        else if (e.key === 'Escape') finish(false);
      }}
      onBlur={() => finish(true)}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      draggable={false}
      spellCheck={false}
      aria-label="chat name"
      className={`min-w-0 rounded bg-canvas border border-edge outline-none focus:border-accent px-1 py-0.5 ${className}`}
    />
  );
}
