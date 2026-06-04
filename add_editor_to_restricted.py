#!/usr/bin/env python3
"""
Add a user to edit restrictions on all restricted pages in a Confluence space.

Uses the per-user restriction endpoint which ADDS the user without removing
existing restrictions.

Usage:
    # Dry run — find all restricted pages, show what would change
    python add_editor_to_restricted.py --space-key CLOS --dry-run

    # Live run — add the API user to all restricted pages
    python add_editor_to_restricted.py --space-key CLOS

Requirements:
    pip install requests

Environment variables:
    CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
"""

import argparse
import json
import logging
import os
import sys
import time

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("add_editor")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


def get_current_user():
    resp = session.get(f"{BASE_URL}/wiki/rest/api/user/current")
    resp.raise_for_status()
    data = resp.json()
    return data["accountId"], data["displayName"], data.get("email", "")


def get_space_id(space_key):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces", params={"keys": space_key})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"Space '{space_key}' not found")
    return results[0]["id"]


def get_all_pages(space_id):
    pages = []
    cursor = None
    while True:
        params = {"limit": 250, "status": "current", "sort": "id"}
        if cursor:
            params["cursor"] = cursor
        resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces/{space_id}/pages", params=params)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("results", []):
            pages.append((p["id"], p["title"]))
        next_link = data.get("_links", {}).get("next")
        if not next_link or "cursor=" not in next_link:
            break
        cursor = next_link.split("cursor=")[1].split("&")[0]
    return pages


def get_page_restrictions(page_id):
    resp = session.get(f"{BASE_URL}/wiki/rest/api/content/{page_id}/restriction")
    resp.raise_for_status()
    data = resp.json()
    restrictions = {"read": {"users": [], "groups": []}, "update": {"users": [], "groups": []}}
    for r in data.get("results", []):
        op = r["operation"]
        for u in r.get("restrictions", {}).get("user", {}).get("results", []):
            restrictions[op]["users"].append(u["accountId"])
        for g in r.get("restrictions", {}).get("group", {}).get("results", []):
            restrictions[op]["groups"].append(g.get("name", g.get("id", "")))
    return restrictions


def add_user_to_update_restriction(page_id, account_id):
    """
    Add a user to the 'update' restriction of a page.
    This endpoint ADDS the user without removing existing restrictions.
    """
    url = f"{BASE_URL}/wiki/rest/api/content/{page_id}/restriction/byOperation/update/user/accountId/{account_id}"
    resp = session.put(url)
    return resp.ok, resp.status_code


def main():
    parser = argparse.ArgumentParser(description="Add API user to edit restrictions on all restricted pages")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-delay", type=float, default=0.2)
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    # Identify current API user
    account_id, display_name, email = get_current_user()
    log.info(f"API user: {display_name} ({email}) — accountId: {account_id}")

    # Get all pages
    space_id = get_space_id(args.space_key)
    log.info(f"Collecting pages in space '{args.space_key}'...")
    all_pages = get_all_pages(space_id)
    log.info(f"Found {len(all_pages)} pages")

    # Check each page for restrictions
    restricted_pages = []
    already_allowed = []
    unrestricted = 0

    log.info("Scanning for edit-restricted pages...")
    for i, (page_id, title) in enumerate(all_pages):
        if (i + 1) % 100 == 0:
            log.info(f"  Scanned {i + 1}/{len(all_pages)}...")
        try:
            restrictions = get_page_restrictions(page_id)
            update_users = restrictions["update"]["users"]
            update_groups = restrictions["update"]["groups"]

            if not update_users and not update_groups:
                unrestricted += 1
                continue

            if account_id in update_users:
                already_allowed.append((page_id, title))
                continue

            restricted_pages.append((page_id, title, len(update_users), update_groups))

        except Exception as e:
            log.error(f"  Error checking {title} ({page_id}): {e}")

        if args.batch_delay > 0 and (i + 1) % 50 == 0:
            time.sleep(args.batch_delay)

    log.info(f"\nScan complete:")
    log.info(f"  Unrestricted pages: {unrestricted}")
    log.info(f"  Already have access: {len(already_allowed)}")
    log.info(f"  Need to be added: {len(restricted_pages)}")

    if not restricted_pages:
        print("\nNo restricted pages need updating. You have access to everything.")
        return

    # Show what we found
    print(f"\n{'=' * 60}")
    print(f"  RESTRICTED PAGES ({len(restricted_pages)} pages)")
    print(f"{'=' * 60}")
    for page_id, title, user_count, groups in restricted_pages[:30]:
        groups_str = f" groups={groups}" if groups else ""
        print(f"  {title} (id={page_id}) — {user_count} allowed users{groups_str}")
    if len(restricted_pages) > 30:
        print(f"  ... and {len(restricted_pages) - 30} more")
    print()

    if args.dry_run:
        print(f"DRY RUN: Would add {display_name} ({account_id}) to {len(restricted_pages)} pages")
        return

    # Add user to each restricted page
    log.info(f"Adding {display_name} to {len(restricted_pages)} restricted pages...")
    success = 0
    failed = 0
    for page_id, title, _, _ in restricted_pages:
        ok, status = add_user_to_update_restriction(page_id, account_id)
        if ok:
            success += 1
            log.debug(f"  Added to {title}")
        else:
            failed += 1
            log.error(f"  Failed to add to {title} ({page_id}): HTTP {status}")
        if args.batch_delay > 0:
            time.sleep(args.batch_delay)

    print(f"\n{'=' * 60}")
    print(f"  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Pages updated: {success}")
    print(f"  Pages failed:  {failed}")
    print(f"  User added: {display_name} ({account_id})")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
