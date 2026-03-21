"""Rule-based contract classifier for political prediction markets.

Classifies NormalizedEvent titles into one of 7 contract types using regex
patterns. Political contract titles are formulaic across Polymarket/Kalshi/
PredictIt, making regex reliable for structured extraction.

Classification order matters — margin_bracket MUST be checked before
candidate_win since "wins by" contains "wins".
"""
import re
from typing import Optional

from adapters.models import NormalizedEvent
from political.models import PoliticalContractInfo


# ============================================================
# US STATE LOOKUP
# ============================================================
_STATE_ABBREVS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


# ============================================================
# REGEX PATTERNS
# ============================================================

# margin_bracket: "wins by >5%", "margin greater than 10%", "wins by more than 3%"
_MARGIN_RE = re.compile(
    r"(?:wins?\s+by|margin)\s+(?:(?:more|greater|less)\s+than\s+|[><]=?\s*)"
    r"(?P<threshold>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# vote_share: "gets >48%", "vote share above 52%", "receives more than 45%"
_VOTE_SHARE_RE = re.compile(
    r"(?:gets?|receives?|vote\s+share)\s+(?:(?:more|greater|above)\s+(?:than\s+)?|[><]=?\s*)"
    r"(?P<threshold>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# matchup: "Name vs Name", "Name versus Name", "Name v. Name"
_MATCHUP_RE = re.compile(
    r"(?P<cand1>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:[Vv]s?\.?|[Vv]ersus)\s+"
    r"(?P<cand2>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)",
)

# party_outcome: "Democratic candidate wins TX Senate", "GOP holds Georgia",
#                "Republican takes PA Governor"
_PARTY_OUTCOME_RE = re.compile(
    r"(?P<party>Democrat(?:ic)?|Republican|GOP|Dem|Rep)\s+"
    r"(?:candidate\s+)?(?:wins?|holds?|takes?|flips?|keeps?)\s+"
    r"(?P<race>.+)",
    re.IGNORECASE,
)

# candidate_win: "Name wins Race", "Name to win Race", "Name will win Race",
#                "Name winning Race"
# NOTE: Cannot use re.IGNORECASE — the candidate group uses [A-Z] to detect
# proper nouns, and IGNORECASE would let it greedily eat "to", "will", etc.
# The verb alternatives are written case-insensitively via character classes.
_CANDIDATE_WIN_RE = re.compile(
    r"(?P<candidate>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+"
    r"(?:[Ww]ins?|[Tt]o\s+[Ww]in|[Ww]ill\s+[Ww]in|[Ww]inning)\s+"
    r"(?P<race>.+)",
)


# ============================================================
# CRYPTO EVENT DETECTION
# ============================================================

_CRYPTO_ASSET_RE = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|ether|solana|sol|xrp|ripple|"
    r"dogecoin|doge|cardano|ada|avalanche|avax|chainlink|link|"
    r"polkadot|dot|polygon|pol|crypto(?:currency)?)\b",
    re.IGNORECASE,
)

_CRYPTO_TICKER_MAP: dict[str, str] = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH", "ether": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "dogecoin": "DOGE", "doge": "DOGE",
    "cardano": "ADA", "ada": "ADA",
    "avalanche": "AVAX", "avax": "AVAX",
    "chainlink": "LINK", "link": "LINK",
    "polkadot": "DOT", "dot": "DOT",
    "polygon": "POL", "pol": "POL",
}

_CRYPTO_REGULATORY_RE = re.compile(
    r"\b(sec|cftc|regulat\w*|classify|classifies|security|securities|"
    r"etf|ban|bans|banned|restrict\w*)\b",
    re.IGNORECASE,
)

_CRYPTO_PRICE_RE = re.compile(
    r"\b(above|below|reach|reaches|hit|hits|exceed|exceeds)\b.*?"
    r"\$[\d,]+",
    re.IGNORECASE,
)

_DOLLAR_AMOUNT_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)")

_CRYPTO_TECHNICAL_RE = re.compile(
    r"\b(hack|hacked|exploit|exploited|upgrade|fork|forked|"
    r"halving|merge|merged)\b",
    re.IGNORECASE,
)

_ETF_APPROVAL_RE = re.compile(
    r"\betf\b.*?\b(approv\w*|reject\w*|denied|deny)\b",
    re.IGNORECASE,
)


