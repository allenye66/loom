import type { ReactNode } from 'react';
import { useRefreshBrief, type BriefState, type BriefStatus } from '../api';

// Status → color, shared with the header button's dot (TerminalView).
export const STATUS_TONE: Record<BriefStatus, { dot: string; pill: string }> = {
  'on track': { dot: 'bg-ok', pill: 'text-ok border-ok/40 bg-ok/10' },
  'possibly drifted': { dot: 'bg-warn', pill: 'text-warn border-warn/40 bg-warn/10' },
  blocked: { dot: 'bg-bad', pill: 'text-bad border-bad/40 bg-bad/10' },
  'waiting on you': { dot: 'bg-accent', pill: 'text-accent border-accent-dim bg-accent/10' },
  done: { dot: 'bg-muted', pill: 'text-muted border-edge bg-surface-2' },
};

function rel(sec: number): string {
  const s = Date.now() / 1000 - sec;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

const cents = (usd: number | null | undefined) => {
  if (usd == null) return '';
  const c = usd * 100;
  return `${c < 1 ? c.toFixed(2) : c.toFixed(1)}¢`;
};

const nothing = (s: string) => /^\s*(nothing|none)\b/i.test(s);

function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <section className="px-3 py-2.5 border-b border-edge">
      <div className="text-[10px] mono uppercase tracking-wider text-muted mb-1.5">{label}</div>
      {children}
    </section>
  );
}

/** Per-chat status brief, docked above the notes drawer: a Haiku-written summary of the whole
 *  transcript (loom/core/briefs.py) — goal, status, working vs idle time, phases, who decided
 *  what. While open, the server refreshes it when the agent's turn ends or after 10 min of new
 *  work; ↻ forces one. */
export function StatusPanel({
  chatId,
  state,
  onClose,
  className = '',
}: {
  chatId: string;
  state: BriefState | undefined;
  onClose: () => void;
  className?: string;
}) {
  const refresh = useRefreshBrief(chatId);
  const b = state?.brief;
  const running = !!state?.running || refresh.isPending;

  let sub = '';
  if (running) sub = b ? 'updating…' : 'reading the transcript…';
  else if (state?.generated_at) sub = `updated ${rel(state.generated_at)}${state.stale ? ' · new activity since' : ''}`;

  return (
    <div className={`min-h-0 bg-surface flex flex-col ${className}`}>
      <div className="px-3 py-2 flex items-center gap-2 border-b border-edge shrink-0">
        <span className="text-[11px] mono text-accent shrink-0">◎ status</span>
        <span className={`text-[10px] mono truncate flex-1 ${running ? 'text-accent animate-pulse' : 'text-muted'}`}>
          {sub}
        </span>
        <button
          onClick={() => refresh.mutate()}
          disabled={running || state?.supported === false}
          title="regenerate the brief now (one Haiku call, ~20–30 s)"
          className="text-[10px] mono text-muted hover:text-accent border border-edge rounded px-1.5 py-0.5 shrink-0 disabled:opacity-40 disabled:pointer-events-none"
        >
          ↻
        </button>
        <button onClick={onClose} title="close status" className="text-muted hover:text-ink text-sm leading-none px-1 shrink-0">
          ✕
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto thin-scroll text-[12.5px] leading-relaxed text-ink">
        {state?.supported === false ? (
          <p className="px-3 py-3 text-muted">
            No Claude transcript for this chat yet. Status briefs read Claude chats only for now (not codex or grok).
          </p>
        ) : !b ? (
          <p className="px-3 py-3 text-muted">
            {state?.error && !running
              ? `Couldn't write a brief: ${state.error}`
              : 'Summarizing this chat with Haiku — about 20–30 seconds.'}
          </p>
        ) : (
          <>
            <section className="px-3 py-2.5 border-b border-edge">
              <span className={`inline-block text-[10.5px] mono px-2 py-0.5 rounded-full border ${STATUS_TONE[b.status]?.pill ?? STATUS_TONE.done.pill}`}>
                {b.status}
              </span>
              <p className="mt-1.5 text-muted">{b.reason}</p>
            </section>

            <Section label="goal">
              <p>{b.goal}</p>
            </Section>

            <Section label="time">
              <div className="mono text-[11.5px] flex flex-wrap gap-x-2">
                <span>{b.time.span}</span>
                <span className="text-muted">·</span>
                <span className="text-ok">{b.time.active} working</span>
                <span className="text-muted">·</span>
                <span className="text-warn">{b.time.idle} idle</span>
              </div>
              {b.time.note && <p className="mt-1 text-muted text-[11.5px]">{b.time.note}</p>}
            </Section>

            <Section label="timeline">
              <ol className="space-y-1.5">
                {b.phases.map((p, i) => (
                  <li key={i} className="flex gap-2">
                    <span className={`mono text-[10.5px] shrink-0 w-[74px] pt-px ${p.end === 'now' ? 'text-accent' : 'text-muted'}`}>
                      {p.start}–{p.end}
                    </span>
                    <span className={p.idle ? 'text-warn/80 italic' : ''}>{p.idle ? `idle — ${p.text}` : p.text}</span>
                  </li>
                ))}
              </ol>
            </Section>

            {b.decisions.length > 0 && (
              <Section label="decisions">
                <ul className="space-y-1.5">
                  {b.decisions.map((d, i) => (
                    <li key={i} className="flex gap-2">
                      <span
                        className={`mono text-[10px] shrink-0 h-fit mt-0.5 px-1.5 rounded border ${
                          d.by === 'you' ? 'text-accent border-accent-dim' : 'text-warn border-warn/40'
                        }`}
                      >
                        {d.by}
                      </span>
                      <span>{d.text}</span>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            <Section label="unasked">
              {b.unasked.length === 0 ? (
                <p className="text-muted">none</p>
              ) : (
                <ul className="space-y-1 list-disc pl-4 marker:text-warn">
                  {b.unasked.map((u, i) => (
                    <li key={i}>{u}</li>
                  ))}
                </ul>
              )}
            </Section>

            <Section label="needs you">
              <p className={nothing(b.needs_you) ? 'text-muted' : 'text-accent'}>{b.needs_you}</p>
            </Section>

            {state?.error && !running && (
              <p className="px-3 py-2 text-[11px] text-bad border-b border-edge">Last refresh failed: {state.error}</p>
            )}
          </>
        )}
      </div>

      <div className="px-3 py-1.5 border-t border-edge text-[10px] mono text-muted flex items-center justify-between shrink-0">
        <span>written by haiku from the transcript</span>
        {state?.cost_usd != null && (
          <span title="list-price equivalent; runs on your Claude login, so it counts toward plan usage rather than billing">
            {cents(state.cost_usd)} · {cents(state.total_cost_usd)} total
          </span>
        )}
      </div>
    </div>
  );
}
