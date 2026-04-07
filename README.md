# API Doc Crawler — Web Edition

Paste any API documentation URL and get a ready-to-import Postman Collection. No browser engine or Chromium install required.

Built with [FastAPI](https://fastapi.tiangolo.com/), [httpx](https://www.python-httpx.org/), and [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/). Lightweight pure-Python stack — just `pip install` and go. Handles password-gated sites, OpenAPI/Swagger specs, and server-rendered documentation platforms like ReadMe, Redocly, and more.

---

## How It Works

1. Paste a documentation URL into the web UI
2. The crawler fetches the page and looks for an OpenAPI/Swagger spec (JSON) — if found, endpoints are parsed directly with no scraping needed
3. If no spec is found, sidebar/nav links are extracted from the HTML and each linked page is scraped for method, path, parameters, descriptions, and response examples
4. Endpoints are categorized, deduplicated, and assembled into a Postman Collection v2.1
5. Download the collection and import it into Postman

---

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000**

### Replit

Click **Run** — no extra setup needed. The `replit.nix` only requires Python and pip (no Chromium or system libraries).

---

## Try It Out

The app includes example APIs you can test with immediately:

| API | URL | Type |
|-----|-----|------|
| **Petstore** | `https://petstore.swagger.io` | Swagger/OpenAPI spec |
| **GitHub** | `https://docs.github.com/en/rest` | Sidebar-navigated docs |
| **Spotify** | `https://developer.spotify.com/documentation/web-api` | Sidebar-navigated docs |
| **Stripe** | `https://stripe.com/docs/api` | Sidebar-navigated docs |
| **OpenAI** | `https://docs.openai.com/api-reference` | Sidebar-navigated docs |

---

## Features

- **Auto-discovery** — finds endpoints via OpenAPI/Swagger specs or sidebar navigation
- **No browser needed** — pure HTTP + HTML parsing, no Chromium or Playwright install
- **Smart extraction** — pulls method, path, parameters, descriptions, permissions, and response examples from each page
- **Password support** — authenticates through password-gated documentation sites
- **Auto-categorization** — groups endpoints by API resource (Wallets, Trading, Webhooks, etc.)
- **Deduplication** — merges duplicate endpoints, keeping the richest data
- **Auth detection** — identifies `Api-Access-Key`, `X-API-Key`, or `Bearer` auth patterns
- **Live progress** — real-time status updates as endpoints are discovered and scraped
- **One-click download** — download the Postman collection JSON directly from the browser

---

## What You Get

A Postman Collection v2.1 JSON file with:

- **Folders** — endpoints grouped by category
- **Auth headers** — auto-detected, applied to every request
- **Path variables** — `:paramName` format with descriptions
- **Request bodies** — JSON with typed placeholders for POST/PUT/PATCH
- **Markdown docs** — title, permissions, and description on every request
- **Collection variables** — `base_url` and auth key ready to configure

---

## Supported Platforms

| Platform | Discovery Method | Notes |
|----------|-----------------|-------|
| Swagger UI / OpenAPI | Parses spec JSON directly — no scraping needed | Best supported |
| ReadMe | Sidebar navigation + per-page extraction | Server-rendered HTML, works well |
| Redocly | Sidebar navigation + per-page extraction | Server-rendered HTML, works well |
| GitBook | Sidebar navigation + per-page extraction | May miss JS-only rendered content |
| Docusaurus | Menu link crawling + per-page extraction | May miss JS-only rendered content |
| Custom sites | Sidebar/nav crawling + per-page extraction | Works best with server-rendered HTML |

> **Note:** Since this version uses HTTP requests instead of a browser, sites that render content entirely via client-side JavaScript may return incomplete results. OpenAPI/Swagger specs and server-rendered sites (ReadMe, Redocly) work best.

---

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| Password | — | For gated documentation sites (HTTP form POST) |
| Collection name | Auto-detected | Custom name for the Postman collection |
| Max endpoints | 500 | Upper limit on endpoints to crawl |
| Delay | 0.5s | Delay between page requests |

---

## Architecture

```
URL Input
   │
   ▼
Step 1: Discovery & Scraping (httpx + BeautifulSoup)
   │  ├─ OpenAPI/Swagger spec parsing
   │  └─ Sidebar navigation + per-page extraction
   ▼
Step 2: Categorization & Cleanup
   │  ├─ Method backfill (slug/title heuristics)
   │  ├─ Path-based categorization
   │  └─ Deduplication (keep richest data)
   ▼
Step 3: Postman Collection Generation
   │  ├─ Auth header detection
   │  ├─ Request body construction
   │  └─ Markdown documentation
   ▼
postman_collection.json
```

---

## Dependencies

- **httpx** — HTTP client (replaces Playwright browser automation)
- **beautifulsoup4** — HTML parsing for sidebar discovery and page extraction
- **fastapi** — Web framework
- **uvicorn** — ASGI server

---

## License

MIT
