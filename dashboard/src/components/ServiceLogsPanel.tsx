/**
 * Live service/test log drawer for a worktree task.
 *
 * Sits under the chat terminal: tabs per service, SSE follow, filter, wrap,
 * copy, clear, resizable height. Backed by /api/tasks/:id/logs* (efficient
 * end-of-file tail — never loads multi‑MB files whole).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Task } from '../api';

const HEIGHT_KEY = 'loom.logsPanelHeight';
const OPEN_KEY = 'loom.logsPanelOpen';
const KIND_KEY = 'loom.logsPanelKind';
const MIN_H = 140;
const MAX_H = () => Math.max(220, Math.floor(window.innerHeight * 0.7));
const DEFAULT_H = 280;

export type LogsPanelApi = {
  open: boolean;
  setOpen: (v: boolean | ((p: boolean) => boolean)) => void;
  kind: string;
  setKind: (k: string) => void;
};

type KindInfo = {
  kind: string;
  source: string;
  exists: boolean;
  size: number;
  mtime: number | null;
  path: string;
};

function loadNum(key: string, fallback: number) {
  try {
    const n = Number(localStorage.getItem(key));
    return Number.isFinite(n) && n > 0 ? n : fallback;
  } catch {
    return fallback;
  }
}
function loadBool(key: string, fallback: boolean) {
  try {
    const v = localStorage.getItem(key);
    if (v === null) return fallback;
    return v === '1' || v === 'true';
  } catch {
    return fallback;
  }
}
function loadStr(key: string, fallback: string) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10_240 ? 1 : 0)} KB`;
  return `${(n / (1024 * 1024)).toFixed(n < 10 * 1024 * 1024 ? 1 : 0)} MB`;
}

/** Classify a log line for subtle coloring (best-effort; never hide content). */
function lineTone(line: string): string {
  const s = line.toLowerCase();
  if (
    /\b(error|exception|traceback|fatal|critical|panic)\b/.test(s) ||
    /\b5\d{2}\b/.test(line) ||
    /err!/i.test(line)
  )
    return 'text-bad/90';
  if (/\b(warn(ing)?|deprecated)\b/.test(s) || /\b4\d{2}\b/.test(line)) return 'text-warn/90';
  if (/\b(success|✓|passed|ready|listening|compiled)\b/.test(s)) return 'text-ok/85';
  if (/\b(debug|trace)\b/.test(s)) return 'text-muted/60';
  if (/\b(info)\b/.test(s) || /^\s*\[?info\]?/i.test(line)) return 'text-muted';
  return 'text-ink/85';
}

function serviceHealthy(task: Task, kind: string): boolean | null {
  if (kind === 'test') return null;
  const svc = task.services?.find((s) => s.name === kind);
  if (!svc) return null;
  return !!svc.healthy;
}

function preferredKinds(task: Task, remote: KindInfo[] | undefined): string[] {
  const fromRemote = remote?.map((k) => k.kind) ?? [];
  const fromTask = (task.services ?? []).map((s) => s.name);
  const defaults = ['backend', 'frontend', 'test'];
  const ordered: string[] = [];
  const seen = new Set<string>();
  for (const k of [...fromTask, ...fromRemote, ...defaults]) {
    if (!k || seen.has(k)) continue;
    seen.add(k);
    ordered.push(k);
  }
  // Put test last if present.
  const testIdx = ordered.indexOf('test');
  if (testIdx >= 0 && testIdx !== ordered.length - 1) {
    ordered.splice(testIdx, 1);
    ordered.push('test');
  }
  return ordered;
}

