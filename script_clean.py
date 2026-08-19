"""FinTalent monitor core: auth, scanning, email lifecycle, cold start, enrichment."""

from __future__ import annotations

import json
import os
import smtplib
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid

from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import database as db
from extraction import (
    BASE_URL,
    CARD_SELECTORS,
    PLATFORM,
    build_project_row,
    extract_card_project,
    extract_detail_project,
    merge_project_data,
    should_auto_enrich,
    wait_for_fintalent_project_detail_page,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

PKT = timezone(timedelta(hours=5))


class Config:
    PLATFORM_NAME = PLATFORM
    FINTALENT_EMAIL = os.getenv("FINTALENT_EMAIL")
    FINTALENT_PASSWORD = os.getenv("FINTALENT_PASSWORD")
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()]
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 60))
    OCCURRENCE_WINDOW_DAYS = int(
        os.getenv("OCCURRENCE_WINDOW_DAYS") or os.getenv("REPOST_MIN_DAYS") or "7"
    )
    HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
    COOKIES_FILE = "fintalent_cookies.json"
    BASE_URL = BASE_URL
    TARGET_URL = "https://talent.fintalent.io/overview"
    DETAIL_FETCH_DELAY_SECONDS = float(os.getenv("DETAIL_FETCH_DELAY_SECONDS", "2"))
    DETAIL_FETCH_MAX_ATTEMPTS = int(os.getenv("DETAIL_FETCH_MAX_ATTEMPTS", "2"))
    EMAIL_MAX_RETRIES = int(os.getenv("EMAIL_MAX_RETRIES", "3"))
    EMAIL_RETRY_BASE_MINUTES = int(os.getenv("EMAIL_RETRY_BASE_MINUTES", "15"))
    DETAIL_AUTO_ENRICHMENT_ENABLED = os.getenv("DETAIL_AUTO_ENRICHMENT_ENABLED", "true").lower() == "true"
    DETAIL_MAX_AUTOMATIC_ATTEMPTS = int(os.getenv("DETAIL_MAX_AUTOMATIC_ATTEMPTS", "3"))
    DETAIL_RETRY_COOLDOWN_MINUTES = int(os.getenv("DETAIL_RETRY_COOLDOWN_MINUTES", "360"))
    FINTALENT_WORKER_LOCK_TTL_SECONDS = int(os.getenv("FINTALENT_WORKER_LOCK_TTL_SECONDS", "180"))
    ERROR_EMAIL_COOLDOWN_MINUTES = int(os.getenv("ERROR_EMAIL_COOLDOWN_MINUTES", "360"))
    ERROR_RECIPIENT_EMAIL = os.getenv("ERROR_RECIPIENT_EMAIL", "").strip()
    SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN = os.getenv("SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN", "false").lower() == "true"


def validate_config(*, require_smtp: bool = True, require_fintalent: bool = True) -> None:
    missing = []
    if not os.getenv("SUPABASE_URL"):
        missing.append("SUPABASE_URL")
    if not (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
        missing.append("SUPABASE_SECRET_KEY")
    if require_fintalent:
        if not Config.FINTALENT_EMAIL:
            missing.append("FINTALENT_EMAIL")
        if not Config.FINTALENT_PASSWORD:
            missing.append("FINTALENT_PASSWORD")
    if require_smtp:
        for k in ("SENDER_EMAIL", "SENDER_PASSWORD"):
            if not getattr(Config, k):
                missing.append(k)
        if not Config.RECIPIENT_EMAILS:
            missing.append("RECIPIENT_EMAILS")
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")


def debug_print(msg, enabled=False):
    if enabled:
        print(msg)


def dump_page_structure(driver):
    print("\n" + "=" * 60)
    print("DIAGNOSTICS: FINTALENT PAGE STRUCTURE DUMP")
    print("=" * 60)
    print(f"  URL: {driver.current_url}")
    card_candidates = [
        "article", ".card", "[class*='card']", "[class*='project']",
        "[class*='brief']", "[class*='opportunity']", "[class*='job']",
        "li[class]", "div[class*='item']",
    ]
    print("\nCard Containers:")
    for sel in card_candidates:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                sample = elems[0]
                cls = sample.get_attribute("class") or ""
                txt = (sample.text or "")[:80].replace("\n", " ")
                print(f"  [{len(elems)}] {sel}  → class='{cls[:50]}' text='{txt}'")
        except Exception:
            pass
    print("=" * 60 + "\n")


# ============================
# SESSION (Supabase + local)
# ============================

def save_cookies(driver):
    try:
        cookies = driver.get_cookies()
        local_storage = driver.execute_script("return window.localStorage;") or {}
        try:
            db.save_scraper_session(cookies, local_storage)
            print("  Saved session to Supabase scraper_sessions")
        except Exception as e:
            print(f"  Warning: could not save session to Supabase: {e}")
        try:
            with open(Config.COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "cookies": cookies,
                    "local_storage": local_storage,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }, f)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"  Warning: could not save cookies: {e}")
        return False


