import json
import os
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from loguru import logger

# Load environment variables so this module also works in scripts and schedulers.
load_dotenv()


def _configure_logger() -> None:
    """Configure Loguru to write both console and file logs."""
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        logs_dir / "exchange_rates.log",
        rotation="1 MB",
        retention="14 days",
        level="INFO",
        enqueue=True,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
    )
    logger.add(
        lambda message: print(message, end=""),
        level="INFO",
        format="{time:HH:mm:ss} | {level} | {message}",
    )


def _fetch_exchange_rate_payload() -> dict:
    """Fetch raw exchange-rate API payload for KRW base."""
    logger.info("Fetching latest KRW exchange-rate payload")

    # Read the API key from the .env file.
    api_key = os.getenv("EXCHANGE_RATE_API_KEY")
    if not api_key:
        logger.error("Missing EXCHANGE_RATE_API_KEY in .env")
        raise RuntimeError("Missing EXCHANGE_RATE_API_KEY in .env")

    # Ask the exchange-rate service for all rates using KRW as the base currency.
    url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/KRW"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; hai5016-project/1.0)"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as error:
        logger.exception("Failed while calling exchange-rate API")
        raise RuntimeError(f"Failed to fetch exchange rates: {error}") from error

    # Surface API-level failures clearly.
    if data.get("result") != "success":
        error_type = data.get("error-type", "unknown_error")
        logger.error(f"ExchangeRate API returned failure: {error_type}")
        raise RuntimeError(f"ExchangeRate API error: {error_type}")

    logger.info("Exchange-rate payload fetched successfully")

    return data


def get_krw_conversions() -> dict[str, float]:
    """Return KRW conversion rates, using today's DB cache before scraping."""
    cached_rates = _get_cached_krw_conversions_from_supabase()
    if cached_rates:
        logger.info(f"Using cached FX rates from Supabase ({len(cached_rates)} rows)")
        return cached_rates

    logger.info("No daily FX cache found in Supabase, fetching fresh rates")
    data = _fetch_exchange_rate_payload()

    rates = data.get("conversion_rates", {})

    if not rates:
        raise RuntimeError("ExchangeRate API returned no conversion rates")

    # Save fresh scrape so future calls can read from cache.
    records = _build_daily_fx_records(data)
    _save_rows_with_available_supabase_method(records)

    return {code: float(value) for code, value in rates.items()}


def save_daily_rates_to_supabase() -> int:
    """Fetch KRW rates and upsert one Supabase row per quote currency for today."""
    logger.info("Preparing daily FX upsert")

    data = _fetch_exchange_rate_payload()
    records = _build_daily_fx_records(data)
    logger.info(f"Built {len(records)} FX rows for upsert")
    return _save_rows_with_available_supabase_method(records)


def _build_daily_fx_records(data: dict) -> list[dict]:
    """Convert API payload to one upsert-ready row per currency pair."""
    rates = data.get("conversion_rates", {})
    base_code = str(data.get("base_code", "KRW")).upper()

    if not rates:
        logger.error("ExchangeRate API returned no conversion rates")
        raise RuntimeError("ExchangeRate API returned no conversion rates")

    now_utc = datetime.now(timezone.utc)
    cache_date = now_utc.date().isoformat()
    fetched_at = now_utc.isoformat()

    records = []
    for quote_code, rate in rates.items():
        records.append(
            {
                "provider": "exchangerate-api",
                "base_code": base_code,
                "quote_code": str(quote_code).upper(),
                "rate": float(rate),
                "cache_date": cache_date,
                "fetched_at": fetched_at,
                "raw_response": data,
            }
        )

    return records


def _save_rows_with_available_supabase_method(records: list[dict]) -> int:
    """Save rows using available Supabase credentials from .env."""
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    supabase_connection_string = os.getenv("SUPABASE_CONNECTION_STRING")

    if supabase_url and supabase_key:
        logger.info("Using Supabase PostgREST upsert path")
        return _save_rows_via_postgrest(
            records=records,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
        )

    if supabase_connection_string:
        logger.info("Using Supabase PostgreSQL upsert path")
        return _save_rows_via_postgres(
            records=records,
            connection_string=supabase_connection_string,
        )

    raise RuntimeError(
        "Missing Supabase credentials. Set either SUPABASE_URL + SUPABASE_KEY "
        "or SUPABASE_CONNECTION_STRING in .env"
    )


def _get_cached_krw_conversions_from_supabase() -> dict[str, float]:
    """Read today's KRW rates from Supabase cache, if available."""
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    supabase_connection_string = os.getenv("SUPABASE_CONNECTION_STRING")
    today = datetime.now(timezone.utc).date().isoformat()

    if supabase_url and supabase_key:
        return _get_cached_rates_via_postgrest(
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            cache_date=today,
        )

    if supabase_connection_string:
        return _get_cached_rates_via_postgres(
            connection_string=supabase_connection_string,
            cache_date=today,
        )

    logger.warning("No Supabase credentials found for cache lookup")
    return {}


