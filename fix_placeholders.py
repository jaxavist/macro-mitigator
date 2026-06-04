#!/usr/bin/env python3
"""
Fix placeholder content in excerpt child pages.

Scans all _-prefixed child pages and converts placeholder ADF nodes
to regular text paragraphs so they render in excerpt-includes.

Usage:
    python fix_placeholders.py --space-key CLOS --dry-run
    python fix_placeholders.py --space-key CLOS --page-id 106594993 --dry-run
    python fix_placeholders.py --space-key CLOS --batch-size 20

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
log = logging.getLogger("fix_placeholders")


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
        msg = message or "Fix: convert placeholders to visible text"
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
        return False


def convert_placeholders(node, stats):
    """
    Recursively convert placeholder nodes to regular text.

    ADF placeholder: {"type": "placeholder", "attrs": {"text": "..."}}
    Converted to: {"type": "text", "text": "...", "marks": [{"type": "em"}]}

    The italic mark distinguishes former placeholder text from real content.
    """
    if node.get("type") == "placeholder":
        text = node.get("attrs", {}).get("text", "")
        if text:
            stats["converted"] += 1
            return {
                "type": "text",
                "text": text,
                "marks": [{"type": "em"}]
            }
        return None

    if "content" in node and isinstance(node["content"], list):
        new_content = []
        for child in node["content"]:
            result = convert_placeholders(child, stats)
            if result is not None:
                new_content.append(result)
        node["content"] = new_content

    return node


def has_placeholders(node):
    """Check if a node tree contains any placeholder nodes."""
    if node.get("type") == "placeholder":
        return True
    for child in node.get("content", []):
        if has_placeholders(child):
            return True
    return False


def main():
    parser = argparse.ArgumentParser(description="Fix placeholder content in excerpt child pages")
    parser.add_argument("--space-key", required=True)
    parser.add_argument("--page-id", help="Fix a single page by ID")
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
    space_id = api.get_space_id(args.space_key)

    # Single page mode
    if args.page_id:
        page = api.get_page_body(args.page_id)
        log.info(f"Processing: {page['title']} (v{page['version']})")
        if not has_placeholders(page["body"]):
            log.info("  No placeholders found")
            return
        new_body = copy.deepcopy(page["body"])
        stats = {"converted": 0}
        convert_placeholders(new_body, stats)
        log.info(f"  Placeholders converted: {stats['converted']}")
        if stats["converted"] > 0 and not args.dry_run:
            ok = api.update_page(page["id"], page["title"], page["version"], new_body)
            log.info(f"  {'Updated' if ok else 'FAILED'}")
        elif stats["converted"] > 0:
            log.info(f"  DRY RUN — would update")
        return

    # Full space mode — only process _-prefixed pages (excerpt child pages)
    log.info(f"Collecting pages...")
    all_pages = api.get_all_pages(space_id)
    child_pages = [(pid, t) for pid, t in all_pages if t.startswith("_")]
    log.info(f"Found {len(child_pages)} excerpt child pages (_{{}}-prefixed)")

    if args.limit:
        child_pages = child_pages[:args.limit]

    totals = {"scanned": 0, "modified": 0, "skipped": 0, "errored": 0, "converted": 0}

    for batch_start in range(0, len(child_pages), args.batch_size):
        batch = child_pages[batch_start:batch_start + args.batch_size]
        batch_num = (batch_start // args.batch_size) + 1
        total_batches = (len(child_pages) + args.batch_size - 1) // args.batch_size
        log.info(f"--- Batch {batch_num}/{total_batches} ---")

        for page_id, title in batch:
            totals["scanned"] += 1
            try:
                page = api.get_page_body(page_id)
                if not has_placeholders(page["body"]):
                    totals["skipped"] += 1
                    continue

                new_body = copy.deepcopy(page["body"])
                stats = {"converted": 0}
                convert_placeholders(new_body, stats)

                if stats["converted"] > 0:
                    if args.dry_run:
                        totals["modified"] += 1
                    else:
                        ok = api.update_page(page_id, title, page["version"], new_body)
                        if ok:
                            totals["modified"] += 1
                        else:
                            totals["errored"] += 1
                    totals["converted"] += stats["converted"]
                else:
                    totals["skipped"] += 1

            except Exception as e:
                totals["errored"] += 1
                log.error(f"  [{title}] Error: {e}")

        if batch_start + args.batch_size < len(child_pages) and args.batch_delay > 0:
            time.sleep(args.batch_delay)

    mode = "DRY RUN" if args.dry_run else "LIVE RUN"
    print(f"\n{'=' * 60}")
    print(f"  PLACEHOLDER FIX SUMMARY ({mode})")
    print(f"{'=' * 60}")
    print(f"  Child pages scanned:         {totals['scanned']}")
    print(f"  Pages with placeholders:     {totals['modified']}")
    print(f"  Pages without placeholders:  {totals['skipped']}")
    print(f"  Pages with errors:           {totals['errored']}")
    print(f"  Placeholders converted:      {totals['converted']}")
    print(f"{'=' * 60}")

    if args.output_json:
        with open(os.path.expanduser(args.output_json), "w") as f:
            json.dump(totals, f, indent=2)


if __name__ == "__main__":
    main()
