#!/usr/bin/env python3
"""
Phase 4: Convert help-text macros to native Expand macros.

Replaces ScriptRunner help-text bodiedExtensions with native ADF expand nodes.
Tip text is added as an italic paragraph inside the expand.

Usage:
    python phase4_help_text.py --space-key CLOS --page-id 106596385 --dry-run
    python phase4_help_text.py --space-key CLOS --dry-run
    python phase4_help_text.py --space-key CLOS --batch-size 20

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

import requests

BASE_URL = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("CONFLUENCE_EMAIL", "")
API_TOKEN = os.environ.get("CONFLUENCE_API_TOKEN", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase4")


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
        return {
            "id": data["id"], "title": data["title"],
            "version": data["version"]["number"],
            "body": json.loads(data["body"]["atlas_doc_format"]["value"]),
        }

    def update_page(self, page_id, title, version, body, message=""):
        msg = message or "Phase 4: help-text to native expand"
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
            log.error(f"  v1 fallback failed for '{title}': {resp2.status_code}")
            return False
        log.error(f"  Update failed for '{title}': {resp.status_code}")
        return False


def get_macro_param(node, param_name):
    params = node.get("attrs", {}).get("parameters", {}).get("macroParams", {})
    val = params.get(param_name, {})
    return val.get("value", "") if isinstance(val, dict) else val


def transform_node(node, stats):
    node_type = node.get("type", "")
    attrs = node.get("attrs", {})
    ext_key = attrs.get("extensionKey", "")
    ext_type = attrs.get("extensionType", "")

    # --- help-text → native expand ---
    if ext_key == "help-text" and node_type == "bodiedExtension":
        title = get_macro_param(node, "text") or get_macro_param(node, "title") or "Details"
        tip = get_macro_param(node, "tip")
        body_content = node.get("content", [])

        # Build expand content: tip (italic) + original body
        expand_content = []
        if tip and tip.strip():
            expand_content.append({
                "type": "paragraph",
                "content": [{
                    "type": "text",
                    "text": tip.strip(),
                    "marks": [{"type": "em"}],
                }],
            })

        # Process body content (unwrap legacy wrappers, convert placeholders)
        for child in body_content:
            expand_content.extend(transform_node(child, stats))

        if not expand_content:
            expand_content.append({
                "type": "paragraph",
                "content": [{"type": "text", "text": " "}],
            })

        stats["help_text_converted"] += 1
        return [{
            "type": "expand",
            "attrs": {"title": title},
            "content": expand_content,
        }]

    # --- help-text-from-shared-block → remove (already handled or orphaned) ---
    if ext_key == "help-text-from-shared-block":
        stats["help_text_sb_removed"] += 1
        return []

    # --- legacy-content → unwrap ---
    if ext_type == "com.atlassian.confluence.migration" and ext_key == "legacy-content":
        nested = attrs.get("parameters", {}).get("nestedContent", {})
        if nested and nested.get("content"):
            stats["legacy_removed"] += 1
            result = []
            for child in nested["content"]:
                result.extend(transform_node(child, stats))
            return result
        stats["legacy_removed"] += 1
        return []

    # --- Convert placeholders to italic text ---
    if node_type == "placeholder":
        text = attrs.get("text", "")
        if text:
            stats["placeholders_converted"] += 1
            return [{
                "type": "text",
                "text": text,
                "marks": [{"type": "em"}],
            }]
        return []

    # --- Recurse ---
    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            new_content.extend(transform_node(child, stats))
        node["content"] = new_content

    return [node]


def transform_body(body):
    body = copy.deepcopy(body)
    stats = {"help_text_converted": 0, "help_text_sb_removed": 0,
             "legacy_removed": 0, "placeholders_converted": 0}
    if "content" in body:
        new_content = []
        for child in body["content"]:
            new_content.extend(transform_node(child, stats))
        body["content"] = new_content
    return body, stats


def main():
    parser = argparse.ArgumentParser(description="Phase 4: help-text to native expand")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--page-id", help="Single page")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--batch-delay", type=float, default=1.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-json")
    args = parser.parse_args()

    if not BASE_URL or not EMAIL or not API_TOKEN:
        print("ERROR: Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN")
        sys.exit(1)

    api = ConfluenceAPI(BASE_URL, EMAIL, API_TOKEN)

    if args.page_id:
        page = api.get_page_body(args.page_id)
        log.info(f"Processing: {page['title']} (v{page['version']})")
        new_body, stats = transform_body(page["body"])
        log.info(f"  help-text converted: {stats['help_text_converted']}")
        log.info(f"  help-text-from-shared-block removed: {stats['help_text_sb_removed']}")
        log.info(f"  legacy wrappers removed: {stats['legacy_removed']}")
        log.info(f"  placeholders converted: {stats['placeholders_converted']}")
        has_changes = any(v > 0 for v in stats.values())
        if has_changes and not args.dry_run:
            ok = api.update_page(page["id"], page["title"], page["version"], new_body)
            log.info(f"  {'Updated' if ok else 'FAILED'}")
        elif has_changes:
            log.info(f"  DRY RUN — would update")
        else:
            log.info(f"  No changes needed")
        return

    space_id = api.get_space_id(args.space_key)
    all_pages = api.get_all_pages(space_id)
    pages = [(pid, t) for pid, t in all_pages if not t.startswith("_")]
    log.info(f"Pages to process: {len(pages)}")

    if args.limit:
        pages = pages[:args.limit]

    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0,
              "ht": 0, "htsb": 0, "legacy": 0, "ph": 0}
    errors = []

    for batch_start in range(0, len(pages), args.batch_size):
        batch = pages[batch_start:batch_start + args.batch_size]
        batch_num = (batch_start // args.batch_size) + 1
        total_batches = (len(pages) + args.batch_size - 1) // args.batch_size
        log.info(f"--- Batch {batch_num}/{total_batches} ---")

        for page_id, title in batch:
            totals["scanned"] += 1
            try:
                page = api.get_page_body(page_id)
                new_body, stats = transform_body(page["body"])
                has_changes = any(v > 0 for v in stats.values())

                if has_changes:
                    if args.dry_run:
                        totals["modified"] += 1
                    else:
                        ok = api.update_page(page_id, title, page["version"], new_body)
                        if ok:
                            totals["modified"] += 1
                        else:
                            totals["errored"] += 1
                            errors.append({"page_id": page_id, "title": title})
                    totals["ht"] += stats["help_text_converted"]
                    totals["htsb"] += stats["help_text_sb_removed"]
                    totals["legacy"] += stats["legacy_removed"]
                    totals["ph"] += stats["placeholders_converted"]
                else:
                    totals["skipped"] += 1

            except Exception as e:
                totals["errored"] += 1
                errors.append({"page_id": page_id, "title": title, "error": str(e)})
                log.error(f"  [{title}] Error: {e}")

        if batch_start + args.batch_size < len(pages) and args.batch_delay > 0:
            time.sleep(args.batch_delay)

    mode = "DRY RUN" if args.dry_run else "LIVE RUN"
    print(f"\n{'=' * 60}")
    print(f"  PHASE 4 SUMMARY ({mode})")
    print(f"{'=' * 60}")
    print(f"  Pages scanned:                  {totals['scanned']}")
    print(f"  Pages modified:                 {totals['modified']}")
    print(f"  Pages with no changes:          {totals['skipped']}")
    print(f"  Pages with errors:              {totals['errored']}")
    print(f"  ---")
    print(f"  help-text → native expand:      {totals['ht']}")
    print(f"  help-text-from-shared-block:    {totals['htsb']}")
    print(f"  legacy wrappers removed:        {totals['legacy']}")
    print(f"  placeholders converted:         {totals['ph']}")
    print(f"{'=' * 60}")

    if errors:
        print(f"\n  Errors:")
        for e in errors:
            print(f"    - {e['title']} ({e['page_id']})")

    if args.output_json:
        with open(os.path.expanduser(args.output_json), "w") as f:
            json.dump({"summary": totals, "errors": errors}, f, indent=2)


if __name__ == "__main__":
    main()
