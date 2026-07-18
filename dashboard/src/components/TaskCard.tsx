import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getLogs, useTaskActions, type Task } from '../api';
import { useOpenChat } from '../chat/ChatContext';

const STATE_STYLES: Record<string, string> = {
  created: 'text-muted border-edge',
  ready: 'text-accent border-accent-dim',
  running: 'text-ok border-ok/40',
  stopped: 'text-muted border-edge',
  error: 'text-bad border-bad/40',
  archived: 'text-muted border-edge',
};

function Dot({ ok }: { ok: boolean | undefined }) {
  const c = ok === undefined ? 'bg-muted' : ok ? 'bg-ok' : 'bg-bad';
  return <span className={`inline-block w-2 h-2 rounded-full ${c}`} />;
}

function Chip({ children, tone = 'muted' }: { children: React.ReactNode; tone?: string }) {
  return <span className={`px-2 py-0.5 rounded border border-edge text-${tone}`}>{children}</span>;
}

/** A port chip that opens the service in a new tab. Accent/clickable styling when the
 *  service is healthy; muted (but still clickable) otherwise. */
function PortLink({ label, url, live }: { label: string; url: string; live?: boolean }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      title={`open ${url}`}
      className={`px-2 py-0.5 rounded border inline-flex items-center gap-1 ${
        live ? 'border-accent-dim text-accent hover:bg-accent/10' : 'border-edge text-muted hover:text-ink'
      }`}
    >
      {label} ↗
    </a>
  );
}