def load_cookies(driver):
    session_data = None
    try:
        session_data = db.load_scraper_session()
        if session_data:
            print("  Loaded cookies from Supabase")
    except Exception as e:
        print(f"  Warning: could not load cookies from Supabase: {e}")

    if not session_data and os.path.exists(Config.COOKIES_FILE):
        try:
            with open(Config.COOKIES_FILE, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            print("  Loaded cookies from local file")
        except Exception:
            pass

    if not session_data or not session_data.get("cookies"):
        return False

    try:
        driver.get(Config.BASE_URL)
        time.sleep(2)
        driver.delete_all_cookies()
        for cookie in session_data["cookies"]:
            domain = cookie.get("domain") or ""
            if "fintalent.io" in domain:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
        if session_data.get("local_storage"):
            for key, val in session_data["local_storage"].items():
                try:
                    driver.execute_script(
                        "window.localStorage.setItem(arguments[0], arguments[1]);",
                        key, val,
                    )
                except Exception:
                    pass
        return True
    except Exception as e:
        print(f"  Warning: error applying cookies: {e}")
        return False


_last_lock_heartbeat = 0.0


def _lock_heartbeat_interval() -> float:
    return max(Config.FINTALENT_WORKER_LOCK_TTL_SECONDS / 3.0, 15.0)


def heartbeat_worker_lock(*, force: bool = False) -> bool:
    """Renew the lease at most once per TTL/3. Renewal doubles as ownership proof."""
    global _last_lock_heartbeat
    elapsed = time.monotonic() - _last_lock_heartbeat
    if not force and elapsed < _lock_heartbeat_interval():
        return True
    try:
        result = db.renew_worker_lock(Config.FINTALENT_WORKER_LOCK_TTL_SECONDS)
    except Exception as e:
        print(f"  Lock renew error: {e}")
        return False
    if result.get("renewed"):
        _last_lock_heartbeat = time.monotonic()
        return True
    return False


def ensure_worker_lock() -> bool:
    """Renew, or re-acquire an expired lease when no other worker has taken it."""
    global _last_lock_heartbeat
    if heartbeat_worker_lock(force=True):
        return True
    try:
        lock = db.acquire_worker_lock(Config.FINTALENT_WORKER_LOCK_TTL_SECONDS)
    except Exception as e:
        print(f"  Lock re-acquire error: {e}")
        return False
    if lock.get("acquired"):
        _last_lock_heartbeat = time.monotonic()
        print(f"Worker lock re-acquired as {lock.get('self_owner')}")
        return True
    print(f"Worker lock is held by another worker ({lock.get('owner')})")
    return False


def sleep_between_cycles(seconds: float, *, holding_lock: bool) -> None:
    """Sleep in chunks so an idle interval longer than the lock TTL cannot drop the lease."""
    deadline = time.monotonic() + seconds
    chunk = max(min(_lock_heartbeat_interval(), 60.0), 5.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(chunk, remaining))
        if holding_lock:
            heartbeat_worker_lock(force=True)


def clear_session_safe():
    """Clear cookies preserving worker lock; never write saved_at=null."""
    db.delete_scraper_session()
    if os.path.exists(Config.COOKIES_FILE):
        try:
            with open(Config.COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "cookies": [],
                    "local_storage": {},
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                }, f)
        except Exception:
            pass


def perform_login(driver):
    try:
        print(f"  Navigating to FinTalent target URL: {Config.TARGET_URL}")
        driver.get(Config.TARGET_URL)
        time.sleep(4)

        if (
            "login" not in driver.current_url.lower()
            and "auth" not in driver.current_url.lower()
            and "overview" in driver.current_url.lower()
        ):
            print("  Already authenticated.")
            return True

        for consent_sel in [
            "button[id*='cookie']",
            "button[class*='cookie']",
            "button[aria-label*='Accept']",
            "button[title*='Accept All']",
        ]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, consent_sel)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1.5)
                break
            except Exception:
                pass

        email_field = None
        for sel in ["input[type='email']", "input[name='email']", "input[id*='email']", "input[name='username']"]:
            try:
                email_field = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except Exception:
                continue

        if not email_field:
            print("Could not find email field.")
            dump_page_structure(driver)
            return False

        email_field.click()
        email_field.clear()
        email_field.send_keys(Config.FINTALENT_EMAIL)
        time.sleep(0.5)

        password_field = None
        for sel in ["input[type='password']", "input[name='password']", "input[id*='password']"]:
            try:
                password_field = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except Exception:
                continue

        if not password_field:
            print("Could not find password field.")
            return False

        password_field.click()
        password_field.clear()
        password_field.send_keys(Config.FINTALENT_PASSWORD)
        time.sleep(0.5)
        password_field.send_keys(Keys.ENTER)
        print("  Submitted login form via Enter")
        time.sleep(5)

        if "login" in driver.current_url.lower() or "auth" in driver.current_url.lower():
            for sel in [
                "button[type='submit']",
                "input[type='submit']",
                "//button[contains(text(), 'Login') or contains(text(), 'Sign')]",
            ]:
                try:
                    if sel.startswith("//"):
                        btn = driver.find_element(By.XPATH, sel)
                    else:
                        btn = driver.find_element(By.CSS_SELECTOR, sel)
                    driver.execute_script("arguments[0].click();", btn)
                    print("  Clicked login button")
                    time.sleep(5)
                    break
                except Exception:
                    continue

        for _ in range(15):
            time.sleep(1)
            if (
                "login" not in driver.current_url.lower()
                and "auth" not in driver.current_url.lower()
                and "overview" in driver.current_url.lower()
            ):
                break
        else:
            print(f"Login redirect failed. URL: {driver.current_url}")
            return False

        save_cookies(driver)
        print(f"Login successful -> {driver.current_url}")
        return True
    except Exception as e:
        print(f"Login error: {e}")
        return False


