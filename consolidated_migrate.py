#!/usr/bin/env python3
"""
Consolidated Glossary Migration Script
=======================================
Single-pass conversion of all DC macro patterns to Cloud-native equivalents.

One read, all transforms, one save per page. Eliminates the revert-on-save
issue caused by multi-script sequential processing.

Transforms applied (in recursive tree-walk order):
  1. legacy-content wrappers → unwrap (promote nestedContent)
  2. extra-table-properties → unwrap (promote table content)
  3. include-shared-block → excerpt-include or include (Template Resources refs)
  4. shared-block → child page with Excerpt + excerpt-include on parent
  5. help-text → native Expand macro (with tip as italic text)
  6. help-text-from-shared-block → remove
  7. button → inline hyperlinked text
  8. placeholder → italic text
  9. panel containing expand → fix nesting (extract expand as sibling)
  10. UNKNOWN_MEDIA_ID / UNKNOWN_ATTACHMENT media → placeholder text
  11. livesearch spaceKey → fix GLOS→target space
  12. DC links → Cloud URLs

Prerequisites:
  - Template Resources child pages must exist FIRST (run phase2_step1_template_resources.py)
  - Page restrictions resolved (run add_editor_to_restricted.py)

Usage:
    # Dry run on single page
    python consolidated_migrate.py --space-key GLOS --page-id 12345 --dry-run

    # Dry run on full space
    python consolidated_migrate.py --space-key GLOS --dry-run

    # Live run
    python consolidated_migrate.py --space-key GLOS --batch-size 20 --output-json results.json

    # With DC link fixing (provide old base URL)
    python consolidated_migrate.py --space-key GLOS --dc-base-url https://confluence.tempustechnologies.com

Environment variables:
    CONFLUENCE_BASE_URL  — e.g. https://tempus-sandbox.atlassian.net
    CONFLUENCE_EMAIL     — your Atlassian account email
    CONFLUENCE_API_TOKEN — API token
"""

import argparse
import copy
import json
import logging
import os
import re
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
log = logging.getLogger("consolidated")

# ---------------------------------------------------------------------------
# Configuration — UPDATE THESE FOR YOUR SPACE
# ---------------------------------------------------------------------------

# Pages to skip (special pages handled separately)
SKIP_PAGE_IDS = set()  # Populated at runtime

# Template Resources child pages (created by phase2_step1)
# Maps (source_page_title, shared_block_key) → child page title
TEMPLATE_INCLUDE_MAP = {}  # Populated at runtime from Template Resources children

# The _Definition(s) child page uses Include Page instead of Excerpt-Include
DEFINITION_INCLUDE_PAGE = "_Definition(s)"


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
        return resp.json()["results"][0]["id"]

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
            nl = data.get("_links", {}).get("next")
            if not nl or "cursor=" not in nl:
                break
            cursor = nl.split("cursor=")[1].split("&")[0]
        return pages

    def get_page_body(self, page_id):
        resp = self.session.get(
            f"{self.base_url}/wiki/api/v2/pages/{page_id}",
            params={"body-format": "atlas_doc_format"}
        )
        resp.raise_for_status()
        d = resp.json()
        return {
            "id": d["id"], "title": d["title"],
            "version": d["version"]["number"],
            "spaceId": d.get("spaceId"),
            "body": json.loads(d["body"]["atlas_doc_format"]["value"]),
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
                "spaceId": space_id, "parentId": parent_id,
                "title": title, "status": "current",
                "body": {"representation": "atlas_doc_format", "value": json.dumps(body)},
            }
        )
        if resp.ok:
            return resp.json()
        # v1 fallback
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
        log.error(f"  Failed to create '{title}': v2={resp.status_code} v1={resp2.status_code}")
        try:
            log.error(f"  {json.dumps(resp2.json())[:300]}")
        except Exception:
            pass
        return None

    def update_page(self, page_id, title, version, body):
        msg = "Consolidated migration: DC macros → Cloud native"
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
        if resp.status_code in (404, 500):
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
            log.error(f"  v1 fallback failed: {resp2.status_code}")
            return False
        log.error(f"  Update failed: {resp.status_code}")
        return False

    def set_space_key(self, space_key):
        self._space_key = space_key


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