export function TaskCard({ task }: { task: Task }) {
  const a = useTaskActions();
  const openChat = useOpenChat();
  const [showLogs, setShowLogs] = useState(false);
  const [pytestArgs, setPytestArgs] = useState('');
  // Open this worktree's chat — terminal by default (new chats are terminal-only); a legacy
  // chat-mode chat still opens the old SDK UI. Resumes the worktree's one chat (any state),
  // falling back to a new one if there isn't yet.
  const onOpen = async (mode: 'chat' | 'terminal') => {
    let resume: string | undefined;
    let agent: 'claude' | 'grok' | undefined = task.chat_agent ?? undefined;
    try {
      const d = await fetch(`/api/tasks/${task.id}/chat`).then((r) => r.json());
      resume = d.chat_id || undefined;
      if (d.agent === 'claude' || d.agent === 'grok') agent = d.agent;
    } catch { /* fall back to a new chat */ }
    if (resume) {
      try {
        await fetch(`/api/chats/${resume}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode }),
        });
      } catch { /* best-effort lock */ }
    }
    openChat({ cwd: task.worktree_path, resume, title: task.branch, mode, agent });
  };

  const testing = task.test?.running ?? false;
  const logs = useQuery({
    queryKey: ['logs', task.id],
    queryFn: () => getLogs(task.id, 'test').then((d) => d.log),
    enabled: showLogs,
    refetchInterval: showLogs && testing ? 1200 : false,
  });

  const testResult =
    task.test && !task.test.running && task.test.exit_code !== null
      ? task.test.exit_code === 0
        ? 'pass'
        : 'fail'
      : null;

  return (
    <div className="rounded-xl border border-edge bg-surface overflow-hidden flex flex-col">
      <div className="p-4 flex flex-col gap-3 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="mono font-semibold text-ink truncate">{task.branch}</div>
            <div className="text-xs text-muted mono">
              {task.repo} · base {task.base_branch}
            </div>
          </div>
          <span
            className={`shrink-0 text-[11px] mono px-2 py-0.5 rounded-full border ${
              STATE_STYLES[task.state] ?? 'text-muted border-edge'
            }`}
          >
            {task.state}
          </span>
        </div>

        {task.note && task.state === 'error' && (
          <div className="text-xs text-bad bg-bad/10 border border-bad/30 rounded-md px-2 py-1.5 mono break-words">
            {task.note}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-1.5 text-[11px] mono">
          {task.git?.branch && (
            <span className={`px-2 py-0.5 rounded border border-edge ${task.git.dirty ? 'text-warn' : 'text-muted'}`}>
              {task.git.dirty ? '● dirty' : '○ clean'}
            </span>
          )}
          {!!task.git?.ahead && <Chip>↑{task.git.ahead}</Chip>}
          {!!task.git?.behind && <Chip>↓{task.git.behind}</Chip>}
          {task.ports && (
            <PortLink
              label={`BE :${task.ports.backend}`}
              url={`http://localhost:${task.ports.backend}/api/health/`}
              live={task.services?.some((s) => s.name === 'backend' && s.healthy)}
            />
          )}
          {task.ports && (
            <PortLink
              label={`FE :${task.ports.frontend}`}
              url={`http://localhost:${task.ports.frontend}`}
              live={task.services?.some((s) => s.name === 'frontend' && s.healthy)}
            />
          )}
        </div>

        {task.services?.length > 0 && (
          <div className="flex flex-wrap gap-3 text-xs">
            {task.services.map((s) => (
              <span key={s.name} className="flex items-center gap-1.5 text-muted">
                <Dot ok={s.healthy} /> {s.name}
                {s.port ? ` :${s.port}` : ''}
              </span>
            ))}
          </div>
        )}

        {task.state === 'running' && (
          <div className="text-[11px] text-warn bg-warn/10 border border-warn/25 rounded-md px-2 py-1">
            ⚠ shared dev DB — destructive testing here can affect other worktrees
          </div>
        )}

        <div className="mt-1 rounded-lg border border-edge bg-surface-2 p-2.5 flex flex-col gap-2">
          <div className="flex items-center gap-2">
            <input
              value={pytestArgs}
              onChange={(e) => setPytestArgs(e.target.value)}
              placeholder="pytest args — e.g. api/tests/test_x.py"
              className="flex-1 min-w-0 mono text-[11px] px-2 py-1.5 rounded bg-surface border border-edge outline-none focus:border-accent"
            />
            <button
              onClick={() => {
                a.test.mutate({ id: task.id, pytest_args: pytestArgs });
                setShowLogs(true);
              }}
              disabled={testing}
              className="shrink-0 text-xs px-2.5 py-1.5 rounded bg-surface border border-edge hover:border-accent disabled:opacity-40"
            >
              {testing ? 'running…' : 'run tests'}
            </button>
          </div>
          <div className="flex items-center justify-between text-[11px] mono">
            <span>
              {testing && <span className="text-accent">● running</span>}
              {testResult === 'pass' && <span className="text-ok">✓ passed</span>}
              {testResult === 'fail' && <span className="text-bad">✗ failed (exit {task.test?.exit_code})</span>}
              {!task.test && <span className="text-muted">no runs yet</span>}
            </span>
            <button onClick={() => setShowLogs((v) => !v)} className="text-muted hover:text-ink">
              {showLogs ? 'hide logs' : 'logs'}
            </button>
          </div>
          {showLogs && (
            <pre className="thin-scroll max-h-48 overflow-auto text-[10.5px] leading-relaxed mono bg-canvas rounded p-2 text-muted whitespace-pre-wrap">
              {logs.data || '(no output yet)'}
            </pre>
          )}
        </div>
      </div>

      <div className="border-t border-edge bg-surface-2/50 px-3 py-2.5 flex items-center gap-2">
        {/* Terminal is the default surface now; a legacy chat-mode task still opens the old chat UI. */}
        {task.chat_mode === 'chat' ? (
          <button
            onClick={() => onOpen('chat')}
            title="open this worktree's chat (legacy chat UI)"
            className="text-xs px-2.5 py-1.5 rounded bg-surface border border-edge text-muted hover:text-ink"
          >
            open chat
          </button>
        ) : (
          <button
            onClick={() => onOpen('terminal')}
            title={`open the ${task.chat_agent ?? 'agent'} terminal`}
            className="text-xs px-2.5 py-1.5 rounded bg-accent/15 text-accent border border-accent-dim hover:bg-accent/25"
          >
            open{task.chat_agent ? ` · ${task.chat_agent}` : ''}
          </button>
        )}
        <div className="flex-1" />
        {task.state === 'running' ? (
          <button
            onClick={() => a.stop.mutate(task.id)}
            className="text-xs px-2.5 py-1.5 rounded border border-edge text-muted hover:text-ink"
          >
            stop
          </button>
        ) : (
          <button
            onClick={() => a.start.mutate({ id: task.id })}
            className="text-xs px-2.5 py-1.5 rounded border border-edge text-muted hover:text-ink"
          >
            start
          </button>
        )}
        <button
          onClick={() => {
            if (confirm(`Remove ${task.branch}? The worktree will be deleted.`))
              a.remove.mutate({ id: task.id, force: true });
          }}
          className="text-xs px-2.5 py-1.5 rounded border border-edge text-muted hover:text-bad hover:border-bad/40"
        >
          remove
        </button>
      </div>
    </div>
  );
}
