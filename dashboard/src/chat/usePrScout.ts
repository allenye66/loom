import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { toast } from '../ui/toast';

/** While you're ON a chat (it's the open one and the tab is visible) ask the server for a PR-scout pass
 *  every minute: it finds PRs the transcript scraper missed and links them (core/prscout.py, D29). The
 *  server throttles and only spends a Haiku read when there's something new to judge, so a quiet chat
 *  costs nothing. Linked PRs appear as chips; a toast says what was found and how. */
export function usePrScout(sid?: string) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!sid) return;
    let alive = true;
    const run = async () => {
      if (!alive || document.visibilityState !== 'visible') return;
      try {
        const r = await fetch(`/api/chats/${sid}/pr-scout`, { method: 'POST' });
        if (!r.ok || !alive) return;
        const d = (await r.json()) as { found?: { number: number; source: string }[] };
        if (d.found?.length) {
          qc.invalidateQueries({ queryKey: ['chat-prs', sid] });
          for (const f of d.found)
            toast(`linked PR #${f.number} to this chat — ${f.source === 'branch' ? "its branch has a PR" : 'found by reading the conversation'}`, { kind: 'ok', ttl: 5000 });
        }
      } catch {
        /* offline / server restarting — try again next minute */
      }
    };
    const first = window.setTimeout(run, 4000); // right after opening the chat
    const every = window.setInterval(run, 60_000);
    const onVisible = () => document.visibilityState === 'visible' && run();
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      alive = false;
      window.clearTimeout(first);
      window.clearInterval(every);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [sid, qc]);
}
