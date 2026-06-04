#!/usr/bin/env python3
"""
Phase 2 Step 3: Convert the Glossary Overview and How-To page.

This page requires special handling:
  1. Extract shared-blocks to child pages with Excerpts
  2. Replace shared-blocks with excerpt-includes
  3. Replace include-shared-blocks (cross-page and self-referencing)
  4. Rename migrated Aura macros to native Cloud versions
  5. Unwrap legacy-content wrappers
  6. Unwrap nested shared-blocks on child pages

Usage:
    python phase2_step3_overview.py --space-key CLOS --dry-run
    python phase2_step3_overview.py --space-key CLOS

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

PAGE_ID = "4605730"
PAGE_TITLE = "Glossary Overview and How-To"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase2_step3")


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
        self._space_key = space_key
        resp = self.session.get(f"{self.base_url}/wiki/api/v2/spaces", params={"keys": space_key})
        resp.raise_for_status()
        return resp.json()["results"][0]["id"]

    def get_page_body(self, page_id):
        resp = self.session.get(
            f"{self.base_url}/wiki/api/v2/pages/{page_id}",
            params={"body-format": "atlas_doc_format"}
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data["id"], "title": data["title"],
            "version": data["version"]["number"],
            "body": json.loads(data["body"]["atlas_doc_format"]["value"]),
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
        # Try v2 API
        resp = self.session.post(
            f"{self.base_url}/wiki/api/v2/pages",
            json={
                "spaceId": space_id, "parentId": parent_id,
                "title": title, "status": "current",
                "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
            }
        )
        if resp.ok:
            return resp.json()

        log.warning(f"  v2 create failed ({resp.status_code}), trying v1...")

        # Fallback: v1 API needs ancestors instead of parentId
        resp2 = self.session.post(
            f"{self.base_url}/wiki/rest/api/content",
            json={
                "type": "page", "title": title, "status": "current",
                "ancestors": [{"id": int(parent_id)}],
                "space": {"key": self._space_key},
                "body": {"atlas_doc_format": {
                    "value": json.dumps(body), "representation": "atlas_doc_format",
                }},
            }
        )
        if resp2.ok:
            return resp2.json()

        log.error(f"  v1 create also failed: {resp2.status_code}")
        try:
            log.error(f"  {json.dumps(resp2.json())[:300]}")
        except Exception:
            pass
        return None

    def update_page(self, page_id, title, version, body, message=""):
        msg = message or "Phase 2 Step 3: Overview page conversion"
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
        # Fall back to v1 on 404 or 500
        if resp.status_code in (404, 500):
            log.info(f"  v2 returned {resp.status_code}, trying v1...")
            resp2 = self.session.put(
                f"{self.base_url}/wiki/rest/api/content/{page_id}",
                json={
                    "type": "page", "title": title,
                    "version": {"number": version + 1, "message": msg},
                    "body": {"atlas_doc_format": {
                        "value": json.dumps(body), "representation": "atlas_doc_format",
                    }},
                }
            )
            if resp2.ok:
                return True
            log.error(f"  v1 also failed: {resp2.status_code}")
            try:
                log.error(f"  {json.dumps(resp2.json())[:500]}")
            except Exception:
                pass
            return False
        log.error(f"  Update failed: {resp.status_code}")
        try:
            log.error(f"  {json.dumps(resp.json())[:500]}")
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# ADF helpers
# ---------------------------------------------------------------------------

def get_macro_param(node, param_name):
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    return val.get("value", "") if isinstance(val, dict) else val


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
# Shared-block scanner (including inside legacy wrappers)
# ---------------------------------------------------------------------------

def find_shared_blocks(node, results=None):
    if results is None:
        results = []
    ext_key = node.get("attrs", {}).get("extensionKey", "")

    if ext_key == "shared-block" and node.get("type") == "bodiedExtension":
        sb_key = get_macro_param(node, "shared-block-key")
        results.append({"key": sb_key, "content": node.get("content", [])})

    for child in node.get("content", []):
        find_shared_blocks(child, results)

    # Also inside legacy-content nestedContent
    if (node.get("attrs", {}).get("extensionType") == "com.atlassian.confluence.migration"
        and node.get("attrs", {}).get("extensionKey") == "legacy-content"):
        nested = node.get("attrs", {}).get("parameters", {}).get("nestedContent", {})
        if nested:
            for child in nested.get("content", []):
                find_shared_blocks(child, results)

    return results


# ---------------------------------------------------------------------------
# Clean child page content: unwrap any nested shared-blocks
# ---------------------------------------------------------------------------

def clean_child_content(nodes):
    """Unwrap nested shared-blocks, strip broken media, fix panel/expand nesting."""
    result = []
    for node in nodes:
        node = copy.deepcopy(node)
        ext_key = node.get("attrs", {}).get("extensionKey", "")

        # Unwrap nested shared-blocks
        if ext_key == "shared-block" and node.get("type") == "bodiedExtension":
            inner = node.get("content", [])
            result.extend(clean_child_content(inner))
            continue

        # Strip mediaSingle with UNKNOWN_MEDIA_ID
        if node.get("type") == "mediaSingle":
            has_unknown = False
            for child in node.get("content", []):
                if child.get("type") == "media" and child.get("attrs", {}).get("id") == "UNKNOWN_MEDIA_ID":
                    has_unknown = True
                    break
            if has_unknown:
                result.append({
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "[Image removed — migration artifact]"}]
                })
                continue

        # Fix panel containing expand (Cloud doesn't allow expand inside panel)
        if node.get("type") == "panel" and "content" in node:
            panel_content = []
            extracted = []
            for child in node["content"]:
                if child.get("type") == "expand":
                    extracted.append(child)
                else:
                    panel_content.append(child)
            if extracted:
                if panel_content:
                    node["content"] = clean_child_content(panel_content)
                    result.append(node)
                # Place extracted expands after the panel
                for exp in extracted:
                    if "content" in exp:
                        exp["content"] = clean_child_content(exp["content"])
                    result.append(exp)
                continue

        # Recurse
        if "content" in node and isinstance(node["content"], list):
            node["content"] = clean_child_content(node["content"])
        result.append(node)
    return result


# ---------------------------------------------------------------------------
# Transform the Overview page ADF
# ---------------------------------------------------------------------------

def transform_node(node, stats, space_key, child_page_map, isb_map):
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # --- include-shared-block → excerpt-include ---
    if ext_key == "include-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")

        # Check all mappings
        for match_key, child_title in isb_map.items():
            if match_key == (source_page, sb_key):
                stats["isb_replaced"] += 1
                return [make_excerpt_include(child_title, space_key)]

        log.warning(f"  No mapping for include-shared-block: page='{source_page}' key='{sb_key}'")
        return [node]

    # --- help-text-from-shared-block → excerpt-include ---
    if ext_key == "help-text-from-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        for match_key, child_title in isb_map.items():
            if match_key == (source_page, sb_key):
                stats["ht_replaced"] += 1
                return [make_excerpt_include(child_title, space_key)]
        return [node]

    # --- shared-block → excerpt-include to child page ---
    if ext_key == "shared-block" and node_type == "bodiedExtension":
        sb_key = get_macro_param(node, "shared-block-key")
        if sb_key in child_page_map:
            stats["sb_replaced"] += 1
            return [make_excerpt_include(child_page_map[sb_key], space_key)]
        return [node]

    # --- legacy-content → unwrap ---
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            stats["legacy_removed"] += 1
            result = []
            for child in nested["content"]:
                result.extend(transform_node(child, stats, space_key, child_page_map, isb_map))
            return result
        stats["legacy_removed"] += 1
        return []

    # --- Aura macro renames: DISABLED ---
    # The -migratable Aura tab macros are functional on Cloud and must remain
    # as bodied macros. Renaming to aura-tab/aura-tab-collection causes
    # Confluence to serialize them as non-bodied macros, losing all tab content.
    # if ext_key == "aura-tab-collection-migratable": ...
    # if ext_key == "aura-tab-migratable": ...

    # --- Strip broken media ---
    if node_type == "mediaSingle":
        for child in node.get("content", []):
            if child.get("type") == "media" and child.get("attrs", {}).get("id") == "UNKNOWN_MEDIA_ID":
                stats["media_stripped"] = stats.get("media_stripped", 0) + 1
                return [{"type": "paragraph", "content": [
                    {"type": "text", "text": "[Image removed — migration artifact]"}
                ]}]

    # --- Fix panel containing expand ---
    if node_type == "panel" and "content" in node:
        panel_content = []
        extracted_expands = []
        for child in node.get("content", []):
            if child.get("type") == "expand":
                extracted_expands.append(child)
            else:
                panel_content.append(child)
        if extracted_expands:
            result_nodes = []
            if panel_content:
                new_panel = copy.deepcopy(node)
                new_panel["content"] = []
                for pc in panel_content:
                    new_panel["content"].extend(
                        transform_node(pc, stats, space_key, child_page_map, isb_map))
                result_nodes.append(new_panel)
            for exp in extracted_expands:
                transformed = transform_node(exp, stats, space_key, child_page_map, isb_map)
                result_nodes.extend(transformed)
            return result_nodes

    # --- Recurse ---
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map, isb_map))
        node["content"] = new_content

    return [node]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 Step 3: Overview page conversion")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--dry-run", action="store_true")
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

    # Fetch the page
    log.info(f"Fetching Overview page ({PAGE_ID})...")
    page = api.get_page_body(PAGE_ID)
    log.info(f"  Title: {page['title']} (v{page['version']})")

    # Pass 1: Find all shared-blocks and create child pages
    log.info("Scanning for shared-blocks...")
    shared_blocks = find_shared_blocks(page["body"])
    log.info(f"  Found {len(shared_blocks)} shared-blocks")

    child_page_map = {}  # {sb_key: child_page_title}

    for sb in shared_blocks:
        sb_key = sb["key"]
        child_title = f"_{PAGE_TITLE} - {sb_key}"
        content = clean_child_content(copy.deepcopy(sb["content"]))

        log.info(f"  Shared-block '{sb_key}' → child page '{child_title}'")

        if args.dry_run:
            child_page_map[sb_key] = child_title
            continue

        existing = api.find_page_by_title(space_id, child_title)
        if existing:
            log.info(f"    Already exists (id={existing['id']})")
            child_page_map[sb_key] = child_title
            continue

        excerpt_body = make_excerpt_body(content)
        result = api.create_page(space_id, PAGE_ID, child_title, excerpt_body)
        if result:
            child_page_map[sb_key] = child_title
            log.info(f"    Created (id={result['id']})")
        else:
            log.error(f"    FAILED to create child page")

    # Build include-shared-block mapping
    # These are the cross-page and self-referencing includes on this page
    isb_map = {
        # Template Resources refs (from Step 1)
        ("Glossary Template Resources", "Definition(s)"): "_Definition(s)",
        ("Glossary Template Resources", "Internal Resources"): "_Internal Resources",
        ("Glossary Template Resources", "External Resources"): "_External Resources",
        ("Glossary Template Resources", "Other Data"): "_Other Data",
        ("Glossary Template Resources", "GlossaryHelpText"): "_GlossaryHelpText",
        # Cross-page: "Glossary" term page's General Definition (converted in Step 2)
        ("Glossary", "General Definition"): "_Glossary - General Definition",
        ("CLOS:Glossary", "General Definition"): "_Glossary - General Definition",
        # Self-referencing: this page's own shared-blocks (now child pages)
        ("Glossary Overview and How-To", "Glossary Reqs Standards"):
            f"_{PAGE_TITLE} - Glossary Reqs Standards",
        ("", "Glossary Reqs Standards"):  # empty page ref = self
            f"_{PAGE_TITLE} - Glossary Reqs Standards",
    }

    # Pass 2: Transform the page
    log.info("\nTransforming page ADF...")
    body = copy.deepcopy(page["body"])
    stats = {"isb_replaced": 0, "sb_replaced": 0, "legacy_removed": 0,
             "aura_renamed": 0, "ht_replaced": 0}

    if "content" in body:
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map, isb_map))
        body["content"] = new_content

    log.info(f"  include-shared-block → excerpt-include: {stats['isb_replaced']}")
    log.info(f"  shared-block → child page + excerpt-include: {stats['sb_replaced']}")
    log.info(f"  legacy wrappers removed: {stats['legacy_removed']}")
    log.info(f"  Aura macros renamed: {stats['aura_renamed']}")
    log.info(f"  help-text replaced: {stats['ht_replaced']}")

    if args.dry_run:
        print(f"\n{'=' * 60}")
        print(f"  DRY RUN SUMMARY")
        print(f"{'=' * 60}")
        print(f"  Child pages to create: {len(child_page_map)}")
        for sb_key, title in child_page_map.items():
            print(f"    - {title}")
        print(f"  include-shared-block replaced: {stats['isb_replaced']}")
        print(f"  shared-block replaced: {stats['sb_replaced']}")
        print(f"  legacy wrappers removed: {stats['legacy_removed']}")
        print(f"  Aura macros renamed: {stats['aura_renamed']}")
        print(f"{'=' * 60}")
        return

    # Save
    log.info("\nSaving updated page...")
    ok = api.update_page(PAGE_ID, page["title"], page["version"], body)

    if ok:
        log.info("  Updated successfully!")
    else:
        log.error("  Update FAILED")

    print(f"\n{'=' * 60}")
    print(f"  PHASE 2 STEP 3 SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Child pages created: {len(child_page_map)}")
    print(f"  include-shared-block replaced: {stats['isb_replaced']}")
    print(f"  shared-block replaced: {stats['sb_replaced']}")
    print(f"  legacy wrappers removed: {stats['legacy_removed']}")
    print(f"  Aura macros renamed: {stats['aura_renamed']}")
    print(f"  Page update: {'SUCCESS' if ok else 'FAILED'}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
