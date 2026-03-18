import re
from collections import defaultdict

class ArbitrageEngine:
    def _normalize_title(self, title: str) -> set[str]:
        normalized_tokens = re.findall(r'\b[a-z0-9]+\b', title.lower())
        return set(normalized_tokens)

    def _jaccard_similarity(self, tokens1: set[str], tokens2: set[str]) -> float:
        if not tokens1 and not tokens2:
            return 1.0
        intersection = len(tokens1.intersection(tokens2))
        union = len(tokens1.union(tokens2))
        return intersection / union if union > 0 else 0.0

    def _build_title_index(self, events: list) -> defaultdict[set[str], list]:
        index = defaultdict(list)
        for event in events:
            normalized_tokens = self._normalize_title(event['title'])
            for token in normalized_tokens:
                index[token].append(event)
        return index

    def match_events(self, events_platform_a: list, events_platform_b: list, threshold: float = 0.5) -> list:
        matched_pairs = []

        index_a = self._build_title_index(events_platform_a)
        index_b = self._build_title_index(events_platform_b)

        for event_b in events_platform_b:
            normalized_tokens_b = self._normalize_title(event_b['title'])
            best_match_info = None
            highest_similarity = -1.0

            for token in normalized_tokens_b:
                if token in index_a:
                    for event_a in index_a[token]:
                        similarity = self._jaccard_similarity(
                            self._normalize_title(event_a['title']),
                            normalized_tokens_b
                        )

                        if similarity > highest_similarity:
                            highest_similarity = similarity
                            best_match_info = event_a

            if best_match_info and highest_similarity >= threshold:
                matched_pairs.append((best_match_info, event_b, highest_similarity))

        return matched_pairs
