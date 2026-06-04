#!/usr/bin/env python3
"""
Post-NBM Verification Scanner
==============================
Scans specified spaces and reports the current state of all migration-relevant
macros. Compares what Atlassian claimed to transform vs what actually exists.

Checks for:
  - widget macros (should have been removed by Atlassian)
  - page-info / Migrated Page Info (should be sr-page-info-macro)
  - link-to-window (should have been removed)
  - multiexcerpt / multiexcerpt-include (should be native excerpt)
  - viewppt (should be view-file)
  - legacy-content wrappers (still present?)
  - shared-block / include-shared-block (our scope)
  - help-text / help-text-from-shared-block (our scope)
  - aura-tab-migratable (missing titles/content?)
  - button macros
  - extra-table-properties

Usage:
    # Scan a single space
    python verify_nbm.py --space-keys GLOS --output-json verify_glos.json

    # Scan all priority spaces
    python verify_nbm.py --space-keys GLOS POL PROD NBU FIXIT ACE --output-json verify_all.json

    # Scan with page-level detail
    python verify_nbm.py --space-keys GLOS --verbose --output-json verify_glos.json

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verify")

session = requests.Session()
session.auth = (EMAIL, API_TOKEN)
session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

# Macros Atlassian claimed to transform
ATLASSIAN_SCOPE = {
    "widget": "Should be removed → standard links",
    "page-info": "Should be → sr-page-info-macro",
    "link-to-window": "Should be → standard links",
    "multiexcerpt": "Should be → native excerpt",
    "multiexcerpt-include": "Should be → excerpt-include",
    "viewppt": "Should be → view-file",
}

# Macros in our scope
OUR_SCOPE = {
    "shared-block": "Our scope: → child page + excerpt",
    "include-shared-block": "Our scope: → excerpt-include",
    "include-shared-block-inline": "Our scope: → excerpt-include",
    "help-text": "Our scope: → native Expand",
    "help-text-from-shared-block": "Our scope: → remove/replace",
    "extra-table-properties": "Our scope: → unwrap",
    "button": "Our scope: → inline link",
}

# Other macros of interest
OTHER_MACROS = {
    "legacy-content": "Migration wrapper — should be unwrapped",
    "aura-tab-collection-migratable": "Migrated Aura Tab Group",
    "aura-tab-migratable": "Migrated Aura Tab",
    "aura-tab-collection": "Cloud-native Aura Tab Group",
    "aura-tab": "Cloud-native Aura Tab",
    "sr-page-info-macro": "ScriptRunner Page Info (Cloud native)",
    "excerpt": "Native Excerpt",
    "excerpt-include": "Native Excerpt-Include",
}

ALL_TRACKED = {**ATLASSIAN_SCOPE, **OUR_SCOPE, **OTHER_MACROS}


def get_space_id(space_key):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/spaces", params={"keys": space_key})
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return None
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
        nl = data.get("_links", {}).get("next")
        if not nl or "cursor=" not in nl:
            break
        cursor = nl.split("cursor=")[1].split("&")[0]
    return pages


def get_page_body(page_id):
    resp = session.get(f"{BASE_URL}/wiki/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
    if not resp.ok:
        return None
    d = resp.json()
    body_raw = d.get("body", {}).get("atlas_doc_format", {}).get("value", "{}")
    return {"id": d["id"], "title": d["title"], "body": json.loads(body_raw)}


def scan_node(node, results, inside_legacy=False):
    """Recursively scan an ADF node for tracked macros."""
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    is_legacy = (ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content")

    if ext_key and ext_key in ALL_TRACKED:
        entry = {"macro": ext_key, "inside_legacy": inside_legacy or is_legacy}

        # Extra detail for tabs
        if ext_key in ("aura-tab-migratable", "aura-tab"):
            title_param = node.get("attrs", {}).get("parameters", {}).get("macroParams", {}).get("title", {})
            tab_title = title_param.get("value", "") if isinstance(title_param, dict) else title_param
            has_content = bool(node.get("content"))
            entry["tab_title"] = tab_title
            entry["has_content"] = has_content

        # Check for Migrated title on page-info
        if ext_key == "page-info":
            meta = attrs.get("parameters", {}).get("macroMetadata", {})
            meta_title = meta.get("title", "")
            entry["meta_title"] = meta_title

        results.append(entry)

    # Recurse into content
    if "content" in node and isinstance(node["content"], list):
        for child in node["content"]:
            scan_node(child, results, inside_legacy or is_legacy)

    # Also scan inside legacy-content nestedContent
    if is_legacy:
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            for child in nested["content"]:
                scan_node(child, results, True)


def scan_page(page_data):
    results = []
    body = page_data.get("body", {})
    if "content" in body:
        for child in body["content"]:
            scan_node(child, results)
    return results


def main():
    parser = argparse.ArgumentParser(description="Post-NBM Verification Scanner")
    parser.add_argument("--space-keys", nargs="+", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--batch-delay", type=float, default=0.3)
    parser.add_argument("--limit", type=int, help="Limit pages per space")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    all_space_results = {}

    for space_key in args.space_keys:
        log.info(f"\n{'='*50}")
        log.info(f"Scanning space: {space_key}")
        log.info(f"{'='*50}")

        space_id = get_space_id(space_key)
        if not space_id:
            log.error(f"  Space '{space_key}' not found — skipping")
            continue

        pages = get_all_pages(space_id)
        log.info(f"  Found {len(pages)} pages")

        if args.limit:
            pages = pages[:args.limit]

        # Per-space counters
        macro_counts = {}
        macro_pages = {}  # macro → list of page titles
        tab_issues = []
        total_scanned = 0
        errors = 0

        for i, (page_id, page_title) in enumerate(pages):
            if (i + 1) % 100 == 0:
                log.info(f"  Scanned {i+1}/{len(pages)}...")

            try:
                page_data = get_page_body(page_id)
                if not page_data:
                    errors += 1
                    continue

                total_scanned += 1
                findings = scan_page(page_data)

                for f in findings:
                    macro = f["macro"]
                    macro_counts[macro] = macro_counts.get(macro, 0) + 1
                    if macro not in macro_pages:
                        macro_pages[macro] = []
                    if len(macro_pages[macro]) < 5:  # Keep first 5 examples
                        macro_pages[macro].append({
                            "page_id": page_id, "title": page_title,
                            "inside_legacy": f.get("inside_legacy", False),
                        })

                    # Track tab issues
                    if macro in ("aura-tab-migratable", "aura-tab"):
                        if not f.get("tab_title") or not f.get("has_content"):
                            tab_issues.append({
                                "page_id": page_id, "title": page_title,
                                "tab_title": f.get("tab_title", "MISSING"),
                                "has_content": f.get("has_content", False),
                            })

            except Exception as e:
                errors += 1
                log.debug(f"  Error on {page_title}: {e}")

            if args.batch_delay > 0 and (i + 1) % 50 == 0:
                time.sleep(args.batch_delay)

        # Categorize findings
        atlassian_remaining = {k: v for k, v in macro_counts.items() if k in ATLASSIAN_SCOPE}
        our_remaining = {k: v for k, v in macro_counts.items() if k in OUR_SCOPE}
        cloud_native = {k: v for k, v in macro_counts.items() if k in OTHER_MACROS}

        space_result = {
            "space_key": space_key,
            "pages_scanned": total_scanned,
            "errors": errors,
            "atlassian_should_have_fixed": atlassian_remaining,
            "our_scope_remaining": our_remaining,
            "cloud_native_present": cloud_native,
            "all_macro_counts": macro_counts,
            "example_pages": macro_pages,
            "tab_issues": tab_issues[:20],
        }
        all_space_results[space_key] = space_result

        # Print summary for this space
        print(f"\n{'='*60}")
        print(f"  SPACE: {space_key} ({total_scanned} pages scanned, {errors} errors)")
        print(f"{'='*60}")

        if atlassian_remaining:
            print(f"\n  ATLASSIAN CLAIMED TO FIX (still present):")
            for macro, count in sorted(atlassian_remaining.items(), key=lambda x: -x[1]):
                desc = ATLASSIAN_SCOPE[macro]
                examples = [p["title"] for p in macro_pages.get(macro, [])[:3]]
                print(f"    {macro}: {count} instances — {desc}")
                if examples:
                    print(f"      Examples: {', '.join(examples)}")
        else:
            print(f"\n  ATLASSIAN SCOPE: All clear (no remaining widget/page-info/link-to-window/etc)")

        if our_remaining:
            print(f"\n  OUR SCOPE (expected, not yet run):")
            for macro, count in sorted(our_remaining.items(), key=lambda x: -x[1]):
                desc = OUR_SCOPE[macro]
                print(f"    {macro}: {count} instances — {desc}")

        if cloud_native:
            print(f"\n  CLOUD-NATIVE / STATUS MACROS:")
            for macro, count in sorted(cloud_native.items(), key=lambda x: -x[1]):
                desc = OTHER_MACROS[macro]
                in_legacy = sum(1 for p in macro_pages.get(macro, []) if p.get("inside_legacy"))
                legacy_note = f" ({in_legacy} inside legacy wrappers)" if in_legacy else ""
                print(f"    {macro}: {count}{legacy_note} — {desc}")

        if tab_issues:
            print(f"\n  AURA TAB ISSUES ({len(tab_issues)} tabs with problems):")
            for t in tab_issues[:10]:
                print(f"    {t['title']}: tab_title='{t['tab_title']}' has_content={t['has_content']}")
            if len(tab_issues) > 10:
                print(f"    ... and {len(tab_issues) - 10} more")

    # Write full report
    path = os.path.expanduser(args.output_json)
    with open(path, "w") as f:
        json.dump(all_space_results, f, indent=2)
    log.info(f"\nFull report written to {path}")


if __name__ == "__main__":
    main()
