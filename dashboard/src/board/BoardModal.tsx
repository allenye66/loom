import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Chat } from '../api';
import { closeBoard, useBoardActions, useBoardPosts, useOpenBoardKey, type BoardPost } from './boardApi';

function rel(ts: number): string {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/**
 * A category's message board: what the chats in it have posted for each other (and you). Posting
 * as you; tick chats under "nudge" to type a one-line heads-up into the ones that are running
 * (their next message carries the post either way). Mounted once in the shell; renders nothing
 * unless a board is open (see boardApi.openBoard).
 */
export function BoardModal() {
  const key = useOpenBoardKey();
  if (!key) return null;
  return <Board key={key} boardKey={key} />;
}

function Board({ boardKey }: { boardKey: string }) {
  const { data } = useBoardPosts(boardKey);
  const { post, markSeen } = useBoardActions();
  const [text, setText] = useState('');
  const [nudge, setNudge] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true); // keep the view pinned to the newest post unless the reader scrolled up

  // Chats in this category — who you can nudge, and names for the "→" recipients.
  const { data: chats } = useQuery({
    queryKey: ['chats', { scope: 'all', tab: 'active', q: '' }],
    queryFn: () =>
      fetch('/api/chats?scope=all&tab=active')
        .then((r) => r.json())
        .then((d) => d.chats as Chat[]),
    refetchInterval: false,
  });
  const members = useMemo(() => (chats ?? []).filter((c) => c.category === boardKey && c.task && !c.archived), [chats, boardKey]);
  const nameOf = useMemo(() => new Map((chats ?? []).map((c) => [c.id, c.name || c.display_title])), [chats]);

  // Opening/seeing posts marks them read for "user" (the sidebar badge clears).
  const lastSeq = data?.last_seq ?? 0;
  useEffect(() => {
    if (lastSeq > 0) markSeen(boardKey, lastSeq);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boardKey, lastSeq]);
  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [data?.posts.length]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && closeBoard();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const send = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    setNote(null);
    try {
      const r = await post(boardKey, t, [...nudge]);
      setText('');
      stick.current = true;
      const outcomes = Object.entries(r.nudged ?? {});
      if (outcomes.length) setNote(outcomes.map(([id, how]) => `${nameOf.get(id) ?? id.slice(0, 8)}: ${how}`).join(' · '));
    } catch (e: any) {
      setNote(`couldn’t post — ${e?.message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  const posts: BoardPost[] = data?.posts ?? [];
  return (
    <div
      className="fixed inset-0 z-[65] flex items-start justify-center bg-black/50 backdrop-blur-sm p-4 sm:p-10"
      onMouseDown={(e) => e.target === e.currentTarget && closeBoard()}
    >
      <div
        role="dialog"
        aria-label="Category message board"
        className="w-full max-w-2xl max-h-full flex flex-col rounded-xl border border-edge bg-surface shadow-2xl overflow-hidden"
      >
        <div className="px-4 py-3 flex items-center gap-2.5 border-b border-edge shrink-0">
          <span className="text-sm font-semibold">▤ {data?.name ?? 'Board'}</span>
          <span className="text-[11px] mono text-muted">
            {posts.length} post{posts.length === 1 ? '' : 's'} · {members.length} chat{members.length === 1 ? '' : 's'}
          </span>
          <div className="flex-1" />
          <button onClick={closeBoard} className="text-muted hover:text-ink text-lg leading-none px-1" aria-label="close">
            ✕
          </button>
        </div>

        <div
          ref={scroller}
          onScroll={(e) => {
            const el = e.currentTarget;
            stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
          className="flex-1 min-h-[12rem] overflow-auto thin-scroll px-4 py-3 flex flex-col gap-3"
        >
          {!data && <div className="mono text-xs text-muted">loading…</div>}
          {data && posts.length === 0 && (
            <div className="text-center py-10 text-muted">
              <div className="mono text-sm">no posts yet</div>
              <div className="text-xs mt-1 max-w-sm mx-auto">
                Chats in this category post here with <span className="mono">loom board post</span> — findings, decisions,
                handoffs. You can post too.
              </div>
            </div>
          )}
          {posts.map((p) => {
            const mine = p.from === 'user';
            return (
              <div key={p.seq} className="text-xs">
                <div className="flex items-baseline gap-2 mb-0.5">
                  <span className="mono text-[10px] text-muted/70">#{p.seq}</span>
                  <span className={`font-semibold ${mine ? 'text-accent' : 'text-ink'}`}>{mine ? 'you' : p.from_name}</span>
                  {p.to.length > 0 && (
                    <span className="mono text-[10px] text-muted">→ {p.to.map((t) => nameOf.get(t) ?? t.slice(0, 8)).join(', ')}</span>
                  )}
                  {p.reply_to && <span className="mono text-[10px] text-muted">re #{p.reply_to}</span>}
                  <span className="flex-1" />
                  <span className="mono text-[10px] text-muted/60">{rel(p.ts)}</span>
                </div>
                <div className={`whitespace-pre-wrap break-words leading-relaxed pl-3 border-l-2 ${mine ? 'border-accent' : 'border-edge'} text-ink/90`}>
                  {p.text}
                </div>
              </div>
            );
          })}
        </div>

        <div className="border-t border-edge px-4 py-3 flex flex-col gap-2 shrink-0">
          {members.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] mono text-muted">nudge:</span>
              {members.map((c) => {
                const on = nudge.has(c.id);
                return (
                  <button
                    key={c.id}
                    onClick={() =>
                      setNudge((prev) => {
                        const n = new Set(prev);
                        if (n.has(c.id)) n.delete(c.id);
                        else n.add(c.id);
                        return n;
                      })
                    }
                    title="type a one-line heads-up into this chat (if it's running)"
                    className={`text-[10px] mono px-1.5 py-0.5 rounded border max-w-[10rem] truncate ${
                      on ? 'border-accent text-accent bg-accent/10' : 'border-edge text-muted hover:text-ink'
                    }`}
                  >
                    {c.name || c.display_title}
                  </button>
                );
              })}
            </div>
          )}
          <div className="flex items-end gap-2">
            <textarea
              autoFocus
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={2}
              maxLength={4000}
              placeholder="post to this board…  (Enter sends · Shift+Enter for a new line)"
              className="flex-1 mono text-xs px-2.5 py-1.5 rounded bg-canvas border border-edge outline-none focus:border-accent resize-none"
            />
            <button
              onClick={send}
              disabled={!text.trim() || busy}
              className="text-xs px-3 py-1.5 rounded border border-accent-dim text-accent hover:bg-accent/10 disabled:opacity-40 shrink-0"
            >
              post
            </button>
          </div>
          {note && <div className="text-[10px] mono text-muted">{note}</div>}
        </div>
      </div>
    </div>
  );
}
