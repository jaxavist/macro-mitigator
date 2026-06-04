#!/usr/bin/env python3
"""
Phase 2 Step 2: Batch convert term pages.

For each term page in the space:
  1. Replace include-shared-block macros with excerpt-include macros
     pointing to the Template Resources child pages
  2. Replace shared-block macros with their inner content (unwrap)
  3. Clean up any remaining legacy-content wrappers (Phase 0 leftovers)

Usage:
    # Dry run on a single page
    python phase2_step2_term_pages.py --space-key CLOS --page-id 106594993 --dry-run

    # Dry run on full space
    python phase2_step2_term_pages.py --space-key CLOS --dry-run

    # Live run
    python phase2_step2_term_pages.py --space-key CLOS --batch-size 20

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
# Mapping: include-shared-block references → excerpt-include targets
# These are the child pages created in Step 1 under Glossary Template Resources
# ---------------------------------------------------------------------------

INCLUDE_SHARED_BLOCK_MAP = {
    ("Glossary Template Resources", "Definition(s)"): "_Definition(s)",
    ("Glossary Template Resources", "Internal Resources"): "_Internal Resources",
    ("Glossary Template Resources", "External Resources"): "_External Resources",
    ("Glossary Template Resources", "Other Data"): "_Other Data",
    ("Glossary Template Resources", "GlossaryHelpText"): "_GlossaryHelpText",
}

# Pages to SKIP (special pages handled separately)
SKIP_PAGE_IDS = {
    "106594481",   # Glossary Template Resources (already converted)
    "107848258",   # Glossary Overview and How-To (Step 3)
    "106594311",   # Glossary Home Page
    "107741227",   # Space overview page
    "106594402",   # _Glossary Main Directory
    "106594537",   # Glossary Libraries
}

# Also skip the child pages we created
SKIP_TITLES_PREFIX = "_"  # Pages starting with _ are our excerpt child pages


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
            "body": json.loads(body_raw),
        }

    def update_page(self, page_id, title, version, body, message=""):
        msg = message or "Phase 2: shared-block to excerpt conversion"
        # Try v2 first
        resp = self.session.put(
            f"{self.base_url}/wiki/api/v2/pages/{page_id}",
            json={
                "id": page_id, "status": "current", "title": title,
                "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
                "version": {"number": version + 1, "message": msg},
            }
        )
        if resp.ok:
            return True, version + 1
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
                return True, version + 1
            log.error(f"  v1 API also failed for '{title}': {resp2.status_code}")
            return False, version
        log.error(f"  Update failed for '{title}': {resp.status_code}")
        try:
            detail = resp.json()
            log.error(f"  {json.dumps(detail)[:300]}")
        except Exception:
            pass
        return False, version


# ---------------------------------------------------------------------------
# ADF transformation
# ---------------------------------------------------------------------------

def get_macro_param(node, param_name):
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    if isinstance(val, dict):
        return val.get("value", "")
    return val


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


class PageStats:
    def __init__(self):
        self.include_shared_blocks_replaced = 0
        self.shared_blocks_unwrapped = 0
        self.legacy_wrappers_removed = 0
        self.help_text_replaced = 0

    @property
    def has_changes(self):
        return (self.include_shared_blocks_replaced > 0
                or self.shared_blocks_unwrapped > 0
                or self.legacy_wrappers_removed > 0
                or self.help_text_replaced > 0)


def transform_node(node, stats, space_key):
    """
    Transform a node, returning a list of replacement nodes.
    Handles: include-shared-block → excerpt-include,
             shared-block → unwrap content,
             legacy-content → unwrap nestedContent,
             help-text-from-shared-block → excerpt-include
    """
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # --- include-shared-block → excerpt-include ---
    if ext_key == "include-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        map_key = (source_page, sb_key)

        if map_key in INCLUDE_SHARED_BLOCK_MAP:
            child_title = INCLUDE_SHARED_BLOCK_MAP[map_key]
            stats.include_shared_blocks_replaced += 1
            return [make_excerpt_include(child_title, space_key)]
        # Unknown source — leave as-is
        return [node]

    # --- help-text-from-shared-block → excerpt-include ---
    if ext_key == "help-text-from-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        map_key = (source_page, sb_key)

        if map_key in INCLUDE_SHARED_BLOCK_MAP:
            child_title = INCLUDE_SHARED_BLOCK_MAP[map_key]
            stats.help_text_replaced += 1
            return [make_excerpt_include(child_title, space_key)]
        return [node]

    # --- shared-block → unwrap content ---
    if ext_key == "shared-block" and node_type == "bodiedExtension":
        content = node.get("content", [])
        stats.shared_blocks_unwrapped += 1
        if content:
            result = []
            for child in content:
                result.extend(transform_node(child, stats, space_key))
            return result
        return [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]

    # --- legacy-content → unwrap nestedContent ---
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            stats.legacy_wrappers_removed += 1
            result = []
            for child in nested["content"]:
                result.extend(transform_node(child, stats, space_key))
            return result
        stats.legacy_wrappers_removed += 1
        return []

    # --- Recurse into children ---
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats, space_key))
        node["content"] = new_content

    return [node]


def transform_body(body, space_key):
    body = copy.deepcopy(body)
    stats = PageStats()
    if "content" in body and isinstance(body["content"], list):
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, stats, space_key))
        body["content"] = new_content
    return body, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 Step 2: Batch term page conversion")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--page-id", help="Process a single page (for testing)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--batch-delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int, help="Process only first N pages")
    parser.add_argument("--output-json", help="Write report to JSON file")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)
    space_key = args.space_key

    # Single page mode
    if args.page_id:
        page = api.get_page_body(args.page_id)
        log.info(f"Processing: {page['title']} (v{page['version']})")
        new_body, stats = transform_body(page["body"], space_key)
        log.info(f"  include-shared-blocks replaced: {stats.include_shared_blocks_replaced}")
        log.info(f"  shared-blocks unwrapped: {stats.shared_blocks_unwrapped}")
        log.info(f"  legacy wrappers removed: {stats.legacy_wrappers_removed}")
        log.info(f"  help-text replaced: {stats.help_text_replaced}")

        if stats.has_changes and not args.dry_run:
            ok, _ = api.update_page(page["id"], page["title"], page["version"], new_body)
            if ok:
                log.info(f"  Updated successfully")
            else:
                log.error(f"  Update failed")
        elif stats.has_changes:
            log.info(f"  DRY RUN — would update")
        else:
            log.info(f"  No changes needed")
        return

    # Full space mode
    log.info(f"Looking up space '{space_key}'...")
    space_id = api.get_space_id(space_key)

    log.info("Collecting all pages...")
    all_pages = api.get_all_pages(space_id)
    log.info(f"Found {len(all_pages)} pages total")

    # Filter out skip pages and excerpt child pages
    pages_to_process = [
        (pid, title) for pid, title in all_pages
        if pid not in SKIP_PAGE_IDS
        and not title.startswith(SKIP_TITLES_PREFIX)
    ]
    log.info(f"Pages to process (after filtering): {len(pages_to_process)}")

    if args.limit:
        pages_to_process = pages_to_process[:args.limit]
        log.info(f"Limited to {args.limit} pages")

    # Process
    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0,
              "isb_replaced": 0, "sb_unwrapped": 0, "legacy_removed": 0, "ht_replaced": 0}
    report_pages = []
    batch_size = args.batch_size

    for batch_start in range(0, len(pages_to_process), batch_size):
        batch = pages_to_process[batch_start:batch_start + batch_size]
        batch_num = (batch_start // batch_size) + 1
        total_batches = (len(pages_to_process) + batch_size - 1) // batch_size
        log.info(f"--- Batch {batch_num}/{total_batches} ---")

        for page_id, page_title in batch:
            totals["scanned"] += 1
            try:
                page = api.get_page_body(page_id)
                new_body, stats = transform_body(page["body"], space_key)

                page_report = {
                    "page_id": page_id, "title": page_title,
                    "isb": stats.include_shared_blocks_replaced,
                    "sb": stats.shared_blocks_unwrapped,
                    "legacy": stats.legacy_wrappers_removed,
                    "ht": stats.help_text_replaced,
                    "changed": stats.has_changes, "error": None,
                }

                if stats.has_changes:
                    if args.dry_run:
                        log.debug(f"  [{page_title}] DRY RUN: isb={stats.include_shared_blocks_replaced} sb={stats.shared_blocks_unwrapped} legacy={stats.legacy_wrappers_removed}")
                        totals["modified"] += 1
                    else:
                        ok, _ = api.update_page(page_id, page_title, page["version"], new_body)
                        if ok:
                            totals["modified"] += 1
                            log.debug(f"  [{page_title}] Updated")
                        else:
                            totals["errored"] += 1
                            page_report["error"] = "Update failed"
                else:
                    totals["skipped"] += 1

                totals["isb_replaced"] += stats.include_shared_blocks_replaced
                totals["sb_unwrapped"] += stats.shared_blocks_unwrapped
                totals["legacy_removed"] += stats.legacy_wrappers_removed
                totals["ht_replaced"] += stats.help_text_replaced
                report_pages.append(page_report)

            except Exception as e:
                totals["errored"] += 1
                report_pages.append({
                    "page_id": page_id, "title": page_title,
                    "changed": False, "error": str(e),
                })
                log.error(f"  [{page_title}] Error: {e}")

        if batch_start + batch_size < len(pages_to_process) and args.batch_delay > 0:
            time.sleep(args.batch_delay)

    # Summary
    mode = "DRY RUN" if args.dry_run else "LIVE RUN"
    print(f"\n{'=' * 60}")
    print(f"  PHASE 2 STEP 2 SUMMARY ({mode})")
    print(f"{'=' * 60}")
    print(f"  Pages scanned:              {totals['scanned']}")
    print(f"  Pages modified:             {totals['modified']}")
    print(f"  Pages with no changes:      {totals['skipped']}")
    print(f"  Pages with errors:          {totals['errored']}")
    print(f"  ---")
    print(f"  include-shared-blocks → excerpt-include: {totals['isb_replaced']}")
    print(f"  shared-blocks unwrapped:                 {totals['sb_unwrapped']}")
    print(f"  legacy wrappers removed:                 {totals['legacy_removed']}")
    print(f"  help-text-from-shared-block replaced:    {totals['ht_replaced']}")
    print(f"{'=' * 60}")

    if totals["errored"] > 0:
        print(f"\n  Pages with errors:")
        for r in report_pages:
            if r.get("error"):
                print(f"    - {r['title']} (id={r['page_id']}): {r['error']}")

    if args.output_json:
        report = {"summary": totals, "pages": report_pages}
        path = os.path.expanduser(args.output_json)
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        log.info(f"Report written to {path}")


if __name__ == "__main__":
    main()
