"""Tests for eval_logger — universal evaluation logger for hindsight analysis."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from eval_logger import EvalLogger


def test_log_entry(tmp_path):
    """Log an 'entered' opportunity, verify JSONL written correctly."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_001",
        action="entered",
        action_reason="high_score",
        score=85.0,
        spread_pct=3.2,
        expected_value_pct=2.1,
        markets=["polymarket", "kalshi"],
        prices_at_decision={"poly_yes": 0.62, "kalshi_no": 0.35},
        metadata={"volume": 50000},
    )

    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "opportunity"
    assert entry["strategy_type"] == "cross_platform_arb"
    assert entry["opportunity_id"] == "opp_001"
    assert entry["action"] == "entered"
    assert entry["action_reason"] == "high_score"
    assert entry["score"] == 85.0
    assert entry["spread_pct"] == 3.2
    assert entry["expected_value_pct"] == 2.1
    assert entry["markets"] == ["polymarket", "kalshi"]
    assert entry["prices_at_decision"]["poly_yes"] == 0.62
    assert entry["metadata"]["volume"] == 50000
    assert "timestamp" in entry


def test_log_skip(tmp_path):
    """Log a 'skipped' opportunity, verify action_reason saved."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="news_arb",
        opportunity_id="opp_skip_001",
        action="skipped",
        action_reason="low_score",
        reason_detail="Score 22 below threshold 50",
        score=22.0,
    )

    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "opportunity"
    assert entry["action"] == "skipped"
    assert entry["action_reason"] == "low_score"
    assert entry["reason_detail"] == "Score 22 below threshold 50"
    assert entry["score"] == 22.0
    # Optional fields not passed should be absent
    assert "markets" not in entry
    assert "metadata" not in entry


def test_backfill_pnl(tmp_path):
    """Log an opportunity + backfill, verify 2 lines in file."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_bf_001",
        action="skipped",
        action_reason="low_liquidity",
        spread_pct=4.5,
    )

    el.backfill_outcome(
        opportunity_id="opp_bf_001",
        actual_pnl_pct=6.2,
        actual_outcome="YES",
        resolution_date="2026-04-01",
        prices_at_resolution={"poly_yes": 1.0, "kalshi_no": 0.0},
    )

    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2

    opp = json.loads(lines[0])
    bf = json.loads(lines[1])

    assert opp["type"] == "opportunity"
    assert opp["opportunity_id"] == "opp_bf_001"

    assert bf["type"] == "backfill"
    assert bf["opportunity_id"] == "opp_bf_001"
    assert bf["actual_pnl_pct"] == 6.2
    assert bf["actual_outcome"] == "YES"
    assert bf["resolution_date"] == "2026-04-01"
    assert bf["prices_at_resolution"]["poly_yes"] == 1.0


def test_get_summary(tmp_path):
    """Log 2 entries, verify counts by strategy+action."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_s1",
        action="entered",
        action_reason="high_score",
    )
    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_s2",
        action="skipped",
        action_reason="low_score",
    )

    summary = el.get_summary()
    assert "cross_platform_arb" in summary
    assert summary["cross_platform_arb"]["entered"] == 1
    assert summary["cross_platform_arb"]["skipped"] == 1


def test_get_missed_opportunities(tmp_path):
    """Skipped opportunity with positive backfill P&L should appear as missed."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_m1",
        action="skipped",
        action_reason="low_score",
    )
    el.backfill_outcome(
        opportunity_id="opp_m1",
        actual_pnl_pct=5.0,
        actual_outcome="YES",
        resolution_date="2026-04-01",
    )

    missed = el.get_missed_opportunities()
    assert len(missed) == 1
    assert missed[0]["opportunity_id"] == "opp_m1"
    assert missed[0]["actual_pnl_pct"] == 5.0
    assert missed[0]["type"] == "missed_opportunity"


def test_get_calibration(tmp_path):
    """Calibration should track correct skips vs missed by action_reason."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    # Two skips with same reason, one correct (negative P&L), one missed (positive P&L)
    el.log_opportunity(
        strategy_type="news_arb",
        opportunity_id="opp_c1",
        action="skipped",
        action_reason="low_score",
    )
    el.backfill_outcome(
        opportunity_id="opp_c1",
        actual_pnl_pct=-2.0,
        actual_outcome="NO",
        resolution_date="2026-04-01",
    )
    el.log_opportunity(
        strategy_type="news_arb",
        opportunity_id="opp_c2",
        action="skipped",
        action_reason="low_score",
    )
    el.backfill_outcome(
        opportunity_id="opp_c2",
        actual_pnl_pct=8.0,
        actual_outcome="YES",
        resolution_date="2026-04-02",
    )

    cal = el.get_calibration()
    assert "low_score" in cal
    bucket = cal["low_score"]
    assert bucket["total_skips"] == 2
    assert bucket["resolved"] == 2
    assert bucket["correct_skips"] == 1
    assert bucket["missed_opportunities"] == 1
    assert bucket["correct_skip_rate"] == 0.5


def test_get_details(tmp_path):
    """get_details should merge opportunity + backfill for a given ID."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_d1",
        action="entered",
        action_reason="high_score",
        score=90.0,
    )
    el.backfill_outcome(
        opportunity_id="opp_d1",
        actual_pnl_pct=4.5,
        actual_outcome="YES",
        resolution_date="2026-04-05",
    )

    details = el.get_details("opp_d1")
    assert details is not None
    assert details["opportunity_id"] == "opp_d1"
    assert details["score"] == 90.0
    assert details["actual_pnl_pct"] == 4.5
    assert details["type"] == "opportunity"

    # Non-existent ID returns None
    assert el.get_details("opp_nonexistent") is None


def test_get_unresolved_skips(tmp_path):
    """Skipped entries without backfill should appear as unresolved."""
    log_file = tmp_path / "eval_log.jsonl"
    el = EvalLogger(path=str(log_file))

    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_u1",
        action="skipped",
        action_reason="low_liquidity",
    )
    el.log_opportunity(
        strategy_type="cross_platform_arb",
        opportunity_id="opp_u2",
        action="skipped",
        action_reason="low_score",
    )
    # Backfill only opp_u2
    el.backfill_outcome(
        opportunity_id="opp_u2",
        actual_pnl_pct=3.0,
        actual_outcome="YES",
        resolution_date="2026-04-01",
    )

    unresolved = el.get_unresolved_skips()
    assert len(unresolved) == 1
    assert unresolved[0]["opportunity_id"] == "opp_u1"


class TestLLMEstimateLogging:
    def test_log_llm_estimate(self, tmp_path):
        from eval_logger import EvalLogger
        import json
        path = str(tmp_path / "eval.jsonl")
        el = EvalLogger(path=path)
        el.log_llm_estimate(
            market_id="test-123",
            title="Will BTC exceed $100K?",
            claude_prob=0.62,
            gemini_prob=0.58,
            consensus_prob=0.60,
            market_price=0.50,
            edge_pct=10.0,
            should_boost=True,
        )
        with open(path) as f:
            line = f.readline()
            data = json.loads(line)
            assert data["type"] == "llm_estimate"
            assert data["market_id"] == "test-123"
            assert data["claude_prob"] == 0.62
            assert data["gemini_prob"] == 0.58
            assert data["consensus_prob"] == 0.60
            assert data["should_boost"] is True
            assert "timestamp" in data
