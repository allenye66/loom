import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useHiddenRepoNames } from '../api';
import { useOpenChat } from '../chat/openChat';
import { BellIcon, BellOffIcon } from '../ui/icons';
import { toast } from '../ui/toast';
import { useNotifyEnabled } from './enabled';

/** One notification an agent pushed with `loom notify` — a question it needs the user to answer. */
export type InboxItem = {
  id: number;
  chat_id: string;
  text: string;
  created: number;
  status: 'open' | 'answered' | 'dismissed';
  chat_name: string;
  repo?: string | null;
  category?: string | null;
  agent?: 'claude' | 'codex' | 'grok' | null;
  muted: boolean;
  archived: boolean;
  cwd?: string | null;
  mode?: 'chat' | 'terminal' | null;
};

export const inboxKey = ['inbox'] as const;

/** The open notifications worth showing: not from an archived chat or a hidden project. */
export function useInbox() {
  const hidden = useHiddenRepoNames();
  const q = useQuery({
    queryKey: inboxKey,
    queryFn: () => fetch('/api/inbox').then((r) => r.json()).then((d) => d.items as InboxItem[]),
    refetchInterval: 6000,
  });
  const items = (q.data ?? []).filter((i) => !i.archived && !(i.repo && hidden.has(i.repo)));
  return { items, unmuted: items.filter((i) => !i.muted) };
}

// --- panel open/closed (module store, so the bell, a notification click and Esc all agree) ---
let panelOpen = false;
const subs = new Set<() => void>();
export const setPanelOpen = (v: boolean) => {
  panelOpen = v;
  subs.forEach((f) => f());
};
const usePanelOpen = () =>
  useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => void subs.delete(f);
    },
    () => panelOpen,
  );

const ago = (t: number) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)}m ago` : s < 86400 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`;
};

/** Sidebar header bell: opens the notification panel; the badge counts what's waiting on you. */
export function NotifyBell() {
  const { unmuted, items } = useInbox();
  const open = usePanelOpen();
  const n = unmuted.length;
  return (
    <button
      data-notify-bell
      onClick={() => setPanelOpen(!open)}
      aria-pressed={open}
      title={n ? `${n} agent${n > 1 ? 's' : ''} waiting on your answer` : items.length ? 'notifications (muted chats only)' : 'notifications — nothing waiting on you'}
      className={`relative leading-none ${n ? 'text-accent' : 'text-muted opacity-70 hover:opacity-100 hover:text-ink'}`}
    >
      <BellIcon size={14} />
      {n > 0 && (
        <span className="absolute -top-1.5 -right-2 min-w-[14px] h-[14px] px-[3px] rounded-full bg-accent text-canvas text-[9px] font-semibold mono flex items-center justify-center">
          {n}
        </span>
      )}
    </button>
  );
}

/** The notification panel: every question an agent has pushed and not yet had answered. Reply right here
 *  (the agent is told which question it answers) or jump to the chat. Mounted once in the shell. */
