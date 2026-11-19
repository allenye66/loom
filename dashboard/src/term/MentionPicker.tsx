import { useMemo, useRef, useState, type MutableRefObject } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Chat } from '../api';
import { AgentIcon } from '../components/AgentIcon';

/** Same slug the server uses to match `@mentions` (loom/core/mentions.py). */
export const slugify = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
const titleOf = (c: Chat) => c.name || c.display_title || c.id.slice(0, 8);

const BOUNDARY = new Set(['\n', ' ', '\t']);
const PASTE_ON = '\x1b[200~';
const PASTE_OFF = '\x1b[201~';
const MAX_SHOWN = 30;

type Scored = { c: Chat; score: number; sameCat: boolean; sameRepo: boolean };

/** Chats you can mention from `me`, best first: same category, then same project, then the rest;
 *  within that, text matches (every typed word must appear) and recency. */
function rank(chats: Chat[], q: string, me: Chat | undefined): Scored[] {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const out: Scored[] = [];
  for (const c of chats) {
    if (!c.task || c.archived || c.id === me?.id) continue;
    const title = titleOf(c).toLowerCase();
    const near = `${c.branch ?? ''} ${c.repo ?? ''} ${c.category_name ?? ''}`.toLowerCase();
    let text = 0;
    for (const w of words) {
      if (title.startsWith(w)) text += 5;
      else if (title.includes(w)) text += 4;
      else if (near.includes(w)) text += 2;
      else {
        text = -1;
        break;
      }
    }
    if (text < 0) continue;
    const sameCat = !!me?.category && c.category === me.category;
    const sameRepo = !!me?.repo && c.repo === me.repo;
    out.push({ c, score: (sameCat ? 10000 : 0) + (sameRepo ? 1000 : 0) + text * 10, sameCat, sameRepo });
  }
  return out.sort((a, b) => b.score - a.score || (b.c.last_active ?? 0) - (a.c.last_active ?? 0));
}

/**
 * `#` (or `@@`) → pick a loom chat to mention.
 *
 * `#` at the start of a word opens the picker and is *swallowed* — claude's input box does nothing
 * special with `#` (verified on 2.1.328), so nothing flickers. `@@` still works too, but claude's own
 * `@` is its file picker: the first `@` has already reached the TUI when the second arrives, so we
 * send a Backspace to take it back out (which draws and tears down claude's big picker for a
 * moment — why `#` is the better trigger). While the picker is open it swallows the keystrokes; on
 * Enter/Tab it pastes `@chat-slug ` (bracketed, so claude doesn't open its file picker on it). The
 * visible text stays a plain `@name`; the agent's UserPromptSubmit hook resolves it server-side
 * (core/workspace.py) and attaches which chat that is and how to reach it.
 *
 * `#` is also how people write `#123` and `# Heading`, so a `#` that isn't a mention types normally:
 * `# ` (Space on an empty query), `##`, and `#123`/`#foo` when nothing matches and you press Space,
 * Enter or punctuation — the literal text is sent as if the picker never opened. Mid-word `#`
 * (`C#`, `a#b`) never triggers. Esc cancels (types nothing); Backspace on an empty `#` query too.
 * For `@@`: Backspace on an empty query leaves a single `@`; Space types a literal `@@ `.
 *
 * Mounted once per terminal. `handleRef.current(data)` is called by the terminal's onData handler
 * for every chunk of keyboard input; returning true means "I consumed it, don't send it".
 */
