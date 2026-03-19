"""Event matcher — groups identical events across platforms using entity extraction."""
import hashlib
import json
import logging
import re
from pathlib import Path

from adapters.models import NormalizedEvent, MatchedEvent

logger = logging.getLogger("event_matcher")

DATA_DIR = Path(__file__).parent / "data" / "arbitrage"


# ============================================================
# CRYPTO TICKER NORMALIZATION
# ============================================================
_TICKER_ALIASES = {
    "bitcoin": "BTC", "btc": "BTC", "$btc": "BTC",
    "ethereum": "ETH", "eth": "ETH", "$eth": "ETH", "ether": "ETH",
    "xrp": "XRP", "$xrp": "XRP", "ripple": "XRP",
    "solana": "SOL", "sol": "SOL", "$sol": "SOL",
    "dogecoin": "DOGE", "doge": "DOGE", "$doge": "DOGE",
    "cardano": "ADA", "ada": "ADA", "$ada": "ADA",
    "polkadot": "DOT", "dot": "DOT", "$dot": "DOT",
    "avalanche": "AVAX", "avax": "AVAX", "$avax": "AVAX",
    "chainlink": "LINK", "link": "LINK", "$link": "LINK",
    "polygon": "MATIC", "matic": "MATIC", "$matic": "MATIC",
    "litecoin": "LTC", "ltc": "LTC", "$ltc": "LTC",
    "bnb": "BNB", "$bnb": "BNB", "binance coin": "BNB",
    "sui": "SUI", "$sui": "SUI",
    "pepe": "PEPE", "$pepe": "PEPE",
}

_KNOWN_TICKERS = set(_TICKER_ALIASES.values())


# ============================================================
# ENTITY EXTRACTION
# ============================================================
_COMMON_WORDS = {
    "will", "the", "a", "an", "be", "by", "in", "of", "for", "to", "on",
    "and", "or", "is", "it", "at", "this", "that", "if", "which", "who",
    "what", "when", "how", "win", "price", "above", "below", "before",
    "after", "between", "from", "than", "more", "most", "less", "over",
    "under", "next", "end", "year", "day", "week", "month", "date",
    "market", "prediction", "contract", "shares", "event", "odds",
    "probability", "chance", "likelihood", "outcome", "result", "winner",
    "election", "vote", "poll", "primary", "general", "runoff",
    "republican", "democrat", "democratic", "governor", "senator", "state",
    "presidential", "president", "cabinet", "house", "senate", "congress",
    "party", "yes", "no", "not", "reach", "exceed", "hit", "close",
    "open", "high", "low", "trading", "trade", "buy", "sell",
    "new", "city", "york", "los", "angeles", "san", "francisco",
    "temperature", "highest", "lowest", "weather", "degrees",
    "2024", "2025", "2026", "2027", "2028", "2029", "2030",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "q1", "q2", "q3", "q4",
}

_COUNTRIES = {
    "us", "usa", "america", "american", "united states",
    "uk", "britain", "british", "england",
    "china", "chinese", "russia", "russian", "ukraine", "ukrainian",
    "india", "indian", "japan", "japanese", "korea", "korean",
    "germany", "german", "france", "french", "brazil", "brazilian",
    "canada", "canadian", "mexico", "mexican", "australia", "australian",
    "israel", "israeli", "iran", "iranian", "turkey", "turkish",
    "italy", "italian", "spain", "spanish", "poland", "polish",
    "taiwan", "taiwanese", "argentina", "argentine",
}


