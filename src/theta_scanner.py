"""Theta decay scanner — STUB, needs full rewrite (task 39).

KNOWN ISSUES (from audit):
- Calls registry.get_all_events() which does not exist (should be fetch_all() or get_all_cached())
- References non-existent event fields (implied_probability, current_price, end_date)
- NormalizedEvent uses: expiry, yes_price, no_price — not the fields used here

DO NOT wire up to API endpoints until rewritten.
See: tasks.md task 39
"""

from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class ThetaScanner:
    def __init__(self, registry):
        self.registry = registry

    def get_theta_opportunities(self):
        """STUB: Returns empty list. Needs rewrite to use real NormalizedEvent fields."""
        logger.warning("ThetaScanner is a stub — returns no opportunities until task 39 is completed")
        return []
