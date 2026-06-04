#!/usr/bin/env python3
"""
Map Data Center JSM portal/request-type URLs to Jira Cloud equivalents via REST API.

Usage:
  export JIRA_CLOUD_SITE=daybreakgames.atlassian.net
  export JIRA_EMAIL=you@daybreakgames.com
  export JIRA_API_TOKEN=...
  # optional, to resolve DC portal names:
  export JIRA_DC_SITE=jira.daybreakgames.com

  python3 map-jsm-portal-urls.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

CLOUD_SITE = os.environ.get("JIRA_CLOUD_SITE", "daybreakgames.atlassian.net")
DC_SITE = os.environ.get("JIRA_DC_SITE", "jira.daybreakgames.com")
EMAIL = os.environ.get("JIRA_EMAIL", "")
TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# DC IDs extracted from automation-rules-202605180957.json
DC_PORTALS = {"6", "9"}
DC_REQUEST_TYPES = {
    "304": "License renewal (from rule 435 email text)",
    "306": "Service Contract (from rule 442)",
    "320": "Offboarding (from rule 442)",
}

DC_URL_TEMPLATES = [
    "https://jira.daybreakgames.com/servicedesk/customer/portal/6/{{issue.key}}",
    "https://jira.daybreakgames.com/servicedesk/customer/portal/6/{{key}}",
    "https://jira.daybreakgames.com/servicedesk/customer/portal/9/{{key}}",
    "https://jira.daybreakgames.com/servicedesk/customer/portal/9/create/304",
    "https://jira.daybreakgames.com/servicedesk/customer/portal/9/create/306",
    "https://jira.daybreakgames.com/servicedesk/customer/portal/9/create/320",
]


def auth_header() -> dict[str, str]:
    if not EMAIL or not TOKEN:
        sys.exit("Set JIRA_EMAIL and JIRA_API_TOKEN environment variables.")
    raw = f"{EMAIL}:{TOKEN}".encode()
    return {
        "Authorization": "Basic " + base64.b64encode(raw).decode(),
        "Accept": "application/json",
    }


def get_json(base: str, path: str) -> dict:
    url = f"https://{base}{path}"
    req = urllib.request.Request(url, headers=auth_header())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} for {url}\n{body[:2000]}") from e


def paginate_values(base: str, path: str) -> list[dict]:
    items: list[dict] = []
    start = 0
    while True:
        sep = "&" if "?" in path else "?"
        data = get_json(base, f"{path}{sep}start={start}&limit=50")
        items.extend(data.get("values", []))
        if data.get("isLastPage", True):
            break
        start = data.get("start", 0) + data.get("limit", 50)
    return items


def cloud_base() -> str:
    return f"https://{CLOUD_SITE}"


def list_servicedesks(site: str) -> list[dict]:
    return paginate_values(site, "/rest/servicedeskapi/servicedesk")


def list_request_types(site: str, service_desk_id: str) -> list[dict]:
    return paginate_values(site, f"/rest/servicedeskapi/servicedesk/{service_desk_id}/requesttype")


def portal_url(site: str, portal_id: str, suffix: str) -> str:
    return f"https://{site}/servicedesk/customer/portal/{portal_id}/{suffix}"


def main() -> None:
    print(f"Cloud site: {CLOUD_SITE}\n")

    cloud_desks = list_servicedesks(CLOUD_SITE)
    print("=== Cloud service desks (portalId often == id) ===")
    for sd in cloud_desks:
        print(
            f"  id={sd['id']:>4}  projectKey={sd.get('projectKey','?'):8}  "
            f"name={sd.get('projectName', sd.get('name', '?'))}"
        )

    print("\n=== Cloud request types (per service desk) ===")
    rt_by_portal: dict[str, list[dict]] = {}
    for sd in cloud_desks:
        sd_id = str(sd["id"])
        rts = list_request_types(CLOUD_SITE, sd_id)
        rt_by_portal[sd_id] = rts
        print(f"\n  Service desk id={sd_id} ({sd.get('projectKey')}):")
        for rt in rts:
            print(f"    rt id={rt['id']:>5}  portalId={rt.get('portalId')}  name={rt.get('name')}")

    # Try DC for portal 6/9 -> projectKey mapping
    dc_portal_projects: dict[str, str] = {}
    if os.environ.get("JIRA_DC_SITE"):
        try:
            dc_desks = list_servicedesks(DC_SITE)
            for sd in dc_desks:
                if str(sd["id"]) in DC_PORTALS:
                    dc_portal_projects[str(sd["id"])] = sd.get("projectKey", "?")
            print("\n=== DC portal -> projectKey ===")
            for pid, pk in sorted(dc_portal_projects.items()):
                print(f"  portal {pid} -> {pk}")
        except SystemExit as e:
            print(f"\n(Warning: could not query DC: {e})")

    print("\n=== Suggested Cloud URL replacements ===")
    print("(Match DC portal 9 to Procurement project by name if IDs differ.)\n")

    for dc_url in DC_URL_TEMPLATES:
        cloud_url = dc_url.replace(f"jira.daybreakgames.com", CLOUD_SITE)
        m_create = re.search(r"/portal/(\d+)/create/(\d+)", dc_url)
        m_issue = re.search(r"/portal/(\d+)/\{\{", dc_url)
        note = ""
        if m_create:
            portal, rt = m_create.group(1), m_create.group(2)
            note = f"  # DC portal {portal}, request type {rt} ({DC_REQUEST_TYPES.get(rt, '?')})"
            pk = dc_portal_projects.get(portal)
            if pk:
                for sd in cloud_desks:
                    if sd.get("projectKey") == pk:
                        for rt_cloud in rt_by_portal.get(str(sd["id"]), []):
                            if rt_cloud.get("name", "").lower() in DC_REQUEST_TYPES.get(rt, "").lower():
                                cloud_url = portal_url(
                                    CLOUD_SITE, str(rt_cloud.get("portalId", sd["id"])),
                                    f"create/{rt_cloud['id']}",
                                )
                                note += f" -> matched '{rt_cloud['name']}'"
        elif m_issue:
            portal = m_issue.group(1)
            pk = dc_portal_projects.get(portal)
            suffix = dc_url.split(f"/portal/{portal}/", 1)[1]
            if pk:
                for sd in cloud_desks:
                    if sd.get("projectKey") == pk:
                        cloud_url = portal_url(CLOUD_SITE, str(sd["id"]), suffix)
                        note += f"  # matched project {pk}"
        print(f"DC:    {dc_url}")
        print(f"Cloud: {cloud_url}{note}\n")

    print("=== Non-portal DC URLs (separate migration) ===")
    others = [
        ("browse", f"{cloud_base()}/browse/{{{{key}}}}"),
        ("filter 16421", f"{cloud_base()}/issues/?filter=<cloud-filter-id>"),
        ("filter 17050", f"{cloud_base()}/issues/?filter=<cloud-filter-id>"),
        ("dashboard 18800", f"{cloud_base()}/jira/dashboards/<id>"),
        ("csv export 22615", "Recreate filter; use /sr/jira.issueviews:... or API export"),
    ]
    for name, url in others:
        print(f"  {name}: {url}")


if __name__ == "__main__":
    main()
