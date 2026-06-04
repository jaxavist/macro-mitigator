"""
Daybreak Games — Cross-Environment Automation Auditor & Fixer (v4)
===================================================================
Compares custom fields, statuses, and other Jira IDs between any two
Cloud environments (e.g., Sandbox → Production) and fixes automation
rules that reference IDs from the source environment.

Usage:
    # Compare Sandbox vs Production fields (no automation changes):
    python3 compare_and_fix.py --compare \
        --source-site daybreakgames-sandbox.atlassian.net \
        --target-site daybreakgames.atlassian.net \
        --token "YOUR_API_TOKEN"

    # Audit automations on target for source IDs:
    python3 compare_and_fix.py --audit \
        --source-site daybreakgames-sandbox.atlassian.net \
        --target-site daybreakgames.atlassian.net \
        --token "YOUR_API_TOKEN"

    # Fix automations on target:
    python3 compare_and_fix.py --fix \
        --source-site daybreakgames-sandbox.atlassian.net \
        --target-site daybreakgames.atlassian.net \
        --token "YOUR_API_TOKEN"

    # DC as source (needs separate DC PAT):
    python3 compare_and_fix.py --audit \
        --source-site jira-test.daybreakgames.com --source-is-dc \
        --target-site daybreakgames.atlassian.net \
        --token "CLOUD_TOKEN" --dc-token "DC_PAT"

Requirements:
    pip install requests
"""

import json
import re
import sys
import os
import argparse
import requests
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════
# Data Fetchers
# ═══════════════════════════════════════════════════════════════

def get_cloud_id(site, auth):
    """Get the Cloud ID for a site."""
    resp = requests.get(f"https://{site}/_edge/tenant_info", headers={"Accept": "application/json"})
    if resp.ok:
        return resp.json().get("cloudId")
    # Fallback: try the REST API
    resp = requests.get(f"https://{site}/rest/api/3/serverInfo", auth=auth, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json().get("cloudId")


def fetch_fields(site, auth=None, headers=None, is_dc=False):
    """Fetch all fields from a Jira instance."""
    api_ver = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api_ver}/field"
    kwargs = {"headers": {"Accept": "application/json"}}
    if auth:
        kwargs["auth"] = auth
    if headers:
        kwargs["headers"].update(headers)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    all_fields = resp.json()
    custom = {}
    for f in all_fields:
        fid = f.get("id", "")
        if fid.startswith("customfield_"):
            custom[fid] = {
                "id": fid,
                "name": f.get("name", f.get("untranslatedName", "")).strip(),
                "type": f.get("schema", {}).get("custom", ""),
                "schema_type": f.get("schema", {}).get("type", ""),
            }
    return custom


def fetch_statuses(site, auth=None, headers=None, is_dc=False):
    """Fetch all statuses from a Jira instance."""
    api_ver = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api_ver}/status"
    kwargs = {"headers": {"Accept": "application/json"}}
    if auth:
        kwargs["auth"] = auth
    if headers:
        kwargs["headers"].update(headers)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    statuses = resp.json()
    return {
        s["id"]: {
            "id": s["id"],
            "name": s.get("name", s.get("untranslatedName", "")).strip(),
            "category": s.get("statusCategory", {}).get("name", ""),
        }
        for s in statuses
    }


def fetch_projects(site, auth=None, headers=None, is_dc=False):
    """Fetch all projects."""
    api_ver = "2" if is_dc else "3"
    url = f"https://{site}/rest/api/{api_ver}/project"
    if not is_dc:
        url = f"https://{site}/rest/api/3/project/search?maxResults=200"
    kwargs = {"headers": {"Accept": "application/json"}}
    if auth:
        kwargs["auth"] = auth
    if headers:
        kwargs["headers"].update(headers)
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    data = resp.json()
    projects = data if isinstance(data, list) else data.get("values", [])
    return {
        p["id"]: {"id": p["id"], "key": p.get("key", ""), "name": p.get("name", "")}
        for p in projects
    }


def fetch_service_desks(site, auth=None, headers=None):
    """Fetch service desk portal IDs."""
    url = f"https://{site}/rest/servicedeskapi/servicedesk"
    kwargs = {"headers": {"Accept": "application/json"}}
    if auth:
        kwargs["auth"] = auth
    if headers:
        kwargs["headers"].update(headers)
    resp = requests.get(url, **kwargs)
    if not resp.ok:
        return {}
    data = resp.json()
    return {
        v["projectKey"]: {"id": v["id"], "projectId": v["projectId"], "name": v["projectName"], "key": v["projectKey"]}
        for v in data.get("values", [])
    }


