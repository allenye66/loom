import { useSyncExternalStore } from 'react';

/** Minimal toasts: a one-line note, bottom-right, that says what an action is attempting and then
 *  what came of it. Reusing an `id` replaces that toast in place ("starting…" → "launched"), so a
 *  single action never stacks two. Click one to dismiss it. */
export type ToastKind = 'pending' | 'ok' | 'err' | 'info';
type Toast = { id: string; text: string; kind: ToastKind; seq: number };

const TTL: Record<ToastKind, number> = { ok: 2600, info: 3200, err: 6500, pending: 30000 }; // pending: safety net only
const MAX = 4;

let toasts: Toast[] = [];
let seq = 0;
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => f());

export function dismissToast(id: string) {
  clearTimeout(timers.get(id));
  timers.delete(id);
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

/** Show (or update) a toast. Returns its id. */
export function toast(text: string, opts: { id?: string; kind?: ToastKind; ttl?: number } = {}): string {
  const id = opts.id ?? `t${++seq}`;
  const kind = opts.kind ?? 'info';
  const next: Toast = { id, text, kind, seq: ++seq };
  const at = toasts.findIndex((t) => t.id === id);
  if (at >= 0) toasts = toasts.map((t, i) => (i === at ? next : t));
  else toasts = [...toasts, next].slice(-MAX);
  clearTimeout(timers.get(id));
  timers.set(id, setTimeout(() => dismissToast(id), opts.ttl ?? TTL[kind]));
  emit();
  return id;
}

/** Run an async action with a toast that follows it: `pending` while it runs, then `ok` / `err`.
 *  Uses promises (not the mutation's per-call callbacks, which don't fire after the component that
 *  started the action unmounts — e.g. you switch chats mid-start). */
export async function toastAction<T>(
  id: string,
  pending: string,
  work: () => Promise<T>,
  done: (r: T) => string,
  failed: (msg: string) => string,
): Promise<T | undefined> {
  toast(pending, { id, kind: 'pending' });
  try {
    const r = await work();
    toast(done(r), { id, kind: 'ok' });
    return r;
  } catch (e) {
    toast(failed(e instanceof Error ? e.message : String(e)), { id, kind: 'err' });
    return undefined;
  }
}

const subscribe = (f: () => void) => {
  subs.add(f);
  return () => void subs.delete(f);
};
const snapshot = () => toasts;

const DOT: Record<ToastKind, string> = {
  pending: 'bg-accent animate-pulse',
  ok: 'bg-ok',
  err: 'bg-bad',
  info: 'bg-muted',
};

/** Mounted once in the shell. */
export function Toaster() {
  const list = useSyncExternalStore(subscribe, snapshot);
  if (!list.length) return null;
  return (
    <div className="fixed bottom-4 right-4 z-[70] flex flex-col items-end gap-1.5 pointer-events-none" role="status" aria-live="polite">
      {list.map((t) => (
        <button
          key={t.id}
          onClick={() => dismissToast(t.id)}
          title="dismiss"
          className={`pointer-events-auto max-w-[360px] mono text-[11px] text-left rounded border bg-surface-2/95 backdrop-blur px-2.5 py-1.5 shadow-lg shadow-black/40 flex items-start gap-2 ${
            t.kind === 'err' ? 'border-bad/50 text-ink' : 'border-edge text-ink'
          }`}
        >
          <span className={`mt-[5px] w-1.5 h-1.5 rounded-full shrink-0 ${DOT[t.kind]}`} />
          <span className="break-words min-w-0">{t.text}</span>
        </button>
      ))}
    </div>
  );
}
