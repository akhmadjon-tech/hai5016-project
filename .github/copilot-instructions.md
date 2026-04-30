# HAI5016 Project – GitHub Copilot Instructions

## Who are the students?
Students in this class are **not professional developers**. They have basic knowledge of:
- Python, JSON, Jupyter Notebook
- Visual Studio Code (on Windows)
- UV for package management (`uv add`, not `pip install`)
- Git basics

Keep all suggestions simple, readable, and well-commented. Avoid advanced patterns, abstractions, or "clever" code. If something can be done in 5 lines, don't do it in 2.

---

## Project Environment

- **Project root**: `~/Developer/hai5016-project`
- **OS**: Windows 11
- **Package manager**: UV only — always use `uv add <package>`, never `pip install`
- **Virtual environment**: `.venv` (managed by UV)

### ⚠️ Before running any terminal command:
Always check if the `.venv` is activated. If not, remind the student to activate it first:
```powershell
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
```
```bash
# macOS/Linux
source .venv/bin/activate
```

---

## Tech Stack (Stick to This)

| Purpose | Tool |
|---|---|
| Language | Python 3.11+ |
| IDE | Visual Studio Code |
| Notebooks | Jupyter Notebook |
| Package manager | UV |
| Version control | Git |
| Web scraping | `httpx`, `BeautifulSoup4` |
| Data manipulation | `pandas` |
| LLM APIs | OpenAI-compatible (Azure OpenAI credit) |
| LLM orchestration | LangChain preferred for open-source and simplicity  |
| Database | Supabase or any other PostgreSQL-based solution |
| Frontend / UI | Streamlit suggested |
| Deployment | Not yet determined but there is Azure and Google Cloud student credit  |

Do **not** suggest alternatives to these unless the student explicitly asks.

---

## Project Overview

This is a **cheap meals finder app** for foreign students in Seoul. The app:

1. **Scrapes** menu pages from university cafeterias and local markets
2. **Extracts** structured data (meal name, price, location, hours) using LLM APIs
3. **Stores** data in Supabase (SQL for structured data, pgvector for semantic search)
4. **Serves** an UI via Streamlit where students can ask natural-language queries like:
   - *"What are cheap vegetarian meals near my campus?"*
   - *"Is today a good day to try bibimbap?"*
5. **Enriches** responses ideas:
   - Price in KRW and USD
   - Weather-based meal suggestions (e.g., warm soups on cold days)
   - How frequently a meal appears (novelty/rarity signal)
   - Special deals or discounts
6. **Runs proactively via scheduled jobs** (e.g., GitHub Actions cron) to:
   - Scrape and refresh meal data automatically every day, without a user triggering it
   - Generate personalised meal suggestion reports based on each user's stored preferences, such as dietary restrictions (vegetarian, halal, etc.), home country and cuisine familiarity, and budget
   - Send or store these reports so users get recommendations waiting for them when they open the app

The app is therefore triggered in at least **two ways**:
- **By the user** — through the Streamlit UI for example asking like a chatbot
- **By a scheduler** — through GitHub Actions (or a similar cron system) running Python scripts directly

The AI agent should **check the database first** before scraping. Only scrape if today's data isn't cached.

---

## Architecture (Keep It Simple)

```
[Web Sources] → [Scraper] → [LLM Extractor] → [JSON] → [Supabase DB]
                                                              ↓
                                              [Streamlit Agent UI]
                                                ↑
                                         [LangChain Agent]
                                         (DB lookup first,
                                          scrape if missing)
```

### Agent components to implement:
- Language model (Azure OpenAI or Google Gemini)
- Prompt with context (today's meals, location, weather)
- Conversation history / memory
- Knowledge base (Supabase with pgvector for semantic search)
- Retrieval system (check DB → scrape fallback)

---

## Coding Guidelines

- **Always add comments** explaining what each block of code does
- **Use clear variable names** (`meal_name`, not `mn`)
- **No unnecessary abstractions** — no metaclasses, decorators, or design patterns unless required
- **No one-liners** when a readable multi-line version exists
- **Functions should do one thing** and be short (under 30 lines)
- **Use f-strings** for string formatting
- **Use `print()` or `st.write()`** for debugging output — keep it visible for students
- Structure data as **JSON first** before moving to the database, so students understand the shape of the data

---

## File & Folder Conventions

```
hai5016-project/
├── .venv/                   # UV virtual environment (do not touch)
├── .github/
│   └── copilot-instructions.md
├── data/
│   ├── raw/                 # Raw scraped HTML or text
│   └── processed/           # Extracted JSON files
├── notebooks/               # Jupyter notebooks for exploration
├── src/
│   ├── scraper.py           # Web scraping logic
│   ├── extractor.py         # LLM-based data extraction
│   ├── database.py          # Supabase read/write helpers
│   ├── agent.py             # LangChain agent logic
│   └── app.py               # Streamlit app entry point
├── docs/                    # Markdown planning files
│   ├── plan.md
│   ├── tasks.md
│   └── progress.md
├── .env                     # API keys (never commit this)
├── .gitignore
├── pyproject.toml           # UV project config
└── README.md
```

---

## Environment Variables

Always use a `.env` file and `python-dotenv`. Never hardcode API keys.

```python
from dotenv import load_dotenv
from openai import OpenAI
import os

# Load environment variables from .env file
load_dotenv()

# Read Azure OpenAI credentials from environment variables
endpoint = os.getenv("AZURE_FOUNDRY_ENDPOINT")
deployment_name = os.getenv("AZURE_FOUNDRY_MODEL")
api_key = os.getenv("AZURE_FOUNDRY_API_KEY")

# Create the OpenAI client pointing to the Azure endpoint
client = OpenAI(
    base_url=endpoint,
    api_key=api_key
)

# Send a message to the model
completion = client.chat.completions.create(
    model=deployment_name,
    messages=[
        {
            "role": "user",
            "content": "What is the capital of France?",
        }
    ],
)

# Print the response
print(completion.choices[0].message)
```

Required `.env` keys during the project development (document these in README, not the values):
- `AZURE_FOUNDRY_API_KEY`, `AZURE_FOUNDRY_ENDPOINT`, `AZURE_FOUNDRY_MODEL`
- `SUPABASE_URL`, `SUPABASE_KEY`
- `OPENWEATHER_API_KEY` (or similar, for weather data)

---

## What NOT to Do

- ❌ Do not use `pip install` — always `uv add`
- ❌ Do not suggest Docker, Kubernetes, or microservices
- ❌ Do not use async/await unless absolutely necessary and explained
- ❌ Do not generate code without comments
- ❌ Do not suggest TypeScript, JavaScript, or non-Python solutions
- ❌ Do not use complex OOP (no inheritance chains, no abstract base classes)
- ❌ Do not skip the `.venv` activation check
- ❌ Do not commit `.env` files or API keys

---

## Documentation

- Use **Markdown files** in `docs/` to track plans, tasks, and progress
- Each new feature or sprint should have a short entry in `docs/progress.md`
- README.md should always explain how to install and run the project using UV