def _get_cached_rates_via_postgrest(
    supabase_url: str,
    supabase_key: str,
    cache_date: str,
) -> dict[str, float]:
    """Read cached rates using Supabase PostgREST API."""
    logger.info("Checking Supabase cache via PostgREST")

    query = urllib.parse.urlencode(
        {
            "select": "quote_code,rate",
            "provider": "eq.exchangerate-api",
            "base_code": "eq.KRW",
            "cache_date": f"eq.{cache_date}",
        }
    )
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/fx_rates_daily_cache?{query}"
    request = urllib.request.Request(
        endpoint,
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        logger.error(f"PostgREST cache lookup failed with HTTP {error.code}: {error_body}")
        return {}
    except urllib.error.URLError:
        logger.exception("Failed to connect to Supabase PostgREST cache endpoint")
        return {}

    cached_rates = {
        str(row["quote_code"]).upper(): float(row["rate"])
        for row in rows
        if row.get("quote_code") and row.get("rate") is not None
    }
    return cached_rates


def _get_cached_rates_via_postgres(connection_string: str, cache_date: str) -> dict[str, float]:
    """Read cached rates using direct PostgreSQL connection."""
    import psycopg

    logger.info("Checking Supabase cache via PostgreSQL")
    sql = """
    SELECT quote_code, rate
    FROM public.fx_rates_daily_cache
    WHERE provider = 'exchangerate-api'
      AND base_code = 'KRW'
      AND cache_date = %s
    """

    try:
        with psycopg.connect(connection_string) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, (cache_date,))
                rows = cursor.fetchall()
    except Exception:
        logger.exception("PostgreSQL cache lookup failed")
        return {}

    cached_rates = {str(quote_code).upper(): float(rate) for quote_code, rate in rows}
    return cached_rates


def _save_rows_via_postgrest(
    records: list[dict],
    supabase_url: str,
    supabase_key: str,
) -> int:
    """Upsert rows using Supabase PostgREST API."""
    logger.info("Sending PostgREST upsert request")

    endpoint = (
        f"{supabase_url.rstrip('/')}/rest/v1/fx_rates_daily_cache"
        "?on_conflict=provider,base_code,quote_code,cache_date"
    )
    body = json.dumps(records).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        logger.error(f"PostgREST upsert failed with HTTP {error.code}: {error_body}")
        raise RuntimeError(
            f"Failed to upsert FX rows into Supabase: HTTP {error.code} - {error_body}"
        ) from error
    except urllib.error.URLError as error:
        logger.exception("Failed to connect to Supabase PostgREST endpoint")
        raise RuntimeError(f"Failed to connect to Supabase: {error}") from error

    logger.info(f"PostgREST upsert completed for {len(records)} rows")

    return len(records)


def _save_rows_via_postgres(records: list[dict], connection_string: str) -> int:
    """Upsert rows using direct PostgreSQL connection."""
    import psycopg
    from psycopg.types.json import Jsonb

    logger.info("Starting PostgreSQL upsert transaction")

    sql = """
    INSERT INTO public.fx_rates_daily_cache (
        provider,
        base_code,
        quote_code,
        rate,
        cache_date,
        fetched_at,
        raw_response
    )
    VALUES (
        %(provider)s,
        %(base_code)s,
        %(quote_code)s,
        %(rate)s,
        %(cache_date)s,
        %(fetched_at)s,
        %(raw_response)s::jsonb
    )
    ON CONFLICT (provider, base_code, quote_code, cache_date)
    DO UPDATE SET
        rate = EXCLUDED.rate,
        fetched_at = EXCLUDED.fetched_at,
        raw_response = EXCLUDED.raw_response,
        updated_at = now()
    """

    payload_rows = []
    for row in records:
        payload_rows.append(
            {
                "provider": row["provider"],
                "base_code": row["base_code"],
                "quote_code": row["quote_code"],
                "rate": row["rate"],
                "cache_date": row["cache_date"],
                "fetched_at": row["fetched_at"],
                "raw_response": Jsonb(row["raw_response"]),
            }
        )

    try:
        with psycopg.connect(connection_string) as connection:
            with connection.cursor() as cursor:
                cursor.executemany(sql, payload_rows)
            connection.commit()
    except Exception as error:
        logger.exception("PostgreSQL upsert failed")
        raise RuntimeError(f"Failed to upsert FX rows into Supabase PostgreSQL: {error}") from error

    logger.info(f"PostgreSQL upsert completed for {len(records)} rows")

    return len(records)


def get_fx(currency: str) -> float:
    """Return the conversion rate from KRW to the requested currency code."""
    rates = get_krw_conversions()
    currency_code = currency.upper()

    if currency_code not in rates:
        raise ValueError(f"Currency code not found: {currency_code}")

    return float(rates[currency_code])


if __name__ == "__main__":
    _configure_logger()
    logger.info("exchange_rates.py started")
    upserted_rows = save_daily_rates_to_supabase()
    logger.info(f"Saved {upserted_rows} FX rows to Supabase.")
