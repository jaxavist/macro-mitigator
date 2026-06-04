#!/usr/bin/env python3
"""
Phase 0: Legacy Unwrap & Macro Normalization
=============================================
Strips legacy-content migration wrappers and normalizes migrated Aura macros
across a Confluence Cloud space.

Operations performed:
1. Unwrap legacy-content nodes — promote nestedContent ADF to replace the wrapper
2. Rename aura-tab-collection-migratable → aura-tab-collection
3. Rename aura-tab-migratable → aura-tab
4. Fix metadata titles ("Migrated Tab Group" → "Tab Group", "Migrated Tab" → "Tab")
5. Catalog page-info macros marked "Migrated Page Info - Unsupported"

Usage:
    # Dry run on entire space (no changes made, generates report)
    python phase0_legacy_unwrap.py --space-key CLOS --dry-run

    # Process a single page (good for testing)
    python phase0_legacy_unwrap.py --space-key CLOS --page-id 106594993

    # Process all pages in batches of 10
    python phase0_legacy_unwrap.py --space-key CLOS --batch-size 10

    # Process only the first N pages (for incremental rollout)
    python phase0_legacy_unwrap.py --space-key CLOS --limit 5

Requirements:
    pip install requests

Environment variables:
    CONFLUENCE_BASE_URL  — e.g. https://tempus-sandbox.atlassian.net
    CONFLUENCE_EMAIL     — your Atlassian account email
    CONFLUENCE_API_TOKEN — API token from https://id.atlassian.com/manage-profile/security/api-tokens
"""

import argparse
import copy
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase0")

# ---------------------------------------------------------------------------
# Data classes for tracking
# ---------------------------------------------------------------------------

@dataclass
class PageReport:
    page_id: str
    title: str
    legacy_wrappers_removed: int = 0
    aura_tab_collections_renamed: int = 0
    aura_tabs_renamed: int = 0
    migrated_page_info_found: int = 0
    changed: bool = False
    error: Optional[str] = None

@dataclass
class RunReport:
    pages_scanned: int = 0
    pages_modified: int = 0
    pages_skipped: int = 0
    pages_errored: int = 0
    total_legacy_wrappers: int = 0
    total_aura_tab_collections: int = 0
    total_aura_tabs: int = 0
    total_migrated_page_info: int = 0
    page_reports: list = field(default_factory=list)

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
            "body": json.loads(body_raw),
        }

    def update_page_body(self, page_id: str, title: str, version: int, body: dict) -> dict:
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
                "message": "Phase 0: Legacy unwrap & macro normalization",
            },
        }
        resp = self.session.put(url, json=payload)
        resp.raise_for_status()
        return resp.json()

# ---------------------------------------------------------------------------
# ADF transformation functions
# ---------------------------------------------------------------------------

def unwrap_legacy_content(node: dict, report: PageReport) -> list:
    """
    If this node is a legacy-content wrapper, return the children from its
    nestedContent doc. Otherwise return the node as-is (in a list).
    """
    if (
        node.get("type") in ("extension", "bodiedExtension")
        and node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"
    ):
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            report.legacy_wrappers_removed += 1
            promoted = []
            for child in nested["content"]:
                promoted.extend(transform_node(child, report))
            return promoted
        # Fallback: check if parameters is at top level
        params = node.get("parameters", {})
        nested = params.get("nestedContent", {})
        if nested and nested.get("content"):
            report.legacy_wrappers_removed += 1
            promoted = []
            for child in nested["content"]:
                promoted.extend(transform_node(child, report))
            return promoted
        # No nestedContent — return empty (drop the broken wrapper)
        log.warning(f"  Legacy wrapper with no nestedContent found, dropping it")
        report.legacy_wrappers_removed += 1
        return []
    return None  # Signal: not a legacy wrapper


