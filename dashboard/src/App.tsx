import { useState } from 'react';
import { useDoctor, useRepos, useTaskActions, type Repo } from './api';
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
  const [view, setView] = useState<'tasks' | 'chats'>('tasks');
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
          <DoctorBadge />
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
