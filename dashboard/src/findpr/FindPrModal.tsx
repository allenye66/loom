import { useEffect, useRef, useState } from 'react';
import { useOpenChat } from '../chat/openChat';
import { useActiveProjectName } from '../chat/activeProject';
import { Spinner } from '../components/Spinner';
import { toast } from '../ui/toast';
import { closeFindPr, useFindPrOpen } from './findPrStore';

/** "Find a PR": describe a PR you made — what it did, roughly when — and Haiku searches your PRs on GitHub and loom's
 *  chats (archived too) for it (core/findpr.py, D31). Suggestions come with a confidence and why, the chat that wrote
 *  each one, and a question that would narrow it down; answer it (or add any detail) to refine. */
type Chat = { id: string; name: string; archived: boolean; project?: string | null; branch?: string | null; cwd?: string | null; agent?: 'claude' | 'codex' | 'grok' | null; mode?: 'chat' | 'terminal' | null };
type PrInfo = { key?: string | null; repo?: string; number: number; title?: string | null; url?: string | null; state?: string | null; branch?: string | null; created?: number | null; merged?: number | null; additions?: number | null; deletions?: number | null; files?: number | null };
type Suggestion = { kind: 'pr' | 'chat'; confidence: 'high' | 'medium' | 'low'; why: string; pr: PrInfo | null; chats: Chat[]; id: string };
type Result = { suggestions: Suggestion[]; narrow_down: string; searched: { prs: number; chats: number; repos: string[]; since: string; until: string | null; widened: boolean }; notes: string[] };

const CONF: Record<Suggestion['confidence'], string> = {
  high: 'border-ok/50 text-ok bg-ok/10',
  medium: 'border-warn/50 text-warn bg-warn/10',
  low: 'border-edge text-muted',
};
const day = (t?: number | null) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '');
const RANGES: [string, number | null][] = [['auto', null], ['7 days', 7], ['30 days', 30], ['90 days', 90], ['1 year', 365]];

