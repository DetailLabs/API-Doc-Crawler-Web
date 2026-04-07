#!/usr/bin/env python3
"""
Step 1: Download all API endpoints from a documentation site.

Usage:
    python3 scripts/01_download.py https://developers.example.com/reference -p "password" -o output
    python3 scripts/01_download.py https://petstore.swagger.io -o output

Output:
    output/endpoints/ — One JSON file per endpoint (e.g. GET_getvaults.json)
"""

import argparse
import json
import os
import re
import time
import logging
from urllib.parse import urlparse, urljoin

logger = logging.getLogger("downloader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

PASSWORD_SELECTORS = [
    'input[type="password"]', 'input[name="password"]',
    'input[placeholder*="assword"]', 'input[type="text"][name*="pass"]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Submit")', 'button:has-text("Enter")',
]


def authenticate(page, password):
    url_lower = page.url.lower()
    has_gate = "password" in url_lower or any(page.query_selector(s) for s in PASSWORD_SELECTORS[:2])
    if not has_gate:
        logger.info("No password gate detected")
        return True

    logger.info("Password gate detected, authenticating...")
    for sel in PASSWORD_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(password)
                break
        except Exception:
            continue

    for sel in SUBMIT_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                break
        except Exception:
            continue
    else:
        page.keyboard.press("Enter")

    page.wait_for_load_state("networkidle")
    time.sleep(3)
    if "password" in page.url.lower():
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        time.sleep(2)

    success = "password" not in page.url.lower()
    logger.info("Auth " + ("succeeded" if success else "FAILED"))
    return success


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_endpoints(page, start_url):
    """Find all endpoint URLs via OpenAPI spec, sidebar nav, and content scan."""
    logger.info("Starting endpoint discovery...")
    all_eps = []

    # Strategy 1: OpenAPI spec
    openapi = try_openapi(page, start_url)
    if openapi:
        logger.info(f"Found {len(openapi)} endpoints via OpenAPI spec")
        all_eps.extend(openapi)

    # If OpenAPI found 3+ complete endpoints, skip noisy strategies
    if len(openapi) >= 3 and all(ep.get("api_path") and ep.get("method") for ep in openapi):
        logger.info("OpenAPI spec is complete, skipping sidebar/content scan")
    else:
        sidebar = discover_sidebar(page, start_url)
        if sidebar:
            logger.info(f"Found {len(sidebar)} links via sidebar")
            all_eps.extend(sidebar)

    # Deduplicate
    seen = set()
    unique = []
    for ep in all_eps:
        if ep.get("source") == "openapi" and ep.get("api_path"):
            key = f"{ep.get('method', '')}:{ep['api_path']}"
        else:
            key = ep["url"].rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append(ep)

    # Filter doc pages
    doc_slugs = {
        "home", "index", "docs", "reference", "getting-started", "getting-started-5",
        "overview", "introduction", "authentication", "errors", "errors-1",
        "rate-limits", "rate-limits-1", "pagination", "pagination-1", "changelog",
        "idempotency-1", "permission-groups-1", "generate-ed25519-keys", "w",
    }
    unique = [ep for ep in unique if ep.get("source") == "openapi" or ep.get("slug", "").lower() not in doc_slugs]

    logger.info(f"Discovery complete: {len(unique)} unique endpoints")
    return unique


def try_openapi(page, start_url):
    """Try to find and parse an OpenAPI/Swagger spec."""
    base = urlparse(start_url)
    candidates = [
        f"{base.scheme}://{base.netloc}/openapi.json",
        f"{base.scheme}://{base.netloc}/swagger.json",
        f"{base.scheme}://{base.netloc}/v2/swagger.json",
        f"{base.scheme}://{base.netloc}/v2/openapi.json",
        f"{base.scheme}://{base.netloc}/api/openapi.json",
    ]

    # Check page for spec links
    try:
        links = page.evaluate("""() => {
            const links = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const h = a.getAttribute('href');
                if (h && (h.includes('openapi') || h.includes('swagger'))
                    && (h.endsWith('.json') || h.endsWith('.yaml')))
                    links.push(h);
            });
            return links;
        }""")
        for link in links:
            candidates.insert(0, urljoin(start_url, link))
    except Exception:
        pass

    for url in candidates:
        try:
            resp = page.request.get(url)
            if not resp.ok:
                continue
            spec = resp.json()
            if "paths" not in spec:
                continue
            logger.info(f"Found OpenAPI spec at {url}")
            return parse_openapi(spec, start_url)
        except Exception:
            continue
    return []


def parse_openapi(spec, base_url):
    """Parse OpenAPI spec into endpoint list with full details."""
    endpoints = []
    paths = spec.get("paths", {})
    definitions = spec.get("definitions", {}) or spec.get("components", {}).get("schemas", {})

    # Base URL from spec
    spec_base = base_url
    if "host" in spec:
        scheme = spec.get("schemes", ["https"])[0]
        spec_base = f"{scheme}://{spec['host']}{spec.get('basePath', '')}"
    elif spec.get("servers"):
        spec_base = spec["servers"][0].get("url", base_url)

    for path, methods in paths.items():
        for method, details in methods.items():
            method_upper = method.upper()
            if method_upper not in HTTP_METHODS:
                continue

            tags = details.get("tags", ["Uncategorized"])
            summary = details.get("summary", "") or ""
            description = details.get("description", "") or summary
            operation_id = details.get("operationId", "")
            slug = operation_id or f"{method_upper}_{path}".replace("/", "_").strip("_")

            # Parameters
            parameters = []
            for p in details.get("parameters", []):
                parameters.append({
                    "name": p.get("name", ""),
                    "type": p.get("type", "") or p.get("schema", {}).get("type", ""),
                    "required": "required" if p.get("required") else "",
                    "description": p.get("description", ""),
                    "in": p.get("in", ""),
                })

            # Swagger 2.x body params
            for p in details.get("parameters", []):
                if p.get("in") == "body" and "schema" in p:
                    props = resolve_schema(p["schema"], definitions)
                    for name, info in props.items():
                        parameters.append({
                            "name": name, "type": info.get("type", ""),
                            "required": "", "description": info.get("description", ""), "in": "body",
                        })

            # Response example
            response_example = ""
            for code in ("200", "201", "default"):
                resp = details.get("responses", {}).get(code, {})
                if "examples" in resp:
                    for _, ex in resp["examples"].items():
                        response_example = json.dumps(ex, indent=2) if isinstance(ex, (dict, list)) else str(ex)
                        break
                if response_example:
                    break

            # Build text
            text_parts = [f"{method_upper} {path}"]
            if summary:
                text_parts.append(summary)
            if description and description != summary:
                text_parts.append(description)

            endpoints.append({
                "url": base_url, "slug": slug, "method": method_upper,
                "api_path": path, "category": tags[0] if tags else "Uncategorized",
                "title": summary or f"{method_upper} {path}",
                "description": description[:200],
                "source": "openapi",
                "text": "\n".join(text_parts),
                "description_body": description,
                "permissions": "",
                "parameters": parameters,
                "code_blocks": [], "response_example": response_example,
                "headers": [], "html": "",
                "spec_base_url": spec_base,
            })

    return endpoints


def resolve_schema(schema, definitions):
    if "$ref" in schema:
        schema = definitions.get(schema["$ref"].split("/")[-1], {})
    props = schema.get("properties", {})
    if not props and "allOf" in schema:
        for sub in schema["allOf"]:
            if "$ref" in sub:
                sub = definitions.get(sub["$ref"].split("/")[-1], {})
            props.update(sub.get("properties", {}))
    return props


def discover_sidebar(page, start_url):
    """Extract endpoint links from sidebar navigation."""
    links = page.evaluate("""(startUrl) => {
        const results = [];
        const baseHost = new URL(startUrl).hostname;
        const selectors = [
            'nav a[href]', '[class*="sidebar"] a[href]', 'aside a[href]',
            '[role="navigation"] a[href]', '.menu__link[href]',
            '[class*="nav"] a[href]', '[id*="sidebar"] a[href]',
        ];
        const seen = new Set();
        for (const sel of selectors) {
            try {
                document.querySelectorAll(sel).forEach(a => {
                    const href = a.getAttribute('href');
                    if (!href || href === '#' || href.startsWith('javascript:')) return;
                    let fullUrl;
                    try { fullUrl = new URL(href, startUrl).href; } catch { return; }
                    try { if (new URL(fullUrl).hostname !== baseHost) return; } catch { return; }
                    if (fullUrl.match(/\\.(png|jpg|gif|css|js|svg|ico|woff)$/i)) return;
                    if (seen.has(fullUrl)) return;
                    seen.add(fullUrl);

                    let method = null;
                    const badge = a.querySelector('[class*="badge"], [class*="method"], span');
                    if (badge) {
                        const t = badge.innerText.trim().toUpperCase();
                        if (['GET','POST','PUT','PATCH','DELETE'].includes(t)) method = t;
                    }
                    results.push({
                        url: fullUrl,
                        slug: fullUrl.split('/').pop().split('#')[0].split('?')[0],
                        text: a.innerText.trim().substring(0, 200),
                        method: method,
                    });
                });
            } catch {}
        }
        return results;
    }""", start_url)

    return [{
        "url": l["url"], "slug": l.get("slug", ""), "method": l.get("method"),
        "category": "Uncategorized", "description": l.get("text", ""),
        "source": "sidebar",
    } for l in links]


# ---------------------------------------------------------------------------
# Extraction (per-page scraping)
# ---------------------------------------------------------------------------

EXTRACT_JS = """() => {
    const result = {
        title: '', text: '', description_body: '', permissions: '',
        method: null, api_path: '', parameters: [],
        code_blocks: [], response_example: '', headers: [],
    };

    // Title
    const h1 = document.querySelector('h1');
    result.title = h1 ? h1.innerText.trim() : document.title;

    // Method + path
    const methodBadge = document.querySelector('[class*="method"], [class*="verb"], [class*="badge"]');
    if (methodBadge) {
        const t = methodBadge.innerText.trim().toUpperCase();
        if (['GET','POST','PUT','PATCH','DELETE'].includes(t)) result.method = t;
    }
    if (!result.method) {
        const m = document.body.innerText.match(/\\b(GET|POST|PUT|PATCH|DELETE)\\s+(\\/[\\w\\/{}:.-]+)/);
        if (m) { result.method = m[1]; result.api_path = m[2]; }
    }
    if (!result.api_path) {
        const el = document.querySelector('[class*="url"], [class*="path"], [class*="endpoint"], code');
        if (el) {
            const pm = el.innerText.trim().match(/(\\/v\\d+\\/[^\\s]+|\\/api\\/[^\\s]+)/);
            if (pm) result.api_path = pm[1];
        }
    }

    // Full text
    const article = document.querySelector('article') || document.querySelector('main')
        || document.querySelector('[class*="content"]') || document.body;
    result.text = article ? article.innerText.trim() : '';

    // Description — clean paragraphs only
    const descParts = [];
    const seen = new Set();
    if (article) {
        article.querySelectorAll('p').forEach(el => {
            if (el.closest('pre, code, nav, table, footer, [class*="sidebar"]')) return;
            const text = el.innerText.trim();
            if (text.length < 15 || text.length > 2000) return;
            if (/^(GET|POST|PUT|PATCH|DELETE)\\s+\\//.test(text)) return;
            if (/^Updated\\s+\\d/.test(text)) return;
            if (/^Did this page help/.test(text)) return;
            if (/^(Yes|No)$/.test(text)) return;
            if (/^\\d+ Requests? This Month/.test(text)) return;
            if (/^(Too Many Requests|Internal Server Error|Unauthenticated|Forbidden)$/i.test(text)) return;
            if (/^(Information|RESPONSE BODY)\\s/i.test(text)) return;
            if (/^Log in to see/.test(text)) return;
            if (/^Make a request to see/.test(text)) return;
            if (seen.has(text)) return;
            seen.add(text);
            descParts.push(text);
        });
    }
    result.description_body = descParts.join('\\n\\n');

    // Permissions
    const pm = result.text.match(/[Pp]ermissions?\\s+required:?\\s*([^\\n]+)/);
    if (pm) result.permissions = pm[0].trim();

    // Parameters — leaf nodes only, deduplicated
    const seenParams = new Set();
    document.querySelectorAll('[class*="Param"], [class*="param"]').forEach(el => {
        if (el.querySelectorAll('[class*="Param"], [class*="param"]').length > 0) return;
        const nameEl = el.querySelector('[class*="name"], [class*="label"], [class*="key"], strong, b, code');
        if (!nameEl) return;
        const name = nameEl.innerText.trim();
        if (!name || name.length > 80) return;
        const typeEl = el.querySelector('[class*="type"]');
        const typeText = typeEl ? typeEl.innerText.trim() : '';
        const key = name + '::' + typeText;
        if (seenParams.has(key)) return;
        seenParams.add(key);
        const descEl = el.querySelector('[class*="desc"], p');
        const reqEl = el.querySelector('[class*="required"]');
        result.parameters.push({
            name: name, type: typeText,
            required: reqEl ? 'required' : '',
            description: descEl ? descEl.innerText.trim().substring(0, 500) : '',
        });
    });

    // Response example
    const respBlocks = document.querySelectorAll('[class*="response"], [class*="Response"]');
    respBlocks.forEach(block => {
        const pre = block.querySelector('pre');
        if (pre && !result.response_example) result.response_example = pre.innerText.trim();
    });

    return result;
}"""


def extract_page(page, url):
    """Navigate to a URL and extract endpoint data."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        pass
    try:
        page.wait_for_selector("article, main, [class*='content']", timeout=10000)
    except Exception:
        pass
    time.sleep(2)
    return page.evaluate(EXTRACT_JS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Step 1: Download all API endpoints")
    parser.add_argument("url", help="Starting URL of the API docs")
    parser.add_argument("-p", "--password", default=None, help="Password for gated docs")
    parser.add_argument("-o", "--output", default="output", help="Output directory")
    parser.add_argument("-d", "--delay", type=float, default=1.5, help="Delay between requests")
    parser.add_argument("--max", type=int, default=500, help="Max endpoints")
    parser.add_argument("--no-headless", action="store_true", help="Show browser")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    os.makedirs(args.output, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.no_headless)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36")
        page = ctx.new_page()

        # Navigate
        logger.info(f"Navigating to {args.url}")
        try:
            page.goto(args.url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            logger.warning(f"Slow navigation: {e}")
        time.sleep(2)

        # Auth
        if args.password:
            if not authenticate(page, args.password):
                browser.close()
                return

        # Discover
        endpoints = discover_endpoints(page, args.url)
        if not endpoints:
            logger.error("No endpoints found")
            browser.close()
            return

        # Split: OpenAPI endpoints already have data
        openapi_eps = [ep for ep in endpoints if ep.get("source") == "openapi" and ep.get("text")]
        scrape_eps = [ep for ep in endpoints if ep not in openapi_eps]

        all_data = []

        if openapi_eps:
            logger.info(f"Loaded {len(openapi_eps)} endpoints from OpenAPI spec")
            for ep in openapi_eps:
                logger.info(f"  ✓ {ep.get('method', '?'):6s} {ep.get('api_path', ep.get('slug', ''))}")
                all_data.append(ep)

        if scrape_eps:
            logger.info(f"\nScraping {len(scrape_eps)} endpoint pages...\n")

        for i, ep in enumerate(scrape_eps, 1):
            slug = ep.get("slug", f"endpoint_{i}")
            logger.info(f"[{i:3d}/{len(scrape_eps)}] {slug}")

            try:
                data = extract_page(page, ep["url"])
                merged = {**ep}
                for key in ("title", "method", "api_path"):
                    if data.get(key):
                        merged[key] = data[key]
                merged["text"] = data.get("text", "")
                merged["description_body"] = data.get("description_body", "")
                merged["permissions"] = data.get("permissions", "")
                merged["parameters"] = data.get("parameters", [])
                merged["code_blocks"] = data.get("code_blocks", [])
                merged["response_example"] = data.get("response_example", "")
                merged["headers"] = data.get("headers", [])

                if merged.get("text", "").strip():
                    all_data.append(merged)
                    logger.info(f"         ✓ {merged.get('title', slug)}")
                else:
                    time.sleep(3)
                    data = extract_page(page, ep["url"])
                    merged["text"] = data.get("text", "")
                    merged["description_body"] = data.get("description_body", "")
                    merged["parameters"] = data.get("parameters", [])
                    all_data.append(merged)
                    logger.info(f"         {'✓' if merged['text'].strip() else '⚠'} {slug}")

            except Exception as e:
                logger.error(f"         ✗ {e}")

            time.sleep(args.delay)

        browser.close()

    # Save each endpoint as its own file
    endpoints_dir = os.path.join(args.output, "endpoints")
    os.makedirs(endpoints_dir, exist_ok=True)

    for i, ep in enumerate(all_data, 1):
        slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", ep.get("slug", f"endpoint_{i}"))
        method = (ep.get("method") or "UNKNOWN").upper()
        filename = f"{method}_{slug}.json"
        filepath = os.path.join(endpoints_dir, filename)
        with open(filepath, "w") as f:
            json.dump(ep, f, indent=2, ensure_ascii=False)

    logger.info(f"\nSaved {len(all_data)} endpoints → {endpoints_dir}/")
    logger.info(f"  Each endpoint is its own JSON file: {endpoints_dir}/GET_example.json")
    logger.info("Next: python3 scripts/02_categorize.py -o " + args.output)


if __name__ == "__main__":
    main()
