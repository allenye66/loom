import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { ChatSidebar, DevStackBar, OpenInIde } from '../chat/ChatSidebar';
import { PrBadges } from '../components/PrBadges';
import { ServiceLogsPanel, useLogsPanel } from '../components/ServiceLogsPanel';
import type { Task } from '../api';

// Match the app palette (index.css @theme tokens) so the TUI feels native.
const THEME = {
  background: '#0a0c11',
  foreground: '#e7eaf3',
  cursor: '#8b7cf6',
  cursorAccent: '#0a0c11',
  selectionBackground: '#5a4fcf66',
};

/** Which server-side host backs a terminal session. `pty` = the smooth-scroll daemon
 *  (xterm owns scrollback); `tmux` = the classic fullscreen mode. */
type TermBackend = 'pty' | 'tmux';

// Smooth wheel scroll over xterm's OWN scrollback (pty mode: xterm owns the
// buffer; there's no tmux to forward SGR wheel escapes to). Mouse-wheel notches
// glide over ~150ms instead of jumping N lines; trackpads skip this (the OS
// already gives momentum at high event frequency).
const SMOOTH_WHEEL_DURATION_MS = 150;

interface WheelAnimState {
  rafId: number | null;
  targetDelta: number; // remaining lines to scroll (signed)
  scrolledSoFar: number; // lines already applied this segment
  startTime: number;
}

function isMouseWheelEvent(deltaY: number, deltaMode: number): boolean {
  if (deltaMode === 1) return true; // explicit LINE mode = classic wheel
  return Math.abs(deltaY) >= 40; // big pixel delta per notch = wheel
}

function animateWheelScroll(
  term: { scrollLines: (n: number) => void; buffer?: any; rows?: number },
  deltaLines: number,
  state: WheelAnimState,
): void {
  if (deltaLines === 0) return;
  // Skip when the requested direction has no room (avoids pointless rAF loops
  // xterm would clamp to no-ops at the buffer edges). Direction-scoped so an
  // overshoot during easing never locks both directions.
  const buf = term.buffer?.active;
  if (buf && typeof buf.viewportY === 'number') {
    const rows = term.rows ?? 0;
    const atBottom = buf.viewportY >= buf.length - rows;
    const atTop = buf.viewportY <= 0;
    if (deltaLines > 0 && atBottom) return;
    if (deltaLines < 0 && atTop) return;
  }
  // Extend the target: remaining work + this new notch (accumulate rapid notches
  // into the in-flight animation and reset the easing clock).
  const remaining = state.targetDelta - state.scrolledSoFar;
  state.targetDelta = remaining + deltaLines;
  state.scrolledSoFar = 0;
  state.startTime = performance.now();
  if (state.rafId !== null) return; // already animating; picks up new target

  const step = (t: number) => {
    const progress = Math.min(1, (t - state.startTime) / SMOOTH_WHEEL_DURATION_MS);
    const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic
    const target = Math.round(state.targetDelta * eased);
    const delta = target - state.scrolledSoFar;
    if (delta !== 0) {
      term.scrollLines(delta);
      state.scrolledSoFar = target;
    }
    if (progress < 1) {
      state.rafId = requestAnimationFrame(step);
    } else {
      state.rafId = null;
      state.targetDelta = 0;
      state.scrolledSoFar = 0;
    }
  };
  state.rafId = requestAnimationFrame(step);
}

function snapTermToBottom(
  term: { scrollToBottom?: () => void } | null | undefined,
  wheelAnim: WheelAnimState | null,
): void {
  // Cancel any in-flight wheel animation FIRST, else its next frame yanks the
  // viewport back up after we snap.
  if (wheelAnim && wheelAnim.rafId !== null) {
    cancelAnimationFrame(wheelAnim.rafId);
    wheelAnim.rafId = null;
    wheelAnim.targetDelta = 0;
    wheelAnim.scrolledSoFar = 0;
  }
  if (term && typeof term.scrollToBottom === 'function') term.scrollToBottom();
}

/** Dropdown to open a native Terminal for this worktree — either ATTACH to the live claude
 *  session (tmux backend only; tmux supports multiple clients) or open a PLAIN SHELL in the
 *  worktree (no claude). */
