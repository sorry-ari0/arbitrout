"""Tests for insider tracker watchlist auto-refresh."""
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from positions.insider_tracker import InsiderTracker, HIGH_CONVICTION_WATCHLIST


class TestWatchlistRefresh:
    def test_initial_watchlist_loaded(self, tmp_path):
        """InsiderTracker should start with the hardcoded watchlist."""
        tracker = InsiderTracker(data_dir=tmp_path)
        assert len(tracker._conviction_watchlist) >= 8

    def test_watchlist_can_be_updated(self, tmp_path):
        """Calling update_watchlist should update the conviction set."""
        tracker = InsiderTracker(data_dir=tmp_path)
        new_wallets = {
            "0xnew_wallet_1": "NewTrader1",
            "0xnew_wallet_2": "NewTrader2",
        }
        tracker.update_watchlist(new_wallets)
        assert "0xnew_wallet_1" in tracker._conviction_watchlist
        assert "0xnew_wallet_2" in tracker._conviction_watchlist

    def test_watchlist_persists_to_disk(self, tmp_path):
        """Updated watchlist should survive restart."""
        t1 = InsiderTracker(data_dir=tmp_path)
        t1.update_watchlist({"0xpersist": "PersistTrader"})

        t2 = InsiderTracker(data_dir=tmp_path)
        assert "0xpersist" in t2._conviction_watchlist
