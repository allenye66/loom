import type { IBuffer, Terminal } from '@xterm/xterm';

// Keep a scrolled-up reader's place across a resize reflow or a buffer rebuild. The pty host's
// snapshot (after a resize or a reconnect) resets xterm and rewrites the whole stream, which would
// otherwise drop you at live output, and a resize re-wraps long rows (claude's full-width prompt
// rows and rules), shifting the view. Distance-from-bottom alone drifts whenever the buffer
// changes shape (new output that arrived while disconnected, re-wrapped rows), so anchor on the
// TEXT at the top of the view, compared as logical lines (a row plus its soft-wrapped continuation
// rows) so a re-wrap doesn't break the match. Fall back to the distance only when it's not found.

export interface ReadingPosition {
  lines: { off: number; text: string }[]; // first few non-blank logical lines, by row offset in the view
  fromTop: number; // old viewportY — breaks ties between repeated matches
  fromBottom: number; // rows above live output — the fallback
}

const ANCHOR_LINES = 3;
const MAX_ANCHOR_GAP = 200; // rows to look past a candidate for the rest of the anchor lines

/** term.scrollToLine, but landing exactly. xterm 6 scrolls by a delta applied to its DOM scroll
 *  position, which a resize reflow leaves in the old row coordinates (its queued viewport sync
 *  skips the reposition), so the first call can land off by the reflow shift. That call resyncs
 *  the DOM, so a second one lands. */
export function scrollToLineExact(term: Terminal, line: number): void {
  term.scrollToLine(line);
  if (term.buffer.active.viewportY !== line) term.scrollToLine(line);
}

/** The logical line starting at row y (the row plus any soft-wrapped continuation rows). */
function logicalLine(b: IBuffer, y: number): string {
  let s = b.getLine(y)?.translateToString(false) ?? '';
  for (let n = y + 1; b.getLine(n)?.isWrapped; n++) s += b.getLine(n)!.translateToString(false);
  return s.trimEnd();
}

/** Null when following live output (nothing to keep) or on the alt screen. */
export function captureReadingPosition(term: Terminal): ReadingPosition | null {
  const b = term.buffer.active;
  if (b.type !== 'normal' || b.viewportY >= b.baseY) return null;
  const lines: ReadingPosition['lines'] = [];
  for (let off = 0; off < term.rows && lines.length < ANCHOR_LINES; off++) {
    const y = b.viewportY + off;
    if (b.getLine(y)?.isWrapped) continue;
    const text = logicalLine(b, y);
    if (text.trim()) lines.push({ off, text });
  }
  return { lines, fromTop: b.viewportY, fromBottom: b.baseY - b.viewportY };
}

/** After row y's logical line, do the next non-blank logical lines read `rest`, in order? */
function followedBy(b: IBuffer, y: number, rest: string[]): boolean {
  let i = 0;
  for (let n = y + 1; i < rest.length && n < b.length && n - y < MAX_ANCHOR_GAP; n++) {
    if (b.getLine(n)?.isWrapped) continue;
    const text = logicalLine(b, n);
    if (!text.trim()) continue;
    if (text !== rest[i]) return false;
    i++;
  }
  return i === rest.length;
}

/** Scroll the (reflowed or rebuilt) buffer back to `pos`: where its lines' text matches, nearest
 *  the old spot; else the same distance above live output. */
export function restoreReadingPosition(term: Terminal, pos: ReadingPosition): void {
  const b = term.buffer.active;
  const [first, ...rest] = pos.lines;
  let best = -1;
  if (first) {
    const restText = rest.map((r) => r.text);
    for (let y = 0; y < b.length; y++) {
      const line = b.getLine(y);
      if (!line || line.isWrapped) continue;
      // cheap prefix gate before assembling the whole logical line
      if (!first.text.startsWith(line.translateToString(true).slice(0, 16))) continue;
      if (logicalLine(b, y) !== first.text || !followedBy(b, y, restText)) continue;
      const top = y - first.off;
      if (best < 0 || Math.abs(top - pos.fromTop) < Math.abs(best - pos.fromTop)) best = top;
    }
  }
  const top = best >= 0 ? best : b.baseY - pos.fromBottom;
  scrollToLineExact(term, Math.max(0, Math.min(top, b.baseY)));
}
