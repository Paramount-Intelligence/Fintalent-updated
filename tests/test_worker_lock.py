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


class TestLockHeartbeat(unittest.TestCase):
    def setUp(self):
        import script_clean
        self.script_clean = script_clean
        script_clean._last_lock_heartbeat = 0.0

    def test_heartbeat_throttles_after_success(self):
        sc = self.script_clean
        with patch.object(sc, "db") as mock_db:
            mock_db.renew_worker_lock.return_value = {"renewed": True}
            self.assertTrue(sc.heartbeat_worker_lock())
            self.assertTrue(sc.heartbeat_worker_lock())
            self.assertEqual(mock_db.renew_worker_lock.call_count, 1)
            self.assertTrue(sc.heartbeat_worker_lock(force=True))
            self.assertEqual(mock_db.renew_worker_lock.call_count, 2)

    def test_heartbeat_false_when_not_renewed(self):
        sc = self.script_clean
        with patch.object(sc, "db") as mock_db:
            mock_db.renew_worker_lock.return_value = {"renewed": False}
            self.assertFalse(sc.heartbeat_worker_lock())

    def test_ensure_lock_reacquires_expired_lease(self):
        sc = self.script_clean
        with patch.object(sc, "db") as mock_db:
            mock_db.renew_worker_lock.return_value = {"renewed": False}
            mock_db.acquire_worker_lock.return_value = {"acquired": True, "self_owner": "me"}
            self.assertTrue(sc.ensure_worker_lock())
            mock_db.acquire_worker_lock.assert_called_once()

    def test_ensure_lock_fails_when_another_worker_holds_it(self):
        sc = self.script_clean
        with patch.object(sc, "db") as mock_db:
            mock_db.renew_worker_lock.return_value = {"renewed": False}
            mock_db.acquire_worker_lock.return_value = {"acquired": False, "owner": "other"}
            self.assertFalse(sc.ensure_worker_lock())

    def _fake_clock(self):
        """sleep() advances a virtual monotonic clock so idle waits run instantly."""
        state = {"now": 1000.0, "slept": []}

        def sleep(seconds):
            state["slept"].append(seconds)
            state["now"] += seconds

        return state, sleep

    def test_idle_interval_longer_than_ttl_keeps_renewing(self):
        sc = self.script_clean
        state, sleep = self._fake_clock()
        with patch.object(sc, "db") as mock_db, \
             patch.object(sc.time, "sleep", side_effect=sleep), \
             patch.object(sc.time, "monotonic", side_effect=lambda: state["now"]), \
             patch.object(sc.Config, "FINTALENT_WORKER_LOCK_TTL_SECONDS", 180):
            mock_db.renew_worker_lock.return_value = {"renewed": True}
            sc.sleep_between_cycles(600, holding_lock=True)
            self.assertEqual(sum(state["slept"]), 600)
            self.assertLessEqual(max(state["slept"]), 60)
            self.assertGreaterEqual(mock_db.renew_worker_lock.call_count, 10)

    def test_idle_interval_without_lock_does_not_renew(self):
        sc = self.script_clean
        state, sleep = self._fake_clock()
        with patch.object(sc, "db") as mock_db, \
             patch.object(sc.time, "sleep", side_effect=sleep), \
             patch.object(sc.time, "monotonic", side_effect=lambda: state["now"]):
            sc.sleep_between_cycles(120, holding_lock=False)
            self.assertEqual(sum(state["slept"]), 120)
            mock_db.renew_worker_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
