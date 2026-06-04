#!/usr/bin/env python3
"""
Restore a Confluence page to a specific version.

Usage:
    python restore_page_version.py --page-id 107848258 --version 1
"""

import argparse
import json
import os
import sys

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


def main():
    parser = argparse.ArgumentParser(description="Restore a Confluence page to a specific version")
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--version", required=True, type=int, help="Version number to restore")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    page_id = args.page_id
    target_version = args.version

    # Get the target version's body
    print(f"Fetching page {page_id} at version {target_version}...")
    resp = session.get(
        f"{BASE_URL}/wiki/rest/api/content/{page_id}",
        params={"expand": "body.atlas_doc_format,version", "version": target_version}
    )
    resp.raise_for_status()
    old_data = resp.json()
    old_body_raw = old_data["body"]["atlas_doc_format"]["value"]
    old_title = old_data["title"]
    print(f"  Title: {old_title}")
    print(f"  Version {target_version} body length: {len(old_body_raw)} chars")

    # Get the current version number
    resp2 = session.get(
        f"{BASE_URL}/wiki/rest/api/content/{page_id}",
        params={"expand": "version"}
    )
    resp2.raise_for_status()
    current_version = resp2.json()["version"]["number"]
    print(f"  Current version: {current_version}")

    if args.dry_run:
        print(f"\nDRY RUN: Would restore to version {target_version} (saving as v{current_version + 1})")
        return

    # Save as new version
    print(f"\nRestoring to version {target_version} (saving as v{current_version + 1})...")
    payload = {
        "type": "page",
        "title": old_title,
        "version": {
            "number": current_version + 1,
            "message": f"Restored to version {target_version}",
        },
        "body": {
            "atlas_doc_format": {
                "value": old_body_raw,
                "representation": "atlas_doc_format",
            },
        },
    }
    resp3 = session.put(f"{BASE_URL}/wiki/rest/api/content/{page_id}", json=payload)
    if resp3.ok:
        new_version = resp3.json()["version"]["number"]
        print(f"  Restored successfully (now at v{new_version})")
    else:
        print(f"  FAILED: {resp3.status_code}")
        try:
            print(f"  {json.dumps(resp3.json(), indent=2)[:500]}")
        except Exception:
            print(f"  {resp3.text[:500]}")


if __name__ == "__main__":
    main()
