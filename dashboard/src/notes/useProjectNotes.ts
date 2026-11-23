import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useRepos, useHiddenRepos, useTasks, type Chat } from '../api';
import { useActiveProjectName } from '../chat/activeProject';
import { chatsKey, fetchChats } from '../notify/mute';
import { setNoteRepos, useNotesList, type Note } from './notesStore';

/** The chat notes that belong to the project you're on. A note is keyed by chat id (and stored in this
 *  browser), so its project is resolved from the chat / task it belongs to — and remembered on the note
 *  once known. A note whose project can't be told (its chat and worktree are gone) is kept in view rather
 *  than silently lost. `scope: 'all'` shows every project's notes. */
export type NoteStatus = 'active' | 'archived' | 'gone';

export function useProjectNotes({ withStatus = false }: { withStatus?: boolean } = {}) {
  const all = useNotesList();
  const active = useActiveProjectName();
  const [scope, setScope] = useState<'project' | 'all'>('project');
  const { data: chats } = useQuery({ queryKey: chatsKey, queryFn: fetchChats, refetchInterval: false });
  // Whether a note's chat is still active, archived, or gone — from the same lists the sidebar's tabs use (the
  // archived list is only fetched while the notes modal is open).
  const { data: archived } = useQuery({
    queryKey: ['chats', { scope: 'all', tab: 'archived', q: '' }],
    queryFn: () => fetch('/api/chats?scope=all&tab=archived').then((r) => r.json()).then((d) => d.chats as Chat[]),
    enabled: withStatus,
    refetchInterval: false,
  });
  const { data: tasks } = useTasks();
  const { data: repos } = useRepos();
  const { data: hidden } = useHiddenRepos();

  const repoOf = useMemo(() => {
    const byChat = new Map((chats ?? []).map((c) => [c.id, c.repo]));
    const taskByChat = new Map((tasks ?? []).filter((t) => (t as { chat_id?: string }).chat_id).map((t) => [(t as { chat_id?: string }).chat_id!, t.repo]));
    const taskByWt = new Map((tasks ?? []).map((t) => [t.worktree_path, t.repo]));
    const roots = [...(repos ?? []), ...(hidden ?? [])].map((r) => [r.root.replace(/\/$/, ''), r.name] as const);
    return (n: Note): string | undefined => {
      if (n.repo) return n.repo;
      const direct = byChat.get(n.id) ?? taskByChat.get(n.id) ?? (n.cwd ? taskByWt.get(n.cwd) : undefined);
      if (direct) return direct;
      if (n.cwd) for (const [root, name] of roots) if (n.cwd === root || n.cwd.startsWith(root + '/')) return name;
      return undefined;
    };
  }, [chats, tasks, repos, hidden]);

  // backfill: remember each note's project the first time it can be resolved
  useEffect(() => {
    const found: Record<string, string> = {};
    for (const n of all) if (!n.repo) {
      const r = repoOf(n);
      if (r) found[n.id] = r;
    }
    if (Object.keys(found).length) setNoteRepos(found);
  }, [all, repoOf]);

  const statusOf = useMemo(() => {
    if (!chats || !archived) return (_n: Note): NoteStatus | undefined => undefined; // not loaded yet — say nothing
    const live = new Set(chats.map((c) => c.id));
    const shelved = new Set(archived.map((c) => c.id));
    return (n: Note): NoteStatus => (live.has(n.id) ? 'active' : shelved.has(n.id) ? 'archived' : 'gone');
  }, [chats, archived]);

  const mine = useMemo(() => (active ? all.filter((n) => { const r = repoOf(n); return !r || r === active; }) : all), [all, active, repoOf]);
  return {
    notes: scope === 'all' ? all : mine,
    mineCount: mine.length,
    otherCount: all.length - mine.length, // other projects' notes (hidden unless scope is 'all')
    scope,
    setScope,
    project: active,
    repoOf,
    statusOf,
  };
}
