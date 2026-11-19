import { useSyncExternalStore } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';

/** Conflict radar (loom/core/radar.py): chats in a category that are changing the same files. */
export type RadarOverlap = {
  a: { id: string; name: string };
  b: { id: string; name: string };
  files: string[];
  n_files: number;
  conflicts: boolean | null; // would their committed work conflict? null = git couldn't say
  conflict_files: string[];
};
export type RadarResult = {
  key: string;
  category: string;
  computed_at: number | null;
  members: { id: string; name: string; files: number }[];
  overlaps: RadarOverlap[];
};
export type RadarSummary = { pairs: number; conflicts: number; computed_at: number };

/** Per category: how many chat pairs overlap / would conflict — for the sidebar badge. */
export const useRadarOverview = () =>
  useQuery({
    queryKey: ['radar'],
    queryFn: () =>
      fetch('/api/radar')
        .then((r) => r.json())
        .then((d) => d.categories as Record<string, RadarSummary>),
    refetchInterval: 20000,
  });

export const useCategoryRadar = (key: string | null) =>
  useQuery({
    queryKey: ['radar', key],
    enabled: !!key,
    queryFn: () => fetch(`/api/categories/${key}/radar`).then((r) => r.json() as Promise<RadarResult>),
    refetchInterval: 15000,
  });

export function useRefreshRadar() {
  const qc = useQueryClient();
  return async (key: string) => {
    const r = (await fetch(`/api/categories/${key}/radar?refresh=true`).then((x) => x.json())) as RadarResult;
    qc.setQueryData(['radar', key], r);
    qc.invalidateQueries({ queryKey: ['radar'], exact: true });
  };
}

// --- which category's radar is open (a tiny module store, like the board modal) ---
let openKey: string | null = null;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};
export const useOpenRadarKey = () => useSyncExternalStore(subscribe, () => openKey);
export const openRadar = (key: string) => {
  openKey = key;
  emit();
};
export const closeRadar = () => {
  if (openKey === null) return;
  openKey = null;
  emit();
};
