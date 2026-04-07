# API Doc Crawler — Web Edition

Paste any API documentation URL and get a ready-to-import Postman Collection. No install, no CLI — just a browser.

Built with [FastAPI](https://fastapi.tiangolo.com/), [httpx](https://www.python-httpx.org/), and [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/). No browser or Chromium required. Handles password-gated sites, OpenAPI/Swagger specs, and sidebar-navigated platforms like ReadMe, GitBook, Docusaurus, and Redocly.

---

## How It Works

1. Paste a documentation URL
2. The crawler discovers endpoints via OpenAPI specs or sidebar navigation
3. Each endpoint page is scraped for method, path, parameters, descriptions, and response examples
4. Endpoints are categorized, deduplicated, and assembled into a Postman Collection v2.1
5. Download the collection and import it into Postman

---

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000**

### Docker

```bash
docker build -t api-doc-crawler .
docker run -p 5000:5000 api-doc-crawler
```

---

## Try It Out

The app includes five public APIs you can test with immediately:

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

| Platform | Discovery Method |
|----------|-----------------|
| Swagger UI / OpenAPI | Parses spec JSON directly — no scraping needed |
| ReadMe | Sidebar navigation + per-page extraction |
| GitBook | Sidebar navigation + per-page extraction |
| Docusaurus | Menu link crawling + per-page extraction |
| Redocly | Sidebar navigation + per-page extraction |
| Custom sites | Sidebar/nav crawling + per-page extraction |

---

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| Password | — | For gated documentation sites |
| Collection name | Auto-detected | Custom name for the Postman collection |
| Max endpoints | 500 | Upper limit on endpoints to crawl |

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

## License

MIT
