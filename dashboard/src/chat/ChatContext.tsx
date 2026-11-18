import { useEffect, useRef, useState, type ReactNode } from 'react';
import { NotesModal } from '../notes/NotesModal';
import { ChatCtx, ChatShellCtx, type ActiveChat, type ChatShell, type PendingChat } from './openChat';

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
      .then((d) => {
        // The server's canonical loom chat id: a link may carry the session id of a chat whose
        // conversation came out under another id (it's bound to the loom chat id). Adopt it and
        // fix the URL, so the sidebar row, terminal and notes all agree on one id.
        const id: string = d.id ?? sid;
        if (id !== sid) setChatParam(id);
        setActive({
          resume: id,
          title: id.slice(0, 8),
          mode: d.mode ?? undefined,
          cwd: d.cwd ?? d.chat?.cwd ?? undefined, // d.cwd: the task worktree, even before any transcript
          agent: d.agent ?? d.chat?.agent ?? undefined,
        });
      })
      .catch(() => setActive({ resume: sid, title: sid.slice(0, 8) }));
  }, []);

  // Tasks being created. `viewing` = the one whose loading state fills the content pane; any
  // navigation (open another chat / close) hands the pane back, while the placeholder row stays.
  const [pending, setPending] = useState<PendingChat[]>([]);
  const [viewing, setViewingState] = useState<number | null>(null);
  const viewingRef = useRef<number | null>(null); // read by async callers (a POST finishing)
  const pendingSeq = useRef(0);
  const setViewing = (id: number | null) => {
    viewingRef.current = id;
    setViewingState(id);
  };
  const beginPending: ChatShell['beginPending'] = (p) => {
    const id = ++pendingSeq.current;
    setPending((l) => [...l, { ...p, id }]);
    setViewing(id);
    return id;
  };
  const readyPending: ChatShell['readyPending'] = (id, chatId) => {
    setPending((l) => l.map((x) => (x.id === id ? { ...x, chatId } : x)));
    const was = viewingRef.current === id;
    if (was) setViewing(null);
    return was;
  };
  const dropPending = (id: number) => {
    setPending((l) => l.filter((x) => x.id !== id));
    if (viewingRef.current === id) setViewing(null);
  };

  const open = (c: ActiveChat) => {
    // Re-titling the open chat (a rename) re-sends it with only `title` changed — that isn't
    // navigation, so it must not hand the pane back from a loading state.
    const retitle =
      !!active && c.resume === active.resume && c.cwd === active.cwd && c.agent === active.agent && c.title !== active.title;
    if (!retitle) setViewing(null);
    setActive(c);
    setChatParam(c.resume ?? null);
  };
  const close = () => {
    setViewing(null);
    setActive(null);
    setChatParam(null);
  };

  return (
    <ChatCtx.Provider value={open}>
      <ChatShellCtx.Provider
        value={{
          active,
          open,
          close,
          pending,
          viewingPending: pending.find((x) => x.id === viewing) ?? null,
          beginPending,
          readyPending,
          dropPending,
        }}
      >
        {children}
        {/* Global "all chat notes" modal — mounted here so it can reach useOpenChat and float
            above the shell. Reads its own open-state; renders null when closed. */}
        <NotesModal />
      </ChatShellCtx.Provider>
    </ChatCtx.Provider>
  );
}
