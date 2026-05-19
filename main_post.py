import hashlib
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import pandas as pd
from dotenv import load_dotenv
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

from main import (
    any_blocked,
    canonical_job_url,
    compute_score,
    detect_text_language,
    find_matching_keyword,
    get_first_scroll_container,
    init_driver,
    load_params,
    login_linkedin,
    matches_blocked_company,
    matches_location_filter,
    normalize_text,
    pick_scrollable_descendant,
    setup_run_logging,
    should_exclude_by_language,
    sleep_random,
    today_iso,
    utc_now_iso,
    write_output_with_failback,
)


PROJECT_DIR = Path(__file__).resolve().parent
BASE_PARAMS_PATH = PROJECT_DIR / "params.yaml"
POST_PARAMS_PATH = PROJECT_DIR / "params_posts.yaml"

POST_OUTPUT_COLUMNS = [
    "post_id",
    "profile_name",
    "profile_headline",
    "profile_url",
    "post_content",
    "post_links",
    "shared_job_title",
    "shared_job_company",
    "shared_job_location",
    "shared_job_url",
    "matched_positive_keywords",
    "matched_negative_keywords",
    "score",
    "description_language",
    "posted_at",
    "email",
    "first_seen",
    "last_seen",
    "last_scraped_at",
    "status_detail",
    "linkedin_status_detail",
    "notes",
]

LOCAL_SKIP_NOTE_PATTERNS = [
    "not applying",
    "skip",
]

EMAIL_REGEX = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
RELATIVE_DATE_REGEX = re.compile(
    r"\b(\d+\s*(min|h|d|sem|m[eÃª]s|mes|ano|anos)|agora|now)\b",
    re.IGNORECASE,
)


def deep_merge_dicts(base: Dict, override: Dict) -> Dict:
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_merged_params(base_path: Path, override_path: Path) -> Dict:
    base_params = load_params(base_path)
    override_params = load_params(override_path) if override_path.exists() else {}
    return deep_merge_dicts(base_params, override_params)


def resolve_post_queries(params: Dict) -> List[str]:
    raw_queries = params.get("post_keywords")
    if raw_queries is None:
        raw_queries = params.get("job_name", "")

    if isinstance(raw_queries, str):
        queries = [raw_queries]
    elif isinstance(raw_queries, list):
        queries = [str(item) for item in raw_queries]
    else:
        raise SystemExit("Invalid 'post_keywords' in params_posts.yaml. Use a string or a list of strings.")

    cleaned = [q.strip() for q in queries if str(q).strip()]
    if not cleaned:
        raise SystemExit("No valid post search queries found. Fill 'post_keywords' in params_posts.yaml.")
    return cleaned


def build_post_search_url(query: str, params: Dict) -> str:
    template = str(params.get("content_search_url", "") or "").strip()
    if template:
        return template.replace("{query}", quote(query))
    return (
        "https://www.linkedin.com/search/results/content/"
        f"?keywords={quote(query)}&origin=FACETED_SEARCH&sortBy=%5B%22date_posted%22%5D"
    )


def ensure_post_output_schema(df: pd.DataFrame) -> pd.DataFrame:
    if "notes" not in df.columns and "Note" in df.columns:
        df["notes"] = df["Note"]
    if "shared_job_company" not in df.columns and "sahred_job_company" in df.columns:
        df["shared_job_company"] = df["sahred_job_company"]
    elif "shared_job_company" in df.columns and "sahred_job_company" in df.columns:
        mask = df["shared_job_company"].fillna("").astype(str).str.strip() == ""
        df.loc[mask, "shared_job_company"] = df.loc[mask, "sahred_job_company"]

    for column in POST_OUTPUT_COLUMNS:
        if column not in df.columns:
            df[column] = None

    df = df[POST_OUTPUT_COLUMNS].copy()
    for column in ["post_links", "email", "status_detail", "linkedin_status_detail", "notes"]:
        df[column] = df[column].fillna("").astype(str)

    return df


