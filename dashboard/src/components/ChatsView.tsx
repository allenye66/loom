import { useEffect, useRef, useState } from 'react';
import { useChatActions, useChats, useTrash, type Chat } from '../api';
import { useOpenChat } from '../chat/ChatContext';
import { PrBadges } from './PrBadges';

const INPUT = 'mono text-[11px] px-2 py-1.5 rounded bg-canvas border border-edge outline-none focus:border-accent';
const ACTION = 'text-[11px] px-2 py-1 rounded border border-edge text-muted hover:text-ink';

function rel(sec: number): string {
  const s = Date.now() / 1000 - sec;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(sec * 1000).toLocaleDateString();
}

function ChatRow({ chat, selected, onSelect }: { chat: Chat; selected: boolean; onSelect: () => void }) {
  const a = useChatActions();
  const openChat = useOpenChat();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(chat.name ?? '');
  const [tags, setTags] = useState(chat.tags.join(', '));
  const [desc, setDesc] = useState(chat.description ?? '');

  const save = () => {
    a.patch.mutate({
      id: chat.id,
      patch: {
        name: name || '',
        tags: tags.split(',').map((s) => s.trim()).filter(Boolean),
        description: desc || '',
      },
    });
    setEditing(false);
  };

  const patch = (p: Parameters<typeof a.patch.mutate>[0]['patch']) => a.patch.mutate({ id: chat.id, patch: p });

  return (
    <div
      onClick={onSelect}
      className={`group rounded-lg border px-3 py-2.5 transition-colors ${
        selected ? 'border-accent-dim bg-surface-2' : 'border-edge bg-surface hover:bg-surface-2/60'
      }`}
    >
      <div className="flex items-start gap-2.5">
        <button
          onClick={(e) => {
            e.stopPropagation();
            patch({ starred: !chat.starred });
          }}
          className={`mt-0.5 text-sm ${chat.starred ? 'text-warn' : 'text-muted hover:text-ink'}`}
          title="star"
        >
          {chat.starred ? '★' : '☆'}
        </button>

        <div className="min-w-0 flex-1">
          <div className="text-ink text-sm font-medium truncate">{chat.display_title}</div>
          {chat.preview && <div className="text-muted text-xs truncate mt-0.5">{chat.preview}</div>}

          <div className="flex flex-wrap items-center gap-1.5 mt-1.5 text-[10.5px] mono text-muted">
            {chat.branch && <span className="px-1.5 py-0.5 rounded border border-edge">{chat.branch}</span>}
            {chat.mode === 'terminal' && (
              <span className="px-1.5 py-0.5 rounded border border-accent-dim text-accent">● term</span>
            )}
            <PrBadges sid={chat.id} prs={chat.prs} repo={chat.pr_repo} />
            {chat.tags.map((t) => (
              <span key={t} className="px-1.5 py-0.5 rounded bg-accent/10 text-accent border border-accent-dim">
                #{t}
              </span>
            ))}
            {chat.repo && <span className="opacity-70">{chat.repo}</span>}
            <span className="opacity-70">
              · {rel(chat.last_active)} · {chat.n_user}↑
            </span>
          </div>

          {editing && (
            <div className="mt-2 flex flex-col gap-1.5" onClick={(e) => e.stopPropagation()}>
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="custom name" className={INPUT} />
              <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="tags, comma, separated" className={INPUT} />
              <textarea value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="description" rows={2} className={INPUT} />
              <div className="flex gap-2">
                <button onClick={save} className="text-[11px] px-2.5 py-1 rounded bg-accent text-canvas font-medium">
                  save
                </button>
                <button onClick={() => setEditing(false)} className={ACTION}>
                  cancel
                </button>
              </div>
            </div>
          )}
        </div>

        <div
          className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={() => openChat({ cwd: chat.cwd ?? undefined, resume: chat.id, title: chat.display_title, mode: chat.mode ?? undefined })}
            className={`${ACTION} text-accent`}
            title={chat.mode === 'terminal' ? 'open / resume (terminal)' : 'open / resume'}
          >
            open
          </button>
          <button onClick={() => setEditing((v) => !v)} className={ACTION} title="rename / tag">
            tag
          </button>
          <button onClick={() => patch({ archived: !chat.archived })} className={ACTION} title="archive">
            {chat.archived ? 'unarc' : 'arc'}
          </button>
          {!chat.hidden && (
            <button onClick={() => patch({ hidden: true })} className={ACTION} title="hide">
              hide
            </button>
          )}
          <button
            onClick={() => confirm('Move this chat to trash?') && a.remove.mutate(chat.id)}
            className={`${ACTION} hover:text-bad hover:border-bad/40`}
            title="delete"
          >
            del
          </button>
        </div>
      </div>
    </div>
  );
}

