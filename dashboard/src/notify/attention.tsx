import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Chat } from '../api';
import { useChatShell, useOpenChat } from '../chat/openChat';
import { WORKING_WITHIN_S } from '../chat/shortcuts';
import { BellIcon, BellOffIcon } from '../ui/icons';
import { chatsKey, fetchChats } from './mute';

const KEY = 'loom.notify';
const supported = typeof window !== 'undefined' && 'Notification' in window;

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}
const listeners = new Set<() => void>();
function write(on: boolean) {
  try {
    localStorage.setItem(KEY, on ? '1' : '0');
  } catch {
    /* private mode — the toggle just won't persist */
  }
  listeners.forEach((l) => l());
}

/** Desktop notifications on/off (per browser). Turning on asks the browser for permission. */
export function useNotifyEnabled(): [boolean, () => void, NotificationPermission | 'unsupported'] {
  const [on, setOn] = useState(read);
  const [perm, setPerm] = useState<NotificationPermission | 'unsupported'>(supported ? Notification.permission : 'unsupported');
  useEffect(() => {
    const l = () => setOn(read());
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);
  const toggle = async () => {
    if (on) return write(false);
    if (supported && Notification.permission === 'default') setPerm(await Notification.requestPermission());
    else if (supported) setPerm(Notification.permission);
    write(true);
  };
  return [on && perm !== 'denied', toggle, perm];
}

/** Sidebar header bell: desktop notifications when a chat needs you (mute individual chats with their bell). */
export function NotifyBell() {
  const [on, toggle, perm] = useNotifyEnabled();
  if (perm === 'unsupported') return null;
  return (
    <button
      onClick={toggle}
      aria-pressed={on}
      title={
        perm === 'denied'
          ? 'notifications are blocked for this site in your browser settings'
          : on
            ? 'desktop notifications on — when an unmuted chat needs you and this tab is in the background (click to turn off)'
            : 'turn on desktop notifications for chats that need you (you can mute individual chats)'
      }
      className={`leading-none ${on ? 'text-accent' : 'text-muted opacity-70 hover:opacity-100 hover:text-ink'}`}
    >
      {on ? <BellIcon size={14} /> : <BellOffIcon size={14} />}
    </button>
  );
}

type Pending = { name: string; waitingOn: boolean };

/**
 * The attention layer, mounted once in the shell (renders nothing):
 *  - tab title `(N) loom`: unmuted chats waiting on you (not the one you're looking at);
 *  - desktop notifications, driven by the agents' own lifecycle events (Stop / Notification hooks →
 *    core/events.py → a server-sent stream, so a hidden tab hears them instantly). Only when the tab
 *    is in the background, never for muted or archived chats, and bursts are coalesced into one.
 */
export function AttentionNotifier() {
  const active = useChatShell().active;
  const openChat = useOpenChat();
  const [enabled] = useNotifyEnabled();
  const { data: chats } = useQuery({ queryKey: chatsKey, queryFn: fetchChats, refetchInterval: 8000 });
  const { data: terms } = useQuery({
    queryKey: ['terminals'],
    queryFn: () =>
      fetch('/api/terminals')
        .then((r) => r.json())
        .then((d) => d.terminals as { chat_id: string; idle_sec: number; needs: boolean }[]),
    refetchInterval: 2000,
  });

  // keep the latest values readable from the long-lived event handler
  const live = useRef({ chats: [] as Chat[], activeSid: undefined as string | undefined, enabled: false, openChat });
  live.current = { chats: chats ?? [], activeSid: active?.resume, enabled, openChat };

  // --- tab title badge ---
  useEffect(() => {
    const byId = new Map((chats ?? []).map((c) => [c.id, c]));
    let n = 0;
    for (const t of terms ?? []) {
      const c = byId.get(t.chat_id);
      if (!c || c.archived || c.muted || t.chat_id === active?.resume) continue;
      if (t.needs && t.idle_sec >= WORKING_WITHIN_S) n++;
    }
    document.title = n ? `(${n}) loom` : 'loom';
  }, [chats, terms, active?.resume]);

  // --- desktop notifications from the live event stream ---
  useEffect(() => {
    if (typeof EventSource === 'undefined') return;
    const pending = new Map<string, Pending>();
    let timer: number | undefined;
    const fire = () => {
      timer = undefined;
      const items = [...pending.entries()];
      pending.clear();
      if (!items.length || !supported || Notification.permission !== 'granted') return;
      const one = items.length === 1;
      const [id, p] = items[0];
      const n = new Notification(one ? p.name : `${items.length} chats need you`, {
        body: one ? (p.waitingOn ? 'needs your attention' : 'finished — waiting for you') : items.map(([, x]) => x.name).slice(0, 4).join(' · '),
        tag: 'loom-attention',
      });
      n.onclick = () => {
        window.focus();
        const c = live.current.chats.find((x) => x.id === id);
        if (one && c) live.current.openChat({ resume: c.id, cwd: c.cwd ?? undefined, title: c.name || c.display_title, mode: c.mode ?? undefined, agent: c.agent ?? undefined });
        n.close();
      };
    };
    const es = new EventSource('/api/events/stream');
    es.onmessage = (m) => {
      let evs: { chat_id: string; event: string }[] = [];
      try {
        evs = JSON.parse(m.data).events ?? [];
      } catch {
        return;
      }
      const l = live.current;
      if (!l.enabled || (!document.hidden && document.hasFocus())) return; // you're here: the sidebar already shows it
      for (const e of evs) {
        if (e.event !== 'Stop' && e.event !== 'Notification' && e.event !== 'StopFailure') continue;
        const c = l.chats.find((x) => x.id === e.chat_id);
        if (!c || c.archived || c.muted) continue;
        pending.set(c.id, { name: c.name || c.display_title || c.id.slice(0, 8), waitingOn: e.event === 'Notification' });
      }
      if (pending.size && timer === undefined) timer = window.setTimeout(fire, 2500); // coalesce a burst
    };
    return () => {
      es.close();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  return null;
}
