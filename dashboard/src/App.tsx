import { useState } from 'react';
import { useDoctor, useRepos, useTasks, useTaskActions, type Repo } from './api';
import { ChatProvider } from './chat/ChatContext';
import { useChatShell } from './chat/openChat';
import { ChatSidebar } from './chat/ChatSidebar';
import { TerminalView } from './term/TerminalView';
import { NotesButton } from './notes/NotesButton';

function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden>
      <g stroke="var(--color-accent)" strokeWidth="1.6" strokeLinecap="round">
        <path d="M3 6h16M3 11h16M3 16h16" opacity="0.35" />
        <path d="M6 3v16M11 3v16M16 3v16" />
      </g>
    </svg>
  );
}

function DoctorBadge() {
  const { data } = useDoctor();
  const [open, setOpen] = useState(false);
  if (!data) return null;
  const ok = data.filter((c) => c.ok).length;
  const bad = data.length - ok;
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-xs mono px-2.5 py-1 rounded-md border border-edge bg-surface hover:bg-surface-2"
      >
        <span className={bad ? 'text-warn' : 'text-ok'}>●</span> doctor {ok}/{data.length}
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-80 z-20 rounded-lg border border-edge bg-surface-2 p-2 shadow-2xl">
          {data.map((c) => (
            <div key={c.name} className="flex items-start gap-2 px-2 py-1 text-xs">
              <span className={c.ok ? 'text-ok' : 'text-bad'}>{c.ok ? '✓' : '✗'}</span>
              <div className="min-w-0">
                <div className="text-ink mono">{c.name}</div>
                {!c.ok && c.hint && <div className="text-muted">{c.hint}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Header quick-action: type a branch (or worktree slug) and open its worktree in the
 *  configured editor. Matches against loom's known worktrees; `/api/ide` opens the editor
 *  ($LOOM_EDITOR / .loom.yaml `editor:` / Cursor) and verifies the dir exists. */
function OpenWorktree() {
  const { data: tasks } = useTasks();
  const [branch, setBranch] = useState('');
  const [state, setState] = useState<'idle' | 'opening' | 'notfound' | 'error'>('idle');
  const flash = (s: 'notfound' | 'error') => {
    setState(s);
    setTimeout(() => setState('idle'), 2500);
  };

  const open = async () => {
    const b = branch.trim();
    if (!b) return;
    const lb = b.toLowerCase();
    const list = (tasks ?? []).filter((t) => t.state !== 'archived' && t.worktree_path);
    // Exact branch/slug match first; else a *unique* substring match (forgiving but unambiguous).
    let t = list.find((t) => t.branch.toLowerCase() === lb || t.id.toLowerCase() === lb);
    if (!t) {
      const m = list.filter((t) => t.branch.toLowerCase().includes(lb) || t.id.toLowerCase().includes(lb));
      if (m.length === 1) t = m[0];
    }
    if (!t) {
      flash('notfound');
      return;
    }
    setState('opening');
    try {
      const r = await fetch('/api/ide', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ cwd: t.worktree_path }),
      });
      if (!r.ok) throw new Error(String(r.status));
      setState('idle');
      setBranch('');
    } catch {
      flash('error');
    }
  };

  return (
    <div className="flex items-center gap-1.5">
      <input
        value={branch}
        onChange={(e) => {
          setBranch(e.target.value);
          if (state === 'notfound' || state === 'error') setState('idle');
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') open();
        }}
        placeholder="open branch in editor…"
        list="loom-worktree-branches"
        title="type a branch (or worktree slug) → opens that worktree in your editor"
        className="mono text-xs px-2.5 py-1 rounded-md bg-surface border border-edge outline-none focus:border-accent w-44"
      />
      <datalist id="loom-worktree-branches">
        {(tasks ?? []).filter((t) => t.state !== 'archived').map((t) => (
          <option key={t.id} value={t.branch} />
        ))}
      </datalist>
      <button
        onClick={open}
        disabled={state === 'opening' || !branch.trim()}
        className={`text-xs mono px-2.5 py-1 rounded-md border bg-surface hover:bg-surface-2 disabled:opacity-40 shrink-0 ${
          state === 'notfound'
            ? 'border-warn/40 text-warn'
            : state === 'error'
              ? 'border-bad/40 text-bad'
              : 'border-edge text-muted hover:text-ink'
        }`}
      >
        {state === 'opening' ? '…' : state === 'notfound' ? 'no worktree' : state === 'error' ? 'failed' : '✎ open'}
      </button>
    </div>
  );
}

function RepoPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data: repos } = useRepos();
  const { addRepo } = useTaskActions();
  const [path, setPath] = useState('');

  if (!repos || repos.length === 0) {
    return (
      <div className="flex items-center gap-2">
        <input
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="/path/to/repo (with .loom.yaml)"
          className="mono text-sm px-3 py-2 rounded-md bg-surface border border-edge outline-none focus:border-accent w-72"
        />
        <button
          onClick={() => addRepo.mutate(path)}
          disabled={!path || addRepo.isPending}
          className="px-3 py-2 rounded-md border border-edge text-sm text-muted hover:text-ink disabled:opacity-40"
        >
          add repo
        </button>
      </div>
    );
  }
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="mono text-sm px-3 py-2 rounded-md bg-surface border border-edge outline-none focus:border-accent"
    >
      {repos.map((r: Repo) => (
        <option key={r.root} value={r.root}>
          {r.name}
        </option>
      ))}
    </select>
  );
}

/** Content pane shown when no chat is open. loom is sidebar-first now, so this is just a
 *  welcome + the global actions (repo add on first run, open-in-editor, notes, doctor) —
 *  not a browsable page. */
function EmptyState() {
  const { data: repos } = useRepos();
  const noRepos = !repos || repos.length === 0;
  return (
    <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-6 p-10 text-center">
      <Logo />
      <div>
        <div className="text-lg font-semibold tracking-tight">loom</div>
        <div className="text-sm text-muted mt-1">
          Pick a chat from the sidebar, or <span className="text-accent mono">+ new</span> to start one.
        </div>
      </div>
      {noRepos && (
        <div className="flex flex-col items-center gap-1.5">
          <div className="text-xs text-muted">Add a repo (with a .loom.yaml) to get started:</div>
          <RepoPicker value="" onChange={() => {}} />
        </div>
      )}
      <div className="flex items-center gap-2">
        <OpenWorktree />
        <NotesButton />
        <DoctorBadge />
      </div>
    </div>
  );
}

/** The whole app: an always-present chat sidebar next to the content pane (the live terminal
 *  for the open chat, or the empty state). No separate home/main page. */
function Shell() {
  const { active, close } = useChatShell();
  return (
    <div className="flex h-screen min-h-0 bg-canvas">
      <ChatSidebar activeSid={active?.resume} />
      {active ? (
        <TerminalView
          // remount (fresh xterm + socket) when switching chats
          key={active.resume ?? active.cwd ?? active.title}
          resume={active.resume}
          cwd={active.cwd}
          title={active.title}
          agent={active.agent}
          onClose={close}
        />
      ) : (
        <EmptyState />
      )}
    </div>
  );
}

export default function App() {
  return (
    <ChatProvider>
      <Shell />
    </ChatProvider>
  );
}
