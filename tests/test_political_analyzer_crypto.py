"""Tests for crypto event filtering in PoliticalAnalyzer."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from adapters.models import NormalizedEvent
from political.classifier import classify_contract
from political.analyzer import _is_crypto_relevant


class TestCryptoRelevanceFilter:
    """Tests for _is_crypto_relevant helper."""

    def test_bitcoin_relevant(self):
        assert _is_crypto_relevant("Will Bitcoin reach $100K?") is True

    def test_btc_relevant(self):
        assert _is_crypto_relevant("BTC above $150,000") is True

    def test_ethereum_etf_relevant(self):
        assert _is_crypto_relevant("SEC approves Ethereum ETF") is True

    def test_pure_political_not_relevant(self):
        assert _is_crypto_relevant("Talarico wins TX Senate") is False

    def test_generic_crypto_not_relevant(self):
        """Generic 'crypto' without specific asset is NOT relevant."""
        assert _is_crypto_relevant("Will crypto be discussed?") is False

    def test_congress_ban_bitcoin_relevant(self):
        """Cross-category: political title with crypto content."""
        assert _is_crypto_relevant("Will Congress ban Bitcoin?") is True


class TestAnalyzerCryptoFiltering:
    """Tests that crypto events flow through the classifier."""

    def test_crypto_category_event_classified(self):
        """Events with category='crypto' get classified."""
        ev = NormalizedEvent(
            platform="polymarket", event_id="btc-test",
            title="Will Bitcoin be above $150,000 by 2026?",
            category="crypto", yes_price=0.35, no_price=0.65,
            volume=50000, expiry="2026-12-31", url="https://polymarket.com/test",
        )
        info = classify_contract(ev)
        assert info.contract_type == "crypto_event"

    def test_politics_category_crypto_content_classified(self):
        """Political event with crypto content gets classified as crypto_event."""
        ev = NormalizedEvent(
            platform="polymarket", event_id="congress-btc",
            title="Will Congress ban Bitcoin?",
            category="politics", yes_price=0.10, no_price=0.90,
            volume=20000, expiry="2026-12-31", url="https://polymarket.com/test",
        )
        info = classify_contract(ev)
        assert info.contract_type == "crypto_event"