function TrashList({ ids }: { ids: string[] }) {
  const a = useChatActions();
  if (ids.length === 0) return <div className="text-center py-20 text-muted mono text-sm">trash is empty</div>;
  return (
    <div className="flex flex-col gap-1.5">
      {ids.map((id) => (
        <div key={id} className="flex items-center justify-between rounded-lg border border-edge bg-surface px-3 py-2 text-sm">
          <span className="mono text-muted truncate">{id}</span>
          <button
            onClick={() => a.restore.mutate(id)}
            className="text-xs px-2.5 py-1 rounded border border-edge text-accent hover:bg-accent/10 shrink-0"
          >
            restore
          </button>
        </div>
      ))}
    </div>
  );
}

export function ChatsView({ repoRoot, repoName }: { repoRoot: string; repoName?: string }) {
  void repoRoot;
  const [tab, setTab] = useState<'active' | 'archived' | 'trash'>('active');
  const [scope, setScope] = useState<'repo' | 'all'>('repo');
  const [q, setQ] = useState('');
  const [sel, setSel] = useState(0);
  const searchRef = useRef<HTMLInputElement>(null);

  const chatsQ = useChats({
    repo: scope === 'repo' ? repoName : undefined,
    scope,
    tab: tab === 'trash' ? 'all' : tab,
    q,
  });
  const trashQ = useTrash();
  const a = useChatActions();
  const openChat = useOpenChat();
  const chats = tab === 'trash' ? [] : chatsQ.data ?? [];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) {
        if (e.key === 'Escape') el.blur();
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (tab === 'trash' || chats.length === 0) return;
      if (e.key === 'j') setSel((s) => Math.min(s + 1, chats.length - 1));
      else if (e.key === 'k') setSel((s) => Math.max(s - 1, 0));
      else {
        const c = chats[sel];
        if (!c) return;
        if (e.key === 'o' || e.key === 'Enter') openChat({ cwd: c.cwd ?? undefined, resume: c.id, title: c.display_title, mode: c.mode ?? undefined });
        else if (e.key === 's') a.patch.mutate({ id: c.id, patch: { starred: !c.starred } });
        else if (e.key === 'e') a.patch.mutate({ id: c.id, patch: { archived: !c.archived } });
        else if (e.key === 'x') a.patch.mutate({ id: c.id, patch: { hidden: true } });
        else if (e.key === 'Backspace' || e.key === 'Delete') {
          if (confirm('Move chat to trash?')) a.remove.mutate(c.id);
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [chats, sel, tab, a]);

  const starred = chats.filter((c) => c.starred);
  const rest = chats.filter((c) => !c.starred);

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <div className="flex rounded-md border border-edge overflow-hidden text-sm">
          {(['active', 'archived', 'trash'] as const).map((t) => (
            <button
              key={t}
              onClick={() => {
                setTab(t);
                setSel(0);
              }}
              className={`px-3 py-1.5 ${tab === t ? 'bg-surface-2 text-ink' : 'bg-surface text-muted hover:text-ink'}`}
            >
              {t}
            </button>
          ))}
        </div>
        <button
          onClick={() => setScope((s) => (s === 'repo' ? 'all' : 'repo'))}
          className="text-xs mono px-2.5 py-1.5 rounded-md border border-edge bg-surface text-muted hover:text-ink"
          title="toggle repo scope"
        >
          {scope === 'repo' ? `repo: ${repoName ?? '—'}` : 'all repos'}
        </button>
        <div className="flex-1 min-w-[200px]">
          <input
            ref={searchRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="search title, branch, PR #, tag…   ( / )"
            className="w-full mono text-sm px-3 py-1.5 rounded-md bg-surface border border-edge focus:border-accent outline-none"
          />
        </div>
        <span className="text-xs text-muted mono">
          {tab === 'trash' ? trashQ.data?.length ?? 0 : chats.length}
        </span>
      </div>

      {tab === 'trash' ? (
        <TrashList ids={trashQ.data ?? []} />
      ) : chats.length === 0 ? (
        <div className="text-center py-20 text-muted">
          <div className="mono text-sm">no chats</div>
          <div className="text-xs mt-1">{q ? 'try a different search' : 'start one from a task, or switch scope'}</div>
        </div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {tab === 'active' && starred.length > 0 && (
            <div className="text-[11px] mono text-muted px-1 pt-1">★ starred</div>
          )}
          {starred.map((c) => (
            <ChatRow key={c.id} chat={c} selected={chats.indexOf(c) === sel} onSelect={() => setSel(chats.indexOf(c))} />
          ))}
          {tab === 'active' && starred.length > 0 && rest.length > 0 && (
            <div className="text-[11px] mono text-muted px-1 pt-2">recent</div>
          )}
          {rest.map((c) => (
            <ChatRow key={c.id} chat={c} selected={chats.indexOf(c) === sel} onSelect={() => setSel(chats.indexOf(c))} />
          ))}
        </div>
      )}
    </div>
  );
}
