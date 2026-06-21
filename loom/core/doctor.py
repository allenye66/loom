"""Preflight checks — the first thing a new dev runs (`loom doctor`)."""

from __future__ import annotations

import shutil
import subprocess


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _gh_authed() -> bool:
    if not _has("gh"):
        return False
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def run_checks() -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, hint: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "hint": hint})

    add("git", _has("git"))
    add("uv", _has("uv"), "pip install uv")
    add("node", _has("node"))
    bun = _has("bun")
    add("bun", bun, "" if bun else "optional — npm fallback works (npm i -g bun)")
    add("tmux", _has("tmux"), "optional — needed for the in-browser terminal chat (brew install tmux)")
    add("claude CLI", _has("claude"), "needed to open sessions in worktrees")
    gh_ok = _gh_authed()
    add(
        "gh authed",
        gh_ok,
        "" if gh_ok else ("gh auth login" if _has("gh") else "brew install gh && gh auth login — needed for PR status in chats"),
    )

    # Optional, app-specific infra: only matters if your repo's `.loom.yaml` services/tests
    # use it. loom itself never requires Docker — kept here purely as a convenience check.
    docker_ok = _has("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    add("docker running", docker_ok, "optional — only if your repo's .loom.yaml needs it")
    return checks


def all_ok(checks: list[dict]) -> bool:
    # Optional tools don't fail the gate — only loom's own prerequisites do.
    optional = {"bun", "tmux", "gh authed", "docker running"}
    return all(c["ok"] for c in checks if c["name"] not in optional)
