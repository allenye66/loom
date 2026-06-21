import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

const BASE = '/api';

export type Ports = {
  offset: number;
  backend: number;
  frontend: number;
};
export type ServiceProc = { name: string; pid?: number | null; port?: number | null; healthy: boolean; health_url?: string | null };
export type Git = { branch?: string; dirty?: boolean; ahead?: number; behind?: number };
export type TestRun = { running: boolean; exit_code: number | null; command?: string } | null;
export type Task = {
  id: string;
  repo: string;
  repo_root: string;
  branch: string;
  base_branch: string;
  worktree_path: string;
  state: string;
  ports: Ports | null;
  services: ServiceProc[];
  note?: string | null;
  git?: Git;
  test?: TestRun;
  chat_archived?: boolean;
  chat_id?: string | null;
  chat_mode?: 'chat' | 'terminal' | null; // locked surface for this task's chat (null = unchosen)
};
export type Repo = { name: string; root: string; base_branch: string };
export type Check = { name: string; ok: boolean; hint: string };

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(BASE + path);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
async function jsend<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(BASE + path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const detail = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(detail.detail ?? r.statusText);
  }
  return r.json();
}

export const useTasks = () =>
  useQuery({ queryKey: ['tasks'], queryFn: () => jget<{ tasks: Task[] }>('/tasks').then((d) => d.tasks) });

export const useRepos = () =>
  useQuery({
    queryKey: ['repos'],
    queryFn: () => jget<{ repos: Repo[] }>('/repos').then((d) => d.repos),
    refetchInterval: false,
  });

export const useDoctor = () =>
  useQuery({
    queryKey: ['doctor'],
    queryFn: () => jget<{ checks: Check[] }>('/doctor').then((d) => d.checks),
    refetchInterval: 15000,
  });

export const getLogs = (id: string, kind = 'test') => jget<{ log: string }>(`/tasks/${id}/logs?kind=${kind}`);

export function useTaskActions() {
  const qc = useQueryClient();
  const invTasks = () => qc.invalidateQueries({ queryKey: ['tasks'] });
  return {
    create: useMutation({
      mutationFn: (b: { repo_root: string; branch: string; base_branch?: string }) =>
        jsend<Task>('/tasks', 'POST', b),
      onSuccess: invTasks,
    }),
    remove: useMutation({
      mutationFn: (v: { id: string; force?: boolean }) =>
        jsend(`/tasks/${v.id}${v.force ? '?force=true' : ''}`, 'DELETE'),
      onSuccess: invTasks,
    }),
    test: useMutation({
      mutationFn: (v: { id: string; pytest_args?: string }) =>
        jsend(`/tasks/${v.id}/test`, 'POST', { pytest_args: v.pytest_args ?? '' }),
      onSuccess: invTasks,
    }),
    start: useMutation({ mutationFn: (id: string) => jsend(`/tasks/${id}/start`, 'POST'), onSuccess: invTasks }),
    stop: useMutation({ mutationFn: (id: string) => jsend(`/tasks/${id}/stop`, 'POST'), onSuccess: invTasks }),
    addRepo: useMutation({
      mutationFn: (root: string) => jsend<Repo>('/repos', 'POST', { root }),
      onSuccess: () => qc.invalidateQueries({ queryKey: ['repos'] }),
    }),
  };
}

// --- chats / sessions --------------------------------------------------------
export type Chat = {
  id: string;
  title?: string | null;
  display_title: string;
  preview?: string | null;
  first_prompt?: string | null;
  branch?: string | null;
  cwd?: string | null;
  prs: (number | string)[];
  pr_repo?: string | null;
  pr_manual?: number | null;
  created?: string | null;
  last_active: number;
  n_user: number;
  n_assistant: number;
  repo?: string | null;
  task?: string | null;
  name?: string | null;
  tags: string[];
  description?: string | null;
  starred: boolean;
  archived: boolean;
  hidden: boolean;
  mode?: 'chat' | 'terminal' | null;
};

export type ChatQuery = { repo?: string; scope: string; tab: string; q?: string; starred?: boolean };

export const useChats = (params: ChatQuery) =>
  useQuery({
    queryKey: ['chats', params],
    queryFn: () => {
      const qs = new URLSearchParams();
      if (params.repo) qs.set('repo', params.repo);
      qs.set('scope', params.scope);
      qs.set('tab', params.tab);
      if (params.q) qs.set('q', params.q);
      if (params.starred) qs.set('starred', 'true');
      return jget<{ chats: Chat[] }>(`/chats?${qs.toString()}`).then((d) => d.chats);
    },
  });

export const useTrash = () =>
  useQuery({ queryKey: ['chats-trash'], queryFn: () => jget<{ trash: string[] }>('/chats-trash').then((d) => d.trash) });

type ChatPatch = Partial<Pick<Chat, 'starred' | 'archived' | 'hidden' | 'name' | 'tags' | 'description' | 'mode'>>;

export function useChatActions() {
  const qc = useQueryClient();
  const inv = () => qc.invalidateQueries({ queryKey: ['chats'] });
  return {
    patch: useMutation({
      mutationFn: (v: { id: string; patch: ChatPatch }) => jsend(`/chats/${v.id}`, 'PATCH', v.patch),
      onSuccess: inv,
    }),
    remove: useMutation({ mutationFn: (id: string) => jsend(`/chats/${id}`, 'DELETE'), onSuccess: inv }),
    restore: useMutation({
      mutationFn: (id: string) => jsend(`/chats/${id}/restore`, 'POST'),
      onSuccess: () => qc.invalidateQueries({ queryKey: ['chats'] }),
    }),
  };
}

