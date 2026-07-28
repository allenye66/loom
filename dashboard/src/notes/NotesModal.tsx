import { useEffect, useState } from 'react';
import { useOpenChat } from '../chat/openChat';
import { closeNotesModal, deleteNote, useNotesList, useNotesModalOpen, type Note } from './notesStore';

function rel(ms: number): string {
  const s = (Date.now() - ms) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(ms).toLocaleDateString();
}

/** Global modal listing every chat note, newest first. Each entry links back to the chat it
 *  came from (reopens the terminal via the stored cwd/agent). Mounted once (inside ChatProvider,
 *  so it can reach `useOpenChat` and float above the terminal overlay); renders null when closed. */
export function NotesModal() {
  const open = useNotesModalOpen();
  const notes = useNotesList();
  const openChat = useOpenChat();
  const [q, setQ] = useState('');

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeNotesModal();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  if (!open) return null;

  const needle = q.trim().toLowerCase();
  const filtered = needle
    ? notes.filter((n) => (n.title + ' ' + n.text).toLowerCase().includes(needle))
    : notes;

  const openFromNote = (n: Note) => {
    closeNotesModal();
    openChat({ resume: n.id, cwd: n.cwd, title: n.title, mode: 'terminal', agent: n.agent });
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 backdrop-blur-sm p-4 sm:p-10"
      onClick={closeNotesModal}
    >
      <div
        className="w-full max-w-2xl max-h-full flex flex-col rounded-xl border border-edge bg-surface shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 flex items-center gap-2.5 border-b border-edge shrink-0">
          <span className="text-sm font-semibold">✎ Chat notes</span>
          <span className="text-[11px] mono text-muted">{notes.length}</span>
          <div className="flex-1" />
          <button onClick={closeNotesModal} className="text-muted hover:text-ink text-lg leading-none px-1">
            ✕
          </button>
        </div>

        {notes.length > 0 && (
          <div className="px-4 py-2.5 border-b border-edge shrink-0">
            <input
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="search notes…"
              className="w-full mono text-xs px-2.5 py-1.5 rounded bg-canvas border border-edge outline-none focus:border-accent"
            />
          </div>
        )}

        <div className="overflow-auto thin-scroll p-3 flex flex-col gap-2">
          {filtered.length === 0 ? (
            <div className="text-center py-16 text-muted">
              <div className="mono text-sm">{notes.length === 0 ? 'no notes yet' : 'no matches'}</div>
              <div className="text-xs mt-1">
                {notes.length === 0
                  ? 'open a chat and jot notes from the ✎ notes panel'
                  : 'try a different search'}
              </div>
            </div>
          ) : (
            filtered.map((n) => (
              <div key={n.id} className="rounded-lg border border-edge bg-surface-2/40 p-3">
                <div className="flex items-center gap-2 mb-1.5">
                  <span className="text-sm text-ink font-medium truncate flex-1">{n.title}</span>
                  {n.agent && (
                    <span className="text-[10px] mono px-1.5 py-0.5 rounded border border-edge text-muted shrink-0">
                      {n.agent}
                    </span>
                  )}
                  <span className="text-[10px] mono text-muted shrink-0">{rel(n.updated)}</span>
                </div>
                <div className="text-[13px] text-ink/90 whitespace-pre-wrap break-words max-h-40 overflow-auto thin-scroll">
                  {n.text}
                </div>
                <div className="flex items-center gap-2 mt-2.5">
                  <button
                    onClick={() => openFromNote(n)}
                    className="text-[11px] px-2 py-1 rounded border border-edge text-accent hover:bg-accent/10"
                  >
                    open chat →
                  </button>
                  <div className="flex-1" />
                  <button
                    onClick={() => {
                      if (confirm('Delete this note? (the chat itself is untouched)')) deleteNote(n.id);
                    }}
                    className="text-[11px] px-2 py-1 rounded border border-edge text-muted hover:text-bad hover:border-bad/40"
                  >
                    delete
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
