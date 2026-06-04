#!/usr/bin/env python3
"""
Phase 0 Diagnostic Step 3: Isolate what inside Tab 0 (Glossary Basics) causes the 500.

Strategy:
  1. Dump the legacy wrapper's nestedContent for tab 0 to a JSON file for inspection
  2. Try unwrapping tab 0 with its content replaced by a simple paragraph
  3. Try unwrapping tab 0 but removing content blocks one at a time

Usage:
    python phase0_diagnose3.py --page-id 107848258
"""

import copy
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


def get_page(page_id):
    url = f"{BASE_URL}/wiki/api/v2/pages/{page_id}"
    resp = session.get(url, params={"body-format": "atlas_doc_format"})
    resp.raise_for_status()
    data = resp.json()
    body_raw = data["body"]["atlas_doc_format"]["value"]
    return {
        "id": data["id"],
        "title": data["title"],
        "version": data["version"]["number"],
        "body": json.loads(body_raw),
    }


def try_update(page_id, title, version, body, label):
    url = f"{BASE_URL}/wiki/api/v2/pages/{page_id}"
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
            "message": f"Phase 0 diag3: {label}",
        },
    }
    resp = session.put(url, json=payload)
    if resp.ok:
        print(f"  [PASS] {label} — saved (v{version + 1})")
        return True
    else:
        print(f"  [FAIL] {label} — {resp.status_code}")
        return False


def find_tab_collection(body):
    """Find the tab collection node and return (parent_content_list, index, node)."""
    for section in body.get("content", []):
        for col in section.get("content", []):
            if not isinstance(col.get("content"), list):
                continue
            for i, node in enumerate(col["content"]):
                ext_key = node.get("attrs", {}).get("extensionKey", "")
                if ext_key in ("aura-tab-collection-migratable", "aura-tab-collection"):
                    return col["content"], i, node
    return None, None, None


def find_first_legacy_wrapper(tab_collection_node):
    """Find the first legacy-content wrapper child (tab 0)."""
    for i, child in enumerate(tab_collection_node.get("content", [])):
        if (
            child.get("type") in ("extension", "bodiedExtension")
            and child.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
            and child.get("attrs", {}).get("extensionKey") == "legacy-content"
        ):
            return i, child
    return None, None


def get_nested_content(legacy_node):
    """Extract nestedContent from a legacy-content wrapper."""
    return legacy_node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})


def summarize_node(node, depth=0):
    """Create a human-readable summary of a node tree."""
    indent = "  " * depth
    node_type = node.get("type", "?")
    ext_key = node.get("attrs", {}).get("extensionKey", "")
    title = node.get("attrs", {}).get("parameters", {}).get("macroParams", {}).get("title", {})
    if isinstance(title, dict):
        title = title.get("value", "")
    shared_key = node.get("attrs", {}).get("parameters", {}).get("macroParams", {}).get("shared-block-key", {})
    if isinstance(shared_key, dict):
        shared_key = shared_key.get("value", "")

    label = node_type
    if ext_key:
        label += f" ({ext_key})"
    if title:
        label += f" title='{title}'"
    if shared_key:
        label += f" key='{shared_key}'"

    # Check for text content
    text = node.get("text", "")
    if text:
        label += f" text='{text[:50]}...'" if len(text) > 50 else f" text='{text}'"

    lines = [f"{indent}{label}"]
    for child in node.get("content", []):
        lines.extend(summarize_node(child, depth + 1))
    return lines


