import { useQuery } from '@tanstack/react-query';

type Pr = { number: number | string; url?: string | null; state: string; title?: string | null; draft?: boolean; merged?: boolean };

function tone(state?: string, draft?: boolean) {
  if (draft) return 'border-edge text-muted';
  switch (state) {
    case 'open': return 'border-ok/40 text-ok bg-ok/10';
    case 'merged': return 'border-accent-dim text-accent bg-accent/10';
    case 'closed': return 'border-bad/40 text-bad bg-bad/10';
    default: return 'border-edge text-muted';
  }
}

/** A chat's PRs as chips with live GitHub status (open / merged / closed / draft).
 *  Shows the PR number immediately (from props) and enriches with status once the
 *  /api/chats/{sid}/prs lookup (gh, cached) resolves. Degrades to a plain link if
 *  gh is unavailable (state "unknown"). */
export function PrBadges({ sid, prs, repo }: { sid?: string; prs: (number | string)[]; repo?: string | null }) {
  const { data } = useQuery({
    queryKey: ['chat-prs', sid],
    queryFn: () => fetch(`/api/chats/${sid}/prs`).then((r) => r.json()).then((d) => (d.prs ?? []) as Pr[]),
    enabled: !!sid && prs.length > 0,
    refetchInterval: 30000,
    staleTime: 20000,
  });
  if (!prs.length) return null;
  const byNum = new Map((data ?? []).map((p) => [String(p.number), p]));
  return (
    <>
      {prs.map((n) => {
        const p = byNum.get(String(n));
        const url = p?.url ?? (repo ? `https://github.com/${repo}/pull/${n}` : undefined);
        const label = p && p.state !== 'unknown' ? ` · ${p.draft ? 'draft' : p.state}` : '';
        const cls = `px-1.5 py-0.5 rounded border ${tone(p?.state, p?.draft)}`;
        const text = `PR #${n}${label}`;
        return url ? (
          <a
            key={String(n)}
            href={url}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
            title={p?.title ? `${p.draft ? 'draft' : p.state} — ${p.title}` : `PR #${n}`}
            className={cls}
          >
            {text}
          </a>
        ) : (
          <span key={String(n)} className={cls}>
            {text}
          </span>
        );
      })}
    </>
  );
}