def setup_session(driver):
    if load_cookies(driver):
        driver.get(Config.TARGET_URL)
        time.sleep(5)
        if (
            "login" not in driver.current_url.lower()
            and "auth" not in driver.current_url.lower()
            and "overview" in driver.current_url.lower()
        ):
            print("Session established via cached cookies")
            return True
        print("  Cookies expired or invalid. Authenticating...")
    return perform_login(driver)


# ============================
# DRIVER
# ============================

def _find_binary(env_var, candidates):
    import shutil
    val = os.getenv(env_var, "")
    if val and os.path.exists(val):
        return val
    for path in candidates:
        if os.path.exists(path):
            return path
    found = shutil.which(candidates[-1].split("/")[-1])
    return found or ""


def initialize_driver():
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin

    from selenium.webdriver.chrome.service import Service

    system_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])
    if system_path:
        service = Service(system_path)
    else:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.os_manager import ChromeType
            is_chromium = "chromium" in (chrome_bin or "").lower()
            mgr = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM if is_chromium else ChromeType.GOOGLE)
            service = Service(mgr.install())
        except Exception:
            service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    return driver


# ============================
# SCANNING
# ============================

def find_project_cards(driver):
    for sel in CARD_SELECTORS:
        try:
            if sel.startswith("//"):
                cards = driver.find_elements(By.XPATH, sel)
            else:
                cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                return cards
        except Exception:
            pass
    try:
        links = driver.find_elements(
            By.XPATH,
            "//a[contains(@href, '/brief/') or contains(@href, '/project/')]",
        )
        cards = []
        seen_parents = set()
        for link in links:
            try:
                parent = link.find_element(
                    By.XPATH,
                    "./ancestor::div[contains(@class, 'card') or contains(@class, 'item') "
                    "or @style or contains(@class, 'border')][1]",
                )
                if parent.id not in seen_parents:
                    seen_parents.add(parent.id)
                    cards.append(parent)
            except Exception:
                pass
        return cards
    except Exception:
        return []


def scan_for_card_extractions(driver, *, debug=False):
    """Return list of extract_card_project results (ok and failed)."""
    try:
        if Config.TARGET_URL not in driver.current_url:
            driver.get(Config.TARGET_URL)
            time.sleep(4)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(3)
        cards = find_project_cards(driver)
        if not cards:
            print("No project cards found with default selectors.")
            dump_page_structure(driver)
            return []
        results = []
        for card in cards:
            results.append(extract_card_project(card))
        ok = sum(1 for r in results if r.get("ok"))
        print(f"Extracted {ok} valid projects from {len(cards)} cards")
        return results
    except TimeoutException:
        print("Timeout waiting for FinTalent overview page to load")
        return []
    except Exception as e:
        print(f"Error scanning FinTalent: {e}")
        return []


def fetch_project_details(driver, url, *, card_fields=None, max_attempts=None):
    max_attempts = max_attempts or Config.DETAIL_FETCH_MAX_ATTEMPTS
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            driver.get(url)
            time.sleep(Config.DETAIL_FETCH_DELAY_SECONDS)
            failure = wait_for_fintalent_project_detail_page(driver)
            if failure:
                last = {
                    "ok": False,
                    "detail_extraction_status": "TIMEOUT" if failure == "DETAIL_TIMEOUT" else "FAILED",
                    "detail_failure_code": failure,
                    "fields": {},
                    "extraction_metadata": {},
                    "missing_fields": [],
                    "extraction_warnings": [failure],
                }
                if failure in ("LOGIN_REDIRECT", "AUTH_REDIRECT"):
                    break
                continue
            last = extract_detail_project(driver, card_fields=card_fields or {})
            if last.get("ok") or last.get("detail_extraction_status") == "PARTIAL":
                break
        except Exception as e:
            last = {
                "ok": False,
                "detail_extraction_status": "FAILED",
                "detail_failure_code": "DETAIL_EXCEPTION",
                "fields": {},
                "extraction_metadata": {},
                "missing_fields": [],
                "extraction_warnings": [str(e)],
            }
    try:
        driver.get(Config.TARGET_URL)
        time.sleep(3)
    except Exception:
        pass
    return last or {
        "ok": False,
        "detail_extraction_status": "FAILED",
        "detail_failure_code": "UNKNOWN",
        "fields": {},
        "extraction_metadata": {},
        "missing_fields": [],
        "extraction_warnings": [],
    }


# ============================
# EMAIL
# ============================

def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _section_header(icon, title, color):
    return (
        f'<tr><td colspan="2" style="padding:14px 16px 6px;background:{color};'
        f'color:#fff;font-size:12px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:1px;">'
        f"{icon}&nbsp; {title}</td></tr>"
    )


def _row(label, value, alt=False, bold_value=False):
    if not value:
        return ""
    bg = "background:#f8f9fa;" if alt else "background:#fff;"
    bold = "font-weight:bold;" if bold_value else ""
    return (
        f"<tr>"
        f"<td style='padding:9px 16px;color:#555;width:200px;{bg}border-bottom:1px solid #eee;'>"
        f"<strong>{_esc(label)}</strong></td>"
        f"<td style='padding:9px 16px;{bg}{bold}border-bottom:1px solid #eee;'>{_esc(str(value))}</td>"
        f"</tr>"
    )