function OpenTerminalMenu({ chatId, cwd, backend }: { chatId?: string; cwd?: string; backend: TermBackend | null }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);
  if (!chatId) return null;

  const run = async (which: 'claude' | 'shell') => {
    setOpen(false);
    setBusy(true);
    setErr(false);
    try {
      const r =
        which === 'claude'
          ? await fetch(`/api/terminals/${chatId}/open-native`, { method: 'POST' })
          : await fetch('/api/shell', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ cwd }),
            });
      setErr(!r.ok);
    } catch {
      setErr(true);
    }
    setBusy(false);
    setTimeout(() => setErr(false), 2500);
  };

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title="open a native Terminal — the live claude session, or a plain shell in this worktree"
        className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5 disabled:opacity-50"
      >
        {busy ? 'opening…' : err ? 'failed' : '⧉ terminal ▾'}
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-20 w-56 rounded-md border border-edge bg-surface shadow-xl overflow-hidden">
          {backend === 'tmux' && (
            <button onClick={() => run('claude')} className="w-full text-left px-3 py-2 hover:bg-surface-2 flex flex-col gap-0.5">
              <span className="text-[11px] mono text-ink">❯ attach claude</span>
              <span className="text-[10px] text-muted">the live session — tmux attach, stays in sync</span>
            </button>
          )}
          <button
            onClick={() => run('shell')}
            disabled={!cwd}
            className="w-full text-left px-3 py-2 hover:bg-surface-2 border-t border-edge first:border-t-0 flex flex-col gap-0.5 disabled:opacity-40"
          >
            <span className="text-[11px] mono text-ink">$ plain shell</span>
            <span className="text-[10px] text-muted">no claude — a shell in this worktree</span>
          </button>
        </div>
      )}
    </div>
  );
}

/** Renderer picker: smooth-scroll (pty, default) vs classic (tmux). Switching an existing
 *  session is a quick restart that keeps the conversation (kill host + `claude --resume`),
 *  not a live flip — so we confirm, POST the switch, then remount the terminal (fresh xterm
 *  + WS; the server relaunches claude under the new host). */
function RendererMenu({
  chatId,
  backend,
  onSwitched,
}: {
  chatId?: string;
  backend: TermBackend | null;
  onSwitched: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);
  if (!chatId || !backend) return null;

  const pick = async (target: TermBackend) => {
    setOpen(false);
    if (target === backend || busy) return;
    if (
      !window.confirm(
        'Switch the renderer? This restarts claude for this session — your conversation is kept ' +
          '(it relaunches with --resume). Best done while claude is idle.',
      )
    )
      return;
    setBusy(true);
    setErr(false);
    try {
      const r = await fetch(`/api/terminals/${chatId}/backend`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ target }),
      });
      if (r.ok) onSwitched();
      else setErr(true);
    } catch {
      setErr(true);
    }
    setBusy(false);
    setTimeout(() => setErr(false), 2500);
  };

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        title="how this session renders — switching restarts claude (conversation kept)"
        className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5 disabled:opacity-50"
      >
        {busy ? 'switching…' : err ? 'failed' : `renderer: ${backend === 'pty' ? 'smooth' : 'classic'} ▾`}
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-20 w-64 rounded-md border border-edge bg-surface shadow-xl overflow-hidden">
          <button onClick={() => pick('pty')} className="w-full text-left px-3 py-2 hover:bg-surface-2 flex flex-col gap-0.5">
            <span className="text-[11px] mono text-ink">smooth-scroll (pty) {backend === 'pty' ? '·  current' : ''}</span>
            <span className="text-[10px] text-muted">native scroll, drag-select/copy — like claude in a normal terminal</span>
          </button>
          <button
            onClick={() => pick('tmux')}
            className="w-full text-left px-3 py-2 hover:bg-surface-2 border-t border-edge flex flex-col gap-0.5"
          >
            <span className="text-[11px] mono text-ink">classic (tmux) {backend === 'tmux' ? '·  current' : ''}</span>
            <span className="text-[10px] text-muted">legacy fullscreen mode — fallback; supports native tmux attach</span>
          </button>
        </div>
      )}
    </div>
  );
}

function toolLine(t: any): string {
  const i = t?.input || {};
  const arg = i.command ?? i.file_path ?? i.pattern ?? i.path ?? '';
  return `→ ${t?.name ?? 'tool'}${arg ? ` ${typeof arg === 'string' ? arg : JSON.stringify(arg)}` : ''}`;
}

