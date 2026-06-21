"""Tiny registry of known repos (name -> root) so the dashboard can list them."""

from __future__ import annotations

import json

from loom.core.config import LOOM_HOME, ensure_dirs, load_repo_config

REPOS_PATH = LOOM_HOME / "repos.json"


def _read() -> dict[str, dict]:
    if REPOS_PATH.exists():
        try:
            return json.loads(REPOS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write(d: dict[str, dict]) -> None:
    ensure_dirs()
    REPOS_PATH.write_text(json.dumps(d, indent=2, sort_keys=True))


def register(root: str) -> dict:
    cfg = load_repo_config(root)  # validates the .loom.yaml
    d = _read()
    d[cfg.name] = {"name": cfg.name, "root": cfg.root, "base_branch": cfg.base_branch}
    _write(d)
    return d[cfg.name]


def list_repos() -> list[dict]:
    return list(_read().values())
