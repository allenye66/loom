import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useChatShell, useOpenChat } from './openChat';
import { InlineRename, useRenameChat } from './InlineRename';
import { PALETTE_KEY_LABEL, WORKING_WITHIN_S } from './shortcuts';
import { BoardButton } from '../board/BoardButtons';
import { MuteButton } from '../notify/mute';
import { NotifyBell } from '../notify/inbox';
import { NotesButton } from '../notes/NotesButton';
import { openFindPr } from '../findpr/findPrStore';
import { setActiveProjectName } from './activeProject';
import {
  addCategory,
  archiveCategory,
  assignChat,
  claimUnscopedCategories,
  refreshCategories,
  removeCategory,
  renameCategory,
  restoreCategory,
  setCategoryOrder,
  useCategories,
} from '../categories/categoriesStore';
import { useBoards } from '../board/boardApi';
import {
  moveChatInCache,
  useChatActions,
  useDoctor,
  useRepos,
  useHiddenRepos,
  useHiddenRepoNames,
  useTasks,
  useTaskActions,
  type AgentId,
  type Chat,
  type Repo,
  type Task,
} from '../api';
import { AgentIcon } from '../components/AgentIcon';
import { Spinner } from '../components/Spinner';
import { toast, toastAction } from '../ui/toast';
import { EyeIcon, EyeOffIcon, PencilIcon } from '../ui/icons';

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
// Which category groups are collapsed in the sidebar (localStorage). Keyed by category id, or
// the literal 'uncat' for the Uncategorized bucket.
const COLLAPSED_KEY = 'loom.sidebarCollapsedCats';
const loadCollapsed = (): Set<string> => {
  try {
    const v = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || '[]');
    return new Set(Array.isArray(v) ? v : []);
  } catch {
    return new Set();
  }
};
const saveCollapsed = (s: Set<string>) => {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...s]));
  } catch {
    /* private mode / quota */
  }
};
// The project (registered repo) the sidebar shows — remembered across reloads (localStorage).
const draftAgo = (since: number) => {
  const s = Math.max(0, Date.now() / 1000 - since);
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
};
const PROJECT_KEY = 'loom.activeProject';
const loadProject = (): string | null => {
  try {
    return localStorage.getItem(PROJECT_KEY);
  } catch {
    return null;
  }
};
const saveProject = (name: string) => {
  try {
    localStorage.setItem(PROJECT_KEY, name);
  } catch {
    /* private mode / quota — the choice just won't persist */
  }
};
// Live branch-name sanitizer: keep only chars git allows in a ref (spaces/anything else → '-').
const sanitizeBranch = (s: string) => s.replace(/[^A-Za-z0-9._/-]+/g, '-');

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
  // Say what the click is attempting — "start" on a half-up stack only brings the down service up,
  // "stop" takes the whole stack down — then what came of it. mutateAsync + promises (not per-call
  // callbacks) so the result still lands if you switch chats while it runs.
  const svcLabel = (n: string) => `${n === 'frontend' ? 'frontend' : 'backend'} :${n === 'frontend' ? task.ports!.frontend : task.ports!.backend}`;
  const runStartStop = () => {
    const id = `devstack:${task.id}`;
    if (allUp) {
      const all = ['frontend', 'backend'].map(svcLabel).join(' + ');
      void toastAction(id, `stopping ${all}…`, () => a.stop.mutateAsync(task.id), () => `stopped ${all}`, (m) => `stop failed — ${m}`);
      return;
    }
    const names = down.map(svcLabel).join(' + ');
    const kept = ['frontend', 'backend'].filter((n) => !down.includes(n)).map(svcLabel).join(' + ');
    void toastAction(
      id,
      `starting ${names}…${kept ? ` (${kept} already running)` : ''}`,
      () => a.start.mutateAsync({ id: task.id, only: down }),
      () => `launched ${names} — status updates when it answers`,
      (m) => `start failed — ${m}`,
    );
  };
  // `ready` with nothing up = parked: loom stopped it while the chat was idle (or archived) and brings
  // it back by itself once the chat is in use again — unlike `stopped`, which someone chose (D34).
  const parked = !anyUp && task.state === 'ready';
  const [statusTxt, statusCls] = allUp
    ? ['● running', 'text-ok']
    : anyUp
      ? ['◐ partial', 'text-warn']
      : parked
        ? ['○ idle', 'text-muted']
        : ['○ stopped', 'text-muted'];

  return (
    <div className="border-b border-edge bg-surface-2/40 px-5 py-1.5 flex items-center gap-2 text-[11px] mono shrink-0">
      <span className="text-muted shrink-0">dev stack</span>
      <PortLink label={`FE :${task.ports.frontend}`} url={feUrl!} up={feUp} />
      <PortLink label={`BE :${task.ports.backend}`} url={beUrl!} up={beUp} />
      <div className="flex-1" />
      <span
        className={statusCls}
        title={
          parked
            ? 'loom stopped this stack while the chat was idle — it starts again by itself while you use the chat (an agent-spawned chat gets only its backend; press start for the rest)'
            : undefined
        }
      >
        {statusTxt}
      </span>
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
        onClick={runStartStop}
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

/** Sidebar header: the project (registered repo) whose chats are listed, a switcher between
 *  projects with each one's chat count, and "add project" — any git repo path; a .loom.yaml
 *  is optional. */
function ProjectMenu({
  repos,
  active,
  counts,
  onPick,
}: {
  repos: Repo[];
  active?: Repo;
  counts: Map<string, number>;
  onPick: (name: string) => void;
}) {
  const { addRepo, setRepoHidden } = useTaskActions();
  const { data: hidden } = useHiddenRepos();
  const [showHidden, setShowHidden] = useState(false);
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const close = () => {
    setOpen(false);
    setAdding(false);
    setShowHidden(false);
    setPath('');
    setErr(null);
  };
  // Hide is reversible and deletes/stops nothing, so it's instant with an undo — no confirm dialog.
  const hide = (r: Repo) => {
    setRepoHidden.mutate({ name: r.name, hidden: true });
    toast(`hid ${r.name} — its chats keep running, nothing is deleted`, {
      id: `repo:${r.name}`,
      kind: 'ok',
      ttl: 9000,
      action: { label: 'undo', run: () => setRepoHidden.mutate({ name: r.name, hidden: false }) },
    });
  };
  const show = (r: Repo) => {
    setRepoHidden.mutate({ name: r.name, hidden: false });
    toast(`showing ${r.name} again`, { id: `repo:${r.name}`, kind: 'ok' });
  };
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const add = async () => {
    const p = path.trim();
    if (!p) return;
    setErr(null);
    try {
      const repo = await addRepo.mutateAsync(p);
      onPick(repo.name);
      close();
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  };

  return (
    <div ref={ref} className="relative min-w-0">
      <button
        onClick={() => (open ? close() : setOpen(true))}
        title={active ? `${active.root} — switch project` : 'add a project'}
        className="max-w-full flex items-center gap-1 text-[11px] mono text-muted hover:text-ink uppercase tracking-wide"
      >
        <span className="truncate">{active?.name ?? 'no project'}</span>
        <span className="text-[9px] shrink-0">▾</span>
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1 z-30 w-48 rounded-md border border-edge bg-surface shadow-xl overflow-hidden">
          {repos.map((r) => (
            <div key={r.name} className="group relative border-b border-edge hover:bg-surface-2">
              <button
                onClick={() => {
                  onPick(r.name);
                  close();
                }}
                className="w-full text-left pl-3 pr-8 py-2 flex flex-col gap-0.5"
              >
                <span className="text-[11px] mono text-ink flex items-center gap-1.5">
                  <span className="truncate">{r.name}</span>
                  {r.name === active?.name && <span className="text-accent shrink-0">✓</span>}
                  <span className="ml-auto text-[10px] text-muted shrink-0">{counts.get(r.name) ?? 0}</span>
                </span>
                <span className="text-[10px] text-muted truncate">{r.root}</span>
              </button>
              <button
                onClick={() => hide(r)}
                title={`hide ${r.name} from loom — chats keep running, nothing is deleted (undo / restore from "hidden" below)`}
                aria-label={`hide ${r.name}`}
                className="absolute right-2 top-2 text-muted hover:text-ink opacity-0 group-hover:opacity-100 focus:opacity-100"
              >
                <EyeOffIcon size={12} />
              </button>
            </div>
          ))}
          {!!hidden?.length && (
            <div className="border-b border-edge">
              <button
                onClick={() => setShowHidden((v) => !v)}
                className="w-full text-left px-3 py-1.5 text-[10px] mono text-muted hover:text-ink flex items-center gap-1.5"
              >
                <span className="text-[9px]">{showHidden ? '▾' : '▸'}</span>
                hidden ({hidden.length})
              </button>
              {showHidden &&
                hidden.map((r) => (
                  <div key={r.name} className="flex items-center gap-2 pl-5 pr-2 py-1 hover:bg-surface-2" title={r.root}>
                    <span className="text-[11px] mono text-muted truncate flex-1">{r.name}</span>
                    <button
                      onClick={() => show(r)}
                      className="shrink-0 text-[10px] mono text-muted hover:text-accent inline-flex items-center gap-1"
                    >
                      <EyeIcon size={11} /> show
                    </button>
                  </div>
                ))}
            </div>
          )}
          {adding ? (
            <div className="p-2 flex flex-col gap-1.5">
              <div className="flex gap-1">
                <input
                  autoFocus
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') add();
                    if (e.key === 'Escape') close();
                  }}
                  placeholder="/path/to/repo"
                  className="flex-1 min-w-0 mono text-[11px] px-2 py-1 rounded bg-canvas border border-edge outline-none focus:border-accent"
                />
                <button
                  onClick={add}
                  disabled={!path.trim() || addRepo.isPending}
                  className="text-[11px] px-2 py-1 rounded bg-accent/15 text-accent border border-accent-dim disabled:opacity-40"
                >
                  {addRepo.isPending ? '…' : 'add'}
                </button>
              </div>
              {err && <div className="text-[10px] text-bad break-words">{err}</div>}
              <div className="text-[10px] text-muted">any git repo — a .loom.yaml is optional</div>
            </div>
          ) : (
            <button
              onClick={() => setAdding(true)}
              className="w-full text-left px-3 py-2 text-[11px] mono text-muted hover:text-accent hover:bg-surface-2"
            >
              + add project…
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/** Sidebar of loom's work — one chat per task worktree (list / search / star / archive /
 *  reorder / create / open-as-terminal). This is loom's own rail, NOT your whole ~/.claude
 *  history (no page lists that anymore). Every chat opens into the terminal surface. */
export function ChatSidebar({ activeSid }: { activeSid?: string }) {
  const openChat = useOpenChat();
  const { active, open: setActiveChat, close, pending, viewingPending, beginPending, readyPending, dropPending } = useChatShell();
  const qc = useQueryClient();
  const { patch } = useChatActions();
  const renameChat = useRenameChat();
  const [renamingChat, setRenamingChat] = useState<string | null>(null); // chat id being renamed in place
  // A non-blocking error line at the top of the sidebar (a failed create / archive / delete).
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(null), 12000);
    return () => window.clearTimeout(t);
  }, [notice]);
  const { create, remove } = useTaskActions();
  const { data: repos } = useRepos();
  const { data: doctor } = useDoctor();
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
  const [newCat, setNewCat] = useState(''); // category id the new chat goes into ('' = none)
  const [branch, setBranch] = useState('');
  const [agent, setAgent] = useState<AgentId>('claude');
  // Which project the sidebar shows: the remembered one while it's still registered, else the
  // first. Its chats are the only ones listed, and `+ new` creates the task in it.
  const [projectName, setProjectName] = useState<string | null>(loadProject);
  const project = repos?.find((r) => r.name === projectName) ?? repos?.[0];
  useEffect(() => setActiveProjectName(project?.name), [project?.name]); // the notes modal scopes itself to this
  const pickProject = (name: string) => {
    saveProject(name);
    setProjectName(name);
  };
  const repoRoot = project?.root || '';
  const hasClaude = doctor?.some((c) => c.name === 'claude CLI' && c.ok) ?? true;
  const hasGrok = doctor?.some((c) => c.name === 'grok CLI' && c.ok) ?? false;
  const hasCodex = doctor?.some((c) => c.name === 'codex CLI' && c.ok) ?? false;
  // This sidebar shows *loom's* work — one chat per task worktree — so filter the chat
  // index down to chats that belong to a task: the server-resolved task link (covers
  // adopted chats, e.g. a codex session started outside loom and bound to a task) or a
  // cwd inside a current worktree.
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
        .then((d) => d.terminals as { chat_id: string; idle_sec: number; needs: boolean; draft?: boolean; draft_since?: number | null; draft_preview?: string }[]),
    refetchInterval: 2000,
  });
  const termById = new Map((termData ?? []).map((t) => [t.chat_id, t]));
  const hiddenRepoNames = useHiddenRepoNames();
  const allTaskChats = (chatList ?? []).filter(
    (c) => (c.task != null || (c.cwd && worktreeSet.has(c.cwd))) && !(c.repo && hiddenRepoNames.has(c.repo)),
  );
  const countByRepo = new Map<string, number>();
  for (const c of allTaskChats) if (c.repo) countByRepo.set(c.repo, (countByRepo.get(c.repo) ?? 0) + 1);
  // Only the active project's chats (each row carries its repo, resolved server-side).
  const taskChats = project ? allTaskChats.filter((c) => c.repo === project.name) : allTaskChats;

  // Opening a chat that lives in another project (a ?chat= deep link, a note) switches the
  // sidebar to that project so the open chat is listed. Once per opened chat, so switching
  // projects by hand afterwards sticks.
  const syncedSid = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!activeSid || syncedSid.current === activeSid || !chatList) return;
    const known = chatList.find((c) => c.id === activeSid)
      ?? qc.getQueriesData<Chat[]>({ queryKey: ['chats'] }).flatMap(([, l]) => (Array.isArray(l) ? l : [])).find((c) => c.id === activeSid);
    const repo = known?.repo;
    if (!repo) return; // not in any list we have (yet) — retry when it refreshes
    syncedSid.current = activeSid;
    if (repo !== project?.name && repos?.some((r) => r.name === repo)) pickProject(repo);
  }, [activeSid, chatList, project?.name, repos]);

  type Row = {
    id: string;
    title: string;
    cwd?: string | null;
    starred: boolean;
    last: number;
    mode?: 'chat' | 'terminal' | null;
    agent?: AgentId | null;
    muted?: boolean;
    queued?: boolean;
  };
  const rows: Row[] = taskChats.map((c) => ({
    id: c.id,
    title: c.name || c.display_title || c.id.slice(0, 8),
    cwd: c.cwd,
    starred: c.starred,
    last: c.last_active,
    mode: c.mode,
    agent: c.agent,
    muted: c.muted,
    queued: c.queued,
  }));

  // The open chat's header title follows its sidebar row — so a rename (here, in the header, or
  // via `loom chats rename`) shows up there, and a ?chat= deep link (which starts out titled
  // with the bare id) picks up the real name.
  const activeTitle = rows.find((r) => r.id === activeSid)?.title;
  useEffect(() => {
    if (active && active.resume === activeSid && activeTitle && active.title !== activeTitle)
      setActiveChat({ ...active, title: activeTitle });
  }, [activeSid, activeTitle, active?.title]);

  // Stable, user-controlled order: each chat keeps its saved slot; brand-new chats sit at the
  // top until persisted. While searching, show matches in the query's order instead.
  const byId = new Map(rows.map((r) => [r.id, r]));
  // The Archived tab is by archive date (the server's order: starred first, then most recently archived) — a chat's saved
  // slot from when it was active must not override that.
  const orderedRows: Row[] = q || tab === 'archived'
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

  // --- categories ---------------------------------------------------------------------------
  // Shared with the (now-removed) board; assignments are by chat id. Chats group under their
  // category; drag a chat onto a group (or a row in it) to (re)file it. Only kicks in once at
  // least one category exists — otherwise the sidebar stays a plain list.
  const { categories: allCategories, assign } = useCategories();
  // Each project has its own categories. Ones made before projects existed have none yet:
  // file each under the project most of its chats are in (else the first project).
  const categories = (project ? allCategories.filter((c) => !c.repo || c.repo === project.name) : []) // no project (e.g. all hidden) → no groups
    .filter((c) => tab === 'archived' || !c.archived); // an archived category lives in the Archived tab until restored
  useEffect(() => {
    if (!repos?.length || !chatList) return;
    const repoOf = new Map(chatList.map((c) => [c.id, c.repo]));
    claimUnscopedCategories((chatIds) => {
      const n = new Map<string, number>();
      for (const id of chatIds) {
        const r = repoOf.get(id);
        if (r) n.set(r, (n.get(r) ?? 0) + 1);
      }
      return [...n].sort((a, b) => b[1] - a[1])[0]?.[0] ?? repos[0].name;
    });
  }, [repos, chatList]);
  const validCatIds = new Set(categories.map((c) => c.id));
  const catOf = (id: string): string | null => {
    const a = assign[id];
    return a && validCatIds.has(a) ? a : null;
  };
  const grouped = !q && categories.length > 0;
  // The sidebar's groups top to bottom: Uncategorized (always first), then the project's
  // categories in order. A category can be dragged by its header to move it.
  const groups: { id: string | null; key: string; name: string; archived?: boolean }[] = [
    { id: null, key: 'uncat', name: 'Uncategorized' },
    ...categories.map((c) => ({ id: c.id, key: c.id, name: c.name, archived: c.archived })),
  ];
  const dragCat = useRef<string | null>(null); // id of the category being dragged (handlers read this)
  const [draggingCat, setDraggingCat] = useState<string | null>(null); // same, for styling
  const [catDropAt, setCatDropAt] = useState<{ key: string; after: boolean } | null>(null);
  const endCatDrag = () => {
    dragCat.current = null;
    setDraggingCat(null);
    setCatDropAt(null);
  };
  // Over a group's top half the dragged category lands before it, over its bottom half after it.
  // Uncategorized stays pinned first, so dropping anywhere on it lands just after it.
  const dropsAfter = (e: React.DragEvent, key: string) => {
    if (key === 'uncat') return true;
    const r = e.currentTarget.getBoundingClientRect();
    return e.clientY > r.top + r.height / 2;
  };
  const dropCat = (toKey: string, after: boolean) => {
    const from = dragCat.current;
    endCatDrag();
    if (!from || from === toKey) return;
    const keys = groups.map((g) => g.key).filter((k) => k !== from);
    const ti = keys.indexOf(toKey);
    if (ti < 0) return;
    keys.splice(after ? ti + 1 : ti, 0, from);
    setCategoryOrder(keys.filter((k) => k !== 'uncat'));
  };
  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed);
  const [dragOverGroup, setDragOverGroup] = useState<string | null>(null);
  const [dragOverRow, setDragOverRow] = useState<string | null>(null); // row the drop would land just before
  const [creatingCat, setCreatingCat] = useState(false);
  const [catName, setCatName] = useState('');
  const [renamingCat, setRenamingCat] = useState<string | null>(null); // category id being renamed in place
  const toggleCollapse = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      saveCollapsed(next);
      return next;
    });
  const createCat = () => {
    if (!catName.trim()) return;
    addCategory(catName, project?.name);
    setCatName('');
    setCreatingCat(false);
  };
  const assignTo = (catId: string | null) => {
    if (dragId.current) assignChat(dragId.current, catId);
    dragId.current = null;
    setDragOverGroup(null);
    setDragOverRow(null);
  };

  // Optimistic (see useChatActions): the row leaves the list at once; the server stops the agent
  // and dev stack in the background. Archiving the chat that's open closes its pane.
  const archiveToggle = (id: string) => {
    const archived = tab !== 'archived';
    if (archived && active?.resume === id) close();
    patch.mutate(
      { id, patch: { archived } },
      {
        onError: (e) => setNotice(`Couldn’t ${archived ? 'archive' : 'unarchive'} the chat: ${e.message}`),
        // restoring one chat of an archived category brings its category back — pick that up now, not on the next poll
        onSuccess: () => { if (!archived) void refreshCategories(); },
      },
    );
  };

  // --- archiving a whole category (reversible; its message board and roster are kept) -----------------
  const { data: boards } = useBoards();
  const postsIn = (catId: string) => boards?.find((b) => b.key === catId)?.count ?? 0;
  const restoreCat = (id: string, name: string) => {
    for (const cid of allCategories.find((c) => c.id === id)?.archived_with ?? []) moveChatInCache(qc, cid, false);
    restoreCategory(id)
      .then((ids) => {
        qc.invalidateQueries({ queryKey: ['chats'] });
        toast(`restored “${name}” — ${ids.length} chat${ids.length === 1 ? '' : 's'} un-archived`, { kind: 'ok' });
      })
      .catch((e) => setNotice(`Couldn’t restore “${name}”: ${e.message}`));
  };
  const archiveCat = (g: { id: string; name: string }) => {
    const mine = allTaskChats.filter((c) => !c.archived && assign[c.id] === g.id);
    const live = mine.filter((c) => termById.has(c.id)).length;
    const posts = postsIn(g.id);
    const s = (n: number) => (n === 1 ? '' : 's');
    if (
      !confirm(
        `Archive category “${g.name}”?\n\n` +
          `• ${mine.length} chat${s(mine.length)} will be archived` + (live ? ` — ${live} running agent${s(live)} will be stopped` : '') + `.\n` +
          `• Its message board${posts ? ` (${posts} post${s(posts)})` : ''} and the chats' category are kept.\n\n` +
          `Restore the whole category — or just one chat — from the Archived tab and it all comes back.`,
      )
    )
      return;
    void qc.cancelQueries({ queryKey: ['chats'] }); // an in-flight refetch would undo the move
    for (const c of mine) moveChatInCache(qc, c.id, true);
    if (active && mine.some((c) => c.id === active.resume)) close();
    archiveCategory(g.id)
      .then((ids) => {
        qc.invalidateQueries({ queryKey: ['chats'] });
        toast(`archived “${g.name}” — ${ids.length} chat${s(ids.length)}; board kept`, {
          id: `cat:${g.id}`,
          kind: 'ok',
          ttl: 10000,
          action: { label: 'undo', run: () => restoreCat(g.id, g.name) },
        });
      })
      .catch((e) => {
        qc.invalidateQueries({ queryKey: ['chats'] });
        setNotice(`Couldn’t archive “${g.name}”: ${e.message}`);
      });
  };
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
    if (active?.resume === r.id) close();
    remove.mutate(
      { id: task.id, force: true },
      { onError: (e) => setNotice(`Couldn’t delete the worktree for ${task.branch}: ${e.message}`) },
    );
  };
  const createTask = async () => {
    const b = branch.trim().replace(/^-+/, ''); // input is sanitized live; also drop any leading "-"
    if (!b || !repoRoot) return;
    setBranch('');
    setCreating(false);
    // Feedback from the first instant: a placeholder row here + a loading state in the content pane
    // (the worktree can take a while to make); the terminal pane then shows "starting <agent>…".
    // An optional category: the server files the chat there as it creates it (so it never flashes in Uncategorized).
    const cat = categories.find((c) => c.id === newCat && !c.archived);
    setNewCat('');
    const pid = beginPending({ branch: b, agent, repo: project?.name, category: cat?.name });
    const fail = (why: string) => {
      dropPending(pid);
      setNotice(`Couldn’t create ${b}: ${why}`);
    };
    try {
      const task = await create.mutateAsync({ repo_root: repoRoot, branch: b, agent, category: cat?.id });
      if (cat) void refreshCategories(); // pick up the new assignment now, not on the next poll
      // Only open a chat for a healthy task — a worktree that failed to create would just
      // produce repeated "working directory does not exist" errors.
      if (task?.state === 'error') return fail(task.note ?? 'unknown error');
      if (!task?.worktree_path) return dropPending(pid);
      const stillHere = readyPending(pid, task.chat_id ?? undefined);
      if (!task.chat_id) dropPending(pid);
      if (stillHere)
        openChat({
          cwd: task.worktree_path,
          resume: task.chat_id ?? undefined,
          title: task.branch,
          mode: 'terminal',
          agent: task.chat_agent ?? agent,
        });
      else if (task.chat_id)
        // They clicked elsewhere while it was being made: don't yank the pane away, but a chat only
        // gets its sidebar row once an agent is attached — so start it headless.
        fetch(`/api/chats/${task.chat_id}/start`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: '{}',
        })
          .catch(() => {})
          .finally(() => qc.invalidateQueries({ queryKey: ['chats'] }));
    } catch (e: any) {
      fail(e?.message ?? String(e));
    }
  };

  // A placeholder row stands in for a task being created until its real chat row shows up in the
  // list (it appears once the agent attaches); whatever never does is dropped after a while.
  const pendingHere = tab === 'active' && !q ? pending.filter((p) => !p.repo || p.repo === project?.name) : [];
  useEffect(() => {
    for (const p of pending) if (p.chatId && (chatList ?? []).some((c) => c.id === p.chatId)) dropPending(p.id);
  }, [pending, chatList]);
  useEffect(() => {
    const timers = pending.filter((p) => p.chatId).map((p) => window.setTimeout(() => dropPending(p.id), 45000));
    return () => timers.forEach((t) => window.clearTimeout(t));
  }, [pending.map((p) => `${p.id}:${p.chatId ?? ''}`).join()]);

  // One chat row. `catId` is the group it's rendered in (undefined in the ungrouped list):
  // dropping another chat on it reorders AND files the dragged chat into this row's group.
  const renderRow = (r: Row, catId?: string | null) => {
    const t = termById.get(r.id); // live terminal activity (undefined = not running this loom session)
    const working = t != null && t.idle_sec < WORKING_WITHIN_S; // recent output → agent is working
    const needs = t != null && t.needs && !working && r.id !== activeSid; // hook flagged a wait, quiet, not the chat you're viewing
    const draft = !!t?.draft && r.id !== activeSid; // unsent text left in this chat's input box (you're not in it now)
    return (
      <div
        key={r.id}
        draggable={!q && renamingChat !== r.id}
        onDragStart={(e) => {
          dragId.current = r.id;
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', r.id); // Firefox needs data set to start a drag
        }}
        onDragOver={(e) => {
          if (q) return;
          e.preventDefault();
          if (dragId.current && dragId.current !== r.id) setDragOverRow(r.id);
        }}
        onDrop={(e) => {
          if (dragCat.current) return; // a group is being dragged — its section's drop handles it
          e.preventDefault();
          e.stopPropagation(); // don't also fire the group's drop
          const from = dragId.current;
          if (from && from !== r.id) {
            reorder(from, r.id);
            if (catId !== undefined) assignChat(from, catId); // inherit this row's group
          }
          dragId.current = null;
          setDragOverGroup(null);
          setDragOverRow(null);
        }}
        onDragEnd={() => {
          dragId.current = null;
          setDragOverGroup(null);
          setDragOverRow(null);
        }}
        onClick={() =>
          openChat({
            resume: r.id,
            cwd: r.cwd ?? undefined,
            title: r.title,
            mode: r.mode ?? undefined,
            agent: r.agent ?? undefined,
          })
        }
        className={`group relative w-full text-left px-3 py-2 flex items-center gap-2 border-l-2 ${
          q ? 'cursor-pointer' : 'cursor-grab active:cursor-grabbing'
        } ${r.id === activeSid ? 'bg-surface-2 border-accent' : 'border-transparent hover:bg-surface-2/60'}`}
      >
        {/* Insertion indicator: the dragged chat lands just before this row. */}
        {dragOverRow === r.id && (
          <span className="pointer-events-none absolute inset-x-1 -top-px h-0.5 rounded-full bg-accent shadow-[0_0_4px_var(--color-accent)] z-10" />
        )}
        <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${working ? 'bg-accent animate-pulse' : needs ? 'bg-ok' : 'bg-muted/40'}`} />
        {renamingChat === r.id ? (
          <InlineRename
            value={r.title}
            onCommit={(name) => { renameChat(r.id, name); setRenamingChat(null); }}
            onCancel={() => setRenamingChat(null)}
            className="flex-1 text-xs text-ink"
          />
        ) : (
          <span
            onDoubleClick={() => setRenamingChat(r.id)}
            title="double-click to rename"
            className="text-xs text-ink truncate flex-1"
          >
            {r.title}
          </span>
        )}
        {draft && (
          <span
            title={`unsent draft${t?.draft_preview ? `: “${t.draft_preview}”` : ''}${t?.draft_since ? ` · started ${draftAgo(t.draft_since)}` : ''} — open the chat to finish or send it`}
            className="text-accent shrink-0 inline-flex items-center"
          >
            <PencilIcon size={11} />
          </span>
        )}
        {r.agent && (
          <span title={`${r.agent} session`} className="text-muted shrink-0 opacity-80 inline-flex items-center">
            <AgentIcon agent={r.agent} />
          </span>
        )}
        {working && <span title="agent is working (recent output)" className="text-[9px] mono text-accent shrink-0 animate-pulse">working</span>}
        {r.queued && !t && (
          <span
            title="spawned by another chat and waiting its turn — it starts on its own when one of that chat's working spawns finishes (open it to start it now)"
            className="text-[9px] mono text-muted shrink-0"
          >
            queued
          </span>
        )}
        {needs && <span title={r.muted ? 'waiting on you (muted — no notification)' : 'idle — agent may be waiting on you'} className={`text-[9px] mono text-ok shrink-0 ${r.muted ? 'opacity-40' : ''}`}>needs you</span>}
        {r.mode === 'terminal' && !working && !needs && !(r.queued && !t) && <span title="terminal chat (real agent TUI)" className="text-[9px] mono text-accent shrink-0">❯</span>}
        {/* Open this chat as a terminal (resumes the same transcript in the real claude TUI). */}
        {r.mode !== 'terminal' && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              openChat({ resume: r.id, cwd: r.cwd ?? undefined, title: r.title, mode: 'terminal', agent: r.agent ?? undefined });
            }}
            title="open as a terminal (real claude TUI)"
            className="opacity-0 group-hover:opacity-100 text-muted hover:text-accent shrink-0 text-[9px] mono leading-none"
          >
            term
          </button>
        )}
        <MuteButton id={r.id} muted={!!r.muted} variant="row" />
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
  };

  // A category section header: click anywhere on chevron/name/count to collapse or expand.
  // Custom categories can also be dragged to move the whole group, renamed ✎ and deleted ✕
  // (Uncategorized is pinned first).
  const renderGroupHeader = (
    g: { id: string | null; key: string; name: string; count: number; archived?: boolean },
    isColl: boolean,
  ) => {
    const renaming = g.id !== null && renamingCat === g.id;
    const movable = g.id !== null && !renaming;
    return (
      <div
        draggable={movable}
        onDragStart={(e) => {
          dragCat.current = g.key;
          setDraggingCat(g.key);
          e.dataTransfer.effectAllowed = 'move';
          e.dataTransfer.setData('text/plain', g.key); // Firefox needs data set to start a drag
        }}
        onDragEnd={endCatDrag}
        onDragOver={() => setDragOverRow(null)}
        className={`group/gh px-2 py-1.5 flex items-center gap-1 bg-surface/95 sticky top-0 z-[1] ${
          movable ? 'cursor-grab active:cursor-grabbing' : ''
        }`}
      >
        {renaming ? (
          <InlineRename
            value={g.name}
            label="category name"
            onCommit={(name) => { renameCategory(g.id!, name); setRenamingCat(null); }}
            onCancel={() => setRenamingCat(null)}
            className="flex-1 mono text-[10px]"
          />
        ) : (
          <button
            // Click toggles. A custom category's double-click renames it instead: its second click
            // is ignored and the double-click puts the first one back, so the group ends as it was.
            onClick={(e) => { if (e.detail < 2 || g.id === null) toggleCollapse(g.key); }}
            onDoubleClick={g.id !== null ? () => { toggleCollapse(g.key); setRenamingCat(g.id); } : undefined}
            aria-expanded={!isColl}
            title={`${isColl ? 'expand' : 'collapse'}${movable ? ' · drag to move · double-click to rename' : ''}`}
            className="flex-1 min-w-0 flex items-center gap-1.5 text-left text-muted hover:text-ink"
          >
            <span className="w-3 shrink-0 text-[11px] leading-none">{isColl ? '▸' : '▾'}</span>
            <span className="flex-1 min-w-0 text-[10px] mono uppercase tracking-wide truncate">
              {g.name}
              {g.archived && <span className="ml-1.5 normal-case tracking-normal text-muted/60">archived</span>}
            </span>
            <span className="text-[9px] mono shrink-0">{g.count}</span>
          </button>
        )}
        {g.id !== null && (
          <>
            <BoardButton catId={g.id} name={g.name} />
            {!g.archived && (
              <button
                onClick={() => { setNewCat(g.id!); setCreating(true); }}
                title={`new chat in “${g.name}”`}
                className="opacity-0 group-hover/gh:opacity-100 text-[11px] text-muted hover:text-accent shrink-0 leading-none"
              >
                +
              </button>
            )}
            <button
              onClick={() => setRenamingCat(g.id)}
              title="rename category"
              className="opacity-0 group-hover/gh:opacity-100 text-[10px] text-muted hover:text-ink shrink-0 leading-none"
            >
              ✎
            </button>
            {g.archived ? (
              <button
                onClick={() => restoreCat(g.id!, g.name)}
                title="restore this category — un-archives the chats its archive archived, and its message board comes back"
                className="text-[11px] text-accent hover:text-ink shrink-0 leading-none"
              >
                ↺
              </button>
            ) : (
              <button
                onClick={() => archiveCat({ id: g.id!, name: g.name })}
                title="archive this category — archives all its chats (agents stopped); the board is kept and you can restore it, or any one chat, later"
                className="opacity-0 group-hover/gh:opacity-100 text-[10px] text-muted hover:text-ink shrink-0 leading-none"
              >
                ⊘
              </button>
            )}
            <button
              onClick={() => {
                const posts = postsIn(g.id!);
                if (
                  confirm(
                    `Delete category “${g.name}” for good?\n\nIts chats stay — they move back to Uncategorized.` +
                      (posts ? `\n\nIts message board (${posts} post${posts === 1 ? '' : 's'}) is deleted with it and can't be restored.` : '') +
                      `\n\nTo keep it, press Cancel and archive the category instead (⊘).`,
                  )
                )
                  removeCategory(g.id!);
              }}
              title="delete category for good (archive it to keep its board)"
              className="opacity-0 group-hover/gh:opacity-100 text-[10px] text-muted hover:text-bad shrink-0 leading-none"
            >
              ✕
            </button>
          </>
        )}
      </div>
    );
  };

  return (
    <div className="w-56 shrink-0 border-r border-edge bg-surface flex flex-col h-full">
      <div className="flex-1 overflow-auto thin-scroll flex flex-col min-h-0">
      <div className="px-3 py-2.5 flex items-center justify-between gap-2">
        <ProjectMenu repos={repos ?? []} active={project} counts={countByRepo} onPick={pickProject} />
        <div className="flex items-center gap-2 shrink-0">
          <NotifyBell />
          <button
            onClick={() => setCreatingCat((v) => !v)}
            title="new category"
            className="text-[11px] mono text-muted hover:text-accent leading-none"
          >
            + cat
          </button>
          <button
            onClick={() => setCreating((v) => !v)}
            title="new task + chat"
            className="text-[11px] mono text-muted hover:text-accent leading-none"
          >
            + new
          </button>
        </div>
      </div>

      {creatingCat && (
        <div className="px-3 pb-2 flex gap-1">
          <input
            autoFocus
            value={catName}
            onChange={(e) => setCatName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') createCat();
              if (e.key === 'Escape') setCreatingCat(false);
            }}
            placeholder="new category name…"
            className="flex-1 min-w-0 mono text-[11px] px-2 py-1 rounded bg-surface border border-edge outline-none focus:border-accent"
          />
          <button
            onClick={createCat}
            disabled={!catName.trim()}
            className="text-[11px] px-2 py-1 rounded bg-accent/15 text-accent border border-accent-dim disabled:opacity-40"
          >
            add
          </button>
        </div>
      )}

      {creating && (
        <div className="px-3 pb-2 flex flex-col gap-1.5">
          <div className="flex gap-1">
            <input
              autoFocus
              value={branch}
              onChange={(e) => setBranch(sanitizeBranch(e.target.value))}
              onKeyDown={(e) => {
                if (e.key === 'Enter') createTask();
                if (e.key === 'Escape') setCreating(false);
              }}
              placeholder={repoRoot ? `new branch in ${project?.name}…` : 'add a project first'}
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
          <div className="flex rounded-md border border-edge overflow-hidden text-[10px] mono">
            {([
              { id: 'claude' as const, label: 'claude', ok: hasClaude },
              { id: 'grok' as const, label: 'grok', ok: hasGrok },
              { id: 'codex' as const, label: 'codex', ok: hasCodex },
            ]).map((opt) => (
              <button
                key={opt.id}
                type="button"
                disabled={!opt.ok}
                title={opt.ok ? `use ${opt.label}` : `${opt.label} CLI not on PATH`}
                onClick={() => setAgent(opt.id)}
                className={`flex-1 px-1.5 py-0.5 inline-flex items-center justify-center gap-1 ${
                  agent === opt.id
                    ? 'bg-accent/15 text-accent'
                    : opt.ok
                      ? 'bg-surface text-muted hover:text-ink'
                      : 'bg-surface text-muted/40 cursor-not-allowed'
                }`}
              >
                <AgentIcon agent={opt.id} size={9} />
                {opt.label}
              </button>
            ))}
          </div>
          {categories.some((c) => !c.archived) && (
            <label className="flex items-center gap-1.5 text-[10px] mono text-muted">
              <span className="shrink-0">category</span>
              <select
                value={categories.some((c) => c.id === newCat && !c.archived) ? newCat : ''}
                onChange={(e) => setNewCat(e.target.value)}
                className="flex-1 min-w-0 mono text-[11px] px-1.5 py-1 rounded bg-surface border border-edge outline-none focus:border-accent text-ink"
              >
                <option value="">none (Uncategorized)</option>
                {categories.filter((c) => !c.archived).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}

      {notice && (
        <div
          role="alert"
          className="mx-3 mb-2 px-2 py-1.5 rounded border border-bad/40 bg-bad/10 text-[11px] text-bad flex items-start gap-2"
        >
          <span className="flex-1 min-w-0 break-words">{notice}</span>
          <button onClick={() => setNotice(null)} title="dismiss" className="shrink-0 leading-none hover:text-ink">
            ✕
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

      <div className="px-3 pb-2 relative">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="search chats…"
          title={`filter this list by title, branch, PR #, tag — or ${PALETTE_KEY_LABEL} to jump to any chat, in any project`}
          className="w-full mono text-[11px] pl-2 pr-9 py-1 rounded bg-surface border border-edge outline-none focus:border-accent"
        />
        {!q && (
          <kbd className="pointer-events-none absolute right-[18px] top-[6px] text-[9px] mono leading-none px-1 py-[3px] rounded border border-edge text-muted/70">
            {PALETTE_KEY_LABEL}
          </kbd>
        )}
      </div>

      {/* Tasks still being created: a placeholder row each, until the real chat row replaces it. */}
      {pendingHere.map((p) => (
        <div
          key={`pending-${p.id}`}
          title={p.chatId ? `starting ${p.agent} in ${p.branch}…` : `creating the worktree for ${p.branch}…`}
          className={`px-3 py-2 flex items-center gap-2 border-l-2 ${
            viewingPending?.id === p.id ? 'bg-surface-2 border-accent' : 'border-transparent'
          }`}
        >
          <Spinner size={10} />
          <span className="text-xs text-muted truncate flex-1">{p.branch}</span>
          {p.category && <span className="text-[9px] mono text-muted/70 truncate max-w-[5rem] shrink-0">→ {p.category}</span>}
          <span className="text-[9px] mono text-accent shrink-0 animate-pulse">{p.chatId ? 'starting…' : 'creating…'}</span>
        </div>
      ))}

      {rows.length === 0 && pendingHere.length === 0 && (
        <div className="px-3 text-[11px] text-muted/70">{q ? `no matches for "${q}"` : `no ${tab} chats`}</div>
      )}

      {/* Ungrouped list — no categories yet, or while searching. */}
      {!grouped && orderedRows.map((r) => renderRow(r))}

      {/* Grouped by category, in the order the groups were dragged into. Drop a chat on a group
          (or a row within it) to file it there; drop a dragged group header to move that group. */}
      {grouped &&
        groups.map((g) => {
          const groupRows = orderedRows.filter((r) => catOf(r.id) === g.id);
          if (g.id === null && groupRows.length === 0) return null; // hide an empty Uncategorized bucket
          const isColl = collapsed.has(g.key);
          return (
            <div
              key={g.key}
              onDragOver={(e) => {
                if (q) return;
                e.preventDefault();
                if (dragCat.current) {
                  const after = dropsAfter(e, g.key);
                  if (catDropAt?.key !== g.key || catDropAt.after !== after) setCatDropAt({ key: g.key, after });
                  return;
                }
                if (dragOverGroup !== g.key) setDragOverGroup(g.key);
              }}
              onDragLeave={() => setDragOverGroup((v) => (v === g.key ? null : v))}
              onDrop={(e) => {
                e.preventDefault();
                if (dragCat.current) dropCat(g.key, dropsAfter(e, g.key));
                else assignTo(g.id);
              }}
              className={`relative ${dragOverGroup === g.key ? 'bg-accent/5' : ''} ${
                draggingCat === g.key ? 'opacity-40' : ''
              }`}
            >
              {/* Insertion indicator: the dragged category lands just above / below this group. */}
              {catDropAt?.key === g.key && draggingCat !== g.key && (
                <span
                  className={`pointer-events-none absolute inset-x-1 ${
                    catDropAt.after ? '-bottom-px' : '-top-px'
                  } h-0.5 rounded-full bg-accent shadow-[0_0_4px_var(--color-accent)] z-10`}
                />
              )}
              {renderGroupHeader({ ...g, count: groupRows.length }, isColl)}
              {!isColl && groupRows.map((r) => renderRow(r, g.id))}
              {!isColl && groupRows.length === 0 && (
                <div
                  onDragOver={() => setDragOverRow(null)}
                  className="px-3 py-2 text-[10px] mono text-muted/40 select-none"
                >
                  drop chats here
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="border-t border-edge p-2 shrink-0 flex flex-col gap-1.5">
        <button
          onClick={openFindPr}
          title="find a session (and its PR) by describing it — Haiku searches an index of your sessions, their logs, and your PRs; archived chats too"
          className="w-full text-[11px] mono px-2 py-1.5 rounded border border-edge text-muted hover:text-ink inline-flex items-center justify-center gap-1.5"
        >
          ⌕ find a session
        </button>
        <NotesButton
          label="✎ all notes"
          className="w-full text-[11px] mono px-2 py-1.5 rounded border border-edge text-muted hover:text-ink inline-flex items-center justify-center gap-1.5"
        />
      </div>
    </div>
  );
}
