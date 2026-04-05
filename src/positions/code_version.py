"""Runtime release identity for journal attribution (git + optional CI PR/tag)."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger("positions.code_version")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_HEAD_CACHE: str | None | bool = False  # False = unset, None = lookup failed


def _git_head_full() -> str | None:
    global _GIT_HEAD_CACHE
    if _GIT_HEAD_CACHE is not False:
        return _GIT_HEAD_CACHE  # type: ignore[return-value]
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        _GIT_HEAD_CACHE = sha or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        logger.debug("git rev-parse HEAD unavailable: %s", e)
        _GIT_HEAD_CACHE = None
    return _GIT_HEAD_CACHE


def get_journal_active_release() -> dict:
    """Snapshot of code identity at journal close time.

    Set in CI/deploy:
      PR_NUMBER or GITHUB_PR — merged PR number
      IMAGE_TAG / DOCKER_TAG — container tag
      RELEASE_NOTE — optional free-form override (prepended to auto note)
    """
    sha = _git_head_full()
    short = sha[:12] if sha else None
    pr = os.environ.get("PR_NUMBER") or os.environ.get("GITHUB_PR") or ""
    pr = pr.strip() or None
    image = (
        os.environ.get("IMAGE_TAG")
        or os.environ.get("DOCKER_TAG")
        or os.environ.get("K_REVISION")
        or ""
    ).strip() or None
    branch = os.environ.get("GITHUB_REF_NAME", "").strip() or None

    parts: list[str] = []
    override = os.environ.get("RELEASE_NOTE", "").strip()
    if pr:
        parts.append(f"pr#{pr}")
    if short:
        parts.append(f"git={short}")
    if branch:
        parts.append(f"branch={branch}")
    if image:
        parts.append(f"image={image}")
    auto_note = " ".join(parts) if parts else None
    if override and auto_note:
        note = f"{override} | {auto_note}"
    elif override:
        note = override
    elif auto_note:
        note = auto_note
    else:
        note = "unknown"

    return {
        "git_sha": sha,
        "git_short": short,
        "pr_number": pr,
        "branch": branch,
        "image_tag": image,
        "note": note,
    }


def reset_git_head_cache_for_tests() -> None:
    """Test hook only."""
    global _GIT_HEAD_CACHE
    _GIT_HEAD_CACHE = False
