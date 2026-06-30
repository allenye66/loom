import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { ChatSidebar, DevStackBar, OpenInIde } from '../chat/ChatSidebar';
import { PrBadges } from '../components/PrBadges';
import type { Task } from '../api';

// Match the app palette (index.css @theme tokens) so the TUI feels native.
const THEME = {
  background: '#0a0c11',
  foreground: '#e7eaf3',
  cursor: '#8b7cf6',
  cursorAccent: '#0a0c11',
  selectionBackground: '#5a4fcf66',
};

/** Open this terminal chat's SAME live tmux session in a native Terminal.app (tmux attach).
 *  No handoff needed — tmux supports the browser and the real terminal attached at once. */
/** Dropdown to open a native Terminal for this worktree — either ATTACH to the live claude
 *  session (tmux attach; both stay in sync) or open a PLAIN SHELL in the worktree (no claude). */
function OpenTerminalMenu({ chatId, cwd }: { chatId?: string; cwd?: string }) {
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
          <button onClick={() => run('claude')} className="w-full text-left px-3 py-2 hover:bg-surface-2 flex flex-col gap-0.5">
            <span className="text-[11px] mono text-ink">❯ attach claude</span>
            <span className="text-[10px] text-muted">the live session — tmux attach, stays in sync</span>
          </button>
          <button
            onClick={() => run('shell')}
            disabled={!cwd}
            className="w-full text-left px-3 py-2 hover:bg-surface-2 border-t border-edge flex flex-col gap-0.5 disabled:opacity-40"
          >
            <span className="text-[11px] mono text-ink">$ plain shell</span>
            <span className="text-[10px] text-muted">no claude — a shell in this worktree</span>
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
 * Terminal mode: the *real* interactive `claude` TUI, bridged from a tmux-hosted PTY
 * (server-side) to xterm.js. No reimplementation — every slash command / permission
 * prompt is claude's own. Wraps the conversation pane in loom's shell (the `ChatSidebar`
 * + dev-stack bar). The tmux session outlives this tab and loom restarts, so a dropped
 * socket just reconnects to the running session.
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

  // Dev-stack parity: map this worktree (cwd) to its loom task for the FE/BE + start/stop strip.
  const { data: tasks } = useQuery({
    queryKey: ['tasks'],
    queryFn: () => fetch('/api/tasks').then((r) => r.json()).then((d) => d.tasks as Task[]),
    refetchInterval: 4000,
  });
  const task = tasks?.find(
    (t) => t.worktree_path && (cwd === t.worktree_path || (cwd?.startsWith(t.worktree_path + '/') ?? false)),
  );

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
    const send = (o: unknown) => {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(o));
    };
    // After a scroll/resize settles, ask the server for a clean tmux redraw — claude's Ink TUI
    // tears xterm's display under the rapid re-renders those gestures trigger; this self-heals it
    // without a manual browser refresh.
    let repaintTimer: number | undefined;
    const scheduleRepaint = () => {
      window.clearTimeout(repaintTimer);
      repaintTimer = window.setTimeout(() => send({ type: 'repaint' }), 200);
    };

    const connect = () => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/api/ws/term`);
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => {
        retry = 0;
        // First frame carries the chat id + cwd + initial size; the server (re)attaches
        // the tmux session and replays its recent output.
        send({ chat_id: resume, cwd, cols: term.cols, rows: term.rows });
        pingTimer = window.setInterval(() => send({ type: 'ping' }), 25000);
      };
      ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
          try {
            const m = JSON.parse(ev.data);
            if (m.type === 'exit')
              term.write('\r\n\x1b[2m[claude exited — close and reopen to start a new session]\x1b[0m\r\n');
            else if (m.type === 'error') term.write(`\r\n\x1b[31m[loom] ${m.message}\x1b[0m\r\n`);
          } catch {
            /* ignore malformed control frame */
          }
        } else {
          term.write(new Uint8Array(ev.data as ArrayBuffer));
        }
      };
      ws.onclose = () => {
        window.clearInterval(pingTimer);
        if (disposed) return;
        // tmux keeps the session running across loom restarts / network blips — reconnect.
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

    // Defer the FIRST fit + connect until the browser has laid out the flex container. Fitting
    // before layout reports a wrong `cols`, and that first value is what creates the tmux session
    // (and launches claude) at the wrong width → claude's input line then wraps at the wrong
    // column (the "text jumps to the next row / garbles" bug). A rAF runs after layout; the
    // ResizeObserver below still catches any later size changes.
    requestAnimationFrame(() => {
      if (disposed) return;
      try {
        fit.fit();
      } catch {
        /* container not measured yet */
      }
      connect();
    });

    // Drive scrollback deterministically: translate the browser wheel into tmux mouse-wheel
    // events ourselves and tell xterm to ignore it (return false). The xterm<->tmux mouse-mode
    // handshake is flaky — scrolling would work, then stop after claude redrew — so we don't
    // rely on it. tmux (mouse on) interprets these and scrolls its scrollback / copy-mode.
    // Calmer scrolling: ACCUMULATE wheel pixels and emit one tmux wheel notch per
    // SCROLL_STEP_PX, rather than forcing >=1 notch per raw wheel event. Trackpad momentum
    // fires a flood of tiny events, so the old per-event minimum flew through the buffer;
    // accumulating decouples scroll speed from event count. Larger SCROLL_STEP_PX = less sensitive.
    const SCROLL_STEP_PX = 80;
    let wheelAccum = 0;
    term.attachCustomWheelEventHandler((e) => {
      const px = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 800 : e.deltaY;
      if (!px) return false; // ignore horizontal / zero-delta
      if (wheelAccum !== 0 && Math.sign(px) !== Math.sign(wheelAccum)) wheelAccum = 0; // snappy reversals
      wheelAccum += px;
      const notches = Math.trunc(wheelAccum / SCROLL_STEP_PX);
      if (notches !== 0) {
        wheelAccum -= notches * SCROLL_STEP_PX;
        const seq = notches < 0 ? '\x1b[<64;1;1M' : '\x1b[<65;1;1M'; // SGR wheel up / down
        for (let i = 0; i < Math.min(Math.abs(notches), 3); i++) send({ type: 'input', data: seq });
        scheduleRepaint(); // self-heal any tear once scrolling stops
      }
      return false;
    });

    const onData = term.onData((d) => {
      // Debug aid for paste truncation: log large inputs (likely pastes) so it's easy to see
      // the full byte count actually leaving the browser. Filter the console by "loom-term".
      if (d.length > 64) console.debug(`[loom-term] input ${d.length} bytes:`, JSON.stringify(d.slice(0, 60)) + (d.length > 60 ? '…' : ''));
      send({ type: 'input', data: d });
    });
    const onResize = term.onResize(({ cols, rows }) => send({ type: 'resize', cols, rows }));
    // Debounce refit: dragging the window fires resize continuously, and re-fitting on every
    // frame (→ resize → SIGWINCH → claude full re-render) makes claude's TUI tear/overlap — its
    // renderer can't keep up with rapid re-renders (upstream Ink bug). Refit once after the drag
    // settles so claude re-renders a single clean frame.
    let fitTimer: number | undefined;
    const ro = new ResizeObserver(() => {
      window.clearTimeout(fitTimer);
      fitTimer = window.setTimeout(() => {
        try {
          fit.fit();
        } catch {
          /* mid-teardown */
        }
        scheduleRepaint();
      }, 150);
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
      window.clearTimeout(fitTimer);
      window.clearTimeout(repaintTimer);
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
  }, [resume, cwd]);

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
            <span className="text-[10.5px] mono text-muted hidden sm:block shrink-0">real claude TUI · tmux-backed</span>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => setShowText((v) => !v)}
              title="view the conversation as selectable text — copy any part (the fullscreen TUI can't drag-select across scroll)"
              className="text-[11px] mono text-muted hover:text-ink border border-edge rounded px-2 py-0.5 shrink-0"
            >
              copy text
            </button>
            <OpenTerminalMenu chatId={resume} cwd={cwd} />
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

        {task && <DevStackBar task={task} />}

        <div
          ref={holderRef}
          className={`flex-1 min-h-0 px-2 py-1.5 overflow-hidden ${dragOver ? 'ring-2 ring-inset ring-accent-dim' : ''}`}
        />

        {showText && resume && <CopyTextPanel chatId={resume} onClose={() => setShowText(false)} />}
      </div>
    </div>
  );
}
