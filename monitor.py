"""FinTalent production entrypoint — shared Supabase project-monitor architecture."""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# Ensure UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FinTalent marketplace monitor")
    p.add_argument("--run-once", action="store_true", help="Run a single scan cycle and exit")
    p.add_argument("--once", action="store_true", help="Alias for --run-once")
    p.add_argument("--debug", action="store_true", help="Verbose diagnostics / page dumps")
    p.add_argument("--debug-extraction", action="store_true", help="Print extraction field coverage")
    p.add_argument("--dry-run", action="store_true", help="Authenticate and scan without DB writes or emails")

    p.add_argument("--test-supabase", action="store_true", help="Verify Supabase connection and schema")
    p.add_argument("--test-login", action="store_true", help="Verify FinTalent authentication only")
    p.add_argument("--test-session-clear", action="store_true", help="Clear session cookies preserving worker lock")
    p.add_argument("--send-test-email", action="store_true", help="Explicitly send one test email (requires confirmation)")

    p.add_argument("--inspect-project", metavar="ID_OR_URL", help="Inspect a FinTalent project card/detail")
    p.add_argument("--backfill-missing-details", action="store_true", help="Enrich existing FinTalent rows")
    p.add_argument("--retry-pending-emails", action="store_true", help="Retry RETRY_PENDING emails")
    p.add_argument("--retry-failed", action="store_true", help="Include FAILED details in backfill")
    p.add_argument("--limit", type=int, default=50, help="Limit for backfill/enrichment")
    p.add_argument("--project-id", metavar="ID", help="Target a single source project_id")

    # Legacy unsafe --test is rejected
    p.add_argument("--test", action="store_true", help=argparse.SUPPRESS)
    return p


def cmd_test_supabase():
    import database as db
    info = db.test_supabase_connection()
    print(json.dumps({k: v for k, v in info.items() if k != "worker_lock_probe"}, default=str, indent=2))
    print("Supabase OK")
    return 0


def cmd_test_login():
    from script_clean import Config, initialize_driver, setup_session, validate_config
    validate_config(require_smtp=False)
    driver = initialize_driver()
    try:
        ok = setup_session(driver)
        print(f"Login {'OK' if ok else 'FAILED'} url={driver.current_url}")
        return 0 if ok else 1
    finally:
        driver.quit()


def cmd_test_session_clear():
    import database as db
    from script_clean import clear_session_safe, validate_config
    validate_config(require_smtp=False, require_fintalent=False)
    before = db.load_scraper_session_row()
    clear_session_safe()
    after = db.load_scraper_session_row()
    print(json.dumps({
        "before_saved_at": (before or {}).get("saved_at"),
        "before_lock_owner": (before or {}).get("worker_lock_owner"),
        "after_saved_at": (after or {}).get("saved_at"),
        "after_lock_owner": (after or {}).get("worker_lock_owner"),
        "after_cookies_empty": not bool(((after or {}).get("session_data") or {}).get("cookies")),
        "saved_at_is_null": (after or {}).get("saved_at") is None,
    }, indent=2))
    if after and after.get("saved_at") is None:
        print("FAIL: saved_at is null")
        return 1
    print("Session clear OK (lock preserved, saved_at non-null)")
    return 0


def cmd_send_test_email():
    from script_clean import Config, create_email_html, send_notification, validate_config
    validate_config(require_smtp=True, require_fintalent=False)
    print("About to send a TEST email to:")
    for r in Config.RECIPIENT_EMAILS:
        print(f"  - {r}")
    confirm = input("Type YES to send: ").strip()
    if confirm != "YES":
        print("Aborted.")
        return 1
    project = {
        "title": "FinTalent Monitor Test Email",
        "project_id": "test-email",
        "source_url": Config.TARGET_URL if hasattr(Config, "TARGET_URL") else "https://talent.fintalent.io/overview",
        "description": "This is an explicit test email from FinTalent monitor.",
        "location": "N/A",
        "budget_text": "N/A",
        "duration_text": "N/A",
        "skills": [],
        "detected_at": "test",
    }
    # Ensure TARGET_URL
    from script_clean import Config as C
    project["source_url"] = C.TARGET_URL
    result = send_notification(project)
    print(json.dumps(result, indent=2))
    return 0 if result.get("success") else 1


