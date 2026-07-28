import { openNotesModal, useNotesCount } from './notesStore';

/** Opens the global "all chat notes" modal. Shows how many chats have a note. `className`
 *  lets each header match its own button styling (App header vs the terminal header); `label`
 *  lets the terminal header say "all notes" (next to the per-chat "notes" toggle). */
export function NotesButton({ className, label = '✎ notes' }: { className?: string; label?: string }) {
  const count = useNotesCount();
  return (
    <button
      onClick={openNotesModal}
      title="all chat notes"
      className={
        className ??
        'text-xs mono px-2.5 py-1 rounded-md border border-edge bg-surface hover:bg-surface-2 inline-flex items-center gap-1.5'
      }
    >
      <span>{label}</span>
      {count > 0 && (
        <span className="text-[10px] mono leading-none px-1.5 py-0.5 rounded-full bg-accent/15 text-accent border border-accent-dim">
          {count}
        </span>
      )}
    </button>
  );
}
