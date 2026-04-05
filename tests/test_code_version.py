"""Journal active_release attribution."""
import os

from positions.code_version import get_journal_active_release, reset_git_head_cache_for_tests


def test_active_release_has_note(monkeypatch):
    reset_git_head_cache_for_tests()
    monkeypatch.delenv("PR_NUMBER", raising=False)
    monkeypatch.delenv("RELEASE_NOTE", raising=False)
    r = get_journal_active_release()
    assert "note" in r
    assert r["note"] in ("unknown",) or "git=" in r["note"] or r["git_short"]


def test_pr_number_in_note(monkeypatch):
    reset_git_head_cache_for_tests()
    monkeypatch.setenv("PR_NUMBER", "128")
    monkeypatch.delenv("RELEASE_NOTE", raising=False)
    r = get_journal_active_release()
    assert r["pr_number"] == "128"
    assert "pr#128" in r["note"]
