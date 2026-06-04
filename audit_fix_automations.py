"""
Daybreak Games — Cloud Automation Field Auditor & Fixer (v3)
=============================================================
Dynamically builds the DC→Cloud field mapping by:
  1. Pulling ALL custom fields from both DC and Cloud REST APIs
  2. Matching by field name + type to build the complete mapping
  3. Scanning every automation rule for any field ID not in Cloud
  4. Optionally fixing them via the Automation REST API

Usage:
    # Audit — finds all bad field references:
    python3 audit_fix_automations.py --audit --token "CLOUD_TOKEN"

    # Fix — audit + replace in place:
    python3 audit_fix_automations.py --fix --token "CLOUD_TOKEN"

    # Dry run — show what fix would do:
    python3 audit_fix_automations.py --fix --dry-run --token "CLOUD_TOKEN"

Requirements:
    pip install requests
"""

import json
import sys
import os
import argparse
import requests
from collections import defaultdict

# ── Configuration ─────────────────────────────────────────────
CLOUD_SITE = "daybreakgames-sandbox.atlassian.net"
CLOUD_ID = "37b586d4-858b-41c8-914a-274e219122cd"
DC_SITE = "jira-test.daybreakgames.com"

AUTOMATION_BASE = f"https://{CLOUD_SITE}/gateway/api/automation/public/jira/{CLOUD_ID}/rest/v1"
CLOUD_FIELDS_URL = f"https://{CLOUD_SITE}/rest/api/3/field"
DC_FIELDS_URL = f"https://{DC_SITE}/rest/api/2/field"

TARGET_SCOPES = {
    f"ari:cloud:jira:{CLOUD_ID}:project/10034",  # ITSM
    f"ari:cloud:jira:{CLOUD_ID}:project/10041",  # SSSD
}


def fetch_fields(url, auth=None, headers=None, label=""):
    """Fetch all custom fields from a Jira instance."""
    print(f"  Fetching {label} fields from {url}...")
    kwargs = {"headers": {"Accept": "application/json"}}
    if auth:
        kwargs["auth"] = auth
    if headers:
        kwargs["headers"].update(headers)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    fields = resp.json()
    custom = {f["id"]: f for f in fields if f.get("custom", False) or f["id"].startswith("customfield_")}
    print(f"  Found {len(custom)} custom fields on {label}")
    return custom


def build_field_mapping(dc_fields, cloud_fields):
    """Build DC→Cloud mapping by matching on field name + type."""
    # Index cloud fields by name
    cloud_by_name = defaultdict(list)
    for fid, f in cloud_fields.items():
        name = f.get("name", f.get("untranslatedName", "")).strip()
        cloud_by_name[name.lower()].append(f)

    mapping = {}
    ambiguous = []
    unmapped = []

    for dc_id, dc_field in dc_fields.items():
        if dc_id in cloud_fields:
            continue  # Same ID exists on Cloud — might be correct or might be a collision

        dc_name = dc_field.get("name", "").strip()
        dc_type = dc_field.get("schema", {}).get("custom", "")

        candidates = cloud_by_name.get(dc_name.lower(), [])

        if len(candidates) == 1:
            cloud_field = candidates[0]
            mapping[dc_id] = {
                "cloud_id": cloud_field["id"],
                "name": dc_name,
                "dc_type": dc_type,
                "cloud_type": cloud_field.get("schema", {}).get("custom", ""),
            }
        elif len(candidates) > 1:
            # Try to narrow by type
            type_matches = [c for c in candidates if c.get("schema", {}).get("custom", "") == dc_type]
            if len(type_matches) == 1:
                cloud_field = type_matches[0]
                mapping[dc_id] = {
                    "cloud_id": cloud_field["id"],
                    "name": dc_name,
                    "dc_type": dc_type,
                    "cloud_type": cloud_field.get("schema", {}).get("custom", ""),
                }
            else:
                ambiguous.append({"dc_id": dc_id, "name": dc_name, "candidates": [c["id"] for c in candidates]})
        # If no candidates, it's unmapped but we only care when it appears in an automation

    return mapping, ambiguous


def find_custom_field_refs(obj, path=""):
    """Find all customfield_ references in a JSON structure."""
    refs = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            p = f"{path}.{key}" if path else key
            if isinstance(val, str) and "customfield_" in val:
                # Extract all customfield IDs from this string
                import re
                for match in re.finditer(r'customfield_\d+', val):
                    refs.append((p, match.group()))
            else:
                refs.extend(find_custom_field_refs(val, p))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            refs.extend(find_custom_field_refs(item, f"{path}[{i}]"))
    return refs


