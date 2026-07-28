/**
 * User-defined chat categories + which category each chat sits in, persisted to this
 * browser's localStorage. Drives the "board" layout on the Chats page (create categories,
 * drag chats between them). Same tiny-external-store shape as notesStore: a module-level
 * value replaced on each change, exposed via a `useSyncExternalStore` hook, synced across
 * tabs through the `storage` event.
 */
import { useSyncExternalStore } from 'react';

export type Category = { id: string; name: string };
type State = {
  categories: Category[]; // display order
  assign: Record<string, string>; // chatId -> categoryId
};

const KEY = 'loom.chatCategories';

function load(): State {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    const categories = Array.isArray(raw?.categories)
      ? raw.categories
          .filter((c: any) => c && typeof c.id === 'string' && typeof c.name === 'string')
          .map((c: any) => ({ id: c.id, name: c.name }))
      : [];
    const assign =
      raw?.assign && typeof raw.assign === 'object'
        ? Object.fromEntries(
            Object.entries(raw.assign as Record<string, unknown>).filter(
              ([, v]) => typeof v === 'string',
            ),
          )
        : {};
    return { categories, assign: assign as Record<string, string> };
  } catch {
    return { categories: [], assign: {} };
  }
}

let state: State = load();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (fn: () => void) => {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
};

function persist() {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    /* private mode / quota — categories just won't persist this session */
  }
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key === KEY) {
      state = load();
      emit();
    }
  });
}

function newId(): string {
  const rnd = Math.random().toString(36).slice(2, 8);
  return `cat_${Date.now().toString(36)}_${rnd}`;
}

export function addCategory(name: string): string {
  const n = name.trim();
  if (!n) return '';
  const id = newId();
  state = { ...state, categories: [...state.categories, { id, name: n }] };
  persist();
  emit();
  return id;
}

export function renameCategory(id: string, name: string) {
  const n = name.trim();
  if (!n) return;
  state = { ...state, categories: state.categories.map((c) => (c.id === id ? { ...c, name: n } : c)) };
  persist();
  emit();
}

/** Drop a category — its chats fall back to Uncategorized (their assignments are cleared). */
export function removeCategory(id: string) {
  const assign = { ...state.assign };
  for (const k of Object.keys(assign)) if (assign[k] === id) delete assign[k];
  state = { categories: state.categories.filter((c) => c.id !== id), assign };
  persist();
  emit();
}

/** Assign a chat to a category, or pass null to move it back to Uncategorized. */
export function assignChat(chatId: string, categoryId: string | null) {
  const assign = { ...state.assign };
  if (!categoryId) delete assign[chatId];
  else assign[chatId] = categoryId;
  state = { ...state, assign };
  persist();
  emit();
}

/** Nudge a category one slot left (-1) or right (+1) in the display order. */
export function moveCategory(id: string, dir: -1 | 1) {
  const idx = state.categories.findIndex((c) => c.id === id);
  if (idx < 0) return;
  const j = idx + dir;
  if (j < 0 || j >= state.categories.length) return;
  const categories = [...state.categories];
  [categories[idx], categories[j]] = [categories[j], categories[idx]];
  state = { ...state, categories };
  persist();
  emit();
}

export function useCategories(): State {
  return useSyncExternalStore(subscribe, () => state);
}
