import os
import json
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger
import pandas as pd
import httpx
from bs4 import BeautifulSoup
from openai import OpenAI

# Load environment variables
load_dotenv()

# Setup logging
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / f"scrape_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
logger.add(log_file, level="INFO", format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")
logger.add(lambda msg: print(msg, end=""), colorize=True, format="<level>{message}</level>")

# Setup results directory
results_dir = Path("results")
results_dir.mkdir(exist_ok=True)

# Initialize OpenAI client
client = OpenAI(
    base_url=os.getenv("AZURE_FOUNDRY_ENDPOINT"),
    api_key=os.getenv("AZURE_FOUNDRY_API_KEY"),
)

MODEL = os.getenv("AZURE_FOUNDRY_MODEL")

# User agent to avoid being blocked
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}

def get_readable_text(html: str) -> str:
    """Extract readable text from HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, 'html.parser')
    
    # Remove script and style elements
    for script in soup(["script", "style"]):
        script.decompose()
    
    # Get text
    text = soup.get_text()
    
    # Break into lines and remove leading and trailing space on each
    lines = (line.strip() for line in text.splitlines())
    
    # Break multi-headlines into a line each
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    
    # Drop blank lines
    text = '\n'.join(chunk for chunk in chunks if chunk)
    
    return text

def extract_menu_info(text: str, url: str, university: str) -> list[dict]:
    """Send text to AI model and extract menu information."""
    try:
        prompt = f"""Extract menu information from the following text scraped from a restaurant website.

URL: {url}
University: {university}

Text:
{text[:3000]}  # Limit to first 3000 characters to avoid token limit issues

Extract menu items with the following JSON format. For each menu item:
- meal_type: "breakfast", "lunch", "dinner", or "unknown" (based on context, not speculation)
- meal_name: The name of the meal (only if it's clearly a meal name, not random text)
- price_krw: Price in Korean Won (empty string if unknown)
- scrape_date: Current date in YYYY-MM-DD HH:MM:SS format
- menu_date: Date of the menu (YYYY-MM-DD format, or empty if unknown)

Return ONLY valid, confident menu items. Skip items where:
- The meal name is unclear or seems like random text
- The item is not clearly a food item
- You're not confident in the meal name

Return a JSON array with this structure (empty array if no valid items found):
[
    {{
        "meal_type": "lunch",
        "meal_name": "김밥",
        "price_krw": "3000",
        "scrape_date": "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "menu_date": "{datetime.now().strftime('%Y-%m-%d')}"
    }}
]

Return ONLY the JSON array, no other text."""

        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        response_text = response.choices[0].message.content.strip()
        
        # Try to parse JSON from the response
        try:
            # Remove markdown code blocks if present
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
            
            items = json.loads(response_text)
            if not isinstance(items, list):
                items = [items]
            
            return items if items else []
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON response from AI model: {response_text[:200]}")
            return []
    
    except Exception as e:
        logger.error(f"Error calling AI model for URL {url}: {str(e)}")
        return []

async def fetch_and_process_url(session: httpx.AsyncClient, url: str, university: str) -> list[dict]:
    """Fetch URL, extract text, and process with AI."""
    try:
        logger.info(f"Fetching URL: {url}")
        
        response = await session.get(url, headers=HEADERS, timeout=5.0)
        response.raise_for_status()
        
        # Extract readable text
        text = get_readable_text(response.text)
        
        if not text or len(text.strip()) < 10:
            logger.warning(f"No readable text found for {url}")
            return []
        
        logger.info(f"Extracted text from {url}, sending to AI model...")
        
        # Extract menu info using AI
        menu_items = extract_menu_info(text, url, university)
        
        # Add URL and university to each item
        for item in menu_items:
            item["url"] = url
            item["university"] = university
        
        logger.info(f"Extracted {len(menu_items)} menu items from {url}")
        return menu_items
    
    except asyncio.TimeoutError:
        logger.error(f"Timeout (>5s) fetching {url}")
        return []
    except httpx.HTTPError as e:
        logger.error(f"HTTP error fetching {url}: {str(e)}")
        return []
    except Exception as e:
        logger.error(f"Error processing {url}: {str(e)}")
        return []

async def main():
    """Main scraping function."""
    try:
        logger.info("Starting menu scraping process...")
        
        # Read Excel file
        logger.info("Reading Excel file...")
        df = pd.read_excel("campus_restaurant_websites.xlsx", header=2)
        
        # Filter out rows with empty URLs
        df = df[df["Url"].notna()]
        df = df[df["University"].notna()]
        
        logger.info(f"Found {len(df)} URLs to process")
        
        # Output file
        output_date = datetime.now().strftime("%Y-%m-%d")
        output_file = results_dir / f"menus-{output_date}.jsonl"
        
        # Process URLs asynchronously
        all_items = []
        
        async with httpx.AsyncClient() as session:
            tasks = [
                fetch_and_process_url(session, row["Url"], row["University"])
                for _, row in df.iterrows()
            ]
            
            # Run tasks with limited concurrency to avoid overwhelming the server
            semaphore = asyncio.Semaphore(3)
            
            async def bounded_task(task):
                async with semaphore:
                    return await task
            
            results = await asyncio.gather(
                *[bounded_task(task) for task in tasks],
                return_exceptions=True
            )
            
            # Flatten results
            for result in results:
                if isinstance(result, list):
                    all_items.extend(result)
                elif isinstance(result, Exception):
                    logger.error(f"Task failed with exception: {result}")
        
        # Write results to JSONL file
        logger.info(f"Writing {len(all_items)} items to {output_file}")
        with open(output_file, "w", encoding="utf-8") as f:
            for item in all_items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        
        logger.info(f"Scraping complete. Results saved to {output_file}")
        
    except FileNotFoundError:
        logger.error("Excel file not found: campus_restaurant_websites.xlsx")
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")

if __name__ == "__main__":
    asyncio.run(main())
