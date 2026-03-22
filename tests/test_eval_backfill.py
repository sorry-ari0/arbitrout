"""Tests for eval logger backfill — resolving skipped opportunities."""
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from eval_logger import EvalLogger


class TestResolveOpportunity:
    def test_resolve_writes_backfill_entry(self):
        """resolve_opportunity should append a backfill entry to the log."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            logger.log_opportunity(
                strategy_type="pure_prediction",
                opportunity_id="opp_btc_100k",
                action="skipped",
                action_reason="low_score",
                markets=[{"condition_id": "0xabc123", "platform": "polymarket"}],
                prices_at_decision={"yes": 0.35, "no": 0.65},
            )
            logger.backfill_outcome(
                opportunity_id="opp_btc_100k",
                actual_pnl_pct=15.0,
                actual_outcome="win",
                resolution_date="2026-03-22",
                prices_at_resolution={"yes": 1.0, "no": 0.0},
            )
            entries = logger._read_all()
            backfills = [e for e in entries if e.get("type") == "backfill"]
            assert len(backfills) == 1
            assert backfills[0]["opportunity_id"] == "opp_btc_100k"
            assert backfills[0]["actual_pnl_pct"] == 15.0
            assert backfills[0]["actual_outcome"] == "win"

    def test_missed_opportunity_detected_after_backfill(self):
        """After backfill, get_missed_opportunities should find profitable skips."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            logger.log_opportunity(
                strategy_type="pure_prediction",
                opportunity_id="opp_missed",
                action="skipped",
                action_reason="low_score",
            )
            logger.backfill_outcome(
                opportunity_id="opp_missed",
                actual_pnl_pct=25.0,
                actual_outcome="win",
                resolution_date="2026-03-22",
            )
            missed = logger.get_missed_opportunities()
            assert len(missed) == 1
            assert missed[0]["opportunity_id"] == "opp_missed"
            assert missed[0]["actual_pnl_pct"] == 25.0

    def test_correct_skip_not_in_missed(self):
        """Skips that resolved at a loss should NOT appear in missed."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            logger.log_opportunity(
                strategy_type="pure_prediction",
                opportunity_id="opp_good_skip",
                action="skipped",
                action_reason="low_score",
            )
            logger.backfill_outcome(
                opportunity_id="opp_good_skip",
                actual_pnl_pct=-30.0,
                actual_outcome="loss",
                resolution_date="2026-03-22",
            )
            missed = logger.get_missed_opportunities()
            assert len(missed) == 0

    def test_calibration_with_backfills(self):
        """Calibration should reflect backfilled data."""
        with tempfile.TemporaryDirectory() as tmp:
            logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
            logger.log_opportunity("pure_prediction", "opp_1", "skipped", "low_score")
            logger.log_opportunity("pure_prediction", "opp_2", "skipped", "low_score")
            logger.backfill_outcome("opp_1", -10.0, "loss", "2026-03-22")
            logger.backfill_outcome("opp_2", 20.0, "win", "2026-03-22")
            cal = logger.get_calibration()
            assert "low_score" in cal
            assert cal["low_score"]["resolved"] == 2
            assert cal["low_score"]["correct_skips"] == 1
            assert cal["low_score"]["missed_opportunities"] == 1
            assert cal["low_score"]["correct_skip_rate"] == 0.5


@pytest.mark.asyncio
async def test_resolve_via_polymarket_closed_market():
    """resolve_via_polymarket should backfill when market is closed."""
    with tempfile.TemporaryDirectory() as tmp:
        logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
        logger.log_opportunity(
            strategy_type="pure_prediction",
            opportunity_id="opp_resolved",
            action="skipped",
            action_reason="low_score",
            markets=[{"condition_id": "0xabc", "platform": "polymarket"}],
            prices_at_decision={"yes": 0.30, "no": 0.70},
        )
        entry = logger.get_unresolved_skips()[0]
        mock_client = AsyncMock()
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: [{"closed": True, "outcomePrices": "[1.0, 0.0]", "endDate": "2026-03-22"}],
        )
        result = await logger.resolve_via_polymarket(entry, mock_client)
        assert result is True
        missed = logger.get_missed_opportunities()
        assert len(missed) == 1
        assert missed[0]["actual_outcome"] == "win"


@pytest.mark.asyncio
async def test_resolve_via_polymarket_still_open():
    """resolve_via_polymarket should return False for open markets."""
    with tempfile.TemporaryDirectory() as tmp:
        logger = EvalLogger(path=os.path.join(tmp, "eval.jsonl"))
        logger.log_opportunity(
            strategy_type="pure_prediction",
            opportunity_id="opp_open",
            action="skipped",
            action_reason="low_score",
            markets=[{"condition_id": "0xdef", "platform": "polymarket"}],
        )
        entry = logger.get_unresolved_skips()[0]
        mock_client = AsyncMock()
        mock_client.get.return_value = AsyncMock(
            status_code=200,
            json=lambda: [{"closed": False}],
        )
        result = await logger.resolve_via_polymarket(entry, mock_client)
        assert result is False
        assert len(logger.get_unresolved_skips()) == 1