def rename_aura_macros(node: dict, report: PageReport):
    """Rename migrated Aura tab macros to their Cloud-native equivalents."""
    if node.get("type") not in ("extension", "bodiedExtension", "inlineExtension"):
        return

    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    params = attrs.get("parameters", {})
    metadata = params.get("macroMetadata", {})

    if ext_key == "aura-tab-collection-migratable":
        attrs["extensionKey"] = "aura-tab-collection"
        title_obj = metadata.get("title")
        if title_obj == "Migrated Tab Group" or title_obj == "Migrated Tab Group":
            metadata["title"] = "Tab Group"
        report.aura_tab_collections_renamed += 1

    elif ext_key == "aura-tab-migratable":
        attrs["extensionKey"] = "aura-tab"
        title_obj = metadata.get("title")
        if title_obj == "Migrated Tab" or title_obj == "Migrated Tab":
            metadata["title"] = "Tab"
        report.aura_tabs_renamed += 1


def catalog_migrated_page_info(node: dict, report: PageReport):
    """Flag page-info macros marked as unsupported migration artifacts."""
    if node.get("type") not in ("extension", "bodiedExtension", "inlineExtension"):
        return

    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    params = attrs.get("parameters", {})
    metadata = params.get("macroMetadata", {})

    if ext_key == "page-info":
        title = metadata.get("title", "")
        if "Migrated" in title or "Unsupported" in title:
            report.migrated_page_info_found += 1


def transform_node(node: dict, report: PageReport) -> list:
    """
    Recursively transform a single ADF node.
    Returns a list of nodes (usually 1, but legacy unwrap can produce multiple).
    """
    # First, check if this is a legacy wrapper
    unwrapped = unwrap_legacy_content(node, report)
    if unwrapped is not None:
        return unwrapped

    # Rename Aura macros
    rename_aura_macros(node, report)

    # Catalog migrated page-info
    catalog_migrated_page_info(node, report)

    # Recurse into children
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, report))
        node["content"] = new_content

    return [node]


def transform_body(body: dict, report: PageReport) -> dict:
    """Transform an entire ADF document body."""
    body = copy.deepcopy(body)
    if "content" in body and isinstance(body["content"], list):
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, report))
        body["content"] = new_content
    return body

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def collect_all_page_ids(api: ConfluenceAPI, space_id: str) -> list:
    """Paginate through all pages in a space and return (id, title) tuples."""
    all_pages = []
    cursor = None
    while True:
        data = api.get_pages_in_space(space_id, limit=250, cursor=cursor)
        for p in data.get("results", []):
            all_pages.append((p["id"], p["title"]))
        next_link = data.get("_links", {}).get("next")
        if not next_link:
            break
        # Extract cursor from the next link
        if "cursor=" in next_link:
            cursor = next_link.split("cursor=")[1].split("&")[0]
        else:
            break
    return all_pages


def process_page(api: ConfluenceAPI, page_id: str, dry_run: bool) -> PageReport:
    """Process a single page: fetch, transform, optionally update."""
    page_data = api.get_page_body(page_id)
    report = PageReport(page_id=page_data["id"], title=page_data["title"])

    try:
        original_body = page_data["body"]
        transformed_body = transform_body(original_body, report)

        has_changes = (
            report.legacy_wrappers_removed > 0
            or report.aura_tab_collections_renamed > 0
            or report.aura_tabs_renamed > 0
        )

        if has_changes:
            report.changed = True
            log.info(
                f"  [{page_data['title']}] "
                f"legacy_unwrapped={report.legacy_wrappers_removed}, "
                f"tab_collections_renamed={report.aura_tab_collections_renamed}, "
                f"tabs_renamed={report.aura_tabs_renamed}, "
                f"migrated_page_info={report.migrated_page_info_found}"
            )

            if not dry_run:
                api.update_page_body(
                    page_id=page_data["id"],
                    title=page_data["title"],
                    version=page_data["version"],
                    body=transformed_body,
                )
                log.info(f"  [{page_data['title']}] Updated successfully (v{page_data['version'] + 1})")
            else:
                log.info(f"  [{page_data['title']}] DRY RUN — would update")
        else:
            if report.migrated_page_info_found > 0:
                log.info(
                    f"  [{page_data['title']}] No legacy/aura changes, "
                    f"but found {report.migrated_page_info_found} migrated page-info macro(s)"
                )

    except Exception as e:
        report.error = str(e)
        log.error(f"  [{page_data['title']}] Error: {e}")

    return report


