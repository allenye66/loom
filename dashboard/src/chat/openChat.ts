import { createContext, useContext } from 'react';

export type ActiveChat = {
  cwd?: string;
  resume?: string;
  title: string;
  mode?: 'chat' | 'terminal'; // vestigial — every chat now opens into the terminal surface
};

export const ChatCtx = createContext<(c: ActiveChat) => void>(() => {});

/** openChat({ cwd, resume?, title }) — opens/switches the live chat overlay (terminal). */
export const useOpenChat = () => useContext(ChatCtx);
