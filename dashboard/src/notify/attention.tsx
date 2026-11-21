import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useHiddenRepoNames, type Chat } from '../api';
import { useOpenChat } from '../chat/openChat';
import { notificationsSupported, useNotifyEnabled } from './enabled';
import { inboxKey, setPanelOpen, useInbox, type InboxItem } from './inbox';
import { chatsKey, fetchChats } from './mute';

/**
 * The attention layer, mounted once in the shell (renders nothing). Push-based and explicit: an agent
 * that needs the user runs `loom notify "<question>"` (core/inbox.py); nothing is inferred from "the
 * turn ended". It does two things:
 *  - tab title `(N) loom`: how many unmuted questions are waiting on you;
 *  - a desktop notification for each new question the moment it's pushed (a server-sent stream, so a
 *    hidden tab hears it instantly) — only while the tab is in the background and the chat isn't muted;
 *    bursts are coalesced; clicking one opens the chat, or the panel if several.
 * The panel itself (reply inline / jump to the chat) is notify/inbox.tsx.
 */
export function AttentionNotifier() {
  const qc = useQueryClient();
  const openChat = useOpenChat();
  const [enabled] = useNotifyEnabled();
  const hiddenRepos = useHiddenRepoNames(); // a hidden project is out of sight — and quiet
  const { unmuted } = useInbox();
  const { data: chats } = useQuery({ queryKey: chatsKey, queryFn: fetchChats, refetchInterval: 8000 });

  // keep the latest values readable from the long-lived stream handler
  const live = useRef({ chats: [] as Chat[], enabled: false, openChat, hiddenRepos: new Set<string>() });
  live.current = { chats: chats ?? [], enabled, openChat, hiddenRepos };

  // --- tab title badge ---
  useEffect(() => {
    document.title = unmuted.length ? `(${unmuted.length}) loom` : 'loom';
  }, [unmuted.length]);

  // --- desktop notifications from the live stream ---
  useEffect(() => {
    if (typeof EventSource === 'undefined') return;
    const pending = new Map<number, InboxItem>();
    let timer: number | undefined;
    const fire = () => {
      timer = undefined;
      const items = [...pending.values()];
      pending.clear();
      if (!items.length || !notificationsSupported || Notification.permission !== 'granted') return;
      const one = items.length === 1;
      const n = new Notification(one ? items[0].chat_name : `${items.length} agents need you`, {
        body: one ? items[0].text.slice(0, 220) : items.map((x) => x.chat_name).slice(0, 4).join(' · '),
        tag: 'loom-inbox',
      });
      n.onclick = () => {
        window.focus();
        if (one) {
          const it = items[0];
          live.current.openChat({ resume: it.chat_id, cwd: it.cwd ?? undefined, title: it.chat_name, mode: 'terminal', agent: it.agent ?? undefined });
        } else {
          setPanelOpen(true);
        }
        n.close();
      };
    };
    const es = new EventSource('/api/events/stream');
    es.onmessage = (m) => {
      let items: InboxItem[] = [];
      try {
        items = JSON.parse(m.data).inbox ?? [];
      } catch {
        return;
      }
      if (!items.length) return;
      qc.invalidateQueries({ queryKey: inboxKey }); // the panel + badge update at once
      const l = live.current;
      if (!l.enabled || (!document.hidden && document.hasFocus())) return; // you're here: the badge already shows it
      for (const it of items) {
        if (it.muted || it.archived || (it.repo && l.hiddenRepos.has(it.repo))) continue;
        pending.set(it.id, it);
      }
      if (pending.size && timer === undefined) timer = window.setTimeout(fire, 2000); // coalesce a burst
    };
    return () => {
      es.close();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [qc]);

  return null;
}