def create_email_html(project):
    title = project.get("title", "Untitled Project")
    url = project.get("source_url") or project.get("url") or Config.TARGET_URL
    detected_at = project.get("detected_at") or datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
    project_id = project.get("project_id") or project.get("id", "")
    description = project.get("description", "")
    location = project.get("location", "") or "Remote / Not specified"
    budget = project.get("budget_text") or project.get("budget", "") or "Not provided"
    duration = project.get("duration_text") or project.get("duration", "")
    start_date = project.get("start_date_text") or project.get("start_date", "")
    skills = project.get("skills", []) or []

    hdr_grad = "linear-gradient(135deg,#0f172a,#334155)"
    desc_section = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", " ")
        paras = [f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||") if p.strip()]
        desc_section = (
            _section_header("📋", "Description", "#1e293b")
            + f"<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            f"font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{''.join(paras)}</td></tr>"
        )
    skills_display = ", ".join(skills) if isinstance(skills, list) else str(skills)
    detail_section = _section_header("📦", "Project Details", "#334155") + (
        _row("Location", location, alt=False)
        + _row("Duration", duration, alt=True)
        + _row("Start Date", start_date, alt=False)
        + _row("Skills/Tools", skills_display, alt=True)
    )
    budget_section = _section_header("💰", "Compensation", "#0f766e") + _row("Rate / Budget", budget, bold_value=True)
    meta_rows = _row("Detected at", detected_at, alt=True) + _row("Project ID", project_id, alt=False)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">
    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">FinTalent Monitor Alert</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">New FinTalent Project</h2>
    </div>
    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
    </div>
    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {desc_section}{detail_section}{budget_section}
        {_section_header("🕒", "Detection Info", "#6b7280")}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:#0f172a;color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Project on FinTalent →
        </a>
      </div>
    </div>
    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      FinTalent Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""


def classify_email_failure(exc: Exception) -> str:
    msg = str(exc).lower()
    if "auth" in msg or "login" in msg or "username" in msg or "password" in msg:
        return "SMTP_AUTH_FAILED"
    if "timed out" in msg or "timeout" in msg:
        return "SMTP_TIMEOUT"
    if "connection" in msg or "refused" in msg:
        return "SMTP_CONNECTION_FAILED"
    return "SMTP_SEND_FAILED"


_last_error_email_at: float = 0.0


def send_error_email(subject: str, body: str) -> None:
    """Send an error notification to ERROR_RECIPIENT_EMAIL. Respects cooldown."""
    global _last_error_email_at
    recipient = Config.ERROR_RECIPIENT_EMAIL
    if not recipient:
        return
    elapsed_min = (time.monotonic() - _last_error_email_at) / 60.0
    if _last_error_email_at > 0 and elapsed_min < Config.ERROR_EMAIL_COOLDOWN_MINUTES:
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[FinTalent Monitor ERROR] {subject}"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = recipient
        html = f"<html><body><h3>{_esc(subject)}</h3><pre>{_esc(body[:5000])}</pre></body></html>"
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.sendmail(Config.SENDER_EMAIL, [recipient], msg.as_string())
        _last_error_email_at = time.monotonic()
        print(f"  Error email sent to {recipient}: {subject[:60]}")
    except Exception as e:
        print(f"  Failed to send error email: {e}")


def send_notification(project) -> dict:
    """Return structured email result (never bare True/False)."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"FinTalent: {project.get('title', 'New Project')}"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.RECIPIENT_EMAILS)
        message_id = make_msgid(domain=(Config.SENDER_EMAIL or "fintalent-monitor").split("@")[-1])
        msg["Message-ID"] = message_id
        msg.attach(MIMEText(create_email_html(project), "html"))

        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)

        print(f"Email sent: {(project.get('title') or 'Unknown')[:50]}...")
        return {"success": True, "message_id": message_id, "failure_code": None, "error": None}
    except Exception as e:
        print(f"Email notification failed: {e}")
        return {
            "success": False,
            "message_id": None,
            "failure_code": classify_email_failure(e),
            "error": str(e)[:500],
        }


def send_project_email(project_uuid: str, project_row: dict, *, attempt_number: int | None = None) -> dict:
    """Insert attempt → send → update same project UUID. Requires lock."""
    if not db.verify_lock_held():
        return {"success": False, "failure_code": "LOCK_LOST", "error": "Worker lock not held", "message_id": None}

    attempt_number = attempt_number or (int(project_row.get("email_attempt_count") or 0) + 1)
    attempt_id = db.create_email_attempt(
        project_uuid,
        attempt_number=attempt_number,
        recipients=Config.RECIPIENT_EMAILS,
    )
    db.update_project_email_status(project_uuid, {
        "email_status": "SENDING",
        "email_last_attempt_at": db._iso(),
    })

    result = send_notification(project_row)
    now = db._iso()

    if result["success"]:
        db.complete_email_attempt_success(attempt_id, message_id=result.get("message_id"))
        db.update_project_email_status(project_uuid, {
            "email_status": "SENT",
            "email_sent": True,
            "email_eligible": True,
            "email_not_sent_reason": None,
            "email_failure_code": None,
            "email_last_error": None,
            "email_attempt_count": attempt_number,
            "email_last_attempt_at": now,
            "email_next_retry_at": None,
            "email_sent_at": now,
            "email_message_id": result.get("message_id"),
        })
    else:
        max_retries = Config.EMAIL_MAX_RETRIES
        if attempt_number >= max_retries:
            email_status = "FAILED"
            next_retry = None
        else:
            email_status = "RETRY_PENDING"
            next_retry = db.compute_email_next_retry(attempt_number, Config.EMAIL_RETRY_BASE_MINUTES)
        db.complete_email_attempt_failure(
            attempt_id,
            failure_code=result.get("failure_code") or "SMTP_SEND_FAILED",
            failure_reason=result.get("error") or "unknown",
        )
        db.update_project_email_status(project_uuid, {
            "email_status": email_status,
            "email_sent": False,
            "email_not_sent_reason": "EMAIL_SEND_FAILED",
            "email_failure_code": result.get("failure_code"),
            "email_last_error": result.get("error"),
            "email_attempt_count": attempt_number,
            "email_last_attempt_at": now,
            "email_next_retry_at": next_retry,
        })
    return result


def within_notification_age(merged: dict) -> bool:
    """MAX_AGE_MINUTES is email policy only — never blocks storage."""
    posted = merged.get("source_posted_at")
    if not posted:
        return True  # unknown age → allow email eligibility decision by caller
    try:
        if isinstance(posted, str):
            posted = posted.replace("Z", "+00:00")
            dt = datetime.fromisoformat(posted)
        else:
            dt = posted
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        return age_min <= Config.MAX_AGE_MINUTES
    except Exception:
        return True


# ============================
# COLD START / SCAN CYCLE
# ============================

def process_cold_start(driver, run_id: str, *, dry_run: bool = False, debug_extraction: bool = False) -> dict:
    counts = {
        "cards_found": 0, "cards_parsed": 0, "cards_failed": 0,
        "details_attempted": 0, "details_completed": 0, "details_failed": 0,
        "projects_inserted": 0, "projects_skipped": 0,
        "emails_sent": 0, "emails_failed": 0, "emails_suppressed": 0,
        "persistence_failed": False,
    }
    print("Cold start: seeding FinTalent projects with detail enrichment...")
    card_results = scan_for_card_extractions(driver, debug=debug_extraction)
    counts["cards_found"] = len(card_results)

    for result in card_results:
        if not dry_run:
            heartbeat_worker_lock()
        if not result.get("ok"):
            counts["cards_failed"] += 1
            continue
        counts["cards_parsed"] += 1
        fields = result["fields"]
        print(f"  Seeding: {fields.get('title', '')[:60]}")
        counts["details_attempted"] += 1
        detail = fetch_project_details(driver, fields["source_url"], card_fields=fields)
        if detail.get("detail_extraction_status") == "COMPLETE":
            counts["details_completed"] += 1
        elif detail.get("detail_extraction_status") in ("FAILED", "TIMEOUT"):
            counts["details_failed"] += 1
        else:
            counts["details_completed"] += 1  # PARTIAL still counts as attempted completion path

        detail_fields = detail.get("fields") or {}
        # Attach metadata onto detail for merge
        detail_fields = {
            **detail_fields,
            "extraction_metadata": detail.get("extraction_metadata") or {},
        }
        card_for_merge = {
            **fields,
            "extraction_metadata": result.get("extraction_metadata") or {},
        }
        merged = merge_project_data(card_for_merge, detail_fields)
        if debug_extraction:
            print(json.dumps({
                "title": merged.get("title"),
                "project_id": merged.get("project_id"),
                "card_status": result.get("card_extraction_status"),
                "detail_status": detail.get("detail_extraction_status"),
                "meta": merged.get("extraction_metadata"),
            }, default=str, indent=2)[:2000])

        suppress = Config.SUPPRESS_PROJECT_EMAILS_ON_FIRST_SCAN
        row = build_project_row(
            merged,
            scraper_run_id=run_id,
            card_status=result.get("card_extraction_status") or "COMPLETE",
            detail_status=detail.get("detail_extraction_status") or "FAILED",
            email_eligible=not suppress,
            email_status="SUPPRESSED" if suppress else "PENDING",
            email_sent=False,
            email_not_sent_reason="COLD_START_SUPPRESSED" if suppress else None,
            detail_failure_code=detail.get("detail_failure_code"),
            missing_fields=list(set((result.get("missing_fields") or []) + (detail.get("missing_fields") or []))),
            extraction_warnings=list(set((result.get("extraction_warnings") or []) + (detail.get("extraction_warnings") or []))),
        )

        if dry_run:
            print(f"  [dry-run] would insert seed {row['project_id']}")
            continue
        try:
            project_uuid = db.insert_project_occurrence(row)
            counts["projects_inserted"] += 1
        except Exception as e:
            print(f"  Persistence failed for {row.get('project_id')}: {e}")
            send_error_email(f"Cold start insert failed: {row.get('project_id')}", str(e))
            counts["persistence_failed"] = True
            continue

        if suppress:
            counts["emails_suppressed"] += 1
        else:
            detected_at = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
            email_result = send_project_email(
                project_uuid,
                {**row, "id": project_uuid, "detected_at": detected_at},
            )
            if email_result.get("success"):
                counts["emails_sent"] += 1
            else:
                counts["emails_failed"] += 1

    if counts["persistence_failed"] and counts["projects_inserted"] == 0 and not dry_run:
        raise db.DatabaseError("Cold start persistence failed for all projects", code="COLD_START_PERSIST_FAILED")
    return counts


def process_scan_cycle(
    driver,
    run_id: str,
    *,
    dry_run: bool = False,
    debug_extraction: bool = False,
    send_emails: bool = True,
) -> dict:
    counts = {
        "cards_found": 0, "cards_parsed": 0, "cards_failed": 0,
        "details_attempted": 0, "details_completed": 0, "details_failed": 0,
        "projects_inserted": 0, "projects_skipped": 0,
        "emails_sent": 0, "emails_failed": 0, "emails_suppressed": 0,
        "partial_failures": False,
        "db_failed": False,
    }

    card_results = scan_for_card_extractions(driver, debug=debug_extraction)
    counts["cards_found"] = len(card_results)

    for result in card_results:
        if not dry_run and not heartbeat_worker_lock():
            print("  Worker lock lost — stopping scan processing")
            counts["partial_failures"] = True
            break

        if not result.get("ok"):
            counts["cards_failed"] += 1
            continue
        counts["cards_parsed"] += 1
        fields = result["fields"]
        project_id = fields["project_id"]

        eligible, reason = db.should_process_project(PLATFORM, project_id)
        if not eligible:
            print(f"  Skip {project_id}: {reason}")
            counts["projects_skipped"] += 1
            continue

        print(f"  New occurrence eligible ({reason}): {fields.get('title', '')[:50]}")
        counts["details_attempted"] += 1
        detail = fetch_project_details(driver, fields["source_url"], card_fields=fields)
        if detail.get("detail_extraction_status") == "COMPLETE":
            counts["details_completed"] += 1
        elif detail.get("detail_extraction_status") in ("FAILED", "TIMEOUT"):
            counts["details_failed"] += 1
            counts["partial_failures"] = True
        else:
            counts["details_completed"] += 1

        detail_fields = {
            **(detail.get("fields") or {}),
            "extraction_metadata": detail.get("extraction_metadata") or {},
        }
        card_for_merge = {
            **fields,
            "extraction_metadata": result.get("extraction_metadata") or {},
        }
        merged = merge_project_data(card_for_merge, detail_fields)

        if debug_extraction:
            print(json.dumps({
                "eligibility": reason,
                "title": merged.get("title"),
                "fields": {k: merged.get(k) for k in (
                    "project_id", "source_url", "budget_text", "location",
                    "duration_text", "status", "time_posted_text", "skills",
                )},
                "meta": merged.get("extraction_metadata"),
            }, default=str, indent=2)[:2500])

        notify_ok = within_notification_age(merged)
        if notify_ok:
            email_eligible = True
            email_status = "PENDING"
            email_reason = None
        else:
            email_eligible = False
            email_status = "NOT_REQUIRED"
            email_reason = "OUTSIDE_NOTIFICATION_AGE_WINDOW"
            counts["emails_suppressed"] += 1

        row = build_project_row(
            merged,
            scraper_run_id=run_id,
            card_status=result.get("card_extraction_status") or "COMPLETE",
            detail_status=detail.get("detail_extraction_status") or "FAILED",
            email_eligible=email_eligible,
            email_status=email_status if not dry_run else email_status,
            email_sent=False,
            email_not_sent_reason=email_reason,
            detail_failure_code=detail.get("detail_failure_code"),
            missing_fields=list(set((result.get("missing_fields") or []) + (detail.get("missing_fields") or []))),
            extraction_warnings=list(set((result.get("extraction_warnings") or []) + (detail.get("extraction_warnings") or []))),
        )
        # detected_at is an email-template value only; projects has no such column
        detected_at = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")

        if dry_run:
            print(f"  [dry-run] would insert + email={email_status} for {project_id}")
            continue

        try:
            project_uuid = db.insert_project_occurrence(row)
            counts["projects_inserted"] += 1
        except Exception as e:
            print(f"  DB insert failed: {e}")
            send_error_email(f"DB insert failed for {project_id}", str(e))
            counts["db_failed"] = True
            counts["partial_failures"] = True
            continue

        if send_emails and email_status == "PENDING":
            if not db.verify_lock_held():
                print("  Lock lost before email — leaving PENDING")
                counts["partial_failures"] = True
                continue
            email_result = send_project_email(
                project_uuid,
                {**row, "id": project_uuid, "detected_at": detected_at},
            )
            if email_result.get("success"):
                counts["emails_sent"] += 1
            else:
                counts["emails_failed"] += 1
                counts["partial_failures"] = True
        elif email_status == "NOT_REQUIRED":
            pass

        if not heartbeat_worker_lock(force=True):
            print("  Lock renew failed")
            counts["partial_failures"] = True
            break

    return counts


def run_auto_enrichment(driver, *, dry_run=False, limit=10) -> dict:
    if not Config.DETAIL_AUTO_ENRICHMENT_ENABLED:
        return {"enriched": 0}
    rows = db.get_projects_needing_enrichment(
        limit=limit,
        max_attempts=Config.DETAIL_MAX_AUTOMATIC_ATTEMPTS,
        cooldown_minutes=Config.DETAIL_RETRY_COOLDOWN_MINUTES,
    )
    enriched = 0
    for row in rows:
        if not should_auto_enrich(
            row,
            max_attempts=Config.DETAIL_MAX_AUTOMATIC_ATTEMPTS,
            cooldown_minutes=Config.DETAIL_RETRY_COOLDOWN_MINUTES,
        ):
            continue
        print(f"  Auto-enrich {row.get('project_id')}")
        if dry_run:
            continue
        detail = fetch_project_details(driver, row["source_url"], card_fields=row)
        _apply_enrichment(row, detail)
        enriched += 1
    return {"enriched": enriched}


def _apply_enrichment(row: dict, detail: dict) -> None:
    now = db._iso()
    detail_fields = detail.get("fields") or {}
    card_like = {k: row.get(k) for k in row.keys()}
    merged = merge_project_data(card_like, {
        **detail_fields,
        "extraction_metadata": detail.get("extraction_metadata") or {},
    })
    attempts = int(row.get("detail_attempt_count") or 0) + 1
    status = detail.get("detail_extraction_status") or "FAILED"
    update = {
        "short_description": merged.get("short_description"),
        "description": merged.get("description"),
        "status": merged.get("status"),
        "platform_category": merged.get("platform_category"),
        "platform_category_path": merged.get("platform_category_path") or [],
        "platform_category_raw": merged.get("platform_category_raw"),
        "platform_category_source": merged.get("platform_category_source"),
        "platform_category_confidence": merged.get("platform_category_confidence"),
        "platform_category_extraction_status": merged.get("platform_category_extraction_status"),
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
        "project_length": merged.get("project_length"),
        "start_date_text": merged.get("start_date_text"),
        "industry": merged.get("industry"),
        "skills": merged.get("skills") if merged.get("skills") is not None else row.get("skills"),
        "engagement_type": merged.get("engagement_type"),
        "remote_or_onsite": merged.get("remote_or_onsite"),
        "time_posted_text": merged.get("time_posted_text"),
        "source_posted_at": merged.get("source_posted_at"),
        "source_posted_at_is_estimated": merged.get("source_posted_at_is_estimated"),
        "detail_extraction_status": status,
        "detail_attempt_count": attempts,
        "detail_last_attempt_at": now,
        "detail_completed_at": now if status == "COMPLETE" else row.get("detail_completed_at"),
        "detail_failure_code": detail.get("detail_failure_code"),
        "detail_last_error": None if status == "COMPLETE" else (detail.get("extraction_warnings") or [None])[0],
        "missing_fields": detail.get("missing_fields") or [],
        "extraction_warnings": detail.get("extraction_warnings") or [],
        "extraction_metadata": merged.get("extraction_metadata"),
        "raw_data": merged.get("raw_data") or row.get("raw_data"),
        "last_seen_at": now,
    }
    # Strip None overwrites for useful existing values handled by merge already
    db.update_project_details(row["id"], update)


def backfill_missing_details(
    driver,
    *,
    dry_run=False,
    limit=50,
    project_id=None,
    retry_failed=False,
) -> dict:
    rows = db.get_projects_needing_enrichment(
        limit=limit,
        project_id=project_id,
        retry_failed=retry_failed,
        override_limits=True,
    )
    # Also allow COMPLETE override only when explicit project_id? Spec: select needing enrichment.
    # If project_id given and COMPLETE, still allow explicit backfill:
    if project_id and not rows:
        row = db.get_latest_project_occurrence(PLATFORM, project_id)
        if row:
            rows = [row]

    updated = 0
    for row in rows:
        if row.get("detail_extraction_status") == "COMPLETE" and not project_id:
            continue
        print(f"  Backfill {row.get('project_id')} ({row.get('detail_extraction_status')})")
        if dry_run:
            updated += 1
            continue
        detail = fetch_project_details(driver, row["source_url"], card_fields=row)
        _apply_enrichment(row, detail)
        updated += 1
    return {"updated": updated, "candidates": len(rows)}


def retry_pending_emails(*, dry_run=False) -> dict:
    rows = db.get_retryable_email_projects(max_retries=Config.EMAIL_MAX_RETRIES)
    sent = failed = skipped = 0
    for row in rows:
        status = row.get("email_status")
        if status in ("SUPPRESSED", "NOT_REQUIRED", "SENT"):
            skipped += 1
            continue
        print(f"  Retry email for {row.get('project_id')} attempt={int(row.get('email_attempt_count') or 0) + 1}")
        if dry_run:
            continue
        if not db.verify_lock_held():
            print("  Lock not held — aborting email retries")
            break
        result = send_project_email(row["id"], row)
        if result.get("success"):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": skipped, "candidates": len(rows)}


def finalize_run_status(counts: dict, *, auth_failed=False, db_failed=False) -> str:
    if auth_failed:
        return "AUTH_FAILED"
    if db_failed or counts.get("db_failed") or counts.get("persistence_failed"):
        return "FAILED"
    if counts.get("partial_failures") or counts.get("emails_failed") or counts.get("details_failed"):
        # details_failed alone during otherwise healthy scan → PARTIAL
        return "PARTIAL"
    return "COMPLETED"


# ============================
# MAIN LOOP
# ============================

def run_monitor(
    *,
    run_once=False,
    dry_run=False,
    debug=False,
    debug_extraction=False,
    skip_lock=False,
):
    print("=" * 50)
    print("FinTalent Project Monitor")
    print("=" * 50)
    print(f"  Account   : {Config.FINTALENT_EMAIL}")
    print(f"  Interval  : {Config.CHECK_INTERVAL}s")
    print(f"  Occurrence: {Config.OCCURRENCE_WINDOW_DAYS} day(s)")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    print(f"  Dry run   : {dry_run}")
    print()

    validate_config(require_smtp=not dry_run)
    db.ensure_schema_ready()

    lock_acquired = False
    if not skip_lock and not dry_run:
        lock = db.acquire_worker_lock(Config.FINTALENT_WORKER_LOCK_TTL_SECONDS)
        if not lock.get("acquired"):
            print(f"Could not acquire worker lock (held by {lock.get('owner')}). Exiting.")
            return 2
        lock_acquired = True
        global _last_lock_heartbeat
        _last_lock_heartbeat = time.monotonic()
        print(f"Worker lock acquired as {lock.get('self_owner')}")
    elif dry_run:
        print("Dry run: skipping worker lock and all writes")

    driver = initialize_driver()
    try:
        if not setup_session(driver):
            print("Failed to authenticate FinTalent session.")
            send_error_email("Authentication failed", "Could not log in to FinTalent. Check credentials.")
            if not dry_run:
                run_id = db.create_scraper_run(metadata={"phase": "auth"})
                db.fail_scraper_run(run_id, failure_code="AUTH_FAILED", failure_reason="Login failed", status="AUTH_FAILED")
            return 1

        check_count = 0
        while True:
            check_count += 1
            print(f"\n{'=' * 30}")
            print(f"Check #{check_count} — {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
            print(f"{'=' * 30}")

            run_id = None
            if not dry_run:
                run_id = db.create_scraper_run(metadata={"check": check_count})

            try:
                if not dry_run and lock_acquired and not ensure_worker_lock():
                    db.fail_scraper_run(run_id, failure_code="LOCK_LOST", failure_reason="Failed to renew lock")
                    print("Lock lost — aborting")
                    send_error_email("Worker lock lost", "Another instance may have taken over. Exiting.")
                    return 2

                driver.get(Config.TARGET_URL)
                time.sleep(4)
                if "login" in driver.current_url.lower() or "auth" in driver.current_url.lower():
                    print("  Session expired. Logging in again...")
                    if not perform_login(driver):
                        if run_id:
                            db.fail_scraper_run(
                                run_id, failure_code="AUTH_FAILED",
                                failure_reason="Re-login failed", status="AUTH_FAILED",
                            )
                        if run_once:
                            return 1
                        sleep_between_cycles(
                            Config.CHECK_INTERVAL,
                            holding_lock=lock_acquired and not dry_run,
                        )
                        continue
                    driver.get(Config.TARGET_URL)
                    time.sleep(4)

                cold = False
                if not dry_run:
                    cold = db.is_platform_cold_start()
                else:
                    # dry-run cold detection still platform-specific
                    try:
                        cold = db.is_platform_cold_start()
                    except Exception:
                        cold = False

                if cold:
                    counts = process_cold_start(
                        driver, run_id or "dry-run",
                        dry_run=dry_run, debug_extraction=debug_extraction or debug,
                    )
                    status = finalize_run_status(counts)
                    if counts.get("persistence_failed"):
                        status = "FAILED"
                    if run_id:
                        db.complete_scraper_run(run_id, status=status, **{
                            k: counts[k] for k in counts
                            if k in (
                                "cards_found", "cards_parsed", "cards_failed",
                                "details_attempted", "details_completed", "details_failed",
                                "projects_inserted", "projects_skipped",
                                "emails_sent", "emails_failed", "emails_suppressed",
                            )
                        })
                    print(f"Cold start finished status={status} inserted={counts['projects_inserted']}")
                else:
                    counts = process_scan_cycle(
                        driver, run_id or "dry-run",
                        dry_run=dry_run,
                        debug_extraction=debug_extraction or debug,
                        send_emails=not dry_run,
                    )
                    if not dry_run:
                        try:
                            run_auto_enrichment(driver, dry_run=False, limit=5)
                            retry_pending_emails(dry_run=False)
                        except Exception as e:
                            print(f"  Enrichment/retry warning: {e}")
                            counts["partial_failures"] = True

                    status = finalize_run_status(counts)
                    if run_id:
                        db.complete_scraper_run(run_id, status=status, **{
                            k: counts[k] for k in counts
                            if k in (
                                "cards_found", "cards_parsed", "cards_failed",
                                "details_attempted", "details_completed", "details_failed",
                                "projects_inserted", "projects_skipped",
                                "emails_sent", "emails_failed", "emails_suppressed",
                            )
                        })
                    print(
                        f"Scan done status={status} inserted={counts['projects_inserted']} "
                        f"skipped={counts['projects_skipped']} emailed={counts['emails_sent']}"
                    )

            except db.DatabaseError as e:
                print(f"Database error: {e}")
                send_error_email(f"Database error: {e.code}", str(e))
                if run_id:
                    db.fail_scraper_run(run_id, failure_code=e.code, failure_reason=str(e))
                if run_once:
                    return 1
            except Exception as e:
                print(f"Check cycle failed: {e}. Reinitializing driver...")
                send_error_email(f"Check cycle exception: {type(e).__name__}", traceback.format_exc()[-3000:])
                traceback.print_exc()
                if run_id:
                    try:
                        db.fail_scraper_run(run_id, failure_code="SCAN_EXCEPTION", failure_reason=str(e)[:500])
                    except Exception:
                        pass
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(min(Config.CHECK_INTERVAL, 30))
                driver = initialize_driver()
                setup_session(driver)

            if run_once:
                print("\nOnce mode complete. Exiting...")
                break
            sleep_between_cycles(Config.CHECK_INTERVAL, holding_lock=lock_acquired and not dry_run)

    except KeyboardInterrupt:
        print("\nMonitor stopped by user.")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        if lock_acquired:
            try:
                db.release_worker_lock()
                print("Worker lock released")
            except Exception as e:
                print(f"Warning releasing lock: {e}")
        print("FinTalent Monitor stopped.")
    return 0
