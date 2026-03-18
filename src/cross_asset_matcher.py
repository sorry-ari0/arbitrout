"""Cross-asset matcher — STUB, needs full rewrite (task 40).

KNOWN ISSUES (from audit):
- Returns empty list always
- Unused import of load_saved
- No actual matching logic implemented

DO NOT wire up to API endpoints until rewritten.
See: tasks.md task 40
"""

import logging

logger = logging.getLogger(__name__)


class CrossAssetMatcher:
    def __init__(self, registry):
        self.registry = registry

    def get_opportunities(self):
        """STUB: Returns empty list. Needs rewrite to match prediction events against real assets."""
        logger.warning("CrossAssetMatcher is a stub — returns no opportunities until task 40 is completed")
        return []
