#!/usr/bin/env python3
"""
Step 3: Generate a Postman Collection v2.1 from categorized endpoints.

Usage:
    python3 scripts/03_postman.py -o output
    python3 scripts/03_postman.py -o output --name "My API"

Input:  output/endpoints.json (from step 2)
Output: output/postman_collection.json
"""

import argparse
import json
import os
import re
import uuid
import logging

logger = logging.getLogger("postman")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def main():
    parser = argparse.ArgumentParser(description="Step 3: Generate Postman collection")
    parser.add_argument("-o", "--output", default="output", help="Output directory")
    parser.add_argument("-n", "--name", default=None, help="Collection name")
    args = parser.parse_args()

    ep_path = os.path.join(args.output, "endpoints.json")
    if not os.path.exists(ep_path):
        logger.error(f"Not found: {ep_path} — run 02_categorize.py first")
        return

    with open(ep_path) as f:
        endpoints = json.load(f)

    logger.info(f"Loaded {len(endpoints)} endpoints")

    name = args.name or infer_name(endpoints)
    base_url = infer_base_url(endpoints)
    auth_header = detect_auth_header(endpoints)

    logger.info(f"Collection: {name}")
    logger.info(f"Base URL: {base_url}")
    logger.info(f"Auth header: {auth_header['key'] if auth_header else 'none detected'}")

    # Build collection
    collection = {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": name,
            "schema": POSTMAN_SCHEMA,
        },
        "variable": build_variables(base_url, auth_header),
        "item": [],
    }

    # Group by category → folders
    categories = {}
    for ep in endpoints:
        cat = ep.get("category", "Uncategorized")
        categories.setdefault(cat, []).append(ep)

    for cat_name, cat_eps in categories.items():
        folder = {"name": cat_name, "item": []}
        for ep in cat_eps:
            folder["item"].append(build_request(ep, auth_header))
        collection["item"].append(folder)

    # Save
    out_path = os.path.join(args.output, "postman_collection.json")
    with open(out_path, "w") as f:
        json.dump(collection, f, indent=2, ensure_ascii=False)

    total = sum(len(f["item"]) for f in collection["item"])
    logger.info(f"\nSaved: {out_path}")
    logger.info(f"  {len(collection['item'])} folders, {total} requests")
    logger.info(f"\nImport into Postman: File → Import → drag {out_path}")


# ---------------------------------------------------------------------------
# Auth detection
# ---------------------------------------------------------------------------

def detect_auth_header(endpoints):
    """Detect the single primary auth header from scraped content."""
    all_text = " ".join(ep.get("text", "")[:3000] for ep in endpoints[:30])

    candidates = [
        (r"CREDENTIALS.*Api-Access-Key|Header[:\s]+Api-Access-Key|Api-Access-Key",
         "Api-Access-Key", "{{api_key}}", "API access key for authentication"),
        (r"Header[:\s]+X-API-Key|x-api-key",
         "X-API-Key", "{{api_key}}", "API key for authentication"),
        (r"Authorization:\s*Bearer",
         "Authorization", "Bearer {{auth_token}}", "Bearer token"),
    ]

    for pattern, key, value, desc in candidates:
        if re.search(pattern, all_text, re.IGNORECASE):
            return {"key": key, "value": value, "description": desc, "type": "text"}

    return None


# ---------------------------------------------------------------------------
# Collection building
# ---------------------------------------------------------------------------

def build_variables(base_url, auth_header):
    variables = [{"key": "base_url", "value": base_url, "type": "string"}]
    if auth_header:
        for var in re.findall(r"\{\{(\w+)\}\}", auth_header["value"]):
            variables.append({"key": var, "value": "", "type": "string"})
    return variables