export function NotifyPanel() {
  const open = usePanelOpen();
  const { items } = useInbox();
  const [enabled, toggle, perm] = useNotifyEnabled();
  const qc = useQueryClient();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setPanelOpen(false);
    const onDown = (e: MouseEvent) => {
      const t = e.target as Element;
      if (ref.current && !ref.current.contains(t) && !t.closest('[data-notify-bell]')) setPanelOpen(false);
    };
    window.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [open]);
  if (!open) return null;
  const dismissAll = async () => {
    await fetch('/api/inbox/dismiss-all', { method: 'POST' });
    qc.invalidateQueries({ queryKey: inboxKey });
  };
  return (
    <div
      ref={ref}
      role="dialog"
      aria-label="Notifications"
      className="fixed left-3 top-11 z-[60] w-[400px] max-w-[calc(100vw-24px)] max-h-[75vh] flex flex-col rounded-lg border border-edge bg-surface shadow-2xl shadow-black/60"
    >
      <div className="flex items-center gap-2 px-3 py-2 border-b border-edge shrink-0">
        <span className="text-[12px] font-semibold">Needs your answer</span>
        <span className="text-[10px] mono text-muted">{items.length ? `${items.length} open` : ''}</span>
        <div className="flex-1" />
        {items.length > 1 && (
          <button onClick={dismissAll} className="text-[10px] mono text-muted hover:text-ink">
            dismiss all
          </button>
        )}
        <button onClick={() => setPanelOpen(false)} aria-label="close" className="text-muted hover:text-ink text-sm leading-none">
          ✕
        </button>
      </div>
      <div className="overflow-auto thin-scroll flex-1 min-h-[3rem]">
        {items.length === 0 ? (
          <div className="px-4 py-8 text-center text-[11px] text-muted leading-relaxed">
            Nothing is waiting on you.
            <br />
            When an agent needs your decision it pushes a question here.
          </div>
        ) : (
          items.map((it) => <Card key={it.id} it={it} />)
        )}
      </div>
      <div className="flex items-center gap-2 px-3 py-1.5 border-t border-edge shrink-0 text-[10px] mono text-muted">
        <button onClick={toggle} className="inline-flex items-center gap-1.5 hover:text-ink" title="pop a desktop notification when an agent pushes one and this tab is in the background">
          {enabled ? <BellIcon size={11} /> : <BellOffIcon size={11} />}
          desktop notifications {perm === 'denied' ? 'blocked by the browser' : enabled ? 'on' : 'off'}
        </button>
      </div>
    </div>
  );
}

function Card({ it }: { it: InboxItem }) {
  const openChat = useOpenChat();
  const qc = useQueryClient();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [force, setForce] = useState(false);
  const refresh = () => qc.invalidateQueries({ queryKey: inboxKey });
  const reply = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setErr(null);
    const r = await fetch(`/api/inbox/${it.id}/reply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text, force }),
    }).catch(() => null);
    setBusy(false);
    if (r?.ok) {
      toast(`replied to ${it.chat_name}`, { kind: 'ok' });
      setText('');
      return refresh();
    }
    const detail = (await r?.json().catch(() => null))?.detail ?? 'could not send';
    setErr(String(detail));
    if (r?.status === 409 && /unsent|typing/.test(String(detail))) setForce(true); // offer "send anyway"
  };
  const go = () => {
    setPanelOpen(false);
    openChat({ resume: it.chat_id, cwd: it.cwd ?? undefined, title: it.chat_name, mode: 'terminal', agent: it.agent ?? undefined });
  };
  const dismiss = async () => {
    await fetch(`/api/inbox/${it.id}/dismiss`, { method: 'POST' });
    refresh();
  };
  return (
    <div className={`px-3 py-2.5 border-b border-edge last:border-b-0 ${it.muted ? 'opacity-60' : ''}`}>
      <div className="flex items-center gap-1.5 text-[11px] mono mb-1">
        <button onClick={go} className="font-semibold text-ink hover:text-accent truncate max-w-[200px]" title="open this chat">
          {it.chat_name}
        </button>
        <span className="text-muted truncate">{[it.repo, it.category].filter(Boolean).join(' · ')}</span>
        <span className="ml-auto text-muted shrink-0">{ago(it.created)}</span>
        <button onClick={dismiss} title="dismiss without answering" aria-label="dismiss" className="text-muted hover:text-ink shrink-0">
          ✕
        </button>
      </div>
      <div className="text-[12px] leading-snug whitespace-pre-wrap break-words text-ink">{it.text}</div>
      <div className="mt-2 flex items-end gap-1.5">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              reply();
            }
          }}
          rows={1}
          placeholder="reply — Enter sends"
          className="flex-1 min-w-0 resize-none mono text-[11px] px-2 py-1 rounded bg-canvas border border-edge outline-none focus:border-accent max-h-28"
        />
        <button
          onClick={reply}
          disabled={!text.trim() || busy}
          className="text-[11px] px-2 py-1 rounded bg-accent/15 text-accent border border-accent-dim disabled:opacity-40 shrink-0"
        >
          {busy ? '…' : force ? 'send anyway' : 'send'}
        </button>
        <button onClick={go} className="text-[11px] px-2 py-1 rounded border border-edge text-muted hover:text-ink shrink-0">
          open
        </button>
      </div>
      {err && <div className="mt-1 text-[10px] text-bad break-words">{err}</div>}
    </div>
  );
}
