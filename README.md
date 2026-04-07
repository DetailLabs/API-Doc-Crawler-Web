# API Doc Crawler — Web Edition

Paste any API documentation URL and get a ready-to-import Postman Collection. No browser engine or Chromium required — pure Python.

Built with [FastAPI](https://fastapi.tiangolo.com/), [httpx](https://www.python-httpx.org/), and [BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/).

---

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000**

---

## How It Works

1. Paste a documentation URL into the web UI
2. The crawler fetches the page and looks for an OpenAPI/Swagger spec — if found, endpoints are parsed directly from JSON with no scraping needed
3. If no spec is found, sidebar/nav links are extracted from the HTML and each page is scraped for method, path, parameters, descriptions, and response examples
4. Endpoints are categorized, deduplicated, and assembled into a Postman Collection v2.1
5. Download the collection and import it into Postman

---

## Example APIs

| API | URL | Type |
|-----|-----|------|
| **Petstore** | `https://petstore.swagger.io` | OpenAPI spec |
| **GitHub** | `https://docs.github.com/en/rest` | Sidebar navigation |
| **Spotify** | `https://developer.spotify.com/documentation/web-api` | Sidebar navigation |
| **Stripe** | `https://stripe.com/docs/api` | Sidebar navigation |
| **OpenAI** | `https://developers.openai.com/api/reference/` | Sidebar navigation |

---

## Features

- **OpenAPI-first** — detects and parses OpenAPI/Swagger specs automatically for best results
- **No browser needed** — pure HTTP + HTML parsing, no Chromium or Playwright
- **Sidebar discovery** — extracts endpoint links from nav elements, with subdomain support
- **Smart extraction** — finds method + path even when split across HTML elements (e.g. Stripe)
- **Auto-categorization** — groups endpoints by API resource
- **Deduplication** — merges duplicates, keeping the richest data
- **Auth detection** — identifies `Api-Access-Key`, `X-API-Key`, or `Bearer` patterns
- **Live progress** — real-time status updates during crawling

---

## What You Get

A Postman Collection v2.1 JSON with:

- Endpoints grouped into **folders** by category
- **Auth headers** auto-detected and applied to every request
- **Path variables** with descriptions
- **Request bodies** with typed placeholders for POST/PUT/PATCH
- **Collection variables** — `base_url` and auth key ready to configure

---

## Supported Platforms

| Platform | How It Works |
|----------|-------------|
| **Swagger / OpenAPI** | Parses spec JSON directly — best results |
| **ReadMe, Redocly** | Server-rendered HTML — works well |
| **Stripe** | Server-rendered HTML with span-based method+path extraction |
| **GitBook, Docusaurus** | Sidebar extraction — may miss JS-only content |
| **Custom sites** | Sidebar/nav crawling — works best with server-rendered HTML |

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

## Dependencies

- **httpx** — HTTP client
- **beautifulsoup4** — HTML parsing
- **fastapi** — Web framework
- **uvicorn** — ASGI server

---

## License

MIT
