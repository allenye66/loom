"""`@name` chat mentions — shared by the server (resolving them for an agent) and the CLI.

A user message can refer to another loom chat as `@some-chat-name` (the dashboard's `@@`
picker types it for you). The token is the chat's name, slugified so it has no spaces; when two
chats slug the same, the picker adds `~<id prefix>` (`@design~3693`). Resolution is exact slug,
then id prefix, then a unique slug prefix — and only chats that really exist match, so an
`@README.md` file mention (claude's own use of `@`) is left alone.
"""

from __future__ import annotations

import re

# `@token`, not glued to a word / path / email / another @. The token may carry `.`, `-`, `_`, `~`.
MENTION_RE = re.compile(r"(?<![\w@/.\-])@([A-Za-z0-9][\w.\-~]*)")
_TRAIL = ".,:;!?)]}'\""


def slugify(name: str) -> str:
    """Lowercase, runs of non-alphanumerics → a single `-`, trimmed. 'Fix: Bug #3!' → 'fix-bug-3'."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def mention_tokens(text: str) -> list[str]:
    """The distinct `@tokens` in a message (trailing punctuation dropped), in first-seen order."""
    seen: list[str] = []
    for m in MENTION_RE.finditer(text or ""):
        tok = m.group(1).rstrip(_TRAIL)
        if tok and tok not in seen:
            seen.append(tok)
    return seen


def resolve_token(token: str, chats: list[dict], *, prefer_category: str | None = None) -> dict | None:
    """The chat a mention token names, or None. `chats` are loom chat rows (id, name/display_title,
    category, last_active). Ties go to the chat in `prefer_category`, then the most recently active."""
    tok = token.lower()
    slug, _, idpart = tok.partition("~")

    def title(c: dict) -> str:
        return c.get("name") or c.get("display_title") or c["id"][:8]

    def best(cands: list[dict]) -> dict | None:
        if not cands:
            return None
        if idpart:
            cands = [c for c in cands if c["id"].startswith(idpart)] or cands
        cands = sorted(
            cands,
            key=lambda c: (c.get("category") != prefer_category if prefer_category else False, -(c.get("last_active") or 0)),
        )
        return cands[0]

    exact = [c for c in chats if slugify(title(c)) == slug]
    if exact:
        return best(exact)
    if len(tok) >= 6 and re.fullmatch(r"[0-9a-f\-]+", tok):  # a bare id / id prefix
        hit = [c for c in chats if c["id"].startswith(tok)]
        if len(hit) == 1:
            return hit[0]
    if len(slug) >= 3:  # unique prefix: @design → design-b, if nothing else starts with "design"
        pre = [c for c in chats if slugify(title(c)).startswith(slug)]
        if len({slugify(title(c)) for c in pre}) == 1:
            return best(pre)
    return None
