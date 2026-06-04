#!/usr/bin/env python3
"""
Phase 0 Diagnostic Step 2: Test if UNKNOWN_MEDIA_ID is causing the 500.

Tries two approaches:
  1. Unwrap legacy + strip UNKNOWN_MEDIA_ID media nodes
  2. Unwrap legacy wrappers one at a time to find the problem tab

Usage:
    python phase0_diagnose2.py --page-id 107848258
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
            "message": f"Phase 0 diag2: {label}",
        },
    }
    resp = session.put(url, json=payload)
    if resp.ok:
        print(f"  [PASS] {label} — saved successfully (v{version + 1})")
        return True
    else:
        print(f"  [FAIL] {label} — {resp.status_code}")
        try:
            print(f"  {json.dumps(resp.json(), indent=2)[:500]}")
        except Exception:
            print(f"  {resp.text[:500]}")
        return False


def strip_unknown_media(node):
    """
    Recursively remove mediaSingle nodes that contain media with UNKNOWN_MEDIA_ID.
    Returns a list of nodes (the node itself, or empty list if stripped).
    """
    if node.get("type") == "mediaSingle":
        for child in node.get("content", []):
            if child.get("type") == "media" and child.get("attrs", {}).get("id") == "UNKNOWN_MEDIA_ID":
                return []  # Strip this node entirely
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(strip_unknown_media(child))
        node["content"] = new_content
    return [node]


def unwrap_legacy(node):
    if (
        node.get("type") in ("extension", "bodiedExtension")
        and node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"
    ):
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            promoted = []
            for child in nested["content"]:
                promoted.extend(walk_unwrap(child))
            return promoted
        return []
    return None


def walk_unwrap(node):
    result = unwrap_legacy(node)
    if result is not None:
        return result
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(walk_unwrap(child))
        node["content"] = new_content
    return [node]


def walk_unwrap_and_strip_media(node):
    """Unwrap legacy + strip unknown media in one pass."""
    result = unwrap_legacy(node)
    if result is not None:
        # The promoted children need media stripping too
        stripped = []
        for child in result:
            stripped.extend(strip_unknown_media(child))
        return stripped
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(walk_unwrap_and_strip_media(child))
        node["content"] = new_content
    return [node]


def count_unknown_media(node, depth=0):
    """Count UNKNOWN_MEDIA_ID references in a node tree."""
    count = 0
    if node.get("type") == "media" and node.get("attrs", {}).get("id") == "UNKNOWN_MEDIA_ID":
        count += 1
    for child in node.get("content", []):
        count += count_unknown_media(child, depth + 1)
    return count


def count_legacy_wrappers(node):
    """Count legacy-content wrappers."""
    count = 0
    if (
        node.get("type") in ("extension", "bodiedExtension")
        and node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"
    ):
        count += 1
    for child in node.get("content", []):
        count += count_legacy_wrappers(child)
    return count


def transform_body(body, walk_fn):
    body = copy.deepcopy(body)
    if "content" in body:
        new_content = []
        for child in body["content"]:
            new_content.extend(walk_fn(child))
        body["content"] = new_content
    return body


def main():
    page_id = sys.argv[2] if len(sys.argv) > 2 else "107848258"

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    print(f"Diagnosing page {page_id}...\n")

    # First, count what we're dealing with
    page = get_page(page_id)
    media_count = count_unknown_media(page["body"])
    legacy_count = count_legacy_wrappers(page["body"])
    print(f"Current state: {legacy_count} legacy wrappers, {media_count} UNKNOWN_MEDIA_ID refs\n")

    # --- Test A: Unwrap legacy + strip unknown media ---
    print("Test A: Unwrap legacy + strip UNKNOWN_MEDIA_ID nodes")
    page = get_page(page_id)
    body_a = transform_body(page["body"], walk_unwrap_and_strip_media)
    result_a = try_update(page["id"], page["title"], page["version"], body_a, "unwrap+strip-media")
    print()

    if result_a:
        print(">>> CONFIRMED: UNKNOWN_MEDIA_ID was the cause.")
        print(">>> The page has been saved with legacy wrappers removed and broken media refs stripped.")
        print(">>> The missing images were already broken (UNKNOWN_MEDIA_ID from DC migration).")
        return

    # --- Test B: Strip media only (no unwrap) ---
    print("Test B: Strip UNKNOWN_MEDIA_ID only (no unwrap) — testing if media strip alone works")
    page = get_page(page_id)
    body_b = transform_body(page["body"], lambda n: strip_unknown_media(n))
    try_update(page["id"], page["title"], page["version"], body_b, "strip-media-only")
    print()

    # --- Test C: Unwrap one tab at a time ---
    print("Test C: Unwrap legacy wrappers one at a time to find the problem")
    page = get_page(page_id)
    body = copy.deepcopy(page["body"])

    # Find the tab-collection and its children
    for section in body.get("content", []):
        for col in section.get("content", []):
            for node in col.get("content", []):
                if node.get("attrs", {}).get("extensionKey") in (
                    "aura-tab-collection-migratable", "aura-tab-collection"
                ):
                    tab_children = node.get("content", [])
                    print(f"  Found tab collection with {len(tab_children)} children")
                    for i, tab_wrapper in enumerate(tab_children):
                        # Try unwrapping just this one tab
                        page = get_page(page_id)
                        test_body = copy.deepcopy(page["body"])
                        # Navigate to the same spot
                        for s2 in test_body.get("content", []):
                            for c2 in s2.get("content", []):
                                for n2 in c2.get("content", []):
                                    if n2.get("attrs", {}).get("extensionKey") in (
                                        "aura-tab-collection-migratable", "aura-tab-collection"
                                    ):
                                        children = n2.get("content", [])
                                        if i < len(children):
                                            child = children[i]
                                            unwrapped = unwrap_legacy(child)
                                            if unwrapped is not None:
                                                children[i:i+1] = unwrapped
                                                tab_title = "unknown"
                                                for uw in unwrapped:
                                                    tp = uw.get("attrs", {}).get("parameters", {}).get("macroParams", {}).get("title", {})
                                                    if isinstance(tp, dict):
                                                        tab_title = tp.get("value", "unknown")
                                                    elif isinstance(tp, str):
                                                        tab_title = tp
                                                try_update(
                                                    page["id"], page["title"],
                                                    page["version"], test_body,
                                                    f"unwrap-tab-{i} ({tab_title})"
                                                )
                                                print()

    print("Diagnosis complete.")


if __name__ == "__main__":
    main()