def main():
    page_id = sys.argv[2] if len(sys.argv) > 2 else "107848258"

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    page = get_page(page_id)
    print(f"Page: {page['title']} (v{page['version']})\n")

    # Find the tab collection
    _, _, tab_collection = find_tab_collection(page["body"])
    if not tab_collection:
        print("ERROR: No tab collection found on this page")
        sys.exit(1)

    # Find tab 0 legacy wrapper
    wrapper_idx, wrapper = find_first_legacy_wrapper(tab_collection)
    if wrapper is None:
        print("No legacy wrapper found — tab 0 may already be unwrapped")
        print("Current tab collection children:")
        for i, child in enumerate(tab_collection.get("content", [])):
            ext_key = child.get("attrs", {}).get("extensionKey", "")
            print(f"  [{i}] type={child.get('type')} ext_key={ext_key}")
        sys.exit(0)

    # Dump the nestedContent for inspection
    nested = get_nested_content(wrapper)
    dump_path = os.path.expanduser("~/Downloads/tab0_nested_content.json")
    with open(dump_path, "w") as f:
        json.dump(nested, f, indent=2)
    print(f"Dumped tab 0 nestedContent to: {dump_path}")

    # Summarize the content
    print(f"\nTab 0 nestedContent structure:")
    if nested and nested.get("content"):
        for child in nested["content"]:
            for line in summarize_node(child):
                print(f"  {line}")
    print()

    # --- Test 1: Replace tab 0 content with a simple paragraph ---
    print("Test 1: Unwrap tab 0 with content replaced by a simple paragraph")
    page = get_page(page_id)
    body1 = copy.deepcopy(page["body"])
    _, _, tc1 = find_tab_collection(body1)
    idx1, _ = find_first_legacy_wrapper(tc1)
    simple_tab = {
        "type": "bodiedExtension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "aura-tab",
            "parameters": {
                "macroParams": {"title": {"value": "Glossary Basics"}},
                "macroMetadata": {
                    "macroId": {"value": "b4bc27d3-daef-4752-8bbb-c09b36e6a64c"},
                    "schemaVersion": {"value": "1"},
                    "title": "Tab",
                },
            },
        },
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Content placeholder — testing structural save."}]}
        ],
    }
    tc1["content"][idx1] = simple_tab
    result1 = try_update(page["id"], page["title"], page["version"], body1, "simple-paragraph-tab")
    print()

    if not result1:
        print("Even a simple paragraph tab fails — the issue is structural, not content-related.")
        print("This might mean bodiedExtension (aura-tab) isn't valid as a direct child here.")

        # Test 1b: Try as an extension (non-bodied) instead
        print("\nTest 1b: Try tab 0 as non-bodied extension")
        page = get_page(page_id)
        body1b = copy.deepcopy(page["body"])
        _, _, tc1b = find_tab_collection(body1b)
        idx1b, _ = find_first_legacy_wrapper(tc1b)
        simple_ext = {
            "type": "extension",
            "attrs": {
                "extensionType": "com.atlassian.confluence.macro.core",
                "extensionKey": "aura-tab",
                "parameters": {
                    "macroParams": {"title": {"value": "Glossary Basics"}},
                    "macroMetadata": {
                        "macroId": {"value": "b4bc27d3-daef-4752-8bbb-c09b36e6a64c"},
                        "schemaVersion": {"value": "1"},
                        "title": "Tab",
                    },
                },
            },
        }
        tc1b["content"][idx1b] = simple_ext
        try_update(page["id"], page["title"], page["version"], body1b, "simple-extension-tab")
        print()
        return

    # --- Test 2: Unwrap tab 0 with real content, removing blocks one at a time ---
    print("Test 2: Unwrap tab 0 with real content — removing children one at a time")
    # Get the actual tab content (the aura-tab-migratable bodiedExtension inside nestedContent)
    page = get_page(page_id)
    _, _, tc_fresh = find_tab_collection(page["body"])
    _, wrapper_fresh = find_first_legacy_wrapper(tc_fresh)
    nested_fresh = get_nested_content(wrapper_fresh)

    if not nested_fresh or not nested_fresh.get("content"):
        print("  No nested content to test")
        return

    # The nested content should be [bodiedExtension (aura-tab-migratable)]
    tab_node = nested_fresh["content"][0]
    tab_children = tab_node.get("content", [])
    print(f"  Tab has {len(tab_children)} top-level content children")

    for skip_idx in range(len(tab_children)):
        page = get_page(page_id)
        body2 = copy.deepcopy(page["body"])
        _, _, tc2 = find_tab_collection(body2)
        idx2, _ = find_first_legacy_wrapper(tc2)

        # Build tab with all content EXCEPT child at skip_idx
        test_tab = copy.deepcopy(tab_node)
        test_tab["attrs"]["extensionKey"] = "aura-tab"
        meta = test_tab["attrs"].get("parameters", {}).get("macroMetadata", {})
        if meta.get("title") == "Migrated Tab":
            meta["title"] = "Tab"

        skipped = test_tab["content"][skip_idx]
        skipped_type = skipped.get("type", "?")
        skipped_ext = skipped.get("attrs", {}).get("extensionKey", "")
        skipped_label = f"{skipped_type}({skipped_ext})" if skipped_ext else skipped_type

        test_tab["content"] = [c for j, c in enumerate(test_tab["content"]) if j != skip_idx]
        tc2["content"][idx2] = test_tab

        try_update(
            page["id"], page["title"], page["version"], body2,
            f"skip-child-{skip_idx} ({skipped_label})"
        )

    print("\nDiagnosis complete.")
    print("Any PASS results above tell you which child, when removed, allows the save.")
    print("That removed child is the one causing the 500.")


if __name__ == "__main__":
    main()
