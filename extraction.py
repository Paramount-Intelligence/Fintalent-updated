"""FinTalent card/detail extraction, identity, merge, and field parsing."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

PLATFORM = "fintalent"
BASE_URL = "https://talent.fintalent.io"

PLATFORM_CAPABILITIES = {
    "catalant": {"category_exposed": True},
    "btg": {"category_exposed": False},
    "movemeon": {"category_exposed": False},
    # Determined from live DOM: FinTalent does not expose a dedicated category field.
    "fintalent": {"category_exposed": False},
}

CARD_SELECTORS = [
    "div.brief-card",
    "div[class*='brief-card']",
    "div[class*='project-card']",
    ".project-card",
    "article",
    "[class*='opportunity-card']",
    "//div[contains(@class, 'card') and .//a[contains(@href, '/brief/') or contains(@href, '/project/')]]",
]

TITLE_SELECTORS = [
    "h3", "h4", "h2", "h5",
    ".title", "[class*='title']",
    "a[href*='brief']", "a[href*='project']",
]

LINK_SELECTORS = [
    "a[href*='/brief/']",
    "a[href*='/project/']",
    "a.brief-link",
    "a[class*='link']",
]

DESCRIPTION_SELECTORS = [
    "[class*='StyledBoxDescrip']",
    "[class*='Descrip']",
    "[class*='brief-description']",
    "[class*='BriefDescription']",
    "[data-testid*='description']",
    ".description",
    "[class*='description']",
    "[class*='Description']",
    "[class*='brief-details']",
    "[class*='project-description']",
    "section[class*='description']",
]

# FinTalent application-pipeline labels are not project status
PIPELINE_STATUS_NOISE = frozenset({
    "be the first to apply",
    "first applicant in",
    "shortlisting",
    "shortlisted",
    "client started interviewing",
    "offer requested",
    "hired",
    "archived",
})

STATUS_PREFERRED_SELECTORS = [
    "h4.status-label",
    ".status-label",
    "[class*='status-label']",
    "[class*='project-detail-top-notification'] h4",
    "[class*='StatusTag']",
]

EXPAND_BUTTON_TEXTS = ("read more", "show more", "see more", "view more", "expand")

PLACEHOLDER_VALUES = frozenset({
    "", "n/a", "na", "none", "null", "not provided", "not specified",
    "recently", "new project", "-", "—", "unknown",
})

CORE_CARD_FIELDS = (
    "title", "project_id", "source_url", "short_description", "status",
    "location", "budget_text", "duration_text", "time_posted_text",
)

CORE_DETAIL_FIELDS = (
    "description", "status", "location", "budget_text", "duration_text",
    "skills", "start_date_text",
)


def field_result(value, *, raw_value=None, source="", selector_or_label="", confidence="HIGH"):
    return {
        "value": value,
        "raw_value": raw_value if raw_value is not None else value,
        "source": source,
        "selector_or_label": selector_or_label,
        "confidence": confidence,
    }


def _empty_meta():
    return {
        "fields_visible_on_page": [],
        "fields_extracted": [],
        "fields_missing_but_visible": [],
        "fields_not_exposed": [],
        "platform_capabilities": dict(PLATFORM_CAPABILITIES.get(PLATFORM, {})),
        "rejected_candidates": [],
    }


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    if isinstance(value, str) and value.strip().lower() in PLACEHOLDER_VALUES:
        return True
    return False


def _first_text(parent, selectors, max_len=200):
    for sel in selectors:
        try:
            if sel.startswith("//"):
                elems = parent.find_elements("xpath", sel)
            else:
                elems = parent.find_elements("css selector", sel)
            for e in elems:
                t = (e.text or "").strip()
                if t:
                    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
                    t = " ".join(lines)
                    return t[:max_len], sel
        except Exception:
            continue
    return "", ""


def is_time_string(t: str) -> bool:
    t_low = (t or "").lower()
    return (
        "ago" in t_low
        or "just now" in t_low
        or "•" in t
        or bool(re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b.*\d{4}", t_low))
        or bool(re.search(r"\d+\s*(hour|min|minute|day|week|month)s?\s*ago", t_low))
        or bool(re.search(r"^\d+\s*(hour|min|minute|day|week|month)s?$", t_low))
    )


def clean_time(t: str) -> str:
    return re.sub(r"\s*•\s*", "", t or "").strip()


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        url = urljoin(BASE_URL, url)
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def extract_id_from_url(url: str) -> str | None:
    if not url:
        return None
    m = re.search(r"/(?:brief|project)s?/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def resolve_project_identity(card=None, *, href: str | None = None, data_attrs: dict | None = None) -> dict:
    """
    Identity priority:
      1. Stable source ID from canonical FinTalent URL
      2. Verified project/brief data attribute
      3. Verified embedded structured project ID
      4. Canonical source-URL SHA-256 hash as last resort
    Never uses title hash or synthetic URLs.
    """
    meta = {
        "project_id_source": None,
        "project_id_confidence": None,
        "rejected": False,
        "reject_reason": None,
    }
    url = canonicalize_url(href or "")
    project_id = extract_id_from_url(url) if url else None

    if project_id and url:
        meta["project_id_source"] = "canonical_url"
        meta["project_id_confidence"] = "HIGH"
        return {"project_id": project_id, "source_url": url, **meta}

    data_attrs = data_attrs or {}
    for key in ("data-project-id", "data-brief-id", "data-id", "data-opportunity-id"):
        val = (data_attrs.get(key) or "").strip()
        if val and re.fullmatch(r"[a-zA-Z0-9_-]{6,}", val):
            if not url and card is not None:
                # still need a real URL
                pass
            meta["project_id_source"] = "data_attribute"
            meta["project_id_confidence"] = "HIGH"
            if url:
                return {"project_id": val, "source_url": url, **meta}
            meta["rejected"] = True
            meta["reject_reason"] = "DATA_ATTR_WITHOUT_URL"
            return {"project_id": None, "source_url": None, **meta}

    # Try reading attributes from card element
    if card is not None and not project_id:
        try:
            for attr in ("data-project-id", "data-brief-id", "data-id"):
                val = (card.get_attribute(attr) or "").strip()
                if val and re.fullmatch(r"[a-zA-Z0-9_-]{6,}", val):
                    meta["project_id_source"] = "data_attribute"
                    meta["project_id_confidence"] = "MEDIUM"
                    if url:
                        return {"project_id": val, "source_url": url, **meta}
        except Exception:
            pass

        # Embedded JSON-ish id on card
        try:
            html = card.get_attribute("outerHTML") or ""
            m = re.search(r'"(?:projectId|briefId|id)"\s*:\s*"([a-zA-Z0-9_-]{6,})"', html)
            if m and url:
                meta["project_id_source"] = "embedded_data"
                meta["project_id_confidence"] = "MEDIUM"
                return {"project_id": m.group(1), "source_url": url, **meta}
        except Exception:
            pass

    # Canonical URL hash last resort — requires a real URL
    if url and not project_id:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        meta["project_id_source"] = "canonical_url_hash"
        meta["project_id_confidence"] = "LOW"
        return {"project_id": digest, "source_url": url, **meta}

    meta["rejected"] = True
    meta["reject_reason"] = "UNSTABLE_IDENTITY"
    return {"project_id": None, "source_url": None, **meta}


# ---------------------------------------------------------------------------
# Budget / rate parsing
# ---------------------------------------------------------------------------

_CURRENCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "CHF": "CHF"}


def _parse_amount(text: str) -> float | None:
    m = re.search(r"([\d,]+(?:\.\d+)?)", text.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_budget_fields(budget_text: str | None, *, title: str = "", description: str = "") -> dict:
    """Parse budget/rate from a verified candidate string. Reject title/description figures."""
    result = {
        "budget_text": None,
        "budget_min": None,
        "budget_max": None,
        "budget_currency": None,
        "billing_type": None,
        "hourly_rate": None,
        "daily_rate": None,
        "rate_currency": None,
        "budget_source": None,
        "budget_confidence": None,
    }
    text = (budget_text or "").strip()
    if not text or is_placeholder(text):
        return result
    if len(text) > 80:
        return result
    if title and text.strip().lower() == title.strip().lower():
        return result
    if description and text in description and len(text) > 20:
        # figure embedded in prose — reject if looks like narrative
        if not re.search(r"(hour|day|fixed|fee|/hr|/day)", text, re.I):
            return result

    result["budget_text"] = text
    result["budget_source"] = "dedicated_container"
    result["budget_confidence"] = "HIGH"

    low = text.lower()
    currency = None
    for sym, code in _CURRENCY_MAP.items():
        if sym in text:
            currency = code
            break
    for code in ("USD", "EUR", "GBP", "CHF"):
        if code in text.upper():
            currency = code
            break
    result["budget_currency"] = currency
    result["rate_currency"] = currency

    if any(w in low for w in ("hourly", "per hour", "/hour", "/hr", "an hour")):
        result["billing_type"] = "Hourly"
        amt = _parse_amount(text)
        if amt is not None:
            result["hourly_rate"] = amt
            result["budget_min"] = amt
            result["budget_max"] = amt
    elif any(w in low for w in ("daily", "per day", "/day", "a day")):
        result["billing_type"] = "Daily"
        amt = _parse_amount(text)
        if amt is not None:
            result["daily_rate"] = amt
            result["budget_min"] = amt
            result["budget_max"] = amt
    elif any(w in low for w in ("fixed", "fee", "project")):
        result["billing_type"] = "Fixed"
        # Range like $20,000–$30,000
        range_m = re.search(
            r"([\$€£]?[\d,]+(?:\.\d+)?)\s*[-–—to]+\s*([\$€£]?[\d,]+(?:\.\d+)?)",
            text,
            re.I,
        )
        if range_m:
            result["budget_min"] = _parse_amount(range_m.group(1))
            result["budget_max"] = _parse_amount(range_m.group(2))
        else:
            amt = _parse_amount(text)
            if amt is not None:
                result["budget_min"] = amt
                result["budget_max"] = amt
    else:
        # bare amount / range
        range_m = re.search(
            r"([\$€£]?[\d,]+(?:\.\d+)?)\s*[-–—to]+\s*([\$€£]?[\d,]+(?:\.\d+)?)",
            text,
            re.I,
        )
        if range_m:
            result["budget_min"] = _parse_amount(range_m.group(1))
            result["budget_max"] = _parse_amount(range_m.group(2))
            result["billing_type"] = "Fixed"
        elif re.search(r"[\$€£]", text) or currency:
            amt = _parse_amount(text)
            if amt is not None:
                result["budget_min"] = amt
                result["budget_max"] = amt
        elif low in ("hourly", "daily", "fixed"):
            result["billing_type"] = text.strip().title()

    return result


def classify_duration_or_engagement(text: str) -> dict:
    """Separate duration vs billing vs engagement type."""
    out = {
        "duration_text": None,
        "project_length": None,
        "billing_type": None,
        "engagement_type": None,
    }
    t = (text or "").strip()
    if not t or is_time_string(t):
        return out
    low = t.lower()
    if low in ("hourly", "daily", "fixed") or re.search(r"\b(hourly|daily|fixed)\b", low):
        if any(w in low for w in ("hour", "daily", "fixed")) and not any(
            w in low for w in ("week", "month", "year", "full-time", "part-time")
        ):
            if "hour" in low:
                out["billing_type"] = "Hourly"
            elif "daily" in low or "day" in low:
                out["billing_type"] = "Daily"
            elif "fixed" in low:
                out["billing_type"] = "Fixed"
            return out
    if any(w in low for w in ("full-time", "full time", "part-time", "part time", "fractional", "interim")):
        out["engagement_type"] = t
        return out
    if any(w in low for w in ("week", "month", "year", "day")) and "ago" not in low:
        out["duration_text"] = t
        out["project_length"] = t
    return out


def parse_posted_time(text: str | None, *, now: datetime | None = None) -> dict:
    """Parse relative/absolute posted times. Never invents 'Recently'."""
    now = now or datetime.now(timezone.utc)
    result = {
        "time_posted_text": None,
        "source_posted_at": None,
        "source_posted_at_is_estimated": False,
    }
    t = clean_time(text or "")
    if not t or t.lower() in PLACEHOLDER_VALUES:
        return result
    result["time_posted_text"] = t
    low = t.lower().strip()

    if low in ("just now", "moments ago", "now"):
        result["source_posted_at"] = now.isoformat()
        result["source_posted_at_is_estimated"] = True
        return result

    m = re.search(r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?\s*ago", low)
    if not m:
        m = re.search(r"^(\d+)\s*(minute|min|hour|hr|day|week|month)s?$", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "minute": timedelta(minutes=n),
            "min": timedelta(minutes=n),
            "hour": timedelta(hours=n),
            "hr": timedelta(hours=n),
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }.get(unit, timedelta())
        result["source_posted_at"] = (now - delta).isoformat()
        result["source_posted_at_is_estimated"] = True
        return result

    # Absolute date e.g. Aug 2, 2026
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
            result["source_posted_at"] = dt.isoformat()
            result["source_posted_at_is_estimated"] = True  # no time component
            return result
        except ValueError:
            continue

    result["source_posted_at_is_estimated"] = False
    return result


def normalize_skills(values) -> list[str]:
    if not values:
        return []
    if isinstance(values, str):
        # Only split if clearly a tag list (short segments)
        parts = [p.strip() for p in re.split(r"[,|;]", values) if p.strip()]
        if any(len(p) > 60 for p in parts) or len(values) > 200:
            return []
        values = parts
    seen = set()
    out = []
    for v in values:
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or len(s) > 60:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def classify_location(text: str) -> dict:
    out = {
        "location": None,
        "location_preference": None,
        "remote_or_onsite": None,
        "country_or_region": None,
        "timezone_requirement": None,
    }
    t = (text or "").strip()
    if not t:
        return out
    low = t.lower()
    out["location"] = t
    if "remote" in low:
        out["remote_or_onsite"] = "Remote"
    elif "hybrid" in low:
        out["remote_or_onsite"] = "Hybrid"
    elif "onsite" in low or "on-site" in low:
        out["remote_or_onsite"] = "Onsite"
    if any(tz in low for tz in ("utc", "gmt", "cet", "est", "pst", "timezone", "time zone", "tz")):
        out["location_preference"] = t
        out["timezone_requirement"] = t
    return out


# ---------------------------------------------------------------------------
# Icon / text helpers shared by card & detail
# ---------------------------------------------------------------------------

def _icon_text(parent, icon_selectors, ancestor_xpath=None):
    ancestor_xpath = ancestor_xpath or (
        "./ancestor::div[contains(@class,'Flex') or"
        " contains(@class,'Option') or contains(@class,'Item') or contains(@class,'styled')][1]"
    )
    for sel in icon_selectors:
        try:
            icon = parent.find_element("css selector", sel)
            container = icon.find_element("xpath", ancestor_xpath)
            txt = (container.text or "").strip()
            if txt:
                return txt, sel
        except Exception:
            continue
    return "", ""


def _status_from_element(parent) -> tuple[str, str]:
    for sel in STATUS_PREFERRED_SELECTORS + [
        "[class*='status']",
        "[class*='badge']",
        "[class*='stage']",
        "[class*='pill']",
        "[class*='Tag']",
    ]:
        try:
            elems = parent.find_elements("css selector", sel)
            texts = []
            for e in elems:
                t = (e.text or "").strip()
                if not t or len(t) >= 80:
                    continue
                low = t.lower()
                if low in PIPELINE_STATUS_NOISE:
                    continue
                if "open for applications" in low or "project closed" in low or "archived" in low:
                    return t.split("\n")[0].strip(), sel
                if low not in ("new",):
                    texts.append(t)
            # Prefer explicit open/closed phrases
            for t in texts:
                low = t.lower()
                if "project open" in low or "project closed" in low or "closed" == low:
                    return t, sel
            if texts:
                # Single clean status-label style value
                if sel in STATUS_PREFERRED_SELECTORS:
                    return texts[0], sel
        except Exception:
            continue
    return "", ""


def clean_notification_title(raw: str) -> str:
    """Overview feed items wrap titles in notification prose."""
    t = re.sub(r"\s+", " ", (raw or "").strip())
    patterns = [
        r"open for applications:\s*(.+)$",
        r"an update was posted to\s+(.+)$",
        r"new project[^:]*:\s*(.+)$",
    ]
    low = t.lower()
    for pat in patterns:
        m = re.search(pat, t, re.I)
        if m:
            return m.group(1).strip(" •|-")
    # Strip leading relative time prefixes
    t2 = re.sub(r"^\d+\s*(hour|min|minute|day|week|month)s?\s*ago\s*[•·-]?\s*", "", t, flags=re.I)
    t2 = re.sub(r"^[^:]*🌟\s*", "", t2).strip()
    return t2 or t


def parse_posted_from_detail_text(body_text: str) -> dict:
    m = re.search(r"Posted:\s*([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", body_text or "")
    if m:
        return parse_posted_time(m.group(1))
    return {
        "time_posted_text": None,
        "source_posted_at": None,
        "source_posted_at_is_estimated": False,
    }


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------

def extract_card_project(card) -> dict:
    """Extract a FinTalent overview card into shared project fields."""
    meta = _empty_meta()
    fields = {}
    warnings = []

    title, title_sel = _first_text(card, TITLE_SELECTORS, 150)
    if not title:
        return {
            "ok": False,
            "card_extraction_status": "FAILED",
            "reject_reason": "MISSING_TITLE",
            "fields": {},
            "extraction_metadata": meta,
            "missing_fields": ["title"],
            "extraction_warnings": ["missing_title"],
        }
    title = re.sub(r"\s*\n\s*", " ", title).strip()
    title = clean_notification_title(title)
    fields["title"] = title
    meta["fields_visible_on_page"].append("title")
    meta["fields_extracted"].append("title")

    # URL / identity
    href = None
    for sel in LINK_SELECTORS:
        try:
            link_elem = card.find_element("css selector", sel)
            href = link_elem.get_attribute("href")
            if href:
                break
        except Exception:
            continue
    if not href:
        try:
            for a in card.find_elements("tag name", "a"):
                h = a.get_attribute("href") or ""
                if "brief" in h or "project" in h:
                    href = h
                    break
        except Exception:
            pass

    identity = resolve_project_identity(card, href=href)
    if identity.get("rejected") or not identity.get("project_id") or not identity.get("source_url"):
        return {
            "ok": False,
            "card_extraction_status": "FAILED",
            "reject_reason": identity.get("reject_reason") or "UNSTABLE_IDENTITY",
            "fields": {"title": title},
            "extraction_metadata": {**meta, "identity": identity},
            "missing_fields": ["project_id", "source_url"],
            "extraction_warnings": ["unstable_identity"],
        }

    fields["project_id"] = identity["project_id"]
    fields["source_url"] = identity["source_url"]
    meta["fields_visible_on_page"].extend(["project_id", "source_url"])
    meta["fields_extracted"].extend(["project_id", "source_url"])
    meta["project_id_source"] = identity.get("project_id_source")
    meta["project_id_confidence"] = identity.get("project_id_confidence")

    location = ""
    budget = ""
    duration = ""
    time_posted = None
    short_description = ""

    # Duration / clock
    txt, sel = _icon_text(card, [
        ".fintalent-icon-clock", "[class*='fintalent-icon-clock']", "[class*='icon-clock']",
    ])
    if txt:
        if is_time_string(txt):
            time_posted = clean_time(txt)
            meta["fields_visible_on_page"].append("time_posted_text")
        else:
            classified = classify_duration_or_engagement(txt)
            if classified["duration_text"]:
                duration = classified["duration_text"]
                meta["fields_visible_on_page"].append("duration_text")
            if classified["billing_type"]:
                fields["billing_type"] = classified["billing_type"]
            if classified["engagement_type"]:
                fields["engagement_type"] = classified["engagement_type"]

    # Location
    txt, sel = _icon_text(card, [
        ".fintalent-icon-map-pin", "[class*='map-pin']", "[class*='location']", "[class*='pin']",
    ])
    if txt and not is_time_string(txt):
        location = txt
        meta["fields_visible_on_page"].append("location")

    # Budget
    txt, sel = _icon_text(card, [
        ".fintalent-icon-wallet", "[class*='wallet']",
        ".fintalent-icon-swap", "[class*='swap']",
        "[class*='money']", "[class*='cash']", "[class*='dollar']",
        "[class*='currency']", "[class*='rate']",
    ])
    if txt and not is_time_string(txt) and len(txt) < 80:
        budget = txt
        meta["fields_visible_on_page"].append("budget_text")

    status, status_sel = _status_from_element(card)
    if status:
        meta["fields_visible_on_page"].append("status")

    # Short description — skip notification-feed fluff
    try:
        paras = card.find_elements("css selector", "p, [class*='subtitle'], [class*='summary'], [class*='excerpt']")
        for p in paras:
            t = (p.text or "").strip()
            if not t or t == title or len(t) < 40 or len(t) > 500:
                continue
            low = t.lower()
            if "open for applications" in low or "update was posted" in low:
                continue
            short_description = t
            meta["fields_visible_on_page"].append("short_description")
            break
    except Exception:
        pass

    # Text-node fallback — restricted, no invented defaults
    try:
        text_elems = card.find_elements("xpath", ".//*[not(child::*)]")
        for elem in text_elems:
            t = (elem.text or "").strip()
            if not t:
                continue
            if is_time_string(t):
                candidate = clean_time(t)
                if candidate and len(candidate) < 60 and not time_posted:
                    time_posted = candidate
                    if "time_posted_text" not in meta["fields_visible_on_page"]:
                        meta["fields_visible_on_page"].append("time_posted_text")
                continue
            if not budget and len(t) < 60:
                if (
                    any(c in t for c in ("$", "€", "£", "¥"))
                    or any(w in t.upper() for w in ("EUR", "USD", "GBP", "CHF"))
                    or any(w in t.lower() for w in ("hourly", "daily", "fixed", "per hour", "per day"))
                ):
                    # Must not equal title
                    if t.strip().lower() != title.strip().lower():
                        budget = t
                        if "budget_text" not in meta["fields_visible_on_page"]:
                            meta["fields_visible_on_page"].append("budget_text")
            elif not duration and len(t) < 60:
                classified = classify_duration_or_engagement(t)
                if classified["duration_text"]:
                    duration = classified["duration_text"]
                    if "duration_text" not in meta["fields_visible_on_page"]:
                        meta["fields_visible_on_page"].append("duration_text")
                elif classified["billing_type"] and not fields.get("billing_type"):
                    fields["billing_type"] = classified["billing_type"]
                elif classified["engagement_type"] and not fields.get("engagement_type"):
                    fields["engagement_type"] = classified["engagement_type"]
            elif not location and len(t) < 150:
                if any(w in t.lower() for w in (
                    "remote", "hybrid", "onsite", "on-site",
                    "utc", "gmt", "cet", "est", "pst",
                    "europe", "london", "dublin", "paris",
                    "germany", "france", "italy", "spain",
                    "united", "casablanca", "lisbon",
                )):
                    location = t
                    if "location" not in meta["fields_visible_on_page"]:
                        meta["fields_visible_on_page"].append("location")
    except Exception:
        pass

    # Apply structured fields — no invented Recently / New Project
    if location:
        loc = classify_location(location)
        fields["location"] = loc["location"]
        if loc["location_preference"]:
            fields["location_preference"] = loc["location_preference"]
        if loc["remote_or_onsite"]:
            fields["remote_or_onsite"] = loc["remote_or_onsite"]
        if loc.get("timezone_requirement"):
            fields.setdefault("raw_data", {})
            fields["raw_data"] = {
                **(fields.get("raw_data") or {}),
                "fintalent_timezone_requirement": loc["timezone_requirement"],
            }
        meta["fields_extracted"].append("location")
    else:
        meta["fields_not_exposed"].append("location")

    if budget:
        parsed = parse_budget_fields(budget, title=title)
        for k, v in parsed.items():
            if v is not None and v != "":
                fields[k] = v
        if fields.get("budget_text"):
            meta["fields_extracted"].append("budget_text")
        else:
            meta["rejected_candidates"].append({"field": "budget_text", "value": budget})
    else:
        meta["fields_not_exposed"].append("budget_text")

    if duration:
        fields["duration_text"] = duration
        fields["project_length"] = duration
        meta["fields_extracted"].append("duration_text")
    else:
        meta["fields_not_exposed"].append("duration_text")

    if time_posted:
        posted = parse_posted_time(time_posted)
        fields.update({k: v for k, v in posted.items() if v is not None})
        meta["fields_extracted"].append("time_posted_text")
    else:
        meta["fields_not_exposed"].append("time_posted_text")
        fields["time_posted_text"] = None
        fields["source_posted_at"] = None
        fields["source_posted_at_is_estimated"] = False

    if status:
        fields["status"] = status
        meta["fields_extracted"].append("status")
    else:
        meta["fields_not_exposed"].append("status")
        fields["status"] = None

    if short_description:
        fields["short_description"] = short_description
        meta["fields_extracted"].append("short_description")
    else:
        meta["fields_not_exposed"].append("short_description")

    # Category — FinTalent does not expose
    if not PLATFORM_CAPABILITIES[PLATFORM]["category_exposed"]:
        fields["platform_category"] = None
        fields["platform_category_path"] = []
        fields["platform_category_raw"] = None
        fields["platform_category_source"] = None
        fields["platform_category_confidence"] = None
        fields["platform_category_extraction_status"] = "NOT_EXPOSED"
        meta["fields_not_exposed"].append("platform_category")

    # Missing = visible but not extracted
    missing = []
    for f in meta["fields_visible_on_page"]:
        key = f
        if key == "project_id" or key == "source_url" or key == "title":
            continue
        val = fields.get(key)
        if is_placeholder(val) and key not in meta["fields_not_exposed"]:
            if key in meta["fields_visible_on_page"] and key not in meta["fields_extracted"]:
                missing.append(key)
    meta["fields_missing_but_visible"] = missing

    if missing:
        status_card = "PARTIAL"
    else:
        status_card = "COMPLETE"

    return {
        "ok": True,
        "card_extraction_status": status_card,
        "fields": fields,
        "extraction_metadata": meta,
        "missing_fields": missing,
        "extraction_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Detail page readiness & extraction
# ---------------------------------------------------------------------------

DETAIL_FAILURE_CODES = (
    "LOGIN_REDIRECT", "AUTH_REDIRECT", "OVERVIEW_REDIRECT",
    "PROJECT_NOT_FOUND", "ACCESS_DENIED", "EMPTY_APP_SHELL",
    "DETAIL_TIMEOUT", "UNEXPECTED_PAGE",
)


def classify_detail_page(driver, expected_url: str | None = None) -> str | None:
    """Return a failure code if page is not a valid detail page, else None."""
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        return "UNEXPECTED_PAGE"

    if "login" in url:
        return "LOGIN_REDIRECT"
    if "auth" in url and "overview" not in url:
        return "AUTH_REDIRECT"
    if "/overview" in url and expected_url and "/overview" not in (expected_url or "").lower():
        return "OVERVIEW_REDIRECT"
    if any(x in url for x in ("/404", "not-found", "notfound")):
        return "PROJECT_NOT_FOUND"
    if any(x in url for x in ("denied", "forbidden", "unauthorized")):
        return "ACCESS_DENIED"

    try:
        body = driver.find_element("tag name", "body").text or ""
    except Exception:
        return "EMPTY_APP_SHELL"

    if len(body.strip()) < 40:
        return "EMPTY_APP_SHELL"
    low = body.lower()
    if "project not found" in low or "brief not found" in low:
        return "PROJECT_NOT_FOUND"
    if "access denied" in low:
        return "ACCESS_DENIED"
    return None


def wait_for_fintalent_project_detail_page(driver, timeout: int = 20) -> str | None:
    """Wait for detail indicators. Returns failure code or None on success."""
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import TimeoutException

    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except TimeoutException:
        return "DETAIL_TIMEOUT"

    # Allow SPA render
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: classify_detail_page(d) is None and (
                len((d.find_element("tag name", "body").text or "")) > 80
                or bool(d.find_elements("css selector", "h1, h2, [class*='title'], [class*='brief']"))
            )
        )
    except TimeoutException:
        code = classify_detail_page(driver)
        return code or "DETAIL_TIMEOUT"

    return classify_detail_page(driver)


def _click_expand_buttons(driver):
    try:
        buttons = driver.find_elements("css selector", "button, a, [role='button'], span")
        for btn in buttons:
            try:
                label = (btn.text or btn.get_attribute("aria-label") or "").strip().lower()
                if any(x in label for x in EXPAND_BUTTON_TEXTS):
                    driver.execute_script("arguments[0].click();", btn)
            except Exception:
                continue
    except Exception:
        pass


def _extract_bounded_description(driver, title: str = "") -> tuple[str, list]:
    """Extract description from bounded containers; never whole page body."""
    warnings = []
    _click_expand_buttons(driver)

    candidates = []
    for sel in DESCRIPTION_SELECTORS:
        try:
            els = driver.find_elements("css selector", sel)
            for el in els:
                txt = (el.text or "").strip()
                if not txt:
                    continue
                if len(txt) > 15000:
                    warnings.append("rejected_oversized_description_candidate")
                    continue
                if title and txt.strip().lower() == title.strip().lower():
                    continue
                low = txt.lower()
                if low.count("sign out") + low.count("logout") + low.count("cookie") > 1:
                    continue
                # Prefer dedicated Descrip boxes
                boost = 5000 if "descrip" in sel.lower() else 0
                candidates.append((len(txt) + boost, txt, sel))
        except Exception:
            continue

    if not candidates:
        try:
            headings = driver.find_elements(
                "xpath",
                "//*[self::h2 or self::h3 or self::h4]"
                "[contains(translate(normalize-space(.),'BRIEFINGDESCRIPTION','briefingdescription'),'briefing')"
                " or contains(translate(normalize-space(.),'DESCRIPTION','description'),'description')]",
            )
            for h in headings:
                try:
                    sibling = h.find_element("xpath", "./following-sibling::*[1]")
                    txt = (sibling.text or "").strip()
                    if txt and len(txt) > 40 and (not title or txt.lower() != title.lower()):
                        candidates.append((len(txt), txt, "label:Briefing"))
                except Exception:
                    continue
        except Exception:
            pass

    if not candidates:
        return "", warnings

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = None
    for length, txt, sel in candidates:
        if "main" in sel and length > 5000:
            continue
        best = (txt, sel)
        break
    if not best:
        best = (candidates[0][1], candidates[0][2])

    text = re.sub(r"\n{3,}", "\n\n", best[0]).strip()
    return text, warnings


def _extract_skill_tags(driver) -> list[str]:
    skills = []
    for sel in (
        "[class*='skill'] [class*='tag']",
        "[class*='skill'] [class*='chip']",
        "[class*='Skill'] [class*='Tag']",
        "[class*='skills'] span",
        "[class*='tag-list'] span",
        "[class*='Chip']",
        "[class*='chip']",
    ):
        try:
            els = driver.find_elements("css selector", sel)
            for el in els:
                t = (el.text or "").strip()
                if not t:
                    continue
                if len(t) > 40:
                    continue
                if t.count(" ") > 3:
                    continue
                if any(x in t.lower() for x in ("responsibilit", "requirement", "ideal profile", "scope of")):
                    continue
                skills.append(t)
            if skills:
                break
        except Exception:
            continue

    # Only accept labeled skills lists with short comma-separated tokens
    if not skills:
        try:
            body = driver.find_element("tag name", "body").text
            m = re.search(
                r"(?:Skills|Required Skills|Tools|Stack)\s*:?\s*([^\n]{2,120})",
                body,
                re.I,
            )
            if m:
                candidate = m.group(1).strip()
                if len(candidate) <= 120 and not candidate.lower().startswith("with "):
                    skills = normalize_skills(candidate)
        except Exception:
            pass
    return normalize_skills(skills)


def extract_detail_project(driver, *, card_fields: dict | None = None) -> dict:
    card_fields = card_fields or {}
    title = card_fields.get("title") or ""
    meta = _empty_meta()
    fields: dict[str, Any] = {}
    warnings: list[str] = []

    page_code = classify_detail_page(driver)
    if page_code:
        return {
            "ok": False,
            "detail_extraction_status": "TIMEOUT" if page_code == "DETAIL_TIMEOUT" else "FAILED",
            "detail_failure_code": page_code,
            "fields": {},
            "extraction_metadata": meta,
            "missing_fields": list(CORE_DETAIL_FIELDS),
            "extraction_warnings": [page_code],
        }

    # Prefer detail page H3 title over overview notification text
    try:
        for h in driver.find_elements("css selector", "h3, h1, h2"):
            t = (h.text or "").strip()
            if t and len(t) > 10 and "open for applications" not in t.lower():
                fields["title"] = clean_notification_title(t)
                title = fields["title"]
                meta["fields_extracted"].append("title")
                break
    except Exception:
        pass

    description, desc_warnings = _extract_bounded_description(driver, title=title)
    warnings.extend(desc_warnings)
    if description:
        fields["description"] = description
        meta["fields_visible_on_page"].append("description")
        meta["fields_extracted"].append("description")
    else:
        meta["fields_not_exposed"].append("description")

    body_text = ""
    try:
        body_text = driver.find_element("tag name", "body").text or ""
    except Exception:
        body_text = ""

    # Start date — require explicit label (avoid matching "Client started interviewing")
    m_start = re.search(
        r"(?:Start Date|Starts on|Start date)\s*:?\s*([^\n]{2,50})",
        body_text,
        re.I,
    )
    if m_start:
        fields["start_date_text"] = m_start.group(1).strip()
        meta["fields_visible_on_page"].append("start_date_text")
        meta["fields_extracted"].append("start_date_text")
    else:
        meta["fields_not_exposed"].append("start_date_text")

    # Client type — require "Client Type" label only
    m_client = re.search(r"Client Type\s*:?\s*([^\n]{2,60})", body_text, re.I)
    if m_client:
        fields.setdefault("raw_data", {})
        fields["raw_data"] = {
            **(fields.get("raw_data") or {}),
            "fintalent_client_type": m_client.group(1).strip(),
        }
        meta["fields_extracted"].append("fintalent_client_type")

    m_ind = re.search(r"(?:Industry|Sector)\s*:?\s*([^\n]{2,80})", body_text, re.I)
    if m_ind:
        fields["industry"] = m_ind.group(1).strip()
        meta["fields_visible_on_page"].append("industry")
        meta["fields_extracted"].append("industry")
    else:
        meta["fields_not_exposed"].append("industry")

    skills = _extract_skill_tags(driver)
    if skills:
        fields["skills"] = skills
        meta["fields_visible_on_page"].append("skills")
        meta["fields_extracted"].append("skills")
    else:
        fields["skills"] = []
        meta["fields_not_exposed"].append("skills")

    # Icon-based meta
    location = ""
    budget = ""
    duration = ""
    status = ""
    languages = ""

    txt, _ = _icon_text(driver, [
        ".fintalent-icon-clock", "[class*='fintalent-icon-clock']", "[class*='icon-clock']",
    ])
    if txt and not is_time_string(txt):
        classified = classify_duration_or_engagement(txt)
        if classified["duration_text"]:
            duration = classified["duration_text"]
        if classified["billing_type"]:
            fields["billing_type"] = classified["billing_type"]
        if classified["engagement_type"]:
            fields["engagement_type"] = classified["engagement_type"]

    # Collect non-nav fintalent-icon ancestor texts for duration/billing/location/languages
    try:
        for ic in driver.find_elements("css selector", "[class*='fintalent-icon']"):
            cls = (ic.get_attribute("class") or "").lower()
            if "nav-li" in cls or "header" in cls:
                continue
            try:
                parent = ic.find_element(
                    "xpath",
                    "./ancestor::div[contains(@class,'Flex')][1]",
                )
                ptxt = (parent.text or "").strip()
            except Exception:
                continue
            if not ptxt or len(ptxt) > 200:
                continue
            if is_time_string(ptxt):
                continue
            low = ptxt.lower()
            if low in ("home", "work", "interviews", "resources", "messages", "notifications"):
                continue
            if any(x in low for x in ("sorry", "back to projects", "archived\n", "be the first")):
                continue
            classified = classify_duration_or_engagement(ptxt)
            if classified["billing_type"] and not fields.get("billing_type"):
                fields["billing_type"] = classified["billing_type"]
                meta["fields_extracted"].append("billing_type")
            elif classified["duration_text"] and not duration:
                duration = classified["duration_text"]
            elif ("utc" in low or "gmt" in low or "europe/" in low or "remote" in low) and not location:
                location = ptxt
            elif ("fluent" in low or "language" in low) and not languages:
                languages = ptxt
            elif low in ("hourly", "daily", "fixed") and not fields.get("billing_type"):
                fields["billing_type"] = ptxt.title()
    except Exception:
        pass

    txt, _ = _icon_text(driver, [
        ".fintalent-icon-map-pin", "[class*='map-pin']", "[class*='location']", "[class*='pin']",
    ])
    if txt and not is_time_string(txt):
        location = txt

    txt, _ = _icon_text(driver, [
        ".fintalent-icon-wallet", "[class*='wallet']",
        ".fintalent-icon-swap", "[class*='swap']",
        "[class*='money']", "[class*='cash']", "[class*='dollar']",
        "[class*='currency']", "[class*='rate']",
    ])
    if txt and not is_time_string(txt) and len(txt) < 80:
        budget = txt

    status, _ = _status_from_element(driver)
    # Prefer explicit project-level phrases over candidate-personal labels (Applied)
    for cand in (
        "Project open for applications",
        "Project closed",
        "Sorry, this project been archived",
    ):
        if cand.lower() in body_text.lower():
            status = "Archived" if "archived" in cand.lower() else cand
            break
    if status and status.strip().lower() in ("applied", "apply"):
        status = "Project open for applications" if "open for applications" in body_text.lower() else status

    # Posted timestamp from detail header
    posted_fields = parse_posted_from_detail_text(body_text)
    if posted_fields.get("time_posted_text"):
        fields.update({k: v for k, v in posted_fields.items() if v is not None})
        meta["fields_extracted"].append("time_posted_text")
    else:
        meta["fields_not_exposed"].append("time_posted_text")

    if languages:
        fields["raw_data"] = {
            **(fields.get("raw_data") or {}),
            "fintalent_languages": languages,
        }
        lang_skills = normalize_skills(re.split(r",|/", languages))
        if lang_skills:
            fields["expertise"] = lang_skills
            meta["fields_extracted"].append("expertise")

    if location:
        loc = classify_location(location)
        fields["location"] = loc["location"]
        if loc["location_preference"]:
            fields["location_preference"] = loc["location_preference"]
        if loc["remote_or_onsite"]:
            fields["remote_or_onsite"] = loc["remote_or_onsite"]
        if loc.get("timezone_requirement"):
            fields["raw_data"] = {
                **(fields.get("raw_data") or {}),
                "fintalent_timezone_requirement": loc["timezone_requirement"],
            }
        meta["fields_visible_on_page"].append("location")
        meta["fields_extracted"].append("location")
    else:
        meta["fields_not_exposed"].append("location")

    if budget:
        parsed = parse_budget_fields(budget, title=title, description=description)
        for k, v in parsed.items():
            if v is not None and v != "":
                fields[k] = v
        if fields.get("budget_text"):
            meta["fields_visible_on_page"].append("budget_text")
            meta["fields_extracted"].append("budget_text")
    else:
        meta["fields_not_exposed"].append("budget_text")

    if duration:
        fields["duration_text"] = duration
        fields["project_length"] = duration
        meta["fields_visible_on_page"].append("duration_text")
        meta["fields_extracted"].append("duration_text")
    else:
        meta["fields_not_exposed"].append("duration_text")

    if status:
        fields["status"] = status
        meta["fields_visible_on_page"].append("status")
        meta["fields_extracted"].append("status")
    else:
        meta["fields_not_exposed"].append("status")

    # Category not exposed
    if not PLATFORM_CAPABILITIES[PLATFORM]["category_exposed"]:
        fields["platform_category"] = None
        fields["platform_category_path"] = []
        fields["platform_category_raw"] = None
        fields["platform_category_source"] = None
        fields["platform_category_confidence"] = None
        fields["platform_category_extraction_status"] = "NOT_EXPOSED"
        if "platform_category" not in meta["fields_not_exposed"]:
            meta["fields_not_exposed"].append("platform_category")

    # Optional not-exposed markers (do not cause PARTIAL)
    for optional in (
        "expertise", "deliverables", "weekly_commitment", "application_deadline",
        "contracting_process", "level_of_support", "workstream", "estimated_hours",
        "project_type", "engagement_type",
    ):
        if optional not in fields and optional not in meta["fields_extracted"]:
            if optional not in meta["fields_not_exposed"]:
                meta["fields_not_exposed"].append(optional)

    # Missing = visible core fields not extracted
    missing = []
    for f in CORE_DETAIL_FIELDS:
        if f in meta["fields_visible_on_page"] and f not in meta["fields_extracted"]:
            missing.append(f)
    meta["fields_missing_but_visible"] = missing

    meaningful = any(fields.get(k) for k in ("description", "budget_text", "location", "skills", "status", "duration_text"))
    if not meaningful and not description:
        detail_status = "FAILED"
        failure_code = "EMPTY_DETAIL"
    elif missing:
        detail_status = "PARTIAL"
        failure_code = None
    else:
        detail_status = "COMPLETE"
        failure_code = None

    return {
        "ok": detail_status in ("COMPLETE", "PARTIAL"),
        "detail_extraction_status": detail_status,
        "detail_failure_code": failure_code,
        "fields": fields,
        "extraction_metadata": meta,
        "missing_fields": missing,
        "extraction_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Safe merge
# ---------------------------------------------------------------------------

def _is_useful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and is_placeholder(value):
        return False
    if isinstance(value, (list, dict)) and len(value) == 0:
        return False
    return True


def merge_project_data(card_data: dict, detail_data: dict) -> dict:
    """Valid detail may replace weaker card data; empty detail cannot overwrite useful card values."""
    merged = dict(card_data or {})
    detail = dict(detail_data or {})
    rejected = []

    for key, dval in detail.items():
        if key in ("extraction_metadata", "missing_fields", "extraction_warnings"):
            continue
        cval = merged.get(key)
        if key == "raw_data" and isinstance(dval, dict):
            merged["raw_data"] = {**(cval if isinstance(cval, dict) else {}), **dval}
            continue
        if not _is_useful(dval):
            if _is_useful(cval):
                rejected.append({"field": key, "reason": "empty_detail_preserved_card", "detail_value": dval})
            continue
        if key in ("budget_text", "budget_min", "budget_max", "hourly_rate", "daily_rate"):
            # Invalid budget should not overwrite structured budget
            if key == "budget_text" and isinstance(dval, str) and len(dval) > 80:
                rejected.append({"field": key, "reason": "invalid_budget_candidate"})
                continue
        merged[key] = dval

    # Merge metadata
    c_meta = (card_data or {}).get("extraction_metadata") or {}
    d_meta = (detail_data or {}).get("extraction_metadata") or detail.get("extraction_metadata") or {}
    if not isinstance(d_meta, dict):
        d_meta = {}

    def _uniq(seq):
        seen = set()
        out = []
        for x in seq or []:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    merged_meta = {
        "fields_visible_on_page": _uniq(
            list(c_meta.get("fields_visible_on_page") or []) + list(d_meta.get("fields_visible_on_page") or [])
        ),
        "fields_extracted": _uniq(
            list(c_meta.get("fields_extracted") or []) + list(d_meta.get("fields_extracted") or [])
        ),
        "fields_missing_but_visible": _uniq(
            list(d_meta.get("fields_missing_but_visible") or [])
        ),
        "fields_not_exposed": _uniq(
            list(c_meta.get("fields_not_exposed") or []) + list(d_meta.get("fields_not_exposed") or [])
        ),
        "platform_capabilities": PLATFORM_CAPABILITIES.get(PLATFORM, {}),
        "rejected_candidates": list(c_meta.get("rejected_candidates") or [])
        + list(d_meta.get("rejected_candidates") or [])
        + rejected,
        "project_id_source": c_meta.get("project_id_source") or merged.get("project_id_source"),
        "project_id_confidence": c_meta.get("project_id_confidence"),
    }
    # Remove extracted items from not_exposed
    merged_meta["fields_not_exposed"] = [
        f for f in merged_meta["fields_not_exposed"] if f not in merged_meta["fields_extracted"]
    ]
    merged["extraction_metadata"] = merged_meta
    return merged


def build_project_row(
    merged: dict,
    *,
    scraper_run_id: str | None,
    card_status: str,
    detail_status: str,
    email_eligible: bool,
    email_status: str,
    email_sent: bool = False,
    email_not_sent_reason: str | None = None,
    detail_failure_code: str | None = None,
    missing_fields: list | None = None,
    extraction_warnings: list | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    iso = now.isoformat()
    raw = dict(merged.get("raw_data") or {})
    meta = dict(merged.get("extraction_metadata") or {})

    row = {
        "platform": PLATFORM,
        "project_id": merged["project_id"],
        "source_url": merged["source_url"],
        "title": merged.get("title"),
        "short_description": merged.get("short_description"),
        "description": merged.get("description"),
        "status": merged.get("status"),
        "platform_category": merged.get("platform_category"),
        "platform_category_path": merged.get("platform_category_path") or [],
        "platform_category_raw": merged.get("platform_category_raw"),
        "platform_category_source": merged.get("platform_category_source"),
        "platform_category_confidence": merged.get("platform_category_confidence"),
        "platform_category_extraction_status": merged.get(
            "platform_category_extraction_status", "NOT_EXPOSED"
        ),
        "location": merged.get("location"),
        "location_preference": merged.get("location_preference"),
        "budget_text": merged.get("budget_text"),
        "budget_min": merged.get("budget_min"),
        "budget_max": merged.get("budget_max"),
        "budget_currency": merged.get("budget_currency"),
        "billing_type": merged.get("billing_type"),
        "hourly_rate": merged.get("hourly_rate"),
        "daily_rate": merged.get("daily_rate"),
        "rate_currency": merged.get("rate_currency"),
        "budget_source": merged.get("budget_source"),
        "budget_confidence": merged.get("budget_confidence"),
        "duration_text": merged.get("duration_text"),
        "project_length": merged.get("project_length") or merged.get("duration_text"),
        "start_date_text": merged.get("start_date_text"),
        "source_start_date": merged.get("source_start_date"),
        "level_of_support": merged.get("level_of_support"),
        "industry": merged.get("industry"),
        "contracting_process": merged.get("contracting_process"),
        "skills": merged.get("skills") if merged.get("skills") is not None else [],
        "expertise": merged.get("expertise") if merged.get("expertise") is not None else [],
        "deliverables": merged.get("deliverables") if merged.get("deliverables") is not None else [],
        "engagement_type": merged.get("engagement_type"),
        "project_type": merged.get("project_type"),
        "workstream": merged.get("workstream"),
        "estimated_hours": merged.get("estimated_hours"),
        "weekly_commitment": merged.get("weekly_commitment"),
        "remote_or_onsite": merged.get("remote_or_onsite"),
        "country_or_region": merged.get("country_or_region"),
        "application_deadline": merged.get("application_deadline"),
        "time_posted_text": merged.get("time_posted_text"),
        "source_posted_at": merged.get("source_posted_at"),
        "source_posted_at_is_estimated": bool(merged.get("source_posted_at_is_estimated", False)),
        "scraped_at": iso,
        "first_detected_at": iso,
        "last_seen_at": iso,
        "card_extraction_status": card_status,
        "detail_extraction_status": detail_status,
        "detail_attempt_count": 1 if detail_status != "NOT_ATTEMPTED" else 0,
        "detail_last_attempt_at": iso if detail_status != "NOT_ATTEMPTED" else None,
        "detail_completed_at": iso if detail_status == "COMPLETE" else None,
        "detail_failure_code": detail_failure_code,
        "detail_last_error": None,
        "missing_fields": missing_fields or [],
        "extraction_warnings": extraction_warnings or [],
        "extraction_metadata": meta,
        "raw_data": raw,
        "email_eligible": email_eligible,
        "email_status": email_status,
        "email_sent": email_sent,
        "email_not_sent_reason": email_not_sent_reason,
        "email_failure_code": None,
        "email_last_error": None,
        "email_attempt_count": 0,
        "email_last_attempt_at": None,
        "email_next_retry_at": None,
        "email_sent_at": None,
        "email_message_id": None,
        "scraper_run_id": scraper_run_id,
    }
    return row


def should_auto_enrich(row: dict, *, max_attempts: int = 3, cooldown_minutes: int = 360, now: datetime | None = None) -> bool:
    """COMPLETE rows must not be enriched merely because optional fields are null."""
    status = row.get("detail_extraction_status")
    if status == "COMPLETE":
        return False
    if status not in ("NOT_ATTEMPTED", "PARTIAL", "FAILED", "TIMEOUT"):
        return False
    attempts = int(row.get("detail_attempt_count") or 0)
    if attempts >= max_attempts:
        return False
    now = now or datetime.now(timezone.utc)
    last = row.get("detail_last_attempt_at")
    if last:
        if isinstance(last, str):
            last_s = last.replace("Z", "+00:00")
            try:
                last_dt = datetime.fromisoformat(last_s)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if (now - last_dt) < timedelta(minutes=cooldown_minutes):
                    return False
            except ValueError:
                pass
    return True
