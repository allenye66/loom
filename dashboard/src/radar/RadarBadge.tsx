import { openRadar, useRadarOverview } from './radarApi';

/** Sidebar category header: ⚠ N when chats in the category are changing the same files (red if their
 *  committed work would actually conflict). Renders nothing when there's no overlap. */
export function RadarBadge({ catId, name }: { catId: string; name: string }) {
  const { data } = useRadarOverview();
  const r = data?.[catId];
  if (!r || r.pairs === 0) return null;
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        openRadar(catId);
      }}
      title={`${r.pairs} pair${r.pairs === 1 ? '' : 's'} of chats in "${name}" changed the same files${r.conflicts ? ` — ${r.conflicts} would conflict` : ''}`}
      className={`shrink-0 text-[10px] mono leading-none px-1 py-0.5 rounded-full ${
        r.conflicts ? 'bg-bad/20 text-bad' : 'bg-warn/20 text-warn'
      }`}
    >
      ⚠ {r.pairs}
    </button>
  );
}