export function FindPrModal() {
  const open = useFindPrOpen();
  const project = useActiveProjectName();
  const openChat = useOpenChat();
  const [query, setQuery] = useState('');
  const [history, setHistory] = useState<string[]>([]);
  const [refine, setRefine] = useState('');
  const [days, setDays] = useState<number | null>(null);
  const [scope, setScope] = useState<'all' | 'project'>('all');
  const [phase, setPhase] = useState<string | null>(null); // non-null while a search runs
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  const run = useRef(0); // search generation: a stale poll must not overwrite a newer search

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && closeFindPr();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);
  if (!open) return null;

  const search = async (q: string, hist: string[], d: number | null) => {
    const gen = ++run.current;
    setError(null);
    setPhase('starting…');
    try {
      const r = await fetch('/api/find-pr', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ query: q, history: hist, days: d, project: scope === 'project' ? project : null }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => null))?.detail ?? `HTTP ${r.status}`);
      const { job } = await r.json();
      for (;;) {
        await new Promise((res) => setTimeout(res, 1100));
        if (gen !== run.current) return;
        const p = await fetch(`/api/find-pr/${job}`).then((x) => x.json());
        if (p.phase) setPhase(p.phase);
        if (p.done) {
          if (p.error) throw new Error(p.error);
          setResult(p.result as Result);
          break;
        }
      }
    } catch (e) {
      if (gen === run.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (gen === run.current) setPhase(null);
    }
  };
  const go = () => {
    if (!query.trim() || phase) return;
    setHistory([]);
    setResult(null);
    void search(query, [], days);
  };
  const narrow = (text: string, d: number | null = days) => {
    if (!text.trim() || phase) return;
    const hist = [...history, text.trim()];
    setHistory(hist);
    setRefine('');
    void search(query, hist, d);
  };
  const reference = (s: Suggestion) => {
    const p = s.pr;
    const ch = s.chats[0];
    const text =
      (p ? `PR ${p.key ?? '#' + p.number} “${p.title ?? ''}”${p.state ? ` (${p.state}${p.created ? ' ' + new Date(p.created * 1000).toISOString().slice(0, 10) : ''})` : ''}${p.url ? ' ' + p.url : ''}` : 'a PR (not found on GitHub)') +
      (ch ? ` — written in loom chat “${ch.name}”${ch.archived ? ' (archived)' : ''}` : '');
    void navigator.clipboard?.writeText(text).then(
      () => toast('copied a reference you can paste into another chat', { kind: 'ok' }),
      () => toast('couldn’t copy — select the text manually', { kind: 'err' }),
    );
  };
  const goChat = (c: Chat) => {
    closeFindPr();
    openChat({ resume: c.id, cwd: c.cwd ?? undefined, title: c.name, mode: 'terminal', agent: c.agent ?? undefined });
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center bg-black/50 backdrop-blur-sm p-4 sm:p-10" onClick={closeFindPr}>
      <div className="w-full max-w-3xl max-h-full flex flex-col rounded-xl border border-edge bg-surface shadow-2xl" onClick={(e) => e.stopPropagation()}>
        <div className="px-4 py-3 flex items-center gap-2.5 border-b border-edge shrink-0">
          <span className="text-sm font-semibold">Find a PR</span>
          <span className="text-[11px] mono text-muted">describe it — what it did, roughly when</span>
          <div className="flex-1" />
          <button onClick={closeFindPr} className="text-muted hover:text-ink text-lg leading-none px-1" aria-label="close">
            ✕
          </button>
        </div>

        <div className="px-4 py-3 border-b border-edge shrink-0 flex flex-col gap-2">
          <textarea
            autoFocus
            value={query}
            rows={3}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                go();
              }
            }}
            placeholder="e.g. “in this PR I added a new export button on the billing page sometime last week”"
            className="w-full resize-none text-[13px] px-3 py-2 rounded bg-canvas border border-edge outline-none focus:border-accent"
          />
          <div className="flex items-center gap-2 flex-wrap text-[10px] mono">
            <span className="text-muted">look back</span>
            {RANGES.map(([label, d]) => (
              <button key={label} onClick={() => setDays(d)} className={`px-2 py-0.5 rounded border ${days === d ? 'border-accent-dim text-accent bg-accent/10' : 'border-edge text-muted hover:text-ink'}`}>
                {label}
              </button>
            ))}
            <span className="text-muted ml-2">in</span>
            <button onClick={() => setScope('all')} className={`px-2 py-0.5 rounded border ${scope === 'all' ? 'border-accent-dim text-accent bg-accent/10' : 'border-edge text-muted hover:text-ink'}`}>
              all projects
            </button>
            {project && (
              <button onClick={() => setScope('project')} className={`px-2 py-0.5 rounded border ${scope === 'project' ? 'border-accent-dim text-accent bg-accent/10' : 'border-edge text-muted hover:text-ink'}`}>
                {project}
              </button>
            )}
            <div className="flex-1" />
            <button onClick={go} disabled={!query.trim() || !!phase} className="text-[11px] px-3 py-1 rounded bg-accent/15 text-accent border border-accent-dim disabled:opacity-40">
              {phase ? 'searching…' : 'find'}
            </button>
          </div>
        </div>

        <div className="overflow-auto thin-scroll p-3 flex flex-col gap-2.5 min-h-[6rem]">
          {history.length > 0 && (
            <div className="text-[10px] mono text-muted flex flex-wrap gap-1.5 items-center">
              refined with:
              {history.map((h, i) => (
                <span key={i} className="px-1.5 py-0.5 rounded border border-edge">
                  {h}
                </span>
              ))}
            </div>
          )}
          {phase && (
            <div className="flex items-center gap-2 text-[12px] text-muted py-6 justify-center">
              <Spinner size={12} /> {phase}
            </div>
          )}
          {error && <div className="text-[12px] text-bad px-1">{error}</div>}
          {!phase && !result && !error && (
            <div className="text-center text-muted py-8 text-[12px] leading-relaxed">
              Haiku reads your PRs on GitHub and loom’s chats — archived ones too — and suggests where it is.
              <br />
              The more you remember (the feature, the page, roughly when), the better. About 10–20 seconds.
            </div>
          )}
          {!phase &&
            result?.suggestions.map((s, i) => (
              <div key={s.id + i} className="rounded-lg border border-edge bg-surface-2/40 p-3">
                <div className="flex items-start gap-2">
                  <span className={`text-[10px] mono px-1.5 py-0.5 rounded border shrink-0 mt-0.5 ${CONF[s.confidence]}`}>{s.confidence}</span>
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] text-ink font-medium break-words">{s.pr?.title ?? 'a chat with no PR linked'}</div>
                    <div className="text-[10px] mono text-muted mt-0.5 flex flex-wrap gap-x-2">
                      {s.pr && <span>{s.pr.key ?? `#${s.pr.number}`}</span>}
                      {s.pr?.state && <span>{s.pr.state}</span>}
                      {s.pr?.created && <span>{day(s.pr.created)}</span>}
                      {s.pr?.additions != null && (
                        <span>
                          +{s.pr.additions}/-{s.pr.deletions ?? 0}
                          {s.pr.files ? ` · ${s.pr.files} files` : ''}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="text-[12px] text-ink/85 mt-2 break-words">{s.why}</div>
                {s.chats.map((c) => (
                  <div key={c.id} className="mt-2 flex items-center gap-2 text-[11px]">
                    <span className="text-muted shrink-0">chat</span>
                    <span className="text-ink truncate">{c.name}</span>
                    <span className={`text-[10px] mono px-1.5 py-0.5 rounded border shrink-0 ${c.archived ? 'border-edge text-muted' : 'border-ok/40 text-ok'}`}>{c.archived ? 'archived' : 'active'}</span>
                    {c.project && <span className="text-[10px] mono text-muted shrink-0">{c.project}</span>}
                    <div className="flex-1" />
                    <button onClick={() => goChat(c)} title={c.archived ? 'this chat is archived — opening it resumes it' : 'open this chat'} className="text-[11px] px-2 py-1 rounded border border-edge text-accent hover:bg-accent/10 shrink-0">
                      open chat →
                    </button>
                  </div>
                ))}
                <div className="mt-2.5 flex items-center gap-2">
                  {s.pr?.url && (
                    <a href={s.pr.url} target="_blank" rel="noreferrer" className="text-[11px] px-2 py-1 rounded border border-edge text-muted hover:text-ink">
                      open PR ↗
                    </a>
                  )}
                  <button onClick={() => reference(s)} className="text-[11px] px-2 py-1 rounded border border-edge text-muted hover:text-ink" title="copy a one-line reference (PR, link, chat) to paste into another chat">
                    copy reference
                  </button>
                </div>
              </div>
            ))}
          {!phase && result && result.suggestions.length === 0 && (
            <div className="text-center text-muted py-6 text-[12px]">
              No match. Try describing it differently, or look further back:
              <div className="mt-2 flex justify-center gap-2">
                {([90, 365] as const).map((d) => (
                  <button key={d} onClick={() => { setDays(d); void search(query, history, d); }} className="px-2 py-1 rounded border border-edge hover:text-ink text-[11px]">
                    {d === 90 ? 'last 90 days' : 'last year'}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

        {result && !phase && (
          <div className="px-4 py-2.5 border-t border-edge shrink-0 flex flex-col gap-1.5">
            {result.narrow_down && <div className="text-[11px] text-accent">to narrow it down: {result.narrow_down}</div>}
            <div className="flex gap-1.5">
              <input
                value={refine}
                onChange={(e) => setRefine(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && narrow(refine)}
                placeholder={result.narrow_down ? 'answer it, or add any detail…' : 'add a detail to narrow it down…'}
                className="flex-1 min-w-0 mono text-[11px] px-2.5 py-1.5 rounded bg-canvas border border-edge outline-none focus:border-accent"
              />
              <button onClick={() => narrow(refine)} disabled={!refine.trim()} className="text-[11px] px-3 py-1 rounded bg-accent/15 text-accent border border-accent-dim disabled:opacity-40">
                refine
              </button>
            </div>
            <div className="text-[10px] mono text-muted/70">
              searched {result.searched.prs} PRs · {result.searched.chats} chats · {result.searched.repos.join(', ') || 'no GitHub repo'} · since {result.searched.since}
              {result.notes.map((n, i) => (
                <span key={i} className="block text-warn/80">
                  {n}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
