"""Dedicated worker lock tests (acquire/renew/release contracts)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import database as db


class TestWorkerLockRPC(unittest.TestCase):
    def test_renew_uses_correct_rpc(self):
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.return_value = MagicMock(data=[{"renewed": True, "owner": "me"}])
        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "worker_lock_owner_id", return_value="me"):
            r = db.renew_worker_lock(120)
            self.assertTrue(r["renewed"])
            mock_sb.rpc.assert_called_with(
                "renew_scraper_worker_lock",
                {"p_platform": "fintalent", "p_owner": "me", "p_ttl_seconds": 120},
            )

    def test_release_uses_correct_rpc(self):
        mock_sb = MagicMock()
        mock_sb.rpc.return_value.execute.return_value = MagicMock(data=[{"released": True}])
        with patch.object(db, "get_supabase_client", return_value=mock_sb), \
             patch.object(db, "worker_lock_owner_id", return_value="me"):
            r = db.release_worker_lock()
            self.assertTrue(r["released"])
            mock_sb.rpc.assert_called_with(
                "release_scraper_worker_lock",
                {"p_platform": "fintalent", "p_owner": "me"},
            )

    def test_expired_lock_verify_false(self):
        from datetime import datetime, timedelta, timezone
        with patch.object(db, "load_scraper_session_row", return_value={
            "worker_lock_owner": "me",
            "worker_lock_expires_at": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
        }), patch.object(db, "worker_lock_owner_id", return_value="me"):
            self.assertFalse(db.verify_lock_held())


if __name__ == "__main__":
    unittest.main()
