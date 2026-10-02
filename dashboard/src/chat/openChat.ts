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
export type ChatShell = {
  active: ActiveChat | null;
  open: (c: ActiveChat) => void;
  close: () => void;
};
export const ChatShellCtx = createContext<ChatShell>({
  active: null,
  open: () => {},
  close: () => {},
});
export const useChatShell = () => useContext(ChatShellCtx);