# ═══════════════════════════════════════════════════════════════
# Mapping Builders
# ═══════════════════════════════════════════════════════════════

def build_mapping(source_items, target_items, label="items"):
    """Build source→target mapping by matching on name (case-insensitive)."""
    target_by_name = defaultdict(list)
    for tid, t in target_items.items():
        target_by_name[t["name"].lower()].append(t)

    mapping = {}
    ambiguous = []
    same_id = 0
    changed_id = 0

    for sid, s in source_items.items():
        if sid in target_items and target_items[sid]["name"].lower() == s["name"].lower():
            same_id += 1
            continue  # Same ID, same name — no mapping needed

        candidates = target_by_name.get(s["name"].lower(), [])

        if len(candidates) == 1:
            target = candidates[0]
            if sid != target["id"]:
                mapping[sid] = {"target_id": target["id"], "name": s["name"]}
                changed_id += 1
        elif len(candidates) > 1:
            # Try matching by type if available
            if "type" in s:
                typed = [c for c in candidates if c.get("type") == s["type"]]
                if len(typed) == 1:
                    mapping[sid] = {"target_id": typed[0]["id"], "name": s["name"]}
                    changed_id += 1
                    continue
            ambiguous.append({"source_id": sid, "name": s["name"], "candidates": [c["id"] for c in candidates]})

    return mapping, ambiguous, same_id


# ═══════════════════════════════════════════════════════════════
# Automation Scanner
# ═══════════════════════════════════════════════════════════════

class AutomationClient:
    def __init__(self, site, cloud_id, email, token):
        self.base = f"https://{site}/gateway/api/automation/public/jira/{cloud_id}/rest/v1"
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
            resp = self.session.get(f"{self.base}/rule/summary", params=params)
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
        resp = self.session.get(f"{self.base}/rule/{uuid}")
        resp.raise_for_status()
        return resp.json()

    def update_rule(self, uuid, data):
        resp = self.session.put(f"{self.base}/rule/{uuid}", json=data)
        resp.raise_for_status()
        return resp.json()


def find_refs(obj, valid_ids, prefix, path=""):
    """Find references matching prefix that are NOT in valid_ids."""
    findings = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            p = f"{path}.{key}" if path else key
            if isinstance(val, str):
                for match in re.finditer(rf'{prefix}\d+', val):
                    ref = match.group()
                    if ref not in valid_ids:
                        findings.append((p, ref))
            else:
                findings.extend(find_refs(val, valid_ids, prefix, p))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            findings.extend(find_refs(item, valid_ids, prefix, f"{path}[{i}]"))
    return findings