/** Hook + controller so DevStackBar and the panel share open/kind state. */
export function useLogsPanel(taskId: string | undefined): LogsPanelApi {
  const [open, setOpenRaw] = useState(() => loadBool(OPEN_KEY, false));
  const [kind, setKindRaw] = useState(() => loadStr(KIND_KEY, 'backend'));

  const setOpen = useCallback((v: boolean | ((p: boolean) => boolean)) => {
    setOpenRaw((prev) => {
      const next = typeof v === 'function' ? v(prev) : v;
      try {
        localStorage.setItem(OPEN_KEY, next ? '1' : '0');
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  const setKind = useCallback((k: string) => {
    setKindRaw(k);
    try {
      localStorage.setItem(KIND_KEY, k);
    } catch {
      /* ignore */
    }
  }, []);

  // Reset kind preference only when switching tasks if current kind has no meaning — keep simple.
  useEffect(() => {
    void taskId;
  }, [taskId]);

  return { open, setOpen, kind, setKind };
}

type Props = {
  task: Task;
  kind: string;
  onKindChange: (k: string) => void;
  onClose: () => void;
};

export function ServiceLogsPanel({ task, kind, onKindChange, onClose }: Props) {
  const [height, setHeight] = useState(() => loadNum(HEIGHT_KEY, DEFAULT_H));
  const [text, setText] = useState('');
  const [filter, setFilter] = useState('');
  const [follow, setFollow] = useState(true);
  const [wrap, setWrap] = useState(true);
  const [live, setLive] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [size, setSize] = useState(0);
  const [path, setPath] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [kinds, setKinds] = useState<KindInfo[] | undefined>();
  const [showFilter, setShowFilter] = useState(false);

  const scrollerRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true); // user wants bottom unless they scrolled up
  const filterRef = useRef<HTMLInputElement>(null);
  const offsetRef = useRef(0);
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);

  // --- kind list ---
  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetch(`/api/tasks/${task.id}/logs/kinds`)
        .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
        .then((d) => {
          if (!cancelled) setKinds(d.kinds as KindInfo[]);
        })
        .catch(() => {
          if (!cancelled) setKinds(undefined);
        });
    };
    load();
    const id = window.setInterval(load, 8000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [task.id]);

  const kindTabs = useMemo(() => preferredKinds(task, kinds), [task, kinds]);

  // Ensure selected kind is in the tab list.
  useEffect(() => {
    if (kindTabs.length && !kindTabs.includes(kind)) onKindChange(kindTabs[0]);
  }, [kindTabs, kind, onKindChange]);

  // --- SSE live follow ---
  useEffect(() => {
    setText('');
    setErr(null);
    setTruncated(false);
    setSize(0);
    setPath(null);
    setLive(false);
    offsetRef.current = 0;
    stickRef.current = true;

    const es = new EventSource(`/api/tasks/${task.id}/logs/stream?kind=${encodeURIComponent(kind)}`);
    let opened = false;

    es.onopen = () => {
      opened = true;
      setLive(true);
      setErr(null);
    };
    es.onerror = () => {
      // EventSource reconnects automatically; only show error if we never connected.
      if (!opened) setErr('log stream disconnected — retrying…');
      setLive(false);
    };
    es.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data) as {
          type: string;
          log?: string;
          offset?: number;
          size?: number;
          path?: string;
          truncated?: boolean;
          message?: string;
        };
        if (m.type === 'ready') {
          setText(m.log ?? '');
          offsetRef.current = m.offset ?? 0;
          setSize(m.size ?? 0);
          if (m.path) setPath(m.path);
          setTruncated(!!m.truncated);
          setLive(true);
          setErr(null);
        } else if (m.type === 'append' && m.log) {
          setText((prev) => {
            // Cap client buffer so a long-running panel doesn't grow forever.
            const next = prev + m.log;
            if (next.length > 1_200_000) return next.slice(next.length - 900_000);
            return next;
          });
          offsetRef.current = m.offset ?? offsetRef.current;
          if (m.size != null) setSize(m.size);
        } else if (m.type === 'reset') {
          setText(m.log ?? '');
          offsetRef.current = m.offset ?? 0;
          if (m.size != null) setSize(m.size);
          setTruncated(!!m.truncated);
        } else if (m.type === 'error') {
          setErr(m.message || 'stream error');
        } else if (m.type === 'ping') {
          setLive(true);
          if (m.size != null) setSize(m.size);
        }
      } catch {
        /* ignore malformed */
      }
    };

    return () => {
      es.close();
      setLive(false);
    };
  }, [task.id, kind]);

  // --- auto-scroll ---
  useEffect(() => {
    if (!follow || !stickRef.current) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [text, follow, filter]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = dist < 40;
    stickRef.current = atBottom;
    if (atBottom && !follow) setFollow(true);
    if (!atBottom && follow) setFollow(false);
  };

  // --- resize drag ---
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      // Dragging the top handle up increases height.
      const next = Math.min(MAX_H(), Math.max(MIN_H, d.startH + (d.startY - e.clientY)));
      setHeight(next);
    };
    const onUp = () => {
      if (!dragRef.current) return;
      dragRef.current = null;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      setHeight((h) => {
        try {
          localStorage.setItem(HEIGHT_KEY, String(h));
        } catch {
          /* ignore */
        }
        return h;
      });
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, []);

  // ⌘F / Ctrl+F focuses filter when panel is open; Escape clears filter then closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key === 'f') {
        e.preventDefault();
        setShowFilter(true);
        requestAnimationFrame(() => filterRef.current?.focus());
      } else if (e.key === 'Escape') {
        if (filter) {
          setFilter('');
          setShowFilter(false);
        } else {
          onClose();
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [filter, onClose]);

  const lines = useMemo(() => {
    if (!text) return [] as string[];
    // Keep empty trailing line visible if the stream ends with \n mid-write.
    return text.split('\n');
  }, [text]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return lines;
    return lines.filter((l) => l.toLowerCase().includes(q));
  }, [lines, filter]);

  const healthy = serviceHealthy(task, kind);
  const kindMeta = kinds?.find((k) => k.kind === kind);

  const jumpBottom = () => {
    stickRef.current = true;
    setFollow(true);
    const el = scrollerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const copyAll = async () => {
    const body = filter ? filtered.join('\n') : text;
    try {
      await navigator.clipboard.writeText(body);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* ignore */
    }
  };

  const clearLog = async () => {
    if (!confirm(`Clear ${kind} log for this worktree? This truncates the file on disk.`)) return;
    setClearing(true);
    try {
      const r = await fetch(`/api/tasks/${task.id}/logs?kind=${encodeURIComponent(kind)}`, {
        method: 'DELETE',
      });
      if (r.ok) {
        setText('');
        setSize(0);
        setTruncated(false);
        offsetRef.current = 0;
      }
    } finally {
      setClearing(false);
    }
  };

  const emptyHint =
    kind === 'test'
      ? 'No test output yet — run tests from the task card.'
      : healthy === false
        ? `No ${kind} log yet — start the stack to capture stdout/stderr.`
        : healthy === true
          ? `Waiting for ${kind} output…`
          : `No ${kind} log yet. Start the dev stack to create the log file.`;

  return (
    <div
      className="shrink-0 border-t border-edge bg-surface flex flex-col"
      style={{ height }}
      role="region"
      aria-label="Service logs"
    >
      {/* drag handle */}
      <div
        className="h-1.5 cursor-ns-resize group relative flex items-center justify-center hover:bg-accent/10 active:bg-accent/20"
        onMouseDown={(e) => {
          e.preventDefault();
          dragRef.current = { startY: e.clientY, startH: height };
          document.body.style.cursor = 'ns-resize';
          document.body.style.userSelect = 'none';
        }}
        title="drag to resize"
      >
        <div className="w-10 h-0.5 rounded-full bg-edge group-hover:bg-accent-dim transition-colors" />
      </div>

      {/* toolbar */}
      <div className="px-3 pb-1.5 flex items-center gap-1.5 flex-wrap shrink-0">
        <span className="text-[10.5px] mono text-muted uppercase tracking-wide shrink-0 mr-0.5">logs</span>

        <div className="flex items-center gap-0.5 bg-surface-2 rounded-md border border-edge p-0.5">
          {kindTabs.map((k) => {
            const up = serviceHealthy(task, k);
            const active = k === kind;
            return (
              <button
                key={k}
                onClick={() => onKindChange(k)}
                className={`px-2 py-0.5 rounded text-[11px] mono transition-colors ${
                  active
                    ? 'bg-accent/20 text-accent border border-accent-dim'
                    : 'text-muted hover:text-ink border border-transparent'
                }`}
                title={
                  k === 'test'
                    ? 'pytest / test runner output'
                    : up === true
                      ? `${k} is healthy`
                      : up === false
                        ? `${k} is down`
                        : k
                }
              >
                {k === 'frontend' ? 'FE' : k === 'backend' ? 'BE' : k}
                {up === true && <span className="text-ok ml-1">●</span>}
                {up === false && <span className="text-muted/50 ml-1">○</span>}
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-1.5 text-[10.5px] mono text-muted shrink-0">
          <span
            className={`inline-flex items-center gap-1 ${live ? 'text-ok' : 'text-muted'}`}
            title={live ? 'live — streaming new lines' : 'reconnecting…'}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${live ? 'bg-ok animate-pulse' : 'bg-muted/40'}`} />
            {live ? 'live' : '…'}
          </span>
          {size > 0 && (
            <span className="text-muted/70" title={path ?? undefined}>
              {formatBytes(size)}
            </span>
          )}
          {truncated && (
            <span className="text-warn/80" title="showing the end of a larger file">
              truncated
            </span>
          )}
        </div>

        <div className="flex-1 min-w-[8px]" />

        {showFilter || filter ? (
          <input
            ref={filterRef}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="filter…"
            className="w-36 mono text-[11px] px-2 py-0.5 rounded bg-canvas border border-edge outline-none focus:border-accent text-ink placeholder:text-muted/50"
          />
        ) : (
          <button
            onClick={() => {
              setShowFilter(true);
              requestAnimationFrame(() => filterRef.current?.focus());
            }}
            className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5"
            title="filter lines (⌘F)"
          >
            filter
          </button>
        )}

        <button
          onClick={() => setWrap((v) => !v)}
          className={`text-[11px] mono border rounded px-2 py-0.5 ${
            wrap ? 'border-accent-dim text-accent' : 'border-edge text-muted hover:text-ink'
          }`}
          title="toggle word wrap"
        >
          wrap
        </button>

        <button
          onClick={() => (follow ? setFollow(false) : jumpBottom())}
          className={`text-[11px] mono border rounded px-2 py-0.5 ${
            follow ? 'border-accent-dim text-accent' : 'border-edge text-muted hover:text-ink'
          }`}
          title={follow ? 'following new output' : 'scroll to bottom & follow'}
        >
          {follow ? '↓ follow' : '↓ jump'}
        </button>

        <button
          onClick={copyAll}
          className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5"
          title={filter ? 'copy filtered lines' : 'copy all visible log text'}
        >
          {copied ? 'copied' : 'copy'}
        </button>

        <button
          onClick={clearLog}
          disabled={clearing}
          className="text-[11px] mono text-muted hover:text-bad border border-edge rounded px-2 py-0.5 disabled:opacity-40"
          title="truncate this log file on disk"
        >
          clear
        </button>

        <button
          onClick={onClose}
          className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5"
          title="close log panel (Esc)"
        >
          ✕
        </button>
      </div>

      {/* body */}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="flex-1 min-h-0 overflow-auto thin-scroll px-3 pb-2 bg-canvas/60"
      >
        {err && (
          <div className="text-[11px] mono text-warn py-1 sticky top-0 bg-canvas/90 z-10">{err}</div>
        )}
        {!text && !err && (
          <div className="h-full flex items-center justify-center">
            <div className="text-center max-w-sm px-4">
              <div className="text-[12px] mono text-muted mb-1.5">{emptyHint}</div>
              {kind !== 'test' && healthy !== true && (
                <div className="text-[10.5px] text-muted/70">
                  Logs only capture processes loom starts (dev stack start).
                </div>
              )}
              {kindMeta?.path && (
                <div className="text-[10px] mono text-muted/50 mt-2 break-all">{kindMeta.path}</div>
              )}
            </div>
          </div>
        )}
        {!!text && filtered.length === 0 && (
          <div className="text-[11px] mono text-muted py-6 text-center">
            no lines match “{filter}”
          </div>
        )}
        {!!text && filtered.length > 0 && (
          <pre
            className={`text-[11px] leading-[1.55] mono select-text ${
              wrap ? 'whitespace-pre-wrap break-words' : 'whitespace-pre'
            }`}
          >
            {filtered.map((line, i) => (
              <div key={i} className={`${lineTone(line)} hover:bg-surface-2/40 px-0.5 -mx-0.5 rounded-sm`}>
                {line || ' '}
              </div>
            ))}
          </pre>
        )}
      </div>

      {/* footer path */}
      {path && text && (
        <div
          className="px-3 py-0.5 border-t border-edge/60 text-[10px] mono text-muted/50 truncate shrink-0"
          title={path}
        >
          {path}
          {filter ? ` · ${filtered.length}/${lines.length} lines` : lines.length ? ` · ${lines.length} lines` : ''}
        </div>
      )}
    </div>
  );
}
