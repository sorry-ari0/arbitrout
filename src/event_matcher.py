"""Event matcher — groups identical events across platforms."""
import hashlib
import json
import logging
import re
from pathlib import Path

from adapters.models import NormalizedEvent, MatchedEvent

logger = logging.getLogger("event_matcher")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# TEXT NORMALIZATION
# ============================================================
_STRIP_PREFIXES = ["will ", "what ", "which ", "when ", "how ", "is ", "are ", "does "]
_STRIP_SUFFIXES = ["?", ".", "!"]


def _normalize_title(title: str) -> str:
    """Normalize a market title for comparison."""
    text = title.lower().strip()
    # Remove common prefixes
    for prefix in _STRIP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    # Remove trailing punctuation
    for suffix in _STRIP_SUFFIXES:
        text = text.rstrip(suffix)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _title_hash(title: str) -> str:
    """Short hash of normalized title for ID generation."""
    return hashlib.md5(_normalize_title(title).encode()).hexdigest()[:12]


# ============================================================
# FUZZY MATCHING
# ============================================================
_STOPWORDS = {
    "will", "the", "a", "an", "be", "by", "in", "of", "for", "to", "on",
    "and", "or", "is", "it", "at", "this", "that", "if", "which", "who",
    "what", "when", "how", "win", "election", "party", "market", "price",
    "next", "after", "before", "between", "from", "than", "more", "most",
    "republican", "democrat", "democratic", "governor", "senator", "state",
    "primary", "general", "runoff", "vote", "year",
    "2024", "2025", "2026", "2027", "2028", "2029", "2030",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}


def _extract_entities(title: str) -> set[str]:
    """Extract likely entity words (names, places, specific subjects).
    Filters out generic political/market stopwords to improve match quality."""
    words = set(title.lower().split())
    return words - _STOPWORDS


def _fuzzy_score(a: str, b: str) -> float:
    """Score similarity between two titles using thefuzz + entity bonus.
    Entity overlap (proper nouns, specific subjects) boosts the score.
    No entity overlap applies a penalty to prevent false matches on
    generic political phrases like 'Republican governor 2026'."""
    entities_a = _extract_entities(a)
    entities_b = _extract_entities(b)
    entity_overlap = len(entities_a & entities_b) if entities_a and entities_b else 0
    entity_max = max(len(entities_a), len(entities_b), 1)
    entity_ratio = entity_overlap / entity_max

    try:
        from thefuzz import fuzz
        score1 = fuzz.token_sort_ratio(a, b) / 100.0
        score2 = fuzz.partial_ratio(a, b) / 100.0
        fuzzy = 0.6 * score1 + 0.4 * score2
    except ImportError:
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        fuzzy = len(intersection) / len(union)

    # Blend: 70% fuzzy score + 30% entity overlap
    # This ensures high fuzzy scores on generic text get penalized
    # without completely blocking matches that have some entity overlap
    return 0.7 * fuzzy + 0.3 * entity_ratio


def _expiry_compatible(a: str, b: str, max_days: int = 7) -> bool:
    """Check if two expiry dates are within max_days of each other."""
    if a == "ongoing" or b == "ongoing":
        return True  # ongoing matches anything
    try:
        from datetime import datetime
        da = datetime.strptime(a[:10], "%Y-%m-%d")
        db = datetime.strptime(b[:10], "%Y-%m-%d")
        return abs((da - db).days) <= max_days
    except (ValueError, TypeError):
        return True  # if we can't parse, don't block match


# ============================================================
# MANUAL LINKS
# ============================================================
def _load_manual_links() -> list[dict]:
    """Load manually linked events from JSON file."""
    f = DATA_DIR / "manual_links.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_manual_links(links: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "manual_links.json").write_text(json.dumps(links, indent=2))


# ============================================================
# MATCH ENGINE
# ============================================================
MATCH_THRESHOLD = 0.72  # fuzzy + entity blend (entity overlap prevents false generic matches)


