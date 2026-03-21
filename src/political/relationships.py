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
    # Crypto relationship types
    "crypto_regulatory_hedge": 3.0,
    "crypto_event_catalyst": 2.5,
    "cross_crypto_correlation": 2.0,
    "crypto_price_spread": 1.5,
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

    # --- Crypto relationship types ---
    # Only apply when both contracts are crypto_event
    if a.contract_type == "crypto_event" and b.contract_type == "crypto_event":

        # 7. crypto_regulatory_hedge: price_target+positive vs regulatory+negative, same asset
        if (a.crypto_asset and a.crypto_asset == b.crypto_asset):
            a_cat, b_cat = a.event_category, b.event_category

            if ({a_cat, b_cat} == {"price_target", "regulatory"}):
                pt = a if a_cat == "price_target" else b
                rg = b if a_cat == "price_target" else a
                if pt.crypto_direction == "positive" and rg.crypto_direction == "negative":
                    return {
                        "type": "crypto_regulatory_hedge",
                        "pair": (idx_a, idx_b),
                        "score": RELATIONSHIP_SCORES["crypto_regulatory_hedge"],
                        "details": f"{a.crypto_asset} price target vs regulatory risk",
                    }

            # 8. crypto_event_catalyst: regulatory/technical + price_target, same asset
            if ("price_target" in {a_cat, b_cat}
                    and {a_cat, b_cat} & {"regulatory", "technical"}
                    and a_cat != b_cat):
                return {
                    "type": "crypto_event_catalyst",
                    "pair": (idx_a, idx_b),
                    "score": RELATIONSHIP_SCORES["crypto_event_catalyst"],
                    "details": f"{a.crypto_asset} event catalyst → price impact",
                }

            # 9. crypto_price_spread: both price_target, same asset, different thresholds
            if (a_cat == "price_target" and b_cat == "price_target"
                    and a.crypto_threshold is not None and b.crypto_threshold is not None
                    and a.crypto_threshold != b.crypto_threshold):
                return {
                    "type": "crypto_price_spread",
                    "pair": (idx_a, idx_b),
                    "score": RELATIONSHIP_SCORES["crypto_price_spread"],
                    "details": f"{a.crypto_asset} spread: ${a.crypto_threshold:,.0f} vs ${b.crypto_threshold:,.0f}",
                }

        # 10. cross_crypto_correlation: different assets, same event_category
        if (a.crypto_asset and b.crypto_asset
                and a.crypto_asset != b.crypto_asset
                and a.event_category == b.event_category):
            return {
                "type": "cross_crypto_correlation",
                "pair": (idx_a, idx_b),
                "score": RELATIONSHIP_SCORES["cross_crypto_correlation"],
                "details": f"{a.crypto_asset}/{b.crypto_asset} correlated {a.event_category}",
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
