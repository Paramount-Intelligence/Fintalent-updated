#!/usr/bin/env python3
"""Optional one-off migration: MongoDB FinTalent history → shared Supabase projects.

Production runtime must NOT import this module.
Does not delete MongoDB data. Defaults to --dry-run safety when requested.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()


def parse_detected_at(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            dt = datetime.strptime(str(value), fmt)
            if dt.tzinfo is None:
                # Stored as PKT historically — treat as UTC+5 then convert
                from datetime import timedelta
                dt = dt.replace(tzinfo=timezone(timedelta(hours=5))).astimezone(timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def map_doc(doc: dict) -> dict:
    scraped = parse_detected_at(doc.get("detected_at")) or datetime.now(timezone.utc)
    iso = scraped.isoformat()
    duration = doc.get("duration") or None
    # Heuristic: billing-like duration → billing_type
    billing = None
    engagement = None
    duration_text = duration
    if duration and str(duration).strip().lower() in ("hourly", "daily", "fixed"):
        billing = str(duration).strip().title()
        duration_text = None

    raw = {"migrated_from": "mongodb", "original": {}}
    for k, v in doc.items():
        if k not in (
            "project_id", "title", "description", "location", "budget", "duration",
            "time_posted", "status", "url", "detected_at", "platform", "emailed",
            "skills", "start_date", "industry", "client_type", "_id",
        ):
            raw["original"][k] = v
    if doc.get("client_type"):
        raw["fintalent_client_type"] = doc.get("client_type")

    emailed = bool(doc.get("emailed"))
    return {
        "platform": "fintalent",
        "project_id": str(doc.get("project_id") or ""),
        "source_url": doc.get("url") or f"https://talent.fintalent.io/brief/{doc.get('project_id')}",
        "title": doc.get("title"),
        "short_description": (doc.get("description") or "")[:500] or None,
        "description": doc.get("description"),
        "status": doc.get("status") if doc.get("status") not in (None, "New Project") else doc.get("status"),
        "location": doc.get("location") or None,
        "budget_text": doc.get("budget") or None,
        "billing_type": billing,
        "duration_text": duration_text,
        "project_length": duration_text,
        "time_posted_text": None if (doc.get("time_posted") or "").lower() == "recently" else doc.get("time_posted"),
        "skills": doc.get("skills") or [],
        "start_date_text": doc.get("start_date") or None,
        "industry": doc.get("industry") or None,
        "platform_category_extraction_status": "NOT_EXPOSED",
        "platform_category_path": [],
        "card_extraction_status": "PARTIAL",
        "detail_extraction_status": "NOT_ATTEMPTED",
        "missing_fields": [],
        "extraction_warnings": ["migrated_from_mongodb"],
        "extraction_metadata": {"migrated": True},
        "raw_data": raw,
        "scraped_at": iso,
        "first_detected_at": iso,
        "last_seen_at": iso,
        "email_eligible": False,
        "email_status": "SUPPRESSED" if not emailed else "SENT",
        "email_sent": emailed,
        "email_not_sent_reason": None if emailed else "MONGODB_MIGRATION",
        "source_posted_at_is_estimated": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        print("MONGO_URI required for migration script only")
        return 1

    try:
        from pymongo import MongoClient
    except ImportError:
        print("Install pymongo to run migration: pip install pymongo dnspython")
        return 1

    from supabase import create_client
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SECRET_KEY required")
        return 1

    client = MongoClient(mongo_uri)
    coll = client["office_monitor"]["projects"]
    query = {"platform": "fintalent"}
    cursor = coll.find(query)
    if args.limit:
        cursor = cursor.limit(args.limit)

    sb = create_client(url, key)
    migrated = skipped = 0
    for doc in cursor:
        row = map_doc(doc)
        if not row["project_id"]:
            skipped += 1
            continue
        print(f"{'[dry-run] ' if args.dry_run else ''}migrate {row['project_id']}: {row.get('title', '')[:50]}")
        if args.dry_run:
            migrated += 1
            continue
        try:
            sb.table("projects").insert(row).execute()
            migrated += 1
        except Exception as e:
            print(f"  skip/error: {e}")
            skipped += 1
    print(f"Done migrated={migrated} skipped={skipped} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
