import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useOpenChat } from './openChat';
import { useChatActions, useRepos, useTasks, useTaskActions, type Chat, type Task } from '../api';

// The sidebar's active/archived tab — module-level so it survives the overlay remount that
// opening a chat triggers (otherwise clicking a chat would snap the tab back to 'active').
let _sidebarTab: 'active' | 'archived' = 'active';
let _sidebarQ = ''; // sidebar search query — likewise persisted across the open-chat remount
// Manual sidebar chat order (localStorage): chats keep their slot — no recency reshuffling —
// and only move when dragged.
const CHAT_ORDER_KEY = 'loom.sidebarChatOrder';
const loadChatOrder = (): string[] => {
  try {
    const v = JSON.parse(localStorage.getItem(CHAT_ORDER_KEY) || '[]');
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
};
const saveChatOrder = (ids: string[]) => {
  try {
    localStorage.setItem(CHAT_ORDER_KEY, JSON.stringify(ids));
  } catch {
    /* private mode / quota — order just won't persist */
  }
};
// Live branch-name sanitizer: keep only chars git allows in a ref (spaces/anything else → '-').
const sanitizeBranch = (s: string) => s.replace(/[^A-Za-z0-9._/-]+/g, '-');
// A terminal that produced output within this many seconds is "working" (claude's TUI
// animates ~1/sec while busy); quieter than that and it's likely waiting on you.
const WORKING_WITHIN_S = 6;

/** Open this worktree in Cursor so an admin can hand-edit the code (the chat keeps
 *  running — no handoff). */
export function OpenInIde({ cwd }: { cwd?: string }) {
  const [state, setState] = useState<'idle' | 'opening' | 'error'>('idle');
  if (!cwd) return null;
  const go = async () => {
    setState('opening');
    try {
      const r = await fetch('/api/ide', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ cwd }),
      });
      if (!r.ok) throw new Error(String(r.status));
      setState('idle');
    } catch {
      setState('error');
      setTimeout(() => setState('idle'), 2500);
    }
  };
  return (
    <button
      title={`Open this worktree in your editor (set 'editor:' in .loom.yaml or $LOOM_EDITOR):\n${cwd}`}
      onClick={go}
      disabled={state === 'opening'}
      className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5 shrink-0 disabled:opacity-50"
    >
      {state === 'opening' ? 'opening…' : state === 'error' ? 'failed' : '✎ edit'}
    </button>
  );
}

/** Poll a URL from the browser every `ms` to see if it's actually serving. Uses `no-cors`:
 *  the response is opaque (we can't read it), but the fetch RESOLVES when the server answered
 *  and REJECTS on connection-refused — enough to know the port is live, regardless of whether
 *  loom started the service. (A failed poll logs a net error to the console, but only while the
 *  service is down.) */
function usePing(url: string | undefined, ms = 5000): boolean {
  const [up, setUp] = useState(false);
  useEffect(() => {
    if (!url) {
      setUp(false);
      return;
    }
    let alive = true;
    const ping = () => {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), 4000);
      fetch(url, { mode: 'no-cors', cache: 'no-store', signal: ctrl.signal })
        .then(() => alive && setUp(true))
        .catch(() => alive && setUp(false))
        .finally(() => clearTimeout(to));
    };
    ping();
    const id = setInterval(ping, ms);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [url, ms]);
  return up;
}

/** A port chip that opens the service in a new tab — purple (accent) when reachable, muted
 *  otherwise. The parent computes `up` (via usePing) so it can drive the start/stop button too. */
function PortLink({ label, url, up }: { label: string; url: string; up?: boolean }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={`open ${url}`}
      className={`px-2 py-0.5 rounded border inline-flex items-center gap-1 ${
        up ? 'border-accent-dim text-accent hover:bg-accent/10' : 'border-edge text-muted hover:text-ink'
      }`}
    >
      {label} ↗
    </a>
  );
}

/** In-chat dev-stack strip: open this worktree's frontend/backend in a new tab, and
 *  start/stop the services without leaving the chat. Only shown when the chat's cwd
 *  maps to a loom task with allocated ports. */
// Link each chip to the service's configured `health:` URL from .loom.yaml (lets a repo point
// the BE chip at e.g. /api/health/ instead of a root that 404s); fall back to the bare port.
// Render the host as `localhost` even though the health *check* uses 127.0.0.1/0.0.0.0 (reliable
// for probing) — browsers prefer localhost, and an app bound to 127.0.0.1 may behave differently
// (auth/cookies) than the same URL on localhost.
const svcLink = (health: string | null | undefined, port: number) =>
  (health || `http://localhost:${port}`).replace('://127.0.0.1', '://localhost').replace('://0.0.0.0', '://localhost');

