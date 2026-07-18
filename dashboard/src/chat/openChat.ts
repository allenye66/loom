import { createContext, useContext } from 'react';
import type { AgentId } from '../api';

export type ActiveChat = {
  cwd?: string;
  resume?: string;
  title: string;
  mode?: 'chat' | 'terminal'; // vestigial — every chat now opens into the terminal surface
  agent?: AgentId; // claude | grok — sticky once the session is created
};

export const ChatCtx = createContext<(c: ActiveChat) => void>(() => {});

/** openChat({ cwd, resume?, title, agent? }) — opens/switches the live chat overlay (terminal). */
export const useOpenChat = () => useContext(ChatCtx);
