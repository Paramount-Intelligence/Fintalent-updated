"""Email lifecycle and monitoring flow tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch, call

import database as db


class TestEmailLifecycle(unittest.TestCase):
    @patch("script_clean.send_notification")
    @patch("script_clean.db")
    def test_insert_before_send_and_attempt_row(self, mock_db, mock_send):
        from script_clean import send_project_email

        mock_db.verify_lock_held.return_value = True
        mock_db.create_email_attempt.return_value = "attempt-1"
        mock_db._iso.return_value = "2026-08-04T00:00:00+00:00"
        mock_send.return_value = {
            "success": True,
            "message_id": "<mid@x>",
            "failure_code": None,
            "error": None,
        }

        project = {"title": "P", "project_id": "p1", "email_attempt_count": 0}
        result = send_project_email("uuid-1", project, attempt_number=1)
        self.assertTrue(result["success"])
        mock_db.create_email_attempt.assert_called_once()
        mock_db.update_project_email_status.assert_any_call("uuid-1", unittest.mock.ANY)
        # SENDING set before send
        first_status_call = mock_db.update_project_email_status.call_args_list[0]
        self.assertEqual(first_status_call[0][0], "uuid-1")
        self.assertEqual(first_status_call[0][1]["email_status"], "SENDING")
        mock_db.complete_email_attempt_success.assert_called_once_with("attempt-1", message_id="<mid@x>")

    @patch("script_clean.send_notification")
    @patch("script_clean.db")
    def test_failure_updates_same_uuid(self, mock_db, mock_send):
        from script_clean import send_project_email, Config

        mock_db.verify_lock_held.return_value = True
        mock_db.create_email_attempt.return_value = "attempt-2"
        mock_db._iso.return_value = "2026-08-04T00:00:00+00:00"
        mock_db.compute_email_next_retry.return_value = "2026-08-04T00:15:00+00:00"
        mock_send.return_value = {
            "success": False,
            "message_id": None,
            "failure_code": "SMTP_TIMEOUT",
            "error": "timed out",
        }
        send_project_email("uuid-2", {"email_attempt_count": 0}, attempt_number=1)
        mock_db.complete_email_attempt_failure.assert_called_once()
        final = mock_db.update_project_email_status.call_args_list[-1][0][1]
        self.assertEqual(final["email_status"], "RETRY_PENDING")
        self.assertEqual(final["email_failure_code"], "SMTP_TIMEOUT")

    @patch("script_clean.send_notification")
    @patch("script_clean.db")
    def test_max_retries_mark_failed(self, mock_db, mock_send):
        from script_clean import send_project_email, Config

        mock_db.verify_lock_held.return_value = True
        mock_db.create_email_attempt.return_value = "a"
        mock_db._iso.return_value = "2026-08-04T00:00:00+00:00"
        mock_send.return_value = {"success": False, "message_id": None, "failure_code": "X", "error": "e"}
        with patch.object(Config, "EMAIL_MAX_RETRIES", 3):
            send_project_email("uuid-3", {}, attempt_number=3)
        final = mock_db.update_project_email_status.call_args_list[-1][0][1]
        self.assertEqual(final["email_status"], "FAILED")
        self.assertIsNone(final["email_next_retry_at"])

    @patch("script_clean.db")
    def test_lock_loss_stops_email(self, mock_db):
        from script_clean import send_project_email
        mock_db.verify_lock_held.return_value = False
        result = send_project_email("uuid", {})
        self.assertFalse(result["success"])
        self.assertEqual(result["failure_code"], "LOCK_LOST")
        mock_db.create_email_attempt.assert_not_called()


class TestRetrySelection(unittest.TestCase):
    def test_suppressed_and_not_required_not_in_retry_query_filter(self):
        import inspect
        src = inspect.getsource(db.get_retryable_email_projects)
        self.assertIn("RETRY_PENDING", src)
        self.assertNotIn("SUPPRESSED", src)
        self.assertNotIn("NOT_REQUIRED", src)
        self.assertNotIn("SENT", src.split("RETRY_PENDING")[0])  # status filter is RETRY_PENDING only


class TestColdStartBehavior(unittest.TestCase):
    def test_other_platforms_do_not_prevent_cold_start(self):
        mock_sb = MagicMock()
        # empty fintalent rows
        mock_sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value = MagicMock(data=[])
        with patch.object(db, "get_supabase_client", return_value=mock_sb):
            self.assertTrue(db.is_platform_cold_start())

    def test_cold_start_row_suppresses_email(self):
        from extraction import build_project_row
        row = build_project_row(
            {"project_id": "c1", "source_url": "https://talent.fintalent.io/brief/c1", "title": "T",
             "extraction_metadata": {}},
            scraper_run_id="run",
            card_status="COMPLETE",
            detail_status="COMPLETE",
            email_eligible=False,
            email_status="SUPPRESSED",
            email_not_sent_reason="COLD_START_SEED",
        )
        self.assertFalse(row["email_eligible"])
        self.assertEqual(row["email_status"], "SUPPRESSED")
        self.assertEqual(row["email_not_sent_reason"], "COLD_START_SEED")
        self.assertEqual(row["email_attempt_count"], 0)

    @patch("script_clean.fetch_project_details")
    @patch("script_clean.scan_for_card_extractions")
    @patch("script_clean.db")
    def test_cold_start_always_suppresses_regardless_of_flag(self, mock_db, mock_scan, mock_fetch):
        from script_clean import process_cold_start, Config
        mock_scan.return_value = [{
            "ok": True,
            "card_extraction_status": "COMPLETE",
            "fields": {
                "project_id": "c2",
                "source_url": "https://talent.fintalent.io/brief/c2",
                "title": "Cold",
            },
            "extraction_metadata": {},
            "missing_fields": [],
            "extraction_warnings": [],
        }]
        mock_fetch.return_value = {
            "ok": True,
            "detail_extraction_status": "COMPLETE",
            "fields": {"description": "d"},
            "extraction_metadata": {},
            "missing_fields": [],
            "extraction_warnings": [],
            "detail_failure_code": None,
        }
        mock_db.insert_project_occurrence.return_value = "uuid-c"
        with patch.object(Config, "SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN", False):
            counts = process_cold_start(MagicMock(), "run", dry_run=False)
        self.assertEqual(counts["emails_suppressed"], 1)
        self.assertEqual(counts["emails_sent"], 0)
        inserted = mock_db.insert_project_occurrence.call_args[0][0]
        self.assertEqual(inserted["email_status"], "SUPPRESSED")
        self.assertEqual(inserted["email_not_sent_reason"], "COLD_START_SEED")


class TestOperationalErrorEmail(unittest.TestCase):
    def setUp(self):
        import script_clean as sc
        self.sc = sc
        sc._last_error_email_at = 0.0
        sc._error_emails_sent_today = 0
        sc._error_emails_day_key = ""
        sc._incident_email_counts.clear()
        sc._incident_first_seen.clear()
        sc._incident_consecutive.clear()

    def test_default_caps_allow_every_error(self):
        sc = self.sc
        with patch.object(sc.Config, "ERROR_RECIPIENT_EMAIL", "err@x.com"), \
             patch.object(sc.Config, "SENDER_EMAIL", "a@b.com"), \
             patch.object(sc.Config, "SENDER_PASSWORD", "pw"), \
             patch.object(sc.Config, "ERROR_EMAIL_COOLDOWN_MINUTES", 0), \
             patch.object(sc.Config, "INCIDENT_CONSECUTIVE_FAILURE_THRESHOLD", 1), \
             patch.object(sc.Config, "INCIDENT_OPEN_AFTER_MINUTES", 0), \
             patch.object(sc.Config, "INCIDENT_REMINDER_HOURS", 0), \
             patch.object(sc.Config, "INCIDENT_MAX_EMAILS", 0), \
             patch.object(sc.Config, "PLATFORM_MAX_ERROR_EMAILS_PER_DAY", 0), \
             patch.object(sc.smtplib, "SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value = MagicMock()
            sc.send_error_email("one", "body", incident_key="k1")
            sc.send_error_email("two", "body", incident_key="k1")
            sc.send_error_email("three", "body", incident_key="k1")
            self.assertEqual(mock_smtp.call_count, 3)

    def test_browser_exhaustion_classification(self):
        from script_clean import classify_operational_failure, is_browser_process_exhaustion
        self.assertTrue(is_browser_process_exhaustion("Cannot fork"))
        self.assertTrue(is_browser_process_exhaustion(OSError("posix_spawn failed")))
        self.assertEqual(
            classify_operational_failure(OSError("Cannot fork")),
            "BROWSER_PROCESS_EXHAUSTION",
        )


class TestFirstRunSeed(unittest.TestCase):
    @patch("script_clean.send_project_email")
    @patch("script_clean.fetch_project_details")
    @patch("script_clean.scan_for_card_extractions")
    @patch("script_clean.db")
    def test_force_suppress_marks_first_run_seed(self, mock_db, mock_scan, mock_fetch, mock_email):
        from script_clean import process_scan_cycle
        mock_db.should_process_project.return_value = (True, "NO_PREVIOUS_OCCURRENCE")
        mock_db.verify_lock_held.return_value = True
        mock_db.renew_worker_lock.return_value = {"renewed": True}
        mock_db.insert_project_occurrence.return_value = "uuid-n"
        mock_scan.return_value = [{
            "ok": True,
            "card_extraction_status": "COMPLETE",
            "fields": {
                "project_id": "n1",
                "source_url": "https://talent.fintalent.io/brief/n1",
                "title": "New",
            },
            "extraction_metadata": {},
            "missing_fields": [],
            "extraction_warnings": [],
        }]
        mock_fetch.return_value = {
            "ok": True,
            "detail_extraction_status": "COMPLETE",
            "fields": {"description": "d"},
            "extraction_metadata": {},
            "missing_fields": [],
            "extraction_warnings": [],
            "detail_failure_code": None,
        }
        counts = process_scan_cycle(
            MagicMock(), "run", dry_run=False, send_emails=False, force_suppress_emails=True,
        )
        mock_email.assert_not_called()
        inserted = mock_db.insert_project_occurrence.call_args[0][0]
        self.assertEqual(inserted["email_status"], "SUPPRESSED")
        self.assertEqual(inserted["email_not_sent_reason"], "FIRST_RUN_SEED")
        self.assertEqual(counts["emails_suppressed"], 1)


class TestProcessRecycle(unittest.TestCase):
    def test_recycle_due_after_hours(self):
        from script_clean import process_recycle_due, Config
        with patch.object(Config, "PROCESS_RECYCLE_HOURS", 3):
            started = 1000.0
            with patch("script_clean.time.monotonic", return_value=1000.0 + 3 * 3600 - 1):
                self.assertFalse(process_recycle_due(started))
            with patch("script_clean.time.monotonic", return_value=1000.0 + 3 * 3600):
                self.assertTrue(process_recycle_due(started))


class TestCLI(unittest.TestCase):
    def test_test_flag_rejected(self):
        from monitor import main
        code = main(["--test"])
        self.assertEqual(code, 2)

    def test_run_once_alias_parsed(self):
        from monitor import build_parser
        args = build_parser().parse_args(["--once"])
        self.assertTrue(args.once)
        args2 = build_parser().parse_args(["--run-once"])
        self.assertTrue(args2.run_once)


class TestSendNotificationContract(unittest.TestCase):
    @patch("script_clean.smtplib.SMTP")
    def test_returns_structured_dict(self, mock_smtp):
        from script_clean import send_notification, Config
        with patch.object(Config, "SENDER_EMAIL", "a@b.com"), \
             patch.object(Config, "SENDER_PASSWORD", "pw"), \
             patch.object(Config, "RECIPIENT_EMAILS", ["c@d.com"]):
            mock_smtp.return_value.__enter__.return_value = MagicMock()
            result = send_notification({"title": "Hello", "project_id": "x"})
            self.assertIn("success", result)
            self.assertIn("message_id", result)
            self.assertIn("failure_code", result)
            self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
