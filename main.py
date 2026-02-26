import os
import time
import random
import re
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yaml
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


# =========================
# Basic helpers
# =========================

def utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def sleep_random(min_s: float, max_s: float) -> None:
    """Sleep random time between min_s and max_s seconds."""
    if min_s < 0:
        min_s = 0
    if max_s < min_s:
        max_s = min_s
    time.sleep(random.uniform(min_s, max_s))


def load_params(params_path: str) -> Dict:
    with open(params_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_linkedin_url(job_name: str, geo_id: int, remote_f_wt: Optional[int], start: int = 0) -> str:
    """
    Build a LinkedIn Jobs search URL using geoId and optional remote filter.
    Example:
      https://www.linkedin.com/jobs/search/?f_WT=2&geoId=92000000&keywords=Data%20Engineer&start=25
    """
    from urllib.parse import quote

    base = "https://www.linkedin.com/jobs/search/?"
    params = []
    if remote_f_wt is not None:
        params.append(f"f_WT={int(remote_f_wt)}")
    params.append(f"geoId={int(geo_id)}")
    params.append(f"keywords={quote(job_name)}")
    params.append(f"start={int(start)}")
    return base + "&".join(params)

def init_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def login_linkedin(driver: webdriver.Chrome, email: str, password: str, sleep_min: float, sleep_max: float) -> None:
    driver.get("https://www.linkedin.com/login")
    sleep_random(sleep_min, sleep_max)

    driver.find_element(By.ID, "username").send_keys(email)
    driver.find_element(By.ID, "password").send_keys(password)
    driver.find_element(By.XPATH, "//button[@type='submit']").click()
    sleep_random(sleep_min, sleep_max)


def find_first_text_by_selectors(driver: webdriver.Chrome, selectors: List[str]) -> str:
    """Try multiple selectors and return the first non-empty text found."""
    for sel in selectors or []:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            text = (el.text or "").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


# =========================
# Keyword logic (ranking + filtering)
# =========================

def keyword_matches(text: str, weighted_keywords: Dict[str, int]) -> List[str]:
    """Return the list of keywords that appear in text (substring match after normalization)."""
    blob = normalize_text(text)
    matched = []
    for k in (weighted_keywords or {}).keys():
        if normalize_text(k) in blob:
            matched.append(k)
    return matched


def any_blocked(text: str, blocklist: List[str]) -> Optional[str]:
    """Return the blocking keyword if any is found, else None."""
    blob = normalize_text(text)
    for kw in blocklist or []:
        if normalize_text(kw) in blob:
            return kw
    return None


def compute_score(text: str, pos: Dict[str, int], neg: Dict[str, int]) -> Tuple[int, List[str], List[str]]:
    """
    score = sum(positive weights) - sum(negative weights)
    Returns: (score, matched_positive, matched_negative)
    """
    matched_pos = keyword_matches(text, pos)
    matched_neg = keyword_matches(text, neg)

    score_pos = sum(int(pos[k]) for k in matched_pos if k in pos)
    score_neg = sum(int(neg[k]) for k in matched_neg if k in neg)

    return (score_pos - score_neg), matched_pos, matched_neg


# =========================
# Output I/O
# =========================

OUTPUT_COLUMNS = [
    "url",
    "title",
    "company",
    "location",
    "score",
    "matched_positive_keywords",
    "matched_negative_keywords",
    "description",
    "first_seen",
    "last_seen",
    "last_scraped_at",
]


def read_existing_output(output_file: str) -> pd.DataFrame:
    """Read existing output file if present, else return empty DataFrame with expected columns."""
    if not os.path.exists(output_file):
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    try:
        if output_file.lower().endswith(".csv"):
            df = pd.read_csv(output_file)
        else:
            df = pd.read_excel(output_file)

        for c in OUTPUT_COLUMNS:
            if c not in df.columns:
                df[c] = None

        return df[OUTPUT_COLUMNS]
    except Exception:
        # If unreadable/corrupted, do not crash; start fresh.
        return pd.DataFrame(columns=OUTPUT_COLUMNS)


def write_output(df: pd.DataFrame, output_file: str) -> None:
    if output_file.lower().endswith(".csv"):
        df.to_csv(output_file, index=False)
    else:
        df.to_excel(output_file, index=False)


# =========================
# Scraping
# =========================

def scrape_jobs(driver: webdriver.Chrome, params: Dict, existing_df: pd.DataFrame, output_file: str) -> pd.DataFrame:
    job_name = str(params.get("job_name", "")).strip()
    location = str(params.get("location", "")).strip()
    max_pages = int(params.get("max_pages", 1))

    sleep_min = float(params.get("sleep_min_seconds", 1.0))
    sleep_max = float(params.get("sleep_max_seconds", 2.0))

    # New structured selectors (from params.yaml)
    job_container_selector = str(params.get("job_container_selector", "")).strip()
    job_link_selector = str(params.get("job_link_selector", "")).strip()
    company_selector = str(params.get("company_selector", "")).strip()
    location_selector = str(params.get("location_selector", "")).strip()

    # Description is still multi-selector fallback (right panel)
    description_selectors = params.get("description_selectors", []) or []

    append_existing_jobs = bool(params.get("append_existing_jobs", False))

    require_pos = bool(params.get("require_at_least_one_positive_keyword", True))
    allow_without_pos = bool(params.get("allow_add_without_positive_match", False))

    pos_keywords = params.get("positive_keywords", {}) or {}
    neg_keywords = params.get("negative_keywords", {}) or {}
    blocklist = params.get("blocklist_keywords", []) or []

    geo_id = int(params.get("geo_id", 92000000))
    remote_f_wt = params.get("remote_filter_f_wt", 2)
    start_step = int(params.get("start_step", 25))

    if not job_container_selector or not job_link_selector:
        raise SystemExit(
            "Missing required selectors in params.yaml: job_container_selector and job_link_selector"
        )

    # Index existing rows by URL for fast lookup
    existing_by_url: Dict[str, int] = {}
    if not existing_df.empty:
        for idx, row in existing_df.iterrows():
            url = str(row.get("url") or "").strip()
            if url:
                existing_by_url[url] = idx

    seen_in_this_run = set()

    for page in range(max_pages):
        start = page * start_step
        search_url = build_linkedin_url(job_name, geo_id, remote_f_wt, start=start)

        print(f"[INFO] Page {page + 1}/{max_pages} -> Opening search URL: {search_url}")
        driver.get(search_url)
        sleep_random(sleep_min, sleep_max)
        
        scroll_left_results_panel(
            driver,
            container_selectors=params.get("left_list_scroll_container_selectors", []) or [],
            job_container_selector=str(params.get("job_container_selector", "")).strip(),
            max_rounds=int(params.get("left_list_scroll_max_rounds", 30)),
            pause_s=float(params.get("left_list_scroll_pause_seconds", 1.0)),
            sleep_min=sleep_min,
            sleep_max=sleep_max,
        )
        sleep_random(sleep_min, sleep_max)

        # Find containers first (structured approach)
        try:
            containers = driver.find_elements(By.CSS_SELECTOR, job_container_selector)
        except Exception as e:
            print(f"[WARN] Page {page + 1}: Failed to find containers -> {e}")
            containers = []

        print(f"[INFO] Page {page + 1}: Found {len(containers)} job containers")

        for j, container in enumerate(containers, start=1):
            print(f"[INFO] Page {page + 1}, Job {j} -> Processing container...")
            sleep_random(sleep_min, sleep_max)

            try:
                # Get link element inside the container
                link_el = container.find_element(By.CSS_SELECTOR, job_link_selector)
                job_url = (link_el.get_attribute("href") or "").strip()
                if not job_url:
                    print(f"[WARN] Page {page + 1}, Job {j}: Missing URL, skipping")
                    continue

                # Avoid duplicates within this run
                if job_url in seen_in_this_run:
                    print(f"[INFO] Page {page + 1}, Job {j}: Already processed in this run, skipping")
                    continue
                seen_in_this_run.add(job_url)

                # Title: prefer aria-label (stable), fallback to text
                title = (link_el.get_attribute("aria-label") or "").strip()
                if not title:
                    title = (link_el.text or "").strip()
                
                title = clean_job_title(title)

                # Company and location (best-effort; can fail if selector changes)
                company = ""
                if company_selector:
                    try:
                        company = (container.find_element(By.CSS_SELECTOR, company_selector).text or "").strip()
                    except Exception:
                        company = ""

                job_loc = ""
                if location_selector:
                    try:
                        job_loc = (container.find_element(By.CSS_SELECTOR, location_selector).text or "").strip()
                    except Exception:
                        job_loc = ""

                # Click the job link to load right panel description (best-effort)
                try:
                    driver.execute_script("arguments[0].click();", link_el)
                except Exception:
                    try:
                        link_el.click()
                    except Exception:
                        pass

                sleep_random(sleep_min, sleep_max)

                description = find_first_text_by_selectors(driver, description_selectors)
                combined_text = " ".join([title, company, job_loc, description])

                # Blocklist: skip entirely if matched
                blocked_by = any_blocked(combined_text, blocklist)
                if blocked_by:
                    print(f"[INFO] Page {page + 1}, Job {j}: Blocked by keyword -> {blocked_by}")
                    continue

                score, matched_pos, matched_neg = compute_score(combined_text, pos_keywords, neg_keywords)

                # Inclusion policy
                has_pos_match = len(matched_pos) > 0
                if require_pos and (not has_pos_match) and (not allow_without_pos):
                    print(f"[INFO] Page {page + 1}, Job {j}: No positive keyword match, skipping by policy")
                    continue

                now_date = today_iso()
                now_ts = utc_now_iso()

                if job_url in existing_by_url:
                    idx = existing_by_url[job_url]

                    # Always update last_seen + last_scraped_at
                    existing_df.at[idx, "last_seen"] = now_date
                    existing_df.at[idx, "last_scraped_at"] = now_ts

                    # Refresh values (optional but useful)
                    if title:
                        existing_df.at[idx, "title"] = title
                    if company:
                        existing_df.at[idx, "company"] = company
                    if job_loc:
                        existing_df.at[idx, "location"] = job_loc

                    existing_df.at[idx, "score"] = score
                    existing_df.at[idx, "matched_positive_keywords"] = ", ".join(matched_pos)
                    existing_df.at[idx, "matched_negative_keywords"] = ", ".join(matched_neg)

                    existing_df.at[idx, "description"] = description or existing_df.at[idx, "description"]

                    print(f"[OK] Page {page + 1}, Job {j}: Updated existing job (last_seen refreshed)")

                    # If user wants duplicates, append another row (not recommended)
                    if append_existing_jobs:
                        new_row = {
                            "url": job_url,
                            "title": title,
                            "company": company,
                            "location": job_loc,
                            "score": score,
                            "matched_positive_keywords": ", ".join(matched_pos),
                            "matched_negative_keywords": ", ".join(matched_neg),
                            "description": description,
                            "first_seen": existing_df.at[idx, "first_seen"] or now_date,
                            "last_seen": now_date,
                            "last_scraped_at": now_ts,
                        }
                        existing_df = pd.concat([existing_df, pd.DataFrame([new_row])], ignore_index=True)
                        print(f"[OK] Page {page + 1}, Job {j}: Appended duplicate row (append_existing_jobs=true)")
                        if bool(params.get("save_after_each_job", False)):
                            try:
                                write_output(existing_df, output_file)
                                print(f"[INFO] Incremental save -> {output_file} (rows={len(existing_df)})")
                            except Exception as e:
                                print(f"[WARN] Incremental save failed -> {e}")

                else:
                    # New job: add it
                    new_row = {
                        "url": job_url,
                        "title": title,
                        "company": company,
                        "location": job_loc,
                        "score": score,
                        "matched_positive_keywords": ", ".join(matched_pos),
                        "matched_negative_keywords": ", ".join(matched_neg),
                        "description": description,
                        "first_seen": now_date,
                        "last_seen": now_date,
                        "last_scraped_at": now_ts,
                    }
                    existing_df = pd.concat([existing_df, pd.DataFrame([new_row])], ignore_index=True)
                    existing_by_url[job_url] = existing_df.index[-1]
                    print(f"[OK] Page {page + 1}, Job {j}: Added new job to output")
                    if bool(params.get("save_after_each_job", False)):
                        try:
                            write_output(existing_df, output_file)
                            print(f"[INFO] Incremental save -> {output_file} (rows={len(existing_df)})")
                        except Exception as e:
                            print(f"[WARN] Incremental save failed -> {e}")
                

            except Exception as e:
                print(f"[WARN] Page {page + 1}, Job {j}: Error processing job -> {e}")

        sleep_random(sleep_min, sleep_max)

    # Sort by score desc, then last_seen desc
    try:
        existing_df["score"] = pd.to_numeric(existing_df["score"], errors="coerce").fillna(0).astype(int)
        existing_df = existing_df.sort_values(by=["score", "last_seen"], ascending=[False, False]).reset_index(drop=True)
    except Exception:
        pass

    return existing_df

def pick_scrollable_descendant(driver, root_el):
    """
    LinkedIn often uses nested containers. This function tries to find the first descendant
    that is actually scrollable (overflow-y: auto/scroll and scrollHeight > clientHeight).
    Returns the best candidate element (or root_el as fallback).
    """
    try:
        candidates = driver.execute_script(
            """
            const root = arguments[0];
            const els = [root, ...root.querySelectorAll('*')];
            function isScrollable(el) {
              const style = window.getComputedStyle(el);
              const oy = style.overflowY;
              const canScroll = (oy === 'auto' || oy === 'scroll');
              return canScroll && el.scrollHeight > el.clientHeight + 5;
            }
            for (const el of els) {
              if (isScrollable(el)) return el;
            }
            return root;
            """,
            root_el,
        )
        return candidates
    except Exception:
        return root_el


def get_first_scroll_container(driver, selectors: List[str]):
    for sel in selectors or []:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            return sel, el
        except Exception:
            continue
    return None, None


def scroll_left_results_panel(driver,
                              container_selectors: List[str],
                              job_container_selector: str,
                              max_rounds: int,
                              pause_s: float,
                              sleep_min: float,
                              sleep_max: float) -> None:
    """
    Force-load more jobs by scrolling the LEFT list until it stops growing.

    Strategy:
    - Find the left panel root element using container_selectors
    - Find the actual scrollable descendant (sometimes LinkedIn scrolls a child)
    - In each round:
        - Count current job containers
        - Scroll to the last visible job container using scrollIntoView
        - Also scroll the scrollable element by a chunk
        - Wait and check if count increased
    """
    sel_used, root = get_first_scroll_container(driver, container_selectors)
    if not root:
        print(f"[WARN] Could not find left scroll container. Tried: {container_selectors}")
        return

    scroll_el = pick_scrollable_descendant(driver, root)
    print(f"[INFO] Using left scroll container selector: {sel_used}")

    prev_count = -1
    stable_rounds = 0

    for r in range(max_rounds):
        # Count job containers currently loaded
        try:
            containers = driver.find_elements(By.CSS_SELECTOR, job_container_selector)
            count = len(containers)
        except Exception:
            containers = []
            count = 0

        # print(f"[DEBUG] Left scroll round {r+1}: current job containers={count}")

        if count == prev_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        # If it hasn't grown for a few rounds, stop
        if stable_rounds >= 3:
            break

        prev_count = count

        # Scroll to last job container (more reliable than scrollTop)
        if containers:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", containers[-1])
            except Exception:
                pass

        # Also scroll the scrollable element itself
        try:
            before = driver.execute_script("return arguments[0].scrollTop;", scroll_el)
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;", scroll_el)
            after = driver.execute_script("return arguments[0].scrollTop;", scroll_el)
            # print(f"[DEBUG] scrollTop before={before} after={after}")
        except Exception:
            pass

        time.sleep(0.5)
        # sleep_random(sleep_min, sleep_max)

    print(f"[INFO] Left panel scroll completed (rounds={r+1})")

def clean_job_title(raw_title: str) -> str:
    """
    Clean LinkedIn job titles that may include extra labels like 'with verification'.
    """
    t = (raw_title or "").strip()
    # Common suffix seen in aria-label
    t = re.sub(r"\s+with verification\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()

def main():
    params = load_params("params.yaml")

    load_dotenv()
    email = os.getenv("LINKEDIN_EMAIL", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "").strip()

    if not email or not password:
        raise SystemExit("Missing LINKEDIN_EMAIL / LINKEDIN_PASSWORD. Create a local .env file (not committed).")

    output_file = str(params.get("output_file", "jobs.xlsx")).strip()
    headless = bool(params.get("headless", False))

    sleep_min = float(params.get("sleep_min_seconds", 1.0))
    sleep_max = float(params.get("sleep_max_seconds", 2.0))

    existing_df = read_existing_output(output_file)
    print(f"[INFO] Existing output rows: {len(existing_df)}")

    driver = init_driver(headless=headless)
    try:
        login_linkedin(driver, email, password, sleep_min, sleep_max)
        updated_df = scrape_jobs(driver, params, existing_df, output_file)
        write_output(updated_df, output_file)
        print(f"[OK] Saved output file: {output_file} (rows={len(updated_df)})")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()