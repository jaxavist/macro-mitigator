#!/usr/bin/env python3
"""
Phase 0 Diagnostic: Isolate the 500 error on the Glossary Overview page.

Tests three approaches separately:
  1. Legacy unwrap ONLY (no Aura renames)
  2. Aura rename ONLY (no legacy unwrap)
  3. Both together (original behavior)

Also captures the full error response body from Confluence.

Usage:
    python phase0_diagnose.py --page-id 107848258
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
            "message": f"Phase 0 diag: {label}",
        },
    }
    resp = session.put(url, json=payload)
    if resp.ok:
        print(f"  [PASS] {label} — saved successfully (v{version + 1})")
        return True
    else:
        print(f"  [FAIL] {label} — {resp.status_code}")
        print(f"  Response body:")
        try:
            error_json = resp.json()
            print(f"  {json.dumps(error_json, indent=2)}")
        except Exception:
            print(f"  {resp.text[:2000]}")
        return False


# --- Transformation functions (separated) ---

def unwrap_legacy_only(node):
    """Unwrap legacy-content, do NOT rename Aura macros."""
    if (
        node.get("type") in ("extension", "bodiedExtension")
        and node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"
    ):
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            promoted = []
            for child in nested["content"]:
                promoted.extend(walk_unwrap_only(child))
            return promoted
        return []
    return None


def walk_unwrap_only(node):
    unwrapped = unwrap_legacy_only(node)
    if unwrapped is not None:
        return unwrapped
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(walk_unwrap_only(child))
        node["content"] = new_content
    return [node]


def rename_aura_only(node):
    """Rename Aura macros, do NOT unwrap legacy-content."""
    if node.get("type") in ("extension", "bodiedExtension", "inlineExtension"):
        attrs = node.get("attrs", {})
        ext_key = attrs.get("extensionKey", "")
        metadata = attrs.get("parameters", {}).get("macroMetadata", {})
        if ext_key == "aura-tab-collection-migratable":
            attrs["extensionKey"] = "aura-tab-collection"
            if metadata.get("title") == "Migrated Tab Group":
                metadata["title"] = "Tab Group"
        elif ext_key == "aura-tab-migratable":
            attrs["extensionKey"] = "aura-tab"
            if metadata.get("title") == "Migrated Tab":
                metadata["title"] = "Tab"
    if "content" in node and isinstance(node["content"], list):
        for child in node["content"]:
            rename_aura_only(child)


def apply_both(node):
    """Unwrap legacy + rename Aura."""
    if (
        node.get("type") in ("extension", "bodiedExtension")
        and node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"
    ):
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            promoted = []
            for child in nested["content"]:
                promoted.extend(walk_both(child))
            return promoted
        return []
    return None


def walk_both(node):
    unwrapped = apply_both(node)
    if unwrapped is not None:
        return unwrapped
    # Rename Aura
    if node.get("type") in ("extension", "bodiedExtension", "inlineExtension"):
        attrs = node.get("attrs", {})
        ext_key = attrs.get("extensionKey", "")
        metadata = attrs.get("parameters", {}).get("macroMetadata", {})
        if ext_key == "aura-tab-collection-migratable":
            attrs["extensionKey"] = "aura-tab-collection"
            if metadata.get("title") == "Migrated Tab Group":
                metadata["title"] = "Tab Group"
        elif ext_key == "aura-tab-migratable":
            attrs["extensionKey"] = "aura-tab"
            if metadata.get("title") == "Migrated Tab":
                metadata["title"] = "Tab"
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(walk_both(child))
        node["content"] = new_content
    return [node]


def transform_body(body, walk_fn):
    body = copy.deepcopy(body)
    if "content" in body and isinstance(body["content"], list):
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

    print(f"Diagnosing page {page_id}...")
    print()

    # --- Test 0: Identity save (no changes at all) ---
    print("Test 0: Identity save (re-save the page as-is with no changes)")
    page = get_page(page_id)
    result = try_update(page["id"], page["title"], page["version"], page["body"], "identity-save")
    print()

    if not result:
        print("  Identity save failed — the page may already be in a broken state.")
        print("  The 500 might not be caused by our transformations at all.")
        print()

    # --- Test 1: Legacy unwrap ONLY ---
    print("Test 1: Legacy unwrap ONLY (no Aura rename)")
    page = get_page(page_id)  # re-fetch to get current version
    body1 = transform_body(page["body"], walk_unwrap_only)
    try_update(page["id"], page["title"], page["version"], body1, "legacy-unwrap-only")
    print()

    # --- Test 2: Aura rename ONLY ---
    print("Test 2: Aura rename ONLY (no legacy unwrap)")
    page = get_page(page_id)
    body2 = copy.deepcopy(page["body"])
    if "content" in body2:
        for child in body2["content"]:
            rename_aura_only(child)
    try_update(page["id"], page["title"], page["version"], body2, "aura-rename-only")
    print()

    # --- Test 3: Both together ---
    print("Test 3: Both (legacy unwrap + Aura rename)")
    page = get_page(page_id)
    body3 = transform_body(page["body"], walk_both)
    try_update(page["id"], page["title"], page["version"], body3, "both")
    print()

    print("Diagnosis complete.")


if __name__ == "__main__":
    main()