def _classify_crypto(title: str) -> dict | None:
    """Check if title is a crypto_event. Returns extracted fields or None."""
    asset_matches = _CRYPTO_ASSET_RE.findall(title)
    if not asset_matches:
        return None

    ticker = None
    for match in asset_matches:
        t = _CRYPTO_TICKER_MAP.get(match.lower())
        if t:
            ticker = t
            break

    if not ticker:
        return None

    is_regulatory = bool(_CRYPTO_REGULATORY_RE.search(title))
    is_price = bool(_CRYPTO_PRICE_RE.search(title))
    is_technical = bool(_CRYPTO_TECHNICAL_RE.search(title))

    if not (is_regulatory or is_price or is_technical):
        return None

    if is_price:
        event_category = "price_target"
    elif is_regulatory:
        event_category = "regulatory"
    else:
        event_category = "technical"

    threshold = None
    if is_price:
        dollar_match = _DOLLAR_AMOUNT_RE.search(title)
        if dollar_match:
            threshold = float(dollar_match.group(1).replace(",", ""))

    direction = "neutral"
    title_lower = title.lower()
    etf_match = _ETF_APPROVAL_RE.search(title)
    if etf_match:
        verb = etf_match.group(1).lower()
        if verb.startswith("approv"):
            direction = "positive"
        else:
            direction = "negative"
    elif event_category == "price_target":
        if re.search(r"\b(above|reach|reaches|hit|hits|exceed|exceeds)\b", title_lower):
            direction = "positive"
        elif re.search(r"\b(below|drop|drops|fall|falls)\b", title_lower):
            direction = "negative"
    elif event_category == "regulatory":
        if re.search(r"\b(ban|bans|banned|restrict|classif(?:y|ies))\b", title_lower):
            direction = "negative"
        elif re.search(r"\b(approv\w*)\b", title_lower):
            direction = "positive"
    elif event_category == "technical":
        if re.search(r"\b(hack|hacked|exploit|exploited)\b", title_lower):
            direction = "negative"
        elif re.search(r"\b(upgrade|merge|merged|halving)\b", title_lower):
            direction = "positive"

    return {
        "crypto_asset": ticker,
        "event_category": event_category,
        "crypto_direction": direction,
        "crypto_threshold": threshold,
    }


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def _extract_state(text: str) -> Optional[str]:
    """Extract a US state from text, returning two-letter abbreviation or None.

    Checks for:
    - Two-letter state abbreviations (e.g. "TX", "CA")
    - Full state names (e.g. "Texas", "California")
    """
    # Check for two-letter abbreviation (word boundary match)
    for match in re.finditer(r"\b([A-Z]{2})\b", text):
        abbrev = match.group(1)
        if abbrev in _STATE_ABBREVS:
            return abbrev

    # Check for full state names (case-insensitive)
    text_lower = text.lower()
    for name, abbrev in _STATE_NAMES.items():
        if name in text_lower:
            return abbrev

    return None


def _extract_party(text: str) -> Optional[str]:
    """Normalize party references to 'dem' or 'gop', or None."""
    text_lower = text.lower()
    if any(p in text_lower for p in ("democrat", "democratic", " dem ", "dem ")):
        return "dem"
    if any(p in text_lower for p in ("republican", "gop", " rep ", "rep ")):
        return "gop"
    # Check at start of string too
    if text_lower.startswith("dem"):
        return "dem"
    if text_lower.startswith("rep") or text_lower.startswith("gop"):
        return "gop"
    return None


