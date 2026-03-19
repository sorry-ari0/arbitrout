"""Political contract clustering — groups classified contracts by race+state.

Takes PoliticalContractInfo objects from the classifier and groups them into
PoliticalCluster objects using normalized race keys. Contracts referring to the
same race with different surface forms (e.g. "TX Senate", "Texas Senate",
"Senate TX") are grouped together via fuzzy normalization.
"""
import re
from collections import defaultdict
from typing import Optional

from political.models import PoliticalContractInfo, PoliticalCluster


# ============================================================
# STATE NAMES → ABBREVIATION LOOKUP (for normalization)
# ============================================================
_STATE_NAMES: dict[str, str] = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}

# Filler words removed during normalization
_FILLER_WORDS = {"the", "race", "election", "seat", "special"}


# ============================================================
# NORMALIZATION
# ============================================================

def _normalize_race(race: str, state: Optional[str] = None) -> str:
    """Normalize a race string for grouping.

    Steps:
    1. Lowercase the input
    2. Replace full state names with their two-letter abbreviations
    3. Remove filler words (the, race, election, seat, special)
    4. Split into tokens, sort alphabetically, join with "-"

    Result: "TX Senate", "Texas Senate", "Senate TX" all become "senate-tx".

    Args:
        race: The race description (e.g. "TX Senate", "Texas Senate").
        state: Optional two-letter state abbreviation (not used directly in
               normalization but available for callers).

    Returns:
        Normalized key string for grouping.
    """
    text = race.lower().strip()

    # Replace full state names with abbreviations (longest-first to handle
    # "west virginia" before "virginia", "new york" before "york", etc.)
    for name in sorted(_STATE_NAMES, key=len, reverse=True):
        if name in text:
            text = text.replace(name, _STATE_NAMES[name])

    # Remove non-alphanumeric characters (keep spaces for tokenization)
    text = re.sub(r"[^a-z0-9\s]", "", text)

    # Split into tokens
    tokens = text.split()

    # Remove filler words
    tokens = [t for t in tokens if t not in _FILLER_WORDS]

    # Sort alphabetically and join
    tokens.sort()
    return "-".join(tokens)


# ============================================================
# CLUSTER BUILDER
# ============================================================

def build_clusters(contracts: list[PoliticalContractInfo]) -> list[PoliticalCluster]:
    """Group classified political contracts into clusters by race+state.

    Groups contracts using _normalize_race() as the grouping key. Contracts
    with race=None are skipped. Groups with fewer than 2 contracts are
    filtered out (need at least 2 contracts to form a synthetic derivative).

    Args:
        contracts: List of classified PoliticalContractInfo objects.

    Returns:
        List of PoliticalCluster objects, each containing >= 2 contracts
        for the same normalized race.
    """
    # Group contracts by normalized race key
    groups: dict[str, list[PoliticalContractInfo]] = defaultdict(list)

    for contract in contracts:
        if contract.race is None:
            continue
        key = _normalize_race(contract.race, contract.state)
        groups[key].append(contract)

    # Build cluster objects, filtering out groups with < 2 contracts
    clusters: list[PoliticalCluster] = []

    for key, group_contracts in groups.items():
        if len(group_contracts) < 2:
            continue

        # Deduplicate events by event_id
        seen_event_ids: set[str] = set()
        matched_events = []
        for c in group_contracts:
            eid = c.event.event_id
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                matched_events.append(c.event)

        # Use the first contract's race/state as representative values
        representative = group_contracts[0]

        cluster = PoliticalCluster(
            cluster_id=f"{key}-2026",
            race=representative.race,
            state=representative.state,
            contracts=group_contracts,
            matched_events=matched_events,
        )
        clusters.append(cluster)

    return clusters
