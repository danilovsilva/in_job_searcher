import os
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv

from main import (
    PARAMS_PATH,
    canonical_job_url,
    detect_job_status,
    init_driver,
    load_params,
    login_linkedin,
    read_existing_output,
    recalculate_output_rows,
    sleep_random,
    write_output_with_failback,
)


PROJECT_DIR = Path(__file__).resolve().parent


def update_status():
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
    apply_row_formatting = bool(params.get("apply_row_formatting", True))

    if not os.path.exists(output_file):
        print(f"Output file not found: {output_file}")
        return

    df = read_existing_output(output_file)
    df = recalculate_output_rows(df, params)
    saved_path, used_failback = write_output_with_failback(
        df,
        output_file,
        apply_formatting=apply_row_formatting,
    )
    if used_failback and saved_path:
        print(f"[WARN] Recalculated output redirected to failback -> {saved_path} (rows={len(df)})")
    elif saved_path:
        print(f"[INFO] Recalculated output using current params -> {saved_path} (rows={len(df)})")
    else:
        print("[WARN] Recalculated output failed and failback also failed. Continuing.")

    open_jobs = df[df["status"].fillna("").astype(str).str.strip().str.lower() == "open"].copy()
    print(f"Open jobs to update: {len(open_jobs)}")

    driver = init_driver(headless=headless)

    try:
        login_linkedin(driver, email, password, sleep_min, sleep_max)

        for position, (idx, row) in enumerate(open_jobs.iterrows(), start=1):
            url = canonical_job_url(str(row.get("url", "")).strip())
            if not url:
                print(f"[WARN] Row {idx}: Missing URL, skipping")
                continue

            print(f"Checking job {position}/{len(open_jobs)} -> {url}")

            try:
                driver.get(url)
                sleep_random(sleep_min, sleep_max)

                status, detail = detect_job_status(driver, params)

                df.at[idx, "linkedin_status"] = status
                df.at[idx, "linkedin_status_detail"] = detail
                df.at[idx, "last_scraped_at"] = datetime.utcnow().isoformat(timespec="seconds")
                df = recalculate_output_rows(df, params)

                print(f"Status -> {df.at[idx, 'status']}")

                saved_path, used_failback = write_output_with_failback(
                    df,
                    output_file,
                    apply_formatting=apply_row_formatting,
                )
                if used_failback and saved_path:
                    print(f"[WARN] Incremental save redirected to failback -> {saved_path} (rows={len(df)})")
                elif saved_path:
                    print(f"[INFO] Incremental save -> {saved_path} (rows={len(df)})")
                else:
                    print("[WARN] Incremental save failed and failback also failed. Continuing.")

                sleep_random(sleep_min, sleep_max)

            except Exception as e:
                print(f"Error checking {url} -> {e}")

        print("Status update completed")

    finally:
        driver.quit()


if __name__ == "__main__":
    update_status()
