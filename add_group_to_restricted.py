#!/usr/bin/env python3
"""
Add a group to edit restrictions on all restricted pages in a space.
Preserves existing restrictions — only adds the specified group.

Usage:
    python add_group_to_restricted.py --space-key POL --group site-admins --dry-run
    python add_group_to_restricted.py --space-key POL --group site-admins
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("add_group")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})


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


def get_page_restrictions(page_id, target_group):
    resp = session.get(f"{BASE_URL}/wiki/rest/api/content/{page_id}/restriction")
    if not resp.ok:
        return None, False
    data = resp.json()
    for r in data.get("results", []):
        if r["operation"] == "update":
            users = r.get("restrictions", {}).get("user", {}).get("results", [])
            groups = r.get("restrictions", {}).get("group", {}).get("results", [])
            group_names = [g.get("name", "") for g in groups]
            if users or groups:
                already_has = target_group in group_names
                return {
                    "users": len(users),
                    "groups": group_names
                }, already_has
    return None, False


def add_group_to_restriction(page_id, group_name, current_user_id):
    """
    Add a group to the update restriction while preserving existing restrictions.
    Must include the current user to avoid the 'evicts current user' error.
    Uses the full restriction PUT endpoint with the complete restriction set.
    """
    # First, get the current restrictions
    resp = session.get(f"{BASE_URL}/wiki/rest/api/content/{page_id}/restriction/byOperation/update")
    if not resp.ok:
        return False, resp.status_code

    data = resp.json()
    existing_users = data.get("restrictions", {}).get("user", {}).get("results", [])
    existing_groups = data.get("restrictions", {}).get("group", {}).get("results", [])

    # Build the new restriction set: existing + new group + current user
    user_list = [{"type": "known", "accountId": u["accountId"]} for u in existing_users]
    # Add current user if not already in the list
    if not any(u["accountId"] == current_user_id for u in existing_users):
        user_list.append({"type": "known", "accountId": current_user_id})

    group_list = [{"type": "group", "name": g["name"]} for g in existing_groups]
    # Add target group if not already in the list
    if not any(g["name"] == group_name for g in existing_groups):
        group_list.append({"type": "group", "name": group_name})

    # PUT the full restriction set
    payload = [{
        "operation": "update",
        "restrictions": {
            "user": user_list,
            "group": group_list,
        }
    }]
    resp2 = session.put(
        f"{BASE_URL}/wiki/rest/api/content/{page_id}/restriction",
        json=payload
    )
    return resp2.ok, resp2.status_code


def verify_group_added(page_id, group_name):
    """Re-read restrictions to verify the group was actually added."""
    resp = session.get(f"{BASE_URL}/wiki/rest/api/content/{page_id}/restriction/byOperation/update")
    if not resp.ok:
        return False
    data = resp.json()
    groups = data.get("restrictions", {}).get("group", {}).get("results", [])
    return any(g.get("name") == group_name for g in groups)


def main():
    parser = argparse.ArgumentParser(description="Add a group to edit restrictions on restricted pages")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--group", required=True, help="Group name to add (e.g., site-admins)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-delay", type=float, default=0.2)
    parser.add_argument("--verify", action="store_true", help="Re-read after adding to verify persistence")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    target_group = args.group

    # Get current user account ID (needed to avoid 'evicts current user' error)
    resp = session.get(f"{BASE_URL}/wiki/rest/api/user/current")
    resp.raise_for_status()
    current_user_id = resp.json()["accountId"]
    current_user_name = resp.json().get("displayName", "")
    log.info(f"Current API user: {current_user_name} ({current_user_id})")

    space_id = get_space_id(args.space_key)
    log.info(f"Collecting pages in space '{args.space_key}'...")
    all_pages = get_all_pages(space_id)
    log.info(f"Found {len(all_pages)} pages")

    restricted = []
    already_has = []

    log.info(f"Scanning for edit-restricted pages (checking for group '{target_group}')...")
    for i, (page_id, title) in enumerate(all_pages):
        if (i + 1) % 200 == 0:
            log.info(f"  Scanned {i + 1}/{len(all_pages)}...")
        info, has_group = get_page_restrictions(page_id, target_group)
        if info:
            if has_group:
                already_has.append((page_id, title))
            else:
                restricted.append((page_id, title, info))
        if (i + 1) % 50 == 0:
            time.sleep(args.batch_delay)

    log.info(f"\nRestricted pages: {len(restricted) + len(already_has)}")
    log.info(f"  Already have '{target_group}': {len(already_has)}")
    log.info(f"  Need '{target_group}' added: {len(restricted)}")

    if not restricted:
        print(f"\nAll restricted pages already have '{target_group}'. Nothing to do.")
        return

    if args.dry_run:
        print(f"\nDRY RUN: Would add '{target_group}' to {len(restricted)} pages")
        for pid, title, info in restricted[:20]:
            print(f"  {title} (id={pid}) — {info['users']} users, groups={info['groups']}")
        if len(restricted) > 20:
            print(f"  ... and {len(restricted) - 20} more")
        return

    # Add group
    success, failed, not_persisted = 0, 0, 0
    for page_id, title, info in restricted:
        ok, status = add_group_to_restriction(page_id, target_group, current_user_id)
        if ok:
            if args.verify:
                if verify_group_added(page_id, target_group):
                    success += 1
                else:
                    not_persisted += 1
                    log.warning(f"  NOT PERSISTED: {title} ({page_id})")
            else:
                success += 1
        else:
            failed += 1
            log.error(f"  Failed ({status}): {title} ({page_id})")
        time.sleep(args.batch_delay)

    print(f"\n{'=' * 50}")
    print(f"  Group '{target_group}' added to: {success} pages")
    if not_persisted:
        print(f"  Add returned OK but NOT persisted: {not_persisted} pages")
    print(f"  Failed: {failed}")
    print(f"{'=' * 50}")

    if not_persisted:
        print(f"\n  WARNING: {not_persisted} pages accepted the add but didn't persist.")
        print(f"  These pages may need manual restriction updates via the Confluence UI.")


if __name__ == "__main__":
    main()