def match_events(events: list[NormalizedEvent]) -> list[MatchedEvent]:
    """Group events into MatchedEvent clusters.

    Algorithm:
    1. Apply manual links first (highest priority)
    2. Group by platform to avoid self-matching
    3. For each cross-platform pair, compute fuzzy score
    4. If score >= threshold AND category compatible AND expiry
       compatible, merge into same MatchedEvent
    """
    if not events:
        return []

    # --- Phase 1: Manual links ---
    manual_links = _load_manual_links()
    manual_groups: dict[str, list[NormalizedEvent]] = {}
    linked_ids: set[str] = set()

    for link in manual_links:
        link_id = link.get("link_id", "")
        event_ids = set(link.get("event_ids", []))
        group: list[NormalizedEvent] = []
        for ev in events:
            key = f"{ev.platform}:{ev.event_id}"
            if key in event_ids:
                group.append(ev)
                linked_ids.add(key)
        if len(group) >= 2:
            manual_groups[link_id] = group

    # --- Phase 2: Auto-match remaining events ---
    unlinked = [e for e in events if f"{e.platform}:{e.event_id}" not in linked_ids]

    # Normalize titles
    norm_titles = [_normalize_title(e.title) for e in unlinked]

    # Union-Find for clustering
    parent = list(range(len(unlinked)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Compare all cross-platform pairs
    for i in range(len(unlinked)):
        for j in range(i + 1, len(unlinked)):
            # Skip same-platform
            if unlinked[i].platform == unlinked[j].platform:
                continue
            # Fuzzy score
            score = _fuzzy_score(norm_titles[i], norm_titles[j])
            if score < MATCH_THRESHOLD:
                continue
            # Category check (same category, or one is "culture" catch-all)
            cat_i, cat_j = unlinked[i].category, unlinked[j].category
            if cat_i != cat_j and cat_i != "culture" and cat_j != "culture":
                continue
            # Expiry check
            if not _expiry_compatible(unlinked[i].expiry, unlinked[j].expiry):
                continue
            union(i, j)

    # Build clusters
    clusters: dict[int, list[int]] = {}
    for i in range(len(unlinked)):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    # --- Phase 3: Build MatchedEvent objects ---
    results: list[MatchedEvent] = []

    # Manual groups
    for link_id, group in manual_groups.items():
        results.append(MatchedEvent(
            match_id=link_id,
            canonical_title=group[0].title,
            category=group[0].category,
            expiry=group[0].expiry,
            markets=group,
            match_type="manual",
        ))

    # Auto clusters (only multi-platform)
    for root, indices in clusters.items():
        cluster_events = [unlinked[i] for i in indices]
        platforms = set(e.platform for e in cluster_events)
        if len(platforms) < 2:
            # Single platform — still include as standalone for browsing
            for ev in cluster_events:
                results.append(MatchedEvent(
                    match_id=f"auto-{_title_hash(ev.title)}",
                    canonical_title=ev.title,
                    category=ev.category,
                    expiry=ev.expiry,
                    markets=[ev],
                    match_type="auto",
                ))
            continue

        # Pick best title (longest, most descriptive)
        best_title = max(cluster_events, key=lambda e: len(e.title)).title
        best_category = max(
            set(e.category for e in cluster_events),
            key=lambda c: sum(1 for e in cluster_events if e.category == c)
        )
        best_expiry = cluster_events[0].expiry

        results.append(MatchedEvent(
            match_id=f"auto-{_title_hash(best_title)}",
            canonical_title=best_title,
            category=best_category,
            expiry=best_expiry,
            markets=cluster_events,
            match_type="auto",
        ))

    return results


# ============================================================
# MANUAL LINK API
# ============================================================
def add_manual_link(event_ids: list[str]) -> dict:
    """Add a manual link between events. event_ids are 'platform:event_id' strings."""
    links = _load_manual_links()
    link_id = f"manual-{hashlib.md5(':'.join(sorted(event_ids)).encode()).hexdigest()[:8]}"
    # Check for duplicate
    for existing in links:
        if set(existing.get("event_ids", [])) == set(event_ids):
            return existing
    link = {"link_id": link_id, "event_ids": event_ids}
    links.append(link)
    _save_manual_links(links)
    return link


def remove_manual_link(link_id: str) -> bool:
    """Remove a manual link by ID."""
    links = _load_manual_links()
    before = len(links)
    links = [l for l in links if l.get("link_id") != link_id]
    if len(links) < before:
        _save_manual_links(links)
        return True
    return False
