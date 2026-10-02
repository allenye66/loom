/**
 * Per-chat notes, persisted to this browser's localStorage.
 *
 * A note is keyed by chat id and holds free text plus enough chat metadata
 * (title / cwd / agent) to reopen the chat straight from the global notes modal
 * without a server round-trip. This is a tiny external store (module-level cache
 * + listeners) exposed through `useSyncExternalStore` hooks, so the per-chat
 * drawer, the header badge count, and the global modal all stay in sync — and
 * so do other tabs (via the `storage` event).
 */
import { useSyncExternalStore } from 'react';
import type { AgentId } from '../api';

export type Note = {
  id: string; // chat id this note belongs to
  text: string;
  updated: number; // epoch ms of last edit
  title: string; // chat title snapshot (for the modal / reopen)
  cwd?: string;
  agent?: AgentId;
};

type NotesMap = Record<string, Note>;

const KEY = 'loom.chatNotes';

function load(): NotesMap {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (!raw || typeof raw !== 'object') return {};
    const out: NotesMap = {};
    for (const [id, n] of Object.entries(raw as Record<string, any>)) {
      if (n && typeof n.text === 'string' && n.text.trim()) {
        out[id] = {
          id,
          text: n.text,
          updated: typeof n.updated === 'number' ? n.updated : 0,
          title: typeof n.title === 'string' && n.title ? n.title : id.slice(0, 8),
          cwd: typeof n.cwd === 'string' ? n.cwd : undefined,
          agent: n.agent === 'claude' || n.agent === 'grok' || n.agent === 'codex' ? n.agent : undefined,
        };
      }
    }
    return out;
  } catch {
    return {};
  }
}

let cache: NotesMap = load();
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
    localStorage.setItem(KEY, JSON.stringify(cache));
  } catch {
    /* private mode / quota — notes just won't persist this session */
  }
}

// Keep sibling tabs in sync — another tab editing notes rewrites our key.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', (e) => {
    if (e.key === KEY) {
      cache = load();
      emit();
    }
  });
}

export type NoteMeta = { title?: string; cwd?: string; agent?: AgentId };

/** Write (or, on empty text, clear) the note for a chat. Metadata is merged so a later
 *  save can't wipe a cwd/agent captured earlier. */
export function setNote(id: string, text: string, meta: NoteMeta = {}) {
  const existing = cache[id];
  if (!text.trim()) {
    if (!existing) return;
    const next = { ...cache };
    delete next[id];
    cache = next;
  } else {
    cache = {
      ...cache,
      [id]: {
        id,
        text,
        updated: Date.now(),
        title: meta.title || existing?.title || id.slice(0, 8),
        cwd: meta.cwd ?? existing?.cwd,
        agent: meta.agent ?? existing?.agent,
      },
    };
  }
  persist();
  emit();
}

export function deleteNote(id: string) {
  if (!cache[id]) return;
  const next = { ...cache };
  delete next[id];
  cache = next;
  persist();
  emit();
}

export function useNote(id: string | undefined): Note | undefined {
  return useSyncExternalStore(subscribe, () => (id ? cache[id] : undefined));
}

// Derived, most-recent-first list. Memoized by `cache` identity so the snapshot ref is
// stable between renders (useSyncExternalStore bails out on Object.is equality).
let listFrom: NotesMap = cache;
let listCache: Note[] = Object.values(cache).sort((a, b) => b.updated - a.updated);
function getList(): Note[] {
  if (cache !== listFrom) {
    listFrom = cache;
    listCache = Object.values(cache).sort((a, b) => b.updated - a.updated);
  }
  return listCache;
}
export function useNotesList(): Note[] {
  return useSyncExternalStore(subscribe, getList);
}

export function useNotesCount(): number {
  return useSyncExternalStore(subscribe, () => Object.keys(cache).length);
}

// --- global "all notes" modal visibility (module store; any header can open it) ----------
let modalOpen = false;
const modalListeners = new Set<() => void>();
const emitModal = () => modalListeners.forEach((l) => l());
const subscribeModal = (fn: () => void) => {
  modalListeners.add(fn);
  return () => {
    modalListeners.delete(fn);
  };
};
export function openNotesModal() {
  if (modalOpen) return;
  modalOpen = true;
  emitModal();
}
export function closeNotesModal() {
  if (!modalOpen) return;
  modalOpen = false;
  emitModal();
}
export function useNotesModalOpen(): boolean {
  return useSyncExternalStore(subscribeModal, () => modalOpen);
}
