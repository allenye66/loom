import { useUsage, type UsageWindow } from '../api';

function relFuture(sec?: number | null): string {
  if (!sec) return '';
  const d = sec - Date.now() / 1000;
  if (d <= 0) return 'now';
  if (d < 3600) return `in ${Math.round(d / 60)}m`;
  if (d < 86400) return `in ${Math.round(d / 3600)}h`;
  return `in ${Math.round(d / 86400)}d`;
}
function relPast(sec?: number | null): string {
  if (!sec) return '';
  const d = Date.now() / 1000 - sec;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}
const pctColor = (p: number) => (p >= 90 ? 'text-bad' : p >= 70 ? 'text-warn' : 'text-ok');

/** Compact Claude Code subscription usage for the chat header — "5h 1% · 7d 25%", color-coded,
 *  read from Claude Code's own statusLine data via /api/usage (official, token-free). Tooltip
 *  shows reset times + last update. Renders a subtle placeholder until the first turn fills it
 *  (Claude only reports rate_limits after the first API response in a session). */
export function UsageChip() {
  const { data } = useUsage();
  const five = data?.available ? data.five_hour : null;
  const seven = data?.available ? data.seven_day : null;

  if (!five && !seven) {
    return (
      <span
        title="Claude subscription usage appears here after your first message in a chat — read from Claude Code's own status line (no token, no scraping). Type /usage in the chat for the full breakdown."
        className="text-[10.5px] mono text-muted/50 shrink-0 hidden lg:inline"
      >
        usage …
      </span>
    );
  }

  const seg = (label: string, w: NonNullable<UsageWindow>) => {
    const p = Math.round(Math.max(0, Math.min(100, w.used_percentage)));
    return (
      <span className={pctColor(p)}>
        {label} {p}%
      </span>
    );
  };
  const rt = (w: NonNullable<UsageWindow>) => (w.resets_at ? ` (resets ${relFuture(w.resets_at)})` : '');
  const title =
    'Claude subscription usage' +
    (five ? ` · 5h ${Math.round(five.used_percentage)}%${rt(five)}` : '') +
    (seven ? ` · 7d ${Math.round(seven.used_percentage)}%${rt(seven)}` : '') +
    (data?.updated ? ` · ${relPast(data.updated)}` : '');

  return (
    <span
      title={title}
      className="text-[10.5px] mono text-muted border border-edge rounded px-2 py-0.5 shrink-0 inline-flex items-center gap-1.5"
    >
      {five && seg('5h', five)}
      {five && seven && <span className="text-muted/40">·</span>}
      {seven && seg('7d', seven)}
    </span>
  );
}
