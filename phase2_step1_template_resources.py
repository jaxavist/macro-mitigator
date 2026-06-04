#!/usr/bin/env python3
"""
Phase 2 Step 1: Convert Glossary Template Resources shared-blocks to child pages with Excerpts.

For each shared-block on the Template Resources page:
  1. Create a child page named "_{SharedBlockKey}" under Template Resources
  2. The child page body wraps the shared-block content in a native Excerpt macro
  3. Replace the shared-block on Template Resources with an Excerpt-Include
     pointing to the new child page

This unblocks the 1123 term pages that reference these shared-blocks via
include-shared-block — those will be updated in Step 2.

Usage:
    # Dry run (shows what would happen, no changes)
    python phase2_step1_template_resources.py --space-key CLOS --dry-run

    # Live run
    python phase2_step1_template_resources.py --space-key CLOS

Requirements:
    pip install requests

Environment variables:
    CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN
"""

import argparse
import copy
import json
import logging
import os
import sys
import uuid

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase2_step1")


class ConfluenceAPI:
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def get_space_id(self, space_key: str) -> str:
        url = f"{self.base_url}/wiki/api/v2/spaces"
        resp = self.session.get(url, params={"keys": space_key})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            raise ValueError(f"Space '{space_key}' not found")
        return results[0]["id"]

    def get_page_body(self, page_id: str) -> dict:
        url = f"{self.base_url}/wiki/api/v2/pages/{page_id}"
        params = {"body-format": "atlas_doc_format"}
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        body_raw = data.get("body", {}).get("atlas_doc_format", {}).get("value", "{}")
        return {
            "id": data["id"],
            "title": data["title"],
            "version": data["version"]["number"],
            "spaceId": data.get("spaceId"),
            "body": json.loads(body_raw),
        }

    def find_page_by_title(self, space_id: str, title: str) -> dict:
        url = f"{self.base_url}/wiki/api/v2/spaces/{space_id}/pages"
        params = {"title": title, "limit": 1}
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0]
        return None

    def create_page(self, space_id: str, parent_id: str, title: str, body: dict) -> dict:
        url = f"{self.base_url}/wiki/api/v2/pages"
        payload = {
            "spaceId": space_id,
            "parentId": parent_id,
            "title": title,
            "status": "current",
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(body),
            },
        }
        resp = self.session.post(url, json=payload)
        if not resp.ok:
            log.error(f"Failed to create page '{title}': {resp.status_code}")
            try:
                log.error(f"  Response: {json.dumps(resp.json(), indent=2)[:500]}")
            except Exception:
                log.error(f"  Response: {resp.text[:500]}")
            resp.raise_for_status()
        return resp.json()

    def update_page_body(self, page_id: str, title: str, version: int, body: dict, message: str = "") -> dict:
        # Try v2 API first
        url = f"{self.base_url}/wiki/api/v2/pages/{page_id}"
        payload = {
            "id": page_id,
            "status": "current",
            "title": title,
            "body": {
                "representation": "atlas_doc_format",
                "value": json.dumps(body),
            },
            "version": {
                "number": version + 1,
                "message": message or "Phase 2: shared-block to excerpt conversion",
            },
        }
        resp = self.session.put(url, json=payload)
        if resp.ok:
            return resp.json()

        # If v2 returns 404, fall back to v1 API
        if resp.status_code == 404:
            log.warning(f"  v2 API returned 404 for '{title}', falling back to v1 API...")
            return self._update_page_body_v1(page_id, title, version, body, message)

        log.error(f"Failed to update page '{title}': {resp.status_code}")
        try:
            log.error(f"  Response: {json.dumps(resp.json(), indent=2)[:500]}")
        except Exception:
            log.error(f"  Response: {resp.text[:500]}")
        resp.raise_for_status()

    def _update_page_body_v1(self, page_id: str, title: str, version: int, body: dict, message: str = "") -> dict:
        """Fallback: update via Confluence v1 REST API with ADF body."""
        url = f"{self.base_url}/wiki/rest/api/content/{page_id}"
        payload = {
            "type": "page",
            "title": title,
            "version": {
                "number": version + 1,
                "message": message or "Phase 2: shared-block to excerpt conversion",
            },
            "body": {
                "atlas_doc_format": {
                    "value": json.dumps(body),
                    "representation": "atlas_doc_format",
                },
            },
        }
        resp = self.session.put(url, json=payload)
        if not resp.ok:
            log.error(f"Failed to update page '{title}' via v1 API: {resp.status_code}")
            try:
                log.error(f"  Response: {json.dumps(resp.json(), indent=2)[:500]}")
            except Exception:
                log.error(f"  Response: {resp.text[:500]}")
            resp.raise_for_status()
        log.info(f"  Updated via v1 API successfully")
        return resp.json()


