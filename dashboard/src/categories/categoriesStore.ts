/**
 * User-defined chat categories + which category each chat sits in. Drives the sidebar's
 * category groups. Each category belongs to one project (`repo`), so every project has its own
 * set.
 *
 * Server-side (`~/.loom/categories.json` via /api/categories) so loom's agents can see and
 * change them too (`loom chats categorize …`, D20). This module is a tiny external store over
 * that: a module-level value replaced on each change, exposed via a `useSyncExternalStore`
 * hook. Mutations apply optimistically, then adopt the server's state from the response; a
 * light poll (while anything is subscribed) picks up changes made by agents or other tabs.
 *
 * Categories used to live in this browser's localStorage (`loom.chatCategories`). The first
 * load imports them into the server once (an idempotent merge) and leaves the old key in place
 * as a backup.
 */
import { useSyncExternalStore } from 'react';

// `repo` = the project (registered repo name) the category belongs to. Categories made before
// projects existed have none until the sidebar files them (claimUnscopedCategories).
export type Category = { id: string; name: string; repo?: string };
type State = {
  categories: Category[]; // display order
  assign: Record<string, string>; // chatId -> categoryId
};

const LEGACY_KEY = 'loom.chatCategories';
const IMPORTED_KEY = 'loom.chatCategories.importedToServer';
const POLL_MS = 4000;

let state: State = { categories: [], assign: {} };
let loaded = false;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

function sanitize(raw: any): State {
  const categories = Array.isArray(raw?.categories)
    ? raw.categories
        .filter((c: any) => c && typeof c.id === 'string' && typeof c.name === 'string')
        .map((c: any) => ({ id: c.id, name: c.name, ...(typeof c.repo === 'string' ? { repo: c.repo } : {}) }))
    : [];
  const assign =
    raw?.assign && typeof raw.assign === 'object'
      ? Object.fromEntries(Object.entries(raw.assign as Record<string, unknown>).filter(([, v]) => typeof v === 'string'))
      : {};
  return { categories, assign: assign as Record<string, string> };
}

/** Adopt a server state, notifying only if it actually changed (the poll mustn't re-render). */
function adopt(next: State) {
  loaded = true;
  if (JSON.stringify(next) === JSON.stringify(state)) return;
  state = next;
  emit();
}

async function call(method: string, path: string, body?: unknown): Promise<void> {
  try {
    const r = await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (r.ok) adopt(sanitize(await r.json()));
    else void refresh(); // rejected → resync from the server instead of keeping the optimistic guess
  } catch {
    /* offline — the next poll resyncs */
  }
}

/** One-time import of this browser's legacy localStorage categories into the server. */
async function importLegacy(): Promise<void> {
  let legacy: State;
  try {
    if (localStorage.getItem(IMPORTED_KEY)) return;
    legacy = sanitize(JSON.parse(localStorage.getItem(LEGACY_KEY) || '{}'));
  } catch {
    return;
  }
  if (legacy.categories.length === 0 && Object.keys(legacy.assign).length === 0) {
    try {
      localStorage.setItem(IMPORTED_KEY, '1');
    } catch {
      /* private mode */
    }
    return;
  }
  const r = await fetch('/api/categories-import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(legacy),
  });
  if (!r.ok) return; // try again next load
  adopt(sanitize(await r.json()));
  try {
    localStorage.setItem(IMPORTED_KEY, '1');
  } catch {
    /* private mode — the import is idempotent, so re-running it next load is harmless */
  }
}

let refreshing: Promise<void> | null = null;
function refresh(): Promise<void> {
  refreshing ??= (async () => {
    try {
      if (!loaded) await importLegacy();
      const r = await fetch('/api/categories');
      if (r.ok) adopt(sanitize(await r.json()));
      else if (r.status === 404 && !loaded) {
        // A loom server older than this dashboard (rebuilt but not restarted yet): show this
        // browser's legacy categories so the sidebar doesn't look emptied. `loaded` stays false,
        // so the import runs as soon as the server knows /api/categories.
        const legacy = sanitize(JSON.parse(localStorage.getItem(LEGACY_KEY) || '{}'));
        if (JSON.stringify(legacy) !== JSON.stringify(state)) setLocal(legacy);
      }
    } catch {
      /* server down — keep what we have */
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

let pollTimer: number | undefined;
const onFocus = () => void refresh();
const subscribe = (fn: () => void) => {
  listeners.add(fn);
  if (listeners.size === 1) {
    void refresh();
    pollTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh();
    }, POLL_MS);
    window.addEventListener('focus', onFocus);
  }
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0) {
      window.clearInterval(pollTimer);
      window.removeEventListener('focus', onFocus);
    }
  };
};

