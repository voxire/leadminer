"""
Regression tests for the lead-signal inversion and phone normalization bugs.

Run with the stdlib only, no pytest needed:

    python3 -m unittest discover -s tests -v

These are the tests that matter most: each one failed against the original
code. See docs/audits/043-scoring-upgrade.md and
docs/audits/001-dedup-phone-normalization.md for the analyses.
"""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _stub_requests() -> None:
    """enricher imports requests at module scope. Stub it if absent so these
    tests run in a bare checkout with no dependencies installed."""
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass
    mod = types.ModuleType("requests")

    class Session:
        def __init__(self, *a, **k):
            self.headers = {}

        def get(self, *a, **k):
            raise RuntimeError("no network in tests")

    class exceptions:
        class RequestException(Exception):
            pass

        class InsecureRequestWarning(Warning):
            pass

    mod.Session = Session
    mod.exceptions = exceptions
    mod.get = lambda *a, **k: None
    sys.modules["requests"] = mod

    u3 = types.ModuleType("urllib3")

    class u3exceptions:
        class InsecureRequestWarning(Warning):
            pass

    u3.exceptions = u3exceptions
    u3.disable_warnings = lambda *a, **k: None
    sys.modules["urllib3"] = u3


_stub_requests()

from dedup import normalize_phone  # noqa: E402
from enricher import DEAD, LIVE, UNKNOWN, lead_score  # noqa: E402
from pitch_recommender import recommend_service  # noqa: E402


class TestLeadSignalIsNotInverted(unittest.TestCase):
    """A site that merely resists the crawler must never outrank a broken one."""

    BASE = {"name": "Acme", "industry_priority": "high", "source": "osm"}

    def test_unreachable_site_gets_no_dead_credit(self):
        blocked = dict(self.BASE, website="https://a.example", website_live=None)
        self.assertEqual(lead_score(blocked), 15, "unreachable site must score like no site")

    def test_dead_site_outscores_unreachable_site(self):
        dead = dict(self.BASE, website="https://a.example", website_live=False)
        blocked = dict(self.BASE, website="https://a.example", website_live=None)
        self.assertGreater(lead_score(dead), lead_score(blocked))

    def test_unreachable_site_is_not_pitched_as_a_rebuild(self):
        blocked = dict(self.BASE, category="restaurant",
                       website="https://a.example", website_live=None)
        self.assertNotEqual(
            recommend_service(blocked), "Website rebuild + maintenance",
            "we never reached the site, so we cannot sell a rebuild",
        )

    def test_confirmed_dead_site_is_still_pitched_as_a_rebuild(self):
        dead = dict(self.BASE, category="restaurant",
                    website="https://a.example", website_live=False)
        self.assertEqual(recommend_service(dead), "Website rebuild + maintenance")

    def test_outcomes_are_distinct_constants(self):
        self.assertEqual(len({LIVE, DEAD, UNKNOWN}), 3)


class TestNormalizePhone(unittest.TestCase):
    def test_garbage_does_not_collapse_to_a_shared_key(self):
        """Regression: "---" and "..." both normalized to "+", so unrelated
        businesses were merged into one row via the phone-keyed dedup index."""
        for junk in ("---", "...", "  ", "n/a", "ext 4"):
            value = normalize_phone(junk, "LB")
            self.assertFalse(value, f"{junk!r} must not yield a usable phone key")

    def test_international_prefix_strips_the_right_country(self):
        """Regression: 00966... under country=LB became +9610966..., relabelling
        a Saudi number as Lebanese and poisoning the dedup index."""
        self.assertEqual(normalize_phone("00966501234567", "LB"), "+966501234567")
        self.assertEqual(normalize_phone("00961370123456", "SA"), "+961370123456")

    def test_national_format_uses_country_code(self):
        self.assertEqual(normalize_phone("0501234567", "SA"), "+966501234567")
        self.assertEqual(normalize_phone("70123456", "LB"), "+96170123456")

    def test_punctuation_is_stripped(self):
        self.assertEqual(
            normalize_phone("+962 51 234 5678", "LB").replace("2", "2"), "+962512345678"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)