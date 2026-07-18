import { useEffect, useState } from 'react';
import { useDoctor, useTasks, useTaskActions, type AgentId } from '../api';
import { useOpenChat } from '../chat/ChatContext';
import { TaskCard } from './TaskCard';

export function TasksView({ repoRoot }: { repoRoot: string }) {
  const { data: tasks } = useTasks();
  const { create } = useTaskActions();
  const { data: doctor } = useDoctor();
  const openChat = useOpenChat();
  const [branch, setBranch] = useState('');
  const [agent, setAgent] = useState<AgentId>('claude');
  const [tab, setTab] = useState<'active' | 'archived'>('active');
  const all = tasks ?? [];
  const archivedCount = all.filter((t) => t.chat_archived).length;
  const shown = all.filter((t) => (tab === 'archived' ? !!t.chat_archived : !t.chat_archived));

  const hasClaude = doctor?.some((c) => c.name === 'claude CLI' && c.ok) ?? true;
  const hasGrok = doctor?.some((c) => c.name === 'grok CLI' && c.ok) ?? false;

  // Prefer an installed agent if the current pick isn't available (e.g. only grok installed).
  useEffect(() => {
    if (!doctor) return;
    if (agent === 'claude' && !hasClaude && hasGrok) setAgent('grok');
    if (agent === 'grok' && !hasGrok && hasClaude) setAgent('claude');
  }, [doctor, hasClaude, hasGrok, agent]);

  // Create the worktree task, then drop straight into a fresh chat with the chosen agent.
  const submit = async () => {
    const b = branch.trim().replace(/^-+/, ''); // input is sanitized live; also drop any leading "-"
    if (!b || !repoRoot) return;
    setBranch('');
    try {
      const task = await create.mutateAsync({ repo_root: repoRoot, branch: b, agent });
      if (task?.state === 'error') return; // surfaced on the task card (and create.isError)
      if (task?.worktree_path)
        openChat({
          cwd: task.worktree_path,
          resume: task.chat_id ?? undefined,
          title: task.branch,
          agent: task.chat_agent ?? agent,
        });
    } catch {
      /* error is surfaced via create.isError below */
    }
  };

  return (
    <>
      <div className="flex flex-col gap-2 mb-6">
        <div className="flex items-center gap-2">
          <input
            value={branch}
            onChange={(e) => setBranch(e.target.value.replace(/[^A-Za-z0-9._/-]+/g, '-'))}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
            placeholder="new branch name…  e.g. alye/fix-no-show-fee"
            className="flex-1 mono text-sm px-3 py-2 rounded-md bg-surface border border-edge focus:border-accent outline-none"
          />
          <button
            disabled={!branch || !repoRoot || create.isPending}
            onClick={submit}
            className="px-3.5 py-2 rounded-md bg-accent text-canvas text-sm font-medium disabled:opacity-40 hover:opacity-90"
          >
            {create.isPending ? 'creating…' : '+ task'}
          </button>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-muted mono">agent</span>
          <div className="flex rounded-md border border-edge overflow-hidden">
            {([
              { id: 'claude' as const, label: 'claude', ok: hasClaude },
              { id: 'grok' as const, label: 'grok', ok: hasGrok },
            ]).map((opt) => (
              <button
                key={opt.id}
                type="button"
                disabled={!opt.ok}
                title={opt.ok ? `use ${opt.label} for this session` : `${opt.label} CLI not found on PATH`}
                onClick={() => setAgent(opt.id)}
                className={`px-3 py-1 mono ${
                  agent === opt.id
                    ? 'bg-accent/15 text-accent'
                    : opt.ok
                      ? 'bg-surface text-muted hover:text-ink'
                      : 'bg-surface text-muted/40 cursor-not-allowed'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {!hasClaude && !hasGrok && (
            <span className="text-bad mono">install claude or grok CLI</span>
          )}
        </div>
      </div>

      {create.isError && (
        <div className="mb-4 text-xs text-bad bg-bad/10 border border-bad/30 rounded-md px-3 py-2 mono">
          {String((create.error as Error)?.message)}
        </div>
      )}

      {all.length > 0 && (
        <div className="flex items-center gap-2 mb-4">
          <div className="flex rounded-md border border-edge overflow-hidden text-sm">
            {(['active', 'archived'] as const).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-3 py-1.5 ${tab === t ? 'bg-surface-2 text-ink' : 'bg-surface text-muted hover:text-ink'}`}
              >
                {t}
                {t === 'archived' && archivedCount ? ` (${archivedCount})` : ''}
              </button>
            ))}
          </div>
          <span className="text-xs text-muted mono">{shown.length}</span>
        </div>
      )}

      {all.length === 0 ? (
        <div className="text-center py-24 text-muted">
          <div className="mono text-sm">no tasks yet</div>
          <div className="text-xs mt-1">type a branch name above to spin up an isolated worktree</div>
        </div>
      ) : shown.length === 0 ? (
        <div className="text-center py-16 text-muted mono text-sm">no {tab} tasks</div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {shown.map((t) => (
            <TaskCard key={t.id} task={t} />
          ))}
        </div>
      )}
    </>
  );
}
