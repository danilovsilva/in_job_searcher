# in_job_searcher

A very small LinkedIn job search scraper built in a single `main.py` file.

It:
- Logs into LinkedIn using credentials from a local `.env` file (not committed)
- Searches jobs by keyword + location
- Applies simple include/exclude keyword filters
- Can skip jobs whose description is not in English
- Exports results to `jobs.xlsx` (or `jobs.csv`)
- Deduplicates by job URL

> Note: Automated scraping may violate LinkedIn Terms of Service and can get your account rate-limited or restricted. Use responsibly (small runs, random delays, ideally a secondary account).

## Requirements
- Python 3.10+ (recommended)
- Google Chrome installed
- Selenium WebDriver support (Chrome)

## Install
Create and activate a virtual environment (recommended), then install dependencies:

### Windows (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

### Mac/Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
