"""
getmenus.py
-----------
Provides the get_menu LangChain tool and a few small helper functions
that the notebook agent uses to query campus menu items from the database.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import psycopg.rows
from langchain_core.tools import tool
from loguru import logger

# The timezone used for deciding "today's" date for menus
MENU_TIMEZONE = os.getenv("MENU_TIMEZONE", "Asia/Seoul")


def setup_logging() -> None:
    """
    Set up loguru to log to both the console and a daily log file in logs/.
    Call this once at the start of your notebook or script.
    """
    # Create the logs directory if it doesn't already exist
    log_dir = (Path.cwd() / "logs").resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    # Name the log file after today's date so each day gets its own file
    log_path = log_dir / f"menu_agent_{get_today()}.log"

    # Remove the default loguru handler, then add our own
    logger.remove()
    logger.add(sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO"))
    logger.add(
        log_path,
        level="DEBUG",
        encoding="utf-8",
        enqueue=True,   # thread-safe writes
        backtrace=True,
        diagnose=False,
    )
    logger.info("Logging to {}", log_path)


def get_today() -> str:
    """Return today's date as a YYYY-MM-DD string in the menu timezone."""
    return datetime.now(ZoneInfo(MENU_TIMEZONE)).date().isoformat()


def get_connection_string() -> str:
    """
    Read the database connection string from environment variables.
    Tries SUPABASE_CONNECTION_STRING first, then DATABASE_URL.
    Raises an error if neither is set.
    """
    conn = os.environ.get("SUPABASE_CONNECTION_STRING") or os.environ.get("DATABASE_URL")
    if not conn:
        raise ValueError(
            "Please set SUPABASE_CONNECTION_STRING or DATABASE_URL in your .env file."
        )
    return conn


@tool
def get_menu(menu_date: str) -> str:
    """
    Fetch all valid campus menu items for a given date (YYYY-MM-DD) from the database.

    Returns a JSON array of rows. Each row has these fields:
    - university: name of the university
    - campus: campus name
    - restaurant_name: name of the cafeteria or restaurant
    - meal_type: e.g. breakfast, lunch, dinner
    - meal_name: name of the dish
    - price_krw: price in Korean Won
    - serving_time: time the meal is served
    - source_url: where the menu was scraped from

    Example: get_menu("2026-06-04")
    """
    # Build the SQL query to fetch valid menu items for the requested date
    query = """
        SELECT university, campus, restaurant_name, meal_type,
               meal_name, price_krw, serving_time, source_url
        FROM public.campus_menu_items
        WHERE is_valid_menu = true
          AND menu_date = %s
        ORDER BY university, restaurant_name, meal_type, meal_name
    """

    logger.debug("get_menu called for date={}", menu_date)

    # Connect to the database and run the query
    with psycopg.connect(get_connection_string()) as conn:
        with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(query, [menu_date])
            rows = cur.fetchall()

    logger.info("get_menu returned {} row(s) for {}", len(rows), menu_date)

    # Return the results as a JSON string so the agent can read them
    return json.dumps(rows, ensure_ascii=False, default=str)
