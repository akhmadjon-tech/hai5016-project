"""
scrape.py
---------
Scrapes campus cafeteria menu pages and stores the results in Supabase.

Pipeline for each valid source in campus_menu_sources:
  1. Check if today's snapshot already exists in scraped_html_snapshots — skip if cached.
  2. Fetch the page.
  3. Extract the HTML of the relevant content div (using content_div_selector if set,
     otherwise the full page body after removing script/style tags).
  4. Compute a SHA-256 hash and compare with the most recent previous snapshot.
  5. Insert a new row into scraped_html_snapshots.
  6. Update last_scraped_at on the source row.
  7. If a source fails (timeout / HTTP error), mark it as valid=False.
"""

import hashlib
import os
from datetime import date, timezone, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from loguru import logger


PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
REQUEST_TIMEOUT_SECONDS = 10.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> Path:
    # Create the logs folder before adding the log file sink.
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "scrape.log"

    logger.remove()
    logger.add(log_path, rotation="1 MB", encoding="utf-8")
    logger.add(lambda message: print(message, end=""))

    return log_path


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_connection_string() -> str:
    # Read the PostgreSQL connection string from the .env file.
    load_dotenv()
    conn_str = os.getenv("SUPABASE_CONNECTION_STRING")
    if not conn_str:
        raise EnvironmentError("SUPABASE_CONNECTION_STRING must be set in .env")
    return conn_str


def load_valid_sources(conn_str: str) -> list[dict]:
    # Fetch every source that is marked as valid so we know what to scrape.
    sql = """
    SELECT id, university_name, campus, source_url, content_div_selector
    FROM public.campus_menu_sources
    WHERE valid = TRUE
    """
    with psycopg.connect(conn_str, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()


def already_scraped_today(conn_str: str, source_id: str, today: date) -> bool:
    # Return True if we already have a snapshot for this source for today.
    # This prevents duplicate work when the script is run more than once a day.
    sql = """
    SELECT 1 FROM public.scraped_html_snapshots
    WHERE source_id = %s AND scrape_date = %s
    LIMIT 1
    """
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (source_id, today.isoformat()))
            return cur.fetchone() is not None


def get_last_snapshot(conn_str: str, source_id: str) -> dict | None:
    # Fetch the most recent snapshot for this source so we can compare hashes.
    sql = """
    SELECT id, html_sha256
    FROM public.scraped_html_snapshots
    WHERE source_id = %s
    ORDER BY scraped_at DESC
    LIMIT 1
    """
    with psycopg.connect(conn_str, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (source_id,))
            return cur.fetchone()


def save_snapshot(conn_str: str, payload: dict) -> None:
    # Insert the new snapshot row into scraped_html_snapshots.
    sql = """
    INSERT INTO public.scraped_html_snapshots (
        source_id, source_url, scrape_date, status_code, final_url,
        content_type, etag, last_modified, html_raw, html_sha256,
        is_changed, previous_snapshot_id, change_reason
    ) VALUES (
        %(source_id)s, %(source_url)s, %(scrape_date)s, %(status_code)s, %(final_url)s,
        %(content_type)s, %(etag)s, %(last_modified)s, %(html_raw)s, %(html_sha256)s,
        %(is_changed)s, %(previous_snapshot_id)s, %(change_reason)s
    )
    """
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, payload)
        conn.commit()


def mark_source_scraped(conn_str: str, source_id: str) -> None:
    # Record the current UTC time as the last_scraped_at on the source row.
    now_utc = datetime.now(timezone.utc).isoformat()
    sql = "UPDATE public.campus_menu_sources SET last_scraped_at = %s WHERE id = %s"
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (now_utc, source_id))
        conn.commit()


