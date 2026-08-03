"""Detail extraction, merge, budget parsing, enrichment tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from extraction import (
    parse_budget_fields,
    merge_project_data,
    should_auto_enrich,
    classify_detail_page,
    extract_detail_project,
    build_project_row,
)


class TestBudgetParsing(unittest.TestCase):
    def test_hourly_rate(self):
        r = parse_budget_fields("$100/hour")
        self.assertEqual(r["billing_type"], "Hourly")
        self.assertEqual(r["hourly_rate"], 100.0)
        self.assertEqual(r["budget_currency"], "USD")

    def test_daily_rate(self):
        r = parse_budget_fields("€800/day")
        self.assertEqual(r["billing_type"], "Daily")
        self.assertEqual(r["daily_rate"], 800.0)
        self.assertEqual(r["budget_currency"], "EUR")

    def test_fixed_price(self):
        r = parse_budget_fields("£75,000 Fixed fee")
        self.assertEqual(r["billing_type"], "Fixed")
        self.assertEqual(r["budget_min"], 75000.0)

    def test_range(self):
        r = parse_budget_fields("$20,000–$30,000")
        self.assertEqual(r["budget_min"], 20000.0)
        self.assertEqual(r["budget_max"], 30000.0)

    def test_currency(self):
        r = parse_budget_fields("USD 500 per hour")
        self.assertEqual(r["budget_currency"], "USD")
        self.assertEqual(r["billing_type"], "Hourly")

    def test_description_figure_rejected_when_title_match(self):
        r = parse_budget_fields("$5M revenue company", title="$5M revenue company")
        self.assertIsNone(r["budget_text"])

    def test_title_figure_rejected(self):
        r = parse_budget_fields("CIO Search $200k", title="CIO Search $200k")
        self.assertIsNone(r["budget_text"])

    def test_oversized_rejected(self):
        r = parse_budget_fields("x" * 100)
        self.assertIsNone(r["budget_text"])


class TestMerge(unittest.TestCase):
    def test_empty_detail_does_not_overwrite_card(self):
        card = {"location": "London", "budget_text": "$100/hour", "extraction_metadata": {}}
        detail = {"location": "", "budget_text": None, "description": "Full desc", "extraction_metadata": {}}
        merged = merge_project_data(card, detail)
        self.assertEqual(merged["location"], "London")
        self.assertEqual(merged["budget_text"], "$100/hour")
        self.assertEqual(merged["description"], "Full desc")

    def test_valid_detail_replaces_card(self):
        card = {"location": "Remote", "extraction_metadata": {}}
        detail = {"location": "Remote · UTC+1 Europe", "extraction_metadata": {}}
        merged = merge_project_data(card, detail)
        self.assertEqual(merged["location"], "Remote · UTC+1 Europe")


class TestEnrichmentGuards(unittest.TestCase):
    def test_complete_row_not_enriched(self):
        row = {
            "detail_extraction_status": "COMPLETE",
            "detail_attempt_count": 0,
            "skills": [],
            "platform_category": None,
        }
        self.assertFalse(should_auto_enrich(row))

    def test_partial_row_enriched(self):
        row = {
            "detail_extraction_status": "PARTIAL",
            "detail_attempt_count": 0,
            "detail_last_attempt_at": None,
        }
        self.assertTrue(should_auto_enrich(row))

    def test_attempt_limit(self):
        row = {
            "detail_extraction_status": "FAILED",
            "detail_attempt_count": 3,
            "detail_last_attempt_at": None,
        }
        self.assertFalse(should_auto_enrich(row, max_attempts=3))

    def test_retry_cooldown(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        row = {
            "detail_extraction_status": "FAILED",
            "detail_attempt_count": 1,
            "detail_last_attempt_at": (now - timedelta(minutes=10)).isoformat(),
        }
        self.assertFalse(should_auto_enrich(row, cooldown_minutes=360, now=now))

    def test_failed_subject_to_bounds_after_cooldown(self):
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        row = {
            "detail_extraction_status": "FAILED",
            "detail_attempt_count": 1,
            "detail_last_attempt_at": (now - timedelta(minutes=400)).isoformat(),
        }
        self.assertTrue(should_auto_enrich(row, max_attempts=3, cooldown_minutes=360, now=now))


class TestDetailPageClassification(unittest.TestCase):
    def test_login_redirect(self):
        driver = MagicMock()
        driver.current_url = "https://talent.fintalent.io/login"
        self.assertEqual(classify_detail_page(driver), "LOGIN_REDIRECT")

    def test_timeout_path_via_extract(self):
        driver = MagicMock()
        driver.current_url = "https://talent.fintalent.io/brief/x"
        body = MagicMock()
        body.text = "short"
        driver.find_element.return_value = body
        # EMPTY_APP_SHELL when body too short
        self.assertEqual(classify_detail_page(driver), "EMPTY_APP_SHELL")


class TestCategoryNotPartial(unittest.TestCase):
    def test_not_exposed_in_row(self):
        row = build_project_row(
            {
                "project_id": "p1",
                "source_url": "https://talent.fintalent.io/brief/p1",
                "title": "T",
                "platform_category_extraction_status": "NOT_EXPOSED",
                "extraction_metadata": {
                    "fields_not_exposed": ["platform_category"],
                    "fields_missing_but_visible": [],
                    "fields_extracted": ["title"],
                    "fields_visible_on_page": ["title"],
                },
            },
            scraper_run_id=None,
            card_status="COMPLETE",
            detail_status="COMPLETE",
            email_eligible=False,
            email_status="SUPPRESSED",
            email_not_sent_reason="COLD_START_SEED",
        )
        self.assertEqual(row["platform_category_extraction_status"], "NOT_EXPOSED")
        self.assertEqual(row["card_extraction_status"], "COMPLETE")
        self.assertEqual(row["detail_extraction_status"], "COMPLETE")
        self.assertEqual(row["email_status"], "SUPPRESSED")


if __name__ == "__main__":
    unittest.main()
