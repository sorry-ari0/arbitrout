# Political Synthetic Analysis & System-Wide Hindsight Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add AI-driven political synthetic derivative analysis (2-4 leg positions from related political contracts) and a universal eval logging system for hindsight analysis across all Arbitrout strategies.

**Architecture:** A `src/political/` package handles classification, clustering, relationship detection, LLM strategy generation, and caching. A separate `src/eval_logger.py` provides universal opportunity logging across all strategy types. Both integrate into the existing server lifecycle, auto trader, and exit engine.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest, dataclasses, re (regex), hashlib, json

**Spec:** `docs/specs/2026-03-19-political-synthetic-analysis-design.md`

**Out of scope:** Dashboard UI (political strategy cards, eval tab). This plan covers backend only. Frontend will be a separate plan.

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/political/__init__.py` | Package init, re-exports key classes |
| `src/political/models.py` | All political dataclasses: PoliticalContractInfo, PoliticalCluster, SyntheticLeg, Scenario, PoliticalSyntheticStrategy, PoliticalOpportunity, PoliticalLeg |
| `src/political/classifier.py` | Regex-based contract type extraction from NormalizedEvent titles |
| `src/political/clustering.py` | Group classified contracts into PoliticalClusters by race+state |
| `src/political/relationships.py` | Detect 6 relationship types between contracts, score pairs, greedy leg extension |
| `src/political/strategy.py` | LLM prompt building, response parsing, post-LLM validation |
| `src/political/cache.py` | SHA-256 keyed LRU cache with 15-min TTL and price-shift invalidation |
| `src/political/analyzer.py` | Main PoliticalAnalyzer orchestrator with 15-min asyncio loop |
| `src/political/router.py` | FastAPI router for `/api/political/*` endpoints |
| `src/eval_logger.py` | Universal EvalEntry dataclass, JSONL logger, hourly backfill task |
| `src/eval_router.py` | FastAPI router for `/api/eval/*` endpoints |
| `tests/test_political_classifier.py` | Contract classification tests |
| `tests/test_political_clustering.py` | Clustering tests |
| `tests/test_political_relationships.py` | Relationship detection tests |
| `tests/test_political_strategy.py` | LLM prompt/response/validation tests |
| `tests/test_political_cache.py` | Cache TTL, LRU, invalidation tests |
| `tests/test_eval_logger.py` | Eval logging + backfill tests |

### Modified Files

| File | Change |
|------|--------|
| `src/positions/position_manager.py:16` | Add `"political_synthetic"` to `STRATEGY_TYPES` |
| `src/positions/exit_engine.py:46-47` | Add trigger #21 `T_POLITICAL_EVENT_RESOLVED = 21`, extend strategy checks at lines 110, 123, 133 |
| `src/positions/auto_trader.py:152-198` | Add political opportunity consumption path |
| `src/positions/decision_log.py` | Add `log_political_analysis()` method |
| `src/server.py:172-333` | Wire PoliticalAnalyzer, eval logger, new routers |

---

## Chunk 1: Data Models & Contract Classifier

### Task 1: Political Data Models

**Files:**
- Create: `src/political/__init__.py`
- Create: `src/political/models.py`

- [ ] **Step 1: Create package init**

```python
# src/political/__init__.py
"""Political synthetic derivative analysis package."""
```

- [ ] **Step 2: Write data model tests**

Create `tests/test_political_classifier.py` with model import tests:

```python
"""Tests for political data models and classifier."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from political.models import (
    PoliticalContractInfo, PoliticalCluster,
    SyntheticLeg, Scenario, PoliticalSyntheticStrategy,
    PoliticalOpportunity, PoliticalLeg,
)
from adapters.models import NormalizedEvent


def _make_event(title="Test", platform="polymarket", event_id="ev1",
                category="politics", yes=0.50, no=0.50):
    return NormalizedEvent(
        platform=platform, event_id=event_id, title=title,
        category=category, yes_price=yes, no_price=no,
        volume=1000, expiry="2026-11-03", url=f"https://{platform}.com/{event_id}",
    )


class TestPoliticalModels:
    def test_political_contract_info_creation(self):
        ev = _make_event("Talarico wins TX Senate")
        info = PoliticalContractInfo(
            event=ev, contract_type="candidate_win",
            candidates=["Talarico"], party="dem",
            race="TX Senate", state="TX",
            threshold=None, direction=None,
        )
        assert info.contract_type == "candidate_win"
        assert info.candidates == ["Talarico"]
        assert info.event.platform == "polymarket"

    def test_political_cluster_creation(self):
        ev1 = _make_event("Talarico wins TX Senate", event_id="ev1")
        ev2 = _make_event("Cruz wins TX Senate", event_id="ev2")
        info1 = PoliticalContractInfo(event=ev1, contract_type="candidate_win",
            candidates=["Talarico"], party="dem", race="TX Senate", state="TX",
            threshold=None, direction=None)
        info2 = PoliticalContractInfo(event=ev2, contract_type="candidate_win",
            candidates=["Cruz"], party="gop", race="TX Senate", state="TX",
            threshold=None, direction=None)
        cluster = PoliticalCluster(
            cluster_id="tx-senate-2026", race="TX Senate", state="TX",
            contracts=[info1, info2], matched_events=["match1"],
        )
        assert len(cluster.contracts) == 2
        assert cluster.race == "TX Senate"

    def test_political_opportunity_to_dict(self):
        ev = _make_event("Talarico wins TX Senate", yes=0.62, no=0.38)
        info = PoliticalContractInfo(event=ev, contract_type="candidate_win",
            candidates=["Talarico"], party="dem", race="TX Senate", state="TX",
            threshold=None, direction=None)
        leg = PoliticalLeg(
            event=ev, contract_info=info, side="YES",
            weight=0.5, platform_fee_pct=2.0,
        )
        strategy = PoliticalSyntheticStrategy(
            cluster_id="tx-senate-2026",
            strategy_name="TX Senate Mispriced Dem Link",
            legs=[SyntheticLeg(contract_idx=1, event_id="ev1", side="YES", weight=0.5)],
            scenarios=[Scenario(outcome="Talarico wins", probability=0.6, pnl_pct=12.5)],
            expected_value_pct=8.2, win_probability=0.65,
            max_loss_pct=-45.0, confidence="high",
            reasoning="Price gap between candidate and party contracts",
        )
        opp = PoliticalOpportunity(
            cluster_id="tx-senate-2026", strategy=strategy,
            legs=[leg], total_fee_pct=2.0,
            net_expected_value_pct=6.2, platforms=["polymarket"],
            created_at="2026-03-19T12:00:00Z",
        )
        d = opp.to_dict()
        assert d["opportunity_type"] == "political_synthetic"
        assert d["cluster_id"] == "tx-senate-2026"
        assert d["net_expected_value_pct"] == 6.2
        assert len(d["legs"]) == 1
        assert d["strategy"]["strategy_name"] == "TX Senate Mispriced Dem Link"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_classifier.py::TestPoliticalModels -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'political'`

- [ ] **Step 4: Implement models**

```python
# src/political/models.py
"""Political synthetic derivative data structures."""
from dataclasses import dataclass, field
from adapters.models import NormalizedEvent


@dataclass
class PoliticalContractInfo:
    """A NormalizedEvent tagged with political contract classification."""
    event: NormalizedEvent
    contract_type: str          # candidate_win, party_outcome, margin_bracket, vote_share, matchup, yes_no_binary
    candidates: list[str]       # extracted candidate names
    party: str | None           # dem, gop, etc.
    race: str | None            # "TX Senate", "President", etc.
    state: str | None           # state abbreviation
    threshold: float | None     # for margin/vote_share brackets
    direction: str | None       # "above", "below", "between"


@dataclass
class PoliticalCluster:
    """Group of related political contracts about the same race/topic."""
    cluster_id: str
    race: str
    state: str | None
    contracts: list[PoliticalContractInfo] = field(default_factory=list)
    matched_events: list[str] = field(default_factory=list)


@dataclass
class SyntheticLeg:
    """One leg of a synthetic strategy as recommended by the LLM."""
    contract_idx: int       # 1-based index from prompt
    event_id: str           # resolved after LLM response
    side: str               # "YES" or "NO"
    weight: float           # allocation weight (0.0-1.0)


@dataclass
class Scenario:
    """One possible outcome scenario with probability and P&L."""
    outcome: str
    probability: float
    pnl_pct: float


@dataclass
class PoliticalSyntheticStrategy:
    """LLM-recommended synthetic position for a political cluster."""
    cluster_id: str
    strategy_name: str
    legs: list[SyntheticLeg]
    scenarios: list[Scenario]
    expected_value_pct: float
    win_probability: float
    max_loss_pct: float
    confidence: str             # high, medium, low
    reasoning: str


@dataclass
class PoliticalLeg:
    """A fully resolved leg with event data and fee info."""
    event: NormalizedEvent
    contract_info: PoliticalContractInfo
    side: str                   # "YES" or "NO"
    weight: float               # allocation (0.0-1.0)
    platform_fee_pct: float     # round-trip fee for this platform


PLATFORM_FEES = {
    "polymarket": 2.0,
    "kalshi": 1.5,
    "predictit": 10.0,
    "limitless": 2.0,
}


@dataclass
class PoliticalOpportunity:
    """A scored political synthetic opportunity ready for auto trader."""
    cluster_id: str
    strategy: PoliticalSyntheticStrategy
    legs: list[PoliticalLeg]
    total_fee_pct: float
    net_expected_value_pct: float
    platforms: list[str]
    created_at: str

    def to_dict(self) -> dict:
        """Convert to dict format compatible with auto trader."""
        return {
            "opportunity_type": "political_synthetic",
            "cluster_id": self.cluster_id,
            "title": self.strategy.strategy_name,
            "canonical_title": self.strategy.strategy_name,
            "strategy": {
                "strategy_name": self.strategy.strategy_name,
                "expected_value_pct": self.strategy.expected_value_pct,
                "win_probability": self.strategy.win_probability,
                "max_loss_pct": self.strategy.max_loss_pct,
                "confidence": self.strategy.confidence,
                "reasoning": self.strategy.reasoning,
                "scenarios": [
                    {"outcome": s.outcome, "probability": s.probability, "pnl_pct": s.pnl_pct}
                    for s in self.strategy.scenarios
                ],
            },
            "legs": [
                {
                    "event_id": leg.event.event_id,
                    "platform": leg.event.platform,
                    "title": leg.event.title,
                    "side": leg.side,
                    "weight": leg.weight,
                    "yes_price": leg.event.yes_price,
                    "no_price": leg.event.no_price,
                    "platform_fee_pct": leg.platform_fee_pct,
                }
                for leg in self.legs
            ],
            "total_fee_pct": self.total_fee_pct,
            "net_expected_value_pct": self.net_expected_value_pct,
            "platforms": self.platforms,
            "profit_pct": self.net_expected_value_pct,  # auto trader reads this field
            "buy_yes_price": self.legs[0].event.yes_price if self.legs else 0,
            "buy_no_price": self.legs[0].event.no_price if self.legs else 0,
            "buy_yes_platform": self.legs[0].event.platform if self.legs else "",
            "buy_no_platform": self.legs[-1].event.platform if len(self.legs) > 1 else "",
            "buy_yes_market_id": self.legs[0].event.event_id if self.legs else "",
            "buy_no_market_id": self.legs[-1].event.event_id if len(self.legs) > 1 else "",
            "volume": sum(l.event.volume for l in self.legs),
            "expiry": self.legs[0].event.expiry if self.legs else "",
            "is_synthetic": True,
            "created_at": self.created_at,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_classifier.py::TestPoliticalModels -v`
Expected: PASS (all 3 tests)

- [ ] **Step 6: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/__init__.py src/political/models.py tests/test_political_classifier.py
git commit -m "feat(political): add data models for political synthetic derivatives"
```

---

### Task 2: Contract Classifier

**Files:**
- Create: `src/political/classifier.py`
- Modify: `tests/test_political_classifier.py`

- [ ] **Step 1: Write classifier tests**

Add to `tests/test_political_classifier.py`:

```python
from political.classifier import classify_contract


class TestClassifier:
    """Test contract type classification from event titles."""

    # --- candidate_win ---
    @pytest.mark.parametrize("title,expected_candidate,expected_race", [
        ("Talarico wins TX Senate", "Talarico", "TX Senate"),
        ("Ted Cruz to win Texas Senate race", "Ted Cruz", "Texas Senate race"),
        ("Will Kamala Harris win the Presidency?", "Kamala Harris", "Presidency"),
        ("Donald Trump winning the 2028 Presidential Election", "Donald Trump", "2028 Presidential Election"),
    ])
    def test_candidate_win(self, title, expected_candidate, expected_race):
        ev = _make_event(title)
        info = classify_contract(ev)
        assert info.contract_type == "candidate_win"
        assert expected_candidate in info.candidates
        assert info.race is not None

    # --- party_outcome ---
    @pytest.mark.parametrize("title,expected_party", [
        ("Democratic candidate wins TX Senate", "dem"),
        ("Republican wins the Presidency", "gop"),
        ("GOP holds Georgia Senate seat", "gop"),
        ("Will a Democrat win Florida Governor?", "dem"),
    ])
    def test_party_outcome(self, title, expected_party):
        ev = _make_event(title)
        info = classify_contract(ev)
        assert info.contract_type == "party_outcome"
        assert info.party == expected_party

    # --- margin_bracket ---
    @pytest.mark.parametrize("title,expected_threshold", [
        ("Talarico wins by >5%", 5.0),
        ("Winner margin greater than 10%", 10.0),
        ("Victory margin >2.5%", 2.5),
    ])
    def test_margin_bracket(self, title, expected_threshold):
        ev = _make_event(title)
        info = classify_contract(ev)
        assert info.contract_type == "margin_bracket"
        assert info.threshold == expected_threshold

    # --- vote_share ---
    @pytest.mark.parametrize("title", [
        "Democrat gets >48% in TX Senate",
        "Republican vote share above 52%",
    ])
    def test_vote_share(self, title):
        ev = _make_event(title)
        info = classify_contract(ev)
        assert info.contract_type == "vote_share"
        assert info.threshold is not None

    # --- matchup ---
    def test_matchup(self):
        ev = _make_event("Talarico vs Cruz")
        info = classify_contract(ev)
        assert info.contract_type == "matchup"
        assert len(info.candidates) == 2

    # --- yes_no_binary fallback ---
    def test_yes_no_binary_fallback(self):
        ev = _make_event("Will there be a government shutdown in 2026?")
        info = classify_contract(ev)
        assert info.contract_type == "yes_no_binary"

    # --- non-political events should still classify ---
    def test_non_political_returns_yes_no_binary(self):
        ev = _make_event("Will Bitcoin reach $200K?", category="crypto")
        info = classify_contract(ev)
        assert info.contract_type == "yes_no_binary"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_classifier.py::TestClassifier -v`
Expected: FAIL with `ImportError: cannot import name 'classify_contract'`

- [ ] **Step 3: Implement classifier**

```python
# src/political/classifier.py
"""Rule-based political contract classifier.

Extracts contract type, candidates, party, race, state, threshold, direction
from prediction market event titles using regex patterns.
"""
import re
from adapters.models import NormalizedEvent
from political.models import PoliticalContractInfo

# US state abbreviations for extraction
_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}

# Party normalization
_PARTY_MAP = {
    "democrat": "dem", "democratic": "dem", "dem": "dem", "dems": "dem",
    "republican": "gop", "gop": "gop", "rep": "gop",
}

# --- Regex patterns (order matters: more specific first) ---

# margin_bracket: "wins by >5%", "margin greater than 10%", "victory margin >2.5%"
_MARGIN_RE = re.compile(
    r"(?:wins?\s+by|margin|victory\s+margin)\s*(?:greater\s+than|>|of\s+more\s+than)?\s*"
    r"(?P<threshold>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# vote_share: "gets >48%", "vote share above 52%"
_VOTE_SHARE_RE = re.compile(
    r"(?:gets?|vote\s+share|share)\s*(?:above|>|over|greater\s+than)?\s*"
    r"(?P<threshold>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# matchup: "Name vs Name" or "Name versus Name"
_MATCHUP_RE = re.compile(
    r"(?P<c1>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:vs\.?|versus)\s+(?P<c2>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
    re.IGNORECASE,
)

# party_outcome: "Democratic candidate wins TX Senate", "GOP holds Georgia"
_PARTY_RE = re.compile(
    r"(?:will\s+)?(?:a\s+)?(?P<party>Democrat(?:ic)?|Republican|GOP|Dem|Rep)\s+"
    r"(?:candidate\s+)?(?:wins?|holds?|takes?|win)\s+(?P<race>.+)",
    re.IGNORECASE,
)

# candidate_win: "Name wins/to win/winning Race"
_CANDIDATE_WIN_RE = re.compile(
    r"(?:will\s+)?(?P<candidate>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\s+"
    r"(?:wins?|to\s+win|winning|win)\s+(?:the\s+)?(?P<race>.+)",
    re.IGNORECASE,
)


def _extract_state(text: str) -> str | None:
    """Extract US state abbreviation from text."""
    # Check for state abbreviations (2-letter uppercase)
    for word in text.split():
        clean = word.strip(",.;:!?()").upper()
        if clean in _STATE_ABBREVS:
            return clean
    # Check for full state names
    lower = text.lower()
    for name, abbrev in _STATE_NAMES.items():
        if name in lower:
            return abbrev
    return None


def _extract_party(text: str) -> str | None:
    """Extract party from text."""
    lower = text.lower()
    for keyword, party in _PARTY_MAP.items():
        if keyword in lower:
            return party
    return None


def _clean_race(race_str: str) -> str:
    """Clean up extracted race string."""
    # Remove trailing punctuation, "race", "election", "?", "in 2026"
    race = re.sub(r"\s*(?:race|election|\?)\s*$", "", race_str, flags=re.IGNORECASE).strip()
    race = re.sub(r"\s+in\s+\d{4}\s*$", "", race).strip()
    race = race.rstrip("?.,;: ")
    return race


def classify_contract(event: NormalizedEvent) -> PoliticalContractInfo:
    """Classify a NormalizedEvent into a political contract type.

    Returns PoliticalContractInfo with extracted parameters.
    Falls back to 'yes_no_binary' for unclassifiable titles.
    """
    title = event.title
    state = _extract_state(title)

    # 1. margin_bracket (must check before candidate_win — "wins by >5%" contains "wins")
    m = _MARGIN_RE.search(title)
    if m:
        threshold = float(m.group("threshold"))
        # Try to extract candidate name before "wins by"
        prefix = title[:m.start()].strip()
        candidates = []
        cand_match = re.match(r"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", prefix)
        if cand_match:
            candidates = [cand_match.group("name")]
        return PoliticalContractInfo(
            event=event, contract_type="margin_bracket",
            candidates=candidates, party=_extract_party(title),
            race=None, state=state,
            threshold=threshold, direction="above",
        )

    # 2. vote_share
    m = _VOTE_SHARE_RE.search(title)
    if m:
        threshold = float(m.group("threshold"))
        return PoliticalContractInfo(
            event=event, contract_type="vote_share",
            candidates=[], party=_extract_party(title),
            race=None, state=state,
            threshold=threshold, direction="above",
        )

    # 3. matchup
    m = _MATCHUP_RE.search(title)
    if m:
        candidates = [m.group("c1").strip(), m.group("c2").strip()]
        race_part = title[m.end():].strip() if m.end() < len(title) else None
        return PoliticalContractInfo(
            event=event, contract_type="matchup",
            candidates=candidates, party=None,
            race=_clean_race(race_part) if race_part else None, state=state,
            threshold=None, direction=None,
        )

    # 4. party_outcome (before candidate_win — "Democratic candidate wins" contains a name-like pattern)
    m = _PARTY_RE.search(title)
    if m:
        party_raw = m.group("party").lower()
        party = _PARTY_MAP.get(party_raw, party_raw)
        race = _clean_race(m.group("race"))
        return PoliticalContractInfo(
            event=event, contract_type="party_outcome",
            candidates=[], party=party,
            race=race, state=state or _extract_state(race),
            threshold=None, direction=None,
        )

    # 5. candidate_win
    m = _CANDIDATE_WIN_RE.search(title)
    if m:
        candidate = m.group("candidate").strip()
        race = _clean_race(m.group("race"))
        return PoliticalContractInfo(
            event=event, contract_type="candidate_win",
            candidates=[candidate], party=_extract_party(title),
            race=race, state=state or _extract_state(race),
            threshold=None, direction=None,
        )

    # 6. Fallback: yes_no_binary
    return PoliticalContractInfo(
        event=event, contract_type="yes_no_binary",
        candidates=[], party=_extract_party(title),
        race=None, state=state,
        threshold=None, direction=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_classifier.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/classifier.py tests/test_political_classifier.py
git commit -m "feat(political): add regex-based contract type classifier"
```

---

### Task 3: Political Clustering

**Files:**
- Create: `src/political/clustering.py`
- Create: `tests/test_political_clustering.py`

- [ ] **Step 1: Write clustering tests**

```python
# tests/test_political_clustering.py
"""Tests for political contract clustering by race+state."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from political.clustering import build_clusters
from political.models import PoliticalContractInfo
from adapters.models import NormalizedEvent


def _make_event(title, platform="polymarket", event_id="ev1", yes=0.50, no=0.50):
    return NormalizedEvent(
        platform=platform, event_id=event_id, title=title,
        category="politics", yes_price=yes, no_price=no,
        volume=1000, expiry="2026-11-03", url=f"https://{platform}.com/{event_id}",
    )


def _make_info(title, contract_type, race, state=None, candidates=None,
               platform="polymarket", event_id="ev1", yes=0.50, no=0.50):
    ev = _make_event(title, platform=platform, event_id=event_id, yes=yes, no=no)
    return PoliticalContractInfo(
        event=ev, contract_type=contract_type,
        candidates=candidates or [], party=None,
        race=race, state=state,
        threshold=None, direction=None,
    )


class TestBuildClusters:
    def test_same_race_grouped(self):
        """Contracts about the same race should be in one cluster."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win", "TX Senate", "TX",
                        ["Talarico"], event_id="e1")
        c2 = _make_info("Cruz wins TX Senate", "candidate_win", "TX Senate", "TX",
                        ["Cruz"], event_id="e2")
        clusters = build_clusters([c1, c2])
        assert len(clusters) == 1
        assert len(clusters[0].contracts) == 2

    def test_different_races_separate(self):
        """Contracts about different races should be in separate clusters."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win", "TX Senate", "TX",
                        event_id="e1")
        c2 = _make_info("Smith wins CA Governor", "candidate_win", "CA Governor", "CA",
                        event_id="e2")
        clusters = build_clusters([c1, c2])
        assert len(clusters) == 2

    def test_fuzzy_race_matching(self):
        """'TX Senate' and 'Texas Senate' should merge."""
        c1 = _make_info("A wins TX Senate", "candidate_win", "TX Senate", "TX", event_id="e1")
        c2 = _make_info("B wins Texas Senate", "candidate_win", "Texas Senate", "TX", event_id="e2")
        clusters = build_clusters([c1, c2])
        assert len(clusters) == 1

    def test_min_two_contracts(self):
        """Clusters with <2 contracts should be filtered out."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win", "TX Senate", "TX",
                        event_id="e1")
        clusters = build_clusters([c1])
        assert len(clusters) == 0

    def test_none_race_excluded(self):
        """Contracts with no race should not form clusters."""
        c1 = _make_info("Shutdown?", "yes_no_binary", None, None, event_id="e1")
        c2 = _make_info("Recession?", "yes_no_binary", None, None, event_id="e2")
        clusters = build_clusters([c1, c2])
        assert len(clusters) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_clustering.py -v`
Expected: FAIL

- [ ] **Step 3: Implement clustering**

```python
# src/political/clustering.py
"""Group classified political contracts into clusters by race+state."""
import re
from political.models import PoliticalContractInfo, PoliticalCluster

# State abbreviation ↔ name mapping for fuzzy matching
_STATE_NAMES_TO_ABBREV = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn", "mississippi": "ms",
    "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy",
}
_ABBREV_TO_NAME = {v: k for k, v in _STATE_NAMES_TO_ABBREV.items()}


def _normalize_race(race: str, state: str | None = None) -> str:
    """Normalize race string for grouping.

    'TX Senate', 'Texas Senate', 'Senate TX' → 'tx-senate'
    """
    if not race:
        return ""
    lower = race.lower().strip()
    # Replace full state names with abbreviations
    for name, abbrev in _STATE_NAMES_TO_ABBREV.items():
        lower = lower.replace(name, abbrev)
    # Remove common filler words
    lower = re.sub(r"\b(the|race|election|seat|special)\b", "", lower)
    # Extract meaningful tokens
    tokens = [t for t in re.split(r"\W+", lower) if t]
    # Sort tokens so "Senate TX" == "TX Senate"
    tokens.sort()
    return "-".join(tokens)


def build_clusters(contracts: list[PoliticalContractInfo]) -> list[PoliticalCluster]:
    """Group contracts by normalized race+state. Min 2 contracts per cluster."""
    groups: dict[str, list[PoliticalContractInfo]] = {}

    for c in contracts:
        if not c.race:
            continue
        key = _normalize_race(c.race, c.state)
        if not key:
            continue
        groups.setdefault(key, []).append(c)

    clusters = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Use first contract's race/state as canonical
        race = members[0].race or key
        state = members[0].state
        cluster_id = f"{key}-2026"
        matched_event_ids = list({c.event.event_id for c in members})
        clusters.append(PoliticalCluster(
            cluster_id=cluster_id, race=race, state=state,
            contracts=members, matched_events=matched_event_ids,
        ))

    return clusters
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_clustering.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/clustering.py tests/test_political_clustering.py
git commit -m "feat(political): add contract clustering by race+state"
```

---

## Chunk 2: Relationship Detection, Cache & LLM Strategy

### Task 4: Relationship Detection

**Files:**
- Create: `src/political/relationships.py`
- Create: `tests/test_political_relationships.py`

- [ ] **Step 1: Write relationship tests**

```python
# tests/test_political_relationships.py
"""Tests for political contract relationship detection."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from political.relationships import detect_relationships, build_leg_combinations
from political.models import PoliticalContractInfo, PoliticalCluster
from adapters.models import NormalizedEvent


def _make_info(title, contract_type, race="TX Senate", state="TX",
               candidates=None, party=None, threshold=None,
               platform="polymarket", event_id="ev1", yes=0.50, no=0.50):
    ev = NormalizedEvent(
        platform=platform, event_id=event_id, title=title,
        category="politics", yes_price=yes, no_price=no,
        volume=1000, expiry="2026-11-03", url=f"https://{platform}.com/{event_id}",
    )
    return PoliticalContractInfo(
        event=ev, contract_type=contract_type,
        candidates=candidates or [], party=party,
        race=race, state=state,
        threshold=threshold, direction=None,
    )


class TestDetectRelationships:
    def test_mispriced_correlation(self):
        """Same race, same outcome, different platforms, price diff >3%."""
        c1 = _make_info("A wins TX Senate", "candidate_win", candidates=["A"],
                        platform="polymarket", event_id="e1", yes=0.62)
        c2 = _make_info("A wins TX Senate", "candidate_win", candidates=["A"],
                        platform="kalshi", event_id="e2", yes=0.55)
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "mispriced_correlation" for r in rels)

    def test_candidate_party_link(self):
        """candidate_win + party_outcome for same race, candidate is that party."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win",
                        candidates=["Talarico"], party="dem", event_id="e1", yes=0.62)
        c2 = _make_info("Democrat wins TX Senate", "party_outcome",
                        party="dem", event_id="e2", yes=0.55)
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "candidate_party_link" for r in rels)

    def test_margin_decomposition(self):
        """candidate_win + margin_bracket for same candidate."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win",
                        candidates=["Talarico"], event_id="e1", yes=0.62)
        c2 = _make_info("Talarico wins by >5%", "margin_bracket",
                        candidates=["Talarico"], threshold=5.0, event_id="e2", yes=0.38)
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "margin_decomposition" for r in rels)

    def test_conditional_hedge(self):
        """Two candidate_win for same race, different candidates."""
        c1 = _make_info("Talarico wins TX Senate", "candidate_win",
                        candidates=["Talarico"], event_id="e1")
        c2 = _make_info("Cruz wins TX Senate", "candidate_win",
                        candidates=["Cruz"], event_id="e2")
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "conditional_hedge" for r in rels)

    def test_bracket_spread(self):
        """Two margin_bracket at different thresholds."""
        c1 = _make_info("Wins by >5%", "margin_bracket", threshold=5.0, event_id="e1")
        c2 = _make_info("Margin <2%", "margin_bracket", threshold=2.0, event_id="e2")
        rels = detect_relationships([c1, c2])
        assert any(r["type"] == "bracket_spread" for r in rels)

    def test_no_relationship_binary_only(self):
        """Two yes_no_binary on same platform shouldn't produce relationships."""
        c1 = _make_info("Shutdown?", "yes_no_binary", race=None, event_id="e1")
        c2 = _make_info("Recession?", "yes_no_binary", race=None, event_id="e2")
        rels = detect_relationships([c1, c2])
        assert len(rels) == 0


class TestBuildLegCombinations:
    def test_caps_at_4_legs(self):
        """Should not produce combinations with more than 4 legs."""
        contracts = [
            _make_info(f"C{i} wins TX", "candidate_win", candidates=[f"C{i}"],
                       event_id=f"e{i}", yes=0.5 + i * 0.02)
            for i in range(6)
        ]
        rels = detect_relationships(contracts)
        combos = build_leg_combinations(contracts, rels)
        for combo in combos:
            assert len(combo["contracts"]) <= 4

    def test_min_score_filter(self):
        """Combinations below min score should be filtered."""
        c1 = _make_info("A", "yes_no_binary", event_id="e1")
        c2 = _make_info("B", "yes_no_binary", event_id="e2")
        rels = detect_relationships([c1, c2])
        combos = build_leg_combinations([c1, c2], rels)
        # No real relationships → score too low → filtered
        assert len(combos) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_relationships.py -v`
Expected: FAIL

- [ ] **Step 3: Implement relationship detection**

```python
# src/political/relationships.py
"""Detect relationships between political contracts and build leg combinations."""
from political.models import PoliticalContractInfo, PLATFORM_FEES

# Score multipliers per relationship type (from spec)
RELATIONSHIP_SCORES = {
    "mispriced_correlation": 3.0,
    "candidate_party_link": 2.5,
    "margin_decomposition": 2.0,
    "conditional_hedge": 1.5,
    "bracket_spread": 1.5,
    "matchup_arbitrage": 2.0,
}

MIN_RELATIONSHIP_SCORE = 1.5
MAX_LEGS = 4
MIN_NET_EV_AFTER_FEES = 1.0  # must have 1% net EV after fees


def detect_relationships(contracts: list[PoliticalContractInfo]) -> list[dict]:
    """Detect pairwise relationships between contracts.

    Returns list of dicts: {"type", "pair": (idx_a, idx_b), "score", "details"}.
    """
    rels = []
    for i in range(len(contracts)):
        for j in range(i + 1, len(contracts)):
            a, b = contracts[i], contracts[j]
            rel = _classify_pair(a, b, i, j)
            if rel:
                rels.append(rel)
    return rels


def _same_candidates(a: PoliticalContractInfo, b: PoliticalContractInfo) -> bool:
    """Check if any candidate names overlap."""
    return bool(set(c.lower() for c in a.candidates) & set(c.lower() for c in b.candidates))


def _different_candidates(a: PoliticalContractInfo, b: PoliticalContractInfo) -> bool:
    """Check if candidates are explicitly different (both have names, no overlap)."""
    if not a.candidates or not b.candidates:
        return False
    return not _same_candidates(a, b)


def _classify_pair(a: PoliticalContractInfo, b: PoliticalContractInfo,
                   idx_a: int, idx_b: int) -> dict | None:
    """Classify the relationship between two contracts."""

    # 1. mispriced_correlation: same contract type, same implied outcome, different platforms, price diff >3%
    if (a.event.platform != b.event.platform
            and a.contract_type == b.contract_type
            and a.contract_type != "yes_no_binary"
            and _same_candidates(a, b)):
        price_diff = abs(a.event.yes_price - b.event.yes_price)
        if price_diff > 0.03:
            return {
                "type": "mispriced_correlation",
                "pair": (idx_a, idx_b),
                "score": RELATIONSHIP_SCORES["mispriced_correlation"],
                "details": f"Price gap {price_diff:.1%} across {a.event.platform}/{b.event.platform}",
            }

    # 2. candidate_party_link: candidate_win + party_outcome, same race, same party
    if ({a.contract_type, b.contract_type} == {"candidate_win", "party_outcome"}):
        cand = a if a.contract_type == "candidate_win" else b
        party = b if a.contract_type == "candidate_win" else a
        if cand.party and cand.party == party.party:
            price_diff = abs(cand.event.yes_price - party.event.yes_price)
            return {
                "type": "candidate_party_link",
                "pair": (idx_a, idx_b),
                "score": RELATIONSHIP_SCORES["candidate_party_link"],
                "details": f"Candidate-party link, price gap {price_diff:.1%}",
            }

    # 3. margin_decomposition: candidate_win + margin_bracket, same candidate
    if ({a.contract_type, b.contract_type} == {"candidate_win", "margin_bracket"}):
        if _same_candidates(a, b):
            return {
                "type": "margin_decomposition",
                "pair": (idx_a, idx_b),
                "score": RELATIONSHIP_SCORES["margin_decomposition"],
                "details": "Win prob must be >= margin prob",
            }

    # 4. conditional_hedge: same race, different candidates (mutually exclusive)
    if (a.contract_type == "candidate_win" and b.contract_type == "candidate_win"
            and _different_candidates(a, b)):
        return {
            "type": "conditional_hedge",
            "pair": (idx_a, idx_b),
            "score": RELATIONSHIP_SCORES["conditional_hedge"],
            "details": f"{a.candidates[0]} vs {b.candidates[0]} — mutually exclusive",
        }

    # 5. bracket_spread: two margin_bracket or vote_share at different thresholds
    if (a.contract_type == b.contract_type
            and a.contract_type in ("margin_bracket", "vote_share")
            and a.threshold is not None and b.threshold is not None
            and a.threshold != b.threshold):
        return {
            "type": "bracket_spread",
            "pair": (idx_a, idx_b),
            "score": RELATIONSHIP_SCORES["bracket_spread"],
            "details": f"Threshold spread: {a.threshold}% vs {b.threshold}%",
        }

    # 6. matchup_arbitrage: matchup + individual candidate_win
    if ({a.contract_type, b.contract_type} == {"matchup", "candidate_win"}):
        matchup = a if a.contract_type == "matchup" else b
        cand = b if a.contract_type == "matchup" else a
        if any(c.lower() in [mc.lower() for mc in matchup.candidates] for c in cand.candidates):
            return {
                "type": "matchup_arbitrage",
                "pair": (idx_a, idx_b),
                "score": RELATIONSHIP_SCORES["matchup_arbitrage"],
                "details": "Matchup price should equal conditional win probability",
            }

    return None


def build_leg_combinations(contracts: list[PoliticalContractInfo],
                           relationships: list[dict]) -> list[dict]:
    """Build candidate leg combinations from relationships.

    Starts with highest-scored pair, greedily adds legs that introduce
    new relationship types. Caps at MAX_LEGS. Checks fee constraints.

    Returns list of dicts: {"contracts": [indices], "relationships": [rel_dicts], "total_score"}.
    """
    if not relationships:
        return []

    # Sort relationships by score descending
    sorted_rels = sorted(relationships, key=lambda r: r["score"], reverse=True)

    # Filter: at least one relationship must have score >= MIN_RELATIONSHIP_SCORE
    if sorted_rels[0]["score"] < MIN_RELATIONSHIP_SCORE:
        return []

    combos = []
    used_seeds = set()

    for seed_rel in sorted_rels:
        seed_key = tuple(sorted(seed_rel["pair"]))
        if seed_key in used_seeds:
            continue
        used_seeds.add(seed_key)

        # Start with the seed pair
        included_indices = set(seed_rel["pair"])
        included_rels = [seed_rel]
        rel_types_used = {seed_rel["type"]}

        # Greedily extend to 3-4 legs
        for ext_rel in sorted_rels:
            if len(included_indices) >= MAX_LEGS:
                break
            pair_set = set(ext_rel["pair"])
            # Must introduce at least one new contract AND ideally a new relationship type
            new_contracts = pair_set - included_indices
            if not new_contracts:
                continue
            if ext_rel["type"] not in rel_types_used or len(included_indices) < 3:
                included_indices.update(pair_set)
                included_rels.append(ext_rel)
                rel_types_used.add(ext_rel["type"])

        total_score = sum(r["score"] for r in included_rels)

        # Fee constraint check
        total_fees = sum(
            PLATFORM_FEES.get(contracts[i].event.platform, 2.0)
            for i in included_indices
        )

        combo = {
            "contracts": sorted(included_indices),
            "relationships": included_rels,
            "total_score": total_score,
            "total_fees_pct": total_fees,
            "rel_types": list(rel_types_used),
        }
        combos.append(combo)

    return combos
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_relationships.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/relationships.py tests/test_political_relationships.py
git commit -m "feat(political): add relationship detection and leg combination builder"
```

---

### Task 5: Political Cache

**Files:**
- Create: `src/political/cache.py`
- Create: `tests/test_political_cache.py`

- [ ] **Step 1: Write cache tests**

```python
# tests/test_political_cache.py
"""Tests for political analysis cache."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import time
import pytest
from political.cache import PoliticalCache


class TestPoliticalCache:
    def test_set_and_get(self):
        cache = PoliticalCache(ttl_seconds=60, max_entries=10)
        cache.set(["id1", "id2"], {"strategy": "test"}, {"id1": 0.50, "id2": 0.60})
        result = cache.get(["id1", "id2"], {"id1": 0.50, "id2": 0.60})
        assert result == {"strategy": "test"}

    def test_ttl_expiry(self):
        cache = PoliticalCache(ttl_seconds=0, max_entries=10)  # immediate expiry
        cache.set(["id1"], {"data": 1}, {"id1": 0.50})
        result = cache.get(["id1"], {"id1": 0.50})
        assert result is None

    def test_price_shift_invalidation(self):
        """Cache invalidated when price shifts >3%."""
        cache = PoliticalCache(ttl_seconds=300, max_entries=10)
        cache.set(["id1"], {"data": 1}, {"id1": 0.50})
        # Price shifted from 0.50 to 0.55 = 10% shift > 3% threshold
        result = cache.get(["id1"], {"id1": 0.55})
        assert result is None

    def test_price_within_threshold(self):
        """Cache NOT invalidated when price shift <=3%."""
        cache = PoliticalCache(ttl_seconds=300, max_entries=10)
        cache.set(["id1"], {"data": 1}, {"id1": 0.50})
        # Price shifted from 0.50 to 0.51 = 2% shift <= 3%
        result = cache.get(["id1"], {"id1": 0.51})
        assert result == {"data": 1}

    def test_lru_eviction(self):
        """Oldest entries evicted when max_entries exceeded."""
        cache = PoliticalCache(ttl_seconds=300, max_entries=3)
        cache.set(["a"], {"v": 1}, {"a": 0.5})
        cache.set(["b"], {"v": 2}, {"b": 0.5})
        cache.set(["c"], {"v": 3}, {"c": 0.5})
        cache.set(["d"], {"v": 4}, {"d": 0.5})  # should evict "a"
        assert cache.get(["a"], {"a": 0.5}) is None
        assert cache.get(["d"], {"d": 0.5}) == {"v": 4}

    def test_cache_key_order_independent(self):
        """['id1', 'id2'] and ['id2', 'id1'] should produce same key."""
        cache = PoliticalCache(ttl_seconds=300, max_entries=10)
        cache.set(["id2", "id1"], {"data": 1}, {"id1": 0.5, "id2": 0.6})
        result = cache.get(["id1", "id2"], {"id1": 0.5, "id2": 0.6})
        assert result == {"data": 1}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_cache.py -v`
Expected: FAIL

- [ ] **Step 3: Implement cache**

```python
# src/political/cache.py
"""LRU cache with TTL and price-shift invalidation for political analysis."""
import hashlib
import time
from collections import OrderedDict

PRICE_SHIFT_THRESHOLD = 0.03  # 3% price change invalidates cache


class PoliticalCache:
    """SHA-256 keyed LRU cache with TTL and price-shift invalidation."""

    def __init__(self, ttl_seconds: int = 900, max_entries: int = 200):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._cache: OrderedDict[str, dict] = OrderedDict()

    @staticmethod
    def _make_key(contract_ids: list[str]) -> str:
        """SHA-256 hash of sorted contract IDs."""
        canonical = ",".join(sorted(contract_ids))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def get(self, contract_ids: list[str], current_prices: dict[str, float]) -> dict | None:
        """Get cached result, or None if expired/invalidated/missing."""
        key = self._make_key(contract_ids)
        entry = self._cache.get(key)
        if entry is None:
            return None

        # TTL check
        if time.time() - entry["created_at"] > self._ttl:
            del self._cache[key]
            return None

        # Price-shift invalidation
        cached_prices = entry.get("prices", {})
        for cid, cached_price in cached_prices.items():
            current = current_prices.get(cid)
            if current is not None and cached_price > 0:
                shift = abs(current - cached_price) / cached_price
                if shift > PRICE_SHIFT_THRESHOLD:
                    del self._cache[key]
                    return None

        # Move to end (most recently used)
        self._cache.move_to_end(key)
        return entry["data"]

    def set(self, contract_ids: list[str], data: dict, prices: dict[str, float]):
        """Cache a result with current prices for invalidation checks."""
        key = self._make_key(contract_ids)
        self._cache[key] = {
            "data": data,
            "prices": prices,
            "created_at": time.time(),
        }
        self._cache.move_to_end(key)

        # LRU eviction
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    def clear(self):
        self._cache.clear()

    def __len__(self):
        return len(self._cache)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/cache.py tests/test_political_cache.py
git commit -m "feat(political): add LRU cache with TTL and price-shift invalidation"
```

---

### Task 6: LLM Strategy Prompt & Response

**Files:**
- Create: `src/political/strategy.py`
- Create: `tests/test_political_strategy.py`

- [ ] **Step 1: Write strategy tests**

```python
# tests/test_political_strategy.py
"""Tests for political LLM strategy prompt building and response parsing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import json
import pytest
from political.strategy import build_cluster_prompt, parse_strategy_response, validate_strategy
from political.models import (
    PoliticalContractInfo, PoliticalCluster,
    PoliticalSyntheticStrategy, SyntheticLeg, Scenario,
)
from adapters.models import NormalizedEvent


def _make_cluster():
    events = [
        NormalizedEvent("polymarket", "e1", "Talarico wins TX Senate", "politics",
                        0.62, 0.38, 1000, "2026-11-03", "https://poly.com/e1"),
        NormalizedEvent("kalshi", "e2", "Democrat wins TX Senate", "politics",
                        0.55, 0.45, 800, "2026-11-03", "https://kalshi.com/e2"),
        NormalizedEvent("polymarket", "e3", "Talarico wins by >5%", "politics",
                        0.38, 0.62, 500, "2026-11-03", "https://poly.com/e3"),
    ]
    contracts = [
        PoliticalContractInfo(events[0], "candidate_win", ["Talarico"], "dem", "TX Senate", "TX", None, None),
        PoliticalContractInfo(events[1], "party_outcome", [], "dem", "TX Senate", "TX", None, None),
        PoliticalContractInfo(events[2], "margin_bracket", ["Talarico"], None, None, "TX", 5.0, "above"),
    ]
    return PoliticalCluster("tx-senate-2026", "TX Senate", "TX", contracts, ["match1"])


class TestBuildPrompt:
    def test_prompt_includes_contracts(self):
        cluster = _make_cluster()
        rels = [{"type": "candidate_party_link", "pair": (0, 1), "score": 2.5,
                 "details": "Price gap 7%"}]
        prompt = build_cluster_prompt(cluster, rels)
        assert "Talarico wins TX Senate" in prompt
        assert "Democrat wins TX Senate" in prompt
        assert "candidate_party_link" in prompt
        assert "Fee rates" in prompt

    def test_prompt_includes_fee_rates(self):
        cluster = _make_cluster()
        prompt = build_cluster_prompt(cluster, [])
        assert "Polymarket=2%" in prompt
        assert "Kalshi=1.5%" in prompt


class TestParseResponse:
    def test_valid_json_parsed(self):
        cluster = _make_cluster()
        response = json.dumps({
            "strategies": [{
                "strategy_name": "TX Senate Dem Link",
                "legs": [
                    {"contract": 1, "side": "YES", "weight": 0.5},
                    {"contract": 2, "side": "YES", "weight": 0.5},
                ],
                "scenarios": [
                    {"outcome": "Talarico wins", "probability": 0.6, "pnl_pct": 12.5},
                    {"outcome": "Other Dem wins", "probability": 0.1, "pnl_pct": -5.0},
                    {"outcome": "Republican wins", "probability": 0.3, "pnl_pct": -40.0},
                ],
                "expected_value_pct": 8.2,
                "win_probability": 0.65,
                "max_loss_pct": -45.0,
                "confidence": "high",
                "reasoning": "Price gap exploitation",
            }]
        })
        strategies = parse_strategy_response(response, cluster)
        assert len(strategies) == 1
        assert strategies[0].strategy_name == "TX Senate Dem Link"
        assert len(strategies[0].legs) == 2
        assert strategies[0].legs[0].event_id == "e1"

    def test_invalid_json_returns_empty(self):
        cluster = _make_cluster()
        strategies = parse_strategy_response("not json at all", cluster)
        assert strategies == []

    def test_missing_fields_returns_empty(self):
        cluster = _make_cluster()
        response = json.dumps({"strategies": [{"strategy_name": "X"}]})
        strategies = parse_strategy_response(response, cluster)
        assert strategies == []


class TestValidateStrategy:
    def _make_strategy(self, **overrides):
        defaults = {
            "cluster_id": "test",
            "strategy_name": "Test",
            "legs": [SyntheticLeg(1, "e1", "YES", 0.5)],
            "scenarios": [Scenario("Win", 0.6, 10.0)],
            "expected_value_pct": 8.0,
            "win_probability": 0.65,
            "max_loss_pct": -40.0,
            "confidence": "high",
            "reasoning": "test",
        }
        defaults.update(overrides)
        return PoliticalSyntheticStrategy(**defaults)

    def test_valid_strategy_passes(self):
        s = self._make_strategy()
        assert validate_strategy(s) is True

    def test_low_win_probability_rejected(self):
        s = self._make_strategy(win_probability=0.40)
        assert validate_strategy(s) is False

    def test_extreme_loss_rejected(self):
        s = self._make_strategy(max_loss_pct=-70.0)
        assert validate_strategy(s) is False

    def test_low_ev_rejected(self):
        s = self._make_strategy(expected_value_pct=2.0)
        assert validate_strategy(s) is False

    def test_low_confidence_rejected(self):
        s = self._make_strategy(confidence="low")
        assert validate_strategy(s) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_strategy.py -v`
Expected: FAIL

- [ ] **Step 3: Implement strategy module**

```python
# src/political/strategy.py
"""LLM prompt building, response parsing, and validation for political strategies."""
import json
import logging
import re

from political.models import (
    PoliticalCluster, PoliticalSyntheticStrategy,
    SyntheticLeg, Scenario, PLATFORM_FEES,
)

logger = logging.getLogger("political.strategy")


def build_cluster_prompt(cluster: PoliticalCluster, relationships: list[dict]) -> str:
    """Build LLM prompt for a single political cluster."""
    contracts_text = []
    for i, c in enumerate(cluster.contracts, 1):
        contracts_text.append(
            f'  {i}. "{c.event.title}" | {c.event.platform} | '
            f'YES=${c.event.yes_price:.2f} NO=${c.event.no_price:.2f}'
        )

    rels_text = []
    for r in relationships:
        a, b = r["pair"]
        rels_text.append(f"  - ({a+1},{b+1}): {r['type']} — {r['details']}")

    return f"""You are a political prediction market analyst. For each cluster below,
analyze the contracts and recommend optimal synthetic positions.

IMPORTANT: All expected value and P&L figures must be AFTER platform fees.
Fee rates (round-trip): Polymarket=2%, Kalshi=1.5%, PredictIt=10%, Limitless=2%.

[CLUSTER:{cluster.cluster_id}]
Race: {cluster.race} {cluster.state or ''}
Contracts:
{chr(10).join(contracts_text)}

Pre-classified relationships:
{chr(10).join(rels_text) if rels_text else '  (none detected)'}

For each recommended position, respond with this exact JSON structure:
{{
  "strategies": [{{
    "strategy_name": "human-readable name",
    "legs": [{{"contract": 1, "side": "YES", "weight": 0.5}}],
    "scenarios": [{{"outcome": "description", "probability": 0.6, "pnl_pct": 12.5}}],
    "expected_value_pct": 8.2,
    "win_probability": 0.65,
    "max_loss_pct": -45.0,
    "confidence": "high",
    "reasoning": "explanation"
  }}]
}}

Respond with ONLY valid JSON. No preamble, no explanation outside the JSON."""


def parse_strategy_response(response_text: str,
                             cluster: PoliticalCluster) -> list[PoliticalSyntheticStrategy]:
    """Parse LLM JSON response into PoliticalSyntheticStrategy objects."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", response_text).strip()
    cleaned = cleaned.rstrip("`")
    # Handle trailing commas
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Political strategy: invalid JSON response")
        return []

    if isinstance(data, dict) and "strategies" in data:
        raw_strategies = data["strategies"]
    elif isinstance(data, list):
        raw_strategies = data
    else:
        return []

    strategies = []
    for s in raw_strategies:
        try:
            legs_raw = s.get("legs", [])
            if not legs_raw:
                continue

            legs = []
            for leg in legs_raw:
                idx = leg.get("contract", 0)
                if idx < 1 or idx > len(cluster.contracts):
                    continue
                contract = cluster.contracts[idx - 1]
                legs.append(SyntheticLeg(
                    contract_idx=idx,
                    event_id=contract.event.event_id,
                    side=leg.get("side", "YES"),
                    weight=leg.get("weight", 1.0 / len(legs_raw)),
                ))

            if not legs:
                continue

            scenarios = [
                Scenario(
                    outcome=sc.get("outcome", ""),
                    probability=sc.get("probability", 0),
                    pnl_pct=sc.get("pnl_pct", 0),
                )
                for sc in s.get("scenarios", [])
            ]

            strategy = PoliticalSyntheticStrategy(
                cluster_id=cluster.cluster_id,
                strategy_name=s.get("strategy_name", "Unnamed"),
                legs=legs,
                scenarios=scenarios,
                expected_value_pct=s.get("expected_value_pct", 0),
                win_probability=s.get("win_probability", 0),
                max_loss_pct=s.get("max_loss_pct", 0),
                confidence=s.get("confidence", "low"),
                reasoning=s.get("reasoning", ""),
            )
            strategies.append(strategy)
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Political strategy: failed to parse strategy entry: %s", e)
            continue

    return strategies


def validate_strategy(strategy: PoliticalSyntheticStrategy) -> bool:
    """Post-LLM validation. Returns True if strategy passes all checks."""
    if strategy.win_probability < 0.50:
        return False
    if strategy.max_loss_pct < -60.0:
        return False
    if strategy.expected_value_pct < 3.0:
        return False
    if strategy.confidence == "low":
        return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_political_strategy.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/strategy.py tests/test_political_strategy.py
git commit -m "feat(political): add LLM strategy prompt builder, parser, and validator"
```

---

## Chunk 3: PoliticalAnalyzer Orchestrator & Integration

### Task 7: PoliticalAnalyzer Orchestrator

**Files:**
- Create: `src/political/analyzer.py`

- [ ] **Step 1: Implement the main analyzer**

```python
# src/political/analyzer.py
"""PoliticalAnalyzer — orchestrates the political synthetic analysis pipeline.

Runs on a 15-minute asyncio loop. Reuses events from arb scanner (no redundant fetch).
Groups political events into clusters, detects relationships, sends top combos to LLM,
and produces PoliticalOpportunity objects for auto trader consumption.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from political.cache import PoliticalCache
from political.classifier import classify_contract
from political.clustering import build_clusters
from political.models import (
    PoliticalCluster, PoliticalOpportunity, PoliticalLeg, PLATFORM_FEES,
)
from political.relationships import detect_relationships, build_leg_combinations
from political.strategy import build_cluster_prompt, parse_strategy_response, validate_strategy

logger = logging.getLogger("political.analyzer")

SCAN_INTERVAL = 900  # 15 minutes
MAX_CLUSTERS_PER_CYCLE = 10
MAX_COMBOS_PER_CLUSTER = 3


class PoliticalAnalyzer:
    """Orchestrates political synthetic derivative analysis."""

    def __init__(self, scanner=None, ai_advisor=None, decision_logger=None,
                 auto_trader=None):
        self.scanner = scanner
        self.ai = ai_advisor
        self.dlog = decision_logger
        self._auto_trader = auto_trader
        self.cache = PoliticalCache(ttl_seconds=SCAN_INTERVAL, max_entries=200)
        self._opportunities: list[dict] = []
        self._clusters: list[PoliticalCluster] = []
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._loop())
        logger.info("Political analyzer started (interval=%ds)", SCAN_INTERVAL)

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Political analyzer stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._analyze_cycle()
            except Exception as e:
                logger.error("Political analyzer cycle error: %s", e)
            await asyncio.sleep(SCAN_INTERVAL)

    async def _analyze_cycle(self):
        """One full analysis cycle."""
        t0 = time.time()

        # Get events from arb scanner cache (no redundant fetch)
        if not self.scanner:
            return
        all_events_raw = self.scanner.get_events()
        if not all_events_raw:
            return

        # Filter to political events and reconstruct NormalizedEvents
        from adapters.models import NormalizedEvent
        political_events = []
        for ev_dict in all_events_raw:
            markets = ev_dict.get("markets", [])
            for m in markets:
                if m.get("category", "") == "politics":
                    try:
                        ne = NormalizedEvent(
                            platform=m["platform"], event_id=m["event_id"],
                            title=m["title"], category=m["category"],
                            yes_price=m["yes_price"], no_price=m["no_price"],
                            volume=m.get("volume", 0),
                            expiry=m.get("expiry", "ongoing"),
                            url=m.get("url", ""),
                            last_updated=m.get("last_updated", ""),
                        )
                        political_events.append(ne)
                    except (KeyError, TypeError):
                        continue

        if not political_events:
            logger.debug("Political analyzer: no political events found")
            return

        # Classify all political events
        classified = [classify_contract(ev) for ev in political_events]

        # Build clusters
        clusters = build_clusters(classified)
        self._clusters = clusters
        if not clusters:
            logger.debug("Political analyzer: no clusters formed from %d events", len(political_events))
            return

        logger.info("Political analyzer: %d political events → %d clusters",
                     len(political_events), len(clusters))

        # Analyze top clusters
        new_opportunities = []
        for cluster in clusters[:MAX_CLUSTERS_PER_CYCLE]:
            opps = await self._analyze_cluster(cluster)
            new_opportunities.extend(opps)

        self._opportunities = [o.to_dict() for o in new_opportunities]
        elapsed_ms = int((time.time() - t0) * 1000)
        logger.info("Political analyzer: produced %d opportunities in %dms",
                     len(new_opportunities), elapsed_ms)

        # Log to decision log
        if self.dlog and new_opportunities:
            self.dlog._write({
                "type": "political_analysis_cycle",
                "political_events": len(political_events),
                "clusters": len(clusters),
                "opportunities": len(new_opportunities),
                "elapsed_ms": elapsed_ms,
            })

        # Wake auto trader if we found opportunities
        # _auto_trader has the _scan_event (not the scanner), so use the reference
        if new_opportunities and self._auto_trader:
            try:
                await self._auto_trader.notify_scan_complete()
            except Exception:
                pass

    async def _analyze_cluster(self, cluster: PoliticalCluster) -> list[PoliticalOpportunity]:
        """Analyze a single cluster: detect relationships, check cache, call LLM."""
        contract_ids = [c.event.event_id for c in cluster.contracts]
        current_prices = {c.event.event_id: c.event.yes_price for c in cluster.contracts}

        # Check cache
        cached = self.cache.get(contract_ids, current_prices)
        if cached is not None:
            logger.debug("Political analyzer: cache hit for %s", cluster.cluster_id)
            return cached  # returns list[PoliticalOpportunity]

        # Detect relationships
        relationships = detect_relationships(cluster.contracts)
        if not relationships:
            self.cache.set(contract_ids, [], current_prices)
            return []

        # Build candidate leg combinations
        combos = build_leg_combinations(cluster.contracts, relationships)
        if not combos:
            self.cache.set(contract_ids, [], current_prices)
            return []

        # LLM strategy analysis
        if not self.ai or not self.ai.is_available:
            logger.debug("Political analyzer: no AI available, skipping LLM analysis")
            self.cache.set(contract_ids, [], current_prices)
            return []

        # Build prompt with top combo's relationships
        top_combo = combos[0]
        prompt = build_cluster_prompt(cluster, top_combo["relationships"])

        # Call AI provider
        try:
            providers = self.ai._get_available_providers()
            response_text = None
            for provider in providers:
                try:
                    response_text = await self.ai._call_provider(provider, prompt)
                    break
                except Exception as e:
                    logger.warning("Political LLM via %s failed: %s", provider["name"], e)
                    continue

            if not response_text:
                logger.warning("Political analyzer: all AI providers failed for %s",
                               cluster.cluster_id)
                return []

        except Exception as e:
            logger.error("Political analyzer: LLM call failed: %s", e)
            return []

        # Parse and validate
        strategies = parse_strategy_response(response_text, cluster)
        valid_strategies = [s for s in strategies if validate_strategy(s)]

        if not valid_strategies:
            self.cache.set(contract_ids, [], current_prices)
            return []

        # Convert to PoliticalOpportunity
        opportunities = []
        for strategy in valid_strategies:
            legs = []
            total_fees = 0.0
            for sleg in strategy.legs:
                contract = cluster.contracts[sleg.contract_idx - 1]
                fee = PLATFORM_FEES.get(contract.event.platform, 2.0)
                legs.append(PoliticalLeg(
                    event=contract.event,
                    contract_info=contract,
                    side=sleg.side,
                    weight=sleg.weight,
                    platform_fee_pct=fee,
                ))
                total_fees += fee

            net_ev = strategy.expected_value_pct - total_fees
            if net_ev < 1.0:
                continue

            opp = PoliticalOpportunity(
                cluster_id=cluster.cluster_id,
                strategy=strategy,
                legs=legs,
                total_fee_pct=total_fees,
                net_expected_value_pct=round(net_ev, 2),
                platforms=list({l.event.platform for l in legs}),
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            opportunities.append(opp)

        self.cache.set(contract_ids, opportunities, current_prices)
        return opportunities

    def get_opportunities(self) -> list[dict]:
        """Return current political opportunities as dicts."""
        return self._opportunities

    def get_clusters(self) -> list[dict]:
        """Return current clusters as dicts."""
        return [
            {
                "cluster_id": c.cluster_id,
                "race": c.race,
                "state": c.state,
                "contract_count": len(c.contracts),
                "contracts": [
                    {
                        "event_id": ci.event.event_id,
                        "platform": ci.event.platform,
                        "title": ci.event.title,
                        "contract_type": ci.contract_type,
                        "yes_price": ci.event.yes_price,
                        "no_price": ci.event.no_price,
                        "candidates": ci.candidates,
                        "party": ci.party,
                    }
                    for ci in c.contracts
                ],
            }
            for c in self._clusters
        ]

    async def analyze_cluster_by_id(self, cluster_id: str) -> list[dict]:
        """Force re-analysis of a specific cluster (bypasses cache)."""
        cluster = next((c for c in self._clusters if c.cluster_id == cluster_id), None)
        if not cluster:
            return []
        # Clear cache for this cluster
        contract_ids = [c.event.event_id for c in cluster.contracts]
        self.cache.set(contract_ids, None, {})  # invalidate
        opps = await self._analyze_cluster(cluster)
        return [o.to_dict() for o in opps]
```

- [ ] **Step 2: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/analyzer.py
git commit -m "feat(political): add PoliticalAnalyzer orchestrator with 15-min loop"
```

---

### Task 8: Position Manager + Exit Engine Integration

**Files:**
- Modify: `src/positions/position_manager.py:16`
- Modify: `src/positions/exit_engine.py:46-47, 110, 123, 133`

- [ ] **Step 1: Add political_synthetic to STRATEGY_TYPES**

In `src/positions/position_manager.py`, line 16:

```python
# BEFORE:
STRATEGY_TYPES = ("spot_plus_hedge", "cross_platform_arb", "synthetic_derivative", "pure_prediction", "news_driven")

# AFTER:
STRATEGY_TYPES = ("spot_plus_hedge", "cross_platform_arb", "synthetic_derivative", "pure_prediction", "news_driven", "political_synthetic")
```

- [ ] **Step 2: Add trigger #21 to exit engine**

In `src/positions/exit_engine.py`, after line 46:

```python
# Category: Political (21)
T_POLITICAL_EVENT_RESOLVED = 21  # Contract in cluster settled → evaluate all legs
```

- [ ] **Step 3: Extend strategy checks in exit engine**

In `src/positions/exit_engine.py`, update lines 110, 123, 133 — every `if strategy in ("cross_platform_arb", "synthetic_derivative")` must include `"political_synthetic"`:

```python
# Line 110 (correlation_break):
if strategy in ("cross_platform_arb", "synthetic_derivative", "political_synthetic") and len(legs) >= 2:

# Line 123 (spread_inversion):
if strategy in ("cross_platform_arb", "synthetic_derivative", "political_synthetic") and len(legs) >= 2:

# Line 133 (spread_compression):
if strategy in ("cross_platform_arb", "synthetic_derivative", "political_synthetic") and len(legs) >= 2:
```

- [ ] **Step 4: Add political_event_resolved trigger evaluation**

In `src/positions/exit_engine.py`, after the longshot_decay block (after line 221), before `return triggers`:

```python
    # ── 21: Political Event Resolved ─────────────────────────────────────
    if strategy == "political_synthetic":
        for leg in legs:
            # Check if any leg price is 0.00 or 1.00 (resolved)
            cur = leg.get("current_price", 0)
            if cur <= 0.01 or cur >= 0.99:
                triggers.append({"trigger_id": T_POLITICAL_EVENT_RESOLVED,
                    "name": "political_event_resolved",
                    "details": f"Leg {leg['leg_id']} resolved (price={cur:.4f})",
                    "action": "immediate_exit", "safety_override": True})
                break
```

- [ ] **Step 5: Add to auto-execute triggers**

In `src/positions/exit_engine.py` `_auto_execute_triggers()`, add `"political_event_resolved"` alongside existing safety triggers — it already has `safety_override: True` so it will be handled by the safety path in `_tick()`.

- [ ] **Step 6: Run existing exit engine tests**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_exit_engine.py -v`
Expected: PASS (existing tests still pass)

- [ ] **Step 7: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/positions/position_manager.py src/positions/exit_engine.py
git commit -m "feat(political): register strategy type and add trigger #21"
```

---

### Task 9: Auto Trader Political Opportunity Consumption

**Files:**
- Modify: `src/positions/auto_trader.py`

- [ ] **Step 1: Add political opportunity merging**

In `src/positions/auto_trader.py`, in `_scan_and_trade()`, after the news opportunities merge block (line ~194), add:

```python
        # Merge political synthetic opportunities
        if hasattr(self, '_political_analyzer') and self._political_analyzer:
            political_opps = self._political_analyzer.get_opportunities()
            for pol_opp in political_opps:
                # Score using LLM's expected value and confidence
                ev_pct = pol_opp.get("net_expected_value_pct", 0)
                confidence = pol_opp.get("strategy", {}).get("confidence", "medium")
                conf_mult = {"high": 1.5, "medium": 1.0}.get(confidence, 0.5)
                cross_platform = len(set(pol_opp.get("platforms", []))) > 1
                platform_mult = 1.5 if cross_platform else 1.0
                pol_opp["_score"] = ev_pct * conf_mult * platform_mult
                pol_opp["profit_pct"] = ev_pct  # auto trader reads this
                opportunities.append(pol_opp)
            if political_opps:
                logger.info("Auto trader: merged %d political opportunities", len(political_opps))
```

- [ ] **Step 2: Add political package creation path**

In `_scan_and_trade()`, before the existing strategy determination block (around line 392), add a handler for political opportunities:

```python
            # Political synthetic: multi-leg with weight-based allocation
            if opp.get("opportunity_type") == "political_synthetic":
                try:
                    pkg = create_package(f"Auto: {trade_title[:60]}", "political_synthetic")
                except ValueError:
                    continue

                opp_legs = opp.get("legs", [])
                if not opp_legs:
                    continue

                for opp_leg in opp_legs:
                    leg_cost = round(trade_size * opp_leg.get("weight", 1.0 / len(opp_legs)), 2)
                    leg_cost = max(MIN_TRADE_SIZE, leg_cost)
                    side = opp_leg.get("side", "YES")
                    leg_type = "prediction_yes" if side == "YES" else "prediction_no"
                    price = opp_leg.get("yes_price", 0.5) if side == "YES" else opp_leg.get("no_price", 0.5)
                    pkg["legs"].append(create_leg(
                        platform=opp_leg.get("platform", "polymarket"),
                        leg_type=leg_type,
                        asset_id=f"{opp_leg['event_id']}:{side}",
                        asset_label=f"{side} @ {opp_leg.get('platform', '?')}: {opp_leg.get('title', '?')[:40]}",
                        entry_price=price if price > 0 else 0.5,
                        cost=leg_cost,
                        expiry=opp.get("expiry", "2026-12-31")[:10],
                    ))

                pkg["exit_rules"].append(create_exit_rule("target_profit", {"target_pct": 30}))
                pkg["exit_rules"].append(create_exit_rule("stop_loss", {"stop_pct": -30}))
                pkg["exit_rules"].append(create_exit_rule("trailing_stop", {"current": 20, "bound_min": 10, "bound_max": 40}))
                pkg["_political_strategy"] = opp.get("strategy", {})

                # Political packages skip the normal strategy/side determination.
                # Fall through directly to exit rules + execution below.
                # The existing code after exit rules (the try/await pm.execute_package block)
                # handles any pkg with legs, regardless of strategy type.
```

- [ ] **Step 3: Set political_analyzer reference**

Add an attribute in `AutoTrader.__init__()`:

```python
        self._political_analyzer = None
```

And add a setter method:

```python
    def set_political_analyzer(self, analyzer):
        """Set the political analyzer reference for opportunity consumption."""
        self._political_analyzer = analyzer
```

- [ ] **Step 4: Run existing tests**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_position_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/positions/auto_trader.py
git commit -m "feat(political): add political opportunity consumption in auto trader"
```

---

## Chunk 4: Eval Logger, API Routers & Server Wiring

### Task 10: Universal Eval Logger

**Files:**
- Create: `src/eval_logger.py`
- Create: `tests/test_eval_logger.py`

- [ ] **Step 1: Write eval logger tests**

```python
# tests/test_eval_logger.py
"""Tests for universal eval logger."""
import sys
import json
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from eval_logger import EvalLogger


class TestEvalLogger:
    def test_log_entry(self, tmp_path):
        logger = EvalLogger(path=str(tmp_path / "eval_log.jsonl"))
        logger.log_opportunity(
            strategy_type="cross_platform_arb",
            opportunity_id="opp_123",
            action="entered",
            action_reason="high_spread",
            reason_detail="Spread 8.5% above threshold",
            markets=[{"event_id": "e1", "platform": "polymarket", "title": "BTC", "yes_price": 0.40}],
            score=25.5,
            spread_pct=8.5,
            prices_at_decision={"e1": 0.40},
        )
        lines = (tmp_path / "eval_log.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["strategy_type"] == "cross_platform_arb"
        assert entry["action"] == "entered"
        assert entry["score"] == 25.5
        assert "timestamp" in entry

    def test_log_skip(self, tmp_path):
        logger = EvalLogger(path=str(tmp_path / "eval_log.jsonl"))
        logger.log_opportunity(
            strategy_type="pure_prediction",
            opportunity_id="opp_456",
            action="skipped",
            action_reason="low_score",
            reason_detail="Score 3.2 below minimum 5.0",
            markets=[{"event_id": "e2"}],
            score=3.2,
            prices_at_decision={"e2": 0.30},
        )
        lines = (tmp_path / "eval_log.jsonl").read_text().strip().split("\n")
        entry = json.loads(lines[0])
        assert entry["action"] == "skipped"
        assert entry["action_reason"] == "low_score"

    def test_backfill_pnl(self, tmp_path):
        logger = EvalLogger(path=str(tmp_path / "eval_log.jsonl"))
        logger.log_opportunity(
            strategy_type="cross_platform_arb",
            opportunity_id="opp_789",
            action="entered",
            action_reason="high_spread",
            reason_detail="test",
            markets=[],
            prices_at_decision={},
        )
        logger.backfill_outcome("opp_789", actual_pnl_pct=12.5,
                                actual_outcome="win", resolution_date="2026-04-01",
                                prices_at_resolution={"e1": 1.0})
        lines = (tmp_path / "eval_log.jsonl").read_text().strip().split("\n")
        # Backfill appends a new line
        assert len(lines) == 2
        backfill = json.loads(lines[1])
        assert backfill["type"] == "backfill"
        assert backfill["opportunity_id"] == "opp_789"
        assert backfill["actual_pnl_pct"] == 12.5

    def test_get_summary(self, tmp_path):
        logger = EvalLogger(path=str(tmp_path / "eval_log.jsonl"))
        logger.log_opportunity("arb", "o1", "entered", "spread", "", [], prices_at_decision={})
        logger.log_opportunity("arb", "o2", "skipped", "low", "", [], prices_at_decision={})
        summary = logger.get_summary()
        assert summary["arb"]["entered"] == 1
        assert summary["arb"]["skipped"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_eval_logger.py -v`
Expected: FAIL

- [ ] **Step 3: Implement eval logger**

```python
# src/eval_logger.py
"""Universal evaluation logger — records all opportunities (entered + skipped) for hindsight analysis.

Logs to src/data/arbitrage/eval_log.jsonl. Supports backfill of outcomes after resolution.
"""
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger("eval_logger")

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "data", "arbitrage", "eval_log.jsonl")


class EvalLogger:
    """Append-only JSONL logger for opportunity evaluation."""

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def _write(self, entry: dict):
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write eval log: %s", e)

    def log_opportunity(self, strategy_type: str, opportunity_id: str,
                        action: str, action_reason: str, reason_detail: str = "",
                        markets: list[dict] | None = None,
                        score: float | None = None,
                        spread_pct: float | None = None,
                        expected_value_pct: float | None = None,
                        prices_at_decision: dict | None = None,
                        metadata: dict | None = None):
        """Log an opportunity decision (entered, skipped, rejected, etc.)."""
        entry = {
            "type": "opportunity",
            "strategy_type": strategy_type,
            "opportunity_id": opportunity_id,
            "action": action,
            "action_reason": action_reason,
            "reason_detail": reason_detail,
            "markets": markets or [],
            "score": score,
            "spread_pct": spread_pct,
            "expected_value_pct": expected_value_pct,
            "prices_at_decision": prices_at_decision or {},
        }
        if metadata:
            entry["metadata"] = metadata
        self._write(entry)

    def backfill_outcome(self, opportunity_id: str, actual_pnl_pct: float,
                         actual_outcome: str, resolution_date: str,
                         prices_at_resolution: dict | None = None):
        """Backfill outcome for a previously logged opportunity."""
        self._write({
            "type": "backfill",
            "opportunity_id": opportunity_id,
            "actual_pnl_pct": actual_pnl_pct,
            "actual_outcome": actual_outcome,
            "resolution_date": resolution_date,
            "prices_at_resolution": prices_at_resolution or {},
        })

    def get_summary(self) -> dict:
        """Read log and produce summary by strategy_type."""
        summary = defaultdict(lambda: defaultdict(int))
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "opportunity":
                            st = entry.get("strategy_type", "unknown")
                            action = entry.get("action", "unknown")
                            summary[st][action] += 1
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        return dict(summary)

    def get_missed_opportunities(self, strategy_type: str | None = None,
                                  min_pnl: float = 0) -> list[dict]:
        """Find skipped opportunities that were profitable (backfilled)."""
        skipped = {}
        backfills = {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "opportunity" and entry.get("action") == "skipped":
                            oid = entry.get("opportunity_id")
                            if strategy_type and entry.get("strategy_type") != strategy_type:
                                continue
                            skipped[oid] = entry
                        elif entry.get("type") == "backfill":
                            backfills[entry.get("opportunity_id")] = entry
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []

        missed = []
        for oid, skip_entry in skipped.items():
            bf = backfills.get(oid)
            if bf and bf.get("actual_pnl_pct", 0) > min_pnl:
                missed.append({**skip_entry, **bf})
        return missed

    def get_calibration(self) -> dict:
        """For each action_reason, show correct-skip rate vs missed-opportunity rate."""
        skips_by_reason = defaultdict(int)
        backfills = {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "opportunity" and entry.get("action") == "skipped":
                            reason = entry.get("action_reason", "unknown")
                            skips_by_reason[reason] += 1
                        elif entry.get("type") == "backfill":
                            backfills[entry.get("opportunity_id")] = entry
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return {}

        # Re-scan to match skips to backfills
        missed_by_reason = defaultdict(int)
        correct_by_reason = defaultdict(int)
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "opportunity" and entry.get("action") == "skipped":
                            oid = entry.get("opportunity_id")
                            reason = entry.get("action_reason", "unknown")
                            bf = backfills.get(oid)
                            if bf and bf.get("actual_pnl_pct", 0) > 0:
                                missed_by_reason[reason] += 1
                            elif bf:
                                correct_by_reason[reason] += 1
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass

        result = {}
        for reason in skips_by_reason:
            total = skips_by_reason[reason]
            missed = missed_by_reason.get(reason, 0)
            correct = correct_by_reason.get(reason, 0)
            resolved = missed + correct
            result[reason] = {
                "total_skips": total,
                "resolved": resolved,
                "correct_skips": correct,
                "missed_opportunities": missed,
                "correct_skip_rate": round(correct / resolved, 3) if resolved > 0 else None,
            }
        return result

    def get_details(self, opportunity_id: str) -> dict | None:
        """Get full entry for a specific opportunity."""
        result = {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        oid = entry.get("opportunity_id")
                        if oid == opportunity_id:
                            if entry.get("type") == "opportunity":
                                result = entry
                            elif entry.get("type") == "backfill":
                                result.update(entry)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        return result if result else None

    def get_unresolved_skips(self) -> list[dict]:
        """Get skipped opportunities that haven't been backfilled yet."""
        skipped_ids = set()
        backfilled_ids = set()
        skipped_entries = {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "opportunity" and entry.get("action") == "skipped":
                            oid = entry.get("opportunity_id")
                            skipped_ids.add(oid)
                            skipped_entries[oid] = entry
                        elif entry.get("type") == "backfill":
                            backfilled_ids.add(entry.get("opportunity_id"))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            return []
        return [skipped_entries[oid] for oid in (skipped_ids - backfilled_ids)
                if oid in skipped_entries]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/test_eval_logger.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/eval_logger.py tests/test_eval_logger.py
git commit -m "feat(eval): add universal eval logger with backfill and summary"
```

---

### Task 11: Political Router

**Files:**
- Create: `src/political/router.py`

- [ ] **Step 1: Implement political API router**

```python
# src/political/router.py
"""Political synthetic analysis API endpoints."""
import logging
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("political.router")

router = APIRouter(prefix="/api/political", tags=["political"])

_analyzer = None


def init_political_router(analyzer):
    """Called by server.py to inject the analyzer instance."""
    global _analyzer
    _analyzer = analyzer


class AnalyzeRequest(BaseModel):
    cluster_id: str


@router.get("/clusters")
async def get_clusters():
    """List active political clusters with contract classifications."""
    if _analyzer is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_analyzer.get_clusters())


@router.get("/strategies")
async def get_strategies():
    """List current LLM-recommended strategies."""
    if _analyzer is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_analyzer.get_opportunities())


@router.get("/strategies/{cluster_id}")
async def get_strategy_by_cluster(cluster_id: str):
    """Detailed strategy for a specific cluster."""
    if _analyzer is None:
        return JSONResponse(content={"error": "Not initialized"}, status_code=503)
    opps = [o for o in _analyzer.get_opportunities() if o.get("cluster_id") == cluster_id]
    return JSONResponse(content=opps)


@router.post("/analyze")
async def force_analyze(req: AnalyzeRequest):
    """Force re-analysis of a cluster (bypasses cache)."""
    if _analyzer is None:
        return JSONResponse(content={"error": "Not initialized"}, status_code=503)
    result = await _analyzer.analyze_cluster_by_id(req.cluster_id)
    return JSONResponse(content=result)


_eval_logger = None


def set_eval_logger(logger):
    """Set eval logger for political-specific eval endpoints."""
    global _eval_logger
    _eval_logger = logger


@router.get("/eval")
async def get_political_eval_summary():
    """Strategy performance summary for political synthetics."""
    if _analyzer is None or _eval_logger is None:
        return JSONResponse(content={})
    summary = _eval_logger.get_summary()
    return JSONResponse(content=summary.get("political_synthetic", {}))


@router.get("/eval/missed")
async def get_political_eval_missed():
    """Skipped political strategies that would have been profitable."""
    if _eval_logger is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_eval_logger.get_missed_opportunities(
        strategy_type="political_synthetic"))
```

- [ ] **Step 2: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/router.py
git commit -m "feat(political): add API router for clusters and strategies"
```

---

### Task 12: Eval Router

**Files:**
- Create: `src/eval_router.py`

- [ ] **Step 1: Implement eval API router**

```python
# src/eval_router.py
"""Evaluation/hindsight analysis API endpoints."""
import logging
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("eval_router")

router = APIRouter(prefix="/api/eval", tags=["eval"])

_eval_logger = None


def init_eval_router(eval_logger):
    """Called by server.py to inject the eval logger."""
    global _eval_logger
    _eval_logger = eval_logger


@router.get("/summary")
async def get_summary():
    """Overall performance by strategy_type."""
    if _eval_logger is None:
        return JSONResponse(content={})
    return JSONResponse(content=_eval_logger.get_summary())


@router.get("/missed")
async def get_missed(strategy_type: str | None = None,
                     min_hypothetical_pnl: float = Query(default=5.0)):
    """Skipped opportunities that would have been profitable."""
    if _eval_logger is None:
        return JSONResponse(content=[])
    return JSONResponse(content=_eval_logger.get_missed_opportunities(
        strategy_type=strategy_type, min_pnl=min_hypothetical_pnl))


@router.get("/calibration")
async def get_calibration():
    """For each action_reason, how often it led to a correct skip vs missed opportunity."""
    if _eval_logger is None:
        return JSONResponse(content={})
    return JSONResponse(content=_eval_logger.get_calibration())


@router.get("/details/{opportunity_id}")
async def get_details(opportunity_id: str):
    """Full entry for a specific opportunity."""
    if _eval_logger is None:
        return JSONResponse(content={"error": "Not initialized"}, status_code=503)
    details = _eval_logger.get_details(opportunity_id)
    if not details:
        return JSONResponse(content={"error": "Not found"}, status_code=404)
    return JSONResponse(content=details)
```

- [ ] **Step 2: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/eval_router.py
git commit -m "feat(eval): add API router for hindsight analysis"
```

---

### Task 13: Server Wiring

**Files:**
- Modify: `src/server.py`

- [ ] **Step 1: Add imports**

At the top of `server.py`, in the arbitrage imports block (around line 42), add:

```python
try:
    from political.analyzer import PoliticalAnalyzer
    from political.router import router as political_router, init_political_router
    _POLITICAL_AVAILABLE = True
except ImportError:
    _POLITICAL_AVAILABLE = False

try:
    from eval_logger import EvalLogger
    from eval_router import router as eval_router, init_eval_router
    _EVAL_AVAILABLE = True
except ImportError:
    _EVAL_AVAILABLE = False
```

- [ ] **Step 2: Wire PoliticalAnalyzer in lifespan**

In `lifespan()`, after the auto trader is started (around line 294), add:

```python
            # Political synthetic analyzer
            _political_analyzer = None
            if _POLITICAL_AVAILABLE and _ARBITRAGE_AVAILABLE:
                try:
                    _political_analyzer = PoliticalAnalyzer(
                        scanner=arb_scanner,
                        ai_advisor=ai,
                        decision_logger=decision_log,
                        auto_trader=_auto_trader,
                    )
                    _political_analyzer.start()
                    _auto_trader.set_political_analyzer(_political_analyzer)
                    init_political_router(_political_analyzer)
                    logger.info("Political analyzer started (15-min cycle)")
                except Exception as e:
                    logger.warning("Political analyzer init failed: %s", e)
```

- [ ] **Step 3: Wire EvalLogger**

After the political analyzer block:

```python
            # Eval logger for hindsight analysis
            _eval_log = None
            _backfill_task = None
            if _EVAL_AVAILABLE:
                _eval_log = EvalLogger()
                init_eval_router(_eval_log)
                if _POLITICAL_AVAILABLE and _political_analyzer:
                    from political.router import set_eval_logger
                    set_eval_logger(_eval_log)
                # Start hourly backfill task for skipped opportunity resolution checks
                async def _backfill_loop():
                    while True:
                        await asyncio.sleep(3600)  # 1 hour
                        try:
                            # TODO(implementer): Add resolution checking via platform APIs
                            # For now, log unresolved count for monitoring
                            unresolved = _eval_log.get_unresolved_skips()
                            if unresolved:
                                logger.info("Eval backfill: %d unresolved skipped opportunities", len(unresolved))
                        except Exception as e:
                            logger.warning("Eval backfill error: %s", e)
                _backfill_task = asyncio.create_task(_backfill_loop())
                logger.info("Eval logger initialized with hourly backfill")
```

- [ ] **Step 4: Register routers**

After the existing router includes (around line 347), add:

```python
if _POLITICAL_AVAILABLE:
    app.include_router(political_router)
if _EVAL_AVAILABLE:
    app.include_router(eval_router)
```

- [ ] **Step 5: Add shutdown cleanup**

In the shutdown block of lifespan (around line 315), add:

```python
            if _political_analyzer:
                _political_analyzer.stop()
            if _backfill_task:
                _backfill_task.cancel()
```

- [ ] **Step 6: Test server starts**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout/src && python -c "from server import app; print('OK')"`
Expected: "OK" (no import errors)

- [ ] **Step 7: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/server.py
git commit -m "feat: wire political analyzer and eval logger into server lifecycle"
```

---

### Task 14: Decision Log Extensions

**Files:**
- Modify: `src/positions/decision_log.py`

- [ ] **Step 1: Add political analysis log method**

After the existing news scanner methods in `decision_log.py`:

```python
    # ── Political Analyzer decisions ──────────────────────────────────────

    def log_political_analysis(self, cluster_id: str, race: str,
                                contracts_count: int, relationships_count: int,
                                strategies_found: int, strategies_valid: int,
                                cache_hit: bool, elapsed_ms: int):
        self._write({
            "type": "political_synthetic_analysis",
            "cluster_id": cluster_id,
            "race": race[:100],
            "contracts_count": contracts_count,
            "relationships_count": relationships_count,
            "strategies_found": strategies_found,
            "strategies_valid": strategies_valid,
            "cache_hit": cache_hit,
            "elapsed_ms": elapsed_ms,
        })
```

- [ ] **Step 2: Add political_event_resolved to _KNOWN_TRIGGERS in ai_advisor.py**

Verify that `"political_event_resolved"` exists in the `_KNOWN_TRIGGERS` set in `src/positions/ai_advisor.py` (line ~202). If missing, add it.

- [ ] **Step 3: Commit**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/positions/decision_log.py
git commit -m "feat(political): add political analysis logging to decision log"
```

---

## Chunk 5: Final Integration Tests

### Task 15: Integration Tests

**Files:**
- Run all existing tests to verify no regressions

- [ ] **Step 1: Run full test suite**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Run server startup test**

Run: `cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout/src && timeout 10 python -m uvicorn server:app --host 127.0.0.1 --port 8501 2>&1 | head -20`
Expected: Server starts without errors, political analyzer and eval logger initialized

- [ ] **Step 3: Test political API endpoints**

```bash
curl.exe http://127.0.0.1:8500/api/political/clusters
curl.exe http://127.0.0.1:8500/api/political/strategies
curl.exe http://127.0.0.1:8500/api/eval/summary
```
Expected: JSON responses (empty arrays/objects initially, no errors)

- [ ] **Step 4: Final commit and push**

```bash
cd C:/Users/afoma/.openclaw/workspace/projects/arbitrout
git add src/political/ src/eval_logger.py src/eval_router.py src/server.py src/positions/position_manager.py src/positions/exit_engine.py src/positions/auto_trader.py src/positions/decision_log.py tests/test_political_*.py tests/test_eval_logger.py
git commit -m "feat: complete political synthetic analysis and eval logging system"
git push
```
