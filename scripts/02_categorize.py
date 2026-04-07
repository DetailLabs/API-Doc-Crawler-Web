#!/usr/bin/env python3
"""
Step 2: Categorize, deduplicate, and clean downloaded endpoints.

Usage:
    python3 scripts/02_categorize.py -o output

Input:  output/endpoints/ (individual JSON files from step 1)
Output: output/endpoints.json — Clean, categorized, deduplicated
"""

import argparse
import json
import os
import re
import logging
from collections import Counter

logger = logging.getLogger("categorizer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

# Slug prefix → HTTP method
SLUG_TO_METHOD = {
    "get": "GET", "list": "GET", "fetch": "GET", "retrieve": "GET",
    "search": "GET", "find": "GET", "download": "GET",
    "create": "POST", "add": "POST", "new": "POST", "submit": "POST",
    "accept": "POST", "reject": "POST", "authorize": "POST", "propose": "POST",
    "collect": "POST", "provision": "POST", "perform": "POST", "request": "POST",
    "convert": "POST", "tag": "POST", "post": "POST",
    "update": "PUT", "edit": "PUT", "modify": "PUT", "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE", "remove": "DELETE", "cancel": "DELETE",
    "destroy": "DELETE", "untag": "DELETE",
}

CRUD_PREFIXES = list(SLUG_TO_METHOD.keys())

# Known compound resource → category
COMPOUNDS = {
    "webhookendpoint": "Webhooks", "webhookeventtype": "Webhooks",
    "webhookvalidation": "Webhooks", "webhook": "Webhooks",
    "subaccount": "Subaccounts",
    "trusteddestination": "Trusted Destinations",
    "kycapplication": "KYC Onboarding", "kycdocument": "KYC Onboarding",
    "kycaffiliate": "KYC Onboarding", "kycagreement": "KYC Onboarding",
    "kycsubaccount": "KYC Onboarding",
    "snsettlement": "Atlas Settlement Network",
    "sntrustedcounterpart": "Atlas Settlement Network",
    "snparticipant": "Atlas Settlement Network",
    "cmpackage": "Collateral Management", "cmoperation": "Collateral Management",
    "cmexposure": "Collateral Management",
    "apikeyinfo": "API Key", "apikey": "API Key",
    "depositattribution": "Deposit Attribution", "spamattribution": "Deposit Attribution",
    "stablecoin": "Stablecoins",
    "assettypes": "Asset Types", "assettype": "Asset Types",
    "taxforms": "Tax", "taxtransaction": "Tax", "taxgains": "Tax",
    "taxinventory": "Tax", "taxaccount": "Tax", "costbasis": "Tax",
    "tradingaccount": "Trading", "tradepair": "Trading",
    "creditlimit": "Trading", "asyncorder": "Trading", "marketdata": "Trading",
    "walletreward": "Wallets", "walletposition": "Staking",
    "delegationaddress": "Staking", "consolidatestake": "Staking",
    "vestingbalance": "Vesting", "offchainvesting": "Vesting",
    "statementtype": "Statements",
    "fundingallocation": "Subaccounts", "billingcharge": "Subaccounts",
}


def main():
    parser = argparse.ArgumentParser(description="Step 2: Categorize endpoints")
    parser.add_argument("-o", "--output", default="output", help="Output directory")
    args = parser.parse_args()

    endpoints_dir = os.path.join(args.output, "endpoints")
    if not os.path.exists(endpoints_dir):
        logger.error(f"Not found: {endpoints_dir}/ — run 01_download.py first")
        return

    endpoints = []
    for filename in sorted(os.listdir(endpoints_dir)):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(endpoints_dir, filename)
        with open(filepath) as f:
            endpoints.append(json.load(f))

    logger.info(f"Loaded {len(endpoints)} endpoints from {endpoints_dir}/")

    # Step 1: Clean descriptions
    clean_descriptions(endpoints)

    # Step 2: Backfill methods
    backfill_methods(endpoints)

    # Step 3: Categorize
    categorize(endpoints)

    # Step 4: Drop non-endpoints (no API path)
    before = len(endpoints)
    endpoints = [ep for ep in endpoints if ep.get("api_path", "").strip() and ep["api_path"] != "/"]
    if len(endpoints) < before:
        logger.info(f"Dropped {before - len(endpoints)} entries with no API path")

    # Step 5: Deduplicate by method + path
    endpoints = deduplicate(endpoints)

    # Step 6: Sort by category
    endpoints.sort(key=lambda e: (e.get("category", "zzz"), e.get("slug", "")))

    # Save
    out_path = os.path.join(args.output, "endpoints.json")
    with open(out_path, "w") as f:
        json.dump(endpoints, f, indent=2, ensure_ascii=False)

    # Print summary
    cats = Counter(ep.get("category", "?") for ep in endpoints)
    methods = Counter(ep.get("method", "?") for ep in endpoints)
    logger.info(f"\nResult: {len(endpoints)} endpoints in {len(cats)} categories")
    logger.info(f"Methods: {dict(methods)}")
    for cat, count in cats.most_common():
        logger.info(f"  {cat}: {count}")

    logger.info(f"\nSaved → {out_path}")
    logger.info("Next: python3 scripts/03_postman.py -o " + args.output)


def clean_descriptions(endpoints):
    """Remove duplicate lines, HTTP method suffixes, and noise from descriptions."""
    method_suffix = re.compile(r"\s*\n?\s*(GET|POST|PUT|PATCH|DELETE|DEL)\s*$", re.IGNORECASE)

    for ep in endpoints:
        for field in ("description", "title"):
            val = ep.get(field, "")
            if val:
                ep[field] = method_suffix.sub("", val).strip()

        # Clean description_body: remove duplicate lines
        body = ep.get("description_body", "")
        if body:
            lines = body.split("\n\n")
            seen = set()
            unique = []
            for line in lines:
                line_clean = line.strip()
                if line_clean and line_clean not in seen:
                    seen.add(line_clean)
                    unique.append(line_clean)
            ep["description_body"] = "\n\n".join(unique)


def backfill_methods(endpoints):
    """Infer HTTP methods from text, slug, or title."""
    fixed = 0
    for ep in endpoints:
        if ep.get("method") and ep["method"] not in ("???", "None", None):
            continue

        method = None

        # From page text
        text = ep.get("text", "")[:2000]
        m = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\s+/[\w/{}:.-]+", text)
        if m:
            method = m.group(1)

        # From slug
        if not method:
            slug = ep.get("slug", "").lower()
            for prefix, m in SLUG_TO_METHOD.items():
                if slug.startswith(prefix):
                    method = m
                    break

        # From title
        if not method:
            title = ep.get("title", "").lower()
            for prefix, m in SLUG_TO_METHOD.items():
                if title.startswith(prefix):
                    method = m
                    break

        if method:
            ep["method"] = method
            fixed += 1

    if fixed:
        logger.info(f"Backfilled methods for {fixed} endpoints")


def categorize(endpoints):
    """Assign categories using API paths and slug analysis."""
    # Try path-based grouping first
    path_groups = {}
    for ep in endpoints:
        api_path = ep.get("api_path", "")
        if not api_path:
            continue
        segments = [s for s in api_path.strip("/").split("/") if s and not s.startswith("{")]
        if segments and re.match(r"v\d+", segments[0]):
            segments = segments[1:]
        if segments:
            resource = segments[0]
            path_groups.setdefault(resource, []).append(ep)

    # Assign categories
    for ep in endpoints:
        # Keep OpenAPI categories if they look good
        if ep.get("source") == "openapi" and ep.get("category") and ep["category"] != "Uncategorized":
            continue

        api_path = ep.get("api_path", "")
        if api_path:
            segments = [s for s in api_path.strip("/").split("/") if s and not s.startswith("{")]
            if segments and re.match(r"v\d+", segments[0]):
                segments = segments[1:]
            if segments:
                ep["category"] = resource_to_category(segments[0])
                continue

        # Fallback: slug-based
        slug = ep.get("slug", "").lower()
        if slug:
            resource = slug
            for prefix in CRUD_PREFIXES:
                if resource.startswith(prefix):
                    resource = resource[len(prefix):]
                    break
            resource = re.sub(r"(byid|byname|bycustomerid|status|details?)$", "", resource).strip("-_ ")
            if resource:
                ep["category"] = resource_to_category(resource)


def resource_to_category(resource):
    """Convert a resource slug to a readable category name."""
    resource_lower = resource.lower().replace("-", "").replace("_", "")

    # Check compound patterns (longest first)
    for pattern in sorted(COMPOUNDS.keys(), key=len, reverse=True):
        if pattern in resource_lower:
            return COMPOUNDS[pattern]

    # Default: title case, handle plurals
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", resource)

    # Don't split plural suffixes
    known = ["wallet", "vault", "transfer", "transaction", "trade", "order",
             "settlement", "address", "stake", "withdrawal", "statement", "account"]
    for res in known:
        if name.lower().startswith(res) and len(name) > len(res):
            rest = name[len(res):]
            if rest.lower() in ("s", "es", "ies"):
                continue
            if rest and rest[0].islower():
                name = res + " " + rest

    words = name.split()
    return " ".join(w.capitalize() for w in words if w) or "Uncategorized"


def deduplicate(endpoints):
    """Keep one endpoint per method+path, preferring richer data."""
    seen = {}
    for ep in endpoints:
        method = (ep.get("method") or "GET").upper()
        path = ep.get("api_path", "")
        key = f"{method}:{path}"

        if key in seen:
            existing = seen[key]
            score_new = len(ep.get("parameters", [])) + len(ep.get("description_body", ""))
            score_old = len(existing.get("parameters", [])) + len(existing.get("description_body", ""))
            if score_new > score_old:
                seen[key] = ep
        else:
            seen[key] = ep

    result = list(seen.values())
    if len(result) < len(endpoints):
        logger.info(f"Deduplicated: {len(endpoints)} → {len(result)} endpoints")
    return result


if __name__ == "__main__":
    main()