# ---------------------------------------------------------------------------
# ADF builders
# ---------------------------------------------------------------------------

def make_excerpt_body(content_nodes: list) -> dict:
    """
    Build an ADF document with content wrapped in a native Excerpt macro.

    The Excerpt macro in ADF is a bodiedExtension with extensionKey "excerpt".
    """
    return {
        "type": "doc",
        "content": [
            {
                "type": "bodiedExtension",
                "attrs": {
                    "extensionType": "com.atlassian.confluence.macro.core",
                    "extensionKey": "excerpt",
                    "parameters": {
                        "macroParams": {},
                        "macroMetadata": {
                            "macroId": {"value": str(uuid.uuid4())},
                            "schemaVersion": {"value": "1"},
                            "title": "Excerpt",
                        },
                    },
                },
                "content": content_nodes if content_nodes else [
                    {"type": "paragraph", "content": [{"type": "text", "text": " "}]}
                ],
            }
        ],
        "version": 1,
    }


def make_excerpt_include_node(page_title: str, space_key: str) -> dict:
    """
    Build an ADF node for the Excerpt-Include (Insert Excerpt) macro.

    This macro pulls in the Excerpt from another page.
    In ADF, it's an extension (non-bodied) with extensionKey "excerpt-include".
    """
    return {
        "type": "extension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "excerpt-include",
            "parameters": {
                "macroParams": {
                    "": {"value": f"{space_key}:{page_title}"},
                    "nopanel": {"value": "true"},
                },
                "macroMetadata": {
                    "macroId": {"value": str(uuid.uuid4())},
                    "schemaVersion": {"value": "1"},
                    "title": "Excerpt Include",
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Shared-block extraction
# ---------------------------------------------------------------------------

def find_shared_blocks(body: dict) -> list:
    """
    Find all shared-block macros in an ADF document.
    Returns list of (parent_content_list, index, node, key) tuples.
    """
    results = []
    _find_shared_blocks_recursive(body, results)
    return results


def _find_shared_blocks_recursive(node, results, parent_list=None, index=None):
    if isinstance(node, dict):
        ext_key = node.get("attrs", {}).get("extensionKey", "")
        if ext_key == "shared-block" and node.get("type") == "bodiedExtension":
            sb_key = node.get("attrs", {}).get("parameters", {}).get("macroParams", {}).get("shared-block-key", {})
            if isinstance(sb_key, dict):
                sb_key = sb_key.get("value", "")
            if parent_list is not None:
                results.append((parent_list, index, node, sb_key))

        if "content" in node and isinstance(node["content"], list):
            for i, child in enumerate(node["content"]):
                _find_shared_blocks_recursive(child, results, node["content"], i)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Phase 2 Step 1: Convert Template Resources shared-blocks to child page excerpts"
    )
    parser.add_argument("--space-key", required=True, help="Confluence space key")
    parser.add_argument("--template-page-id", default="106594481",
                        help="Page ID of Glossary Template Resources (default: 106594481)")
    parser.add_argument("--dry-run", action="store_true", help="Report without making changes")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)

    # Get space info
    log.info(f"Looking up space '{args.space_key}'...")
    space_id = api.get_space_id(args.space_key)
    log.info(f"Space ID: {space_id}")

    # Get Template Resources page
    log.info(f"Fetching Template Resources page ({args.template_page_id})...")
    page_data = api.get_page_body(args.template_page_id)
    log.info(f"Page: {page_data['title']} (v{page_data['version']})")

    # Find all shared-blocks
    shared_blocks = find_shared_blocks(page_data["body"])
    log.info(f"Found {len(shared_blocks)} shared-blocks on this page")

    if not shared_blocks:
        print("No shared-blocks found. Nothing to do.")
        return

    # Process each shared-block
    created_pages = {}
    for parent_list, idx, sb_node, sb_key in shared_blocks:
        child_title = f"_{sb_key}"
        content_nodes = sb_node.get("content", [])

        log.info(f"\n--- Shared Block: '{sb_key}' ---")
        log.info(f"  Content children: {len(content_nodes)}")
        log.info(f"  Child page title: '{child_title}'")

        if args.dry_run:
            log.info(f"  DRY RUN: Would create child page '{child_title}'")
            log.info(f"  DRY RUN: Would replace shared-block with excerpt-include")
            created_pages[sb_key] = {"title": child_title, "id": "DRY_RUN"}
            continue

        # Check if child page already exists
        existing = api.find_page_by_title(space_id, child_title)
        if existing:
            log.info(f"  Child page '{child_title}' already exists (id={existing['id']}). Skipping creation.")
            created_pages[sb_key] = {"title": child_title, "id": existing["id"]}
            continue

        # Create child page with Excerpt
        excerpt_body = make_excerpt_body(copy.deepcopy(content_nodes))
        try:
            new_page = api.create_page(
                space_id=space_id,
                parent_id=args.template_page_id,
                title=child_title,
                body=excerpt_body,
            )
            created_pages[sb_key] = {"title": child_title, "id": new_page["id"]}
            log.info(f"  Created child page '{child_title}' (id={new_page['id']})")
        except Exception as e:
            log.error(f"  Failed to create child page: {e}")
            continue

    # Now update the Template Resources page: replace shared-blocks with excerpt-includes
    if args.dry_run:
        log.info(f"\nDRY RUN: Would update Template Resources page with {len(created_pages)} excerpt-includes")
        print("\n" + "=" * 60)
        print("  DRY RUN SUMMARY")
        print("=" * 60)
        print(f"  Shared-blocks found: {len(shared_blocks)}")
        print(f"  Child pages to create: {len(created_pages)}")
        for sb_key, info in created_pages.items():
            print(f"    - _{sb_key}")
        print(f"  Template Resources page would be updated with excerpt-includes")
        print("=" * 60)
        return

    # Re-fetch the page (version may have changed if we're re-running)
    page_data = api.get_page_body(args.template_page_id)
    updated_body = copy.deepcopy(page_data["body"])

    # Find shared-blocks again in the fresh copy and replace them
    shared_blocks_fresh = find_shared_blocks(updated_body)
    replacements_made = 0

    for parent_list, idx, sb_node, sb_key in shared_blocks_fresh:
        if sb_key in created_pages:
            child_title = created_pages[sb_key]["title"]
            excerpt_include = make_excerpt_include_node(child_title, args.space_key)
            parent_list[idx] = excerpt_include
            replacements_made += 1
            log.info(f"  Replaced shared-block '{sb_key}' with excerpt-include → '{child_title}'")

    if replacements_made > 0:
        try:
            api.update_page_body(
                page_id=page_data["id"],
                title=page_data["title"],
                version=page_data["version"],
                body=updated_body,
                message=f"Phase 2: Replaced {replacements_made} shared-blocks with excerpt-includes",
            )
            log.info(f"\nUpdated Template Resources page (v{page_data['version'] + 1})")
        except Exception as e:
            log.error(f"\nFailed to update Template Resources page: {e}")
    else:
        log.info("No replacements needed on Template Resources page")

    # Summary
    print("\n" + "=" * 60)
    print("  PHASE 2 STEP 1 SUMMARY")
    print("=" * 60)
    print(f"  Shared-blocks processed: {len(shared_blocks)}")
    print(f"  Child pages created: {len([v for v in created_pages.values() if v['id'] != 'DRY_RUN'])}")
    print(f"  Replacements on Template Resources: {replacements_made}")
    print(f"  ---")
    print(f"  Child pages:")
    for sb_key, info in created_pages.items():
        print(f"    - {info['title']} (id={info['id']})")
    print("=" * 60)


if __name__ == "__main__":
    main()
