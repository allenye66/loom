import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useHiddenRepoNames, useTasks, type Chat } from '../api';
import { AgentIcon } from '../components/AgentIcon';
import { useChatShell, useOpenChat } from './openChat';
import { PALETTE_KEY_LABEL, WORKING_WITHIN_S, isPaletteShortcut } from './shortcuts';

const MAX_SHOWN = 60;

const titleOf = (c: Chat) => c.name || c.display_title || c.id.slice(0, 8);

function ago(sec: number): string {
  const s = Date.now() / 1000 - sec;
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d`;
  return `${Math.floor(s / (86400 * 30))}mo`;
}

/** `text` with every occurrence of any search term wrapped in a highlight. */
function Highlight({ text, terms }: { text: string; terms: string[] }) {
  if (!terms.length) return <>{text}</>;
  const lower = text.toLowerCase();
  const marks: [number, number][] = [];
  for (const t of terms)
    for (let i = lower.indexOf(t); i !== -1; i = lower.indexOf(t, i + t.length)) marks.push([i, i + t.length]);
  if (!marks.length) return <>{text}</>;
  marks.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const m of marks) {
    const last = merged[merged.length - 1];
    if (last && m[0] <= last[1]) last[1] = Math.max(last[1], m[1]);
    else merged.push([m[0], m[1]]);
  }
  const out: ReactNode[] = [];
  let at = 0;
  merged.forEach(([s, e], k) => {
    if (s > at) out.push(text.slice(at, s));
    out.push(
      <mark key={k} className="bg-transparent text-accent">
        {text.slice(s, e)}
      </mark>,
    );
    at = e;
  });
  if (at < text.length) out.push(text.slice(at));
  return <>{out}</>;
}

/** Every term must appear somewhere; hits in the title outrank the branch/project, which
 *  outrank the body (first prompt, preview, tags…). Returns 0 for no match. */
function score(c: Chat, terms: string[]): number {
  if (!terms.length) return 1;
  const title = titleOf(c).toLowerCase();
  const near = `${c.branch ?? ''} ${c.repo ?? ''}`.toLowerCase();
  const body = [c.title, c.preview, c.first_prompt, c.description, ...(c.tags ?? []), ...(c.prs ?? []), c.pr_manual]
    .filter((x) => x != null && x !== '')
    .join(' ')
    .toLowerCase();
  let total = 0;
  for (const t of terms) {
    if (title.startsWith(t)) total += 5;
    else if (title.includes(t)) total += 4;
    else if (near.includes(t)) total += 2;
    else if (body.includes(t)) total += 1;
    else return 0;
  }
  return total;
}

/** ⌘K: jump to any of loom's chats, across every project, by typing part of its title, branch,
 *  project, PR # or tag. ↑/↓ + Enter (or click) opens it; Esc closes. Mounted once in the shell. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isPaletteShortcut(e)) return;
      e.preventDefault();
      e.stopPropagation(); // capture phase: the terminal never sees the chord
      if (!e.repeat) setOpen((v) => !v);
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);
  return open ? <PaletteDialog onClose={() => setOpen(false)} /> : null;
}

function PaletteDialog({ onClose }: { onClose: () => void }) {
  const openChat = useOpenChat();
  const activeSid = useChatShell().active?.resume;
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const returnFocusTo = useRef<Element | null>(document.activeElement);

  // Same keys as the sidebar's own lists (so a rename's optimistic update lands here too), but
  // fetched once on open rather than polled.
  const chatsQuery = (tab: 'active' | 'archived') => ({
    queryKey: ['chats', { scope: 'all', tab, q: '' }],
    queryFn: () =>
      fetch(`/api/chats?scope=all&tab=${tab}`)
        .then((r) => r.json())
        .then((d) => d.chats as Chat[]),
    refetchInterval: false as const,
  });
  const active = useQuery(chatsQuery('active'));
  const archived = useQuery(chatsQuery('archived'));
  const { data: tasks } = useTasks();
  const hiddenRepos = useHiddenRepoNames();
  const { data: terms } = useQuery({
    queryKey: ['terminals'],
    queryFn: () =>
      fetch('/api/terminals')
        .then((r) => r.json())
        .then((d) => d.terminals as { chat_id: string; idle_sec: number; needs: boolean }[]),
    refetchInterval: false,
  });
  const termById = useMemo(() => new Map((terms ?? []).map((t) => [t.chat_id, t])), [terms]);

  // loom's work only (a chat that belongs to a task worktree), live ones before archived.
  const all = useMemo(() => {
    const worktrees = new Set((tasks ?? []).map((t) => t.worktree_path));
    const mine = (c: Chat) => c.task != null || (!!c.cwd && worktrees.has(c.cwd));
    return [...(active.data ?? []), ...(archived.data ?? [])].filter((c) => mine(c) && !(c.repo && hiddenRepos.has(c.repo)));
  }, [active.data, archived.data, tasks, hiddenRepos]);
  const multiRepo = useMemo(() => new Set(all.map((c) => c.repo)).size > 1, [all]);

  const words = useMemo(() => q.toLowerCase().split(/\s+/).filter(Boolean), [q]);
  const results = useMemo(
    () =>
      all
        .map((c, i) => ({ c, i, s: score(c, words) }))
        .filter((r) => r.s > 0)
        // Best match first; ties keep the server's order (starred, then most recent), live before archived.
        .sort((a, b) => b.s - a.s || Number(a.c.archived) - Number(b.c.archived) || a.i - b.i)
        .map((r) => r.c),
    [all, words],
  );
  const shown = results.slice(0, MAX_SHOWN);

  useEffect(() => setSel(0), [q]);
  useEffect(() => {
    listRef.current?.querySelector('[data-sel="true"]')?.scrollIntoView({ block: 'nearest' });
  }, [sel, shown.length]);

  const close = () => {
    onClose();
    const el = returnFocusTo.current as HTMLElement | null; // e.g. back into the terminal on Esc
    requestAnimationFrame(() => el?.isConnected && el.focus());
  };
  const choose = (c: Chat) => {
    openChat({ resume: c.id, cwd: c.cwd ?? undefined, title: titleOf(c), mode: c.mode ?? undefined, agent: c.agent ?? undefined });
    close();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    e.stopPropagation(); // keep the terminal's and other panels' key handlers out of the palette
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    } else if (e.key === 'ArrowDown' || (e.ctrlKey && e.key === 'n')) {
      e.preventDefault();
      if (shown.length) setSel((s) => (s + 1) % shown.length);
    } else if (e.key === 'ArrowUp' || (e.ctrlKey && e.key === 'p')) {
      e.preventDefault();
      if (shown.length) setSel((s) => (s - 1 + shown.length) % shown.length);
    } else if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (shown[sel]) choose(shown[sel]);
    }
  };

  const loading = active.isLoading || archived.isLoading;
  return (
    <div
      className="fixed inset-0 z-[70] flex items-start justify-center bg-black/50 backdrop-blur-sm px-4 pt-[14vh]"
      onMouseDown={(e) => e.target === e.currentTarget && close()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Jump to chat"
        className="w-full max-w-2xl max-h-[60vh] flex flex-col rounded-xl border border-edge bg-surface shadow-2xl overflow-hidden"
      >
        <div className="px-3.5 py-3 flex items-center gap-2.5 border-b border-edge shrink-0">
          <span className="text-muted text-sm leading-none" aria-hidden>
            ⌕
          </span>
          <input
            autoFocus
            role="combobox"
            aria-expanded="true"
            aria-controls="chat-palette-list"
            aria-activedescendant={shown[sel] ? `chat-palette-${shown[sel].id}` : undefined}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            spellCheck={false}
            placeholder="jump to a chat — title, branch, project, PR #, tag…"
            className="flex-1 min-w-0 bg-transparent outline-none mono text-sm text-ink placeholder:text-muted/60"
          />
        </div>

        <div ref={listRef} id="chat-palette-list" role="listbox" className="overflow-auto thin-scroll py-1 min-h-[3rem]">
          {loading && !all.length && <div className="px-4 py-6 text-center mono text-xs text-muted">loading chats…</div>}
          {!loading && shown.length === 0 && (
            <div className="px-4 py-6 text-center mono text-xs text-muted">
              {q.trim() ? `no chats match “${q.trim()}”` : 'no chats yet'}
            </div>
          )}
          {shown.map((c, i) => {
            const t = termById.get(c.id);
            const working = t != null && t.idle_sec < WORKING_WITHIN_S;
            const needs = t != null && t.needs && !working && c.id !== activeSid;
            const title = titleOf(c);
            const isSel = i === sel;
            return (
              <div
                key={c.id}
                id={`chat-palette-${c.id}`}
                role="option"
                aria-selected={isSel}
                data-sel={isSel}
                onMouseMove={() => sel !== i && setSel(i)}
                onClick={() => choose(c)}
                className={`flex items-center gap-2 px-3.5 py-2 cursor-pointer border-l-2 ${
                  c.id === activeSid ? 'border-accent' : 'border-transparent'
                } ${isSel ? 'bg-surface-2' : ''}`}
              >
                <span
                  className={`inline-block w-2 h-2 rounded-full shrink-0 ${
                    working ? 'bg-accent animate-pulse' : needs ? 'bg-ok' : 'bg-muted/40'
                  }`}
                />
                <span className="text-xs text-ink truncate flex-1 min-w-0">
                  <Highlight text={title} terms={words} />
                </span>
                {multiRepo && c.repo && (
                  <span className="text-[10px] mono text-muted shrink-0 max-w-[7rem] truncate">{c.repo}</span>
                )}
                {c.branch && c.branch !== title && (
                  <span className="text-[10px] mono text-muted/70 shrink-0 max-w-[9rem] truncate">
                    ⎇ <Highlight text={c.branch} terms={words} />
                  </span>
                )}
                {c.agent && (
                  <span className="text-muted shrink-0 opacity-80 inline-flex items-center">
                    <AgentIcon agent={c.agent} />
                  </span>
                )}
                {c.archived && <span className="text-[9px] mono text-muted shrink-0">archived</span>}
                {working && <span className="text-[9px] mono text-accent shrink-0 animate-pulse">working</span>}
                {needs && <span className="text-[9px] mono text-ok shrink-0">needs you</span>}
                <span className="text-[10px] mono text-muted/60 shrink-0 w-7 text-right">{ago(c.last_active)}</span>
              </div>
            );
          })}
          {results.length > shown.length && (
            <div className="px-4 py-2 text-center mono text-[10px] text-muted/70">
              +{results.length - shown.length} more — keep typing to narrow
            </div>
          )}
        </div>

        <div className="px-3.5 py-1.5 border-t border-edge flex items-center gap-3 text-[10px] mono text-muted shrink-0">
          <span>↑↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
          <span className="flex-1" />
          <span>
            {results.length} chat{results.length === 1 ? '' : 's'} · {PALETTE_KEY_LABEL} toggles
          </span>
        </div>
      </div>
    </div>
  );
}