def _clean_race(text: str) -> str:
    """Clean and normalize a race description.

    Strips trailing punctuation, extra whitespace, and normalizes common terms.
    """
    text = text.strip()
    # Remove trailing punctuation
    text = re.sub(r"[?.!,;:]+$", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ============================================================
# MAIN CLASSIFIER
# ============================================================

def classify_contract(event: NormalizedEvent) -> PoliticalContractInfo:
    """Classify a political prediction market contract by its title.

    Classification order (checked first to last):
    1. margin_bracket  — "wins by >5%" (MUST be before candidate_win)
    2. vote_share      — "gets >48%"
    3. matchup         — "Name vs Name"
    4. party_outcome   — "Democratic candidate wins TX Senate"
    5. candidate_win   — "Name wins Race"
    6. crypto_event    — "Bitcoin above $150K", "SEC classify Ethereum"
    7. yes_no_binary   — fallback for anything unclassifiable

    Args:
        event: A NormalizedEvent with a political contract title.

    Returns:
        PoliticalContractInfo with extracted classification data.
    """
    title = event.title
    state = _extract_state(title)
    party = _extract_party(title)

    # 1. margin_bracket — MUST check before candidate_win ("wins by" contains "wins")
    m = _MARGIN_RE.search(title)
    if m:
        threshold = float(m.group("threshold"))
        # Determine direction from context
        text_before = title[:m.start()].lower()
        direction = "above"  # default for "wins by >N%", "margin greater than N%"
        if "less" in text_before or "<" in m.group(0):
            direction = "below"

        # Try to extract candidate name before the margin phrase
        candidates = []
        cand_match = re.match(r"(?P<cand>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+wins?\s+by",
                              title, re.IGNORECASE)
        if cand_match:
            candidates = [cand_match.group("cand")]

        # Try to extract race after the percentage
        race = None
        after = title[m.end():].strip()
        if after:
            # Remove leading "in" or "of"
            after = re.sub(r"^(?:in|of)\s+", "", after, flags=re.IGNORECASE)
            if after:
                race = _clean_race(after)

        return PoliticalContractInfo(
            event=event,
            contract_type="margin_bracket",
            candidates=candidates,
            party=party,
            race=race,
            state=state,
            threshold=threshold,
            direction=direction,
        )

    # 2. vote_share
    m = _VOTE_SHARE_RE.search(title)
    if m:
        threshold = float(m.group("threshold"))
        direction = "above"  # default for "gets >N%", "above N%"
        if "less" in title[:m.start()].lower() or "below" in title[:m.start()].lower():
            direction = "below"

        candidates = []
        cand_match = re.match(r"(?P<cand>[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:gets?|receives?)",
                              title, re.IGNORECASE)
        if cand_match:
            candidates = [cand_match.group("cand")]

        race = None
        after = title[m.end():].strip()
        if after:
            after = re.sub(r"^(?:in|of)\s+", "", after, flags=re.IGNORECASE)
            if after:
                race = _clean_race(after)

        return PoliticalContractInfo(
            event=event,
            contract_type="vote_share",
            candidates=candidates,
            party=party,
            race=race,
            state=state,
            threshold=threshold,
            direction=direction,
        )

    # 3. matchup
    m = _MATCHUP_RE.search(title)
    if m:
        candidates = [m.group("cand1"), m.group("cand2")]
        # Race may follow after the second candidate name
        race = None
        after = title[m.end():].strip()
        if after:
            after = re.sub(r"^(?:in|for|,)\s*", "", after, flags=re.IGNORECASE)
            if after:
                race = _clean_race(after)

        return PoliticalContractInfo(
            event=event,
            contract_type="matchup",
            candidates=candidates,
            party=party,
            race=race,
            state=state,
            threshold=None,
            direction=None,
        )

    # 4. party_outcome — MUST be before candidate_win
    m = _PARTY_OUTCOME_RE.search(title)
    if m:
        detected_party = _extract_party(m.group("party"))
        race = _clean_race(m.group("race"))

        return PoliticalContractInfo(
            event=event,
            contract_type="party_outcome",
            candidates=[],
            party=detected_party or party,
            race=race,
            state=state or _extract_state(race) if race else state,
            threshold=None,
            direction=None,
        )

    # 5. candidate_win
    m = _CANDIDATE_WIN_RE.search(title)
    if m:
        candidate = m.group("candidate")
        race = _clean_race(m.group("race"))

        return PoliticalContractInfo(
            event=event,
            contract_type="candidate_win",
            candidates=[candidate],
            party=party,
            race=race,
            state=state or (_extract_state(race) if race else state),
            threshold=None,
            direction=None,
        )

    # 6. crypto_event — crypto asset + actionable keyword
    crypto = _classify_crypto(title)
    if crypto:
        return PoliticalContractInfo(
            event=event,
            contract_type="crypto_event",
            candidates=[],
            party=party,
            race=None,
            state=state,
            threshold=crypto["crypto_threshold"],
            direction=crypto.get("crypto_direction"),
            crypto_asset=crypto["crypto_asset"],
            event_category=crypto["event_category"],
            crypto_direction=crypto["crypto_direction"],
            crypto_threshold=crypto["crypto_threshold"],
        )

    # 7. yes_no_binary — fallback
    return PoliticalContractInfo(
        event=event,
        contract_type="yes_no_binary",
        candidates=[],
        party=party,
        race=None,
        state=state,
        threshold=None,
        direction=None,
    )
