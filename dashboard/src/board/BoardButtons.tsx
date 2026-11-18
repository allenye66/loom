import { useQuery } from '@tanstack/react-query';
import type { Chat } from '../api';
import { openBoard, useBoards } from './boardApi';

/** The sidebar category header's board button. Off (the default): a hover-only ▤ that opens the
 *  "enable" prompt. On: always visible, with an unread badge when there's something new. */
export function BoardButton({ catId, name }: { catId: string; name: string }) {
  const { data } = useBoards();
  const b = data?.find((x) => x.key === catId);
  const unread = b?.unread ?? 0;
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        openBoard(catId);
      }}
      title={
        b?.enabled
          ? `message board for "${name}" — ${b.count} post${b.count === 1 ? '' : 's'}${unread ? `, ${unread} unread` : ''}`
          : `enable a message board for "${name}" (off)`
      }
      className={`text-[10px] leading-none shrink-0 inline-flex items-center gap-0.5 ${
        unread
          ? 'text-accent'
          : b?.enabled
            ? 'text-muted/80 hover:text-ink'
            : 'opacity-0 group-hover/gh:opacity-100 text-muted/60 hover:text-ink'
      }`}
    >
      ▤{unread > 0 && <span className="mono text-[9px] px-1 py-px rounded-full bg-accent/20">{unread}</span>}
    </button>
  );
}

/** A chip in an open chat's header row, for a chat that's in a category: opens that category's board. */
export function BoardChip({ chatId }: { chatId?: string }) {
  const { data: chats } = useQuery({
    queryKey: ['chats', { scope: 'all', tab: 'active', q: '' }],
    queryFn: () =>
      fetch('/api/chats?scope=all&tab=active')
        .then((r) => r.json())
        .then((d) => d.chats as Chat[]),
    refetchInterval: false,
  });
  const { data: boards } = useBoards();
  const me = chats?.find((c) => c.id === chatId);
  if (!me?.category) return null;
  const b = boards?.find((x) => x.key === me.category);
  if (!b?.enabled) return null; // boards are opt-in per category; the sidebar's ▤ is where you turn one on
  const unread = b?.unread ?? 0;
  return (
    <button
      onClick={() => openBoard(me.category!)}
      title={`open the "${me.category_name}" message board — chats in this category coordinate here`}
      className={`px-2 py-0.5 rounded border inline-flex items-center gap-1.5 ${
        unread ? 'border-accent-dim text-accent' : 'border-edge text-muted hover:text-ink'
      }`}
    >
      ▤ {me.category_name}
      {b && b.count > 0 && <span className="text-muted">· {b.count}</span>}
      {unread > 0 && <span className="px-1 rounded-full bg-accent/20 text-[9px]">{unread} new</span>}
    </button>
  );
}