def mark_source_invalid(conn_str: str, source_id: str) -> None:
    # Mark the source as invalid so future runs skip it.
    sql = "UPDATE public.campus_menu_sources SET valid = FALSE WHERE id = %s"
    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (source_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def extract_content_html(page_html: str, selector: str | None) -> str:
    """
    Parse the page and return the inner HTML of the best content area.

    If a CSS selector is provided (e.g. '#content', 'main', '.sub_container')
    we try to use it.  If it doesn't match we fall back to the full body.
    Script, style, and noscript tags are always removed first.
    """
    soup = BeautifulSoup(page_html, "html.parser")

    # Remove noise tags that add no menu information.
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Try the stored selector first.
    if selector:
        element = soup.select_one(selector)
        if element:
            return str(element)
        else:
            logger.warning(f"Selector '{selector}' did not match — falling back to full body")

    # Fallback: return the whole body (or the full soup if there is no body tag).
    body = soup.find("body")
    return str(body) if body else str(soup)


def compute_sha256(text: str) -> str:
    # Return the SHA-256 hex digest of the text encoded in UTF-8.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Scrape one source
# ---------------------------------------------------------------------------

def scrape_source(
    http_client: httpx.Client,
    conn_str: str,
    source: dict,
    today: date,
) -> None:
    """
    Fetch one cafeteria page and store the result in scraped_html_snapshots.
    Skips the fetch entirely if a snapshot for today already exists.
    """
    source_id = source["id"]
    url = source["source_url"].strip()
    selector = source.get("content_div_selector")
    name = f"{source['university_name']} / {source.get('campus') or 'main'}"

    # --- Cache check: don't scrape the same page twice in one day --------
    if already_scraped_today(conn_str, source_id, today):
        logger.info(f"  Skipping {name} — already scraped today")
        return

    # --- Fetch ----------------------------------------------------------
    try:
        response = http_client.get(url)
        response.raise_for_status()
    except httpx.TimeoutException:
        logger.error(f"  Timeout fetching {name}: {url} — marking invalid")
        mark_source_invalid(conn_str, source_id)
        return
    except httpx.HTTPStatusError as error:
        logger.error(
            f"  HTTP {error.response.status_code} for {name}: {url} — marking invalid"
        )
        mark_source_invalid(conn_str, source_id)
        return
    except httpx.HTTPError as error:
        logger.error(f"  HTTP error for {name}: {url} | {error} — marking invalid")
        mark_source_invalid(conn_str, source_id)
        return

    # --- Extract content ------------------------------------------------
    html_raw = extract_content_html(response.text, selector)
    new_hash = compute_sha256(html_raw)

    # --- Compare with previous snapshot ---------------------------------
    last_snapshot = get_last_snapshot(conn_str, source_id)
    if last_snapshot and last_snapshot["html_sha256"] == new_hash:
        is_changed = False
        change_reason = "content unchanged since last snapshot"
    else:
        is_changed = True
        change_reason = "new content" if not last_snapshot else "content changed"

    # --- Build the snapshot row -----------------------------------------
    snapshot = {
        "source_id": source_id,
        "source_url": str(response.url),   # final URL after any redirects
        "scrape_date": today.isoformat(),
        "status_code": response.status_code,
        "final_url": str(response.url),
        "content_type": response.headers.get("content-type"),
        "etag": response.headers.get("etag"),
        "last_modified": response.headers.get("last-modified"),
        "html_raw": html_raw,
        "html_sha256": new_hash,
        "is_changed": is_changed,
        "previous_snapshot_id": last_snapshot["id"] if last_snapshot else None,
        "change_reason": change_reason,
    }

    # --- Save -----------------------------------------------------------
    save_snapshot(conn_str, snapshot)
    mark_source_scraped(conn_str, source_id)

    logger.info(
        f"  Saved snapshot for {name} | "
        f"changed={is_changed} | "
        f"selector='{selector or 'none'}' | "
        f"size={len(html_raw)} bytes"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log_path = setup_logging()
    logger.info(f"Logging to {log_path}")

    conn_str = get_connection_string()
    sources = load_valid_sources(conn_str)
    logger.info(f"Loaded {len(sources)} valid sources from Supabase")

    today = date.today()
    headers = {"User-Agent": USER_AGENT}

    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as http_client:
        total = len(sources)

        for index, source in enumerate(sources, start=1):
            name = f"{source['university_name']} / {source.get('campus') or 'main'}"
            logger.info(f"[{index}/{total}] {name}")

            try:
                scrape_source(http_client, conn_str, source, today)
            except Exception as error:
                # Catch unexpected errors so one bad page doesn't stop the whole run.
                logger.exception(f"  Unexpected error for {name}: {error}")

    logger.info("Scraping finished.")


if __name__ == "__main__":
    main()