#!/usr/bin/env python3
"""
Phase 2 Audit: Map all shared-block and include-shared-block macros across a space.

Produces a comprehensive dependency report showing:
  - Every shared-block (page, key, content summary)
  - Every include-shared-block (page, source page, key)
  - Every help-text-from-shared-block (page, source page, key)
  - Cross-reference: which include-shared-blocks reference which shared-blocks
  - Orphan detection: shared-blocks with no consumers, includes with no source

Usage:
    python phase2_audit.py --space-key CLOS --output-json ~/Downloads/phase2_audit.json

Requirements:
    pip install requests

Environment variables:
    CONFLUENCE_BASE_URL  — e.g. https://tempus-sandbox.atlassian.net
    CONFLUENCE_EMAIL     — your Atlassian account email
    CONFLUENCE_API_TOKEN — API token
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase2_audit")

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

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

    def get_pages_in_space(self, space_id: str, limit: int = 250, cursor: str = None) -> dict:
        url = f"{self.base_url}/wiki/api/v2/spaces/{space_id}/pages"
        params = {"limit": min(limit, 250), "status": "current", "sort": "id"}
        if cursor:
            params["cursor"] = cursor
        resp = self.session.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

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
            "parentId": data.get("parentId"),
            "body": json.loads(body_raw),
        }


def collect_all_page_ids(api: ConfluenceAPI, space_id: str) -> list:
    all_pages = []
    cursor = None
    while True:
        data = api.get_pages_in_space(space_id, limit=250, cursor=cursor)
        for p in data.get("results", []):
            all_pages.append((p["id"], p["title"]))
        next_link = data.get("_links", {}).get("next")
        if not next_link:
            break
        if "cursor=" in next_link:
            cursor = next_link.split("cursor=")[1].split("&")[0]
        else:
            break
    return all_pages

# ---------------------------------------------------------------------------
# ADF scanning functions
# ---------------------------------------------------------------------------

def extract_text_preview(node, max_len=120):
    """Extract a text preview from an ADF node tree."""
    texts = []
    if node.get("type") == "text":
        texts.append(node.get("text", ""))
    if node.get("type") == "placeholder":
        texts.append(f"[placeholder: {node.get('attrs', {}).get('text', '')[:60]}]")
    for child in node.get("content", []):
        texts.extend(_collect_text(child))
    combined = " ".join(texts).strip()
    if len(combined) > max_len:
        combined = combined[:max_len] + "..."
    return combined


def _collect_text(node):
    texts = []
    if node.get("type") == "text":
        texts.append(node.get("text", ""))
    if node.get("type") == "placeholder":
        texts.append(f"[placeholder]")
    for child in node.get("content", []):
        texts.extend(_collect_text(child))
    return texts


def get_macro_param(node, param_name):
    """Get a macro parameter value from an extension node."""
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    if isinstance(val, dict):
        return val.get("value", "")
    return val


def scan_node(node, page_id, page_title, results, inside_legacy=False, inside_aura_tab=False, path=""):
    """
    Recursively scan an ADF node for shared-block, include-shared-block,
    and help-text-from-shared-block macros.
    Also tracks whether we're inside a legacy-content wrapper or Aura tab.
    """
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # Check if we're entering a legacy-content wrapper
    is_legacy = (
        ext_type == "com.atlassian.confluence.migration"
        and ext_key == "legacy-content"
    )

    # Check if we're entering an Aura tab
    is_aura_tab = ext_key in ("aura-tab", "aura-tab-migratable")
    is_aura_tab_collection = ext_key in ("aura-tab-collection", "aura-tab-collection-migratable")

    # --- Shared Block (source/definition) ---
    if ext_key == "shared-block":
        shared_key = get_macro_param(node, "shared-block-key")
        content_preview = extract_text_preview(node)
        content_children = len(node.get("content", []))
        results["shared_blocks"].append({
            "page_id": page_id,
            "page_title": page_title,
            "shared_block_key": shared_key,
            "content_preview": content_preview,
            "content_children_count": content_children,
            "inside_legacy_wrapper": inside_legacy,
            "inside_aura_tab": inside_aura_tab,
            "path": path,
        })

    # --- Include Shared Block (consumer/reference) ---
    elif ext_key == "include-shared-block":
        shared_key = get_macro_param(node, "shared-block-key")
        source_page = get_macro_param(node, "page")
        results["include_shared_blocks"].append({
            "page_id": page_id,
            "page_title": page_title,
            "shared_block_key": shared_key,
            "source_page_title": source_page,
            "inside_legacy_wrapper": inside_legacy,
            "inside_aura_tab": inside_aura_tab,
            "path": path,
        })

    # --- Help Text From Shared Block ---
    elif ext_key == "help-text-from-shared-block":
        shared_key = get_macro_param(node, "shared-block-key")
        source_page = get_macro_param(node, "page")
        results["help_text_from_shared_blocks"].append({
            "page_id": page_id,
            "page_title": page_title,
            "shared_block_key": shared_key,
            "source_page_title": source_page,
            "inside_legacy_wrapper": inside_legacy,
            "inside_aura_tab": inside_aura_tab,
            "path": path,
        })

    # --- Excerpt (native, already existing) ---
    elif ext_key == "excerpt":
        excerpt_name = get_macro_param(node, "atlassian-macro-output-type") or get_macro_param(node, "name") or ""
        results["excerpts"].append({
            "page_id": page_id,
            "page_title": page_title,
            "excerpt_name": excerpt_name,
            "path": path,
        })

    # --- Excerpt Include (native, already existing) ---
    elif ext_key == "excerpt-include":
        results["excerpt_includes"].append({
            "page_id": page_id,
            "page_title": page_title,
            "path": path,
        })

    # --- Include Page macro ---
    elif ext_key == "include":
        included_page = get_macro_param(node, "")
        results["include_pages"].append({
            "page_id": page_id,
            "page_title": page_title,
            "included_page_title": included_page,
            "path": path,
        })

    # Recurse into content
    if "content" in node and isinstance(node["content"], list):
        for i, child in enumerate(node["content"]):
            child_path = f"{path}/{node_type}[{i}]"
            scan_node(
                child, page_id, page_title, results,
                inside_legacy=inside_legacy or is_legacy,
                inside_aura_tab=inside_aura_tab or is_aura_tab,
                path=child_path,
            )

    # Also recurse into legacy-content nestedContent
    if is_legacy:
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            for i, child in enumerate(nested["content"]):
                child_path = f"{path}/nestedContent[{i}]"
                scan_node(
                    child, page_id, page_title, results,
                    inside_legacy=True,
                    inside_aura_tab=inside_aura_tab,
                    path=child_path,
                )


def scan_page(page_data: dict, results: dict):
    """Scan an entire page body for macros."""
    body = page_data["body"]
    if "content" in body:
        for i, child in enumerate(body["content"]):
            scan_node(
                child,
                page_data["id"],
                page_data["title"],
                results,
                path=f"/doc[{i}]",
            )

# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def analyze_results(results: dict) -> dict:
    """Produce cross-reference analysis from raw scan results."""
    analysis = {}

    # Group shared-blocks by (page_title, key)
    sb_by_source = {}
    for sb in results["shared_blocks"]:
        key = (sb["page_title"], sb["shared_block_key"])
        sb_by_source.setdefault(key, []).append(sb)

    # Group include-shared-blocks by (source_page_title, key)
    isb_by_target = {}
    for isb in results["include_shared_blocks"]:
        key = (isb["source_page_title"], isb["shared_block_key"])
        isb_by_target.setdefault(key, []).append(isb)

    # Cross-reference: for each shared-block, find its consumers
    cross_refs = []
    for (page_title, sb_key), sbs in sb_by_source.items():
        consumers = isb_by_target.get((page_title, sb_key), [])
        cross_refs.append({
            "source_page": page_title,
            "shared_block_key": sb_key,
            "source_count": len(sbs),
            "consumer_count": len(consumers),
            "consumer_pages": sorted(set(c["page_title"] for c in consumers)),
            "inside_aura_tab": any(sb["inside_aura_tab"] for sb in sbs),
            "inside_legacy": any(sb["inside_legacy_wrapper"] for sb in sbs),
        })

    # Find orphan include-shared-blocks (no matching shared-block in this space)
    orphan_includes = []
    for (source_page, sb_key), isbs in isb_by_target.items():
        if (source_page, sb_key) not in sb_by_source:
            # Check if the source page exists under a different title match
            orphan_includes.append({
                "source_page_title": source_page,
                "shared_block_key": sb_key,
                "consumer_count": len(isbs),
                "consumer_pages": sorted(set(i["page_title"] for i in isbs)),
            })

    # Find shared-blocks with no consumers
    unused_blocks = []
    for (page_title, sb_key), sbs in sb_by_source.items():
        consumers = isb_by_target.get((page_title, sb_key), [])
        if len(consumers) == 0:
            unused_blocks.append({
                "page_title": page_title,
                "shared_block_key": sb_key,
                "content_preview": sbs[0]["content_preview"],
            })

    # Unique shared-block keys
    unique_keys = sorted(set(sb["shared_block_key"] for sb in results["shared_blocks"]))

    # Source pages (pages that define shared-blocks)
    source_pages = {}
    for sb in results["shared_blocks"]:
        source_pages.setdefault(sb["page_title"], {
            "page_id": sb["page_id"],
            "keys": set(),
        })
        source_pages[sb["page_title"]]["keys"].add(sb["shared_block_key"])
    for sp in source_pages.values():
        sp["keys"] = sorted(sp["keys"])

    analysis["cross_references"] = sorted(cross_refs, key=lambda x: (-x["consumer_count"], x["source_page"]))
    analysis["orphan_includes"] = orphan_includes
    analysis["unused_blocks"] = unused_blocks
    analysis["unique_shared_block_keys"] = unique_keys
    analysis["source_pages"] = {k: v for k, v in sorted(source_pages.items())}

    return analysis

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 Audit: Shared-Block Dependency Map")
    parser.add_argument("--space-key", required=True, help="Confluence space key")
    parser.add_argument("--page-id", help="Audit a single page only")
    parser.add_argument("--batch-size", type=int, default=25, help="Pages per batch")
    parser.add_argument("--batch-delay", type=float, default=0.5, help="Seconds between batches")
    parser.add_argument("--limit", type=int, help="Only process first N pages")
    parser.add_argument("--output-json", required=True, help="Output JSON report path")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)

    results = {
        "shared_blocks": [],
        "include_shared_blocks": [],
        "help_text_from_shared_blocks": [],
        "excerpts": [],
        "excerpt_includes": [],
        "include_pages": [],
    }

    if args.page_id:
        log.info(f"Auditing single page: {args.page_id}")
        page_data = api.get_page_body(args.page_id)
        scan_page(page_data, results)
    else:
        log.info(f"Looking up space '{args.space_key}'...")
        space_id = api.get_space_id(args.space_key)
        log.info(f"Space ID: {space_id}")

        log.info("Collecting all pages...")
        all_pages = collect_all_page_ids(api, space_id)
        log.info(f"Found {len(all_pages)} pages")

        if args.limit:
            all_pages = all_pages[:args.limit]
            log.info(f"Limited to {args.limit} pages")

        batch_size = args.batch_size
        total = len(all_pages)
        errors = 0

        for batch_start in range(0, total, batch_size):
            batch_end = min(batch_start + batch_size, total)
            batch = all_pages[batch_start:batch_end]
            batch_num = (batch_start // batch_size) + 1
            total_batches = (total + batch_size - 1) // batch_size

            log.info(f"Batch {batch_num}/{total_batches} (pages {batch_start + 1}-{batch_end})")

            for page_id, page_title in batch:
                try:
                    page_data = api.get_page_body(page_id)
                    scan_page(page_data, results)
                except Exception as e:
                    log.error(f"  Error on {page_title} ({page_id}): {e}")
                    errors += 1

            if batch_end < total and args.batch_delay > 0:
                time.sleep(args.batch_delay)

        log.info(f"Scan complete. Errors: {errors}")

    # Analyze
    analysis = analyze_results(results)

    # Build report
    report = {
        "summary": {
            "total_shared_blocks": len(results["shared_blocks"]),
            "total_include_shared_blocks": len(results["include_shared_blocks"]),
            "total_help_text_from_shared_blocks": len(results["help_text_from_shared_blocks"]),
            "total_native_excerpts": len(results["excerpts"]),
            "total_native_excerpt_includes": len(results["excerpt_includes"]),
            "total_include_pages": len(results["include_pages"]),
            "unique_shared_block_keys": len(analysis["unique_shared_block_keys"]),
            "source_pages_count": len(analysis["source_pages"]),
            "orphan_includes_count": len(analysis["orphan_includes"]),
            "unused_blocks_count": len(analysis["unused_blocks"]),
        },
        "analysis": analysis,
        "raw": results,
    }

    # Write report
    output_path = os.path.expanduser(args.output_json)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"Report written to {output_path}")

    # Print summary
    print("\n" + "=" * 70)
    print("  PHASE 2 AUDIT SUMMARY")
    print("=" * 70)
    print(f"  Shared Blocks (definitions):      {report['summary']['total_shared_blocks']}")
    print(f"  Include Shared Blocks (consumers): {report['summary']['total_include_shared_blocks']}")
    print(f"  Help-Text-From-Shared-Block:       {report['summary']['total_help_text_from_shared_blocks']}")
    print(f"  Native Excerpts (already exist):   {report['summary']['total_native_excerpts']}")
    print(f"  Native Excerpt-Includes:           {report['summary']['total_native_excerpt_includes']}")
    print(f"  Include Page macros:               {report['summary']['total_include_pages']}")
    print(f"  ---")
    print(f"  Unique shared-block keys:          {report['summary']['unique_shared_block_keys']}")
    print(f"  Pages that define shared-blocks:   {report['summary']['source_pages_count']}")
    print(f"  Orphan includes (no source found): {report['summary']['orphan_includes_count']}")
    print(f"  Unused blocks (no consumers):      {report['summary']['unused_blocks_count']}")
    print("=" * 70)

    # Top cross-references
    print("\n  Top shared-blocks by consumer count:")
    for cr in analysis["cross_references"][:15]:
        in_tab = " [IN AURA TAB]" if cr["inside_aura_tab"] else ""
        in_legacy = " [IN LEGACY]" if cr["inside_legacy"] else ""
        print(
            f"    {cr['source_page']}/{cr['shared_block_key']}: "
            f"{cr['consumer_count']} consumers{in_tab}{in_legacy}"
        )

    # Source pages
    print(f"\n  Pages that define shared-blocks:")
    for page_title, info in analysis["source_pages"].items():
        print(f"    {page_title} (id={info['page_id']}): {', '.join(info['keys'])}")

    # Orphans
    if analysis["orphan_includes"]:
        print(f"\n  Orphan include-shared-blocks (source not found in space):")
        for o in analysis["orphan_includes"]:
            print(
                f"    source='{o['source_page_title']}' key='{o['shared_block_key']}': "
                f"{o['consumer_count']} consumers on {o['consumer_pages'][:3]}..."
            )

    print()


if __name__ == "__main__":
    main()
