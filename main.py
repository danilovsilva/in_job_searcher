import os
import time
import random
import re
from pathlib import Path
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yaml
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import StaleElementReferenceException

from openpyxl import load_workbook
from openpyxl.styles import PatternFill


# =========================
# Basic helpers
# =========================
PROJECT_DIR = Path(__file__).resolve().parent
PARAMS_PATH = PROJECT_DIR / "params.yaml"


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")



def today_iso() -> str:
    return date.today().isoformat()



def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())



def extract_job_id_from_url(raw_url: str) -> Optional[str]:
    raw_url = (raw_url or "").strip()
    m = re.search(r"/jobs/view/(\d+)", raw_url)
    if not m:
        return None
    return m.group(1)



def canonical_job_url(raw_url: str) -> str:
    """
    Normalize LinkedIn job URLs so the same job doesn't create duplicates due to tracking params.
    Keeps only: https://www.linkedin.com/jobs/view/<job_id>/
    """
    job_id = extract_job_id_from_url(raw_url)
    if not job_id:
        return (raw_url or "").strip()
    return f"https://www.linkedin.com/jobs/view/{job_id}/"



def sleep_random(min_s: float, max_s: float) -> None:
    """Sleep random time between min_s and max_s seconds."""
    if min_s < 0:
        min_s = 0
    if max_s < min_s:
        max_s = min_s
    time.sleep(random.uniform(min_s, max_s))



def load_params(params_path) -> Dict:
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



def clean_job_title(raw_title: str) -> str:
    """
    Clean LinkedIn job titles that may include extra labels like 'with verification'.
    """
    t = (raw_title or "").strip()
    t = re.sub(r"\s+with verification\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


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
    "title",
    "company",
    "location",
    "status",
    "score",
    "matched_positive_keywords",
    "matched_negative_keywords",
    "url",
    "job_id",
    "description",
    "first_seen",
    "last_seen",
    "last_scraped_at",
    "status_detail",
    "notes",
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

        df = df[OUTPUT_COLUMNS]
        df["url"] = df["url"].apply(lambda x: canonical_job_url(str(x)) if pd.notna(x) else "")
        df["job_id"] = df["url"].apply(lambda u: extract_job_id_from_url(u) or "")
        return df
    except Exception:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)



def apply_status_formatting_xlsx(output_file: str, status_col_name: str = "status") -> None:
    wb = load_workbook(output_file)
    ws = wb.active

    header = [cell.value for cell in ws[1]]
    if status_col_name not in header:
        wb.save(output_file)
        return

    status_idx = header.index(status_col_name) + 1
    red_fill = PatternFill(start_color="FFFFC7CE", end_color="FFFFC7CE", fill_type="solid")
    gray_fill = PatternFill(start_color="FFD9D9D9", end_color="FFD9D9D9", fill_type="solid")

    for row in range(2, ws.max_row + 1):
        status_val = ws.cell(row=row, column=status_idx).value
        status_norm = normalize_text(str(status_val)) if status_val is not None else ""

        fill = None
        if status_norm == "closed":
            fill = red_fill
        elif status_norm == "applied":
            fill = gray_fill

        if fill:
            for col in range(1, ws.max_column + 1):
                ws.cell(row=row, column=col).fill = fill
        else:
            for col in range(1, ws.max_column + 1):
                ws.cell(row=row, column=col).fill = PatternFill(fill_type=None)

    wb.save(output_file)



def write_output(df: pd.DataFrame, output_file: str, apply_formatting: bool = True) -> None:
    if output_file.lower().endswith(".csv"):
        df.to_csv(output_file, index=False)
        return

    df.to_excel(output_file, index=False)

    if apply_formatting:
        try:
            apply_status_formatting_xlsx(output_file, status_col_name="status")
        except Exception:
            pass


# =========================
# Job status detection
# =========================

