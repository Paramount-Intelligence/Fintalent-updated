"""Session storage and worker lock tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, call

import database as db


class TestSessionStorage(unittest.TestCase):
    def test_save_preserves_lock_columns(self):
        existing = {
            "platform": "fintalent",
            "session_data": {"cookies": [{"x": 1}], "local_storage": {}},
            "saved_at": "2026-01-01T00:00:00+00:00",
            "session_version": 1,
            "metadata": {},
            "worker_lock_owner": "ownerA",
            "worker_lock_expires_at": "2099-01-01T00:00:00+00:00",
            "worker_lock_heartbeat_at": "2026-01-01T00:00:00+00:00",
        }
        mock_sb = MagicMock()
        update_chain = mock_sb.table.return_value.update.return_value.eq.return_value
        update_chain.execute.return_value = MagicMock(data=[{}])

        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "load_scraper_session_row", return_value=existing):
            db.save_scraper_session([{"name": "c"}], {"k": "v"})

        payload = mock_sb.table.return_value.update.call_args[0][0]
        self.assertIn("session_data", payload)
        self.assertIn("saved_at", payload)
        self.assertIsNotNone(payload["saved_at"])
        self.assertNotIn("worker_lock_owner", payload)
        self.assertNotIn("worker_lock_expires_at", payload)

    def test_clear_preserves_lock_and_non_null_saved_at(self):
        existing = {
            "platform": "fintalent",
            "session_data": {"cookies": [{"a": 1}], "local_storage": {"t": "1"}},
            "saved_at": "2026-01-01T00:00:00+00:00",
            "metadata": {},
            "worker_lock_owner": "ownerA",
        }
        mock_sb = MagicMock()
        mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = MagicMock(data=[{}])
        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "load_scraper_session_row", return_value=existing):
            db.delete_scraper_session()
        payload = mock_sb.table.return_value.update.call_args[0][0]
        self.assertEqual(payload["session_data"], {"cookies": [], "local_storage": {}})
        self.assertIsNotNone(payload["saved_at"])
        self.assertNotIn("worker_lock_owner", payload)

    def test_load_returns_none_without_cookies(self):
        with patch.object(db, "load_scraper_session_row", return_value={
            "session_data": {"cookies": [], "local_storage": {}},
            "saved_at": "x",
        }):
            self.assertIsNone(db.load_scraper_session())


class TestWorkerLock(unittest.TestCase):
    def test_first_owner_acquires(self):
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.return_value = MagicMock(data=[{
            "acquired": True, "owner": "me", "expires_at": "x", "heartbeat_at": "y",
        }])
        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "worker_lock_owner_id", return_value="me"):
            result = db.acquire_worker_lock(60)
            self.assertTrue(result["acquired"])

    def test_second_owner_rejected(self):
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.return_value = MagicMock(data=[{
            "acquired": False, "owner": "other", "expires_at": "x", "heartbeat_at": "y",
        }])
        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "worker_lock_owner_id", return_value="me"):
            result = db.acquire_worker_lock(60)
            self.assertFalse(result["acquired"])
            self.assertEqual(result["owner"], "other")

    def test_lock_failure_fails_closed(self):
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.side_effect = RuntimeError("rpc down")
        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "worker_lock_owner_id", return_value="me"):
            with self.assertRaises(db.DatabaseError) as ctx:
                db.acquire_worker_lock(60)
            self.assertEqual(ctx.exception.code, "LOCK_FAILED")

    def test_verify_lock_held_false_on_error(self):
        with patch.object(db, "load_scraper_session_row", side_effect=RuntimeError("boom")):
            self.assertFalse(db.verify_lock_held())

    def test_verify_lock_held_true(self):
        from datetime import datetime, timedelta, timezone
        with patch.object(db, "load_scraper_session_row", return_value={
            "worker_lock_owner": "me",
            "worker_lock_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }), patch.object(db, "worker_lock_owner_id", return_value="me"):
            self.assertTrue(db.verify_lock_held())


class TestRunTracking(unittest.TestCase):
    def test_finalize_completed(self):
        from script_clean import finalize_run_status
        self.assertEqual(finalize_run_status({}), "COMPLETED")

    def test_finalize_partial(self):
        from script_clean import finalize_run_status
        self.assertEqual(finalize_run_status({"emails_failed": 1}), "PARTIAL")

    def test_finalize_auth(self):
        from script_clean import finalize_run_status
        self.assertEqual(finalize_run_status({}, auth_failed=True), "AUTH_FAILED")

    def test_finalize_db_failed(self):
        from script_clean import finalize_run_status
        self.assertEqual(finalize_run_status({"db_failed": True}), "FAILED")

    def test_category_not_exposed_does_not_cause_partial(self):
        from script_clean import finalize_run_status
        # no partial flags → COMPLETED even if category not exposed (not in counts)
        self.assertEqual(finalize_run_status({
            "cards_found": 1, "cards_parsed": 1, "projects_inserted": 1,
        }), "COMPLETED")


if __name__ == "__main__":
    unittest.main()