export function MentionPicker({
  handleRef,
  sendRef,
  chatId,
}: {
  handleRef: MutableRefObject<((d: string) => boolean) | null>;
  sendRef: MutableRefObject<((data: string) => void) | null>;
  chatId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const [trigger, setTrigger] = useState<'#' | '@@'>('#'); // which keystroke opened the picker
  // Typing state outside React (read on every keystroke): the last typed char, and whether the
  // previous `@` sat at a word start. null = unknown (cursor moved, pasted, backspaced…) → no trigger.
  const st = useRef<{ last: string | null; pendingAt: boolean }>({ last: '\n', pendingAt: false });

  const { data: chats } = useQuery({
    queryKey: ['chats', { scope: 'all', tab: 'active', q: '' }],
    queryFn: () =>
      fetch('/api/chats?scope=all&tab=active')
        .then((r) => r.json())
        .then((d) => d.chats as Chat[]),
    enabled: open,
    refetchInterval: false,
  });
  const me = useMemo(() => chats?.find((c) => c.id === chatId), [chats, chatId]);
  const results = useMemo(() => rank(chats ?? [], q, me).slice(0, MAX_SHOWN), [chats, q, me]);
  const slugCounts = useMemo(() => {
    const m = new Map<string, number>();
    for (const c of chats ?? []) if (c.task) m.set(slugify(titleOf(c)), (m.get(slugify(titleOf(c))) ?? 0) + 1);
    return m;
  }, [chats]);

  const close = () => {
    setOpen(false);
    setQ('');
    setSel(0);
  };
  const choose = (c: Chat) => {
    const slug = slugify(titleOf(c)) || c.id.slice(0, 8);
    const token = (slugCounts.get(slug) ?? 0) > 1 ? `${slug}~${c.id.slice(0, 4)}` : slug;
    sendRef.current?.(`${PASTE_ON}@${token} ${PASTE_OFF}`);
    st.current = { last: ' ', pendingAt: false };
    close();
  };

  const isPrintable = (d: string) => d.length >= 1 && [...d].every((ch) => ch >= ' ' && ch !== '\x7f');

  // Literal text for a `#` that turned out not to be a mention (an issue ref, a heading…).
  const literal = (text: string) => {
    sendRef.current?.('#' + text);
    st.current = { last: text.length ? text[text.length - 1] : '#', pendingAt: false };
    close();
  };
  const PUNCT = /^[.,;:!?)\]}'"]$/;

  handleRef.current = (d: string): boolean => {
    // Terminal-generated reports (focus in/out) aren't typing: leave state alone, let them through.
    if (d === '\x1b[I' || d === '\x1b[O') return false;

    if (open) {
      const hash = trigger === '#';
      if (d === '\r' || d === '\t') {
        if (results[sel]) choose(results[sel].c);
        else if (hash && d === '\r') literal(q); // nothing matches: keep what was typed (don't submit)
        return true;
      }
      if (d === '\x1b' || d === '\x03') {
        // Cancelled. `#` was never forwarded and `@@`'s first @ is already out, so the cursor sits at
        // the word start we triggered at — a boundary again (an "unknown" state would make the next
        // trigger silently not fire).
        st.current = { last: ' ', pendingAt: false };
        close();
        return true;
      }
      if (d === '\x1b[A' || d === '\x1bOA' || d === '\x10') {
        if (results.length) setSel((s) => (s - 1 + results.length) % results.length);
        return true;
      }
      if (d === '\x1b[B' || d === '\x1bOB' || d === '\x0e') {
        if (results.length) setSel((s) => (s + 1) % results.length);
        return true;
      }
      if (d === '\x7f') {
        if (q === '') {
          if (!hash) sendRef.current?.('@'); // `@@`: undo one of the two @s
          st.current = { last: hash ? ' ' : '@', pendingAt: false };
          close();
        } else {
          setQ(q.slice(0, -1));
          setSel(0);
        }
        return true;
      }
      if (d === ' ' && q === '') {
        sendRef.current?.(hash ? '# ' : '@@ '); // a literal "# " (heading) / "@@ "
        st.current = { last: ' ', pendingAt: false };
        close();
        return true;
      }
      if (hash && d === '#' && q === '') {
        literal('#'); // `##` (markdown heading) — not a mention
        return true;
      }
      if (hash && results.length === 0 && q !== '' && (d === ' ' || PUNCT.test(d))) {
        literal(q + d); // `#123 ` / `#foo,` — nothing matched, so it was a literal
        return true;
      }
      // typed text / paste → into the query (pasted bracketed text arrives wrapped in the markers)
      const text = d.replace(PASTE_ON, '').replace(PASTE_OFF, '').replace(/[\r\n\t]+/g, ' ');
      if (isPrintable(text)) {
        setQ((prev) => prev + text);
        setSel(0);
      }
      return true; // swallow everything else (other escape sequences) while the picker is up
    }

    // --- closed: watch for `#` / `@@` at the start of a word ---
    const s = st.current;
    if (d === '#') {
      if (s.last !== null && BOUNDARY.has(s.last)) {
        s.pendingAt = false;
        s.last = null;
        setTrigger('#');
        setQ('');
        setSel(0);
        setOpen(true);
        return true; // swallowed: claude ignores `#`, so there's nothing to take back
      }
      s.pendingAt = false;
      s.last = '#';
      return false; // mid-word (C#, a#b): type it normally
    }
    if (d === '@') {
      if (s.pendingAt) {
        s.pendingAt = false;
        s.last = null;
        sendRef.current?.('\x7f'); // take the first @ back out of claude's input box
        setTrigger('@@');
        setQ('');
        setSel(0);
        setOpen(true);
        return true;
      }
      s.pendingAt = s.last !== null && BOUNDARY.has(s.last);
      s.last = '@';
      return false;
    }
    s.pendingAt = false;
    if (d === '\r') s.last = '\n';
    else if (d === '\x03' || d === '\x15') s.last = '\n';
    else if (d.length === 1 && d >= ' ' && d !== '\x7f') s.last = d;
    else s.last = null; // backspace, escape sequences, pastes: we no longer know what precedes the cursor
    return false;
  };

  if (!open) return null;

  return (
    <div
      className="absolute left-4 right-4 bottom-4 z-20 max-w-2xl rounded-xl border border-edge bg-surface shadow-2xl overflow-hidden flex flex-col"
      role="dialog"
      aria-label="Mention a loom chat"
      onMouseDown={(e) => e.preventDefault() /* keep the terminal focused */}
    >
      <div className="px-3 py-2 border-b border-edge flex items-center gap-2 mono text-xs">
        <span className="text-accent shrink-0">{trigger}</span>
        <span className="text-ink truncate">{q}</span>
        <span className="inline-block w-px h-3.5 bg-accent animate-pulse shrink-0" />
        <span className="flex-1" />
        <span className="text-[10px] text-muted shrink-0 hidden sm:inline">
          ↑↓ pick · ↵ insert · esc cancel · {trigger === '#' ? 'space = literal #' : 'space = literal @@'}
        </span>
      </div>
      <div className="max-h-64 overflow-auto thin-scroll py-1" role="listbox">
        {!chats && <div className="px-3 py-3 mono text-xs text-muted">loading chats…</div>}
        {chats && results.length === 0 && (
          <div className="px-3 py-3 mono text-xs text-muted">
            no chat matches “{q}”
            {trigger === '#' && q && <span className="text-muted/70"> — space / enter / punctuation types “#{q}” as is</span>}
          </div>
        )}
        {results.map((r, i) => (
          <div
            key={r.c.id}
            role="option"
            aria-selected={i === sel}
            ref={(el) => i === sel && el?.scrollIntoView({ block: 'nearest' })}
            onMouseMove={() => sel !== i && setSel(i)}
            onClick={() => choose(r.c)}
            className={`px-3 py-1.5 flex items-center gap-2 cursor-pointer border-l-2 ${
              r.sameCat ? 'border-accent' : 'border-transparent'
            } ${i === sel ? 'bg-surface-2' : ''}`}
          >
            <span className="text-xs text-ink truncate flex-1 min-w-0">{titleOf(r.c)}</span>
            {r.c.category_name && (
              <span
                className={`text-[10px] mono shrink-0 max-w-[9rem] truncate ${r.sameCat ? 'text-accent' : 'text-muted/70'}`}
                title={r.sameCat ? 'same category as this chat' : 'category'}
              >
                {r.c.category_name}
              </span>
            )}
            {!r.sameRepo && r.c.repo && <span className="text-[10px] mono text-muted/70 shrink-0 max-w-[7rem] truncate">{r.c.repo}</span>}
            {r.c.agent && (
              <span className="text-muted shrink-0 opacity-80 inline-flex items-center">
                <AgentIcon agent={r.c.agent} />
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
