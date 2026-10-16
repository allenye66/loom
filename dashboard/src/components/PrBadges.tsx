import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

type Pr = {
  number: number | string;
  repo?: string | null;
  source?: 'created' | 'pinned';
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
 *  checks running / error / draft / open). The list comes from /api/chats/{sid}/prs: the PRs
 *  this chat CREATED (verified against GitHub's createdAt) plus any pinned by hand. Until that
 *  resolves, the `prs` prop (transcript pr-links) is shown as plain chips. `+ pr` pins a PR by
 *  URL/number; × on a chip removes it. Degrades to plain links if gh is unavailable. */
export function PrBadges({ sid, prs, repo }: { sid?: string; prs: (number | string)[]; repo?: string | null }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ['chat-prs', sid],
    queryFn: () => fetch(`/api/chats/${sid}/prs`).then((r) => r.json()).then((d) => (d.prs ?? []) as Pr[]),
    enabled: !!sid,
    refetchInterval: 30000,
    staleTime: 20000,
  });
  const [adding, setAdding] = useState(false);
  const [ref, setRef] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ['chat-prs', sid] });

  const pin = async () => {
    if (!ref.trim()) return setAdding(false);
    const r = await fetch(`/api/chats/${sid}/prs`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ ref }),
    });
    if (!r.ok) return setErr((await r.json().catch(() => null))?.detail ?? 'could not add');
    setRef('');
    setErr(null);
    setAdding(false);
    refresh();
  };
  const remove = async (p: Pr) => {
    const q = new URLSearchParams({ repo: p.repo ?? '', number: String(p.number) });
    await fetch(`/api/chats/${sid}/prs?${q}`, { method: 'DELETE' });
    refresh();
  };

  const list: Pr[] = data ?? prs.map((n) => ({ number: n, repo, state: 'unknown' }));
  return (
    <>
      {list.map((p) => {
        const n = p.number;
        const url = p.url ?? (p.repo ? `https://github.com/${p.repo}/pull/${n}` : undefined);
        const meta = STATUS[p.status ?? 'unknown'] ?? STATUS.unknown;
        const label = meta.label ? ` · ${meta.label}` : '';
        const cls = `px-1.5 py-0.5 rounded border ${meta.cls}`;
        const text = `PR #${n}${label}`;
        const title =
          (p.title ? `${meta.label || 'PR'} — ${p.title}` : `PR #${n}`) +
          (p.source === 'pinned' ? ' (pinned)' : '');
        const key = `${p.repo ?? ''}#${n}`;
        const chip = url ? (
          <a href={url} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} title={title} className={cls}>
            {text}
          </a>
        ) : (
          <span className={cls} title={title}>
            {text}
          </span>
        );
        return (
          <span key={key} className="group relative inline-flex">
            {chip}
            {sid && data && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  remove(p);
                }}
                title="remove from this chat"
                className="absolute -top-1.5 -right-1.5 hidden group-hover:flex h-3.5 w-3.5 items-center justify-center rounded-full bg-surface-2 border border-edge text-muted hover:text-bad text-[9px] leading-none"
              >
                ×
              </button>
            )}
          </span>
        );
      })}
      {sid &&
        (adding ? (
          <span className="inline-flex items-center gap-1">
            <input
              autoFocus
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') pin();
                if (e.key === 'Escape') {
                  setAdding(false);
                  setErr(null);
                }
              }}
              onBlur={() => !ref.trim() && setAdding(false)}
              placeholder="PR URL or #number"
              className="w-44 px-1.5 py-0.5 rounded border border-edge bg-transparent text-ink outline-none focus:border-accent-dim"
            />
            {err && <span className="text-bad">{err}</span>}
          </span>
        ) : (
          <button
            onClick={() => setAdding(true)}
            title="pin a PR this chat didn't create"
            className="px-1.5 py-0.5 rounded border border-dashed border-edge text-muted hover:text-ink"
          >
            + pr
          </button>
        ))}
    </>
  );
}
