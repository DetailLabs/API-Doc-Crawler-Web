#!/usr/bin/env python3
"""
API Doc Crawler — Web Edition

Usage:
    pip install -r requirements.txt && playwright install chromium
    python app.py

Then open http://localhost:5000
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

# Import pipeline modules
from scripts import download_module as step1
from scripts import categorize_module as step2
from scripts import postman_module as step3

logger = logging.getLogger("webapp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

# ---------------------------------------------------------------------------
# Chromium detection
# ---------------------------------------------------------------------------

def find_chromium():
    """Find a usable Chromium binary, handling Nix and system installs."""
    # 1. Explicit env var (set in replit.nix)
    env_path = os.environ.get("CHROMIUM_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # 2. Nix playwright-driver browsers directory
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if browsers_path:
        for root, dirs, files in os.walk(browsers_path):
            for name in ("chromium", "chrome", "headless_shell"):
                if name in files:
                    candidate = os.path.join(root, name)
                    if os.access(candidate, os.X_OK):
                        return candidate

    # 3. System PATH
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path

    # 4. Fallback: let Playwright manage its own
    return None


CHROMIUM_PATH = find_chromium()
if CHROMIUM_PATH:
    logger.info(f"Using Chromium at: {CHROMIUM_PATH}")
else:
    logger.info("No system Chromium found — Playwright will use its bundled browser")

# ---------------------------------------------------------------------------
# Job storage (in-memory)
# ---------------------------------------------------------------------------

jobs: Dict[str, dict] = {}
JOBS_DIR = Path("jobs")


def get_job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(exist_ok=True)
    # Install Playwright browsers if not using system Chromium
    if not CHROMIUM_PATH and not os.environ.get("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"):
        try:
            subprocess.run(
                ["playwright", "install", "chromium"],
                check=True, capture_output=True, text=True, timeout=120,
            )
            logger.info("Playwright Chromium installed successfully")
        except Exception as e:
            logger.warning(f"Could not install Playwright browsers: {e}")
    yield


app = FastAPI(title="API Doc Crawler", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CrawlRequest(BaseModel):
    url: str
    password: Optional[str] = None
    collection_name: Optional[str] = None
    max_endpoints: int = 500
    delay: float = 1.5


class JobStatus(BaseModel):
    job_id: str
    status: str  # queued, downloading, categorizing, generating, completed, failed
    progress: str
    endpoint_count: int = 0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Pipeline runner (runs in thread to avoid blocking)
# ---------------------------------------------------------------------------

def run_pipeline(job_id: str, req: CrawlRequest):
    """Execute the 3-step pipeline synchronously (called from a thread)."""
    job = jobs[job_id]
    job_dir = get_job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    output_dir = str(job_dir / "output")
    os.makedirs(output_dir, exist_ok=True)

    try:
        # --- Step 1: Download ---
        job["status"] = "downloading"
        job["progress"] = "Launching browser and discovering endpoints..."

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            launch_args = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--single-process",
                ],
            }
            if CHROMIUM_PATH:
                launch_args["executable_path"] = CHROMIUM_PATH

            browser = p.chromium.launch(**launch_args)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            )
            page = ctx.new_page()

            # Navigate
            job["progress"] = f"Navigating to {req.url}"
            try:
                page.goto(req.url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                logger.warning(f"Slow navigation: {e}")
            time.sleep(2)

            # Auth
            if req.password:
                job["progress"] = "Authenticating..."
                if not step1.authenticate(page, req.password):
                    job["status"] = "failed"
                    job["error"] = "Authentication failed — check password"
                    browser.close()
                    return

            # Discover
            job["progress"] = "Discovering endpoints..."
            endpoints = step1.discover_endpoints(page, req.url)
            if not endpoints:
                job["status"] = "failed"
                job["error"] = "No endpoints found at this URL"
                browser.close()
                return

            # Split OpenAPI vs scrape
            openapi_eps = [ep for ep in endpoints if ep.get("source") == "openapi" and ep.get("text")]
            scrape_eps = [ep for ep in endpoints if ep not in openapi_eps]

            all_data = list(openapi_eps)
            job["endpoint_count"] = len(openapi_eps)
            job["progress"] = f"Found {len(openapi_eps)} OpenAPI endpoints, scraping {len(scrape_eps)} pages..."

            for i, ep in enumerate(scrape_eps[:req.max_endpoints], 1):
                slug = ep.get("slug", f"endpoint_{i}")
                job["progress"] = f"Scraping page {i}/{len(scrape_eps)}: {slug}"

                try:
                    data = step1.extract_page(page, ep["url"])
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

                    if not merged.get("text", "").strip():
                        time.sleep(3)
                        data = step1.extract_page(page, ep["url"])
                        merged["text"] = data.get("text", "")
                        merged["description_body"] = data.get("description_body", "")
                        merged["parameters"] = data.get("parameters", [])

                    all_data.append(merged)
                    job["endpoint_count"] = len(all_data)
                except Exception as e:
                    logger.error(f"Error scraping {slug}: {e}")

                time.sleep(req.delay)

            browser.close()

        # Save individual endpoint files
        endpoints_dir = os.path.join(output_dir, "endpoints")
        os.makedirs(endpoints_dir, exist_ok=True)
        for i, ep in enumerate(all_data, 1):
            slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", ep.get("slug", f"endpoint_{i}"))
            method = (ep.get("method") or "UNKNOWN").upper()
            filename = f"{method}_{slug}.json"
            with open(os.path.join(endpoints_dir, filename), "w") as f:
                json.dump(ep, f, indent=2, ensure_ascii=False)

        # --- Step 2: Categorize ---
        job["status"] = "categorizing"
        job["progress"] = "Cleaning and categorizing endpoints..."

        endpoints_list = all_data[:]
        step2.clean_descriptions(endpoints_list)
        step2.backfill_methods(endpoints_list)
        step2.categorize(endpoints_list)

        # Drop non-endpoints
        endpoints_list = [ep for ep in endpoints_list if ep.get("api_path", "").strip() and ep["api_path"] != "/"]
        endpoints_list = step2.deduplicate(endpoints_list)
        endpoints_list.sort(key=lambda e: (e.get("category", "zzz"), e.get("slug", "")))

        ep_path = os.path.join(output_dir, "endpoints.json")
        with open(ep_path, "w") as f:
            json.dump(endpoints_list, f, indent=2, ensure_ascii=False)

        job["endpoint_count"] = len(endpoints_list)
        job["progress"] = f"Categorized {len(endpoints_list)} endpoints"

        # --- Step 3: Postman collection ---
        job["status"] = "generating"
        job["progress"] = "Generating Postman collection..."

        name = req.collection_name or step3.infer_name(endpoints_list)
        base_url = step3.infer_base_url(endpoints_list)
        auth_header = step3.detect_auth_header(endpoints_list)

        collection = {
            "info": {
                "_postman_id": str(uuid.uuid4()),
                "name": name,
                "schema": step3.POSTMAN_SCHEMA,
            },
            "variable": step3.build_variables(base_url, auth_header),
            "item": [],
        }

        categories = {}
        for ep in endpoints_list:
            cat = ep.get("category", "Uncategorized")
            categories.setdefault(cat, []).append(ep)

        for cat_name, cat_eps in categories.items():
            folder = {"name": cat_name, "item": []}
            for ep in cat_eps:
                folder["item"].append(step3.build_request(ep, auth_header))
            collection["item"].append(folder)

        out_path = os.path.join(output_dir, "postman_collection.json")
        with open(out_path, "w") as f:
            json.dump(collection, f, indent=2, ensure_ascii=False)

        total = sum(len(f["item"]) for f in collection["item"])
        job["status"] = "completed"
        job["progress"] = f"Done! {total} requests in {len(collection['item'])} folders"
        job["collection_path"] = out_path
        job["endpoints_path"] = ep_path

    except Exception as e:
        logger.exception(f"Pipeline failed for job {job_id}")
        job["status"] = "failed"
        job["error"] = str(e)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(html_path.read_text())


@app.post("/api/crawl", response_model=JobStatus)
async def start_crawl(req: CrawlRequest):
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "progress": "Job queued...",
        "endpoint_count": 0,
        "error": None,
        "collection_path": None,
        "endpoints_path": None,
    }

    # Run pipeline in a background thread
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, run_pipeline, job_id, req)

    return JobStatus(**{k: v for k, v in jobs[job_id].items() if k in JobStatus.model_fields})


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    return JobStatus(**{k: v for k, v in job.items() if k in JobStatus.model_fields})


@app.get("/api/jobs/{job_id}/endpoints")
async def get_endpoints(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if not job.get("endpoints_path") or not os.path.exists(job["endpoints_path"]):
        raise HTTPException(404, "Endpoints not ready yet")
    with open(job["endpoints_path"]) as f:
        return json.load(f)


@app.get("/api/jobs/{job_id}/download")
async def download_collection(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "completed" or not job.get("collection_path"):
        raise HTTPException(404, "Collection not ready yet")
    return FileResponse(
        job["collection_path"],
        media_type="application/json",
        filename="postman_collection.json",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 5000))
    uvicorn.run(app, host="0.0.0.0", port=port)
