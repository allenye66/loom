import type { IBuffer, IBufferCell, IDecoration, IMarker, Terminal } from '@xterm/xterm';
import { scrollToLineExact } from './readingPosition';

// "Jump to my message": step the terminal between the prompts YOU sent, so after a long burst of
// agent output you land on your message and read the reply from its first line.
//
// Works on xterm's own scrollback, i.e. the pty ("smooth") host running claude's inline renderer.
// claude prints each sent prompt as a `❯` in column 0 on a grey-background row (a long prompt's
// continuation rows keep the background). The live input box draws the same glyph on the DEFAULT
// background, and a queued prompt claude hasn't picked up yet sits above the spinner with a
// "ctrl+x ctrl+s to send now" hint, so neither counts. Once claude takes a queued prompt (at the
// next tool boundary or turn end) it is reprinted at that point in the transcript, which is the
// spot you want to land on. Verified against claude 2.1.282's inline output (2026-09).

/** One of your messages in the scrollback: its first buffer row and how many rows it spans. */
export interface SentMessage {
  row: number;
  rows: number;
}

export type JumpResult =
  | { kind: 'message'; index: number; total: number } // landed on message `index` of `total` (1-based)
  | { kind: 'live'; moved: boolean } // stepped past the newest message → back at live output
  | { kind: 'oldest' } // nothing older above the current position — stayed put
  | { kind: 'none' }; // none of your messages in the scrollback (yet)

const PROMPT_GLYPHS = new Set(['❯', '>']); // claude 2.x draws ❯; older builds drew >
const PENDING_HINT = /send now/i;
const MAX_MESSAGE_ROWS = 400; // only bounds the highlight on a huge pasted prompt
const FLASH_MS = 1600; // keep in sync with the .loom-msg-flash animation in index.css

function shaded(buf: IBuffer, y: number, cell: IBufferCell): boolean {
  const line = buf.getLine(y);
  return !!line && !!line.getCell(0, cell) && !cell.isBgDefault();
}

function startsMessage(buf: IBuffer, y: number, cell: IBufferCell): boolean {
  const line = buf.getLine(y);
  return (
    !!line && !line.isWrapped && !!line.getCell(0, cell) && !cell.isBgDefault() && PROMPT_GLYPHS.has(cell.getChars())
  );
}

/** Every message you sent that is still in the scrollback, oldest first. Empty on the alt screen. */
export function findSentMessages(term: Terminal): SentMessage[] {
  const buf = term.buffer.active;
  if (buf.type !== 'normal') return [];
  const cell = buf.getNullCell();
  const found: (SentMessage & { hint: boolean; chained: boolean })[] = [];
  for (let y = 0; y < buf.length; y++) {
    if (!startsMessage(buf, y, cell)) continue;
    let end = y + 1;
    while (end < buf.length && end - y < MAX_MESSAGE_ROWS && shaded(buf, end, cell) && !startsMessage(buf, end, cell))
      end++;
    found.push({
      row: y,
      rows: end - y,
      hint: PENDING_HINT.test(buf.getLine(end)?.translateToString(true) ?? ''),
      chained: startsMessage(buf, end, cell),
    });
    y = end - 1;
  }
  // A still-queued prompt carries the hint; one queued right above it (back to back) is queued too.
  const out: SentMessage[] = [];
  let pending = false;
  for (let i = found.length - 1; i >= 0; i--) {
    pending = found[i].hint || (found[i].chained && pending);
    if (!pending) out.push({ row: found[i].row, rows: found[i].rows });
  }
  return out.reverse();
}

export interface MessageNav {
  /** Older (-1) or newer (+1) message. Newer than the newest returns to live output. */
  step(dir: -1 | 1): JumpResult;
  /** Forget the jump position (call when the buffer is wholesale replaced, e.g. a snapshot). */
  reset(): void;
  dispose(): void;
}

/** `beforeScroll` runs before any programmatic scroll (cancel an in-flight wheel animation). */
export function createMessageNav(term: Terminal, beforeScroll: () => void): MessageNav {
  // Where the last jump landed: the message's row (a marker follows it as old scrollback is
  // trimmed) and the viewport position it left. If the viewport hasn't moved since, the next
  // step continues from that message; otherwise it starts over from what's on screen.
  let anchor: { marker: IMarker; viewportY: number } | null = null;
  let flash: { marker: IMarker; deco: IDecoration | undefined; timer: number } | null = null;

  const clearAnchor = () => {
    anchor?.marker.dispose();
    anchor = null;
  };
  const clearFlash = () => {
    if (!flash) return;
    window.clearTimeout(flash.timer);
    flash.deco?.dispose();
    flash.marker.dispose();
    flash = null;
  };
  const reset = () => {
    clearAnchor();
    clearFlash();
  };
  const markRow = (row: number): IMarker | undefined => {
    const buf = term.buffer.active;
    return term.registerMarker(row - (buf.baseY + buf.cursorY));
  };

  const reference = (): number => {
    const buf = term.buffer.active;
    if (anchor && !anchor.marker.isDisposed && anchor.viewportY === buf.viewportY) return anchor.marker.line;
    if (buf.viewportY >= buf.baseY) return Infinity; // following live output → newest message first
    return buf.viewportY + 1; // scrolled by hand → the row a jump would put a message on
  };

  const highlight = (m: SentMessage) => {
    clearFlash();
    const marker = markRow(m.row);
    if (!marker) return;
    let deco: IDecoration | undefined;
    try {
      deco = term.registerDecoration({ marker, width: term.cols, height: m.rows });
      deco?.onRender((el) => el.classList.add('loom-msg-flash'));
    } catch {
      /* decorations unavailable — the jump itself still happened */
    }
    flash = { marker, deco, timer: window.setTimeout(clearFlash, FLASH_MS) };
  };

  return {
    step(dir) {
      const msgs = findSentMessages(term);
      if (!msgs.length) return { kind: 'none' };
      const ref = reference();
      let i: number;
      if (dir < 0) {
        i = msgs.length - 1;
        while (i >= 0 && msgs[i].row >= ref) i--;
        if (i < 0) return { kind: 'oldest' };
      } else {
        if (ref === Infinity) return { kind: 'live', moved: false };
        i = msgs.findIndex((m) => m.row > ref);
        if (i < 0) {
          clearAnchor();
          beforeScroll();
          scrollToLineExact(term, term.buffer.active.baseY);
          return { kind: 'live', moved: true };
        }
      }
      const m = msgs[i];
      const top = Math.max(0, Math.min(m.row - 1, term.buffer.active.baseY)); // one row of context above
      beforeScroll();
      scrollToLineExact(term, top);
      clearAnchor();
      const marker = markRow(m.row);
      if (marker) anchor = { marker, viewportY: top };
      highlight(m);
      return { kind: 'message', index: i + 1, total: msgs.length };
    },
    reset,
    dispose: reset,
  };
}