def _extract_crypto(title: str) -> dict:
    """Extract crypto entities: ticker, price target(s), direction.

    Handles three market types:
    - Directional: "BTC above $74K" → price=74000, direction="above"
    - Range: "BTC between $74K and $76K" → price=75000 (midpoint),
             price_low=74000, price_high=76000, direction="between"
    - Plain: "BTC price on March 23" → price extracted, direction=None
    """
    lower = title.lower()
    result = {"ticker": None, "price": None, "direction": None,
              "price_low": None, "price_high": None}

    for alias, ticker in _TICKER_ALIASES.items():
        if len(alias) <= 3:
            if re.search(r'\b' + re.escape(alias) + r'\b', lower):
                result["ticker"] = ticker
                break
        else:
            if alias in lower:
                result["ticker"] = ticker
                break

    if not result["ticker"]:
        ticker_match = re.search(r'\$([A-Za-z]{2,6})\b', title)
        if ticker_match:
            t = ticker_match.group(1).upper()
            if t in _KNOWN_TICKERS:
                result["ticker"] = t

    # Check for "between X and Y" range pattern FIRST
    range_pat = r'between\s+\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)?\s*(?:and|[-–])\s*\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)?'
    range_match = re.search(range_pat, title, re.IGNORECASE)
    if range_match:
        low_str = range_match.group(1).replace(",", "")
        high_str = range_match.group(2).replace(",", "")
        low_price = float(low_str)
        high_price = float(high_str)
        # Handle K suffix
        range_text = title[range_match.start():range_match.end()]
        if re.search(r'(?:' + re.escape(low_str) + r')\s*(?:k|K)', range_text):
            low_price *= 1000
        if re.search(r'(?:' + re.escape(high_str) + r')\s*(?:k|K)', range_text):
            high_price *= 1000
        # Ensure low < high
        if low_price > high_price:
            low_price, high_price = high_price, low_price
        result["price_low"] = low_price
        result["price_high"] = high_price
        result["price"] = (low_price + high_price) / 2  # midpoint for matching
        result["direction"] = "between"
        return result

    # Standard single-price extraction
    price_patterns = [
        r'\$?([\d,]+(?:\.\d+)?)\s*(?:k|K)',
        r'\$\s*([\d,]+(?:\.\d+)?)',
        r'([\d,]+(?:\.\d+)?)\s*(?:dollars?|usd)',
    ]
    for pat in price_patterns:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            price_str = m.group(1).replace(",", "")
            price = float(price_str)
            if re.search(r'k\b', title[m.start():m.end()], re.IGNORECASE):
                price *= 1000
            result["price"] = price
            break

    if re.search(r'\b(above|over|exceed|surpass|reach|hit|higher)\b', lower):
        result["direction"] = "above"
    elif re.search(r'\b(below|under|drop|fall|lower|dip|sink|crash|plunge|decline)\b', lower):
        result["direction"] = "below"

    return result


def _extract_names(title: str) -> set:
    """Extract person names (capitalized proper nouns, 3+ chars)."""
    names = set()
    for m in re.finditer(r'\b([A-Z][a-z]{2,})\b', title):
        word = m.group(1)
        if word.lower() not in _COMMON_WORDS and word.lower() not in _COUNTRIES:
            names.add(word.lower())
    for m in re.finditer(r'\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b', title):
        first, last = m.group(1).lower(), m.group(2).lower()
        if first not in _COMMON_WORDS and last not in _COMMON_WORDS:
            names.add(first)
            names.add(last)
    return names


def _extract_countries(title: str) -> set:
    """Extract country/nationality mentions."""
    lower = title.lower()
    found = set()
    for c in _COUNTRIES:
        if re.search(r'\b' + re.escape(c) + r'\b', lower):
            found.add(c)
    return found


def _extract_quoted_terms(title: str) -> set:
    """Extract quoted terms like 'word' or "word"."""
    terms = set()
    for m in re.finditer(r'''['"\u2018\u2019\u201c\u201d]([^'"\u2018\u2019\u201c\u201d]{2,30})['"\u2018\u2019\u201c\u201d]''', title):
        terms.add(m.group(1).lower().strip())
    return terms


def _extract_key_terms(title: str) -> set:
    """Extract important non-stopword terms from the title."""
    clean = re.sub(r'[^\w\s\'-]', ' ', title.lower())
    words = clean.split()
    return {w for w in words if len(w) >= 3 and w not in _COMMON_WORDS}


def _extract_acronyms(title: str) -> set:
    """Extract uppercase acronyms/identifiers (2-10 chars) from a title.

    Catches party names (MSZP, TISZA, KDNP), org abbreviations (NATO, FIFA),
    and PredictIt contract identifiers that _extract_names misses.
    Also catches mixed-case identifiers like 'Fidesz' after the colon in
    PredictIt's 'Market: Contract' format.
    """
    acronyms = set()
    # Pure uppercase acronyms (2-10 chars)
    for m in re.finditer(r'\b([A-Z][A-Z0-9]{1,9})\b', title):
        word = m.group(1)
        if word.lower() not in _COMMON_WORDS and word.lower() not in _COUNTRIES:
            acronyms.add(word.lower())
    # After colon = PredictIt contract name — extract as distinguishing identifier
    if ':' in title:
        contract_part = title.split(':', 1)[1].strip()
        # Extract capitalized words from the contract portion
        for m in re.finditer(r'\b([A-Za-z][A-Za-z]{2,})\b', contract_part):
            word = m.group(1)
            if word.lower() not in _COMMON_WORDS and word.lower() not in _COUNTRIES:
                acronyms.add(word.lower())
    return acronyms


