import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useChatActions, type Chat } from '../api';

/** The shared active-chat list (same cache key the sidebar and ⌘K use). */
export const chatsKey = ['chats', { scope: 'all', tab: 'active', q: '' }] as const;
export const fetchChats = () =>
  fetch('/api/chats?scope=all&tab=active')
    .then((r) => r.json())
    .then((d) => d.chats as Chat[]);

/** Mute / unmute a chat's notifications. Optimistic (every cached chat list updates at once), then
 *  persisted in the chat's overlay (`PATCH /chats/{id}` muted) so every browser — and any future
 *  push channel — honours it. */
export function useSetMuted() {
  const qc = useQueryClient();
  const { patch } = useChatActions();
  return (id: string, muted: boolean) => {
    qc.setQueriesData<Chat[]>({ queryKey: ['chats'] }, (list) =>
      Array.isArray(list) ? list.map((c) => (c.id === id ? { ...c, muted } : c)) : list,
    );
    patch.mutate({ id, patch: { muted } });
  };
}

export function useChatMuted(id?: string): boolean {
  const { data } = useQuery({ queryKey: chatsKey, queryFn: fetchChats, refetchInterval: false });
  return !!data?.find((c) => c.id === id)?.muted;
}

/** 🔔 / 🔕 — `variant="row"` is the sidebar's hover control (always visible once muted). */
export function MuteButton({ id, muted, variant = 'header' }: { id: string; muted: boolean; variant?: 'row' | 'header' }) {
  const setMuted = useSetMuted();
  const base = variant === 'row' ? 'shrink-0 text-xs leading-none' : 'text-sm leading-none px-1.5';
  const vis = variant === 'row' ? (muted ? 'opacity-60 hover:opacity-100' : 'opacity-0 group-hover:opacity-100') : muted ? 'opacity-80' : 'opacity-60 hover:opacity-100';
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        setMuted(id, !muted);
      }}
      title={muted ? 'muted — no desktop notifications for this chat (click to unmute)' : 'mute notifications for this chat'}
      aria-pressed={muted}
      className={`${base} ${vis}`}
    >
      {muted ? '🔕' : '🔔'}
    </button>
  );
}