function setLocal(next: State) {
  state = next;
  emit();
}

function newId(): string {
  const rnd = Math.random().toString(36).slice(2, 8);
  return `cat_${Date.now().toString(36)}_${rnd}`;
}

/** Make a category. It goes first among its project's categories (just under Uncategorized),
 *  so it shows up near the top of the sidebar, ready to drag chats into. */
export function addCategory(name: string, repo?: string): string {
  const n = name.trim();
  if (!n) return '';
  const id = newId();
  setLocal({ ...state, categories: [{ id, name: n, ...(repo ? { repo } : {}) }, ...state.categories] });
  void call('POST', '/categories', { id, name: n, repo: repo ?? null });
  return id;
}

export function renameCategory(id: string, name: string) {
  const n = name.trim();
  if (!n) return;
  setLocal({ ...state, categories: state.categories.map((c) => (c.id === id ? { ...c, name: n } : c)) });
  void call('PATCH', `/categories/${encodeURIComponent(id)}`, { name: n });
}

/** Drop a category — its chats fall back to Uncategorized (their assignments are cleared). */
export function removeCategory(id: string) {
  const assign = { ...state.assign };
  for (const k of Object.keys(assign)) if (assign[k] === id) delete assign[k];
  setLocal({ categories: state.categories.filter((c) => c.id !== id), assign });
  void call('DELETE', `/categories/${encodeURIComponent(id)}`);
}

/** Assign a chat to a category, or pass null to move it back to Uncategorized. */
export function assignChat(chatId: string, categoryId: string | null) {
  const assign = { ...state.assign };
  if (!categoryId) delete assign[chatId];
  else assign[chatId] = categoryId;
  setLocal({ ...state, assign });
  void call('PUT', '/categories-assign', { chat_ids: [chatId], category_id: categoryId });
}

/** Save a new order for some categories after a drag in the sidebar: `ids` = one project's
 *  categories top to bottom. Every other category keeps its slot in the global list. */
export function setCategoryOrder(ids: string[]) {
  const byId = new Map(state.categories.map((c) => [c.id, c]));
  const ordered = ids.filter((id) => byId.has(id));
  const moving = new Set(ordered);
  const queue = ordered.map((id) => byId.get(id)!);
  // Refill the slots these categories already hold, in their new order.
  setLocal({ ...state, categories: state.categories.map((c) => (moving.has(c.id) ? queue.shift()! : c)) });
  void call('PUT', '/categories-order', { ids: ordered });
}

/** File every category that has no project yet under `pick(its chat ids)`. A no-op once all
 *  are filed, so it's cheap to call on every chat-list refresh. Waits for the first server
 *  load so it never files categories it hasn't seen yet. */
export function claimUnscopedCategories(pick: (chatIds: string[]) => string) {
  if (!loaded || !state.categories.some((c) => !c.repo)) return;
  const chatsIn = (catId: string) => Object.keys(state.assign).filter((k) => state.assign[k] === catId);
  const claims = state.categories.filter((c) => !c.repo).map((c) => ({ id: c.id, repo: pick(chatsIn(c.id)) }));
  setLocal({
    ...state,
    categories: state.categories.map((c) => {
      const cl = claims.find((x) => x.id === c.id);
      return cl && cl.repo ? { ...c, repo: cl.repo } : c;
    }),
  });
  for (const cl of claims) if (cl.repo) void call('PATCH', `/categories/${encodeURIComponent(cl.id)}`, { repo: cl.repo });
}

export function useCategories(): State {
  return useSyncExternalStore(subscribe, () => state);
}