def extract_entities(title: str) -> dict:
    """Extract all entity types from a market title."""
    crypto = _extract_crypto(title)
    return {
        "crypto_ticker": crypto["ticker"],
        "crypto_price": crypto["price"],
        "crypto_direction": crypto["direction"],
        "crypto_price_low": crypto.get("price_low"),
        "crypto_price_high": crypto.get("price_high"),
        "names": _extract_names(title),
        "countries": _extract_countries(title),
        "quoted": _extract_quoted_terms(title),
        "key_terms": _extract_key_terms(title),
        "acronyms": _extract_acronyms(title),
    }


# ============================================================
# TWO-PHASE MATCHING
# ============================================================
def _entity_overlap_score(ent_a: dict, ent_b: dict) -> float:
    """Score how much two entity sets overlap. Returns 0.0-1.0."""
    score = 0.0
    max_score = 0.0

    if ent_a["crypto_ticker"] and ent_b["crypto_ticker"]:
        max_score += 3.0
        if ent_a["crypto_ticker"] == ent_b["crypto_ticker"]:
            score += 3.0
            if ent_a["crypto_direction"] and ent_b["crypto_direction"]:
                max_score += 1.0
                if ent_a["crypto_direction"] == ent_b["crypto_direction"]:
                    score += 1.0
            if ent_a["crypto_price"] and ent_b["crypto_price"]:
                max_score += 1.0
                ratio = min(ent_a["crypto_price"], ent_b["crypto_price"]) / max(ent_a["crypto_price"], ent_b["crypto_price"])
                if ratio >= 0.90:
                    score += ratio
    elif ent_a["crypto_ticker"] or ent_b["crypto_ticker"]:
        return 0.0

    names_a, names_b = ent_a["names"], ent_b["names"]
    if names_a and names_b:
        max_score += 2.0
        overlap = names_a & names_b
        if overlap:
            score += 2.0 * len(overlap) / max(len(names_a), len(names_b))

    countries_a, countries_b = ent_a["countries"], ent_b["countries"]
    if countries_a and countries_b:
        max_score += 1.0
        overlap = countries_a & countries_b
        if overlap:
            score += 1.0 * len(overlap) / max(len(countries_a), len(countries_b))

    quoted_a, quoted_b = ent_a["quoted"], ent_b["quoted"]
    if quoted_a and quoted_b:
        max_score += 2.0
        if quoted_a & quoted_b:
            score += 2.0

    terms_a, terms_b = ent_a["key_terms"], ent_b["key_terms"]
    if terms_a and terms_b:
        max_score += 2.0
        overlap = terms_a & terms_b
        ratio = len(overlap) / max(len(terms_a), len(terms_b))
        score += 2.0 * ratio

    if max_score == 0:
        return 0.0
    return score / max_score


def _is_interval_market(title: str) -> bool:
    """Detect Polymarket-style short-interval markets like 'Bitcoin Up or Down - 9:55PM-10:00PM'."""
    return bool(re.search(r'up or down\b', title.lower()))