def print_summary(run_report: RunReport, dry_run: bool):
    """Print a summary of the run."""
    print("\n" + "=" * 70)
    print(f"  PHASE 0 SUMMARY {'(DRY RUN)' if dry_run else '(LIVE RUN)'}")
    print("=" * 70)
    print(f"  Pages scanned:              {run_report.pages_scanned}")
    print(f"  Pages modified:             {run_report.pages_modified}")
    print(f"  Pages with no changes:      {run_report.pages_skipped}")
    print(f"  Pages with errors:          {run_report.pages_errored}")
    print(f"  ---")
    print(f"  Legacy wrappers removed:    {run_report.total_legacy_wrappers}")
    print(f"  Aura Tab Collections fixed: {run_report.total_aura_tab_collections}")
    print(f"  Aura Tabs fixed:            {run_report.total_aura_tabs}")
    print(f"  Migrated Page-Info found:   {run_report.total_migrated_page_info}")
    print("=" * 70)

    # Pages with changes
    changed_pages = [r for r in run_report.page_reports if r.changed]
    if changed_pages:
        print(f"\n  Pages {'that would be' if dry_run else ''} modified:")
        for r in changed_pages:
            print(
                f"    - {r.title} (id={r.page_id}): "
                f"legacy={r.legacy_wrappers_removed}, "
                f"tab_collections={r.aura_tab_collections_renamed}, "
                f"tabs={r.aura_tabs_renamed}"
            )

    # Pages with migrated page-info (no other changes)
    info_only = [r for r in run_report.page_reports if not r.changed and r.migrated_page_info_found > 0]
    if info_only:
        print(f"\n  Pages with migrated page-info macros (no other changes):")
        for r in info_only:
            print(f"    - {r.title} (id={r.page_id}): {r.migrated_page_info_found} macro(s)")

    # Errors
    error_pages = [r for r in run_report.page_reports if r.error]
    if error_pages:
        print(f"\n  Pages with errors:")
        for r in error_pages:
            print(f"    - {r.title} (id={r.page_id}): {r.error}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Phase 0: Legacy Unwrap & Macro Normalization for Confluence Cloud"
    )
    parser.add_argument("--space-key", required=True, help="Confluence space key (e.g. CLOS)")
    parser.add_argument("--page-id", help="Process a single page by ID (for testing)")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without applying them")
    parser.add_argument("--batch-size", type=int, default=25, help="Pages per batch (default: 25)")
    parser.add_argument("--batch-delay", type=float, default=1.0, help="Seconds between batches (default: 1.0)")
    parser.add_argument("--limit", type=int, help="Only process the first N pages")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    parser.add_argument("--output-json", help="Write detailed report to JSON file")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Validate environment
    if not BASE_URL:
        print("ERROR: Set CONFLUENCE_BASE_URL environment variable")
        print("  e.g., export CONFLUENCE_BASE_URL=https://tempus-sandbox.atlassian.net")
        sys.exit(1)
    if not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN environment variables")
        print("  e.g., export CONFLUENCE_EMAIL=you@example.com")
        print("  e.g., export CONFLUENCE_API_TOKEN=your-api-token")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)
    run_report = RunReport()

    if args.dry_run:
        log.info("DRY RUN MODE — no changes will be made")

    # Single page mode
    if args.page_id:
        log.info(f"Processing single page: {args.page_id}")
        report = process_page(api, args.page_id, args.dry_run)
        run_report.pages_scanned = 1
        run_report.page_reports.append(report)
        if report.changed:
            run_report.pages_modified = 1
        elif report.error:
            run_report.pages_errored = 1
        else:
            run_report.pages_skipped = 1
        run_report.total_legacy_wrappers = report.legacy_wrappers_removed
        run_report.total_aura_tab_collections = report.aura_tab_collections_renamed
        run_report.total_aura_tabs = report.aura_tabs_renamed
        run_report.total_migrated_page_info = report.migrated_page_info_found
        print_summary(run_report, args.dry_run)
        if args.output_json:
            write_json_report(run_report, args.output_json)
        return

    # Full space mode
    log.info(f"Looking up space '{args.space_key}'...")
    space_id = api.get_space_id(args.space_key)
    log.info(f"Space ID: {space_id}")

    log.info("Collecting all pages in space...")
    all_pages = collect_all_page_ids(api, space_id)
    log.info(f"Found {len(all_pages)} pages")

    if args.limit:
        all_pages = all_pages[:args.limit]
        log.info(f"Limited to first {args.limit} pages")

    # Process in batches
    batch_size = args.batch_size
    total_pages = len(all_pages)

    for batch_start in range(0, total_pages, batch_size):
        batch_end = min(batch_start + batch_size, total_pages)
        batch = all_pages[batch_start:batch_end]
        batch_num = (batch_start // batch_size) + 1
        total_batches = (total_pages + batch_size - 1) // batch_size

        log.info(f"--- Batch {batch_num}/{total_batches} (pages {batch_start + 1}-{batch_end} of {total_pages}) ---")

        for page_id, page_title in batch:
            run_report.pages_scanned += 1
            try:
                report = process_page(api, page_id, args.dry_run)
                run_report.page_reports.append(report)

                if report.error:
                    run_report.pages_errored += 1
                elif report.changed:
                    run_report.pages_modified += 1
                else:
                    run_report.pages_skipped += 1

                run_report.total_legacy_wrappers += report.legacy_wrappers_removed
                run_report.total_aura_tab_collections += report.aura_tab_collections_renamed
                run_report.total_aura_tabs += report.aura_tabs_renamed
                run_report.total_migrated_page_info += report.migrated_page_info_found

            except Exception as e:
                log.error(f"  Failed to process page {page_id} ({page_title}): {e}")
                run_report.pages_errored += 1
                run_report.page_reports.append(
                    PageReport(page_id=page_id, title=page_title, error=str(e))
                )

        # Delay between batches (except after the last one)
        if batch_end < total_pages and args.batch_delay > 0:
            log.info(f"  Waiting {args.batch_delay}s before next batch...")
            time.sleep(args.batch_delay)

    print_summary(run_report, args.dry_run)

    if args.output_json:
        write_json_report(run_report, args.output_json)


def write_json_report(run_report: RunReport, filepath: str):
    """Write detailed report to a JSON file."""
    data = {
        "summary": {
            "pages_scanned": run_report.pages_scanned,
            "pages_modified": run_report.pages_modified,
            "pages_skipped": run_report.pages_skipped,
            "pages_errored": run_report.pages_errored,
            "total_legacy_wrappers": run_report.total_legacy_wrappers,
            "total_aura_tab_collections": run_report.total_aura_tab_collections,
            "total_aura_tabs": run_report.total_aura_tabs,
            "total_migrated_page_info": run_report.total_migrated_page_info,
        },
        "pages": [
            {
                "page_id": r.page_id,
                "title": r.title,
                "changed": r.changed,
                "legacy_wrappers_removed": r.legacy_wrappers_removed,
                "aura_tab_collections_renamed": r.aura_tab_collections_renamed,
                "aura_tabs_renamed": r.aura_tabs_renamed,
                "migrated_page_info_found": r.migrated_page_info_found,
                "error": r.error,
            }
            for r in run_report.page_reports
        ],
    }
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    log.info(f"Detailed report written to {filepath}")


if __name__ == "__main__":
    main()
