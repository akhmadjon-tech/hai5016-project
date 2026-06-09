#!/usr/bin/env python3
"""
Extract campus menu rows from Supabase HTML snapshots using Azure GPT-5 mini.

The script intentionally uses only the Python standard library so it can run
from cron without a dependency install step.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - handled with a clear runtime error
    BeautifulSoup = None

try:
    from dotenv import load_dotenv as load_dotenv_package
except ImportError:  # pragma: no cover - fallback for bare Python smoke tests
    load_dotenv_package = None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - handled with a clear runtime error
    psycopg = None
    dict_row = None


REQUEST_TIMEOUT_SECONDS = 90


REQUIRED_KEYS = {
    "campus",
    "menu_date",
    "meal_type",
    "meal_name",
    "price_krw",
    "raw_text",
    "restaurant_name",
    "serving_time",
    "snapshot_id",
    "source_id",
    "source_url",
    "is_valid_menu",
    "name_confidence",
    "university",
}

MEAL_TYPES = {"breakfast", "lunch", "dinner", "unknown"}
KOREAN_WEEKDAYS = {
    "월요일": 0,
    "화요일": 1,
    "수요일": 2,
    "목요일": 3,
    "금요일": 4,
    "토요일": 5,
    "일요일": 6,
}

SYSTEM_PROMPT = """You extract structured cafeteria menu items from Korean university HTML snapshots.

Return only JSON matching the provided schema. Each item must contain exactly these keys:
menu_date, meal_type, meal_name, price_krw, is_valid_menu, name_confidence, university, campus, restaurant_name, source_id, source_url, snapshot_id, raw_text, serving_time.

Field rules:
- menu_date: string in YYYY-MM-DD when available, else "".
- meal_type: one of breakfast, lunch, dinner, unknown.
- meal_name: food name only. Remove prices, calories, likes, labels, dates, times, "menu", "notice", "closed", "holiday", "operating hours", "location", and similar non-food text.
- price_krw: number-like string without commas, such as "5000", or "" when missing. Convert "4,800원", "4800 won", and "(1,000원)" to digits only.
- serving_time: meal serving/open time string when available, such as "11:30~14:00", else "".
- restaurant_name: restaurant/cafeteria/location/corner name when available, else "".
- campus: campus or campus-like location from SOURCE_METADATA. Do not invent or translate it.
- source_id: exact source_id from SOURCE_METADATA.
- source_url: exact source_url from SOURCE_METADATA.
- snapshot_id: exact snapshot_id from SOURCE_METADATA.
- raw_text: short source evidence for this item, copied or lightly cleaned from the menu block only. Keep it under 240 characters.
- is_valid_menu: true only when meal_name is clearly a real meal item or a real menu set.
- name_confidence: number between 0 and 1 for confidence that meal_name is the correct food item.
- university: the university name from SOURCE_METADATA. Do not invent or translate it.

Extraction rules:
- Skip navigation, headers, footers, notices, locations, phone numbers, operating hours, allergen/origin statements, buttons, likes, image filenames, and random words.
- Skip closed/holiday/no-menu placeholders, including 휴무, 대체공휴일, 운영없음, 미운영, 등록된 메뉴가 없습니다, and similar text.
- Extract menu items, not side-dish noise, when the HTML has a clear main item element such as caf_main, title, category menu card, or a day/meal cell. If a whole set meal is written as one line and no main item is marked, keep the meaningful set line as one meal_name rather than splitting into every side dish.
- Treat [caf_main] and [title] markers as stronger evidence for the primary meal name. Never extract text under [caf_menu_sides_do_not_extract] as separate rows unless it contains separate explicit prices; that section is usually soup, rice, kimchi, salad, calories, or other sides.
- For weekly tables, use the date from the column/header nearest the menu cell. If only a scrape_date is available and no menu date is visible, use scrape_date.
- Infer meal_type from Korean or English labels: 조식/아침=breakfast, 중식/점심/lunch=lunch, 석식/저녁/dinner=dinner. Snacks, ramen, cup rice, food court, or unclear sections should be unknown unless a breakfast/lunch/dinner label is present.
- If unsure whether text is a real food item, include it only with is_valid_menu false and name_confidence <= 0.4. Prefer omitting obvious non-menu text entirely.
- Do not include duplicate rows within the same chunk.
- Do not include any keys other than the required keys inside each item."""

USER_PROMPT_TEMPLATE = """SOURCE_METADATA:
university: {university}
campus: {campus}
source_id: {source_id}
snapshot_id: {snapshot_id}
source_url: {source_url}
scrape_date: {scrape_date}
scraped_at: {scraped_at}
content_div_selector: {content_div_selector}
chunk: {chunk_index}/{chunk_count}

