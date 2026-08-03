"""Worker lock and monitoring flow integration-style unit tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from extraction import should_auto_enrich, build_project_row


class TestBackfillGuards(unittest.TestCase):
    def test_backfill_preserves_identity_fields_in_update_whitelist(self):
        import database as db
        self.assertNotIn("project_id", db.ENRICHMENT_FIELDS)
        self.assertNotIn("source_url", db.ENRICHMENT_FIELDS)
        self.assertNotIn("scraped_at", db.ENRICHMENT_FIELDS)
        self.assertNotIn("email_status", db.ENRICHMENT_FIELDS)
        self.assertNotIn("email_attempt_count", db.ENRICHMENT_FIELDS)
        self.assertIn("description", db.ENRICHMENT_FIELDS)

    @patch("script_clean.fetch_project_details")
    @patch("script_clean._apply_enrichment")
    @patch("script_clean.db")
    def test_backfill_updates_same_uuid_no_email(self, mock_db, mock_apply, mock_fetch):
        from script_clean import backfill_missing_details
        mock_db.get_projects_needing_enrichment.return_value = [{
            "id": "uuid-9",
            "project_id": "p9",
            "source_url": "https://talent.fintalent.io/brief/p9",
            "detail_extraction_status": "PARTIAL",
            "scraped_at": "2026-01-01T00:00:00+00:00",
            "email_status": "SENT",
            "email_attempt_count": 1,
        }]
        mock_fetch.return_value = {"detail_extraction_status": "COMPLETE", "fields": {"description": "x"}}
        driver = MagicMock()
        result = backfill_missing_details(driver, dry_run=False, limit=5)
        self.assertEqual(result["updated"], 1)
        mock_apply.assert_called_once()
        # apply receives same row id
        self.assertEqual(mock_apply.call_args[0][0]["id"], "uuid-9")


class TestDryRunSemantics(unittest.TestCase):
    @patch("script_clean.fetch_project_details")
    @patch("script_clean.scan_for_card_extractions")
    @patch("script_clean.db")
    def test_dry_run_inserts_nothing(self, mock_db, mock_scan, mock_fetch):
        from script_clean import process_scan_cycle
        mock_db.should_process_project.return_value = (True, "NO_PREVIOUS_OCCURRENCE")
        mock_db.verify_lock_held.return_value = True
        mock_scan.return_value = [{
            "ok": True,
            "card_extraction_status": "COMPLETE",
            "fields": {
                "project_id": "dry1",
                "source_url": "https://talent.fintalent.io/brief/dry1",
                "title": "Dry",
            },
            "extraction_metadata": {},
            "missing_fields": [],
            "extraction_warnings": [],
        }]
        mock_fetch.return_value = {
            "ok": True,
            "detail_extraction_status": "COMPLETE",
            "fields": {"description": "hi"},
            "extraction_metadata": {},
            "missing_fields": [],
            "extraction_warnings": [],
            "detail_failure_code": None,
        }
        counts = process_scan_cycle(MagicMock(), "run", dry_run=True, send_emails=False)
        mock_db.insert_project_occurrence.assert_not_called()
        self.assertEqual(counts["projects_inserted"], 0)


class TestMonitoringInsertOrder(unittest.TestCase):
    @patch("script_clean.send_project_email")
    @patch("script_clean.fetch_project_details")
    @patch("script_clean.scan_for_card_extractions")
    @patch("script_clean.db")
    def test_project_inserted_before_email(self, mock_db, mock_scan, mock_fetch, mock_email):
        from script_clean import process_scan_cycle
        order = []

        def insert(row):
            order.append("insert")
            return "uuid-new"

        def email(uuid, row):
            order.append("email")
            return {"success": True}

        mock_db.should_process_project.return_value = (True, "NO_PREVIOUS_OCCURRENCE")
        mock_db.verify_lock_held.return_value = True
        mock_db.insert_project_occurrence.side_effect = insert
        mock_db.renew_worker_lock.return_value = {"renewed": True}
        mock_email.side_effect = email
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
        process_scan_cycle(MagicMock(), "run", dry_run=False, send_emails=True)
        self.assertEqual(order, ["insert", "email"])


if __name__ == "__main__":
    unittest.main()
