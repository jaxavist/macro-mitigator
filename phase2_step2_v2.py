#!/usr/bin/env python3
"""
Phase 2 Step 2 (v2): Batch convert term pages.

For each term page:
  1. Replace include-shared-block macros with excerpt-include macros
     pointing to Template Resources child pages
  2. For each shared-block: create a child page with the content wrapped
     in an Excerpt, then replace the shared-block with an excerpt-include
  3. Clean up any remaining legacy-content wrappers

Child page naming: _{ParentTitle} - {SharedBlockKey}
  e.g., _Accelitas - General Definition

Usage:
    # Dry run on single page
    python phase2_step2_v2.py --space-key CLOS --page-id 106594993 --dry-run

    # Live run on single page
    python phase2_step2_v2.py --space-key CLOS --page-id 106594993

    # Dry run on full space
    python phase2_step2_v2.py --space-key CLOS --dry-run

    # Live run on full space
    python phase2_step2_v2.py --space-key CLOS --batch-size 20

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
import time
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
log = logging.getLogger("phase2_step2")

# ---------------------------------------------------------------------------
# include-shared-block → excerpt-include mapping (from Step 1)
# ---------------------------------------------------------------------------

INCLUDE_SHARED_BLOCK_MAP = {
    ("Glossary Template Resources", "Definition(s)"): "_Definition(s)",
    ("Glossary Template Resources", "Internal Resources"): "_Internal Resources",
    ("Glossary Template Resources", "External Resources"): "_External Resources",
    ("Glossary Template Resources", "Other Data"): "_Other Data",
    ("Glossary Template Resources", "GlossaryHelpText"): "_GlossaryHelpText",
}

SKIP_PAGE_IDS = {
    "106594481",   # Glossary Template Resources (already converted)
    "107848258",   # Glossary Overview and How-To (Step 3)
    "106594311",   # Glossary Home Page
    "107741227",   # Space overview page
    "106594402",   # _Glossary Main Directory
    "106594537",   # Glossary Libraries
}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class ConfluenceAPI:
    def __init__(self, base_url, email, api_token):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def get_space_id(self, space_key):
        resp = self.session.get(f"{self.base_url}/wiki/api/v2/spaces", params={"keys": space_key})
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            raise ValueError(f"Space '{space_key}' not found")
        return results[0]["id"]

    def get_all_pages(self, space_id):
        pages = []
        cursor = None
        while True:
            params = {"limit": 250, "status": "current", "sort": "id"}
            if cursor:
                params["cursor"] = cursor
            resp = self.session.get(f"{self.base_url}/wiki/api/v2/spaces/{space_id}/pages", params=params)
            resp.raise_for_status()
            data = resp.json()
            for p in data.get("results", []):
                pages.append((p["id"], p["title"]))
            next_link = data.get("_links", {}).get("next")
            if not next_link or "cursor=" not in next_link:
                break
            cursor = next_link.split("cursor=")[1].split("&")[0]
        return pages

    def get_page_body(self, page_id):
        resp = self.session.get(
            f"{self.base_url}/wiki/api/v2/pages/{page_id}",
            params={"body-format": "atlas_doc_format"}
        )
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

    def find_page_by_title(self, space_id, title):
        resp = self.session.get(
            f"{self.base_url}/wiki/api/v2/spaces/{space_id}/pages",
            params={"title": title, "limit": 1}
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_page(self, space_id, parent_id, title, body):
        resp = self.session.post(
            f"{self.base_url}/wiki/api/v2/pages",
            json={
                "spaceId": space_id,
                "parentId": parent_id,
                "title": title,
                "status": "current",
                "body": {
                    "representation": "atlas_doc_format",
                    "value": json.dumps(body),
                },
            }
        )
        if not resp.ok:
            log.error(f"  Failed to create '{title}': {resp.status_code}")
            try:
                log.error(f"  {json.dumps(resp.json())[:300]}")
            except Exception:
                pass
            return None
        return resp.json()

    def update_page(self, page_id, title, version, body, message=""):
        msg = message or "Phase 2: shared-block to excerpt-include conversion"
        # Try v2
        resp = self.session.put(
            f"{self.base_url}/wiki/api/v2/pages/{page_id}",
            json={
                "id": page_id, "status": "current", "title": title,
                "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
                "version": {"number": version + 1, "message": msg},
            }
        )
        if resp.ok:
            return True
        # Fall back to v1
        if resp.status_code == 404:
            resp2 = self.session.put(
                f"{self.base_url}/wiki/rest/api/content/{page_id}",
                json={
                    "type": "page", "title": title,
                    "version": {"number": version + 1, "message": msg},
                    "body": {"atlas_doc_format": {
                        "value": json.dumps(body),
                        "representation": "atlas_doc_format",
                    }},
                }
            )
            if resp2.ok:
                return True
            log.error(f"  v1 fallback failed for '{title}': {resp2.status_code}")
            return False
        log.error(f"  Update failed for '{title}': {resp.status_code}")
        return False


# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------

def get_macro_param(node, param_name):
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    if isinstance(val, dict):
        return val.get("value", "")
    return val


def make_excerpt_body(content_nodes):
    return {
        "type": "doc",
        "content": [{
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
            "content": content_nodes or [
                {"type": "paragraph", "content": [{"type": "text", "text": " "}]}
            ],
        }],
        "version": 1,
    }


def make_excerpt_include(child_page_title, space_key):
    return {
        "type": "extension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "excerpt-include",
            "parameters": {
                "macroParams": {
                    "": {"value": f"{space_key}:{child_page_title}"},
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
# Two-pass approach:
#   Pass 1: Scan for shared-blocks, create child pages
#   Pass 2: Transform ADF (replace shared-blocks + include-shared-blocks)
# ---------------------------------------------------------------------------

def find_shared_blocks_in_tree(node, results=None):
    """Find all shared-block macros anywhere in the ADF tree."""
    if results is None:
        results = []
    ext_key = node.get("attrs", {}).get("extensionKey", "")
    if ext_key == "shared-block" and node.get("type") == "bodiedExtension":
        sb_key = get_macro_param(node, "shared-block-key")
        content = node.get("content", [])
        results.append({"key": sb_key, "content": content})

    # Recurse into content
    for child in node.get("content", []):
        find_shared_blocks_in_tree(child, results)

    # Also check inside legacy-content nestedContent
    if (node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"):
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested:
            for child in nested.get("content", []):
                find_shared_blocks_in_tree(child, results)

    return results


class TransformStats:
    def __init__(self):
        self.isb_replaced = 0
        self.sb_replaced = 0
        self.legacy_removed = 0
        self.ht_replaced = 0

    @property
    def has_changes(self):
        return (self.isb_replaced + self.sb_replaced +
                self.legacy_removed + self.ht_replaced) > 0


def transform_node(node, stats, space_key, child_page_map):
    """
    Transform a single ADF node. Returns a list of replacement nodes.

    child_page_map: dict of {shared_block_key: child_page_title}
    """
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # --- include-shared-block → excerpt-include (Template Resources refs) ---
    if ext_key == "include-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        map_key = (source_page, sb_key)
        if map_key in INCLUDE_SHARED_BLOCK_MAP:
            stats.isb_replaced += 1
            return [make_excerpt_include(INCLUDE_SHARED_BLOCK_MAP[map_key], space_key)]
        return [node]

    # --- help-text-from-shared-block → excerpt-include ---
    if ext_key == "help-text-from-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        map_key = (source_page, sb_key)
        if map_key in INCLUDE_SHARED_BLOCK_MAP:
            stats.ht_replaced += 1
            return [make_excerpt_include(INCLUDE_SHARED_BLOCK_MAP[map_key], space_key)]
        return [node]

    # --- shared-block → excerpt-include pointing to child page ---
    if ext_key == "shared-block" and node_type == "bodiedExtension":
        sb_key = get_macro_param(node, "shared-block-key")
        if sb_key in child_page_map:
            stats.sb_replaced += 1
            return [make_excerpt_include(child_page_map[sb_key], space_key)]
        # No child page created (maybe dry run) — leave as-is
        return [node]

    # --- legacy-content → unwrap ---
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            stats.legacy_removed += 1
            result = []
            for child in nested["content"]:
                result.extend(transform_node(child, stats, space_key, child_page_map))
            return result
        stats.legacy_removed += 1
        return []

    # --- Recurse ---
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map))
        node["content"] = new_content

    return [node]


def transform_body(body, space_key, child_page_map):
    body = copy.deepcopy(body)
    stats = TransformStats()
    if "content" in body and isinstance(body["content"], list):
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map))
        body["content"] = new_content
    return body, stats


# ---------------------------------------------------------------------------
# Process a single page
# ---------------------------------------------------------------------------

def process_page(api, space_id, space_key, page_id, dry_run):
    page = api.get_page_body(page_id)
    title = page["title"]

    # Pass 1: Find shared-blocks and create child pages
    shared_blocks = find_shared_blocks_in_tree(page["body"])
    child_page_map = {}  # {sb_key: child_page_title}

    for sb in shared_blocks:
        sb_key = sb["key"]
        child_title = f"_{title} - {sb_key}"

        if dry_run:
            child_page_map[sb_key] = child_title
            continue

        # Check if child already exists
        existing = api.find_page_by_title(space_id, child_title)
        if existing:
            child_page_map[sb_key] = child_title
            log.debug(f"  Child '{child_title}' already exists")
            continue

        # Create child page with Excerpt
        excerpt_body = make_excerpt_body(copy.deepcopy(sb["content"]))
        result = api.create_page(space_id, page_id, child_title, excerpt_body)
        if result:
            child_page_map[sb_key] = child_title
            log.debug(f"  Created child '{child_title}'")
        else:
            log.error(f"  Failed to create child '{child_title}'")

    # Pass 2: Transform the page ADF
    new_body, stats = transform_body(page["body"], space_key, child_page_map)

    if not stats.has_changes:
        return {"title": title, "page_id": page_id, "changed": False,
                "children_created": 0, "stats": None, "error": None}

    children_created = len(child_page_map)

    if dry_run:
        return {"title": title, "page_id": page_id, "changed": True,
                "children_created": children_created,
                "stats": {"isb": stats.isb_replaced, "sb": stats.sb_replaced,
                          "legacy": stats.legacy_removed, "ht": stats.ht_replaced},
                "error": None}

    # Save
    ok = api.update_page(page_id, title, page["version"], new_body)
    if not ok:
        return {"title": title, "page_id": page_id, "changed": True,
                "children_created": children_created,
                "stats": {"isb": stats.isb_replaced, "sb": stats.sb_replaced,
                          "legacy": stats.legacy_removed, "ht": stats.ht_replaced},
                "error": "Update failed"}

    return {"title": title, "page_id": page_id, "changed": True,
            "children_created": children_created,
            "stats": {"isb": stats.isb_replaced, "sb": stats.sb_replaced,
                      "legacy": stats.legacy_removed, "ht": stats.ht_replaced},
            "error": None}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 Step 2 (v2): Batch term page conversion with child pages")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--page-id", help="Process a single page")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--batch-delay", type=float, default=1.5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-json", help="Write report to JSON")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)
    space_key = args.space_key
    space_id = api.get_space_id(space_key)

    # Single page mode
    if args.page_id:
        log.info(f"Processing single page: {args.page_id}")
        result = process_page(api, space_id, space_key, args.page_id, args.dry_run)
        mode = "DRY RUN" if args.dry_run else "LIVE"
        print(f"\n  [{mode}] {result['title']} (id={result['page_id']})")
        if result["changed"] and result["stats"]:
            s = result["stats"]
            print(f"    Child pages created: {result['children_created']}")
            print(f"    include-shared-block → excerpt-include: {s['isb']}")
            print(f"    shared-block → child page + excerpt-include: {s['sb']}")
            print(f"    legacy wrappers removed: {s['legacy']}")
            if result["error"]:
                print(f"    ERROR: {result['error']}")
        else:
            print(f"    No changes needed")
        return

    # Full space mode
    log.info(f"Collecting pages in space '{space_key}'...")
    all_pages = api.get_all_pages(space_id)
    log.info(f"Found {len(all_pages)} pages total")

    pages_to_process = [
        (pid, t) for pid, t in all_pages
        if pid not in SKIP_PAGE_IDS and not t.startswith("_")
    ]
    log.info(f"Pages to process: {len(pages_to_process)}")

    if args.limit:
        pages_to_process = pages_to_process[:args.limit]
        log.info(f"Limited to {args.limit}")

    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0,
              "children_created": 0, "isb": 0, "sb": 0, "legacy": 0, "ht": 0}
    all_results = []
    batch_size = args.batch_size
    total = len(pages_to_process)

    for batch_start in range(0, total, batch_size):
        batch = pages_to_process[batch_start:batch_start + batch_size]
        batch_num = (batch_start // batch_size) + 1
        total_batches = (total + batch_size - 1) // batch_size
        log.info(f"--- Batch {batch_num}/{total_batches} (pages {batch_start+1}-{batch_start+len(batch)}) ---")

        for page_id, page_title in batch:
            totals["scanned"] += 1
            try:
                result = process_page(api, space_id, space_key, page_id, args.dry_run)
                all_results.append(result)

                if result["error"]:
                    totals["errored"] += 1
                elif result["changed"]:
                    totals["modified"] += 1
                    if result["stats"]:
                        totals["children_created"] += result["children_created"]
                        totals["isb"] += result["stats"]["isb"]
                        totals["sb"] += result["stats"]["sb"]
                        totals["legacy"] += result["stats"]["legacy"]
                        totals["ht"] += result["stats"]["ht"]
                else:
                    totals["skipped"] += 1

            except Exception as e:
                totals["errored"] += 1
                all_results.append({"page_id": page_id, "title": page_title,
                                    "changed": False, "error": str(e)})
                log.error(f"  [{page_title}] Error: {e}")

        if batch_start + batch_size < total and args.batch_delay > 0:
            log.info(f"  Waiting {args.batch_delay}s...")
            time.sleep(args.batch_delay)

    # Summary
    mode = "DRY RUN" if args.dry_run else "LIVE RUN"
    print(f"\n{'=' * 65}")
    print(f"  PHASE 2 STEP 2 SUMMARY ({mode})")
    print(f"{'=' * 65}")
    print(f"  Pages scanned:                {totals['scanned']}")
    print(f"  Pages modified:               {totals['modified']}")
    print(f"  Pages with no changes:        {totals['skipped']}")
    print(f"  Pages with errors:            {totals['errored']}")
    print(f"  ---")
    print(f"  Child pages created (excerpts):              {totals['children_created']}")
    print(f"  include-shared-block → excerpt-include:      {totals['isb']}")
    print(f"  shared-block → child page + excerpt-include: {totals['sb']}")
    print(f"  legacy wrappers removed:                     {totals['legacy']}")
    print(f"  help-text replaced:                          {totals['ht']}")
    print(f"{'=' * 65}")

    if totals["errored"] > 0:
        print(f"\n  Pages with errors:")
        for r in all_results:
            if r.get("error"):
                print(f"    - {r['title']} (id={r['page_id']}): {r['error']}")

    if args.output_json:
        path = os.path.expanduser(args.output_json)
        with open(path, "w") as f:
            json.dump({"summary": totals, "pages": all_results}, f, indent=2)
        log.info(f"Report written to {path}")


if __name__ == "__main__":
    main()
