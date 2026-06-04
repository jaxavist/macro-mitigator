#!/usr/bin/env python3
"""
Quick fix: Replace excerpt-include for _Definition(s) with Include Page macro.

The _Definition(s) page content doesn't render properly through excerpt-include
(heading + inline help-text macro). Include Page works correctly.

Usage:
    python fix_definitions_include.py --space-key CLOS --dry-run
    python fix_definitions_include.py --space-key CLOS --batch-size 20
"""

import argparse
import copy
import json
import logging
import os
import sys
import time
import uuid

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fix_def")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

TARGET_TITLES = {"_Definition(s)", "CLOS:_Definition(s)"}


def get_space_id(space_key):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces", params={"keys": space_key})
    resp.raise_for_status()
    return resp.json()["results"][0]["id"]


def get_all_pages(space_id):
    pages, cursor = [], None
    while True:
        params = {"limit": 250, "status": "current", "sort": "id"}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces/{space_id}/pages", params=params)
        resp.raise_for_status()
        data = resp.json()
        pages.extend((p["id"], p["title"]) for p in data.get("results", []))
        nl = data.get("_links", {}).get("next")
        if not nl or "cursor=" not in nl:
            break
        cursor = nl.split("cursor=")[1].split("&")[0]
    return pages


def get_page_body(page_id):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    resp.raise_for_status()
    d = resp.json()
    return {"id": d["id"], "title": d["title"], "version": d["version"]["number"],
            "body": json.loads(d["body"]["atlas_doc_format"]["value"])}


def update_page(page_id, title, version, body):
    resp = session.put(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", json={
        "id": page_id, "status": "current", "title": title,
        "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
        "version": {"number": version + 1, "message": "Fix: _Definition(s) excerpt-include → include"},
    })
    if resp.ok:
        return True
    if resp.status_code in (404, 500):
        resp2 = session.put(f"{BASE_URL}/wiki/rest/api/content/{page_id}", json={
            "type": "page", "title": title,
            "version": {"number": version + 1, "message": "Fix: _Definition(s) excerpt-include → include"},
            "body": {"atlas_doc_format": {"value": json.dumps(body), "representation": "atlas_doc_format"}},
        })
        if resp2.ok:
            return True
    return False


def make_include_page(page_title):
    return {
        "type": "extension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "include",
            "parameters": {
                "macroParams": {"": {"value": page_title}},
                "macroMetadata": {
                    "macroId": {"value": str(uuid.uuid4())},
                    "schemaVersion": {"value": "1"},
                    "title": "Include Page",
                },
            },
        },
    }


def transform_node(node, stats):
    ext_key = node.get("attrs", {}).get("extensionKey", "")

    if ext_key == "excerpt-include":
        params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
        target = params.get("", {})
        target_val = target.get("value", "") if isinstance(target, dict) else target

        if target_val in TARGET_TITLES:
            stats["replaced"] += 1
            return [make_include_page("_Definition(s)")]

    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats))
        node["content"] = new_content

    return [node]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--batch-delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set env vars"); sys.exit(1)

    space_id = get_space_id(args.space_key)
    pages = [(pid, t) for pid, t in get_all_pages(space_id) if not t.startswith("_")]
    log.info(f"Pages: {len(pages)}")
    if args.limit:
        pages = pages[:args.limit]

    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0, "replaced": 0}

    for i in range(0, len(pages), args.batch_size):
        batch = pages[i:i + args.batch_size]
        log.info(f"--- Batch {i // args.batch_size + 1} ---")
        for pid, title in batch:
            totals["scanned"] += 1
            try:
                page = get_page_body(pid)
                body = copy.deepcopy(page["body"])
                stats = {"replaced": 0}
                if "content" in body:
                    nc = []
                    for child in body["content"]:
                        nc.extend(transform_node(child, stats))
                    body["content"] = nc
                if stats["replaced"] > 0:
                    if not args.dry_run:
                        ok = update_page(pid, title, page["version"], body)
                        totals["modified" if ok else "errored"] += 1
                    else:
                        totals["modified"] += 1
                    totals["replaced"] += stats["replaced"]
                else:
                    totals["skipped"] += 1
            except Exception as e:
                totals["errored"] += 1
                log.error(f"  [{title}] {e}")
        if i + args.batch_size < len(pages):
            time.sleep(args.batch_delay)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\n{'='*50}")
    print(f"  SUMMARY ({mode})")
    print(f"{'='*50}")
    print(f"  Scanned: {totals['scanned']}")
    print(f"  Modified: {totals['modified']}")
    print(f"  Skipped: {totals['skipped']}")
    print(f"  Errors: {totals['errored']}")
    print(f"  Replacements: {totals['replaced']}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
