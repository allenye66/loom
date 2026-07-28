import { useRef, useState } from 'react';
import { openNotesModal, setNote, useNote, type NoteMeta } from './notesStore';

function rel(ms: number): string {
  const s = (Date.now() - ms) / 1000;
  if (s < 5) return 'saved just now';
  if (s < 60) return `saved ${Math.floor(s)}s ago`;
  if (s < 3600) return `saved ${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `saved ${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `saved ${Math.floor(s / 86400)}d ago`;
  return `saved ${new Date(ms).toLocaleDateString()}`;
}

/** Collapsible per-chat notes drawer, docked to the right of the terminal. Free text,
 *  auto-saved to localStorage on every keystroke (see notesStore). Seeded once from the
 *  stored note — the parent keys the whole terminal by chat, so this remounts per chat. */
export function NotesPanel({
  chatId,
  meta,
  onClose,
}: {
  chatId: string;
  meta: NoteMeta;
  onClose: () => void;
}) {
  const note = useNote(chatId);
  const [text, setText] = useState(note?.text ?? '');
  // Keep the latest chat metadata without re-seeding text; captured into each save.
  const metaRef = useRef(meta);
  metaRef.current = meta;

  const change = (v: string) => {
    setText(v);
    setNote(chatId, v, metaRef.current);
  };

  return (
    <div className="w-80 shrink-0 border-l border-edge bg-surface flex flex-col min-h-0">
      <div className="px-3 py-2 flex items-center gap-2 border-b border-edge shrink-0">
        <span className="text-[11px] mono text-accent shrink-0">✎ notes</span>
        <span className="text-[10px] mono text-muted truncate flex-1">
          {note?.updated ? rel(note.updated) : 'not saved yet'}
        </span>
        <button
          onClick={openNotesModal}
          title="see all chat notes"
          className="text-[10px] mono text-muted hover:text-accent border border-edge rounded px-1.5 py-0.5 shrink-0"
        >
          all ↗
        </button>
        <button onClick={onClose} title="close notes" className="text-muted hover:text-ink text-sm leading-none px-1 shrink-0">
          ✕
        </button>
      </div>
      <textarea
        autoFocus
        value={text}
        onChange={(e) => change(e.target.value)}
        placeholder="notes for this chat… (saved to this browser)"
        className="flex-1 min-h-0 resize-none bg-canvas text-ink text-[13px] leading-relaxed px-3 py-2.5 outline-none thin-scroll placeholder:text-muted/50"
      />
      <div className="px-3 py-1.5 border-t border-edge text-[10px] mono text-muted flex items-center justify-between shrink-0">
        <span>local to this browser</span>
        <span>{text.length} chars</span>
      </div>
    </div>
  );
}