def _passes_quick_filter(ent_a: dict, ent_b: dict, title_a: str, title_b: str) -> bool:
    """Phase 1: Quick check — must share at least one meaningful entity."""
    # Interval markets only match other interval markets
    int_a, int_b = _is_interval_market(title_a), _is_interval_market(title_b)
    if int_a != int_b:
        return False

    # Same crypto ticker
    if (ent_a["crypto_ticker"] and ent_b["crypto_ticker"]
            and ent_a["crypto_ticker"] == ent_b["crypto_ticker"]):
        dir_a = ent_a.get("crypto_direction")
        dir_b = ent_b.get("crypto_direction")
        pa, pb = ent_a["crypto_price"], ent_b["crypto_price"]

        # "between" (range) markets are structurally different from "above"/"below"
        # They can still be grouped for synthetic derivatives, but must be
        # explicitly recognized as different market types
        if pa and pb:
            ratio = min(pa, pb) / max(pa, pb)
            # Tight threshold for same-direction matches to prevent
            # grouping e.g. "BTC > $2000" with "BTC > $2200"
            # Range/between markets use 0.90 since they cover wider spans
            if dir_a == dir_b and dir_a in ("above", "below"):
                if ratio < 0.98:
                    return False
            elif ratio < 0.90:
                return False
        elif pa or pb:
            return False
        return True

    if ent_a["crypto_ticker"] or ent_b["crypto_ticker"]:
        return False

    # Check for conflicting identifiers: if both events have unique
    # acronyms/identifiers that the other lacks, they're different outcomes
    # of the same market (e.g., MSZP vs TISZA in Hungarian election)
    acr_a, acr_b = ent_a.get("acronyms", set()), ent_b.get("acronyms", set())
    if acr_a and acr_b:
        shared_acr = acr_a & acr_b
        unique_a = acr_a - acr_b - ent_a.get("countries", set())
        unique_b = acr_b - acr_a - ent_b.get("countries", set())
        if unique_a and unique_b and not shared_acr:
            # Both have identifiers the other lacks = different contracts
            return False

    # Shared person name — need at least 1 shared key_term beyond the name
    # OR 2+ shared names (multiple person names = strong signal)
    shared_names = ent_a["names"] & ent_b["names"]
    if shared_names:
        if len(shared_names) >= 2:
            return True
        shared_terms = ent_a["key_terms"] & ent_b["key_terms"]
        context_terms = shared_terms - shared_names
        if len(context_terms) >= 1:
            return True

    # Shared quoted term
    if ent_a["quoted"] & ent_b["quoted"]:
        return True

    # Shared country + significant key term overlap
    if ent_a["countries"] & ent_b["countries"]:
        shared_terms = ent_a["key_terms"] & ent_b["key_terms"]
        if len(shared_terms) >= 3:
            return True

    # Very strong key term overlap
    shared = ent_a["key_terms"] & ent_b["key_terms"]
    if len(shared) >= 5:
        return True

    return False


# ============================================================
# PREDICTIT TITLE HANDLING
# ============================================================
def _clean_predictit_title(title: str) -> str:
    """Keep PredictIt's full 'Market: Contract' title for accurate matching.

    Previously stripped to just the market name, but that caused false matches:
    'Who will win most seats: Fidesz' and 'Who will win most seats: MSZP'
    would both become 'Who will win most seats' and match the same external events.
    """
    return title


def _normalize_title(title: str) -> str:
    """Normalize a market title for comparison."""
    text = title.lower().strip()
    for prefix in ["will ", "what ", "which ", "when ", "how ", "is ", "are ", "does "]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.rstrip("?.!")
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _title_hash(title: str) -> str:
    return hashlib.md5(_normalize_title(title).encode()).hexdigest()[:12]


# ============================================================
# EXPIRY CHECK
# ============================================================
def _parse_expiry_date(s: str):
    """Parse an expiry string into a datetime, supporting multiple formats."""
    from datetime import datetime
    s = s.strip()
    # ISO format: "2026-03-19" or "2026-03-19T..."
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    # Limitless format: "Mar 19, 2026" or "March 19, 2026"
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass
    return None


def _expiry_compatible(a: str, b: str, max_days: int = 7) -> bool:
    if a == "ongoing" or b == "ongoing":
        return True
    da = _parse_expiry_date(a)
    db = _parse_expiry_date(b)
    if da and db:
        return abs((da - db).days) <= max_days
    # If we can't parse either date, allow the match (conservative)
    return True


# ============================================================
# MANUAL LINKS
# ============================================================
def _load_manual_links() -> list[dict]:
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
MATCH_THRESHOLD = 0.45


