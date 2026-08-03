"""Supabase repository layer for FinTalent (shared project-monitor schema)."""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

PLATFORM = "fintalent"
SCRAPER_NAME = "fintalent-monitor"
SCRAPER_VERSION = os.getenv("SCRAPER_VERSION", "2.0.0")


def get_occurrence_window_days() -> int:
    """Days between eligible occurrences. Prefer OCCURRENCE_WINDOW_DAYS; fall back to REPOST_MIN_DAYS."""
    raw = os.getenv("OCCURRENCE_WINDOW_DAYS") or os.getenv("REPOST_MIN_DAYS") or "7"
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 7
    return max(days, 0)


def get_occurrence_window() -> timedelta:
    return timedelta(days=get_occurrence_window_days())

# Whitelisted columns for detail enrichment updates (never identity / email / scraped_at)
ENRICHMENT_FIELDS = frozenset({
    "short_description", "description", "status",
    "platform_category", "platform_category_path", "platform_category_raw",
    "platform_category_source", "platform_category_confidence",
    "platform_category_extraction_status",
    "location", "location_preference",
    "budget_text", "budget_min", "budget_max", "budget_currency",
    "billing_type", "hourly_rate", "daily_rate", "rate_currency",
    "budget_source", "budget_confidence",
    "duration_text", "project_length", "start_date_text", "source_start_date",
    "level_of_support", "industry", "contracting_process",
    "skills", "expertise", "deliverables",
    "engagement_type", "project_type", "workstream",
    "estimated_hours", "weekly_commitment",
    "remote_or_onsite", "country_or_region", "application_deadline",
    "time_posted_text", "source_posted_at", "source_posted_at_is_estimated",
    "card_extraction_status", "detail_extraction_status",
    "detail_attempt_count", "detail_last_attempt_at", "detail_completed_at",
    "detail_failure_code", "detail_last_error",
    "missing_fields", "extraction_warnings", "extraction_metadata", "raw_data",
    "last_seen_at",
})

EMAIL_UPDATE_FIELDS = frozenset({
    "email_eligible", "email_status", "email_sent", "email_not_sent_reason",
    "email_failure_code", "email_last_error", "email_attempt_count",
    "email_last_attempt_at", "email_next_retry_at", "email_sent_at",
    "email_message_id",
})

_client = None


class DatabaseError(Exception):
    """Structured database error with secrets redacted."""

    def __init__(self, message: str, *, code: str = "DB_ERROR", cause: Exception | None = None):
        self.code = code
        self.cause = cause
        super().__init__(_redact(str(message)))


def _redact(text: str) -> str:
    secrets = [
        os.getenv("SUPABASE_SECRET_KEY", ""),
        os.getenv("SUPABASE_SERVICE_ROLE_KEY", ""),
        os.getenv("FINTALENT_PASSWORD", ""),
        os.getenv("SENDER_PASSWORD", ""),
    ]
    out = text or ""
    for s in secrets:
        if s and len(s) > 4:
            out = out.replace(s, "***REDACTED***")
    return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    if dt is None:
        dt = utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            return None
    return None


