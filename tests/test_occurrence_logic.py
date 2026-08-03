"""Project identity and occurrence logic tests."""

from __future__ import annotations

import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import database as db
from extraction import resolve_project_identity, canonicalize_url, extract_id_from_url


class FakeCard:
    def __init__(self, attrs=None, html=""):
        self._attrs = attrs or {}
        self._html = html

    def get_attribute(self, name):
        if name == "outerHTML":
            return self._html
        return self._attrs.get(name)


class TestProjectIdentity(unittest.TestCase):
    def test_id_from_brief_url(self):
        url = "https://talent.fintalent.io/brief/abc123XYZ"
        self.assertEqual(extract_id_from_url(url), "abc123XYZ")
        ident = resolve_project_identity(href=url)
        self.assertEqual(ident["project_id"], "abc123XYZ")
        self.assertEqual(ident["project_id_source"], "canonical_url")
        self.assertEqual(ident["project_id_confidence"], "HIGH")

    def test_id_from_project_url(self):
        url = "https://talent.fintalent.io/project/proj999"
        ident = resolve_project_identity(href=url)
        self.assertEqual(ident["project_id"], "proj999")

    def test_id_from_data_attribute(self):
        card = FakeCard({"data-project-id": "attrid123456"})
        url = "https://talent.fintalent.io/some/path"
        # no brief id in URL → data attr with URL
        ident = resolve_project_identity(card, href=url)
        self.assertTrue(ident["project_id"])
        self.assertIn(ident["project_id_source"], ("canonical_url_hash", "data_attribute", "embedded_data"))

    def test_canonical_url_hash_fallback(self):
        url = "https://talent.fintalent.io/opportunity/no-id-here"
        ident = resolve_project_identity(href=url)
        expected = hashlib.sha256(canonicalize_url(url).encode()).hexdigest()[:24]
        self.assertEqual(ident["project_id"], expected)
        self.assertEqual(ident["project_id_source"], "canonical_url_hash")
        self.assertEqual(ident["project_id_confidence"], "LOW")

    def test_title_hash_not_used(self):
        # No URL and no attributes → reject; never invent title hash URL
        ident = resolve_project_identity(href=None)
        self.assertTrue(ident["rejected"])
        self.assertIsNone(ident["project_id"])
        self.assertIsNone(ident["source_url"])

    def test_synthetic_url_not_created(self):
        ident = resolve_project_identity(href=None)
        self.assertNotIn("brief/", str(ident.get("source_url")))

    def test_unstable_identity_rejects(self):
        ident = resolve_project_identity(href="")
        self.assertTrue(ident["rejected"])
        self.assertEqual(ident["reject_reason"], "UNSTABLE_IDENTITY")


class TestOccurrenceLogic(unittest.TestCase):
    def test_source_posted_time_does_not_affect(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        # scraped_at recent, even if source_posted_at old
        with patch.object(db, "get_latest_project_occurrence", return_value={
            "scraped_at": (now - timedelta(days=1)).isoformat(),
            "source_posted_at": (now - timedelta(days=30)).isoformat(),
        }):
            ok, _ = db.should_process_project("fintalent", "x", now=now)
            self.assertFalse(ok)

    def test_same_id_different_platform_independent(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)

        def side_effect(platform, project_id):
            if platform == "fintalent":
                return None
            return {"scraped_at": now.isoformat()}

        with patch.object(db, "get_latest_project_occurrence", side_effect=side_effect):
            ok_ft, _ = db.should_process_project("fintalent", "shared-id", now=now)
            self.assertTrue(ok_ft)


if __name__ == "__main__":
    unittest.main()