def cmd_inspect_project(id_or_url: str, *, debug_extraction=False):
    from script_clean import (
        Config, initialize_driver, setup_session, validate_config,
        find_project_cards, fetch_project_details, scan_for_card_extractions,
    )
    from extraction import extract_card_project, canonicalize_url, extract_id_from_url
    validate_config(require_smtp=False)
    driver = initialize_driver()
    try:
        if not setup_session(driver):
            print("Auth failed")
            return 1
        target_id = extract_id_from_url(id_or_url) if "://" in id_or_url or id_or_url.startswith("/") else id_or_url
        url = id_or_url if id_or_url.startswith("http") else None

        driver.get(Config.TARGET_URL)
        card_results = scan_for_card_extractions(driver)
        matched = None
        for r in card_results:
            if not r.get("ok"):
                continue
            f = r["fields"]
            if f.get("project_id") == target_id or (url and canonicalize_url(f.get("source_url")) == canonicalize_url(url)):
                matched = r
                url = f.get("source_url")
                break

        if not matched and url:
            matched = {"ok": True, "fields": {"project_id": target_id, "source_url": url, "title": ""}, "card_extraction_status": "UNKNOWN", "extraction_metadata": {}, "missing_fields": [], "extraction_warnings": []}
        if not matched:
            # try constructing URL
            if target_id:
                for path in (f"/brief/{target_id}", f"/project/{target_id}"):
                    candidate = "https://talent.fintalent.io" + path
                    print(f"Trying {candidate}")
                    detail = fetch_project_details(driver, candidate, card_fields={"project_id": target_id, "title": ""})
                    print(json.dumps({
                        "source_url": candidate,
                        "detail_status": detail.get("detail_extraction_status"),
                        "failure": detail.get("detail_failure_code"),
                        "fields": detail.get("fields"),
                        "meta": detail.get("extraction_metadata"),
                    }, default=str, indent=2)[:5000])
                    if detail.get("ok") or detail.get("fields"):
                        return 0
            print("Project not found on overview")
            return 1

        print("CARD:")
        print(json.dumps({
            "status": matched.get("card_extraction_status"),
            "fields": matched.get("fields"),
            "meta": matched.get("extraction_metadata"),
            "missing": matched.get("missing_fields"),
        }, default=str, indent=2)[:4000])

        detail = fetch_project_details(driver, matched["fields"]["source_url"], card_fields=matched["fields"])
        print("DETAIL:")
        print(json.dumps({
            "status": detail.get("detail_extraction_status"),
            "failure": detail.get("detail_failure_code"),
            "fields": detail.get("fields"),
            "meta": detail.get("extraction_metadata"),
            "missing": detail.get("missing_fields"),
            "warnings": detail.get("extraction_warnings"),
        }, default=str, indent=2)[:6000])
        return 0
    finally:
        driver.quit()


def cmd_backfill(args):
    import database as db
    from script_clean import (
        Config,
        initialize_driver,
        setup_session,
        validate_config,
        backfill_missing_details as do_backfill,
    )
    validate_config(require_smtp=False)
    lock = None
    if not args.dry_run:
        lock = db.acquire_worker_lock(Config.FINTALENT_WORKER_LOCK_TTL_SECONDS)
        if not lock.get("acquired"):
            print(f"Could not acquire lock: {lock}")
            return 2
    driver = initialize_driver()
    try:
        if not setup_session(driver):
            print("Auth failed")
            return 1
        result = do_backfill(
            driver,
            dry_run=args.dry_run,
            limit=args.limit,
            project_id=args.project_id,
            retry_failed=args.retry_failed,
        )
        print(json.dumps(result, indent=2))
        return 0
    finally:
        driver.quit()
        if lock and lock.get("acquired"):
            db.release_worker_lock()


def cmd_retry_emails(args):
    import database as db
    from script_clean import Config, validate_config, retry_pending_emails
    validate_config(require_smtp=not args.dry_run)
    if not args.dry_run:
        lock = db.acquire_worker_lock(Config.FINTALENT_WORKER_LOCK_TTL_SECONDS)
        if not lock.get("acquired"):
            print(f"Could not acquire lock: {lock}")
            return 2
    else:
        lock = None
    try:
        result = retry_pending_emails(dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    finally:
        if lock and lock.get("acquired"):
            db.release_worker_lock()


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.test:
        print(
            "ERROR: --test is disabled because it previously sent real emails unsafely.\n"
            "Use --send-test-email for an explicit test email,\n"
            "or --dry-run --run-once --debug-extraction for a safe scan."
        )
        return 2

    run_once = args.run_once or args.once

    if args.test_supabase:
        return cmd_test_supabase()
    if args.test_login:
        return cmd_test_login()
    if args.test_session_clear:
        return cmd_test_session_clear()
    if args.send_test_email:
        return cmd_send_test_email()
    if args.inspect_project:
        return cmd_inspect_project(args.inspect_project, debug_extraction=args.debug_extraction)
    if args.backfill_missing_details:
        return cmd_backfill(args)
    if args.retry_pending_emails:
        return cmd_retry_emails(args)

    from script_clean import run_monitor
    return run_monitor(
        run_once=run_once,
        dry_run=args.dry_run,
        debug=args.debug,
        debug_extraction=args.debug_extraction,
    )


if __name__ == "__main__":
    raise SystemExit(main())