def make_include_page(page_title):
    return {
        "type": "extension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "include",
            "parameters": {
                "macroParams": {"": {"value": page_title}},
                "macroMetadata": {
                    "macroId": {"value": str(uuid.uuid4())},
                    "schemaVersion": {"value": "1"},
                    "title": "Include Page",
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Shared-block scanner
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


def clean_child_content(nodes):
    """Clean content for child pages: unwrap shared-blocks, strip media, fix panel/expand."""
    result = []
    for node in nodes:
        node = copy.deepcopy(node)
        ext_key = node.get("attrs", {}).get("extensionKey", "")
        if ext_key == "shared-block" and node.get("type") == "bodiedExtension":
            result.extend(clean_child_content(node.get("content", [])))
            continue
        if node.get("type") == "mediaSingle":
            has_broken = False
            for child in node.get("content", []):
                if child.get("type") == "media":
                    media_id = child.get("attrs", {}).get("id", "")
                    if media_id == "UNKNOWN_MEDIA_ID":
                        has_broken = True
                        break
                if child.get("type") == "image" or node.get("type") == "mediaSingle":
                    # Check for UNKNOWN_ATTACHMENT in nested image references
                    pass
            if has_broken:
                result.append({"type": "paragraph", "content": [
                    {"type": "text", "text": "[Image removed — migration artifact]"}]})
                continue
            if "content" in node:
                node["content"] = clean_child_content(node["content"])
            result.append(node)
            continue
        if node.get("type") == "panel" and "content" in node:
            panel_content, extracted = [], []
            for child in node["content"]:
                (extracted if child.get("type") == "expand" else panel_content).append(child)
            if extracted:
                if panel_content:
                    node["content"] = clean_child_content(panel_content)
                    result.append(node)
                for exp in extracted:
                    if "content" in exp:
                        exp["content"] = clean_child_content(exp["content"])
                    result.append(exp)
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


# ---------------------------------------------------------------------------
# Consolidated transform
# ---------------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.legacy_removed = 0
        self.etp_removed = 0
        self.isb_replaced = 0
        self.sb_replaced = 0
        self.help_text_converted = 0
        self.help_text_sb_removed = 0
        self.buttons_converted = 0
        self.placeholders_converted = 0
        self.panel_expand_fixed = 0
        self.media_stripped = 0
        self.livesearch_fixed = 0
        self.links_fixed = 0

    @property
    def has_changes(self):
        return any(v > 0 for v in vars(self).values())

    def summary(self):
        parts = []
        if self.legacy_removed: parts.append(f"legacy={self.legacy_removed}")
        if self.etp_removed: parts.append(f"etp={self.etp_removed}")
        if self.isb_replaced: parts.append(f"isb={self.isb_replaced}")
        if self.sb_replaced: parts.append(f"sb={self.sb_replaced}")
        if self.help_text_converted: parts.append(f"help-text={self.help_text_converted}")
        if self.buttons_converted: parts.append(f"buttons={self.buttons_converted}")
        if self.placeholders_converted: parts.append(f"placeholders={self.placeholders_converted}")
        if self.panel_expand_fixed: parts.append(f"panel-fix={self.panel_expand_fixed}")
        if self.media_stripped: parts.append(f"media={self.media_stripped}")
        if self.livesearch_fixed: parts.append(f"livesearch={self.livesearch_fixed}")
        if self.links_fixed: parts.append(f"links={self.links_fixed}")
        return ", ".join(parts) if parts else "no changes"


def transform_node(node, stats, space_key, child_page_map, dc_base_url, title_to_id):
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # --- 1. legacy-content → unwrap ---
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            stats.legacy_removed += 1
            result = []
            for child in nested["content"]:
                result.extend(transform_node(child, stats, space_key, child_page_map, dc_base_url, title_to_id))
            return result
        stats.legacy_removed += 1
        return []

    # --- 2. extra-table-properties → unwrap ---
    if ext_key == "extra-table-properties" and node_type == "bodiedExtension":
        stats.etp_removed += 1
        result = []
        for child in node.get("content", []):
            result.extend(transform_node(child, stats, space_key, child_page_map, dc_base_url, title_to_id))
        return result

    # --- 3. include-shared-block → excerpt-include or include ---
    if ext_key == "include-shared-block":
        source_page = get_macro_param(node, "page")
        sb_key = get_macro_param(node, "shared-block-key")
        map_key = (source_page, sb_key)
        if map_key in TEMPLATE_INCLUDE_MAP:
            child_title = TEMPLATE_INCLUDE_MAP[map_key]
            stats.isb_replaced += 1
            if child_title == DEFINITION_INCLUDE_PAGE:
                return [make_include_page(child_title)]
            return [make_excerpt_include(child_title, space_key)]
        return [node]

    # --- 4. shared-block → excerpt-include to child page ---
    if ext_key == "shared-block" and node_type == "bodiedExtension":
        sb_key = get_macro_param(node, "shared-block-key")
        if sb_key in child_page_map:
            stats.sb_replaced += 1
            return [make_excerpt_include(child_page_map[sb_key], space_key)]
        return [node]

    # --- 5. help-text → native expand ---
    if ext_key == "help-text" and node_type == "bodiedExtension":
        title = get_macro_param(node, "text") or get_macro_param(node, "title") or "Details"
        tip = get_macro_param(node, "tip")
        body_content = node.get("content", [])
        expand_content = []
        if tip and tip.strip():
            expand_content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": tip.strip(), "marks": [{"type": "em"}]}],
            })
        for child in body_content:
            expand_content.extend(transform_node(child, stats, space_key, child_page_map, dc_base_url, title_to_id))
        if not expand_content:
            expand_content.append({"type": "paragraph", "content": [{"type": "text", "text": " "}]})
        stats.help_text_converted += 1
        return [{"type": "expand", "attrs": {"title": title}, "content": expand_content}]

    # --- 6. help-text-from-shared-block → remove ---
    if ext_key == "help-text-from-shared-block":
        stats.help_text_sb_removed += 1
        return []

    # --- 7. button → inline link ---
    if ext_key == "button":
        btn_text = get_macro_param(node, "button-text")
        btn_url = get_macro_param(node, "button-url")
        if dc_base_url and dc_base_url in btn_url:
            btn_url = fix_dc_url(btn_url, dc_base_url, space_key, title_to_id)
        stats.buttons_converted += 1
        return [{"type": "text", "text": btn_text, "marks": [{"type": "link", "attrs": {"href": btn_url}}]}]

    # --- 8. placeholder → italic text ---
    if node_type == "placeholder":
        text = attrs.get("text", "")
        if text:
            stats.placeholders_converted += 1
            return [{"type": "text", "text": text, "marks": [{"type": "em"}]}]
        return []

    # --- 9. panel containing expand → fix nesting ---
    if node_type == "panel" and "content" in node:
        panel_content, extracted = [], []
        for child in node.get("content", []):
            (extracted if child.get("type") == "expand" else panel_content).append(child)
        if extracted:
            stats.panel_expand_fixed += 1
            result = []
            if panel_content:
                new_panel = copy.deepcopy(node)
                new_panel["content"] = []
                for pc in panel_content:
                    new_panel["content"].extend(
                        transform_node(pc, stats, space_key, child_page_map, dc_base_url, title_to_id))
                result.append(new_panel)
            for exp in extracted:
                result.extend(transform_node(exp, stats, space_key, child_page_map, dc_base_url, title_to_id))
            return result

    # --- 10. UNKNOWN_MEDIA_ID / UNKNOWN_ATTACHMENT → strip ---
    if node_type == "mediaSingle":
        for child in node.get("content", []):
            if child.get("type") == "media":
                media_id = child.get("attrs", {}).get("id", "")
                if media_id == "UNKNOWN_MEDIA_ID":
                    stats.media_stripped += 1
                    return [{"type": "paragraph", "content": [
                        {"type": "text", "text": "[Image removed — migration artifact]"}]}]
                # Check for UNKNOWN_ATTACHMENT in collection field
                collection = child.get("attrs", {}).get("collection", "")
                if "UNKNOWN" in media_id.upper() or "UNKNOWN" in collection.upper():
                    stats.media_stripped += 1
                    return [{"type": "paragraph", "content": [
                        {"type": "text", "text": "[Image removed — migration artifact]"}]}]

    # --- 11. livesearch spaceKey fix ---
    if ext_key == "livesearch":
        params = attrs.get("parameters", {}).get("macroParams", {})
        sk = params.get("spaceKey", {})
        if isinstance(sk, dict) and sk.get("value") != space_key:
            old = sk["value"]
            sk["value"] = space_key
            stats.livesearch_fixed += 1

    # --- 12. DC links → Cloud URLs ---
    if "marks" in node and isinstance(node["marks"], list) and dc_base_url:
        for mark in node["marks"]:
            if mark.get("type") == "link":
                href = mark.get("attrs", {}).get("href", "")
                if dc_base_url in href:
                    new_href = fix_dc_url(href, dc_base_url, space_key, title_to_id)
                    if new_href != href:
                        mark["attrs"]["href"] = new_href
                        stats.links_fixed += 1

    # --- Recurse into children ---
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map, dc_base_url, title_to_id))
        node["content"] = new_content

    return [node]


