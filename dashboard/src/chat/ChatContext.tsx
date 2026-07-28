import { useEffect, useState, type ReactNode } from 'react';
import { NotesModal } from '../notes/NotesModal';
import { ChatCtx, ChatShellCtx, type ActiveChat } from './openChat';

export { useOpenChat } from './openChat';
export type { ActiveChat } from './openChat';

function setChatParam(sid: string | null) {
  const url = new URL(location.href);
  if (sid) url.searchParams.set('chat', sid);
  else url.searchParams.delete('chat');
  history.replaceState(null, '', url.toString());
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const [active, setActive] = useState<ActiveChat | null>(null);

  // Restore from ?chat=<session_id> on first load (refresh / deep link). Fetch the chat's
  // locked mode/agent so a terminal chat restores into the right surface + CLI.
  useEffect(() => {
    const sid = new URLSearchParams(location.search).get('chat');
    if (!sid) return;
    fetch(`/api/chats/${sid}`)
      .then((r) => r.json())
      .then((d) =>
        setActive({
          resume: sid,
          title: sid.slice(0, 8),
          mode: d.mode ?? undefined,
          cwd: d.chat?.cwd ?? undefined,
          agent: d.agent ?? d.chat?.agent ?? undefined,
        }),
      )
      .catch(() => setActive({ resume: sid, title: sid.slice(0, 8) }));
  }, []);

  const open = (c: ActiveChat) => {
    setActive(c);
    setChatParam(c.resume ?? null);
  };
  const close = () => {
    setActive(null);
    setChatParam(null);
  };

  return (
    <ChatCtx.Provider value={open}>
      <ChatShellCtx.Provider value={{ active, open, close }}>
        {children}
        {/* Global "all chat notes" modal — mounted here so it can reach useOpenChat and float
            above the shell. Reads its own open-state; renders null when closed. */}
        <NotesModal />
      </ChatShellCtx.Provider>
    </ChatCtx.Provider>
  );
}