def apply_mapping(obj, mapping):
    """Replace source IDs with target IDs. Returns count."""
    count = 0
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            val = obj[key]
            if isinstance(val, str):
                new_val = val
                for src_id, info in mapping.items():
                    if src_id in new_val:
                        new_val = new_val.replace(src_id, info["target_id"])
                        count += 1
                if new_val != val:
                    obj[key] = new_val
            else:
                count += apply_mapping(val, mapping)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_item = item
                for src_id, info in mapping.items():
                    if src_id in new_item:
                        new_item = new_item.replace(src_id, info["target_id"])
                        count += 1
                if new_item != item:
                    obj[i] = new_item
            else:
                count += apply_mapping(item, mapping)
    return count


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Compare Jira environments and fix automation ID mismatches",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compare Sandbox vs Production:
  python3 compare_and_fix.py --compare --source-site sandbox.atlassian.net --target-site prod.atlassian.net --source-token X --target-token Y

  # Audit + fix automations on target:
  python3 compare_and_fix.py --fix --source-site sandbox.atlassian.net --target-site prod.atlassian.net --source-token X --target-token Y
        """)

    parser.add_argument("--compare", action="store_true", help="Compare fields/statuses between environments (no automation changes)")
    parser.add_argument("--audit", action="store_true", help="Audit target automations for source IDs")
    parser.add_argument("--fix", action="store_true", help="Audit + fix target automations")
    parser.add_argument("--dry-run", action="store_true", help="Show what fix would do")

    parser.add_argument("--source-site", required=True, help="Source site (e.g., daybreakgames-sandbox.atlassian.net)")
    parser.add_argument("--target-site", required=True, help="Target site (e.g., daybreakgames.atlassian.net)")
    parser.add_argument("--token", required=True, help="Atlassian API token (works for all Cloud sites on your account)")
    parser.add_argument("--dc-token", help="Separate DC PAT if source is Data Center")
    parser.add_argument("--email", default="jkane@adaptavist.com", help="Email for Cloud Basic auth")
    parser.add_argument("--source-is-dc", action="store_true", help="Source is Data Center (uses Bearer auth with --dc-token)")
    parser.add_argument("--all", action="store_true", help="Scan all projects, not just service desk projects")
    parser.add_argument("--export", help="Export mapping to JSON file")

    args = parser.parse_args()

    if not args.compare and not args.audit and not args.fix:
        parser.print_help()
        sys.exit(1)

    # ── Auth setup ────────────────────────────────────────────
    cloud_auth = (args.email, args.token)
    target_auth = cloud_auth

    if args.source_is_dc:
        dc_token = args.dc_token or args.token
        source_auth = None
        source_headers = {"Authorization": f"Bearer {dc_token}"}
    else:
        source_auth = cloud_auth
        source_headers = None

    # ── Step 1: Fetch data from both environments ─────────────
    print("\n" + "=" * 65)
    print(f"SOURCE: {args.source_site}")
    print(f"TARGET: {args.target_site}")
    print("=" * 65)

    print("\n── Custom Fields ──")
    src_fields = fetch_fields(args.source_site, auth=source_auth, headers=source_headers, is_dc=args.source_is_dc)
    tgt_fields = fetch_fields(args.target_site, auth=target_auth)
    print(f"  Source: {len(src_fields)} custom fields")
    print(f"  Target: {len(tgt_fields)} custom fields")

    print("\n── Statuses ──")
    src_statuses = fetch_statuses(args.source_site, auth=source_auth, headers=source_headers, is_dc=args.source_is_dc)
    tgt_statuses = fetch_statuses(args.target_site, auth=target_auth)
    print(f"  Source: {len(src_statuses)} statuses")
    print(f"  Target: {len(tgt_statuses)} statuses")

    print("\n── Projects ──")
    src_projects = fetch_projects(args.source_site, auth=source_auth, headers=source_headers, is_dc=args.source_is_dc)
    tgt_projects = fetch_projects(args.target_site, auth=target_auth)
    print(f"  Source: {len(src_projects)} projects")
    print(f"  Target: {len(tgt_projects)} projects")

    print("\n── Service Desks ──")
    src_desks = fetch_service_desks(args.source_site, auth=source_auth, headers=source_headers)
    tgt_desks = fetch_service_desks(args.target_site, auth=target_auth)
    print(f"  Source: {len(src_desks)} service desks")
    print(f"  Target: {len(tgt_desks)} service desks")

    # ── Step 2: Build mappings ────────────────────────────────
    print(f"\n{'=' * 65}")
    print("BUILDING MAPPINGS")
    print("=" * 65)

    field_map, field_ambig, field_same = build_mapping(src_fields, tgt_fields, "fields")
    status_map, status_ambig, status_same = build_mapping(src_statuses, tgt_statuses, "statuses")
    project_map, proj_ambig, proj_same = build_mapping(src_projects, tgt_projects, "projects")

    # Portal mapping by project key
    portal_map = {}
    for key, src_desk in src_desks.items():
        if key in tgt_desks:
            src_portal = src_desk["id"]
            tgt_portal = tgt_desks[key]["id"]
            if src_portal != tgt_portal:
                portal_map[f"/portal/{src_portal}/"] = {"target_id": f"/portal/{tgt_portal}/", "name": f"{key} portal"}

    print(f"\n  Custom Fields:  {field_same} same, {len(field_map)} changed, {len(field_ambig)} ambiguous")
    print(f"  Statuses:       {status_same} same, {len(status_map)} changed, {len(status_ambig)} ambiguous")
    print(f"  Projects:       {proj_same} same, {len(project_map)} changed, {len(proj_ambig)} ambiguous")
    print(f"  Portals:        {len(portal_map)} changed")

    if field_map:
        print(f"\n  Field ID changes:")
        for src, info in sorted(field_map.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} → {info['target_id']}  ({info['name']})")

    if status_map:
        print(f"\n  Status ID changes:")
        for src, info in sorted(status_map.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} → {info['target_id']}  ({info['name']})")

    if project_map:
        print(f"\n  Project ID changes:")
        for src, info in sorted(project_map.items(), key=lambda x: x[1]["name"]):
            print(f"    {src} → {info['target_id']}  ({info['name']})")

    if portal_map:
        print(f"\n  Portal ID changes:")
        for src, info in portal_map.items():
            print(f"    {src} → {info['target_id']}  ({info['name']})")

    if field_ambig:
        print(f"\n  ⚠ Ambiguous field matches:")
        for a in field_ambig:
            print(f"    {a['source_id']} ({a['name']}) → candidates: {a['candidates']}")

    # Merge all mappings
    all_mappings = {}
    all_mappings.update(field_map)
    # Status mappings need special handling (only in status value contexts)
    # We'll add them with a marker
    for sid, info in status_map.items():
        all_mappings[sid] = info
    for pid, info in project_map.items():
        all_mappings[pid] = info
    for portal_src, info in portal_map.items():
        all_mappings[portal_src] = info

    # Export mapping if requested
    if args.export:
        export_data = {
            "source": args.source_site,
            "target": args.target_site,
            "fields": field_map,
            "statuses": status_map,
            "projects": project_map,
            "portals": portal_map,
            "ambiguous_fields": field_ambig,
            "ambiguous_statuses": status_ambig,
        }
        with open(args.export, "w") as f:
            json.dump(export_data, f, indent=2)
        print(f"\n  Mapping exported to {args.export}")

    if args.compare:
        print(f"\n{'=' * 65}")
        print(f"TOTAL: {len(all_mappings)} ID changes detected between environments")
        print("=" * 65)
        print()
        return

    # ── Step 3: Audit/Fix automations ─────────────────────────
    if not args.audit and not args.fix:
        return

    print(f"\n{'=' * 65}")
    print(f"SCANNING AUTOMATIONS ON TARGET: {args.target_site}")
    print("=" * 65)

    target_cloud_id = get_cloud_id(args.target_site, target_auth)
    if not target_cloud_id:
        print("Error: could not determine target Cloud ID")
        sys.exit(1)
    print(f"  Target Cloud ID: {target_cloud_id}")

    auto_client = AutomationClient(args.target_site, target_cloud_id, args.email, args.token)

    print("  Fetching rules...")
    summaries = auto_client.list_rules()
    print(f"  Total rules: {len(summaries)}")

    tgt_field_ids = set(tgt_fields.keys())
    tgt_status_ids = set(tgt_statuses.keys())

    issues = []
    clean = 0

    for summary in summaries:
        uuid = summary.get("uuid")
        name = summary.get("name", "?")

        try:
            full_rule = auto_client.get_rule(uuid)
        except requests.HTTPError:
            continue

        # Find bad custom field refs
        bad_fields = find_refs(full_rule, tgt_field_ids, "customfield_")

        # Check if any mapping applies
        rule_issues = []
        for path, ref in bad_fields:
            if ref in field_map:
                rule_issues.append((path, ref, field_map[ref]["target_id"], field_map[ref]["name"]))
            else:
                rule_issues.append((path, ref, "??? (no mapping)", "unknown"))

        if rule_issues:
            issues.append({"uuid": uuid, "name": name, "issues": rule_issues, "full_rule": full_rule})
            print(f"  ✗ {name} — {len(rule_issues)} bad ref(s)")
        else:
            clean += 1

    print(f"\n{'─' * 65}")
    print(f"Clean: {clean}")
    print(f"Need fixing: {len(issues)}")

    if issues:
        print(f"\n{'─' * 65}")
        for issue in issues:
            print(f"\n  [{issue['name']}]")
            seen = {}
            for _, ref, target, fname in issue["issues"]:
                if ref not in seen:
                    seen[ref] = 0
                    print(f"    {ref} → {target}  ({fname})")
                seen[ref] += 1

    if args.fix and issues:
        fixable = [i for i in issues if all(ref in all_mappings for _, ref, _, _ in i["issues"])]
        if not fixable:
            print("\nNo rules can be auto-fixed (missing mappings)")
            return

        print(f"\n{'=' * 65}")
        if args.dry_run:
            print(f"DRY RUN — would fix {len(fixable)} rule(s)")
        else:
            print(f"Fix {len(fixable)} rule(s)? [y/N] ", end="")
            if input().strip().lower() != "y":
                print("Aborted.")
                return

        for issue in fixable:
            rule = issue["full_rule"]
            count = apply_mapping(rule, all_mappings)
            print(f"\n  {issue['name']}: {count} replacement(s)")

            if not args.dry_run and count > 0:
                try:
                    auto_client.update_rule(issue["uuid"], rule)
                    print(f"    ✓ Updated")
                except requests.HTTPError as e:
                    print(f"    ✗ Error: {e.response.status_code} — {e.response.text[:200]}")
            elif args.dry_run:
                print(f"    (would update)")

    print()


if __name__ == "__main__":
    main()