def read_existing_post_output(output_file: str) -> pd.DataFrame:
    if not os.path.exists(output_file):
        return pd.DataFrame(columns=POST_OUTPUT_COLUMNS)

    try:
        if output_file.lower().endswith(".csv"):
            df = pd.read_csv(output_file)
        else:
            df = pd.read_excel(output_file)
        return ensure_post_output_schema(df)
    except Exception:
        return pd.DataFrame(columns=POST_OUTPUT_COLUMNS)


def extract_emails(text: str) -> str:
    found = []
    seen = set()
    for email in EMAIL_REGEX.findall(text or ""):
        email_norm = email.lower()
        if email_norm in seen:
            continue
        seen.add(email_norm)
        found.append(email)
    return "\n".join(found)


def first_non_empty_text(container, selectors: List[str]) -> str:
    for selector in selectors or []:
        try:
            element = container.find_element(By.CSS_SELECTOR, selector)
            text = (element.text or "").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def clean_linkedin_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def extract_post_id_from_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""

    patterns = [
        r"urn:li:(?:activity|share):(\d+)",
        r"(?:activity|share)-(\d+)",
        r"/feed/update/([^/?#]+)",
        r"[?&]updateId=([^&#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return ""


def get_text_lines(element) -> List[str]:
    try:
        raw_text = (element.text or "").strip()
    except Exception:
        raw_text = ""

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if lines:
        return lines

    try:
        aria_label = (element.get_attribute("aria-label") or "").strip()
    except Exception:
        aria_label = ""

    if not aria_label:
        return []

    cleaned = re.sub(r"^(view|open|visit)\s+", "", aria_label, flags=re.IGNORECASE).strip()
    return [cleaned] if cleaned else []


def is_relative_date_text(text: str) -> bool:
    return bool(RELATIVE_DATE_REGEX.search(normalize_text(text)))


def meaningful_profile_lines(lines: List[str]) -> List[str]:
    skipped_values = {"follow", "following", "connect", "1st", "2nd", "3rd", "promoted"}
    cleaned = []
    seen = set()
    for line in lines:
        value = line.strip()
        value_norm = normalize_text(value)
        if not value_norm or value_norm in skipped_values or is_relative_date_text(value):
            continue
        if value_norm in seen:
            continue
        seen.add(value_norm)
        cleaned.append(value)
    return cleaned


def build_record_id(post_id: str, shared_job_url: str, profile_url: str, post_content: str) -> str:
    if post_id:
        seed = f"post_id||{normalize_text(post_id)}"
    elif shared_job_url:
        seed = f"shared_job_url||{normalize_text(canonical_job_url(shared_job_url))}"
    else:
        seed = "profile_content||" + "||".join(
            [
                normalize_text(clean_linkedin_url(profile_url)),
                normalize_text(post_content)[:2000],
            ]
        )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def build_legacy_record_id(profile_url: str, posted_marker: str, post_content: str) -> str:
    seed = "||".join(
        [
            normalize_text(profile_url),
            normalize_text(posted_marker),
            normalize_text(post_content),
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def extract_post_identity(container) -> str:
    try:
        anchors = container.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        anchors = []

    for anchor in anchors:
        try:
            href = (anchor.get_attribute("href") or "").strip()
        except Exception:
            href = ""
        post_id = extract_post_id_from_url(href)
        if post_id:
            return post_id
    return ""


def job_name_matches_text(text: str, params: Dict) -> List[str]:
    blob = normalize_text(text)
    matches = []
    raw_job_names = params.get("job_name", []) or []
    if isinstance(raw_job_names, str):
        raw_job_names = [raw_job_names]

    for job_name in raw_job_names:
        job_name_str = str(job_name).strip()
        job_name_norm = normalize_text(job_name_str)
        if job_name_norm and job_name_norm in blob:
            matches.append(job_name_str)
    return matches


def post_matches_configured_job_names(row: Dict[str, str], params: Dict) -> bool:
    if not bool(params.get("require_post_job_name_match", True)):
        return True

    combined_text = " ".join(
        [
            str(row.get("shared_job_title") or ""),
            str(row.get("post_content") or ""),
            str(row.get("profile_headline") or ""),
        ]
    )
    return bool(job_name_matches_text(combined_text, params))


def extract_relative_value(raw_text: str) -> Optional[Tuple[int, str]]:
    text = normalize_text(raw_text)
    match = re.search(r"(\d+)\s*(min|h|d|sem|m[eê]s|mes|ano|anos)", text)
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def parse_posted_at(raw_text: str, now_local: datetime) -> Tuple[str, str]:
    raw_text = (raw_text or "").strip()
    normalized = normalize_text(raw_text)
    if not normalized:
        fallback = now_local.isoformat(timespec="seconds")
        return fallback, "relative_date_fallback:missing"

    if "agora" in normalized or "now" in normalized:
        return now_local.isoformat(timespec="seconds"), f"relative_date_parsed:{raw_text}"

    value_unit = extract_relative_value(normalized)
    if value_unit is None:
        fallback = now_local.isoformat(timespec="seconds")
        return fallback, f"relative_date_fallback:{raw_text}"

    value, unit = value_unit
    if unit == "min":
        posted_at = now_local - timedelta(minutes=value)
    elif unit == "h":
        posted_at = now_local - timedelta(hours=value)
    elif unit == "d":
        posted_at = now_local - timedelta(days=value)
    elif unit == "sem":
        posted_at = now_local - timedelta(weeks=value)
    elif unit in {"mês", "mes"}:
        posted_at = now_local - timedelta(days=value * 30)
    elif unit in {"ano", "anos"}:
        posted_at = now_local - timedelta(days=value * 365)
    else:
        fallback = now_local.isoformat(timespec="seconds")
        return fallback, f"relative_date_fallback:{raw_text}"

    return posted_at.isoformat(timespec="seconds"), f"relative_date_parsed:{raw_text}"


def collect_filtered_links(container, profile_url: str, current_url: str) -> Tuple[str, str]:
    post_links: List[str] = []
    shared_job_url = ""
    seen: Set[str] = set()
    current_norm = (current_url or "").strip()

    try:
        anchors = container.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        anchors = []

    for anchor in anchors:
        try:
            href = (anchor.get_attribute("href") or "").strip()
        except StaleElementReferenceException:
            continue
        except Exception:
            href = ""

        if not href:
            continue
        if href.startswith("javascript:") or href.startswith("#"):
            continue
        if href == profile_url:
            continue
        if "/search/results/content/" in href:
            continue
        if current_norm and href == current_norm:
            continue

        if "/jobs/view/" in href:
            shared_job_url = canonical_job_url(href)

        if href in seen:
            continue
        seen.add(href)
        post_links.append(href)

    return "\n".join(post_links), shared_job_url


def maybe_expand_post_text(driver, container, text_selector: str, button_selector: str) -> bool:
    if not text_selector or not button_selector:
        return False

    try:
        container.find_element(By.CSS_SELECTOR, text_selector)
    except Exception:
        return False

    try:
        button = container.find_element(By.CSS_SELECTOR, button_selector)
    except Exception:
        return False

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
    except Exception:
        pass

    try:
        driver.execute_script("arguments[0].click();", button)
        return True
    except Exception:
        try:
            button.click()
            return True
        except Exception:
            return False


def extract_post_header_fields(container, params: Dict) -> Tuple[str, str, str, str]:
    profile_link_selector = str(params.get("profile_link_selector", 'a[href*="/in/"]')).strip()
    profile_name_selectors = params.get("profile_name_selectors", []) or []
    profile_headline_selectors = params.get("profile_headline_selectors", []) or []
    post_date_selector = str(params.get("post_date_selector", "")).strip()

    profile_url = ""
    profile_name = ""
    profile_headline = ""
    posted_at_text = ""

    try:
        profile_link = container.find_element(By.CSS_SELECTOR, profile_link_selector)
        profile_url = (profile_link.get_attribute("href") or "").strip()
        paragraphs = profile_link.find_elements(By.CSS_SELECTOR, "p")
        texts = [(p.text or "").strip() for p in paragraphs if (p.text or "").strip()]
        if texts:
            profile_name = texts[0]
        if len(texts) >= 3:
            profile_headline = texts[2]
        elif len(texts) >= 2:
            profile_headline = texts[1]
        for text in texts[1:]:
            if is_relative_date_text(text):
                posted_at_text = text
                break
    except Exception:
        profile_link = None

    if profile_link is not None:
        if not profile_name:
            profile_name = first_non_empty_text(profile_link, profile_name_selectors)
        if not profile_headline:
            profile_headline = first_non_empty_text(profile_link, profile_headline_selectors)
        if not profile_name or not profile_headline:
            profile_lines = meaningful_profile_lines(get_text_lines(profile_link))
            if not profile_name and profile_lines:
                profile_name = profile_lines[0]
            if not profile_headline and len(profile_lines) >= 2:
                profile_headline = profile_lines[1]

    if not posted_at_text and post_date_selector:
        try:
            date_nodes = container.find_elements(By.CSS_SELECTOR, post_date_selector)
            for node in date_nodes:
                candidate = (node.text or "").strip()
                if is_relative_date_text(candidate):
                    posted_at_text = candidate
                    break
        except Exception:
            posted_at_text = ""

    if not posted_at_text:
        try:
            texts = [(p.text or "").strip() for p in container.find_elements(By.CSS_SELECTOR, "p")]
            for text in texts:
                if is_relative_date_text(text):
                    posted_at_text = text
                    break
        except Exception:
            pass

    return profile_name, profile_headline, profile_url, posted_at_text


def extract_shared_job_fields(container, params: Dict) -> Tuple[str, str, str, str]:
    link_selector = str(params.get("shared_job_link_selector", 'a[href*="/jobs/view/"]')).strip()
    title_selectors = params.get("shared_job_title_selectors", []) or []
    company_selectors = params.get("shared_job_company_selectors", []) or []
    location_selectors = params.get("shared_job_location_selectors", []) or []

    shared_job_url = ""
    shared_job_title = ""
    shared_job_company = ""
    shared_job_location = ""

    try:
        shared_link = container.find_element(By.CSS_SELECTOR, link_selector)
        shared_job_url = canonical_job_url((shared_link.get_attribute("href") or "").strip())
        shared_parent = shared_link
        for _ in range(3):
            try:
                shared_parent = shared_parent.find_element(By.XPATH, "./..")
            except Exception:
                break
        if title_selectors:
            shared_job_title = first_non_empty_text(shared_parent, title_selectors)
        if company_selectors:
            shared_job_company = first_non_empty_text(shared_parent, company_selectors)
        if location_selectors:
            shared_job_location = first_non_empty_text(shared_parent, location_selectors)
        link_lines = [line for line in get_text_lines(shared_link) if not is_relative_date_text(line)]
        parent_lines = [line for line in get_text_lines(shared_parent) if not is_relative_date_text(line)]
        fallback_lines = parent_lines or link_lines
        if not shared_job_title and fallback_lines:
            shared_job_title = fallback_lines[0]
        if not shared_job_company and len(fallback_lines) >= 2:
            shared_job_company = fallback_lines[1]
        if not shared_job_location and len(fallback_lines) >= 3:
            shared_job_location = fallback_lines[2]
    except Exception:
        pass

    return shared_job_title, shared_job_company, shared_job_location, shared_job_url


def extract_post_row(driver, container, params: Dict) -> Optional[Dict[str, str]]:
    text_selector = str(params.get("post_text_selector", 'span[data-testid="expandable-text-box"]')).strip()
    button_selector = str(params.get("post_more_button_selector", 'button[data-testid="expandable-text-button"]')).strip()

    expanded = maybe_expand_post_text(driver, container, text_selector, button_selector)
    post_content = ""
    if text_selector:
        try:
            post_content = (container.find_element(By.CSS_SELECTOR, text_selector).text or "").strip()
        except Exception:
            post_content = ""

    if not post_content:
        return None

    profile_name, profile_headline, profile_url, posted_at_text = extract_post_header_fields(container, params)
    now_local = datetime.now().astimezone()
    posted_at, posted_detail = parse_posted_at(posted_at_text, now_local)

    shared_job_title, shared_job_company, shared_job_location, shared_job_url = extract_shared_job_fields(container, params)
    post_id = extract_post_identity(container)
    post_links, collected_shared_job_url = collect_filtered_links(
        container,
        profile_url=profile_url,
        current_url=driver.current_url,
    )
    if not shared_job_url:
        shared_job_url = canonical_job_url(collected_shared_job_url)

    combined_link_text = "\n".join([post_links, shared_job_url]).strip()
    email = extract_emails(" ".join([post_content, combined_link_text]))

    detail_parts = [posted_detail]
    if expanded:
        detail_parts.append("expanded_text")
    if shared_job_url:
        detail_parts.append("shared_job_found")
    if not profile_url:
        detail_parts.append("missing_profile_url")

    return {
        "post_id": post_id,
        "profile_name": profile_name,
        "profile_headline": profile_headline,
        "profile_url": profile_url,
        "post_content": post_content,
        "post_links": post_links,
        "shared_job_title": shared_job_title,
        "shared_job_company": shared_job_company,
        "shared_job_location": shared_job_location,
        "shared_job_url": shared_job_url,
        "posted_at": posted_at,
        "posted_at_raw": posted_at_text,
        "email": email,
        "linkedin_status_detail": "; ".join([part for part in detail_parts if part]),
        "record_id": build_record_id(post_id, shared_job_url, profile_url, post_content),
    }


def should_exclude_post(row: Dict[str, str], params: Dict) -> Optional[str]:
    shared_company = str(row.get("shared_job_company") or "").strip()
    combined_company = " ".join(
        [
            shared_company,
            str(row.get("profile_name") or "").strip(),
            str(row.get("profile_headline") or "").strip(),
        ]
    ).strip()

    blocked_company = matches_blocked_company(combined_company, params.get("blocked_companies", []) or [])
    if blocked_company:
        return f"blocked company: {blocked_company}"

    language_match = should_exclude_by_language(str(row.get("post_content") or ""), params)
    if language_match:
        return language_match

    title = str(row.get("shared_job_title") or "").strip()
    company = str(row.get("shared_job_company") or "").strip()
    location = str(row.get("shared_job_location") or "").strip()
    description = " ".join(
        [
            str(row.get("post_content") or ""),
            str(row.get("profile_headline") or ""),
            str(row.get("post_links") or ""),
        ]
    ).strip()
    location_match = matches_location_filter(title, company, location, description, params)
    if location_match:
        return f"location filter: {location_match}"

    return None


def derive_post_row_state(row: pd.Series, params: Dict) -> Dict[str, str]:
    post_content = str(row.get("post_content") or "").strip()
    profile_name = str(row.get("profile_name") or "").strip()
    profile_headline = str(row.get("profile_headline") or "").strip()
    shared_job_title = str(row.get("shared_job_title") or "").strip()
    shared_job_company = str(row.get("shared_job_company") or "").strip()
    shared_job_location = str(row.get("shared_job_location") or "").strip()
    post_links = str(row.get("post_links") or "").strip()
    notes = str(row.get("notes") or row.get("Note") or "").strip()
    description_language = detect_text_language(post_content) or ""

    combined_text = " ".join(
        [
            profile_name,
            profile_headline,
            shared_job_title,
            shared_job_company,
            shared_job_location,
            post_content,
            post_links,
        ]
    ).strip()

    score, matched_pos, matched_neg = compute_score(
        combined_text,
        params.get("positive_keywords", {}) or {},
        params.get("negative_keywords", {}) or {},
    )

    require_pos = bool(params.get("require_at_least_one_positive_keyword", True))
    allow_without_pos = bool(params.get("allow_add_without_positive_match", False))

    note_skip_match = find_matching_keyword(notes, LOCAL_SKIP_NOTE_PATTERNS)
    excluded_by = should_exclude_post(row.to_dict(), params)
    blocked_by = any_blocked(combined_text, params.get("blocklist_keywords", []) or [])
    job_name_matches = job_name_matches_text(combined_text, params)

    status_detail = str(row.get("status_detail") or "").strip()
    if note_skip_match:
        status_detail = f"Skipped by notes: {notes or note_skip_match}"
    elif bool(params.get("require_post_job_name_match", True)) and not job_name_matches:
        status_detail = "Canceled by job_name policy"
    elif excluded_by:
        status_detail = f"Skipped by company/location filter: {excluded_by}"
    elif blocked_by:
        status_detail = f"Canceled by blocklist_keywords: {blocked_by}"
    elif require_pos and (not matched_pos) and (not allow_without_pos):
        status_detail = "Canceled by positive keyword policy"
    elif not status_detail:
        status_detail = "Included"

    linkedin_status_detail = str(row.get("linkedin_status_detail") or "").strip()
    if not linkedin_status_detail:
        linkedin_status_detail = "post_processed"

    return {
        "score": int(score),
        "matched_positive_keywords": ", ".join(matched_pos),
        "matched_negative_keywords": ", ".join(matched_neg),
        "description_language": description_language,
        "status_detail": status_detail,
        "linkedin_status_detail": linkedin_status_detail,
    }


def recalculate_post_rows(df: pd.DataFrame, params: Dict) -> pd.DataFrame:
    df = ensure_post_output_schema(df.copy())
    for idx, row in df.iterrows():
        derived = derive_post_row_state(row, params)
        for key, value in derived.items():
            df.at[idx, key] = value

    if bool(params.get("drop_posts_without_job_name_match", True)):
        keep_mask = df.apply(lambda row: post_matches_configured_job_names(row.to_dict(), params), axis=1)
        dropped = len(df) - int(keep_mask.sum())
        if dropped:
            print(f"[INFO] Dropped {dropped} post rows that did not match params.yaml job_name.")
        df = df.loc[keep_mask].copy()

    df = dedupe_post_rows(df)

    try:
        df["score"] = pd.to_numeric(df["score"], errors="coerce").fillna(0).astype(int)
        df = df.sort_values(by=["score", "last_seen", "posted_at"], ascending=[False, False, False]).reset_index(drop=True)
    except Exception:
        pass

    return df


def build_existing_record_map(df: pd.DataFrame) -> Dict[str, int]:
    existing = {}
    for idx, row in df.iterrows():
        record_id = build_record_id(
            str(row.get("post_id") or ""),
            str(row.get("shared_job_url") or ""),
            str(row.get("profile_url") or ""),
            str(row.get("post_content") or ""),
        )
        legacy_record_id = build_legacy_record_id(
            str(row.get("profile_url") or ""),
            str(row.get("posted_at") or "").strip(),
            str(row.get("post_content") or ""),
        )
        existing[record_id] = idx
        existing[legacy_record_id] = idx
    return existing


def dedupe_post_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    df = ensure_post_output_schema(df.copy())
    df["_record_key"] = df.apply(
        lambda row: build_record_id(
            str(row.get("post_id") or ""),
            str(row.get("shared_job_url") or ""),
            str(row.get("profile_url") or ""),
            str(row.get("post_content") or ""),
        ),
        axis=1,
    )
    df["_has_profile"] = df["profile_name"].fillna("").astype(str).str.strip().ne("").astype(int)
    df["_has_shared_job"] = df["shared_job_url"].fillna("").astype(str).str.strip().ne("").astype(int)
    before = len(df)
    df = df.sort_values(
        by=["_record_key", "_has_profile", "_has_shared_job", "last_scraped_at", "last_seen"],
        ascending=[True, False, False, False, False],
    ).drop_duplicates(subset=["_record_key"], keep="first")
    dropped = before - len(df)
    if dropped:
        print(f"[INFO] Dropped {dropped} duplicate post rows.")
    return df.drop(columns=["_record_key", "_has_profile", "_has_shared_job"])


def scroll_results_feed(driver, params: Dict, stable_rounds: int) -> int:
    container_selectors = params.get("post_root_scroll_container_selectors", []) or []
    pause_s = float(params.get("post_scroll_pause_seconds", 1.0))
    sleep_min = float(params.get("sleep_min_seconds", 1.0))
    sleep_max = float(params.get("sleep_max_seconds", 2.0))
    post_container_selector = str(params.get("post_container_selector", '[role="listitem"]')).strip()

    sel_used, root = get_first_scroll_container(driver, container_selectors)
    if root:
        scroll_target = pick_scrollable_descendant(driver, root)
        print(f"[INFO] Using posts scroll container selector: {sel_used}")
    else:
        scroll_target = None
        print(f"[WARN] Could not find posts scroll container. Falling back to window scroll. Tried: {container_selectors}")

    prev_count = len(driver.find_elements(By.CSS_SELECTOR, post_container_selector))

    if prev_count:
        try:
            last_card = driver.find_elements(By.CSS_SELECTOR, post_container_selector)[-1]
            driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", last_card)
        except Exception:
            pass

    try:
        if scroll_target is not None:
            driver.execute_script(
                "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;",
                scroll_target,
            )
        else:
            driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.9));")
    except Exception:
        pass

    sleep_random(pause_s, pause_s)
    sleep_random(sleep_min, sleep_max)

    current_count = len(driver.find_elements(By.CSS_SELECTOR, post_container_selector))
    if current_count == prev_count:
        return stable_rounds + 1
    return 0


def scrape_posts_for_query(driver, params: Dict, existing_df: pd.DataFrame, output_file: str, query: str) -> pd.DataFrame:
    sleep_min = float(params.get("sleep_min_seconds", 1.0))
    sleep_max = float(params.get("sleep_max_seconds", 2.0))
    save_after_each_post = bool(params.get("save_after_each_post", False))
    apply_formatting = bool(params.get("apply_row_formatting", True))
    post_container_selector = str(params.get("post_container_selector", '[role="listitem"]')).strip()
    max_scroll_rounds = int(params.get("post_scroll_max_rounds", 40))
    max_stable_rounds = int(params.get("post_scroll_stop_after_stable_rounds", 3))
    stop_after_known_posts = int(params.get("post_scroll_stop_after_known_posts", 10))

    if not post_container_selector:
        raise SystemExit("Missing required 'post_container_selector' in params_posts.yaml.")

    existing_df = ensure_post_output_schema(existing_df)
    existing_by_record = build_existing_record_map(existing_df)
    seen_in_run: Set[str] = set()

    search_url = build_post_search_url(query, params)
    print(f"[INFO] Opening post search URL for '{query}': {search_url}")
    driver.get(search_url)
    sleep_random(sleep_min, sleep_max)

    stable_rounds = 0
    consecutive_known_hits = 0
    rounds_completed = 0

    while rounds_completed < max_scroll_rounds:
        rounds_completed += 1
        try:
            containers = driver.find_elements(By.CSS_SELECTOR, post_container_selector)
        except Exception as exc:
            print(f"[WARN] Failed to locate post containers on round {rounds_completed}: {exc}")
            containers = []

        print(f"[INFO] Query '{query}' round {rounds_completed}/{max_scroll_rounds}: found {len(containers)} visible post cards")

        new_records_this_round = 0

        for index, container in enumerate(containers, start=1):
            try:
                extracted = extract_post_row(driver, container, params)
            except StaleElementReferenceException:
                print(f"[WARN] Query '{query}' round {rounds_completed}, card {index}: stale element while extracting")
                continue
            except Exception as exc:
                print(f"[WARN] Query '{query}' round {rounds_completed}, card {index}: extraction error -> {exc}")
                continue

            if not extracted:
                continue

            record_id = extracted["record_id"]
            if record_id in seen_in_run:
                continue
            seen_in_run.add(record_id)

            if not post_matches_configured_job_names(extracted, params):
                print(
                    f"[INFO] Query '{query}' round {rounds_completed}, card {index}: skipped post without params.yaml job_name match"
                )
                continue

            now_date = today_iso()
            now_ts = utc_now_iso()

            row_payload = {
                "post_id": extracted["post_id"],
                "profile_name": extracted["profile_name"],
                "profile_headline": extracted["profile_headline"],
                "profile_url": extracted["profile_url"],
                "post_content": extracted["post_content"],
                "post_links": extracted["post_links"],
                "shared_job_title": extracted["shared_job_title"],
                "shared_job_company": extracted["shared_job_company"],
                "shared_job_location": extracted["shared_job_location"],
                "shared_job_url": extracted["shared_job_url"],
                "posted_at": extracted["posted_at"],
                "email": extracted["email"],
                "first_seen": now_date,
                "last_seen": now_date,
                "last_scraped_at": now_ts,
                "status_detail": "",
                "linkedin_status_detail": extracted["linkedin_status_detail"],
                "notes": "",
            }

            if record_id in existing_by_record:
                consecutive_known_hits += 1
                idx = existing_by_record[record_id]
                for key, value in row_payload.items():
                    if key == "first_seen":
                        continue
                    if str(value).strip():
                        existing_df.at[idx, key] = value
                existing_df.at[idx, "last_seen"] = now_date
                existing_df.at[idx, "last_scraped_at"] = now_ts
                derived = derive_post_row_state(existing_df.loc[idx], params)
                for key, value in derived.items():
                    existing_df.at[idx, key] = value
                print(f"[INFO] Query '{query}' round {rounds_completed}, card {index}: updated existing post record")
            else:
                consecutive_known_hits = 0
                new_records_this_round += 1
                existing_df = pd.concat([existing_df, pd.DataFrame([row_payload])], ignore_index=True)
                new_idx = existing_df.index[-1]
                existing_by_record[record_id] = new_idx
                derived = derive_post_row_state(existing_df.loc[new_idx], params)
                for key, value in derived.items():
                    existing_df.at[new_idx, key] = value
                print(f"[OK] Query '{query}' round {rounds_completed}, card {index}: added new post record")

                if save_after_each_post:
                    saved_path, used_failback = write_output_with_failback(
                        recalculate_post_rows(existing_df, params),
                        output_file,
                        apply_formatting=apply_formatting,
                    )
                    if used_failback and saved_path:
                        print(f"[WARN] Incremental post save redirected to failback -> {saved_path}")
                    elif saved_path:
                        print(f"[INFO] Incremental post save -> {saved_path}")

        if consecutive_known_hits >= stop_after_known_posts:
            print(
                f"[INFO] Stopping query '{query}' because {consecutive_known_hits} consecutive known posts were reached."
            )
            break

        if new_records_this_round == 0 and not containers:
            stable_rounds += 1
        else:
            stable_rounds = scroll_results_feed(driver, params, stable_rounds)

        if stable_rounds >= max_stable_rounds:
            print(f"[INFO] Stopping query '{query}' because the feed stayed stable for {stable_rounds} rounds.")
            break

    return recalculate_post_rows(existing_df, params)


def main() -> None:
    log_file_path = setup_run_logging("main_post")
    print(f"[INFO] Writing logs to: {log_file_path}")

    params = load_merged_params(BASE_PARAMS_PATH, POST_PARAMS_PATH)
    queries = resolve_post_queries(params)

    load_dotenv()
    email = os.getenv("LINKEDIN_EMAIL", "").strip()
    password = os.getenv("LINKEDIN_PASSWORD", "").strip()

    if not email or not password:
        raise SystemExit("Missing LINKEDIN_EMAIL / LINKEDIN_PASSWORD. Create a local .env file (not committed).")

    output_file = os.path.abspath(os.path.expanduser(str(params.get("output_file", "output_posts.xlsx")).strip()))
    existing_df = read_existing_post_output(output_file)
    existing_df = recalculate_post_rows(existing_df, params)
    print(f"[INFO] Existing post output rows: {len(existing_df)}")

    driver = init_driver(headless=bool(params.get("headless", False)))
    try:
        login_linkedin(
            driver,
            email,
            password,
            float(params.get("sleep_min_seconds", 1.0)),
            float(params.get("sleep_max_seconds", 2.0)),
        )

        updated_df = existing_df
        total_queries = len(queries)
        for index, query in enumerate(queries, start=1):
            print(f"[INFO] Starting post query {index}/{total_queries} for '{query}'")
            updated_df = scrape_posts_for_query(driver, params, updated_df, output_file, query)

        saved_path, used_failback = write_output_with_failback(
            updated_df,
            output_file,
            apply_formatting=bool(params.get("apply_row_formatting", True)),
        )
        if used_failback and saved_path:
            print(f"[WARN] Final save redirected to failback -> {saved_path} (rows={len(updated_df)})")
        elif saved_path:
            print(f"[OK] Saved post output file: {saved_path} (rows={len(updated_df)})")
        else:
            print("[WARN] Final save failed and failback also failed. Finishing without saving final file.")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