def fix_dc_url(href, dc_base_url, space_key, title_to_id):
    # display/SPACE/Title pattern
    m = re.match(re.escape(dc_base_url) + r'/display/\w+/(.+)', href)
    if m:
        title = requests.utils.unquote(m.group(1)).replace('+', ' ')
        pid = title_to_id.get(title)
        if pid:
            return f"{BASE_URL}/wiki/spaces/{space_key}/pages/{pid}/{requests.utils.quote(title, safe='')}"

    # CQL search pattern
    m2 = re.search(r'cql=(.+?)(?:&|$)', href)
    if m2:
        cql = requests.utils.unquote(m2.group(1))
        cql = re.sub(r'"GLOS"', f'"{space_key}"', cql)
        cql = re.sub(r'GLOS', space_key, cql)
        return f"{BASE_URL}/wiki/search?cql={requests.utils.quote(cql)}"

    return href


def transform_body(body, space_key, child_page_map, dc_base_url, title_to_id):
    body = copy.deepcopy(body)
    stats = Stats()
    if "content" in body:
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, stats, space_key, child_page_map, dc_base_url, title_to_id))
        body["content"] = new_content
    return body, stats


# ---------------------------------------------------------------------------
# Process a single page
# ---------------------------------------------------------------------------

def process_page(api, space_id, space_key, page_id, dry_run, dc_base_url, title_to_id):
    page = api.get_page_body(page_id)
    title = page["title"]

    # Pass 1: Find shared-blocks and create child pages
    shared_blocks = find_shared_blocks(page["body"])
    child_page_map = {}

    for sb in shared_blocks:
        sb_key = sb["key"]
        child_title = f"_{title} - {sb_key}"

        if dry_run:
            child_page_map[sb_key] = child_title
            continue

        existing = api.find_page_by_title(space_id, child_title)
        if existing:
            child_page_map[sb_key] = child_title
            continue

        content = clean_child_content(copy.deepcopy(sb["content"]))
        excerpt_body = make_excerpt_body(content)
        result = api.create_page(space_id, page_id, child_title, excerpt_body)
        if result:
            child_page_map[sb_key] = child_title
        else:
            log.error(f"  [{title}] Failed to create child: {child_title}")

    # Pass 2: Transform the page ADF (single pass, all transforms)
    new_body, stats = transform_body(page["body"], space_key, child_page_map, dc_base_url, title_to_id)

    if not stats.has_changes:
        return {"title": title, "page_id": page_id, "changed": False,
                "children": len(child_page_map), "stats": "no changes", "error": None}

    if dry_run:
        return {"title": title, "page_id": page_id, "changed": True,
                "children": len(child_page_map), "stats": stats.summary(), "error": None}

    ok = api.update_page(page_id, title, page["version"], new_body)
    return {"title": title, "page_id": page_id, "changed": True,
            "children": len(child_page_map), "stats": stats.summary(),
            "error": None if ok else "Update failed"}