export function DevStackBar({
  task,
  logsOpen,
  onToggleLogs,
}: {
  task: Task;
  /** Whether the live log drawer is open (highlights the logs button). */
  logsOpen?: boolean;
  onToggleLogs?: () => void;
}) {
  const a = useTaskActions();
  const fe = task.services?.find((s) => s.name === 'frontend');
  const be = task.services?.find((s) => s.name === 'backend');
  // Per-service URLs + liveness (browser ping OR loom's health check). usePing runs before the
  // early-return so the hook order is stable (it returns false for an undefined URL).
  const feUrl = task.ports ? svcLink(fe?.health_url, task.ports.frontend) : undefined;
  const beUrl = task.ports ? svcLink(be?.health_url, task.ports.backend) : undefined;
  const feUp = usePing(feUrl) || !!fe?.healthy;
  const beUp = usePing(beUrl) || !!be?.healthy;
  if (!task.ports) return null;

  const allUp = feUp && beUp;
  const anyUp = feUp || beUp;
  // The down services to (re)start when not everything is up. Stop only when both are up.
  const down = [...(feUp ? [] : ['frontend']), ...(beUp ? [] : ['backend'])];
  const busy = a.start.isPending || a.stop.isPending;
  const [statusTxt, statusCls] = allUp
    ? ['● running', 'text-ok']
    : anyUp
      ? ['◐ partial', 'text-warn']
      : ['○ stopped', 'text-muted'];

  return (
    <div className="border-b border-edge bg-surface-2/40 px-5 py-1.5 flex items-center gap-2 text-[11px] mono shrink-0">
      <span className="text-muted shrink-0">dev stack</span>
      <PortLink label={`FE :${task.ports.frontend}`} url={feUrl!} up={feUp} />
      <PortLink label={`BE :${task.ports.backend}`} url={beUrl!} up={beUp} />
      <div className="flex-1" />
      <span className={statusCls}>{statusTxt}</span>
      {onToggleLogs && (
        <button
          onClick={onToggleLogs}
          title="Live frontend / backend / test logs — follow, filter, copy. Esc closes."
          className={`px-2 py-0.5 rounded border shrink-0 inline-flex items-center gap-1.5 ${
            logsOpen
              ? 'border-accent-dim text-accent bg-accent/10'
              : 'border-edge text-muted hover:text-ink'
          }`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${logsOpen ? 'bg-accent' : anyUp ? 'bg-ok/70' : 'bg-muted/40'}`} />
          logs
        </button>
      )}
      <button
        onClick={() => (allUp ? a.stop.mutate(task.id) : a.start.mutate({ id: task.id, only: down }))}
        disabled={busy}
        title={
          allUp
            ? 'stop this worktree’s dev services'
            : `start ${down.join(' + ')} (leaves any running service alone)`
        }
        className="px-2 py-0.5 rounded border border-edge text-muted hover:text-ink disabled:opacity-40 shrink-0"
      >
        {busy ? '…' : allUp ? 'stop' : 'start'}
      </button>
    </div>
  );
}

/** Sidebar of loom's work — one chat per task worktree (list / search / star / archive /
 *  reorder / create / open-as-terminal). This is loom's own rail, NOT your whole ~/.claude
 *  history (that's the Chats page). Every chat opens into the terminal surface. */
export function ChatSidebar({ activeSid }: { activeSid?: string }) {
  const openChat = useOpenChat();
  const { patch } = useChatActions();
  const { create, remove } = useTaskActions();
  const { data: repos } = useRepos();
  const [tab, setTabState] = useState<'active' | 'archived'>(_sidebarTab);
  const setTab = (t: 'active' | 'archived') => {
    _sidebarTab = t; // persist across the remount that opening a chat causes
    setTabState(t);
  };
  const [q, setQState] = useState(_sidebarQ);
  const setQ = (v: string) => {
    _sidebarQ = v;
    setQState(v);
  };
  const [order, setOrder] = useState<string[]>(loadChatOrder);
  const dragId = useRef<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [branch, setBranch] = useState('');
  const repoRoot = repos?.[0]?.root || '';
  // This sidebar shows *loom's* work — one chat per task worktree — so filter the chat
  // index down to chats whose cwd is a current task worktree.
  const { data: tasks } = useTasks();
  const worktreeSet = new Set((tasks ?? []).map((t) => t.worktree_path));

  const { data: chatList } = useQuery({
    queryKey: ['chats', { scope: 'all', tab, q }],
    queryFn: () =>
      fetch(`/api/chats?scope=all&tab=${tab}${q ? `&q=${encodeURIComponent(q)}` : ''}`)
        .then((r) => r.json())
        .then((d) => d.chats as Chat[]),
    refetchInterval: 8000,
  });
  // Live activity per terminal session: `idle_sec` (output recency) drives the working-pulse;
  // `needs` (claude's own Stop/Notification hook marker) drives "needs you".
  const { data: termData } = useQuery({
    queryKey: ['terminals'],
    queryFn: () =>
      fetch('/api/terminals')
        .then((r) => r.json())
        .then((d) => d.terminals as { chat_id: string; idle_sec: number; needs: boolean }[]),
    refetchInterval: 2000,
  });
  const termById = new Map((termData ?? []).map((t) => [t.chat_id, t]));
  const taskChats = (chatList ?? []).filter((c) => c.cwd && worktreeSet.has(c.cwd));

  type Row = { id: string; title: string; cwd?: string | null; starred: boolean; last: number; mode?: 'chat' | 'terminal' | null };
  const rows: Row[] = taskChats.map((c) => ({
    id: c.id,
    title: c.name || c.display_title || c.id.slice(0, 8),
    cwd: c.cwd,
    starred: c.starred,
    last: c.last_active,
    mode: c.mode,
  }));

  // Stable, user-controlled order: each chat keeps its saved slot; brand-new chats sit at the
  // top until persisted. While searching, show matches in the query's order instead.
  const byId = new Map(rows.map((r) => [r.id, r]));
  const orderedRows: Row[] = q
    ? rows
    : [
        ...rows.filter((r) => !order.includes(r.id)),
        ...order.map((id) => byId.get(id)).filter((r): r is Row => !!r),
      ];

  // Remember any newly-seen chats so their slot persists across reloads.
  useEffect(() => {
    const newIds = rows.map((r) => r.id).filter((id) => !order.includes(id));
    if (newIds.length) {
      const next = [...newIds, ...order];
      setOrder(next);
      saveChatOrder(next);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows]);

  const reorder = (fromId: string | null, toId: string) => {
    if (!fromId || fromId === toId) return;
    const cur = order.filter((id) => id !== fromId);
    const ti = cur.indexOf(toId);
    if (ti < 0) return;
    cur.splice(ti, 0, fromId); // drop the dragged chat just before the one it was released on
    setOrder(cur);
    saveChatOrder(cur);
  };

  const archiveToggle = (id: string) => patch.mutate({ id, patch: { archived: tab !== 'archived' } });
  // Fully delete an archived chat's worktree (git worktree + files). Warns first; the branch and
  // the chat transcript are kept. Also trims the worktree count, which keeps loom snappy.
  const deleteWorktree = (r: Row) => {
    const task = (tasks ?? []).find((t) => t.worktree_path && t.worktree_path === r.cwd);
    if (!task) {
      alert('No worktree found for this chat — it may already be removed.');
      return;
    }
    if (
      !confirm(
        `Fully delete the worktree for "${task.branch}"?\n\n` +
          `This permanently removes the git worktree and its files — any uncommitted changes are lost. ` +
          `The branch and the chat transcript are kept.`,
      )
    )
      return;
    remove.mutate({ id: task.id, force: true });
  };
  const createTask = async () => {
    const b = branch.trim().replace(/^-+/, ''); // input is sanitized live; also drop any leading "-"
    if (!b || !repoRoot) return;
    setBranch('');
    setCreating(false);
    try {
      const task = await create.mutateAsync({ repo_root: repoRoot, branch: b });
      // Only open a chat for a healthy task — a worktree that failed to create would just
      // produce repeated "working directory does not exist" errors.
      if (task?.state === 'error') {
        alert(`Couldn’t create the worktree:\n\n${task.note ?? 'unknown error'}`);
        return;
      }
      if (task?.worktree_path)
        openChat({ cwd: task.worktree_path, resume: task.chat_id ?? undefined, title: task.branch, mode: 'terminal' });
    } catch (e: any) {
      alert(`Couldn’t create the task:\n\n${e?.message ?? e}`);
    }
  };

  return (
    <div className="w-56 shrink-0 border-r border-edge bg-surface overflow-auto thin-scroll flex flex-col">
      <div className="px-3 py-2.5 flex items-center justify-between">
        <span className="text-[11px] mono text-muted uppercase tracking-wide">chats</span>
        <button
          onClick={() => setCreating((v) => !v)}
          title="new task + chat"
          className="text-[11px] mono text-muted hover:text-accent leading-none"
        >
          + new
        </button>
      </div>

      {creating && (
        <div className="px-3 pb-2 flex gap-1">
          <input
            autoFocus
            value={branch}
            onChange={(e) => setBranch(sanitizeBranch(e.target.value))}
            onKeyDown={(e) => {
              if (e.key === 'Enter') createTask();
              if (e.key === 'Escape') setCreating(false);
            }}
            placeholder={repoRoot ? 'new branch name…' : 'add a repo first'}
            disabled={!repoRoot}
            className="flex-1 min-w-0 mono text-[11px] px-2 py-1 rounded bg-surface border border-edge outline-none focus:border-accent"
          />
          <button
            onClick={createTask}
            disabled={!branch || !repoRoot || create.isPending}
            className="text-[11px] px-2 py-1 rounded bg-accent/15 text-accent border border-accent-dim disabled:opacity-40"
          >
            {create.isPending ? '…' : 'go'}
          </button>
        </div>
      )}

      <div className="px-3 pb-2 flex items-center gap-2">
        <div className="flex rounded-md border border-edge overflow-hidden text-[11px]">
          {(['active', 'archived'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-2 py-1 ${tab === t ? 'bg-surface-2 text-ink' : 'bg-surface text-muted hover:text-ink'}`}
            >
              {t}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-muted mono">{rows.length}</span>
      </div>

      <div className="px-3 pb-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="search title, branch, PR #, tag…"
          className="w-full mono text-[11px] px-2 py-1 rounded bg-surface border border-edge outline-none focus:border-accent"
        />
      </div>

      {rows.length === 0 && (
        <div className="px-3 text-[11px] text-muted/70">{q ? `no matches for "${q}"` : `no ${tab} chats`}</div>
      )}
      {orderedRows.map((r) => {
        const t = termById.get(r.id); // live terminal activity (undefined = not running this loom session)
        const working = t != null && t.idle_sec < WORKING_WITHIN_S; // recent output → claude is working
        const needs = t != null && t.needs && !working && r.id !== activeSid; // claude's hook flagged a wait, it's quiet, and it's not the chat you're viewing
        return (
        <div
          key={r.id}
          draggable={!q}
          onDragStart={(e) => {
            dragId.current = r.id;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', r.id); // Firefox needs data set to start a drag
          }}
          onDragOver={(e) => !q && e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            reorder(dragId.current, r.id);
            dragId.current = null;
          }}
          onDragEnd={() => {
            dragId.current = null;
          }}
          onClick={() => openChat({ resume: r.id, cwd: r.cwd ?? undefined, title: r.title, mode: r.mode ?? undefined })}
          className={`group w-full text-left px-3 py-2 flex items-center gap-2 border-l-2 ${
            q ? 'cursor-pointer' : 'cursor-grab active:cursor-grabbing'
          } ${r.id === activeSid ? 'bg-surface-2 border-accent' : 'border-transparent hover:bg-surface-2/60'}`}
        >
          <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${working ? 'bg-accent animate-pulse' : needs ? 'bg-ok' : 'bg-muted/40'}`} />
          <span className="text-xs text-ink truncate flex-1">{r.title}</span>
          {working && <span title="claude is working (recent output)" className="text-[9px] mono text-accent shrink-0 animate-pulse">working</span>}
          {needs && <span title="idle — claude may be waiting on you" className="text-[9px] mono text-ok shrink-0">needs you</span>}
          {r.mode === 'terminal' && !working && !needs && <span title="terminal chat (real claude TUI)" className="text-[9px] mono text-accent shrink-0">❯</span>}
          {/* Open this chat as a terminal (resumes the same transcript in the real claude TUI). */}
          {r.mode !== 'terminal' && (
            <button
              onClick={(e) => { e.stopPropagation(); openChat({ resume: r.id, cwd: r.cwd ?? undefined, title: r.title, mode: 'terminal' }); }}
              title="open as a terminal (real claude TUI)"
              className="opacity-0 group-hover:opacity-100 text-muted hover:text-accent shrink-0 text-[9px] mono leading-none"
            >
              term
            </button>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); archiveToggle(r.id); }}
            title={tab === 'archived' ? 'unarchive' : 'archive'}
            className="opacity-0 group-hover:opacity-100 text-muted hover:text-ink shrink-0 text-xs leading-none"
          >
            {tab === 'archived' ? '↺' : '⊘'}
          </button>
          {tab === 'archived' && (
            <button
              onClick={(e) => { e.stopPropagation(); deleteWorktree(r); }}
              title="fully delete this worktree (removes the git worktree + files; branch & transcript kept)"
              className="opacity-0 group-hover:opacity-100 text-muted hover:text-bad shrink-0 text-xs leading-none"
            >
              🗑
            </button>
          )}
        </div>
        );
      })}
    </div>
  );
}
