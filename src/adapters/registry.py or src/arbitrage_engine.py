import re
from collections import defaultdict

def normalize_title(title: str) -> str:
    """Converts title to lowercase and removes non-alphanumeric characters."""
    return re.sub(r'[^a-z0-9\s]', '', title.lower()).strip()

def tokenize(text: str) -> set:
    """Splits text into tokens (words)."""
    return set(text.split())

def jaccard_similarity(set1: set, set2: set) -> float:
    """Calculates the Jaccard similarity between two sets."""
    intersection = len(set1.intersection(set2))
    union = len(set1.union(set2))
    return intersection / union if union > 0 else 0.0

def find_matching_events(events_platform1: list[dict], events_platform2: list[dict], threshold: float = 0.7) -> list[tuple]:
    """
    Matches events between two platforms using normalized titles and Jaccard similarity.
    This implements an optimized event matching strategy using a fuzzy matching index.
    """
    # Build an inverted index for platform 2 events based on normalized tokens
    index = defaultdict(list)
    platform2_processed_events = []
    for i, event in enumerate(events_platform2):
        normalized_title = normalize_title(event.get('title', ''))
        tokens = tokenize(normalized_title)
        platform2_processed_events.append({'original_event': event, 'normalized_title': normalized_title, 'tokens': tokens, 'id': i})
        for token in tokens:
            index[token].append(i) # Store index of the event in platform2_processed_events

    matches = []
    for event1 in events_platform1:
        normalized_title1 = normalize_title(event1.get('title', ''))
        tokens1 = tokenize(normalized_title1)

        potential_matches_indices = set()
        for token in tokens1:
            potential_matches_indices.update(index[token])

        best_match = None
        highest_similarity = threshold

        for p2_idx in potential_matches_indices:
            event2_data = platform2_processed_events[p2_idx]
            similarity = jaccard_similarity(tokens1, event2_data['tokens'])
            if similarity > highest_similarity:
                highest_similarity = similarity
                best_match = event2_data['original_event']
        
        if best_match:
            matches.append((event1, best_match, highest_similarity))
    
    return matches
