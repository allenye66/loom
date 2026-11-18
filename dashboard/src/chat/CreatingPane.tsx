import { useEffect, useState } from 'react';
import { Spinner } from '../components/Spinner';
import type { PendingChat } from './openChat';

/** The content pane while a new task's worktree is being made (`POST /tasks`: git worktree add +
 *  the repo's setup commands — which can take a while on a big repo). Once it returns, the chat
 *  opens and the terminal pane shows its own "starting <agent>…" until the agent prints. */
export function CreatingPane({ pending }: { pending: PendingChat }) {
  const [secs, setSecs] = useState(0);
  useEffect(() => {
    const t0 = Date.now();
    const id = window.setInterval(() => setSecs(Math.floor((Date.now() - t0) / 1000)), 1000);
    return () => window.clearInterval(id);
  }, [pending.id]);
  return (
    <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-3 p-10 text-center">
      <Spinner size={22} />
      <div className="mono text-sm text-ink">creating {pending.branch}…</div>
      <div className="text-xs text-muted max-w-sm">
        making the git worktree and running the repo’s setup — then {pending.agent} starts here.
        {secs >= 3 && <span className="mono"> {secs}s</span>}
      </div>
    </div>
  );
}