def detect_job_status(driver, params: Dict) -> Tuple[str, Optional[str]]:
    """
    Detect job status based on UI messages in the job details page/panel.
    """
    selectors = params.get("status_selectors", {}) or {}
    closed_texts = params.get("status_closed_texts", []) or []
    applied_texts = params.get("status_applied_texts", []) or []

    applied_sel = (selectors.get("applied_banner") or "").strip()
    alert_sel = (selectors.get("any_alert_container") or "").strip()
    root_sel = (selectors.get("right_panel_root") or "body").strip()

    if applied_sel:
        try:
            el = driver.find_element(By.CSS_SELECTOR, applied_sel)
            msg = (el.text or "").strip()
            if msg:
                msg_norm = normalize_text(msg)
                for t in applied_texts:
                    if normalize_text(t) in msg_norm:
                        return "applied", msg
        except Exception:
            pass

    page_text = ""
    try:
        root = driver.find_element(By.CSS_SELECTOR, root_sel)
        page_text = (root.text or "").strip()
    except Exception:
        try:
            page_text = (driver.find_element(By.TAG_NAME, "body").text or "").strip()
        except Exception:
            page_text = ""

    page_text_norm = normalize_text(page_text)

    if "status da candidatura" in page_text_norm:
        for t in applied_texts:
            if normalize_text(t) in page_text_norm:
                return "applied", t

    if "application status" in page_text_norm:
        for t in applied_texts:
            if normalize_text(t) in page_text_norm:
                return "applied", t

    if "acessar site da empresa" in page_text_norm:
        for t in applied_texts:
            if normalize_text(t) in page_text_norm:
                return "applied", t
        return "applied", "Acessar site da empresa"

    if "visit company website" in page_text_norm or "go to company website" in page_text_norm:
        for t in applied_texts:
            if normalize_text(t) in page_text_norm:
                return "applied", t
        return "applied", "Company website application status"

    alert_text = ""
    if alert_sel:
        try:
            alerts = driver.find_elements(By.CSS_SELECTOR, alert_sel)
            alert_text = " ".join([(a.text or "").strip() for a in alerts if (a.text or "").strip()]).strip()
        except Exception:
            alert_text = ""

    blob = normalize_text(" ".join([page_text, alert_text]))

    for t in closed_texts:
        if normalize_text(t) in blob:
            return "closed", t

    for t in applied_texts:
        if normalize_text(t) in blob:
            return "applied", t

    return "open", None


# =========================
# Scrolling (left panel)
# =========================

def pick_scrollable_descendant(driver, root_el):
    try:
        candidate = driver.execute_script(
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
        return candidate
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
    sel_used, root = get_first_scroll_container(driver, container_selectors)
    if not root:
        print(f"[WARN] Could not find left scroll container. Tried: {container_selectors}")
        return

    scroll_el = pick_scrollable_descendant(driver, root)
    print(f"[INFO] Using left scroll container selector: {sel_used}")

    prev_count = -1
    stable_rounds = 0

    for r in range(max_rounds):
        try:
            containers = driver.find_elements(By.CSS_SELECTOR, job_container_selector)
            count = len(containers)
        except Exception:
            containers = []
            count = 0

        if count == prev_count:
            stable_rounds += 1
        else:
            stable_rounds = 0

        if stable_rounds >= 3:
            break

        prev_count = count

        if containers:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", containers[-1])
            except Exception:
                pass

        try:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;",
                scroll_el
            )
        except Exception:
            pass

        time.sleep(pause_s)
        sleep_random(sleep_min, sleep_max)

    print(f"[INFO] Left panel scroll completed (rounds={r+1})")


# =========================
# Scraping (stale-proof: snapshot URLs)
# =========================