def match_events(events: list[NormalizedEvent]) -> list[MatchedEvent]:
    """Group events into MatchedEvent clusters using entity-based matching.

    Algorithm:
    1. Apply manual links first (highest priority)
    2. Extract entities from all event titles
    3. Phase 1: Quick filter — must share a key entity (name, ticker, etc.)
    4. Phase 2: Detailed scoring on candidates that pass Phase 1
    5. Union-Find clustering on matches above threshold
    """
    if not events:
        return []

    # --- Phase 0: Manual links ---
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

    # --- Phase 1: Extract entities for all unlinked events ---
    unlinked = [e for e in events if f"{e.platform}:{e.event_id}" not in linked_ids]

    effective_titles = []
    for ev in unlinked:
        if ev.platform == "predictit":
            effective_titles.append(_clean_predictit_title(ev.title))
        else:
            effective_titles.append(ev.title)

    entities = [extract_entities(t) for t in effective_titles]

    # --- Phase 2: Two-phase matching with Union-Find ---
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

    match_count = 0
    for i in range(len(unlinked)):
        for j in range(i + 1, len(unlinked)):
            if unlinked[i].platform == unlinked[j].platform:
                continue
            if not _passes_quick_filter(entities[i], entities[j], effective_titles[i], effective_titles[j]):
                continue
            score = _entity_overlap_score(entities[i], entities[j])
            if score < MATCH_THRESHOLD:
                continue
            cat_i, cat_j = unlinked[i].category, unlinked[j].category
            if cat_i != cat_j and cat_i != "culture" and cat_j != "culture":
                continue
            if not _expiry_compatible(unlinked[i].expiry, unlinked[j].expiry):
                continue
            union(i, j)
            match_count += 1

    logger.info("Entity matching: %d events, %d cross-platform matches found", len(unlinked), match_count)

    # Build clusters
    clusters: dict[int, list[int]] = {}
    for i in range(len(unlinked)):
        root = find(i)
        clusters.setdefault(root, []).append(i)

    # --- Phase 2.5: Split mega-clusters ---
    # If a cluster has crypto markets with >5% price divergence, split them
    # This prevents Union-Find transitivity from merging A↔B↔C where A and C
    # are actually different price targets
    split_clusters: dict[int, list[int]] = {}
    next_id = max(clusters.keys()) + 1 if clusters else 0
    for root, indices in clusters.items():
        if len(indices) <= 2:
            split_clusters[root] = indices
            continue
        # Check if this is a crypto cluster with price data
        crypto_prices = []
        for idx in indices:
            p = entities[idx].get("crypto_price")
            if p:
                crypto_prices.append((idx, p))
        if len(crypto_prices) < 2:
            split_clusters[root] = indices
            continue
        # Group by price proximity: markets within 5% of each other
        price_groups: list[list[int]] = []
        for idx, price in crypto_prices:
            placed = False
            for group in price_groups:
                ref_price = entities[group[0]].get("crypto_price", 0)
                if ref_price > 0 and min(price, ref_price) / max(price, ref_price) >= 0.95:
                    group.append(idx)
                    placed = True
                    break
            if not placed:
                price_groups.append([idx])
        # Non-crypto markets in the cluster go into the largest group
        non_crypto = [i for i in indices if entities[i].get("crypto_price") is None]
        if price_groups:
            largest = max(price_groups, key=len)
            largest.extend(non_crypto)
        if len(price_groups) <= 1:
            split_clusters[root] = indices
        else:
            for group in price_groups:
                split_clusters[next_id] = group
                next_id += 1
    clusters = split_clusters

    # --- Phase 3: Build MatchedEvent objects ---
    results: list[MatchedEvent] = []

    for link_id, group in manual_groups.items():
        results.append(MatchedEvent(
            match_id=link_id,
            canonical_title=group[0].title,
            category=group[0].category,
            expiry=group[0].expiry,
            markets=group,
            match_type="manual",
        ))

    for root, indices in clusters.items():
        cluster_events = [unlinked[i] for i in indices]
        platforms = set(e.platform for e in cluster_events)

        if len(platforms) < 2:
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
    links = _load_manual_links()
    link_id = f"manual-{hashlib.md5(':'.join(sorted(event_ids)).encode()).hexdigest()[:8]}"
    for existing in links:
        if set(existing.get("event_ids", [])) == set(event_ids):
            return existing
    link = {"link_id": link_id, "event_ids": event_ids}
    links.append(link)
    _save_manual_links(links)
    return link


def remove_manual_link(link_id: str) -> bool:
    links = _load_manual_links()
    before = len(links)
    links = [l for l in links if l.get("link_id") != link_id]
    if len(links) < before:
        _save_manual_links(links)
        return True
    return False
