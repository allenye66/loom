import { useSyncExternalStore } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

/** The dashboard reads/writes the per-category message boards (loom/server/board_api.py) as the
 *  reader/author "user". A board's key is its category id. */
export type BoardPost = {
  seq: number;
  ts: number; // epoch seconds
  from: string; // chat id, or "user"
  from_name: string;
  to: string[]; // chat ids the post was addressed to / nudged
  text: string;
  reply_to: number | null;
};
export type BoardSummary = {
  key: string;
  name: string;
  enabled: boolean; // boards are off until the user turns one on, per category
  repo?: string | null;
  members: number;
  count: number;
  last_seq: number;
  last_ts: number | null;
  unread?: number; // for the "user" reader
};

const json = (r: Response) => {
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
};

/** Every category's board with this user's unread count — drives the sidebar/header badges. */
export const useBoards = () =>
  useQuery({
    queryKey: ['boards'],
    queryFn: () => fetch('/api/boards?reader=user').then(json).then((d) => d.boards as BoardSummary[]),
    refetchInterval: 5000,
  });

/** One board's posts, live while it's open. */
export const useBoardPosts = (key: string | null) =>
  useQuery({
    queryKey: ['board', key],
    enabled: !!key,
    queryFn: () =>
      fetch(`/api/boards/${key}?reader=user`)
        .then(json)
        .then((d) => ({
          name: d.board.name as string,
          enabled: !!d.board.enabled,
          posts: d.posts as BoardPost[],
          last_seq: d.last_seq as number,
        })),
    refetchInterval: 3000,
  });

export function useBoardActions() {
  const qc = useQueryClient();
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['board'] });
    qc.invalidateQueries({ queryKey: ['boards'] });
  };
  return {
    post: async (key: string, text: string, to: string[] = [], replyTo?: number) => {
      const r = await fetch(`/api/boards/${key}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text, to, reply_to: replyTo ?? null }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail ?? `${r.status}`);
      refresh();
      return (await r.json()) as { post: BoardPost; nudged: Record<string, string> };
    },
    /** Turn a category's board on (there's no off switch for now). */
    enable: async (key: string) => {
      const r = await fetch(`/api/boards/${key}/enable`, { method: 'POST' });
      if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail ?? `${r.status}`);
      refresh();
    },
    markSeen: async (key: string, seq?: number) => {
      await fetch(`/api/boards/${key}/seen`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reader: 'user', seq: seq ?? null }),
      });
      qc.invalidateQueries({ queryKey: ['boards'] });
    },
  };
}

// --- which board (category) is open in the modal: a tiny module store, like the notes modal ---
let openKey: string | null = null;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};
export const useOpenBoardKey = () => useSyncExternalStore(subscribe, () => openKey);
export function openBoard(key: string) {
  openKey = key;
  emit();
}
export function closeBoard() {
  if (openKey === null) return;
  openKey = null;
  emit();
}
