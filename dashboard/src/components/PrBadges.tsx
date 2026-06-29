import { useQuery } from '@tanstack/react-query';

type Pr = {
  number: number | string;
  url?: string | null;
  state: string;
  title?: string | null;
  draft?: boolean;
  merged?: boolean;
  checks?: string; // pass | fail | pending | none | unknown
  status?: string; // merged | closed | draft | error | ready | passing | running | unmerged | unknown
};

// Coarse merge-readiness → chip label + colors.
const STATUS: Record<string, { label: string; cls: string }> = {
  merged: { label: 'merged', cls: 'border-accent-dim text-accent bg-accent/10' },
  ready: { label: 'ready to merge', cls: 'border-ok/60 text-ok bg-ok/15' },
  passing: { label: 'tests passing', cls: 'border-ok/40 text-ok bg-ok/10' },
  running: { label: 'checks running', cls: 'border-warn/40 text-warn bg-warn/10 animate-pulse' },
  error: { label: 'error', cls: 'border-bad/40 text-bad bg-bad/10' },
  closed: { label: 'closed', cls: 'border-bad/40 text-bad bg-bad/10' },
  draft: { label: 'draft', cls: 'border-edge text-muted' },
  unmerged: { label: 'open', cls: 'border-edge text-muted' },
  unknown: { label: '', cls: 'border-edge text-muted' },
};

/** A chat's PRs as chips with live GitHub status (merged / ready to merge / tests passing /
 *  checks running / error / draft / open). Shows the PR number immediately (from props) and
 *  enriches once the /api/chats/{sid}/prs lookup (gh, cached) resolves. Degrades to a plain
 *  link if gh is unavailable (status "unknown"). */
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
        const meta = STATUS[p?.status ?? 'unknown'] ?? STATUS.unknown;
        const label = meta.label ? ` · ${meta.label}` : '';
        const cls = `px-1.5 py-0.5 rounded border ${meta.cls}`;
        const text = `PR #${n}${label}`;
        const title = p?.title ? `${meta.label || 'PR'} — ${p.title}` : `PR #${n}`;
        return url ? (
          <a
            key={String(n)}
            href={url}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
            title={title}
            className={cls}
          >
            {text}
          </a>
        ) : (
          <span key={String(n)} className={cls} title={title}>
            {text}
          </span>
        );
      })}
    </>
  );
}
