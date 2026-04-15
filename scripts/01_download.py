#!/usr/bin/env python3
"""
Step 1: Download all API endpoints from a documentation site.

Uses httpx + BeautifulSoup with recursive link following (BFS) for deep
endpoint discovery.

Usage:
    python3 scripts/01_download.py https://developers.example.com/reference -o output
    python3 scripts/01_download.py https://petstore.swagger.io -o output

Output:
    output/endpoints/ — One JSON file per endpoint (e.g. GET_getvaults.json)
"""

import argparse
import json
import os
import re
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger("downloader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
}


def fetch_page_html(client, url, max_retries=3):
    """Fetch a URL via httpx and return the HTML string.
    Retries with exponential backoff on 429 (rate limit) responses."""
    for attempt in range(max_retries):
        try:
            resp = client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 429:
                # Rate limited — wait and retry
                retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                wait = min(retry_after, 30)
                logger.warning(f"Rate limited (429) on {url} — retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            # Other non-200 status
            logger.warning(f"HTTP {resp.status_code} for {url}")
            return ""
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            return ""
    logger.warning(f"Gave up on {url} after {max_retries} retries (rate limited)")
    return ""


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def is_password_protected(client, url):
    """Check whether *url* is behind a password gate (without submitting anything)."""
    try:
        resp = client.get(url, follow_redirects=True)
        if resp.status_code != 200:
            return False
        soup = BeautifulSoup(resp.text, "html.parser")
        pw_input = soup.find("input", attrs={"type": "password"})
        if not pw_input:
            pw_input = soup.find("input", attrs={"name": re.compile(r"pass", re.I)})
        return pw_input is not None
    except Exception:
        return False


def authenticate(client, url, password):
    """Attempt password authentication by POSTing to the page."""
    logger.info("Attempting password authentication...")
    try:
        resp = client.get(url, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Find a form with a password field
        password_input = soup.find("input", attrs={"type": "password"})
        if not password_input:
            password_input = soup.find("input", attrs={"name": re.compile(r"pass", re.I)})

        if not password_input:
            logger.info("No password gate detected")
            return True

        # Find the form
        form = password_input.find_parent("form")
        action = url
        method = "POST"
        form_data = {}

        if form:
            action = urljoin(url, form.get("action", ""))
            method = (form.get("method", "POST")).upper()
            # Collect all hidden inputs
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    if inp.get("type") == "password":
                        form_data[name] = password
                    else:
                        form_data[name] = inp.get("value", "")
        else:
            form_data["password"] = password

        if method == "POST":
            resp = client.post(action, data=form_data, follow_redirects=True)
        else:
            form_data_str = "&".join(f"{k}={v}" for k, v in form_data.items())
            resp = client.get(f"{action}?{form_data_str}", follow_redirects=True)

        success = resp.status_code == 200 and "password" not in resp.url.path.lower()
        logger.info("Auth " + ("succeeded" if success else "FAILED"))
        return success
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return False


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_endpoints(client, start_url, progress_callback=None, max_endpoints=500, max_pages=50):
    """Find all endpoint URLs via OpenAPI spec, sidebar nav, and content scan.

    progress_callback: optional callable(str) to report progress messages.
    max_endpoints: cap on how many endpoints to discover.
    max_pages: how many sub-pages to scan during recursive discovery.
    """
    def _progress(msg):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    _progress("Checking for OpenAPI spec...")
    all_eps = []

    # Strategy 1: OpenAPI spec
    openapi = try_openapi(client, start_url)
    if openapi:
        _progress(f"Found {len(openapi)} endpoints via OpenAPI spec")
        all_eps.extend(openapi)

    # If OpenAPI found 3+ complete endpoints, skip noisy strategies
    if len(openapi) >= 3 and all(ep.get("api_path") and ep.get("method") for ep in openapi):
        _progress("OpenAPI spec is complete — skipping sidebar scan")
    else:
        # Try ReadMe.io sidebar API first (JS-rendered sidebars)
        _progress("Checking for ReadMe.io sidebar...")
        readme_eps = discover_readme_sidebar(client, start_url)
        if readme_eps:
            _progress(f"Found {len(readme_eps)} pages via ReadMe sidebar API")
            all_eps.extend(readme_eps)
        else:
            # Try static sidebar first
            _progress("Scanning sidebar navigation...")
            sidebar = discover_sidebar(client, start_url)
            if sidebar:
                _progress(f"Found {len(sidebar)} links via static sidebar")

            # Always try recursive discovery to find deeper endpoint pages
            # Static sidebar often finds only top-level category pages
            _progress("Scanning sub-pages for more endpoints...")
            recursive = discover_sidebar_recursive(
                client, start_url, max_pages=max_pages, delay=0.5,
                progress_callback=progress_callback,
            )
            if recursive and len(recursive) > len(sidebar):
                _progress(f"Recursive scan found {len(recursive)} pages (vs {len(sidebar)} from sidebar)")
                all_eps.extend(recursive)
            elif sidebar:
                _progress(f"Using {len(sidebar)} sidebar links")
                all_eps.extend(sidebar)

    # Deduplicate
    seen = set()
    unique = []
    for ep in all_eps:
        if ep.get("source") == "openapi" and ep.get("api_path"):
            key = "{}:{}".format(ep.get("method", ""), ep["api_path"])
        else:
            key = ep["url"].rstrip("/")
        if key not in seen:
            seen.add(key)
            unique.append(ep)

    # Filter doc pages (keep OpenAPI entries and ReadMe "endpoint" types unconditionally)
    doc_slugs = {
        "home", "index", "docs", "reference", "getting-started", "getting-started-5",
        "overview", "introduction", "authentication", "errors", "errors-1",
        "rate-limits", "rate-limits-1", "pagination", "pagination-1", "changelog",
        "idempotency-1", "permission-groups-1", "generate-ed25519-keys", "w",
    }

    def _keep(ep):
        if ep.get("source") == "openapi":
            return True
        if ep.get("source") == "readme_sidebar":
            return ep.get("page_type") == "endpoint"
        return ep.get("slug", "").lower() not in doc_slugs

    unique = [ep for ep in unique if _keep(ep)]

    logger.info(f"Discovery complete: {len(unique)} unique endpoints")
    return unique


def try_openapi(client, start_url):
    """Try to find and parse an OpenAPI/Swagger spec."""
    base = urlparse(start_url)
    candidates = [
        "{}://{}/openapi.json".format(base.scheme, base.netloc),
        "{}://{}/swagger.json".format(base.scheme, base.netloc),
        "{}://{}/v2/swagger.json".format(base.scheme, base.netloc),
        "{}://{}/v2/openapi.json".format(base.scheme, base.netloc),
        "{}://{}/api/openapi.json".format(base.scheme, base.netloc),
        "{}://{}/v3/api-docs".format(base.scheme, base.netloc),
        "{}://{}/api-docs".format(base.scheme, base.netloc),
    ]

    # Check page for spec links
    page_html = fetch_page_html(client, start_url)
    if page_html:
        soup = BeautifulSoup(page_html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ("openapi" in href or "swagger" in href) and (href.endswith(".json") or href.endswith(".yaml")):
                candidates.insert(0, urljoin(start_url, href))

    for url in candidates:
        try:
            resp = client.get(url, follow_redirects=True)
            if resp.status_code != 200:
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
        spec_base = "{}://{}{}".format(scheme, spec["host"], spec.get("basePath", ""))
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
            slug = operation_id or "{}_{}".format(method_upper, path).replace("/", "_").strip("_")

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

            # RequestBody (OpenAPI 3.x)
            request_body = details.get("requestBody", {})
            if request_body:
                content = request_body.get("content", {})
                json_schema = content.get("application/json", {}).get("schema", {})
                if json_schema:
                    props = resolve_schema(json_schema, definitions)
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
            text_parts = ["{} {}".format(method_upper, path)]
            if summary:
                text_parts.append(summary)
            if description and description != summary:
                text_parts.append(description)

            endpoints.append({
                "url": base_url, "slug": slug, "method": method_upper,
                "api_path": path, "category": tags[0] if tags else "Uncategorized",
                "title": summary or "{} {}".format(method_upper, path),
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


def _same_site(host1, host2):
    """Check if two hostnames belong to the same site (allowing subdomains)."""
    if host1 == host2:
        return True
    # Extract root domain (last two parts): docs.stripe.com -> stripe.com
    parts1 = (host1 or "").split(".")
    parts2 = (host2 or "").split(".")
    root1 = ".".join(parts1[-2:]) if len(parts1) >= 2 else host1
    root2 = ".".join(parts2[-2:]) if len(parts2) >= 2 else host2
    return root1 == root2


def discover_readme_sidebar(client, start_url):
    """Discover endpoints via ReadMe.io's sidebar JSON API.

    ReadMe.io sites render their sidebar with JavaScript, so the HTML returned
    by the server doesn't contain navigation links.  However, the page embeds
    metadata (``<meta name="readme-subdomain">``, ``<meta name="readme-version">``)
    that lets us call the internal sidebar API directly.
    """
    try:
        resp = client.get(start_url, follow_redirects=True)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    subdomain_tag = soup.find("meta", attrs={"name": "readme-subdomain"})
    version_tag = soup.find("meta", attrs={"name": "readme-version"})
    if not subdomain_tag:
        return []

    subdomain = subdomain_tag.get("content", "")
    version = version_tag.get("content", "1.0") if version_tag else "1.0"
    if not subdomain:
        return []

    base = urlparse(start_url)
    sidebar_url = "{}://{}/{}/api-next/v2/branches/{}/sidebar?page_type=reference".format(
        base.scheme, base.netloc, subdomain, version
    )
    logger.info(f"Detected ReadMe.io site (subdomain={subdomain}), fetching sidebar API...")

    try:
        resp = client.get(sidebar_url, follow_redirects=True)
        if resp.status_code != 200:
            return []
        categories = resp.json()
    except Exception as e:
        logger.warning(f"ReadMe sidebar API failed: {e}")
        return []

    # Flatten the nested category → page → subpage structure
    endpoints = []

    def _collect(pages, category_name):
        for page in pages:
            slug = page.get("slug", "")
            title = page.get("title", "")
            page_type = page.get("type", "basic")  # "endpoint" or "basic"
            full_url = "{}://{}/reference/{}".format(base.scheme, base.netloc, slug)

            endpoints.append({
                "url": full_url,
                "slug": slug,
                "title": title,
                "method": None,
                "category": category_name,
                "description": title,
                "source": "readme_sidebar",
                "page_type": page_type,
            })
            # Recurse into child pages
            if page.get("pages"):
                _collect(page["pages"], category_name)

    for cat in categories:
        cat_title = cat.get("title", "Uncategorized")
        if cat.get("pages"):
            _collect(cat["pages"], cat_title)

    return endpoints


def discover_sidebar(client, start_url):
    """Extract endpoint links from sidebar navigation using BeautifulSoup."""
    html = fetch_page_html(client, start_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(start_url).hostname

    # Find sidebar/nav elements
    nav_selectors = [
        {"name": "nav"},
        {"attrs": {"class": re.compile(r"sidebar", re.I)}},
        {"name": "aside"},
        {"attrs": {"role": "navigation"}},
        {"attrs": {"class": re.compile(r"menu", re.I)}},
        {"attrs": {"class": re.compile(r"nav", re.I)}},
        {"attrs": {"id": re.compile(r"sidebar", re.I)}},
    ]

    links = []
    seen = set()

    for selector in nav_selectors:
        for container in soup.find_all(**selector):
            for a in container.find_all("a", href=True):
                href = a["href"]
                if href == "#" or href.startswith("javascript:"):
                    continue

                full_url = urljoin(start_url, href)
                parsed = urlparse(full_url)

                # Same site only (allow subdomains like docs.stripe.com for stripe.com)
                if not _same_site(parsed.hostname, base_host):
                    continue
                # Skip static assets
                if re.search(r"\.(png|jpg|gif|css|js|svg|ico|woff)$", full_url, re.I):
                    continue
                if full_url in seen:
                    continue
                seen.add(full_url)

                # Check for method badge
                method = None
                badge = a.find(class_=re.compile(r"badge|method", re.I))
                if not badge:
                    badge = a.find("span")
                if badge:
                    t = badge.get_text(strip=True).upper()
                    if t in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        method = t

                slug = full_url.split("/")[-1].split("#")[0].split("?")[0]
                text = a.get_text(strip=True)[:200]

                links.append({
                    "url": full_url,
                    "slug": slug,
                    "method": method,
                    "category": "Uncategorized",
                    "description": text,
                    "source": "sidebar",
                })

    return links


def _extract_nav_links(html, start_url):
    """Extract same-site navigation links from HTML string.
    Returns a set of absolute URLs found in nav/sidebar elements."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(start_url).hostname
    start_path = urlparse(start_url).path.rstrip("/")

    nav_selectors = [
        {"name": "nav"},
        {"attrs": {"class": re.compile(r"sidebar", re.I)}},
        {"name": "aside"},
        {"attrs": {"role": "navigation"}},
        {"attrs": {"class": re.compile(r"menu", re.I)}},
        {"attrs": {"class": re.compile(r"nav", re.I)}},
        {"attrs": {"id": re.compile(r"sidebar", re.I)}},
    ]

    urls = set()
    for selector in nav_selectors:
        for container in soup.find_all(**selector):
            for a in container.find_all("a", href=True):
                href = a["href"]
                if href == "#" or href.startswith("javascript:"):
                    continue
                full_url = urljoin(start_url, href)
                # Strip fragment and query
                parsed = urlparse(full_url)
                clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                clean_url = clean_url.rstrip("/")

                if not _same_site(parsed.hostname, base_host):
                    continue
                if re.search(r"\.(png|jpg|gif|css|js|svg|ico|woff)$", clean_url, re.I):
                    continue
                # Only follow links under the starting path prefix
                if start_path and not parsed.path.startswith(start_path):
                    continue
                urls.add(clean_url)

    # Also check all links in the page body (not just nav) that match the path prefix
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href == "#" or href.startswith("javascript:"):
            continue
        full_url = urljoin(start_url, href)
        parsed = urlparse(full_url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        if not _same_site(parsed.hostname, base_host):
            continue
        if re.search(r"\.(png|jpg|gif|css|js|svg|ico|woff)$", clean_url, re.I):
            continue
        if start_path and not parsed.path.startswith(start_path):
            continue
        urls.add(clean_url)

    return urls


def discover_sidebar_recursive(client, start_url, max_pages=50, delay=0.3, progress_callback=None, concurrency=8):
    """Recursively discover endpoint pages via BFS link following.

    Follows links under the start URL's path prefix to discover sub-pages.
    Pages within each BFS level are fetched in parallel for speed.

    Returns a list of endpoint dicts (same format as discover_sidebar).
    """
    start_url_clean = start_url.rstrip("/")
    visited = {start_url_clean}
    current_level = [start_url_clean]
    all_page_urls = set()

    limit_label = str(max_pages) if max_pages > 0 else "∞"
    logger.info(
        f"Starting recursive discovery from {start_url_clean} "
        f"(max {limit_label} pages, x{concurrency} parallel)"
    )

    pages_visited = 0
    while current_level and (max_pages <= 0 or pages_visited < max_pages):
        # Cap this batch so we don't exceed max_pages
        if max_pages > 0:
            remaining = max_pages - pages_visited
            batch = current_level[:remaining]
        else:
            batch = current_level
        current_level = current_level[len(batch):]

        found_count = len(all_page_urls)
        progress_msg = (
            f"Scanning {len(batch)} pages in parallel "
            f"({pages_visited + len(batch)}/{limit_label}) — {found_count} endpoints found"
        )
        if progress_callback:
            progress_callback(progress_msg)

        # Fetch this level in parallel
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {pool.submit(fetch_page_html, client, u): u for u in batch}
            results = []
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    html = fut.result()
                except Exception as e:
                    logger.warning(f"BFS fetch error for {url}: {e}")
                    html = ""
                results.append((url, html))

        pages_visited += len(batch)

        next_level = []
        for url, html in results:
            if not html:
                continue
            found_urls = _extract_nav_links(html, start_url)
            for u in found_urls:
                if u not in visited:
                    visited.add(u)
                    next_level.append(u)
                all_page_urls.add(u)

        # Add any carry-over from the previous current_level (if batch was capped)
        current_level = current_level + next_level

        if delay and current_level and (max_pages <= 0 or pages_visited < max_pages):
            time.sleep(delay)

    # Remove the start URL itself and any obvious category/index pages
    all_page_urls.discard(start_url_clean)

    logger.info(f"  BFS complete: visited {pages_visited} pages, found {len(all_page_urls)} unique links")

    # Build endpoint list from discovered URLs
    endpoints = []
    seen = set()
    for url in sorted(all_page_urls):
        if url in seen:
            continue
        seen.add(url)

        slug = url.split("/")[-1].split("#")[0].split("?")[0]
        if not slug:
            continue

        endpoints.append({
            "url": url,
            "slug": slug,
            "method": None,
            "category": "Uncategorized",
            "description": "",
            "source": "recursive_sidebar",
        })

    logger.info(f"  Recursive discovery yielded {len(endpoints)} endpoint candidates")
    return endpoints


# ---------------------------------------------------------------------------
# Extraction (per-page scraping with BeautifulSoup)
# ---------------------------------------------------------------------------

def _visible_text(el):
    """Get visible text from an element, skipping scripts/styles."""
    if isinstance(el, Tag):
        return el.get_text(separator=" ", strip=True)
    return str(el).strip()


def extract_page(client, url):
    """Fetch a URL and extract endpoint data using BeautifulSoup."""
    result = {
        "title": "", "text": "", "description_body": "", "permissions": "",
        "method": None, "api_path": "", "parameters": [],
        "code_blocks": [], "response_example": "", "headers": [],
    }

    html = fetch_page_html(client, url)
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")

    # Remove scripts, styles, nav, footer
    for tag in soup.find_all(["script", "style", "nav", "footer"]):
        tag.decompose()

    # Title
    h1 = soup.find("h1")
    result["title"] = h1.get_text(strip=True) if h1 else (soup.title.get_text(strip=True) if soup.title else "")

    # Method + path extraction — try multiple strategies

    # Strategy 1: Find spans/elements containing HTTP method text, then grab path from parent
    for span in soup.find_all("span"):
        t = span.get_text(strip=True).upper()
        if t in ("GET", "POST", "PUT", "PATCH", "DELETE") and span.parent:
            full = span.parent.get_text(strip=True)
            m = re.search(r"(GET|POST|PUT|PATCH|DELETE)\s*(/v\d+/[\w/:.\-{}]+)", full)
            if m:
                result["method"] = m.group(1)
                result["api_path"] = m.group(2)
                break

    # Strategy 2: Badge/method class elements
    if not result["method"]:
        method_el = soup.find(class_=re.compile(r"method|verb|badge", re.I))
        if method_el:
            t = method_el.get_text(strip=True).upper()
            if t in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                result["method"] = t

    # Get article text for further extraction
    article = soup.find("article") or soup.find("main") or soup.find(class_=re.compile(r"content", re.I)) or soup.body
    if article:
        article_text = article.get_text(separator="\n", strip=True)
        result["text"] = article_text

        # Strategy 3: Method+path pattern in text (with space)
        if not result["method"]:
            m = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/{}\.:=-]+)", article_text)
            if m:
                result["method"] = m.group(1)
                result["api_path"] = m.group(2)

    # Strategy 4: Find API path from specific elements
    if not result["api_path"]:
        path_el = soup.find(class_=re.compile(r"url|path|endpoint", re.I))
        if not path_el:
            path_el = soup.find("code")
        if path_el:
            path_text = path_el.get_text(strip=True)
            pm = re.search(r"(/v\d+/[^\s]+|/api/[^\s]+)", path_text)
            if pm:
                result["api_path"] = pm.group(1)

    # Description — clean paragraphs only
    desc_parts = []
    seen = set()
    if article:
        for p_el in article.find_all("p"):
            # Skip paragraphs inside code, tables, sidebars
            if p_el.find_parent(["pre", "code", "table"]):
                continue
            parent_class = " ".join(p_el.find_parent(attrs={"class": True}).get("class", []) if p_el.find_parent(attrs={"class": True}) else [])
            if "sidebar" in parent_class.lower():
                continue

            text = p_el.get_text(strip=True)
            if len(text) < 15 or len(text) > 2000:
                continue
            if re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+/", text):
                continue
            if re.match(r"^Updated\s+\d", text):
                continue
            if re.match(r"^Did this page help", text):
                continue
            if re.match(r"^(Yes|No)$", text):
                continue
            if re.match(r"^\d+ Requests? This Month", text):
                continue
            if re.match(r"^(Too Many Requests|Internal Server Error|Unauthenticated|Forbidden)$", text, re.I):
                continue
            if re.match(r"^(Information|RESPONSE BODY)\s", text, re.I):
                continue
            if re.match(r"^Log in to see", text):
                continue
            if re.match(r"^Make a request to see", text):
                continue
            if text in seen:
                continue
            seen.add(text)
            desc_parts.append(text)

    result["description_body"] = "\n\n".join(desc_parts)

    # Permissions
    pm = re.search(r"[Pp]ermissions?\s+required:?\s*([^\n]+)", result.get("text", ""))
    if pm:
        result["permissions"] = pm.group(0).strip()

    # Parameters
    seen_params = set()
    for param_el in soup.find_all(class_=re.compile(r"[Pp]aram")):
        # Skip containers that have child param elements
        if param_el.find(class_=re.compile(r"[Pp]aram")):
            continue

        name_el = param_el.find(class_=re.compile(r"name|label|key", re.I))
        if not name_el:
            name_el = param_el.find(["strong", "b", "code"])
        if not name_el:
            continue

        name = name_el.get_text(strip=True)
        if not name or len(name) > 80:
            continue

        type_el = param_el.find(class_=re.compile(r"type", re.I))
        type_text = type_el.get_text(strip=True) if type_el else ""

        key = "{}::{}".format(name, type_text)
        if key in seen_params:
            continue
        seen_params.add(key)

        desc_el = param_el.find(class_=re.compile(r"desc", re.I))
        if not desc_el:
            desc_el = param_el.find("p")
        req_el = param_el.find(class_=re.compile(r"required", re.I))

        result["parameters"].append({
            "name": name,
            "type": type_text,
            "required": "required" if req_el else "",
            "description": desc_el.get_text(strip=True)[:500] if desc_el else "",
        })

    # Response example
    for resp_el in soup.find_all(class_=re.compile(r"[Rr]esponse")):
        pre = resp_el.find("pre")
        if pre and not result["response_example"]:
            result["response_example"] = pre.get_text(strip=True)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Step 1: Download all API endpoints")
    parser.add_argument("url", help="Starting URL of the API docs")
    parser.add_argument("-p", "--password", default=None, help="Password for gated docs")
    parser.add_argument("-o", "--output", default="output", help="Output directory")
    parser.add_argument("-d", "--delay", type=float, default=0.5, help="Delay between requests")
    parser.add_argument("--max", type=int, default=500, help="Max endpoints")
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)

    with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as client:
        # Auth
        if args.password:
            if not authenticate(client, args.url, args.password):
                return

        # Discover
        logger.info(f"Discovering endpoints at {args.url}")
        endpoints = discover_endpoints(client, args.url)
        if not endpoints:
            logger.error("No endpoints found")
            return

        # Split: OpenAPI endpoints already have data
        openapi_eps = [ep for ep in endpoints if ep.get("source") == "openapi" and ep.get("text")]
        scrape_eps = [ep for ep in endpoints if ep not in openapi_eps]

        all_data = []

        if openapi_eps:
            logger.info(f"Loaded {len(openapi_eps)} endpoints from OpenAPI spec")
            for ep in openapi_eps:
                logger.info(f"  {ep.get('method', '?'):6s} {ep.get('api_path', ep.get('slug', ''))}")
                all_data.append(ep)

        if scrape_eps:
            logger.info(f"\nScraping {len(scrape_eps)} endpoint pages...\n")

        for i, ep in enumerate(scrape_eps[:args.max], 1):
            slug = ep.get("slug", "endpoint_{}".format(i))
            logger.info(f"[{i:3d}/{len(scrape_eps)}] {slug}")

            try:
                data = extract_page(client, ep["url"])
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

                all_data.append(merged)
                logger.info(f"         {'done' if merged.get('text', '').strip() else 'empty'} {merged.get('title', slug)}")

            except Exception as e:
                logger.error(f"         error: {e}")

            time.sleep(args.delay)

    # Save each endpoint as its own file
    endpoints_dir = os.path.join(args.output, "endpoints")
    os.makedirs(endpoints_dir, exist_ok=True)

    for i, ep in enumerate(all_data, 1):
        slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", ep.get("slug", "endpoint_{}".format(i)))
        method = (ep.get("method") or "UNKNOWN").upper()
        filename = "{}_{}.json".format(method, slug)
        filepath = os.path.join(endpoints_dir, filename)
        with open(filepath, "w") as f:
            json.dump(ep, f, indent=2, ensure_ascii=False)

    logger.info(f"\nSaved {len(all_data)} endpoints to {endpoints_dir}/")
    logger.info("Next: python3 scripts/02_categorize.py -o " + args.output)


if __name__ == "__main__":
    main()
