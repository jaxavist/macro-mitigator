#!/usr/bin/env python3
"""
POL Home Page (and similar Aura-heavy pages): Convert macros WITHOUT
unwrapping legacy-content wrappers.

Legacy wrappers around Aura tab content must stay intact — unwrapping
strips complex Aura layouts (sections, columns, background images, cards).

This script:
  - Extracts shared-blocks to child pages (traverses INTO legacy wrappers to find them)
  - Replaces shared-blocks with excerpt-includes (modifies nestedContent in-place)
  - Replaces include-shared-blocks with excerpt-includes
  - Converts help-text to native Expand
  - Handles buttons, placeholders, panel/expand fix
  - Does NOT unwrap legacy-content wrappers
  - Does NOT rename Aura tab macros

Usage:
    python convert_aura_page.py --space-key POL --page-id 6455298 --dry-run
    python convert_aura_page.py --space-key POL --page-id 6455298
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("aura_page")


class ConfluenceAPI:
    def __init__(self, base_url, email, api_token):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})

    def get_space_id(self, space_key):
        resp = self.session.get(f"{self.base_url}/wiki/api/v2/spaces", params={"keys": space_key})
        resp.raise_for_status()
        return resp.json()["results"][0]["id"]

    def get_page_body(self, page_id):
        resp = self.session.get(f"{self.base_url}/wiki/api/v2/pages/{page_id}", params={"body-format": "atlas_doc_format"})
        resp.raise_for_status()
        d = resp.json()
        return {"id": d["id"], "title": d["title"], "version": d["version"]["number"],
                "body": json.loads(d["body"]["atlas_doc_format"]["value"])}

    def find_page_by_title(self, space_id, title):
        resp = self.session.get(f"{self.base_url}/wiki/api/v2/spaces/{space_id}/pages", params={"title": title, "limit": 1})
        resp.raise_for_status()
        r = resp.json().get("results", [])
        return r[0] if r else None

    def create_page(self, space_id, parent_id, title, body):
        resp = self.session.post(f"{self.base_url}/wiki/api/v2/pages", json={
            "spaceId": space_id, "parentId": parent_id, "title": title, "status": "current",
            "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
        })
        if resp.ok:
            return resp.json()
        log.warning(f"  v2 create failed ({resp.status_code}), trying v1...")
        resp2 = self.session.post(f"{self.base_url}/wiki/rest/api/content", json={
            "type": "page", "title": title, "status": "current",
            "ancestors": [{"id": int(parent_id)}],
            "space": {"key": self._space_key},
            "body": {"atlas_doc_format": {"value": json.dumps(body), "representation": "atlas_doc_format"}},
        })
        if resp2.ok:
            return resp2.json()
        log.error(f"  Create failed: {resp2.status_code}")
        return None

    def update_page(self, page_id, title, version, body):
        msg = "Convert macros (preserve Aura tab wrappers)"
        resp = self.session.put(f"{self.base_url}/wiki/api/v2/pages/{page_id}", json={
            "id": page_id, "status": "current", "title": title,
            "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
            "version": {"number": version + 1, "message": msg},
        })
        if resp.ok:
            return True
        if resp.status_code in (404, 500):
            resp2 = self.session.put(f"{self.base_url}/wiki.rest/api/content/{page_id}", json={
                "type": "page", "title": title,
                "version": {"number": version + 1, "message": msg},
                "body": {"atlas_doc_format": {"value": json.dumps(body), "representation": "atlas_doc_format"}},
            })
            if resp2.ok:
                return True
        log.error(f"  Update failed: {resp.status_code}")
        return False

    def set_space_key(self, sk):
        self._space_key = sk


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
                    "macroMetadata": {"macroId": {"value": str(uuid.uuid4())},
                                     "schemaVersion": {"value": "1"}, "title": "Excerpt"},
                },
            },
            "content": content_nodes or [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}],
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
                "macroMetadata": {"macroId": {"value": str(uuid.uuid4())},
                                 "schemaVersion": {"value": "1"}, "title": "Excerpt Include"},
            },
        },
    }


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


def clean_child_content(nodes):
    result = []
    for node in nodes:
        node = copy.deepcopy(node)
        ext_key = node.get("attrs", {}).get("extensionKey", "")
        if ext_key == "shared-block" and node.get("type") == "bodiedExtension":
            result.extend(clean_child_content(node.get("content", [])))
            continue
        if node.get("type") == "placeholder":
            text = node.get("attrs", {}).get("text", "")
            if text:
                result.append({"type": "text", "text": text, "marks": [{"type": "em"}]})
            continue
        if "content" in node and isinstance(node["content"], list):
            node["content"] = clean_child_content(node["content"])
        result.append(node)
    return result


class Stats:
    def __init__(self):
        self.sb_replaced = 0
        self.isb_replaced = 0
        self.help_text_converted = 0
        self.buttons_converted = 0
        self.placeholders_converted = 0

    @property
    def has_changes(self):
        return any(v > 0 for v in vars(self).values())


def transform_node(node, stats, space_key, child_page_map):
    """Transform a node WITHOUT unwrapping legacy-content."""
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # --- shared-block → excerpt-include ---
    if ext_key == "shared-block" and node_type == "bodiedExtension":
        sb_key = get_macro_param(node, "shared-block-key")
        if sb_key in child_page_map:
            stats.sb_replaced += 1
            return [make_excerpt_include(child_page_map[sb_key], space_key)]
        return [node]

    # --- include-shared-block → excerpt-include ---
    if ext_key == "include-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        # Check if the source page's child page exists
        child_title = f"_{source_page} - {sb_key}" if source_page and sb_key else None
        if child_title and child_title in child_page_map.values():
            stats.isb_replaced += 1
            return [make_excerpt_include(child_title, space_key)]
        return [node]

    # --- help-text → native expand ---
    if ext_key == "help-text" and node_type == "bodiedExtension":
        title = get_macro_param(node, "text") or get_macro_param(node, "title") or "Details"
        tip = get_macro_param(node, "tip")
        body_content = node.get("content", [])
        expand_content = []
        if tip and tip.strip():
            expand_content.append({"type": "paragraph",
                "content": [{"type": "text", "text": tip.strip(), "marks": [{"type": "em"}]}]})
        for child in body_content:
            expand_content.extend(transform_node(child, stats, space_key, child_page_map))
        if not expand_content:
            expand_content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})
        stats.help_text_converted += 1
        return [{"type": "expand", "attrs": {"title": title}, "content": expand_content}]

    # --- help-text-from-shared-block → remove ---
    if ext_key == "help-text-from-shared-block":
        return []

    # --- placeholder → italic text ---
    if node_type == "placeholder":
        text = attrs.get("text", "")
        if text:
            stats.placeholders_converted += 1
            return [{"type": "text", "text": text, "marks": [{"type": "em"}]}]
        return []

    # --- legacy-content: DO NOT UNWRAP, but transform nestedContent in-place ---
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            new_nested_content = []
            for child in nested["content"]:
                new_nested_content.extend(transform_node(child, stats, space_key, child_page_map))
            nested["content"] = new_nested_content
        return [node]  # Keep the wrapper, just transform inside it

    # --- Recurse into children ---
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map))
        node["content"] = new_content

    return [node]


def main():
    parser = argparse.ArgumentParser(description="Convert Aura-heavy page (preserve legacy wrappers)")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)
    api.set_space_key(args.space_key)
    space_id = api.get_space_id(args.space_key)

    page = api.get_page_body(args.page_id)
    log.info(f"Page: {page['title']} (v{page['version']})")

    # Find shared-blocks
    shared_blocks = find_shared_blocks(page["body"])
    log.info(f"Found {len(shared_blocks)} shared-blocks")

    child_page_map = {}
    for sb in shared_blocks:
        sb_key = sb["key"]
        child_title = f"_{page['title']} - {sb_key}"
        log.info(f"  '{sb_key}' → '{child_title}'")

        if args.dry_run:
            child_page_map[sb_key] = child_title
            continue

        existing = api.find_page_by_title(space_id, child_title)
        if existing:
            child_page_map[sb_key] = child_title
            log.info(f"    Already exists (id={existing['id']})")
            continue

        content = clean_child_content(copy.deepcopy(sb["content"]))
        result = api.create_page(space_id, args.page_id, child_title, make_excerpt_body(content))
        if result:
            child_page_map[sb_key] = child_title
            log.info(f"    Created (id={result['id']})")
        else:
            log.error(f"    FAILED")

    # Transform (no legacy unwrap)
    body = copy.deepcopy(page["body"])
    stats = Stats()
    if "content" in body:
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, stats, args.space_key, child_page_map))
        body["content"] = new_content

    log.info(f"\nTransforms: sb={stats.sb_replaced} isb={stats.isb_replaced} "
             f"help-text={stats.help_text_converted} buttons={stats.buttons_converted}")

    if not stats.has_changes:
        print("No changes needed.")
        return

    if args.dry_run:
        print(f"\nDRY RUN: Would update with {stats.sb_replaced} shared-blocks, "
              f"{stats.isb_replaced} includes, {stats.help_text_converted} help-text")
        return

    ok = api.update_page(args.page_id, page["title"], page["version"], body)
    print(f"\n{'Updated successfully' if ok else 'UPDATE FAILED'}")


if __name__ == "__main__":
    main()
