"""Configuration and database layer tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import database as db


class TestConfigCredentials(unittest.TestCase):
    def tearDown(self):
        db.reset_client()

    def test_preferred_secret_key(self):
        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SECRET_KEY": "sb_secret_preferred",
            "SUPABASE_SERVICE_ROLE_KEY": "legacy_key",
        }, clear=False):
            url, key = db.get_supabase_credentials()
            self.assertEqual(key, "sb_secret_preferred")

    def test_legacy_fallback(self):
        env = {
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "legacy_service_role",
        }
        # Remove preferred if present
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("SUPABASE_SECRET_KEY", None)
            url, key = db.get_supabase_credentials()
            self.assertEqual(key, "legacy_service_role")

    def test_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(db.DatabaseError) as ctx:
                db.get_supabase_credentials()
            self.assertEqual(ctx.exception.code, "CONFIG_MISSING")


class TestQueryFailureNotEmpty(unittest.TestCase):
    def test_failed_query_raises(self):
        mock_q = MagicMock()
        mock_q.execute.side_effect = RuntimeError("network boom")
        with self.assertRaises(db.DatabaseError) as ctx:
            db._execute(mock_q, context="test_query")
        self.assertEqual(ctx.exception.code, "QUERY_FAILED")
        self.assertIn("network boom", str(ctx.exception))


class TestMongoAbsentFromRuntime(unittest.TestCase):
    def test_no_pymongo_import_in_runtime_modules(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        for name in ("monitor.py", "script_clean.py", "database.py", "extraction.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("pymongo", text)
            self.assertNotIn("MongoClient", text)
            self.assertNotIn("from pymongo", text)
            # Runtime must not reference Mongo connection settings
            self.assertNotIn("MONGO_URI", text)


class TestOccurrenceHelpers(unittest.TestCase):
    def test_should_process_first_occurrence(self):
        with patch.object(db, "get_latest_project_occurrence", return_value=None):
            ok, reason = db.should_process_project("fintalent", "abc")
            self.assertTrue(ok)
            self.assertEqual(reason, "NO_PREVIOUS_OCCURRENCE")

    def test_within_window_skipped(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        scraped = now - timedelta(days=2)
        with patch.dict(os.environ, {"OCCURRENCE_WINDOW_DAYS": "7"}, clear=False), \
             patch.object(db, "get_latest_project_occurrence", return_value={"scraped_at": scraped.isoformat()}):
            ok, reason = db.should_process_project("fintalent", "abc", now=now)
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("skipped_within_7_days_age_"))

    def test_exactly_n_days_skipped(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        scraped = now - timedelta(days=7)
        with patch.dict(os.environ, {"OCCURRENCE_WINDOW_DAYS": "7"}, clear=False), \
             patch.object(db, "get_latest_project_occurrence", return_value={"scraped_at": scraped.isoformat()}):
            ok, reason = db.should_process_project("fintalent", "abc", now=now)
            self.assertFalse(ok)
            self.assertTrue(reason.startswith("skipped_within_7_days_age_"))

    def test_more_than_n_days_eligible(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        scraped = now - timedelta(days=7, seconds=1)
        with patch.dict(os.environ, {"OCCURRENCE_WINDOW_DAYS": "7"}, clear=False), \
             patch.object(db, "get_latest_project_occurrence", return_value={"scraped_at": scraped.isoformat()}):
            ok, reason = db.should_process_project("fintalent", "abc", now=now)
            self.assertTrue(ok)
            self.assertTrue(reason.startswith("eligible_after_"))

    def test_window_days_from_env(self):
        with patch.dict(os.environ, {"OCCURRENCE_WINDOW_DAYS": "5"}, clear=False):
            os.environ.pop("REPOST_MIN_DAYS", None)
            self.assertEqual(db.get_occurrence_window_days(), 5)
        with patch.dict(os.environ, {"REPOST_MIN_DAYS": "10"}, clear=False):
            os.environ.pop("OCCURRENCE_WINDOW_DAYS", None)
            self.assertEqual(db.get_occurrence_window_days(), 10)


if __name__ == "__main__":
    unittest.main()
