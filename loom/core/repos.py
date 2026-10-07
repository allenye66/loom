"""Tiny registry of known repos (name -> root) so the dashboard can list them."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def _git_toplevel(root: str) -> str:
    p = Path(root).expanduser()
    if not p.is_dir():
        raise ValueError(f"no such directory: {root}")
    r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise ValueError(f"not a git repository: {p}")
    return r.stdout.strip()


def register(root: str) -> dict:
    """Add (or refresh) a project: any git repo, registered at its top level. A `.loom.yaml`
    is optional (see load_repo_config). The name is the dashboard's project key, so a
    different repo with the same name is refused rather than silently replacing the first."""
    cfg = load_repo_config(_git_toplevel(root))
    d = _read()
    existing = d.get(cfg.name)
    if existing and existing["root"] != cfg.root:
        raise ValueError(
            f"a project named {cfg.name!r} is already registered at {existing['root']} — "
            "give this one a different `name:` in its .loom.yaml"
        )
    d[cfg.name] = {"name": cfg.name, "root": cfg.root, "base_branch": cfg.base_branch}
    _write(d)
    return d[cfg.name]


def list_repos() -> list[dict]:
    return list(_read().values())
