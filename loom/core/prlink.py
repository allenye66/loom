"""Which PRs does a chat own, and which loom chat wrote a PR? (both directions)

An agent that has to build on, review or debug someone else's PR often needs the *why* the diff and
the PR description don't carry. The agent that wrote it already has that context, so this finds it.
A chat is tied to a PR by the strongest evidence available:

  created  the chat's own transcript shows it opening the PR (agent-side PR sightings that fall inside
           its tool calls, confirmed against GitHub's createdAt — see sessions._PrEvidence / D19)
  scouted  loom's PR scout found it (the chat's branch has a PR on GitHub, or the scout read the
           conversation and GitHub confirmed — core/prscout.py, D29)
  pinned   the user attached the PR to the chat (a PR chip they added)
  branch   the PR's head branch is the chat's task branch (computed here, at lookup time)

Only loom task chats are considered (those are the ones the sidebar shows). Read-only; the `gh` calls
are the same cached ones the PR chips use, and it never raises (no `gh` → just the transcript evidence).
"""

from __future__ import annotations

from loom.core import github, sessions

_RANK = {"created": 0, "scouted": 1, "pinned": 2, "branch": 3}


def chat_pr_keys(raw_row: dict | None, ov: dict, default_repo: str | None = None) -> dict[str, str]:
    """{"owner/repo#n": evidence} for the PRs one chat owns by created / scouted / pinned evidence
    (the strongest wins; ones the user removed are dropped). `raw_row` is the chat's index row."""
    raw = raw_row or {}
    out: dict[str, str] = {}
    for k in ov.get("prs_pinned") or []:
        out[k] = "pinned"
    legacy = ov.get("pr")  # a single bare PR number the user attached long ago
    repo = raw.get("pr_repo") or default_repo
    if legacy and repo:
        out.setdefault(f"{repo}#{int(legacy)}", "pinned")
    for k in ov.get("prs_scouted") or []:
        out[k] = "scouted"
    seen = raw.get("pr_seen") or {}
    if seen:
        for k in github.created_prs(seen, raw.get("tool_windows") or []):
            out[k] = "created"
    for k in ov.get("prs_hidden") or []:
        out.pop(k, None)
    return out


def chats_for_pr(repo: str, number: int) -> dict:
    """{"pr": {...status incl. head `branch`}, "chats": [{id, name, project, branch, archived, agent,
    evidence}]} for the loom chats tied to `repo#number`, strongest evidence first."""
    key = f"{repo}#{number}"
    pr = github.pr_status(repo, number)
    head = pr.get("branch")
    raw = {s["id"]: s for s in sessions.build_index()}  # carries pr_seen / tool_windows (list rows don't)
    found: dict[str, dict] = {}
    for c in sessions.list_chats(scope="all", tab="all"):
        if not c.get("task") or c.get("deleted"):
            continue
        ov = sessions.get_overlay(c["id"])
        if key in (ov.get("prs_hidden") or []):
            continue  # the user removed this PR from the chat
        evidence = chat_pr_keys(raw.get(c["id"]), ov).get(key)
        if evidence is None and head and c.get("branch") == head and (github.repo_for_dir(c.get("cwd")) or "").lower() == repo.lower():
            evidence = "branch"
        if evidence:
            found[c["id"]] = {
                "id": c["id"], "name": c.get("name") or c.get("display_title") or c["id"][:8],
                "project": c.get("repo"), "branch": c.get("branch"), "archived": bool(c.get("archived")),
                "agent": c.get("agent"), "evidence": evidence,
            }
    chats = sorted(found.values(), key=lambda x: (_RANK[x["evidence"]], x["archived"], x["name"]))
    return {"pr": pr, "chats": chats}