def get_supabase_credentials() -> tuple[str, str]:
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
    key_source = "SUPABASE_SECRET_KEY"
    if not key:
        key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        key_source = "SUPABASE_SERVICE_ROLE_KEY"
    if not url or not key:
        raise DatabaseError(
            "Missing SUPABASE_URL and SUPABASE_SECRET_KEY (or SUPABASE_SERVICE_ROLE_KEY fallback)",
            code="CONFIG_MISSING",
        )
    if "anon" in key_source.lower() or key.startswith("eyJ") and "service" not in key_source.lower():
        # Prefer secret key; anon keys often start with eyJ — warn via structured check
        publishable = (os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY") or "").strip()
        if key == publishable and publishable:
            raise DatabaseError("Refusing to use anonymous/publishable Supabase key for scraper writes", code="CONFIG_INVALID")
    return url, key


def get_supabase_client():
    global _client
    if _client is not None:
        return _client
    from supabase import create_client

    url, key = get_supabase_credentials()
    _client = create_client(url, key)
    return _client


def reset_client() -> None:
    global _client
    _client = None


def _execute(query, *, context: str):
    try:
        result = query.execute()
        return result
    except Exception as e:
        raise DatabaseError(f"{context} failed: {e}", code="QUERY_FAILED", cause=e) from e


def ensure_schema_ready() -> dict[str, Any]:
    """Verify shared tables are reachable. Never treats failures as empty."""
    sb = get_supabase_client()
    required = ("projects", "scraper_runs", "email_attempts", "scraper_sessions")
    ready = {}
    for table in required:
        try:
            res = _execute(
                sb.table(table).select("*").limit(1),
                context=f"schema_check:{table}",
            )
            ready[table] = True
            ready[f"{table}_sample_count"] = len(res.data or [])
        except DatabaseError:
            raise
        except Exception as e:
            raise DatabaseError(f"Table {table} not ready: {e}", code="SCHEMA_NOT_READY", cause=e) from e

    # Probe worker lock RPCs exist
    try:
        probe = sb.rpc(
            "acquire_scraper_worker_lock",
            {"p_platform": "__schema_probe__", "p_owner": "schema_probe", "p_ttl_seconds": 1},
        ).execute()
        ready["worker_lock_rpc"] = True
        ready["worker_lock_probe"] = probe.data
        try:
            sb.rpc(
                "release_scraper_worker_lock",
                {"p_platform": "__schema_probe__", "p_owner": "schema_probe"},
            ).execute()
        except Exception:
            pass
    except Exception as e:
        raise DatabaseError(f"Worker lock RPCs not ready: {e}", code="SCHEMA_NOT_READY", cause=e) from e

    return ready


def test_supabase_connection() -> dict[str, Any]:
    info = ensure_schema_ready()
    sb = get_supabase_client()
    count_res = _execute(
        sb.table("projects").select("id", count="exact").eq("platform", PLATFORM),
        context="test_connection:fintalent_count",
    )
    info["fintalent_project_count"] = count_res.count
    info["platform"] = PLATFORM
    info["ok"] = True
    return info


def is_platform_cold_start() -> bool:
    """Cold start is platform-specific: only FinTalent rows matter."""
    sb = get_supabase_client()
    res = _execute(
        sb.table("projects").select("id").eq("platform", PLATFORM).limit(1),
        context="cold_start_check",
    )
    return len(res.data or []) == 0


# ---------------------------------------------------------------------------
# Scraper runs
# ---------------------------------------------------------------------------

def create_scraper_run(*, metadata: dict | None = None) -> str:
    sb = get_supabase_client()
    now = _iso()
    payload = {
        "platform": PLATFORM,
        "scraper_name": SCRAPER_NAME,
        "scraper_version": SCRAPER_VERSION,
        "started_at": now,
        "status": "RUNNING",
        "cards_found": 0,
        "cards_parsed": 0,
        "cards_failed": 0,
        "details_attempted": 0,
        "details_completed": 0,
        "details_failed": 0,
        "projects_inserted": 0,
        "projects_skipped": 0,
        "emails_sent": 0,
        "emails_failed": 0,
        "emails_suppressed": 0,
        "metadata": metadata or {},
    }
    res = _execute(sb.table("scraper_runs").insert(payload).select("id"), context="create_scraper_run")
    if not res.data or not res.data[0].get("id"):
        raise DatabaseError("create_scraper_run returned no id", code="INSERT_FAILED")
    return res.data[0]["id"]


def update_scraper_run_counts(run_id: str, **counts) -> None:
    allowed = {
        "cards_found", "cards_parsed", "cards_failed",
        "details_attempted", "details_completed", "details_failed",
        "projects_inserted", "projects_skipped",
        "emails_sent", "emails_failed", "emails_suppressed",
        "metadata", "failure_code", "failure_reason", "status",
    }
    payload = {k: v for k, v in counts.items() if k in allowed}
    if not payload:
        return
    payload["updated_at"] = _iso()
    sb = get_supabase_client()
    _execute(
        sb.table("scraper_runs").update(payload).eq("id", run_id),
        context="update_scraper_run_counts",
    )


def complete_scraper_run(
    run_id: str,
    *,
    status: str = "COMPLETED",
    failure_code: str | None = None,
    failure_reason: str | None = None,
    **counts,
) -> None:
    payload = {k: v for k, v in counts.items()}
    payload.update({
        "status": status,
        "completed_at": _iso(),
        "updated_at": _iso(),
        "failure_code": failure_code,
        "failure_reason": _redact(failure_reason) if failure_reason else None,
    })
    allowed = {
        "cards_found", "cards_parsed", "cards_failed",
        "details_attempted", "details_completed", "details_failed",
        "projects_inserted", "projects_skipped",
        "emails_sent", "emails_failed", "emails_suppressed",
        "metadata", "status", "completed_at", "updated_at",
        "failure_code", "failure_reason",
    }
    payload = {k: v for k, v in payload.items() if k in allowed}
    sb = get_supabase_client()
    _execute(sb.table("scraper_runs").update(payload).eq("id", run_id), context="complete_scraper_run")


def fail_scraper_run(run_id: str, *, failure_code: str, failure_reason: str, status: str = "FAILED") -> None:
    complete_scraper_run(
        run_id,
        status=status,
        failure_code=failure_code,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# Occurrence / projects
# ---------------------------------------------------------------------------

def get_latest_project_occurrence(platform: str, project_id: str) -> dict | None:
    sb = get_supabase_client()
    res = _execute(
        sb.table("projects")
        .select("*")
        .eq("platform", platform)
        .eq("project_id", project_id)
        .order("scraped_at", desc=True)
        .limit(1),
        context="get_latest_project_occurrence",
    )
    if res.data is None:
        raise DatabaseError("get_latest_project_occurrence returned null data", code="QUERY_FAILED")
    return res.data[0] if res.data else None


def should_process_project(platform: str, project_id: str, now: datetime | None = None) -> tuple[bool, str]:
    """Occurrence rule using scraped_at only. Window from OCCURRENCE_WINDOW_DAYS (or REPOST_MIN_DAYS).

    age > N days → eligible
    age <= N days → skip
    """
    now = now or utcnow()
    window_days = get_occurrence_window_days()
    latest = get_latest_project_occurrence(platform, project_id)
    if not latest:
        return True, "NO_PREVIOUS_OCCURRENCE"
    scraped_at = _parse_ts(latest.get("scraped_at"))
    if scraped_at is None:
        return True, "MISSING_SCRAPED_AT"
    age = now - scraped_at
    if age > timedelta(days=window_days):
        return True, f"eligible_after_{age.total_seconds():.0f}s"
    return False, f"skipped_within_{window_days}_days_age_{age.total_seconds():.0f}s"


def insert_project_occurrence(row: dict) -> str:
    sb = get_supabase_client()
    payload = dict(row)
    payload.setdefault("platform", PLATFORM)
    payload.setdefault("scraped_at", _iso())
    payload.setdefault("first_detected_at", payload["scraped_at"])
    payload.setdefault("last_seen_at", payload["scraped_at"])
    payload.setdefault("created_at", _iso())
    payload.setdefault("updated_at", _iso())
    res = _execute(
        sb.table("projects").insert(payload).select("id"),
        context="insert_project_occurrence",
    )
    if not res.data or not res.data[0].get("id"):
        raise DatabaseError("insert_project_occurrence returned no id", code="INSERT_FAILED")
    return res.data[0]["id"]


def get_project_by_uuid(project_uuid: str) -> dict | None:
    sb = get_supabase_client()
    res = _execute(
        sb.table("projects").select("*").eq("id", project_uuid).limit(1),
        context="get_project_by_uuid",
    )
    if res.data is None:
        raise DatabaseError("get_project_by_uuid returned null data", code="QUERY_FAILED")
    return res.data[0] if res.data else None


def update_project_details(project_uuid: str, fields: dict) -> None:
    payload = {k: v for k, v in fields.items() if k in ENRICHMENT_FIELDS}
    if not payload:
        return
    payload["updated_at"] = _iso()
    sb = get_supabase_client()
    _execute(
        sb.table("projects").update(payload).eq("id", project_uuid),
        context="update_project_details",
    )


def update_project_email_status(project_uuid: str, fields: dict) -> None:
    payload = {k: v for k, v in fields.items() if k in EMAIL_UPDATE_FIELDS}
    if not payload:
        return
    payload["updated_at"] = _iso()
    sb = get_supabase_client()
    _execute(
        sb.table("projects").update(payload).eq("id", project_uuid),
        context="update_project_email_status",
    )


def get_projects_needing_enrichment(
    *,
    limit: int = 50,
    project_id: str | None = None,
    retry_failed: bool = False,
    max_attempts: int = 3,
    cooldown_minutes: int = 360,
    override_limits: bool = False,
) -> list[dict]:
    sb = get_supabase_client()
    statuses = ["NOT_ATTEMPTED", "PARTIAL", "FAILED", "TIMEOUT"]
    if not retry_failed:
        statuses = ["NOT_ATTEMPTED", "PARTIAL", "TIMEOUT"]
    q = (
        sb.table("projects")
        .select("*")
        .eq("platform", PLATFORM)
        .in_("detail_extraction_status", statuses)
        .order("scraped_at", desc=True)
        .limit(limit)
    )
    if project_id:
        q = (
            sb.table("projects")
            .select("*")
            .eq("platform", PLATFORM)
            .eq("project_id", project_id)
            .limit(limit)
        )
    res = _execute(q, context="get_projects_needing_enrichment")
    rows = list(res.data or [])
    if override_limits or project_id:
        return rows

    now = utcnow()
    filtered = []
    for row in rows:
        if row.get("detail_extraction_status") == "COMPLETE":
            continue
        attempts = int(row.get("detail_attempt_count") or 0)
        if attempts >= max_attempts:
            continue
        last = _parse_ts(row.get("detail_last_attempt_at"))
        if last and (now - last) < timedelta(minutes=cooldown_minutes):
            continue
        filtered.append(row)
    return filtered


# ---------------------------------------------------------------------------
# Email attempts
# ---------------------------------------------------------------------------

def create_email_attempt(
    project_uuid: str,
    *,
    attempt_number: int,
    recipients: list[str],
    metadata: dict | None = None,
) -> str:
    sb = get_supabase_client()
    payload = {
        "project_id": project_uuid,
        "attempt_number": attempt_number,
        "status": "SENDING",
        "attempted_at": _iso(),
        "recipients": recipients,
        "provider": "smtp",
        "metadata": metadata or {},
    }
    res = _execute(
        sb.table("email_attempts").insert(payload).select("id"),
        context="create_email_attempt",
    )
    if not res.data or not res.data[0].get("id"):
        raise DatabaseError("create_email_attempt returned no id", code="INSERT_FAILED")
    return res.data[0]["id"]


def complete_email_attempt_success(attempt_id: str, *, message_id: str | None = None) -> None:
    sb = get_supabase_client()
    _execute(
        sb.table("email_attempts").update({
            "status": "SENT",
            "completed_at": _iso(),
            "message_id": message_id,
        }).eq("id", attempt_id),
        context="complete_email_attempt_success",
    )


def complete_email_attempt_failure(
    attempt_id: str,
    *,
    failure_code: str,
    failure_reason: str,
) -> None:
    sb = get_supabase_client()
    _execute(
        sb.table("email_attempts").update({
            "status": "FAILED",
            "completed_at": _iso(),
            "failure_code": failure_code,
            "failure_reason": _redact(failure_reason),
        }).eq("id", attempt_id),
        context="complete_email_attempt_failure",
    )


def get_retryable_email_projects(
    *,
    max_retries: int = 3,
    now: datetime | None = None,
    limit: int = 50,
) -> list[dict]:
    now = now or utcnow()
    sb = get_supabase_client()
    res = _execute(
        sb.table("projects")
        .select("*")
        .eq("platform", PLATFORM)
        .eq("email_status", "RETRY_PENDING")
        .lt("email_attempt_count", max_retries)
        .lte("email_next_retry_at", _iso(now))
        .order("email_next_retry_at", desc=False)
        .limit(limit),
        context="get_retryable_email_projects",
    )
    return list(res.data or [])


def compute_email_next_retry(attempt_count: int, base_minutes: int = 15) -> str:
    # Exponential backoff: base * 2^(attempt-1)
    minutes = base_minutes * (2 ** max(attempt_count - 1, 0))
    return _iso(utcnow() + timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# Sessions (cookies + localStorage) — preserve worker lock columns
# ---------------------------------------------------------------------------

def save_scraper_session(
    cookies: list,
    local_storage: dict,
    *,
    expires_at: datetime | None = None,
    session_version: int | None = None,
    metadata: dict | None = None,
) -> None:
    """Save session_data without clearing worker-lock columns."""
    sb = get_supabase_client()
    now = utcnow()
    session_data = {"cookies": cookies or [], "local_storage": local_storage or {}}
    existing = load_scraper_session_row()
    payload = {
        "platform": PLATFORM,
        "session_data": session_data,
        "saved_at": _iso(now),
        "expires_at": _iso(expires_at) if expires_at else _iso(now + timedelta(days=14)),
        "updated_at": _iso(now),
        "metadata": metadata if metadata is not None else (existing or {}).get("metadata") or {},
    }
    if session_version is not None:
        payload["session_version"] = session_version
    elif existing and existing.get("session_version") is not None:
        payload["session_version"] = existing["session_version"]
    else:
        payload["session_version"] = 1

    if existing:
        # Update only session fields — do not touch worker_lock_*
        _execute(
            sb.table("scraper_sessions")
            .update({
                "session_data": payload["session_data"],
                "saved_at": payload["saved_at"],
                "expires_at": payload["expires_at"],
                "session_version": payload["session_version"],
                "metadata": payload["metadata"],
                "updated_at": payload["updated_at"],
            })
            .eq("platform", PLATFORM),
            context="save_scraper_session",
        )
    else:
        payload["created_at"] = _iso(now)
        _execute(
            sb.table("scraper_sessions").insert(payload),
            context="save_scraper_session_insert",
        )


def load_scraper_session_row() -> dict | None:
    sb = get_supabase_client()
    res = _execute(
        sb.table("scraper_sessions").select("*").eq("platform", PLATFORM).limit(1),
        context="load_scraper_session_row",
    )
    if res.data is None:
        raise DatabaseError("load_scraper_session_row returned null data", code="QUERY_FAILED")
    return res.data[0] if res.data else None


def load_scraper_session() -> dict | None:
    """Return {cookies, local_storage, saved_at} or None."""
    row = load_scraper_session_row()
    if not row:
        return None
    data = row.get("session_data") or {}
    cookies = data.get("cookies") or []
    if not cookies:
        return None
    return {
        "cookies": cookies,
        "local_storage": data.get("local_storage") or {},
        "saved_at": row.get("saved_at"),
        "expires_at": row.get("expires_at"),
        "metadata": row.get("metadata") or {},
    }


def delete_scraper_session() -> None:
    """Clear cookies/localStorage but preserve worker-lock columns. Never set saved_at=null."""
    sb = get_supabase_client()
    now = utcnow()
    existing = load_scraper_session_row()
    if not existing:
        # Ensure row exists for lock use without null saved_at
        _execute(
            sb.table("scraper_sessions").insert({
                "platform": PLATFORM,
                "session_data": {"cookies": [], "local_storage": {}},
                "saved_at": _iso(now),
                "expires_at": _iso(now),
                "session_version": 1,
                "metadata": {"cleared": True},
                "created_at": _iso(now),
                "updated_at": _iso(now),
            }),
            context="delete_scraper_session_insert",
        )
        return
    _execute(
        sb.table("scraper_sessions")
        .update({
            "session_data": {"cookies": [], "local_storage": {}},
            "saved_at": _iso(now),
            "expires_at": _iso(now),
            "metadata": {**(existing.get("metadata") or {}), "cleared": True},
            "updated_at": _iso(now),
        })
        .eq("platform", PLATFORM),
        context="delete_scraper_session",
    )


# ---------------------------------------------------------------------------
# Distributed worker lock
# ---------------------------------------------------------------------------

def worker_lock_owner_id() -> str:
    return (
        os.getenv("RAILWAY_DEPLOYMENT_ID")
        or os.getenv("RAILWAY_REPLICA_ID")
        or socket.gethostname()
        or "fintalent-worker"
    )


def acquire_worker_lock(ttl_seconds: int | None = None) -> dict:
    ttl = int(ttl_seconds or os.getenv("FINTALENT_WORKER_LOCK_TTL_SECONDS", "180"))
    owner = worker_lock_owner_id()
    sb = get_supabase_client()
    try:
        res = sb.rpc(
            "acquire_scraper_worker_lock",
            {"p_platform": PLATFORM, "p_owner": owner, "p_ttl_seconds": ttl},
        ).execute()
    except Exception as e:
        raise DatabaseError(f"acquire_worker_lock failed: {e}", code="LOCK_FAILED", cause=e) from e
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else {}
    if not data or not data.get("acquired"):
        return {"acquired": False, "owner": (data or {}).get("owner"), "self_owner": owner, **(data or {})}
    return {"acquired": True, "self_owner": owner, **data}


def renew_worker_lock(ttl_seconds: int | None = None) -> dict:
    ttl = int(ttl_seconds or os.getenv("FINTALENT_WORKER_LOCK_TTL_SECONDS", "180"))
    owner = worker_lock_owner_id()
    sb = get_supabase_client()
    try:
        res = sb.rpc(
            "renew_scraper_worker_lock",
            {"p_platform": PLATFORM, "p_owner": owner, "p_ttl_seconds": ttl},
        ).execute()
    except Exception as e:
        raise DatabaseError(f"renew_worker_lock failed: {e}", code="LOCK_FAILED", cause=e) from e
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else {}
    renewed = bool(data and data.get("renewed"))
    return {"renewed": renewed, "self_owner": owner, **(data or {})}


def release_worker_lock() -> dict:
    owner = worker_lock_owner_id()
    sb = get_supabase_client()
    try:
        res = sb.rpc(
            "release_scraper_worker_lock",
            {"p_platform": PLATFORM, "p_owner": owner},
        ).execute()
    except Exception as e:
        raise DatabaseError(f"release_worker_lock failed: {e}", code="LOCK_FAILED", cause=e) from e
    data = res.data
    if isinstance(data, list):
        data = data[0] if data else {}
    return {"released": bool(data and data.get("released")), "self_owner": owner, **(data or {})}


def verify_lock_held() -> bool:
    """Fail-closed lock verification."""
    try:
        row = load_scraper_session_row()
        if not row:
            return False
        owner = worker_lock_owner_id()
        if row.get("worker_lock_owner") != owner:
            return False
        expires = _parse_ts(row.get("worker_lock_expires_at"))
        if not expires or expires <= utcnow():
            return False
        return True
    except Exception:
        return False