def replace_field_ids(obj, mapping):
    """Replace DC field IDs with Cloud IDs. Returns replacement count."""
    count = 0
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if isinstance(val, str):
                new_val = val
                for dc_id, info in mapping.items():
                    if dc_id in new_val:
                        new_val = new_val.replace(dc_id, info["cloud_id"])
                        count += 1
                if new_val != val:
                    obj[key] = new_val
            else:
                count += replace_field_ids(val, mapping)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_item = item
                for dc_id, info in mapping.items():
                    if dc_id in new_item:
                        new_item = new_item.replace(dc_id, info["cloud_id"])
                        count += 1
                if new_item != item:
                    obj[i] = new_item
            else:
                count += replace_field_ids(item, mapping)
    return count


class AutomationClient:
    def __init__(self, email, token):
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def list_rules(self):
        all_rules = []
        cursor = None
        while True:
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = self.session.get(f"{AUTOMATION_BASE}/rule/summary", params=params)
            resp.raise_for_status()
            data = resp.json()
            rules = data.get("data", [])
            all_rules.extend(rules)
            next_link = data.get("links", {}).get("next", "")
            if next_link and "cursor=" in next_link:
                cursor = next_link.split("cursor=")[1].split("&")[0]
            else:
                break
        return all_rules

    def get_rule(self, uuid):
        resp = self.session.get(f"{AUTOMATION_BASE}/rule/{uuid}")
        resp.raise_for_status()
        return resp.json()

    def update_rule(self, uuid, data):
        resp = self.session.put(f"{AUTOMATION_BASE}/rule/{uuid}", json=data)
        resp.raise_for_status()
        return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Audit/fix Cloud automations using dynamic field mapping")
    parser.add_argument("--audit", action="store_true", help="Audit only")
    parser.add_argument("--fix", action="store_true", help="Audit + fix")
    parser.add_argument("--dry-run", action="store_true", help="Show what fix would do")
    parser.add_argument("--all", action="store_true", help="Scan all projects, not just ITSM/SSSD")
    parser.add_argument("--rule-uuid", help="Target one rule")
    parser.add_argument("--email", default=os.environ.get("ATLASSIAN_EMAIL", "jkane@adaptavist.com"))
    parser.add_argument("--token", default=os.environ.get("ATLASSIAN_API_TOKEN"))
    parser.add_argument("--dc-token", default=os.environ.get("DC_API_TOKEN"), help="DC Personal Access Token (Bearer auth)")
    parser.add_argument("--skip-dc", action="store_true", help="Skip DC fetch, only check if field exists on Cloud")
    args = parser.parse_args()

    if not args.audit and not args.fix:
        parser.print_help()
        sys.exit(1)
    if not args.token:
        print("Error: --token required")
        sys.exit(1)

    cloud_auth = (args.email, args.token)
    auto_client = AutomationClient(args.email, args.token)

    # ── Step 1: Build field mapping ───────────────────────────
    print("\n" + "=" * 60)
    print("STEP 1: Building DC → Cloud field mapping")
    print("=" * 60)

    cloud_fields = fetch_fields(CLOUD_FIELDS_URL, auth=cloud_auth, label="Cloud")
    cloud_field_ids = set(cloud_fields.keys())

    if not args.skip_dc:
        try:
            if args.dc_token:
                dc_headers = {"Authorization": f"Bearer {args.dc_token}"}
                dc_fields = fetch_fields(DC_FIELDS_URL, headers=dc_headers, label="DC")
            else:
                dc_fields = fetch_fields(DC_FIELDS_URL, auth=cloud_auth, label="DC")
            field_mapping, ambiguous = build_field_mapping(dc_fields, cloud_fields)
            print(f"\n  Mapped {len(field_mapping)} DC fields to Cloud equivalents")
            if ambiguous:
                print(f"  ⚠ {len(ambiguous)} ambiguous (multiple Cloud matches):")
                for a in ambiguous[:5]:
                    print(f"    {a['dc_id']} ({a['name']}) → candidates: {a['candidates']}")
        except Exception as e:
            print(f"  ⚠ Could not fetch DC fields: {e}")
            print(f"  Falling back to Cloud-only mode (will flag any field not in Cloud)")
            field_mapping = {}
            args.skip_dc = True
    else:
        field_mapping = {}

    # ── Step 2: Scan automation rules ─────────────────────────
    print(f"\n{'=' * 60}")
    print("STEP 2: Scanning automation rules")
    print("=" * 60)

    if args.rule_uuid:
        summaries = [{"uuid": args.rule_uuid, "name": "(direct)", "state": "?", "ruleScopeARIs": []}]
    else:
        print("\nFetching rule list...")
        summaries = auto_client.list_rules()
        print(f"Total rules: {len(summaries)}")

    issues = []
    clean = 0
    skipped = 0

    for summary in summaries:
        uuid = summary.get("uuid")
        name = summary.get("name", "?")
        scopes = summary.get("ruleScopeARIs", [])

        if not args.all and not args.rule_uuid:
            if not any(s in TARGET_SCOPES for s in scopes):
                skipped += 1
                continue

        try:
            full_rule = auto_client.get_rule(uuid)
        except requests.HTTPError as e:
            print(f"  ✗ Could not fetch {name}: {e}")
            continue

        # Find all customfield_ references
        refs = find_custom_field_refs(full_rule)

        # Check which ones are NOT valid Cloud fields
        bad_refs = []
        for path, field_id in refs:
            if field_id not in cloud_field_ids:
                cloud_equiv = field_mapping.get(field_id, {}).get("cloud_id", "???")
                field_name = field_mapping.get(field_id, {}).get("name", "unknown")
                bad_refs.append((path, field_id, cloud_equiv, field_name))

        if bad_refs:
            issues.append({
                "uuid": uuid,
                "name": name,
                "bad_refs": bad_refs,
                "full_rule": full_rule,
            })
            print(f"  ✗ {name} — {len(bad_refs)} bad field ref(s)")
        else:
            clean += 1
            print(f"  ✓ {name}")

    # ── Step 3: Report ────────────────────────────────────────
    print(f"\n{'─' * 60}")
    if not args.rule_uuid:
        print(f"Skipped (non-ITSM/SSSD): {skipped}")
    print(f"Clean: {clean}")
    print(f"Need fixing: {len(issues)}")

    if issues:
        print(f"\n{'─' * 60}")
        print("BAD FIELD REFERENCES:")
        for issue in issues:
            print(f"\n  [{issue['name']}]")
            print(f"  UUID: {issue['uuid']}")
            seen = {}
            for path, dc_id, cloud_id, fname in issue["bad_refs"]:
                if dc_id not in seen:
                    seen[dc_id] = 0
                seen[dc_id] += 1
            for dc_id, count in seen.items():
                cloud_id = field_mapping.get(dc_id, {}).get("cloud_id", "??? (no mapping found)")
                fname = field_mapping.get(dc_id, {}).get("name", "unknown")
                print(f"    {dc_id} → {cloud_id}  ({fname}) — {count} occurrence(s)")

    # ── Step 4: Fix ───────────────────────────────────────────
    if args.fix and issues:
        # Filter to only fixable issues (where we have a mapping)
        fixable = [i for i in issues if all(
            field_mapping.get(dc_id) for _, dc_id, _, _ in i["bad_refs"]
        )]
        unfixable = [i for i in issues if i not in fixable]

        if unfixable:
            print(f"\n⚠ {len(unfixable)} rule(s) have fields with no known mapping — skipping those")
            for uf in unfixable:
                unmapped_ids = set(dc_id for _, dc_id, cloud_id, _ in uf["bad_refs"] if cloud_id == "???")
                print(f"  {uf['name']}: {unmapped_ids}")

        if not fixable:
            print("\nNo rules can be auto-fixed (missing mappings)")
            return

        print(f"\n{'=' * 60}")
        if args.dry_run:
            print(f"DRY RUN — would fix {len(fixable)} rule(s)")
        else:
            print(f"About to fix {len(fixable)} rule(s). Continue? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("Aborted.")
                return
        print("=" * 60)

        # Build a simple dc_id→cloud_id dict for replacement
        replace_map = {dc_id: info for dc_id, info in field_mapping.items()}

        for issue in fixable:
            rule = issue["full_rule"]
            name = issue["name"]
            uuid = issue["uuid"]

            count = replace_field_ids(rule, replace_map)
            print(f"\n  {name}: {count} replacement(s)")

            if not args.dry_run and count > 0:
                try:
                    auto_client.update_rule(uuid, rule)
                    print(f"    ✓ Updated")
                except requests.HTTPError as e:
                    print(f"    ✗ Error: {e.response.status_code} — {e.response.text[:200]}")
            elif args.dry_run:
                print(f"    (would update)")

    elif args.fix and not issues:
        print("\n✓ All rules are clean!")

    print()


if __name__ == "__main__":
    main()