def scrape_jobs(driver: webdriver.Chrome, params: Dict, existing_df: pd.DataFrame, output_file: str) -> pd.DataFrame:
    job_name = str(params.get("job_name", "")).strip()
    max_pages = int(params.get("max_pages", 1))

    sleep_min = float(params.get("sleep_min_seconds", 1.0))
    sleep_max = float(params.get("sleep_max_seconds", 2.0))

    job_container_selector = str(params.get("job_container_selector", "")).strip()
    job_link_selector = str(params.get("job_link_selector", "")).strip()
    company_selector = str(params.get("company_selector", "")).strip()
    location_selector = str(params.get("location_selector", "")).strip()
    description_selectors = params.get("description_selectors", []) or []

    require_pos = bool(params.get("require_at_least_one_positive_keyword", True))
    allow_without_pos = bool(params.get("allow_add_without_positive_match", False))

    pos_keywords = params.get("positive_keywords", {}) or {}
    neg_keywords = params.get("negative_keywords", {}) or {}
    blocklist = params.get("blocklist_keywords", []) or []

    geo_id = int(params.get("geo_id", 92000000))
    remote_f_wt = params.get("remote_filter_f_wt", 2)
    start_step = int(params.get("start_step", 25))

    ignored_job_ids = {str(x).strip() for x in (params.get("ignored_job_ids", []) or []) if str(x).strip()}
    save_after_each_job = bool(params.get("save_after_each_job", False))
    apply_row_formatting = bool(params.get("apply_row_formatting", True))
    required_title_keywords = params.get("required_title_keywords", []) or []
    title_blocklist_keywords = params.get("title_blocklist_keywords", []) or []

    if not job_container_selector or not job_link_selector:
        raise SystemExit("Missing required selectors in params.yaml: job_container_selector and job_link_selector")

    existing_by_url: Dict[str, int] = {}
    if not existing_df.empty:
        for idx, row in existing_df.iterrows():
            url = canonical_job_url(str(row.get("url") or "").strip())
            if url:
                existing_df.at[idx, "url"] = url
                existing_df.at[idx, "job_id"] = extract_job_id_from_url(url) or ""
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
            job_container_selector=job_container_selector,
            max_rounds=int(params.get("left_list_scroll_max_rounds", 30)),
            pause_s=float(params.get("left_list_scroll_pause_seconds", 1.0)),
            sleep_min=sleep_min,
            sleep_max=sleep_max,
        )

        try:
            containers = driver.find_elements(By.CSS_SELECTOR, job_container_selector)
        except Exception as e:
            print(f"[WARN] Page {page + 1}: Failed to find containers -> {e}")
            containers = []

        job_urls: List[str] = []
        job_meta_by_url: Dict[str, Dict[str, str]] = {}

        for c in containers:
            try:
                link_el = c.find_element(By.CSS_SELECTOR, job_link_selector)
                url = canonical_job_url(link_el.get_attribute("href") or "")
                if not url:
                    continue

                job_id = extract_job_id_from_url(url) or ""
                if job_id and job_id in ignored_job_ids:
                    continue

                title = (link_el.get_attribute("aria-label") or "").strip() or (link_el.text or "").strip()
                title = clean_job_title(title)
                title_norm = normalize_text(title)

                if required_title_keywords and not any(normalize_text(k) in title_norm for k in required_title_keywords):
                    print(f"[INFO] Page {page + 1}: Skipped due to title filter -> {title}")
                    continue

                if title_blocklist_keywords and any(normalize_text(k) in title_norm for k in title_blocklist_keywords):
                    print(f"[INFO] Page {page + 1}: Skipped due to title blocklist -> {title}")
                    continue

                company = ""
                if company_selector:
                    try:
                        company = (c.find_element(By.CSS_SELECTOR, company_selector).text or "").strip()
                    except Exception:
                        company = ""

                job_loc = ""
                if location_selector:
                    try:
                        job_loc = (c.find_element(By.CSS_SELECTOR, location_selector).text or "").strip()
                    except Exception:
                        job_loc = ""

                job_urls.append(url)
                job_meta_by_url[url] = {
                    "job_id": job_id,
                    "title": title,
                    "company": company,
                    "location": job_loc,
                }
            except Exception:
                continue

        seen = set()
        job_urls = [u for u in job_urls if not (u in seen or seen.add(u))]
        print(f"[INFO] Page {page + 1}: Found {len(job_urls)} job URLs")

        for j, job_url in enumerate(job_urls, start=1):
            print(f"[INFO] Page {page + 1}, Job {j} -> Processing URL...")
            sleep_random(sleep_min, sleep_max)

            try:
                if job_url in seen_in_this_run:
                    print(f"[INFO] Page {page + 1}, Job {j}: Already processed in this run, skipping")
                    continue
                seen_in_this_run.add(job_url)

                job_id = extract_job_id_from_url(job_url) or ""
                if job_id and job_id in ignored_job_ids:
                    print(f"[INFO] Page {page + 1}, Job {j}: Ignored by job_id={job_id}")
                    continue

                clicked = False
                if job_id:
                    try:
                        fresh_link = driver.find_element(By.CSS_SELECTOR, f'a[href*="/jobs/view/"][href*="{job_id}"]')
                        driver.execute_script("arguments[0].click();", fresh_link)
                        clicked = True
                    except Exception:
                        clicked = False

                if not clicked:
                    driver.get(job_url)

                sleep_random(sleep_min, sleep_max)

                meta = job_meta_by_url.get(job_url, {})
                title = meta.get("title", "")
                company = meta.get("company", "")
                job_loc = meta.get("location", "")

                if not title:
                    try:
                        title = driver.find_element(By.CSS_SELECTOR, "h1").text.strip()
                        title = clean_job_title(title)
                    except Exception:
                        title = ""

                description = find_first_text_by_selectors(driver, description_selectors)
                status, status_detail = detect_job_status(driver, params)
                combined_text = " ".join([title, company, job_loc, description])

                blocked_by = any_blocked(combined_text, blocklist)
                if blocked_by:
                    print(f"[INFO] Page {page + 1}, Job {j}: Blocked by keyword -> {blocked_by}")
                    continue

                score, matched_pos, matched_neg = compute_score(combined_text, pos_keywords, neg_keywords)
                has_pos_match = len(matched_pos) > 0
                if require_pos and (not has_pos_match) and (not allow_without_pos):
                    print(f"[INFO] Page {page + 1}, Job {j}: No positive keyword match, skipping by policy")
                    continue

                now_date = today_iso()
                now_ts = utc_now_iso()

                if job_url in existing_by_url:
                    idx = existing_by_url[job_url]
                    existing_df.at[idx, "last_seen"] = now_date
                    existing_df.at[idx, "last_scraped_at"] = now_ts
                    existing_df.at[idx, "status"] = status
                    existing_df.at[idx, "status_detail"] = status_detail

                    if title:
                        existing_df.at[idx, "title"] = title
                    if company:
                        existing_df.at[idx, "company"] = company
                    if job_loc:
                        existing_df.at[idx, "location"] = job_loc

                    existing_df.at[idx, "job_id"] = job_id
                    existing_df.at[idx, "score"] = score
                    existing_df.at[idx, "matched_positive_keywords"] = ", ".join(matched_pos)
                    existing_df.at[idx, "matched_negative_keywords"] = ", ".join(matched_neg)
                    existing_df.at[idx, "description"] = description or existing_df.at[idx, "description"]
                    print(f"[OK] Page {page + 1}, Job {j}: Updated existing job")
                else:
                    new_row = {
                        "title": title,
                        "company": company,
                        "location": job_loc,
                        "status": status,
                        "score": score,
                        "matched_positive_keywords": ", ".join(matched_pos),
                        "matched_negative_keywords": ", ".join(matched_neg),
                        "url": job_url,
                        "job_id": job_id,
                        "description": description,
                        "first_seen": now_date,
                        "last_seen": now_date,
                        "last_scraped_at": now_ts,
                        "status_detail": status_detail,
                        "notes": "",
                    }
                    existing_df = pd.concat([existing_df, pd.DataFrame([new_row])], ignore_index=True)
                    existing_by_url[job_url] = existing_df.index[-1]
                    print(f"[OK] Page {page + 1}, Job {j}: Added new job to output")

                if save_after_each_job:
                    try:
                        write_output(existing_df, output_file, apply_formatting=apply_row_formatting)
                        print(f"[INFO] Incremental save -> {output_file} (rows={len(existing_df)})")
                    except Exception as e:
                        print(f"[WARN] Incremental save failed -> {e}")

            except StaleElementReferenceException as e:
                print(f"[WARN] Page {page + 1}, Job {j}: Stale element -> {e}")
            except Exception as e:
                print(f"[WARN] Page {page + 1}, Job {j}: Error processing job -> {e}")

        sleep_random(sleep_min, sleep_max)

    try:
        existing_df["score"] = pd.to_numeric(existing_df["score"], errors="coerce").fillna(0).astype(int)
        existing_df = existing_df.sort_values(by=["score", "last_seen"], ascending=[False, False]).reset_index(drop=True)
    except Exception:
        pass

    return existing_df



def main():
    params = load_params(PARAMS_PATH)

    load_dotenv()
    email = os.getenv("LINKEDIN_EMAIL", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "").strip()

    if not email or not password:
        raise SystemExit("Missing LINKEDIN_EMAIL / LINKEDIN_PASSWORD. Create a local .env file (not committed).")

    output_file = str(params.get("output_file", "jobs.xlsx")).strip()
    output_file = os.path.expanduser(output_file)
    output_file = os.path.abspath(output_file)
    headless = bool(params.get("headless", False))

    sleep_min = float(params.get("sleep_min_seconds", 1.0))
    sleep_max = float(params.get("sleep_max_seconds", 2.0))

    existing_df = read_existing_output(output_file)
    print(f"[INFO] Existing output rows: {len(existing_df)}")

    driver = init_driver(headless=headless)
    try:
        login_linkedin(driver, email, password, sleep_min, sleep_max)
        updated_df = scrape_jobs(driver, params, existing_df, output_file)
        write_output(updated_df, output_file, apply_formatting=bool(params.get("apply_row_formatting", True)))
        print(f"[OK] Saved output file: {output_file} (rows={len(updated_df)})")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
