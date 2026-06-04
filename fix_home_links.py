#!/usr/bin/env python3
"""
Fix DC links on the Glossary Home Page.

Replaces confluence.tempustechnologies.com/display/GLOS/... links
with correct CLOS space links on the sandbox site.
Also fixes the Live Search spaceKey from GLOS to CLOS.

Usage:
    python fix_home_links.py --space-key CLOS --dry-run
    python fix_home_links.py --space-key CLOS
"""

import argparse
import copy
import json
import logging
import os
import re
import sys

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")
HOME_PAGE_ID = "4587522"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fix_links")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


def get_space_id(space_key):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces", params={"keys": space_key})
    resp.raise_for_status()
    return resp.json()["results"][0]["id"]


def get_all_pages(space_id):
    pages = {}
    cursor = None
    while True:
        params = {"limit": 250, "status": "current", "sort": "id"}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces/{space_id}/pages", params=params)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("results", []):
            pages[p["title"]] = p["id"]
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
    msg = "Fix: DC links → CLOS links"
    resp = session.put(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", json={
        "id": page_id, "status": "current", "title": title,
        "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
        "version": {"number": version + 1, "message": msg},
    })
    if resp.ok:
        return True
    if resp.status_code in (404, 500):
        resp2 = session.put(f"{BASE_URL}/wiki/rest/api/content/{page_id}", json={
            "type": "page", "title": title,
            "version": {"number": version + 1, "message": msg},
            "body": {"atlas_doc_format": {"value": json.dumps(body), "representation": "atlas_doc_format"}},
        })
        if resp2.ok:
            return True
    return False


def fix_links(node, title_to_id, space_key, stats):
    """Recursively fix DC links and GLOS spaceKey references."""

    # Fix link marks on text nodes
    if "marks" in node and isinstance(node["marks"], list):
        for mark in node["marks"]:
            if mark.get("type") == "link":
                href = mark.get("attrs", {}).get("href", "")
                new_href = resolve_dc_link(href, title_to_id, space_key)
                if new_href and new_href != href:
                    mark["attrs"]["href"] = new_href
                    stats["links_fixed"] += 1
                    log.debug(f"  Fixed link: {href[:60]} → {new_href[:60]}")

    # Fix Live Search spaceKey
    ext_key = node.get("attrs", {}).get("extensionKey", "")
    if ext_key == "livesearch":
        params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
        sk = params.get("spaceKey", {})
        if isinstance(sk, dict) and sk.get("value") == "GLOS":
            sk["value"] = space_key
            stats["livesearch_fixed"] += 1
            log.info(f"  Fixed Live Search spaceKey: GLOS → {space_key}")

    # Recurse
    if "content" in node and isinstance(node["content"], list):
        for child in node["content"]:
            fix_links(child, title_to_id, space_key, stats)


def resolve_dc_link(href, title_to_id, space_key):
    """Convert a DC URL to a Cloud URL if possible."""
    if not href:
        return None

    # Pattern: https://confluence.tempustechnologies.com/display/GLOS/{title}
    m = re.match(r'https?://confluence\.tempustechnologies\.com/display/GLOS/(.+)', href)
    if m:
        title = requests.utils.unquote(m.group(1)).replace('+', ' ')
        page_id = title_to_id.get(title)
        if page_id:
            return f"{BASE_URL}/wiki/spaces/{space_key}/pages/{page_id}/{requests.utils.quote(title, safe='')}"
        return None

    # Pattern: viewpage.action?pageId=... (the & page)
    m2 = re.match(r'https?://confluence\.tempustechnologies\.com/pages/viewpage\.action\?pageId=(\d+)', href)
    if m2:
        # The & page — find it by title
        for title, pid in title_to_id.items():
            if title == "&":
                return f"{BASE_URL}/wiki/spaces/{space_key}/pages/{pid}"
        return None

    # Pattern: any other tempustechnologies.com link
    if "confluence.tempustechnologies.com" in href:
        log.warning(f"  Unresolved DC link: {href[:80]}")
        return None

    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set env vars"); sys.exit(1)

    space_key = args.space_key

    # Build title → page ID mapping
    log.info("Building page title → ID mapping...")
    space_id = get_space_id(space_key)
    title_to_id = get_all_pages(space_id)
    log.info(f"  {len(title_to_id)} pages indexed")

    # Fetch home page
    log.info(f"Fetching Home Page ({HOME_PAGE_ID})...")
    page = get_page_body(HOME_PAGE_ID)
    log.info(f"  {page['title']} (v{page['version']})")

    # Fix links
    body = copy.deepcopy(page["body"])
    stats = {"links_fixed": 0, "livesearch_fixed": 0}

    if "content" in body:
        for child in body["content"]:
            fix_links(child, title_to_id, space_key, stats)

    log.info(f"\n  Links fixed: {stats['links_fixed']}")
    log.info(f"  Live Search fixed: {stats['livesearch_fixed']}")

    if stats["links_fixed"] == 0 and stats["livesearch_fixed"] == 0:
        print("No changes needed.")
        return

    if args.dry_run:
        print(f"\nDRY RUN: Would fix {stats['links_fixed']} links and {stats['livesearch_fixed']} Live Search refs")
        return

    ok = update_page(page["id"], page["title"], page["version"], body)
    print(f"\n{'Updated' if ok else 'FAILED'}: {stats['links_fixed']} links, {stats['livesearch_fixed']} Live Search")


if __name__ == "__main__":
    main()