/** Read-only, fully selectable view of the conversation (from the saved transcript) — the
 *  workaround for the fullscreen TUI, where you can't drag-select across scroll. Open it,
 *  select any part across as much history as you want, ⌘C, close. */
function CopyTextPanel({ chatId, onClose }: { chatId: string; onClose: () => void }) {
  const { data: items, isLoading } = useQuery({
    queryKey: ['transcript', chatId],
    queryFn: () => fetch(`/api/chats/${chatId}/transcript`).then((r) => r.json()).then((d) => (d.items ?? []) as any[]),
    refetchOnWindowFocus: false,
  });
  return (
    <div className="absolute inset-0 z-10 bg-canvas flex flex-col">
      <div className="flex items-center gap-2 px-4 py-2 border-b border-edge bg-surface shrink-0">
        <span className="text-[11px] mono text-accent">conversation text — select any part &amp; ⌘C</span>
        <div className="flex-1" />
        <button onClick={onClose} className="text-xs px-2.5 py-1.5 rounded border border-edge text-muted hover:text-ink">
          close
        </button>
      </div>
      <div className="flex-1 overflow-auto thin-scroll px-5 py-4 text-[13px] leading-relaxed select-text">
        {isLoading && <div className="text-muted mono text-xs">loading…</div>}
        {!isLoading && (items?.length ?? 0) === 0 && (
          <div className="text-muted mono text-xs">no transcript yet — it fills in as turns complete.</div>
        )}
        {(items ?? []).map((it: any, i: number) =>
          it.kind === 'user' ? (
            <div key={i} className="my-3 whitespace-pre-wrap text-accent">{'› ' + (it.text || '')}</div>
          ) : it.kind === 'error' ? (
            <div key={i} className="my-3 whitespace-pre-wrap text-bad">{it.text}</div>
          ) : (
            <div key={i} className="my-3">
              {it.thinking && <div className="whitespace-pre-wrap text-muted/50 text-[12px] mb-1">{it.thinking}</div>}
              {(it.tools ?? []).map((t: any, j: number) => (
                <div key={j} className="whitespace-pre-wrap text-muted text-[12px]">{toolLine(t)}</div>
              ))}
              {it.text && <div className="whitespace-pre-wrap text-ink mt-1">{it.text}</div>}
            </div>
          ),
        )}
      </div>
    </div>
  );
}

/**
 * Terminal mode: the *real* interactive `claude` TUI, bridged from a server-side PTY to
 * xterm.js. No reimplementation — every slash command / permission prompt is claude's own.
 * Two hosts (see loom/core/terminals.py): `pty` (default) = a detached daemon, inline
 * renderer, xterm owns scrollback → native smooth scroll + select/copy; `tmux` (classic) =
 * fullscreen-pinned, scroll forwarded as SGR wheel events. Both outlive this tab and loom
 * restarts, so a dropped socket just reconnects to the running session. Wraps the
 * conversation pane in loom's shell (the `ChatSidebar` + dev-stack bar).
 */
