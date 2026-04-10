#!/usr/bin/env python3
"""
API Doc Crawler — Web Edition (No Browser Required)

Usage:
    pip install -r requirements.txt
    python app.py

Then open http://localhost:5000
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Import pipeline modules
from scripts import download_module as step1
from scripts import categorize_module as step2
from scripts import postman_module as step3

logger = logging.getLogger("webapp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

# ---------------------------------------------------------------------------
# Job storage (in-memory)
# ---------------------------------------------------------------------------

jobs: Dict[str, dict] = {}
JOBS_DIR = Path(os.environ.get("JOBS_DIR", "jobs"))


def get_job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(exist_ok=True)
    logger.info("API Doc Crawler ready (no browser required)")
    yield


app = FastAPI(title="API Doc Crawler", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class CrawlRequest(BaseModel):
    url: str
    password: Optional[str] = None
    collection_name: Optional[str] = None
    max_endpoints: int = 500
    delay: float = 0.5


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
        job["progress"] = "Discovering endpoints..."

        with httpx.Client(
            headers=step1.DEFAULT_HEADERS,
            timeout=30,
            follow_redirects=True,
        ) as client:

            # Auth
            if req.password:
                job["progress"] = "Authenticating..."
                if not step1.authenticate(client, req.url, req.password):
                    job["status"] = "failed"
                    job["error"] = "Authentication failed — check password"
                    return

            # Check for password gate before discovery
            if not req.password and step1.is_password_protected(client, req.url):
                job["status"] = "failed"
                job["error"] = "This site is password-protected. Please provide a password in Advanced Options and try again."
                return

            # Discover
            job["progress"] = "Discovering endpoints..."
            endpoints = step1.discover_endpoints(client, req.url)
            if not endpoints:
                job["status"] = "failed"
                job["error"] = "No endpoints found at this URL"
                return

            # Split OpenAPI vs scrape
            openapi_eps = [ep for ep in endpoints if ep.get("source") == "openapi" and ep.get("text")]
            scrape_eps = [ep for ep in endpoints if ep not in openapi_eps]

            all_data = list(openapi_eps)
            job["endpoint_count"] = len(openapi_eps)
            job["progress"] = "Found {} OpenAPI endpoints, scraping {} pages...".format(
                len(openapi_eps), len(scrape_eps)
            )

            for i, ep in enumerate(scrape_eps[:req.max_endpoints], 1):
                slug = ep.get("slug", "endpoint_{}".format(i))
                job["progress"] = "Scraping page {}/{}: {}".format(i, len(scrape_eps), slug)

                try:
                    data = step1.extract_page(client, ep["url"])
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
                    job["endpoint_count"] = len(all_data)
                except Exception as e:
                    logger.error(f"Error scraping {slug}: {e}")

                time.sleep(req.delay)

        # Save individual endpoint files
        endpoints_dir = os.path.join(output_dir, "endpoints")
        os.makedirs(endpoints_dir, exist_ok=True)
        for i, ep in enumerate(all_data, 1):
            slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", ep.get("slug", "endpoint_{}".format(i)))
            method = (ep.get("method") or "UNKNOWN").upper()
            filename = "{}_{}.json".format(method, slug)
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
        job["progress"] = "Categorized {} endpoints".format(len(endpoints_list))

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
        job["progress"] = "Done! {} requests in {} folders".format(total, len(collection["item"]))
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
