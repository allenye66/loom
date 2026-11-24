import { createContext, useContext } from 'react';
import type { AgentId } from '../api';

export type ActiveChat = {
  cwd?: string;
  resume?: string;
  title: string;
  mode?: 'chat' | 'terminal'; // vestigial — every chat now opens into the terminal surface
  agent?: AgentId; // claude | grok | codex — sticky once the session is created
};

export const ChatCtx = createContext<(c: ActiveChat) => void>(() => {});

/** openChat({ cwd, resume?, title, agent? }) — opens/switches the live chat in the content pane. */
export const useOpenChat = () => useContext(ChatCtx);

/** The whole chat-shell state: the active chat plus open/close. The app shell (App.tsx)
 *  reads this to render the terminal pane beside the always-present sidebar, or an empty
 *  state when nothing is open. */
/** A task whose worktree is still being created (`+ new`): shown as a placeholder sidebar row and,
 *  while the user hasn't clicked elsewhere, as a loading state in the content pane. */
export type PendingChat = { id: number; branch: string; agent: AgentId; repo?: string; category?: string; chatId?: string };

export type ChatShell = {
  active: ActiveChat | null;
  open: (c: ActiveChat) => void;
  close: () => void;
  pending: PendingChat[];
  /** The pending task whose loading state the content pane is showing (null once the user
   *  navigates to another chat). */
  viewingPending: PendingChat | null;
  beginPending: (p: Omit<PendingChat, 'id' | 'chatId'>) => number;
  /** The worktree exists and its chat id is known: the pane stops showing the loading state.
   *  Returns whether it still was — i.e. the user is still waiting here, so open the new chat. */
  readyPending: (id: number, chatId?: string) => boolean;
  /** Forget a pending task (it failed, or its real chat row has appeared in the sidebar). */
  dropPending: (id: number) => void;
};
export const ChatShellCtx = createContext<ChatShell>({
  active: null,
  open: () => {},
  close: () => {},
  pending: [],
  viewingPending: null,
  beginPending: () => 0,
  readyPending: () => false,
  dropPending: () => {},
});
export const useChatShell = () => useContext(ChatShellCtx);