def build_request(ep, auth_header):
    """Build a Postman request item from an endpoint dict."""
    method = (ep.get("method") or "GET").upper()
    api_path = ep.get("api_path", "")
    title = ep.get("title") or ep.get("description") or ep.get("slug", "Unknown")

    # Classify parameters
    path_params, query_params, body_params = classify_params(ep.get("parameters", []), api_path, method)

    # URL
    postman_path = re.sub(r"\{(\w+)\}", r":\1", api_path) if api_path else ""
    url_raw = "{{base_url}}" + postman_path if api_path else ep.get("url", "")
    path_segments = [s for s in postman_path.strip("/").split("/") if s] if postman_path else []

    # Path variables with descriptions
    path_variables = []
    for match in re.finditer(r":(\w+)", url_raw):
        var_name = match.group(1)
        desc = f"(Required) The {var_name}"
        for p in path_params:
            if p.get("name") == var_name:
                d = p.get("description", "")
                desc = f"(Required) {d}" if d else desc
                break
        path_variables.append({"key": var_name, "value": "", "description": desc})

    url_obj = {"raw": url_raw, "host": ["{{base_url}}"], "path": path_segments}
    if path_variables:
        url_obj["variable"] = path_variables

    # Body
    body = None
    if method in ("POST", "PUT", "PATCH") and body_params:
        body_obj = {}
        for p in body_params:
            body_obj[p["name"]] = placeholder(p)
        body = {"mode": "raw", "raw": json.dumps(body_obj, indent=2), "options": {"raw": {"language": "json"}}}

    # Headers: only the primary auth header
    headers = [dict(auth_header)] if auth_header else []

    # Description
    description = build_description(ep)

    item = {
        "name": title,
        "request": {
            "method": method,
            "header": headers,
            "url": url_obj,
            "description": description,
        },
    }
    if body:
        item["request"]["body"] = body

    return item


def classify_params(parameters, api_path, method):
    """Split parameters into path, query, and body lists."""
    path_params, query_params, body_params = [], [], []

    for p in parameters:
        name = p.get("name", "")
        if not name:
            continue
        loc = p.get("in", "").lower()

        if loc == "path" or (api_path and f"{{{name}}}" in api_path):
            path_params.append(p)
        elif loc == "query":
            query_params.append(p)
        elif loc in ("body", "formdata"):
            body_params.append(p)
        elif loc == "header":
            continue
        elif method in ("POST", "PUT", "PATCH"):
            body_params.append(p)
        else:
            query_params.append(p)

    return path_params, query_params, body_params


def build_description(ep):
    """Build Markdown description: title, permissions, clean prose."""
    parts = []

    title = ep.get("title") or ep.get("slug", "")
    if title:
        parts.append(f"# {title}")

    permissions = ep.get("permissions", "")
    if permissions:
        parts.append(f"\n**{permissions}**")

    body = ep.get("description_body", "")
    if body:
        # Take first ~600 chars of clean prose
        lines = body.split("\n\n")
        trimmed = []
        total = 0
        for line in lines:
            if total + len(line) > 600:
                break
            trimmed.append(line)
            total += len(line)
        if trimmed:
            parts.append("\n" + "\n\n".join(trimmed))
    elif ep.get("description") and ep["description"] != title:
        parts.append(f"\n{ep['description']}")

    return "\n".join(parts) if parts else ""


def placeholder(param):
    ptype = param.get("type", "").lower()
    if "int" in ptype or "number" in ptype:
        return 0
    if "bool" in ptype:
        return False
    if "array" in ptype:
        return []
    if "object" in ptype:
        return {}
    return ""


def infer_name(endpoints):
    for ep in endpoints:
        url = ep.get("url", "")
        if url:
            host = re.sub(r"https?://", "", url).split("/")[0]
            return f"{host} API"
    return "API Collection"


def infer_base_url(endpoints):
    for ep in endpoints:
        if ep.get("spec_base_url"):
            return ep["spec_base_url"]
    for ep in endpoints:
        path = ep.get("api_path", "")
        url = ep.get("url", "")
        if path and url:
            from urllib.parse import urlparse
            p = urlparse(url)
            return f"{p.scheme}://{p.netloc}"
    return ""


if __name__ == "__main__":
    main()
