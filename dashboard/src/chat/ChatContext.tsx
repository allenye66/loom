import { useEffect, useState, type ReactNode } from 'react';
import { TerminalView } from '../term/TerminalView';
import { ChatCtx, type ActiveChat } from './openChat';

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
      {children}
      {active && (
        // Terminal is the only surface now — every chat opens into the real agent TUI.
        <TerminalView
          // remount (fresh socket) when switching chats
          key={active.resume ?? active.cwd ?? active.title}
          resume={active.resume}
          cwd={active.cwd}
          title={active.title}
          agent={active.agent}
          onClose={close}
        />
      )}
    </ChatCtx.Provider>
  );
}