HTML_TEXT:
{html_text}
"""


JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "campus_menu_extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "campus": {"type": "string"},
                            "menu_date": {"type": "string"},
                            "meal_type": {
                                "type": "string",
                                "enum": ["breakfast", "lunch", "dinner", "unknown"],
                            },
                            "meal_name": {"type": "string"},
                            "price_krw": {"type": "string"},
                            "raw_text": {"type": "string"},
                            "restaurant_name": {"type": "string"},
                            "serving_time": {"type": "string"},
                            "snapshot_id": {"type": "string"},
                            "source_id": {"type": "string"},
                            "source_url": {"type": "string"},
                            "is_valid_menu": {"type": "boolean"},
                            "name_confidence": {"type": "number"},
                            "university": {"type": "string"},
                        },
                        "required": sorted(REQUIRED_KEYS),
                    },
                }
            },
            "required": ["items"],
        },
    },
}


def load_dotenv(path: Path) -> None:
    if load_dotenv_package is not None:
        load_dotenv_package(path, override=False)
        return
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def setup_logging(log_dir: Path) -> Path:
    if not log_dir.is_absolute():
        log_dir = (Path.cwd() / log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"extract_menu_items_{dt.date.today().isoformat()}.log"
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.add(
        log_path,
        level=os.environ.get("LOG_LEVEL", "INFO"),
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
    return log_path


def request_json(url: str, headers: dict[str, str], payload: Any | None = None) -> Any:
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
        headers = {**headers, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1200]}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Request failed for {url}: {exc}") from exc


def fetch_snapshots_from_connection_string(
    run_date: str,
    limit: int | None,
    valid_sources_only: bool = False,
) -> list[dict[str, Any]]:
    connection_string = os.environ.get("SUPABASE_CONNECTION_STRING", "")
    if not connection_string:
        return []
    if psycopg is None or dict_row is None:
        raise RuntimeError(
            "SUPABASE_CONNECTION_STRING is set, but psycopg is not installed. "
            "Run: env UV_CACHE_DIR=.uv-cache uv sync"
        )

    sql = """
        select
            x.id,
            x.source_id,
            x.source_url,
            x.scrape_date::text as scrape_date,
            x.scraped_at::text as scraped_at,
            x.html_raw,
            coalesce(s.university_name, '') as university_name,
            coalesce(s.campus, '') as campus,
            coalesce(s.content_div_selector, '') as content_div_selector,
            coalesce(s.valid, true) as source_valid
        from public.scraped_html_snapshots x
        left join public.campus_menu_sources s on s.id = x.source_id
        where x.scrape_date = %s
    """
    params: list[Any] = [run_date]
    if valid_sources_only:
        sql += "\nand coalesce(s.valid, true) = true"
    sql += "\norder by s.university_name nulls last, s.campus nulls last, x.source_url"
    if limit:
        sql += "\nlimit %s"
        params.append(limit)

    with psycopg.connect(connection_string, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def fetch_latest_scrape_date() -> str:
    """Return the most recent scrape_date present in scraped_html_snapshots."""
    connection_string = os.environ.get("SUPABASE_CONNECTION_STRING", "")
    if connection_string:
        if psycopg is None:
            raise RuntimeError(
                "SUPABASE_CONNECTION_STRING is set, but psycopg is not installed. "
                "Run: env UV_CACHE_DIR=.uv-cache uv sync"
            )
        with psycopg.connect(connection_string) as conn:
            with conn.cursor() as cur:
                cur.execute("select max(scrape_date)::text from public.scraped_html_snapshots")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
        raise RuntimeError("No scrape_date found in scraped_html_snapshots.")

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "Missing SUPABASE_CONNECTION_STRING, or SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY/SUPABASE_ANON_KEY. "
            "Use --input-json for local testing."
        )
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }
    params = {"select": "scrape_date", "order": "scrape_date.desc", "limit": "1"}
    url = f"{supabase_url}/rest/v1/scraped_html_snapshots?{urllib.parse.urlencode(params)}"
    rows = request_json(url, headers)
    if rows and rows[0].get("scrape_date"):
        return rows[0]["scrape_date"]
    raise RuntimeError("No scrape_date found in scraped_html_snapshots.")


def fetch_snapshots_from_supabase(
    run_date: str,
    limit: int | None,
    valid_sources_only: bool = False,
) -> list[dict[str, Any]]:
    connection_rows = fetch_snapshots_from_connection_string(run_date, limit, valid_sources_only)
    if connection_rows:
        return connection_rows

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError(
            "Missing SUPABASE_CONNECTION_STRING, or SUPABASE_URL plus SUPABASE_SERVICE_ROLE_KEY/SUPABASE_ANON_KEY. "
            "Use --input-json for local testing."
        )

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }
    select = "id,source_id,source_url,scrape_date,scraped_at,html_raw"
    params = {"select": select, "order": "scrape_date.desc,source_url.asc"}
    params["scrape_date"] = f"eq.{run_date}"
    if limit:
        params["limit"] = str(limit)
    snapshots_url = f"{supabase_url}/rest/v1/scraped_html_snapshots?{urllib.parse.urlencode(params)}"
    snapshots = request_json(snapshots_url, headers)

    source_ids = sorted({row.get("source_id") for row in snapshots if row.get("source_id")})
    sources_by_id: dict[str, dict[str, Any]] = {}
    if source_ids:
        quoted_ids = ",".join(str(x) for x in source_ids)
        source_params = {
            "select": "id,university_name,campus,source_url,content_div_selector,valid",
            "id": f"in.({quoted_ids})",
        }
        sources_url = f"{supabase_url}/rest/v1/campus_menu_sources?{urllib.parse.urlencode(source_params)}"
        sources = request_json(sources_url, headers)
        sources_by_id = {row["id"]: row for row in sources}

    for row in snapshots:
        source = sources_by_id.get(row.get("source_id"), {})
        row["university_name"] = source.get("university_name", "")
        row["campus"] = source.get("campus", "")
        row["content_div_selector"] = source.get("content_div_selector", "")
        row["source_valid"] = source.get("valid", True)
    if valid_sources_only:
        snapshots = [row for row in snapshots if row.get("source_valid") is not False]
    return snapshots


def select_relevant_html(raw_html: str, selector: str | None) -> tuple[str, dict[str, Any]]:
    raw_html = raw_html or ""
    selector = (selector or "").strip()
    stats = {
        "selector": selector,
        "selector_used": False,
        "selector_matched": False,
        "selector_match_count": 0,
        "raw_chars": len(raw_html),
        "selected_chars": len(raw_html),
        "reduction_ratio": 0.0,
    }
    if not selector:
        return raw_html, stats
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 is not installed. Run: env UV_CACHE_DIR=.uv-cache uv sync")

    soup = BeautifulSoup(raw_html, "html.parser")
    try:
        matches = soup.select(selector)
    except Exception:
        return raw_html, stats
    stats["selector_used"] = True
    stats["selector_match_count"] = len(matches)
    if not matches:
        return raw_html, stats

    selected_html = "\n".join(str(match) for match in matches)
    stats["selector_matched"] = True
    stats["selected_chars"] = len(selected_html)
    if raw_html:
        stats["reduction_ratio"] = round(1 - (len(selected_html) / len(raw_html)), 4)
    return selected_html, stats


def html_to_model_text(raw_html: str, max_chars: int = 60000) -> str:
    text = raw_html or ""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    marker_classes = {
        "caf_main": "caf_main",
        "caf_menu": "caf_menu_sides_do_not_extract",
        "card-title": "card-title",
        "daily-menu": "daily-menu",
        "category": "category",
        "title": "title",
        "price": "price",
        "content-item": "content-item",
        "day-container": "day-container",
    }
    for class_name, marker_name in marker_classes.items():
        pattern = rf"(?i)<[^>]+class=[\"'][^\"']*\b{re.escape(class_name)}\b[^\"']*[\"'][^>]*>"
        text = re.sub(pattern, f"\n[{marker_name}]\n", text)
    text = re.sub(r"(?i)<\s*(br|/p|/div|/li|/tr|/td|/th|/h[1-6])\b[^>]*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:max_chars]


def chunk_text(text: str, chunk_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= chunk_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_chars)
        if end < len(text):
            split_at = text.rfind("\n\n", start, end)
            if split_at > start + chunk_chars // 2:
                end = split_at
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(0, end - overlap_chars)
    return [c for c in chunks if c]


def add_weekday_date_hints(text: str, scrape_date: str) -> str:
    scrape_date = normalize_date(scrape_date)
    if not scrape_date:
        return text
    base = dt.date.fromisoformat(scrape_date)
    monday = base - dt.timedelta(days=base.weekday())

    def replace(match: re.Match[str]) -> str:
        weekday = match.group(1)
        menu_text = match.group(2)
        date_hint = (monday + dt.timedelta(days=KOREAN_WEEKDAYS[weekday])).isoformat()
        return f"{date_hint} {weekday} : {menu_text}"

    return re.sub(
        r"\b(월요일|화요일|수요일|목요일|금요일|토요일|일요일)\s*[:：]\s*([^\n]+)",
        replace,
        text,
    )


def build_messages(snapshot: dict[str, Any], html_text: str, chunk_index: int, chunk_count: int) -> list[dict[str, str]]:
    user_prompt = USER_PROMPT_TEMPLATE.format(
        university=snapshot.get("university_name") or "",
        campus=snapshot.get("campus") or "",
        source_id=str(snapshot.get("source_id") or ""),
        snapshot_id=str(snapshot.get("id") or ""),
        source_url=snapshot.get("source_url") or "",
        scrape_date=snapshot.get("scrape_date") or "",
        scraped_at=snapshot.get("scraped_at") or "",
        content_div_selector=snapshot.get("content_div_selector") or "",
        chunk_index=chunk_index,
        chunk_count=chunk_count,
        html_text=html_text,
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def azure_chat_completion(messages: list[dict[str, str]], max_retries: int = 3) -> dict[str, Any]:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
    if endpoint.endswith("/openai/v1"):
        url = f"{endpoint}/chat/completions"
        model_field = {"model": deployment}
    else:
        url = (
            f"{endpoint}/openai/deployments/{urllib.parse.quote(deployment)}/chat/completions"
            f"?api-version={urllib.parse.quote(api_version)}"
        )
        model_field = {}
    payload = {
        **model_field,
        "messages": messages,
        "response_format": JSON_SCHEMA,
        "reasoning_effort": "minimal",
        "max_completion_tokens": 8192,
    }
    headers = {
        "api-key": api_key,
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    for attempt in range(1, max_retries + 1):
        try:
            return request_json(url, headers, payload)
        except RuntimeError:
            if attempt == max_retries:
                raise
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def parse_model_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    message = response["choices"][0].get("message", {})
    content = message.get("content") or ""
    if not content.strip():
        raise RuntimeError(f"Azure returned an empty message: {json.dumps(response, ensure_ascii=False)[:1500]}")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Azure returned non-JSON content: {content[:1500]}") from exc
    if isinstance(parsed, list):
        return parsed
    return parsed.get("items", [])


def normalize_price(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    digits = re.sub(r"\D+", "", text)
    return digits


def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.search(r"(20\d{2})[./](\d{1,2})[./](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def derive_weekday_date(weekday_label: str, scrape_date: str) -> str:
    weekday_index = KOREAN_WEEKDAYS.get(weekday_label)
    scrape_date = normalize_date(scrape_date)
    if weekday_index is None or not scrape_date:
        return ""
    base = dt.date.fromisoformat(scrape_date)
    monday = base - dt.timedelta(days=base.weekday())
    return (monday + dt.timedelta(days=weekday_index)).isoformat()


def clean_meal_name_and_date(name: str, scrape_date: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", name or "").strip()
    derived_date = ""

    weekday_match = re.match(r"^(월요일|화요일|수요일|목요일|금요일|토요일|일요일)\s*[:：-]\s*(.+)$", text)
    if weekday_match:
        derived_date = derive_weekday_date(weekday_match.group(1), scrape_date)
        text = weekday_match.group(2).strip()

    text = re.sub(r"^(조식|아침|중식|점심|석식|저녁)\s*[-:：]\s*", "", text).strip()
    return text, derived_date


def validate_item(item: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    row = {key: item.get(key, "") for key in REQUIRED_KEYS}
    fallback_university = snapshot.get("university_name") or ""
    scrape_date = snapshot.get("scrape_date") or ""
    row["meal_name"], derived_date = clean_meal_name_and_date(str(row["meal_name"] or ""), scrape_date)
    row["menu_date"] = normalize_date(row["menu_date"]) or derived_date or normalize_date(scrape_date)
    row["campus"] = str(row.get("campus") or snapshot.get("campus") or "").strip()
    row["source_id"] = str(snapshot.get("source_id") or row.get("source_id") or "").strip()
    row["source_url"] = str(snapshot.get("source_url") or row.get("source_url") or "").strip()
    row["snapshot_id"] = str(snapshot.get("id") or row.get("snapshot_id") or "").strip()
    row["restaurant_name"] = str(row.get("restaurant_name") or "").strip()
    row["serving_time"] = re.sub(r"\s+", " ", str(row.get("serving_time") or "")).strip()
    row["raw_text"] = re.sub(r"\s+", " ", str(row.get("raw_text") or row["meal_name"] or "")).strip()[:240]
    row["meal_type"] = str(row["meal_type"] or "unknown").lower()
    if row["meal_type"] not in MEAL_TYPES:
        row["meal_type"] = "unknown"
    row["price_krw"] = normalize_price(row["price_krw"])
    row["is_valid_menu"] = bool(row["is_valid_menu"])
    try:
        confidence = float(row["name_confidence"])
    except (TypeError, ValueError):
        confidence = 0.0
    row["name_confidence"] = max(0.0, min(1.0, round(confidence, 3)))
    row["university"] = str(row["university"] or fallback_university or "").strip()
    if not row["campus"] and snapshot.get("campus"):
        row["campus"] = str(snapshot.get("campus") or "").strip()
    if not row["restaurant_name"]:
        row["restaurant_name"] = row["campus"] or row["university"]

    if not row["meal_name"]:
        return None
    lowered = row["meal_name"].lower()
    banned = [
        "home",
        "login",
        "notice",
        "operating hours",
        "location",
        "closed",
        "휴무",
        "대체공휴일",
        "운영없음",
        "미운영",
        "등록된 메뉴",
    ]
    if any(term in lowered or term in row["meal_name"] for term in banned):
        return None
    if len(row["meal_name"]) <= 1:
        return None
    if not row["is_valid_menu"] and derived_date and row["meal_name"]:
        row["is_valid_menu"] = True
        row["name_confidence"] = max(row["name_confidence"], 0.7)
    return row


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        key = (
            row["university"],
            row["campus"],
            row["restaurant_name"],
            row["menu_date"],
            row["meal_type"],
            row["meal_name"],
            row["price_krw"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    unique.sort(key=lambda r: (r["university"], r["menu_date"], r["meal_type"], r["meal_name"]))
    return unique


def extract_snapshot(snapshot: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected_html, selector_stats = select_relevant_html(
        snapshot.get("html_raw") or "",
        snapshot.get("content_div_selector") or "",
    )
    source_text = html_to_model_text(selected_html, max_chars=args.max_snapshot_chars)
    source_text = add_weekday_date_hints(source_text, snapshot.get("scrape_date") or "")
    chunks = chunk_text(source_text, args.chunk_chars, args.overlap_chars)
    if args.dry_run:
        preview = build_messages(snapshot, chunks[0] if chunks else "", 1, len(chunks) or 1)[1]["content"]
        print(json.dumps({
            "snapshot_id": str(snapshot.get("id") or ""),
            "university": snapshot.get("university_name"),
            "selector_stats": selector_stats,
            "chunks": len(chunks),
            "first_user_prompt_preview": preview[:2500],
        }, ensure_ascii=False, indent=2))
        return []

    rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        messages = build_messages(snapshot, chunk, index, len(chunks))
        response = azure_chat_completion(messages)
        items = parse_model_items(response)
        for item in items:
            normalized = validate_item(item, snapshot)
            if normalized:
                rows.append(normalized)
    return rows


def inspect_selectors(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        selected_html, stats = select_relevant_html(
            snapshot.get("html_raw") or "",
            snapshot.get("content_div_selector") or "",
        )
        selected_text = html_to_model_text(selected_html, max_chars=200000)
        rows.append({
            "university": snapshot.get("university_name") or "",
            "campus": snapshot.get("campus") or "",
            "source_url": snapshot.get("source_url") or "",
            **stats,
            "selected_text_chars": len(selected_text),
            "chunk_estimate": len(chunk_text(selected_text, 12000, 1000)),
        })
    return rows


def summarize_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_university: dict[str, dict[str, Any]] = {}
    for row in rows:
        university = row["university"] or "(unknown)"
        entry = by_university.setdefault(
            university,
            {"rows": 0, "valid_rows": 0, "dates": set(), "meal_types": set()},
        )
        entry["rows"] += 1
        if row["is_valid_menu"]:
            entry["valid_rows"] += 1
        if row["menu_date"]:
            entry["dates"].add(row["menu_date"])
        entry["meal_types"].add(row["meal_type"])

    return {
        university: {
            "rows": entry["rows"],
            "valid_rows": entry["valid_rows"],
            "dates": sorted(entry["dates"]),
            "meal_types": sorted(entry["meal_types"]),
        }
        for university, entry in sorted(by_university.items())
    }


def save_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "snapshot_id",
            "source_id",
            "source_url",
            "university",
            "campus",
            "restaurant_name",
            "menu_date",
            "meal_type",
            "meal_name",
            "price_krw",
            "serving_time",
            "raw_text",
            "is_valid_menu",
            "name_confidence",
        ])
        writer.writeheader()
        writer.writerows(rows)


def save_outputs(rows: list[dict[str, Any]], output_dir: Path, output_prefix: str, save_json: bool = False) -> tuple[Path | None, Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = output_dir / f"{output_prefix}_menu_items_{timestamp}.csv"
    valid_csv_path = output_dir / f"{output_prefix}_valid_menu_items_{timestamp}.csv"
    coverage_path = output_dir / f"{output_prefix}_coverage_{timestamp}.json"
    json_path: Path | None = None
    if save_json:
        json_path = output_dir / f"{output_prefix}_menu_items_{timestamp}.json"
        json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage_path.write_text(json.dumps(summarize_coverage(rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save_csv(rows, csv_path)
    save_csv([row for row in rows if row["is_valid_menu"]], valid_csv_path)
    return json_path, csv_path, valid_csv_path, coverage_path


def upsert_menu_items(rows: list[dict[str, Any]], parser_run_id: str) -> int:
    connection_string = os.environ.get("SUPABASE_CONNECTION_STRING", "")
    if not connection_string:
        raise RuntimeError("SUPABASE_CONNECTION_STRING is required when using --save-to-db.")
    if psycopg is None:
        raise RuntimeError("psycopg is required when using --save-to-db. Run: env UV_CACHE_DIR=.uv-cache uv sync")

    parser_model = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    valid_rows = [row for row in rows if row["is_valid_menu"]]
    if not valid_rows:
        return 0

    payload = []
    for row in valid_rows:
        menu_date = normalize_date(row.get("menu_date"))
        payload.append((
            row["source_id"] or None,
            row["snapshot_id"] or None,
            row.get("source_url", ""),
            row["university"],
            row["campus"],
            row["restaurant_name"],
            dt.date.fromisoformat(menu_date) if menu_date else None,
            row["meal_type"],
            row["meal_name"],
            row["price_krw"],
            row["serving_time"],
            row["raw_text"],
            row["is_valid_menu"],
            row["name_confidence"],
            parser_model,
            parser_run_id,
        ))

    sql = """
        insert into public.campus_menu_items (
            source_id,
            snapshot_id,
            source_url,
            university,
            campus,
            restaurant_name,
            menu_date,
            meal_type,
            meal_name,
            price_krw,
            serving_time,
            raw_text,
            is_valid_menu,
            name_confidence,
            parser_model,
            parser_run_id
        )
        values (
            %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s
        )
        on conflict on constraint campus_menu_items_unique_menu
        do update set
            snapshot_id = excluded.snapshot_id,
            source_url = excluded.source_url,
            raw_text = excluded.raw_text,
            is_valid_menu = excluded.is_valid_menu,
            name_confidence = greatest(public.campus_menu_items.name_confidence, excluded.name_confidence),
            parser_model = excluded.parser_model,
            parser_run_id = excluded.parser_run_id,
            last_seen_at = now(),
            seen_count = public.campus_menu_items.seen_count + 1,
            updated_at = now()
    """

    with psycopg.connect(connection_string) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)
        conn.commit()
    return len(payload)


def load_input_json(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Input JSON must be a list of snapshot objects.")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract campus menu items from Supabase HTML snapshots.")
    parser.add_argument("--date", help="Only process snapshots with this scrape_date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, help="Limit number of snapshots fetched from Supabase.")
    parser.add_argument("--input-json", type=Path, help="Read snapshots from a local JSON file instead of Supabase.")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--output-prefix", help="Prefix for output files. Defaults to sample or supabase.")
    parser.add_argument("--dry-run", action="store_true", help="Prepare prompts but do not call Azure.")
    parser.add_argument("--inspect-selectors", action="store_true", help="Print selector match/reduction stats and exit.")
    parser.add_argument("--valid-sources-only", action="store_true", help="Skip sources where campus_menu_sources.valid is false.")
    parser.add_argument("--skip-db", action="store_true", help="Skip upserting valid menu items into public.campus_menu_items.")
    parser.add_argument("--save-json", action="store_true", help="Save full JSON output file in addition to CSV outputs.")
    parser.add_argument("--parser-run-id", help="Optional parser_run_id value for DB upserts.")
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="Directory for daily log files.")
    parser.add_argument("--max-run-seconds", type=int, default=7200, help="Stop gracefully after this many seconds and save partial results.")
    parser.add_argument("--chunk-chars", type=int, default=8000)
    parser.add_argument("--overlap-chars", type=int, default=1000)
    parser.add_argument("--max-snapshot-chars", type=int, default=60000)
    return parser.parse_args()


def main() -> int:
    global REQUEST_TIMEOUT_SECONDS
    args = parse_args()
    load_dotenv(Path(".env"))
    log_path = setup_logging(args.log_dir)
    REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", str(REQUEST_TIMEOUT_SECONDS)))
    logger.info(
        "starting extractor date={} input_json={} valid_sources_only={} skip_db={} save_json={} max_run_seconds={} log_path={}",
        args.date,
        str(args.input_json) if args.input_json else "",
        args.valid_sources_only,
        args.skip_db,
        args.save_json,
        args.max_run_seconds,
        str(log_path),
    )

    if args.input_json:
        snapshots = load_input_json(args.input_json)
        output_prefix = args.output_prefix or "sample"
    else:
        if not args.date:
            args.date = fetch_latest_scrape_date()
            logger.info("no --date provided; using latest scrape_date from database: {}", args.date)
        snapshots = fetch_snapshots_from_supabase(args.date, args.limit, args.valid_sources_only)
        output_prefix = args.output_prefix or "supabase"

    if args.inspect_selectors:
        logger.info("running selector inspection for {} snapshots", len(snapshots))
        print(json.dumps(inspect_selectors(snapshots), ensure_ascii=False, indent=2))
        return 0

    all_rows: list[dict[str, Any]] = []
    started = 0
    failed_snapshots = 0
    timed_out = False
    run_started_at = time.monotonic()
    for index, snapshot in enumerate(snapshots, start=1):
        elapsed = time.monotonic() - run_started_at
        if args.max_run_seconds and elapsed >= args.max_run_seconds:
            timed_out = True
            logger.warning(
                "stopping run after {:.1f}s because max_run_seconds={} was reached",
                elapsed,
                args.max_run_seconds,
            )
            break
        university = snapshot.get("university_name") or "(unknown university)"
        source_url = snapshot.get("source_url") or ""
        started += 1
        snapshot_started_at = time.monotonic()
        logger.info(
            "starting snapshot {}/{} university={} source_id={} source_url={}",
            index,
            len(snapshots),
            university,
            snapshot.get("source_id") or "",
            source_url,
        )
        print(f"[{index}/{len(snapshots)}] extracting {university} {snapshot.get('scrape_date') or ''}", file=sys.stderr)
        try:
            snapshot_rows = extract_snapshot(snapshot, args)
            all_rows.extend(snapshot_rows)
            logger.info(
                "finished snapshot {}/{} rows={} duration_seconds={:.1f}",
                index,
                len(snapshots),
                len(snapshot_rows),
                time.monotonic() - snapshot_started_at,
            )
        except Exception:
            failed_snapshots += 1
            logger.exception(
                "snapshot failed {}/{} university={} source_id={}",
                index,
                len(snapshots),
                university,
                snapshot.get("source_id") or "",
            )

    if args.dry_run:
        logger.info("dry run complete")
        return 0

    rows = dedupe_rows(all_rows)
    json_path, csv_path, valid_csv_path, coverage_path = save_outputs(rows, args.output_dir, output_prefix, save_json=args.save_json)
    valid_count = sum(1 for row in rows if row["is_valid_menu"])
    upserted_rows = 0
    if not args.skip_db:
        parser_run_id = args.parser_run_id or f"{output_prefix}:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        upserted_rows = upsert_menu_items(rows, parser_run_id)
    summary = {
        "snapshots_processed": started,
        "snapshots_total": len(snapshots),
        "failed_snapshots": failed_snapshots,
        "timed_out": timed_out,
        "rows": len(rows),
        "valid_rows": valid_count,
        "invalid_rows": len(rows) - valid_count,
        "universities": len(summarize_coverage(rows)),
        "db_upserted_valid_rows": upserted_rows,
        "json": str(json_path) if json_path else None,
        "csv": str(csv_path),
        "valid_csv": str(valid_csv_path),
        "coverage": str(coverage_path),
    }
    logger.info("run summary {}", json.dumps(summary, ensure_ascii=False))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
