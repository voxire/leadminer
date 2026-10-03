"""
Regression tests for the lead-signal inversion and phone normalization bugs.

Run with the stdlib only, no pytest needed:

    python3 -m unittest discover -s tests -v

These are the tests that matter most: each one failed against the original
code. See docs/audits/043-scoring-upgrade.md and
docs/audits/001-dedup-phone-normalization.md for the analyses.
"""

import argparse
import sys
import tempfile
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
from enricher import DEAD, LIVE, UNKNOWN, infer_region, lead_score  # noqa: E402
from main import load_master, resolve_country, write_csv  # noqa: E402
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


class TestResolveCountry(unittest.TestCase):
    """Regression: `.get("country", "LB")` never fires its default because
    load_master turns blank cells into None, and None is a present key."""

    def test_none_country_falls_back(self):
        self.assertEqual(resolve_country({"country": None}), "LB")
        self.assertEqual(resolve_country({}), "LB")
        self.assertEqual(resolve_country({"country": "  "}), "LB")

    def test_explicit_country_is_preserved(self):
        self.assertEqual(resolve_country({"country": "SA"}), "SA")
        self.assertEqual(resolve_country({"country": "sa"}), "SA")

    def test_aliases(self):
        self.assertEqual(resolve_country({"country": "Lebanon"}), "LB")
        self.assertEqual(resolve_country({"country": "KSA"}), "SA")

    def test_phone_is_used_to_disambiguate(self):
        self.assertEqual(resolve_country({"country": None, "phone": "+966501234567"}), "SA")
        self.assertEqual(resolve_country({"country": None, "phone": "+96170123456"}), "LB")

    def test_region_inference_is_not_disabled_by_none_country(self):
        """The bug that made every master record fall out of address-based
        region inference: infer_region checks `country == "LB"`."""
        r = {"country": None, "address": "Hamra, Beirut, Lebanon"}
        self.assertEqual(
            infer_region(r["address"], None, None, resolve_country(r)), "Beirut"
        )


class TestAtomicCsvWrite(unittest.TestCase):
    def test_partial_write_does_not_destroy_the_previous_file(self):
        """A crash mid-write must leave the old master intact, not truncated."""

        class Boom(dict):
            # csv.DictWriter with extrasaction="ignore" never calls .keys();
            # it builds each row via rowdict.get(field, restval).
            def get(self, *a, **k):
                raise RuntimeError("simulated OOM mid-write")

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "master.csv"
            good = [{"name": "Acme", "country": "LB"}]
            write_csv(p, good)
            before = p.read_text(encoding="utf-8-sig")
            self.assertIn("Acme", before)

            with self.assertRaises(RuntimeError):
                write_csv(p, [Boom()])

            self.assertEqual(p.read_text(encoding="utf-8-sig"), before,
                             "master was damaged by a failed write")
            self.assertFalse(p.with_suffix(".csv.tmp").exists(), "temp file left behind")

    def test_bom_is_written_and_stripped_on_read(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.csv"
            write_csv(p, [{"name": "مقهى", "country": "LB"}])
            raw = p.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "missing UTF-8 BOM for Excel")
            rows = load_master(p)
            self.assertEqual(rows[0]["name"], "مقهى")
            self.assertNotIn("\ufeff", rows[0]["name"])


class TestCli(unittest.TestCase):
    """The CLI is what replaces 'python main.py', so its offline commands are
    the first thing an operator reaches for when a run looks wrong."""

    def _fixture(self, d: str, **overrides) -> str:
        base = {
            "name": "Cafe", "category": "cafe", "region": "Beirut", "country": "LB",
            "address": "1 St, Beirut", "lat": "33.89", "lon": "35.50",
            "phone": "70123456", "email": "", "website": "https://x.com",
            "website_live": "True", "facebook": "", "instagram": "", "whatsapp": "",
            "linkedin": "", "rating": "4.2", "review_count": "12",
            "completeness_score": "2", "lead_score": "30",
            "industry_priority": "high", "recommended_service": "SEO audit",
            "source": "osm", "scraped_at": "2026-01-01T00:00:00Z",
        }
        base.update(overrides)
        p = Path(d) / "f.csv"
        write_csv(p, [base])
        return str(p)

    def test_stats_handles_tri_state_website_live(self):
        import contextlib
        import io

        from cli import cmd_stats

        with tempfile.TemporaryDirectory() as d:
            p = self._fixture(d)
            ns = argparse.Namespace(file=p)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = cmd_stats(ns)
            out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("website live    : 1", out)
        self.assertIn("unreachable", out)

    def test_validate_rejects_out_of_range_rating(self):
        import contextlib
        import io

        from cli import cmd_validate

        with tempfile.TemporaryDirectory() as d:
            p = self._fixture(d, rating="9.9")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = cmd_validate(argparse.Namespace(file=p))
            self.assertEqual(rc, 1, "must fail the gate")
            self.assertIn("outside 0-5", buf.getvalue())

    def test_validate_accepts_clean_data(self):
        import contextlib
        import io

        from cli import cmd_validate

        with tempfile.TemporaryDirectory() as d:
            p = self._fixture(d)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = cmd_validate(argparse.Namespace(file=p))
            self.assertEqual(rc, 0, buf.getvalue())

    def test_missing_file_is_an_error_not_a_traceback(self):
        import contextlib
        import io

        from cli import cmd_stats

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = cmd_stats(argparse.Namespace(file="/nonexistent/nope.csv"))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)