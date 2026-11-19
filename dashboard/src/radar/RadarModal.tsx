import { useEffect, useState } from 'react';
import { closeRadar, useCategoryRadar, useOpenRadarKey, useRefreshRadar, type RadarOverlap } from './radarApi';

/** The conflict radar for one category: which chats are changing the same files, and whether their
 *  committed work would conflict. "tell both" types a heads-up into the two chats (if running). */
export function RadarModal() {
  const key = useOpenRadarKey();
  if (!key) return null;
  return <Radar key={key} catKey={key} />;
}

function Radar({ catKey }: { catKey: string }) {
  const { data } = useCategoryRadar(catKey);
  const refresh = useRefreshRadar();
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && closeRadar();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const tell = async (o: RadarOverlap) => {
    setNote(null);
    const files = o.files.slice(0, 6).join(', ') + (o.n_files > 6 ? ` (+${o.n_files - 6} more)` : '');
    const text = (other: string) =>
      `Heads-up from the user: your branch and "${other}" both changed ${files}${o.conflicts ? ' — their committed work would CONFLICT' : ''}. Coordinate before you both finish, and expect to rebase/merge in turn.`;
    const send = async (id: string, other: string) => {
      const r = await fetch(`/api/chats/${id}/send`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text: text(other), submit: true }),
      });
      return r.ok ? 'sent' : ((await r.json().catch(() => ({})))?.detail ?? `failed (${r.status})`);
    };
    const [x, y] = await Promise.all([send(o.a.id, o.b.name), send(o.b.id, o.a.name)]);
    setNote(`${o.a.name}: ${x} · ${o.b.name}: ${y}`);
  };

  const overlaps = data?.overlaps ?? [];
  return (
    <div
      className="fixed inset-0 z-[65] flex items-start justify-center bg-black/50 backdrop-blur-sm p-4 sm:p-10"
      onMouseDown={(e) => e.target === e.currentTarget && closeRadar()}
    >
      <div role="dialog" aria-label="Conflict radar" className="w-full max-w-2xl max-h-full flex flex-col rounded-xl border border-edge bg-surface shadow-2xl overflow-hidden">
        <div className="px-4 py-3 flex items-center gap-2.5 border-b border-edge shrink-0">
          <span className="text-sm font-semibold">⚠ {data?.category ?? 'Category'} · conflict radar</span>
          <span className="text-[11px] mono text-muted">{overlaps.length} overlapping pair{overlaps.length === 1 ? '' : 's'}</span>
          <div className="flex-1" />
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              await refresh(catKey);
              setBusy(false);
            }}
            className="text-[11px] mono text-muted hover:text-ink disabled:opacity-40"
          >
            {busy ? 'checking…' : '↻ re-check'}
          </button>
          <button onClick={closeRadar} className="text-muted hover:text-ink text-lg leading-none px-1" aria-label="close">
            ✕
          </button>
        </div>
        <div className="overflow-auto thin-scroll px-4 py-3 flex flex-col gap-3">
          {!data && <div className="mono text-xs text-muted">checking worktrees…</div>}
          {data && overlaps.length === 0 && (
            <div className="text-center py-10 text-muted">
              <div className="mono text-sm">no overlaps</div>
              <div className="text-xs mt-1 max-w-sm mx-auto">
                None of the {data.members.length} chats here with a worktree have changed the same files (lockfiles ignored).
              </div>
            </div>
          )}
          {overlaps.map((o) => (
            <div key={o.a.id + o.b.id} className="rounded-lg border border-edge p-3 text-xs">
              <div className="flex items-center gap-2 mb-1.5">
                <span className="font-semibold">{o.a.name}</span>
                <span className="text-muted">⟷</span>
                <span className="font-semibold">{o.b.name}</span>
                <span className="flex-1" />
                <span className={`mono text-[10px] px-1.5 py-0.5 rounded-full ${o.conflicts ? 'bg-bad/20 text-bad' : o.conflicts === false ? 'bg-warn/20 text-warn' : 'bg-surface-2 text-muted'}`}>
                  {o.conflicts ? 'would conflict' : o.conflicts === false ? 'overlap · merges cleanly so far' : 'overlap'}
                </span>
              </div>
              <div className="mono text-[11px] text-muted leading-relaxed break-all">
                {o.files.map((f) => (
                  <div key={f} className={o.conflict_files.includes(f) ? 'text-bad' : ''}>
                    {o.conflict_files.includes(f) ? '✕ ' : '· '}
                    {f}
                  </div>
                ))}
                {o.n_files > o.files.length && <div>… +{o.n_files - o.files.length} more</div>}
              </div>
              <div className="mt-2 flex items-center gap-2">
                <button onClick={() => tell(o)} className="text-[11px] px-2 py-1 rounded border border-accent-dim text-accent hover:bg-accent/10">
                  tell both chats
                </button>
                <span className="text-[10px] text-muted">committed work is tested with git merge-tree; uncommitted edits count as overlap only</span>
              </div>
            </div>
          ))}
          {note && <div className="mono text-[10px] text-muted">{note}</div>}
        </div>
      </div>
    </div>
  );
}
