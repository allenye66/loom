import { useState } from 'react';
import { useDoctor, useRepos, useTasks, useTaskActions, type Repo } from './api';
import { TasksView } from './components/TasksView';
import { ChatsView } from './components/ChatsView';
import { ChatProvider } from './chat/ChatContext';

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

export default function App() {
  const { data: repos } = useRepos();
  const [view, setView] = useState<'tasks' | 'chats'>('chats');
  const [repoRoot, setRepoRoot] = useState('');

  const activeRoot = repoRoot || repos?.[0]?.root || '';
  const activeName = repos?.find((r) => r.root === activeRoot)?.name;

  return (
    <ChatProvider>
    <div className="min-h-full">
      <header className="border-b border-edge bg-surface/70 backdrop-blur sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-5 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <Logo />
            <span className="text-lg font-semibold tracking-tight">loom</span>
            <nav className="ml-3 flex gap-1">
              {(['tasks', 'chats'] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => setView(v)}
                  className={`text-sm px-2.5 py-1 rounded-md ${
                    view === v ? 'bg-surface-2 text-ink' : 'text-muted hover:text-ink'
                  }`}
                >
                  {v}
                </button>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <OpenWorktree />
            <DoctorBadge />
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-5 py-6">
        <div className="mb-5">
          <RepoPicker value={activeRoot} onChange={setRepoRoot} />
        </div>
        {view === 'tasks' ? (
          <TasksView repoRoot={activeRoot} />
        ) : (
          <ChatsView repoRoot={activeRoot} repoName={activeName} />
        )}
      </main>
    </div>
    </ChatProvider>
  );
}
