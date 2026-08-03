"""Save FinTalent cookies/localStorage to Supabase scraper_sessions + local JSON."""
import json
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()


class Config:
    FINTALENT_EMAIL = os.getenv("FINTALENT_EMAIL")
    FINTALENT_PASSWORD = os.getenv("FINTALENT_PASSWORD")
    TARGET_URL = "https://talent.fintalent.io/overview"
    HEADLESS = os.getenv("HEADLESS", "False").lower() == "true"
    COOKIES_FILE = "fintalent_cookies.json"
    CHROME_BIN = os.getenv("CHROME_BIN")
    CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH")


def initialize_driver():
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    if Config.CHROME_BIN:
        options.binary_location = Config.CHROME_BIN
    service = Service(executable_path=Config.CHROMEDRIVER_PATH) if Config.CHROMEDRIVER_PATH else Service()
    driver = webdriver.Chrome(service=service, options=options)
    return driver


def is_logged_in(driver):
    try:
        current_url = driver.current_url.lower()
        if "login" in current_url or "signin" in current_url or "auth" in current_url:
            return False
        return "overview" in current_url
    except Exception:
        return False


def save_cookies(driver):
    cookies = driver.get_cookies()
    local_storage = driver.execute_script("return window.localStorage;") or {}
    if not cookies:
        print("WARNING: No cookies found to save.")
        return False
    session_data = {
        "cookies": cookies,
        "local_storage": local_storage,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(Config.COOKIES_FILE, "w", encoding="utf-8") as f:
        json.dump(session_data, f)
    print(f"SUCCESS: Session data saved to local file: {Config.COOKIES_FILE}")
    try:
        import database as db
        db.save_scraper_session(cookies, local_storage)
        print("SUCCESS: Session data saved to Supabase scraper_sessions")
    except Exception as e:
        print(f"WARNING: Supabase session save failed: {e}")
    return True


def perform_login(driver):
    driver.get(Config.TARGET_URL)
    time.sleep(4)
    if is_logged_in(driver):
        return True
    email_field = None
    for sel in ["input[type='email']", "input[name='email']", "input[id*='email']", "input[name='username']"]:
        try:
            email_field = WebDriverWait(driver, 8).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            break
        except Exception:
            continue
    if not email_field:
        return False
    email_field.clear()
    email_field.send_keys(Config.FINTALENT_EMAIL)
    pass_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    pass_field.clear()
    pass_field.send_keys(Config.FINTALENT_PASSWORD)
    pass_field.send_keys(Keys.ENTER)
    time.sleep(5)
    WebDriverWait(driver, 15).until(lambda d: "overview" in d.current_url.lower())
    return True


def main():
    print("FinTalent Cookie Saver (Supabase)")
    driver = initialize_driver()
    try:
        driver.get(Config.TARGET_URL)
        time.sleep(4)
        if not is_logged_in(driver):
            if not perform_login(driver):
                print("MANUAL LOGIN REQUIRED — press Enter after overview loads")
                input()
        if is_logged_in(driver) or "overview" in driver.current_url.lower():
            save_cookies(driver)
        else:
            print(f"FAILED: URL={driver.current_url}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
