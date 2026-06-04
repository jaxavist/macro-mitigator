#!/usr/bin/env python3
"""
Fix _Glossary Main Directory page:
  1. Convert button macros to inline hyperlinked text with Cloud URLs
  2. Fix Live Search spaceKey from GLOS → CLOS

Usage:
    python fix_main_directory.py --space-key CLOS --dry-run
    python fix_main_directory.py --space-key CLOS
"""

import argparse
import copy
import json
import logging
import os
import re
import sys
import uuid

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")
PAGE_ID = "4595004"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("fix_dir")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


def get_page_body(page_id):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    resp.raise_for_status()
    d = resp.json()
    return {"id": d["id"], "title": d["title"], "version": d["version"]["number"],
            "body": json.loads(d["body"]["atlas_doc_format"]["value"])}


def update_page(page_id, title, version, body):
    msg = "Fix: buttons → links, GLOS → CLOS"
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


def fix_dc_search_url(url, space_key):
    """Convert DC search URL to Cloud search URL, replacing GLOS with space_key."""
    if "confluence.tempustechnologies.com" not in url:
        return url

    # Extract CQL from the URL
    m = re.search(r'cql=(.+?)(?:&|$)', url)
    if not m:
        return url

    cql = requests.utils.unquote(m.group(1))
    cql = cql.replace('"GLOS"', f'"{space_key}"')
    cql = cql.replace("GLOS", space_key)

    new_url = f"{BASE_URL}/wiki/search?cql={requests.utils.quote(cql)}"
    return new_url


def get_macro_param(node, param_name):
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    return val.get("value", "") if isinstance(val, dict) else val


def transform_node(node, space_key, stats):
    ext_key = node.get("attrs", {}).get("extensionKey", "")

    # Convert button macro → inline linked text
    if ext_key == "button":
        button_text = get_macro_param(node, "button-text")
        button_url = get_macro_param(node, "button-url")

        # Fix the URL
        new_url = fix_dc_search_url(button_url, space_key)

        stats["buttons_converted"] += 1
        log.info(f"  Button '{button_text}' → link")
        log.info(f"    URL: {new_url[:80]}...")

        return [{
            "type": "text",
            "text": button_text,
            "marks": [{"type": "link", "attrs": {"href": new_url}}],
        }]

    # Fix Live Search spaceKey
    if ext_key == "livesearch":
        params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
        sk = params.get("spaceKey", {})
        if isinstance(sk, dict) and sk.get("value") == "GLOS":
            sk["value"] = space_key
            stats["livesearch_fixed"] += 1
            log.info(f"  Fixed Live Search: GLOS → {space_key}")

    # Fix any DC links in text marks
    if "marks" in node and isinstance(node["marks"], list):
        for mark in node["marks"]:
            if mark.get("type") == "link":
                href = mark.get("attrs", {}).get("href", "")
                if "confluence.tempustechnologies.com" in href:
                    new_href = fix_dc_search_url(href, space_key)
                    if new_href != href:
                        mark["attrs"]["href"] = new_href
                        stats["links_fixed"] += 1

    # Recurse
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, space_key, stats))
        node["content"] = new_content

    return [node]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set env vars"); sys.exit(1)

    page = get_page_body(PAGE_ID)
    log.info(f"Page: {page['title']} (v{page['version']})")

    body = copy.deepcopy(page["body"])
    stats = {"buttons_converted": 0, "livesearch_fixed": 0, "links_fixed": 0}

    if "content" in body:
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, args.space_key, stats))
        body["content"] = new_content

    log.info(f"\n  Buttons converted: {stats['buttons_converted']}")
    log.info(f"  Live Search fixed: {stats['livesearch_fixed']}")
    log.info(f"  Other links fixed: {stats['links_fixed']}")

    if all(v == 0 for v in stats.values()):
        print("No changes needed.")
        return

    if args.dry_run:
        print(f"\nDRY RUN: Would convert {stats['buttons_converted']} buttons, fix {stats['livesearch_fixed']} Live Search, fix {stats['links_fixed']} links")
        return

    ok = update_page(page["id"], page["title"], page["version"], body)
    print(f"\n{'Updated' if ok else 'FAILED'}")


if __name__ == "__main__":
    main()