export function TerminalView({
  resume,
  cwd,
  title,
  onClose,
}: {
  resume?: string;
  cwd?: string;
  title?: string;
  onClose: () => void;
}) {
  const holderRef = useRef<HTMLDivElement>(null);
  // Which host backs this session — reported by the server on WS open. Drives the header
  // label/menus; the live wheel/snapshot branching uses the effect-local mirror below.
  const [backend, setBackend] = useState<TermBackend | null>(null);
  // Bumped after a renderer switch → remounts the whole terminal (fresh xterm state — the
  // old host's alt-screen/scrollback state must not leak into the new one) + a fresh WS.
  const [termEpoch, setTermEpoch] = useState(0);

  // Dev-stack parity: map this worktree (cwd) to its loom task for the FE/BE + start/stop strip.
  const { data: tasks } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => fetch('/api/tasks').then((r) => r.json()).then((d) => d.tasks as Task[]),
    refetchInterval: 4000,
  });
  const task = tasks?.find(
    (t) => t.worktree_path && (cwd === t.worktree_path || (cwd?.startsWith(t.worktree_path + '/') ?? false)),
  );
  // Live service/test logs drawer (shared open/kind state with DevStackBar).
  const logsPanel = useLogsPanel(task?.id);

  // The chat's branch + auto-detected PRs (from the transcript's pr-link records), with live
  // GitHub merge status (open / tests passing / ready to merge / error / merged …).
  const { data: chatMeta } = useQuery({
    queryKey: ['chat-meta', resume],
    queryFn: () => fetch(`/api/chats/${resume}`).then((r) => r.json()).then((d) => d.chat),
    enabled: !!resume,
    refetchInterval: 30000,
  });

  // Drag an image in → save it server-side and type its path into claude's input (mirrors a
  // native terminal's image drop). xterm has no file-drop handling, so we do it on the wrapper.
  const [dragOver, setDragOver] = useState(false);
  const [showText, setShowText] = useState(false); // selectable transcript panel (copy workaround)

  useEffect(() => {
    const term = new Terminal({
      fontFamily: "ui-monospace, 'SF Mono', 'JetBrains Mono', Menlo, monospace",
      fontSize: 13,
      cursorBlink: true,
      scrollback: 12000,
      theme: THEME,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(holderRef.current!);
    term.focus();

    let ws: WebSocket | null = null;
    let disposed = false;
    let pingTimer: number | undefined;
    let retry = 0;
    let lastSentDims = ''; // last "COLSxROWS" sent — dedup redundant resizes (each is a SIGWINCH → full re-render)
    // Which host backs this session — the server says in its `backend` control frame.
    // Branches the wheel path: tmux owns the screen (forward SGR wheel events), pty gives
    // xterm its own scrollback (scroll locally). Default pty = the server's default.
    let liveBackend: TermBackend = 'pty';
    // Persistent streaming UTF-8 decoder. claude's TUI is mostly box-drawing/Unicode; a multibyte
    // char split across two WS frames would decode to U+FFFD if each frame were decoded on its own.
    // decode(…, {stream:true}) carries the partial bytes into the next frame; flushed on reconnect
    // (below) so stale bytes from a dead socket can't splice onto the new stream.
    const dec = new TextDecoder('utf-8');
    const send = (o: unknown) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(o));
    };
    // After a resize settles (or a tmux scroll), ask the server for a clean repaint — tmux
    // answers with a refresh-client redraw, pty with a settled snapshot bracket (below).
    // Self-heals display tearing without a manual browser refresh.
    let repaintTimer: number | undefined;
    const scheduleRepaint = () => {
      window.clearTimeout(repaintTimer);
      repaintTimer = window.setTimeout(() => send({ type: 'repaint' }), 200);
    };
    // Send a resize only when the dims actually changed and aren't degenerate. Each resize is a
    // SIGWINCH → full Ink re-render, so redundant ones cause flashes/tearing; a <20col / <5row size
    // (collapsed/hidden pane) makes claude reflow to a few columns and corrupts the TUI, so drop it.
    const sendResize = (cols: number, rows: number) => {
      if (cols < 20 || rows < 5) return;
      const k = `${cols}x${rows}`;
      if (k === lastSentDims) return;
      lastSentDims = k;
      send({ type: 'resize', cols, rows });
    };

    // Snapshot bracketing (pty backend): between snapshot-start and snapshot-end, buffer
    // binary frames instead of writing them live, then apply all at once — reset + one
    // write, so the settled snapshot atomically REPLACES the screen and scrollback (the
    // payload is the full authoritative state; writing it on top would duplicate history).
    let snapBuffering = false;
    let snapBuf: Uint8Array[] = [];
    let snapTimer: number | undefined;
    const resetSnap = () => {
      snapBuffering = false;
      snapBuf = [];
      if (snapTimer) {
        window.clearTimeout(snapTimer);
        snapTimer = undefined;
      }
    };

    const wheelAnim: WheelAnimState = { rafId: null, targetDelta: 0, scrolledSoFar: 0, startTime: 0 };

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/api/ws/term`);
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => {
        retry = 0;
        // First frame carries the chat id + cwd + initial size; the server (re)attaches
        // the session and replays its recent output.
        send({ chat_id: resume, cwd, cols: term.cols, rows: term.rows });
        lastSentDims = `${term.cols}x${term.rows}`; // session is (re)attached at this size — seed the dedup
        pingTimer = window.setInterval(() => send({ type: 'ping' }), 25000);
      };
      ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const m = JSON.parse(ev.data);
            if (m.type === 'backend') {
              liveBackend = m.backend === 'tmux' ? 'tmux' : 'pty';
              setBackend(liveBackend);
            } else if (m.type === 'exit') {
              term.write('\r\n\x1b[2m[claude exited — close and reopen to start a new session]\x1b[0m\r\n');
            } else if (m.type === 'error') {
              term.write(`\r\n\x1b[31m[loom] ${m.message}\x1b[0m\r\n`);
            } else if (m.type === 'snapshot-start') {
              // Begin buffering; a 3s fallback returns to live if end never arrives.
              snapBuffering = true;
              snapBuf = [];
              if (snapTimer) window.clearTimeout(snapTimer);
              snapTimer = window.setTimeout(resetSnap, 3000);
            } else if (m.type === 'snapshot-end') {
              let total = 0;
              for (const c of snapBuf) total += c.length;
              const combined = new Uint8Array(total);
              let off = 0;
              for (const c of snapBuf) {
                combined.set(c, off);
                off += c.length;
              }
              // Reset first: the snapshot is the FULL authoritative state (for inline
              // content it includes the scrollback history), so it must replace the
              // buffer, not append — and a reset also clears any stale alt-screen mode
              // left by the previous host after a renderer switch. Decode with a FRESH
              // (non-streaming) decoder so partial-UTF-8 state can't leak between the
              // live stream and the snapshot; flush the shared decoder too.
              term.reset();
              term.write(new TextDecoder('utf-8').decode(combined));
              dec.decode(new Uint8Array());
              resetSnap();
            } else if (m.type === 'pong') {
              /* keepalive */
            }
          } catch {
            /* ignore malformed control frame */
          }
        } else {
          const arr = new Uint8Array(ev.data as ArrayBuffer);
          if (snapBuffering) {
            snapBuf.push(arr); // hold until snapshot-end
            return;
          }
          term.write(dec.decode(arr, { stream: true }));
        }
      };
      ws.onclose = () => {
        window.clearInterval(pingTimer);
        dec.decode(new Uint8Array()); // flush partial multibyte so it can't splice onto the reconnect stream
        resetSnap(); // a snapshot mid-flight on a dying socket must not swallow the reconnect stream
        if (disposed) return;
        // The session host keeps claude running across loom restarts / network blips — reconnect.
        retry += 1;
        term.write('\r\n\x1b[2m[disconnected — reconnecting…]\x1b[0m\r\n');
        window.setTimeout(() => {
          if (!disposed) connect();
        }, Math.min(1000 * retry, 4000));
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* already closing */
        }
      };
    };

    // Defer the FIRST fit + connect until BOTH (a) fonts are loaded and (b) the flex container has
    // real layout. xterm measures cell width at open, so a not-yet-loaded font gives a wrong width
    // (chars overlap/stagger); and fitting before layout reports a wrong `cols`. That first `cols`
    // is what creates the session (and launches claude) at the wrong width → claude's input
    // line then wraps at the wrong column (the "text jumps to the next row / garbles" bug). The
    // ResizeObserver below still catches any later size changes.
    const firstFit = () => {
      if (disposed) return;
      const el = holderRef.current;
      if (el && (el.offsetWidth === 0 || el.offsetHeight === 0)) {
        requestAnimationFrame(firstFit); // hidden/zero-size — a fit here mis-sizes the session; wait a frame
        return;
      }
      try {
        fit.fit();
      } catch {
        /* container not measured yet */
      }
      connect();
    };
    Promise.resolve(document.fonts?.ready).then(() => requestAnimationFrame(firstFit));

    // Wheel handling branches on the session's host:
    //  • tmux: tmux owns the screen (xterm only sees its alt buffer), so translate the wheel
    //    into tmux SGR mouse-wheel events ourselves and return false (the xterm<->tmux
    //    mouse-mode handshake is flaky). Accumulate pixels → one notch per SCROLL_STEP_PX so
    //    trackpad momentum floods don't fly through the buffer.
    //  • pty: xterm owns a real scrollback — glide mouse-wheel notches over it (150ms ease);
    //    trackpads return true and use xterm's native scroll (OS momentum is already smooth).
    const SCROLL_STEP_PX = 40; // px of wheel travel per tmux scroll notch — lower = more sensitive
    const MAX_NOTCHES_PER_EVENT = 6; // cap per DOM wheel event so a fast flick still can't flood tmux
    let wheelAccum = 0;
    term.attachCustomWheelEventHandler((e) => {
      if (liveBackend === 'tmux') {
        const px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 800 : e.deltaY;
        if (!px) return false; // ignore horizontal / zero-delta
        if (wheelAccum !== 0 && Math.sign(px) !== Math.sign(wheelAccum)) wheelAccum = 0; // snappy reversals
        wheelAccum += px;
        const notches = Math.trunc(wheelAccum / SCROLL_STEP_PX);
        if (notches !== 0) {
          wheelAccum -= notches * SCROLL_STEP_PX;
          const seq = notches < 0 ? '\x1b[<64;1;1M' : '\x1b[<65;1;1M'; // SGR wheel up / down
          for (let i = 0; i < Math.min(Math.abs(notches), MAX_NOTCHES_PER_EVENT); i++) send({ type: 'input', data: seq });
          scheduleRepaint(); // self-heal any tear once scrolling stops
        }
        return false;
      }
      // pty: scroll xterm's own buffer. Return false so xterm does NOT also apply its
      // default wheel handling (we own the motion); trackpads fall through to native.
      if (isMouseWheelEvent(e.deltaY, e.deltaMode)) {
        const lineHeight =
          term.rows && holderRef.current?.offsetHeight ? holderRef.current.offsetHeight / term.rows : 17;
        const lines = e.deltaMode === 1 ? Math.round(e.deltaY) : Math.round(e.deltaY / lineHeight);
        animateWheelScroll(term, lines, wheelAnim);
        return false;
      }
      return true; // trackpad → native xterm scroll (OS momentum)
    });

    const onData = term.onData((d) => {
      // Any keystroke returns a scrolled-up user to live content (what a native terminal
      // does). Only meaningful on pty, where xterm owns the scrollback position.
      if (liveBackend === 'pty') snapTermToBottom(term, wheelAnim);
      // Debug aid for paste truncation: log large inputs (likely pastes) so it's easy to see
      // the full byte count actually leaving the browser. Filter the console by "loom-term".
      if (d.length > 64) console.debug(`[loom-term] input ${d.length} bytes:`, JSON.stringify(d.slice(0, 60)) + (d.length > 60 ? '…' : ''));
      send({ type: 'input', data: d });
    });
    const onResize = term.onResize(({ cols, rows }) => sendResize(cols, rows));
    // Coalesce refits to the FINAL stable size. Dragging fires resize continuously; re-fitting per
    // tick (→ SIGWINCH → full re-render) tears the TUI. A single rAF still fires before the next
    // observer tick mid-drag (rAF → Style → Layout → ResizeObserver → Paint), so use TWO frames —
    // that guarantees one fit at the settled dims.
    let fitRaf1 = 0;
    let fitRaf2 = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(fitRaf1);
      cancelAnimationFrame(fitRaf2);
      fitRaf1 = requestAnimationFrame(() => {
        fitRaf2 = requestAnimationFrame(() => {
          try {
            fit.fit();
          } catch {
            /* mid-teardown */
          }
          scheduleRepaint();
        });
      });
    });
    if (holderRef.current) ro.observe(holderRef.current);

    // File drop → save server-side + type the path into claude (mirrors a native terminal's drag).
    // Works for ANY file claude can read from a path — images, PDFs, CSVs, etc. Native CAPTURE-phase
    // listeners so xterm's inner elements can't swallow the drop before us. Logged under "loom-term".
    const holderEl = holderRef.current;
    const onDragOverFiles = (e: DragEvent) => {
      if (Array.from(e.dataTransfer?.types || []).includes('Files')) {
        e.preventDefault();
        setDragOver(true);
      }
    };
    const onDragLeaveFiles = () => setDragOver(false);
    const onDropFiles = (e: DragEvent) => {
      const files = Array.from(e.dataTransfer?.files || []);
      console.debug(`[loom-term] drop: ${files.length} file(s)`);
      if (!files.length) return;
      e.preventDefault();
      e.stopPropagation();
      setDragOver(false);
      if (!resume) return;
      for (const f of files) {
        const reader = new FileReader();
        reader.onload = () => {
          const data = (reader.result as string).split(',')[1] || '';
          fetch(`/api/terminals/${resume}/upload`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ data, name: f.name }),
          })
            .then((resp) => console.debug(`[loom-term] file "${f.name}" -> ${resp.ok ? 'ok (path typed into claude)' : 'FAILED ' + resp.status}`))
            .catch((err) => console.debug('[loom-term] upload error', err));
        };
        reader.readAsDataURL(f);
      }
    };
    holderEl?.addEventListener('dragover', onDragOverFiles, true);
    holderEl?.addEventListener('dragleave', onDragLeaveFiles, true);
    holderEl?.addEventListener('drop', onDropFiles, true);

    return () => {
      disposed = true;
      window.clearInterval(pingTimer);
      cancelAnimationFrame(fitRaf1);
      cancelAnimationFrame(fitRaf2);
      window.clearTimeout(repaintTimer);
      resetSnap();
      if (wheelAnim.rafId !== null) cancelAnimationFrame(wheelAnim.rafId);
      holderEl?.removeEventListener('dragover', onDragOverFiles, true);
      holderEl?.removeEventListener('dragleave', onDragLeaveFiles, true);
      holderEl?.removeEventListener('drop', onDropFiles, true);
      ro.disconnect();
      onData.dispose();
      onResize.dispose();
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
      term.dispose();
    };
  }, [resume, cwd, termEpoch]);

  return (
    <div className="fixed inset-0 z-50 bg-canvas/95 backdrop-blur flex">
      <ChatSidebar activeSid={resume} />
      <div className="flex-1 flex flex-col min-w-0 relative">
        <header className="border-b border-edge px-5 h-12 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="text-[10.5px] mono px-2 py-0.5 rounded-full border border-accent-dim text-accent shrink-0">
              ❯ terminal
            </span>
            <span className="mono text-sm text-ink truncate">{title ?? 'claude'}</span>
            <span className="text-[10.5px] mono text-muted hidden sm:block shrink-0">
              real claude TUI · {backend === 'tmux' ? 'classic (tmux)' : backend === 'pty' ? 'smooth scroll (pty)' : '…'}
            </span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => setShowText((v) => !v)}
              title="view the conversation as selectable text — copy any part (the fullscreen TUI can't drag-select across scroll)"
              className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5 shrink-0"
            >
              copy text
            </button>
            <RendererMenu chatId={resume} backend={backend} onSwitched={() => setTermEpoch((e) => e + 1)} />
            <OpenTerminalMenu chatId={resume} cwd={cwd} backend={backend} />
            <OpenInIde cwd={cwd} />
            <button
              onClick={onClose}
              title="detach (the session keeps running)"
              className="text-muted hover:text-ink text-lg leading-none px-2"
            >
              ✕
            </button>
          </div>
        </header>

        {chatMeta && (chatMeta.branch || (chatMeta.prs?.length ?? 0) > 0) && (
          <div className="border-b border-edge bg-surface-2/30 px-5 py-1.5 flex flex-wrap items-center gap-2 text-[11px] mono shrink-0">
            {chatMeta.branch && <span className="px-2 py-0.5 rounded border border-edge text-muted">⎇ {chatMeta.branch}</span>}
            <PrBadges sid={resume} prs={chatMeta.prs ?? []} repo={chatMeta.pr_repo} />
          </div>
        )}

        {task && (
          <DevStackBar
            task={task}
            logsOpen={logsPanel.open}
            onToggleLogs={() => logsPanel.setOpen((v) => !v)}
          />
        )}

        <div
          ref={holderRef}
          className={`flex-1 min-h-0 px-2 py-1.5 overflow-hidden ${dragOver ? 'ring-2 ring-inset ring-accent-dim' : ''}`}
        />

        {task && logsPanel.open && (
          <ServiceLogsPanel
            task={task}
            kind={logsPanel.kind}
            onKindChange={logsPanel.setKind}
            onClose={() => logsPanel.setOpen(false)}
          />
        )}

        {showText && resume && <CopyTextPanel chatId={resume} onClose={() => setShowText(false)} />}
      </div>
    </div>
  );
}