# ---------------------------------------------------------------------------
# Setup: discover Template Resources children and build maps
# ---------------------------------------------------------------------------

def build_template_include_map(api, space_id, template_resources_title):
    """Find the Template Resources page and its _-prefixed children to build the include map."""
    tr_page = api.find_page_by_title(space_id, template_resources_title)
    if not tr_page:
        log.warning(f"  Template Resources page '{template_resources_title}' not found")
        return

    # Known shared-block keys on Template Resources
    keys = ["Definition(s)", "Internal Resources", "External Resources", "Other Data", "GlossaryHelpText"]
    for key in keys:
        child_title = f"_{key}"
        child = api.find_page_by_title(space_id, child_title)
        if child:
            TEMPLATE_INCLUDE_MAP[(template_resources_title, key)] = child_title
            log.info(f"  Mapped ({template_resources_title}, {key}) → {child_title}")
        else:
            log.warning(f"  Child page '{child_title}' not found — include-shared-blocks for '{key}' won't be converted")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Consolidated Glossary Migration Script")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--page-id", help="Process a single page")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--batch-delay", type=float, default=1.5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-json")
    parser.add_argument("--dc-base-url", default="https://confluence.tempustechnologies.com",
                        help="Old DC Confluence base URL for link fixing")
    parser.add_argument("--template-resources-title", default="Glossary Template Resources")
    parser.add_argument("--skip-page-ids", nargs="*", default=[],
                        help="Additional page IDs to skip")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)
    space_key = args.space_key
    api.set_space_key(space_key)
    dc_base_url = args.dc_base_url

    log.info(f"Space: {space_key}")
    space_id = api.get_space_id(space_key)
    log.info(f"Space ID: {space_id}")

    # Build Template Resources include map
    log.info("Discovering Template Resources children...")
    build_template_include_map(api, space_id, args.template_resources_title)

    # Build title → page ID map for DC link fixing
    log.info("Building page title → ID map...")
    all_pages = api.get_all_pages(space_id)
    title_to_id = {t: pid for pid, t in all_pages}
    log.info(f"  {len(title_to_id)} pages indexed")

    # Build skip set
    SKIP_PAGE_IDS.update(args.skip_page_ids)
    # Find and skip special pages
    for special_title in [args.template_resources_title, "Glossary Home Page",
                          "Glossary Overview and How-To", "_Glossary Main Directory",
                          "Glossary Libraries"]:
        sp = api.find_page_by_title(space_id, special_title)
        if sp:
            SKIP_PAGE_IDS.add(sp["id"])
            log.debug(f"  Skipping: {special_title} ({sp['id']})")

    # Single page mode
    if args.page_id:
        log.info(f"\nProcessing single page: {args.page_id}")
        result = process_page(api, space_id, space_key, args.page_id, args.dry_run, dc_base_url, title_to_id)
        mode = "DRY RUN" if args.dry_run else "LIVE"
        print(f"\n  [{mode}] {result['title']}")
        print(f"    Children: {result['children']}")
        print(f"    Transforms: {result['stats']}")
        if result.get("error"):
            print(f"    ERROR: {result['error']}")
        return

    # Full space mode
    pages_to_process = [
        (pid, t) for pid, t in all_pages
        if pid not in SKIP_PAGE_IDS and not t.startswith("_")
    ]
    log.info(f"\nPages to process: {len(pages_to_process)} (skipping {len(SKIP_PAGE_IDS)} special + _-prefixed)")

    if args.limit:
        pages_to_process = pages_to_process[:args.limit]

    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0, "children_created": 0}
    all_results = []
    total = len(pages_to_process)

    for batch_start in range(0, total, args.batch_size):
        batch = pages_to_process[batch_start:batch_start + args.batch_size]
        batch_num = (batch_start // args.batch_size) + 1
        total_batches = (total + args.batch_size - 1) // args.batch_size
        log.info(f"--- Batch {batch_num}/{total_batches} (pages {batch_start+1}-{batch_start+len(batch)}) ---")

        for page_id, page_title in batch:
            totals["scanned"] += 1
            try:
                result = process_page(api, space_id, space_key, page_id, args.dry_run, dc_base_url, title_to_id)
                all_results.append(result)

                if result.get("error"):
                    totals["errored"] += 1
                elif result["changed"]:
                    totals["modified"] += 1
                    totals["children_created"] += result["children"]
                else:
                    totals["skipped"] += 1

            except Exception as e:
                totals["errored"] += 1
                all_results.append({"page_id": page_id, "title": page_title, "error": str(e)})
                log.error(f"  [{page_title}] Error: {e}")

        if batch_start + args.batch_size < total and args.batch_delay > 0:
            time.sleep(args.batch_delay)

    # Summary
    mode = "DRY RUN" if args.dry_run else "LIVE RUN"
    print(f"\n{'=' * 65}")
    print(f"  CONSOLIDATED MIGRATION SUMMARY ({mode})")
    print(f"{'=' * 65}")
    print(f"  Pages scanned:          {totals['scanned']}")
    print(f"  Pages modified:         {totals['modified']}")
    print(f"  Pages with no changes:  {totals['skipped']}")
    print(f"  Pages with errors:      {totals['errored']}")
    print(f"  Child pages created:    {totals['children_created']}")
    print(f"{'=' * 65}")

    if totals["errored"] > 0:
        print(f"\n  Errors:")
        for r in all_results:
            if r.get("error"):
                print(f"    - {r['title']} ({r['page_id']}): {r['error']}")

    if args.output_json:
        path = os.path.expanduser(args.output_json)
        with open(path, "w") as f:
            json.dump({"summary": totals, "pages": all_results}, f, indent=2)
        log.info(f"Report: {path}")


if __name__ == "__main__":
    main()
