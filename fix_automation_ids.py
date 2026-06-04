"""
Daybreak Games — Jira DC→Cloud Automation Remediation Script
=============================================================
Reads the Cloud automation export JSON, applies all DC→Cloud ID
replacements (custom fields, projects, portals, statuses, URLs,
and user IDs), and writes a corrected JSON ready for re-import.

Usage:
    python fix_automation_ids.py <input_json> [output_json]

If no output path is given, writes to <input>_corrected.json
"""

import json
import sys
import re
from pathlib import Path

# ── Custom Field ID Mapping ──────────────────────────────────
CUSTOM_FIELD_MAP = {
    "customfield_10101": "customfield_10147",   # Teams
    "customfield_10102": "customfield_10133",   # Stakeholders
    "customfield_10203": "customfield_10136",   # Platforms
    "customfield_11002": "customfield_10034",   # Request participants
    "customfield_11003": "customfield_10010",   # Customer Request Type
    "customfield_11008": "customfield_10003",   # Approvers
    "customfield_12479": "customfield_10140",   # Change start date
    "customfield_12480": "customfield_10131",   # Change completion date
    "customfield_13411": "customfield_10165",   # Shared Services Teams
    "customfield_15300": "customfield_10332",   # Waiting On
}

# ── Project ID Mapping ───────────────────────────────────────
PROJECT_ID_MAP = {
    "13100": "10034",   # ITSM
    "13202": "10041",   # SSSD
    "13200": "10049",   # FSD
}

# ── Status ID Mapping ────────────────────────────────────────
STATUS_ID_MAP = {
    "10000": "10061",   # To Do
    "10001": "10042",   # Done
    "10002": "10066",   # In Review
    "10207": "10109",   # Fixed
    "10300": "10062",   # Blocked
    "11403": "10044",   # Waiting for support
    "11404": "10045",   # Waiting for customer
    "11405": "10046",   # Pending
    "11407": "10047",   # Escalated
    "11408": "10048",   # Canceled
    "11409": "10043",   # Declined
    "11410": "10050",   # Awaiting approval
    "11411": "10051",   # Planning
    "11412": "10052",   # Awaiting implementation
    "11413": "10053",   # Implementing
    "11414": "10054",   # Peer review / approval
    "11415": "10055",   # Work in progress
    "11416": "10056",   # Completed
    "11417": "10057",   # Under investigation
    "11418": "10058",   # Under review
    "12103": "10049",   # Final Review & Approval
    "13000": "11199",   # Invoice Processing
}

# ── DC Username → Cloud Account ID Mapping ───────────────────
# Named users
USER_MAP = {
    "hhuynh":         "712020:fc282b81-8689-4043-99a7-96cc35f46fe2",
    "dscaduto":       "557058:1f9060d1-9046-450a-ad12-125a25d4c02d",
    "csavage":        "70121:05dfd084-d344-476f-9bee-c71e8ab163c7",
    "gbjornsson":     "70121:60720f73-2415-437f-85b7-d9e9d9221213",
    "ewebb":          "620bdee1bba9ca0070ca6cf6",
    "nbeaton":        "640f48aeb05b4e3e7da8509f",
    "rtruong":        "60e87f2384c992007197221d",
    "jchan":          "70121:1df21e23-e8fb-40dc-9d28-59e1900cad71",
    "smelton":        "557058:9fe9d10e-4cae-4f0e-9744-3ca2719a50a4",
    "jfermo":         "712020:f7933b9b-d3df-482b-8a87-b816f6f6a27d",
    "rmase":          "712020:de93cb27-e833-40d0-a16e-d5efd044f170",
    "rwager":         "712020:f2cd3229-21eb-4c99-ae0f-ae9b9f90c194",
    "smcwherter":     "712020:a94490f7-6569-4124-803a-eab34678d784",
    "dgonzales":      "70121:8ac9f2ee-fe08-4b12-a368-2357007294e9",
    "rkline":         "712020:8548b2a8-d837-45af-8654-00289e3711e9",
    "tpettigrew":     "712020:6bb07dba-ccb1-46b0-968e-943b4e16b6cf",
    "fbaecker":       "5bc63308c8f90064f0caa59a",
    "csickels":       "712020:40e79083-f95c-4f79-89a9-1f7163f61b42",
    "jfox":           "712020:b0cd0dd0-8d14-4e11-a215-1b32f065f890",
    "hfung":          "712020:4d1b0cfd-22c6-4c8e-9cf0-da0ff02506ec",
    "jcracchiolo":    "712020:7e053196-4952-4db7-b3cd-d12089f16493",
    "rciccolini":     "712020:c0e74257-251b-4649-a861-c09c613770d9",
    "dyoussefi":      "712020:a28fa742-2683-4a25-bd35-3a7d0e63c5ac",
    "sluciani":       "712020:98f7cbf8-aade-4608-a8e1-fde5129dcf24",
    "eramos":         "712020:17331283-8caf-4163-8dac-305dee2c8cac",
    "jlauterwasser":  "712020:675354e1-b883-4ea5-8643-2b9c72b75575",
    "ahuse":          "712020:2e5c5ed1-f332-4be8-b35d-714eed8fb592",
    "ptighe":         "712020:83340abe-168e-4384-9b47-2ac47f74ccff",
    "jfloyd":         "70121:9fbe2efb-bfe8-4455-b5f7-8658216313a5",
    "zilyes":         "712020:cfcb419b-aec4-476e-bc60-2f9abde7df5c",
    "tsaiyed":        "712020:3a4994c8-35b4-4608-b057-24577a2951d6",
    "jconrado":       "557058:c6c9c668-6b29-40b1-80ba-7dedb8571fd1",
    "mmahler":        "712020:2382dd85-3a9f-4422-b715-65a75d1ab3a2",
    "amartini":       "60a6b0153fae6f0068ee8eff",
    "agrow":          "712020:61f57e8a-af1d-4cfa-ae66-970e4cca6023",
    "tneises":        "712020:4d6395bf-bf5c-41b6-b8f0-6696381a06f2",
    "jpablo":         "712020:55141467-3200-4dd2-b237-975b26b28daa",
    "jlee":           "712020:5f7e2b47-8a20-4180-bbcf-c60fe3fdf7e9",
    "jschwarz":       "712020:9d514e43-ca37-4750-9e29-06d6c6afd02f",
    "tatkinson":      "638f6907f6c85b343c0ce7c1",
    "rpartridge":     "712020:b3abae4e-58bb-4817-b59d-0d90ea5bc6d1",
    "bchampagne":     "712020:6435b76e-61ab-4071-8dc2-c614d70fc86f",
    "jsnook":         "712020:6f25deaa-ba8e-41b6-a4ad-3aa39d325f5e",
    "poleary":        "712020:aaae31cb-269f-44d1-adf3-268d1b3422c4",
    "bthompson":      "712020:e8be5629-8e9c-485f-875c-1592ca04953d",
    # JIRAUSER IDs
    "JIRAUSER15400":  "712020:d4ab1147-6a2a-4955-8cab-39b13f69e5d9",   # Jared Cate
    "JIRAUSER15680":  "712020:f3e0bdb9-1404-4148-b64d-01fbfa9adf6f",   # Walter Tuanqui
    "JIRAUSER15800":  "64232fc90152b5f4f9f2d69f",                      # Abe Delosreyes
    "JIRAUSER15820":  "712020:8c2a99b0-d7fa-48d2-bd68-0c3d401a5131",   # Dennis Antapli
    "JIRAUSER15900":  "5fd2348334847e0069f2f8db",                      # Cain Neal
    "JIRAUSER16026":  "712020:4dbc0ef6-f3e6-43f9-a130-5053c92ab99a",   # Derek Morris
    "JIRAUSER16413":  "712020:ff4985cd-2e3f-4963-87c3-a2ca037922a4",   # Zeno Makoliakunu
    "JIRAUSER16800":  "712020:c1185e34-949f-488d-aa59-ddd3763b0cf9",   # Jeremy Alcarion
    "JIRAUSER16901":  "712020:60dfb142-ad68-438c-8e6a-8abf502ca0cc",   # Satarupa Chakraborty
    "JIRAUSER17701":  "712020:173a7a5d-9ac3-4c44-83ba-db9b1c25dad3",   # Allan Atrushi
    "JIRAUSER18301":  "712020:0ae98bca-8f7b-44f5-a8a1-90ed4fcc798e",   # Sue Gaudino
    "JIRAUSER18328":  "62df200b4b574e9f2caf61b2",                      # Davon Moss
    "JIRAUSER18826":  "712020:9b401a08-3035-4f26-9401-30c8b171809a",   # Corey Kriescher
    "JIRAUSER18869":  "712020:ae668666-612b-419c-be5b-6371bb0f6e0c",   # Eric Bair
    "JIRAUSER19109":  "JIRAUSER19109",                                  # Balasingam, J — NOT FOUND ON CLOUD
    "JIRAUSER19907":  "712020:c057d01a-634b-49d8-9fc5-9c222376ff46",   # Andre Emerson
    "JIRAUSER20007":  "5b633d02c564683b7b905daa",                      # Anthony Leung
    "JIRAUSER20013":  "5dc9a77d97a0a20c663fe62b",                      # Jeff Denis
    "JIRAUSER20306":  "712020:c102c143-a186-46ab-a3e0-adc4980a256c",   # Goodwyn Villegas
    "JIRAUSER20307":  "712020:58c9b865-6321-41c0-a179-bb05ff322350",   # Daniel Flannagan
    "JIRAUSER20316":  "712020:e8a86208-9559-422c-b166-c7ecf6b19149",   # Andrew Boni
    # JIRAUSER15201 (Justin Foote) — INACTIVE, not migrated
}

# ── URL Replacements ─────────────────────────────────────────
URL_REPLACEMENTS = {
    "https://jira.daybreakgames.com":      "https://daybreakgames-sandbox.atlassian.net",
    "https://jira-test.daybreakgames.com": "https://daybreakgames-sandbox.atlassian.net",
    "https://confluence.daybreakgames.com": "https://daybreakgames-sandbox.atlassian.net/wiki",
}

# ── Portal ID Replacements (in URL paths) ────────────────────
PORTAL_MAP = {
    "/portal/6/": "/portal/1/",    # ITSM
    "/portal/9/": "/portal/34/",   # SSSD
    "/portal/7/": "/portal/67/",   # FSD
}


def apply_replacements(text: str) -> tuple[str, dict]:
    """Apply all find-and-replace operations to a JSON string.
    Returns (corrected_text, change_counts)."""
    counts = {
        "custom_fields": 0,
        "project_ids": 0,
        "status_ids": 0,
        "user_ids": 0,
        "urls": 0,
        "portals": 0,
    }

    # 1. Custom field IDs (most specific — match the full ID string)
    for dc_id, cloud_id in CUSTOM_FIELD_MAP.items():
        before = text
        text = text.replace(dc_id, cloud_id)
        if text != before:
            counts["custom_fields"] += before.count(dc_id)

    # 2. URLs (do before portal IDs so portal paths are in Cloud URLs)
    for dc_url, cloud_url in URL_REPLACEMENTS.items():
        before = text
        text = text.replace(dc_url, cloud_url)
        if text != before:
            counts["urls"] += before.count(dc_url)

    # 3. Portal IDs (in URL paths)
    for dc_portal, cloud_portal in PORTAL_MAP.items():
        before = text
        text = text.replace(dc_portal, cloud_portal)
        if text != before:
            counts["portals"] += before.count(dc_portal)

    # 4. User IDs — only replace in value contexts to avoid false positives
    #    Match patterns like "value":"username" or "value":"JIRAUSER12345"
    for dc_user, cloud_account_id in USER_MAP.items():
        if dc_user == cloud_account_id:
            continue  # Skip unmapped users
        # Replace in JSON value positions: after "value":" or "name":"
        patterns = [
            f'"value":"{dc_user}"',
            f'"name":"{dc_user}"',
        ]
        for pattern in patterns:
            replacement = pattern.replace(dc_user, cloud_account_id)
            before = text
            text = text.replace(pattern, replacement)
            if text != before:
                counts["user_ids"] += before.count(pattern)

        # Also replace in arrays like ["user1","user2"]
        # Match "username" when preceded by [ or , and followed by " or ]
        array_pattern = f'"{dc_user}"'
        # Only do this for JIRAUSER IDs to avoid false positives on common words
        if dc_user.startswith("JIRAUSER"):
            before = text
            text = text.replace(array_pattern, f'"{cloud_account_id}"')
            if text != before:
                counts["user_ids"] += before.count(array_pattern)

    # 5. Status IDs — replace in status value contexts
    #    Match "value":"12345" where 12345 is a status ID
    for dc_status, cloud_status in STATUS_ID_MAP.items():
        pattern = f'"value":"{dc_status}"'
        replacement = f'"value":"{cloud_status}"'
        before = text
        text = text.replace(pattern, replacement)
        if text != before:
            counts["status_ids"] += before.count(pattern)

    # 6. Project IDs — replace in project value contexts
    for dc_proj, cloud_proj in PROJECT_ID_MAP.items():
        pattern = f'"value":"{dc_proj}"'
        replacement = f'"value":"{cloud_proj}"'
        before = text
        text = text.replace(pattern, replacement)
        if text != before:
            counts["project_ids"] += before.count(pattern)

        # Also in projectId fields
        pattern2 = f'"projectId":"{dc_proj}"'
        replacement2 = f'"projectId":"{cloud_proj}"'
        before = text
        text = text.replace(pattern2, replacement2)
        if text != before:
            counts["project_ids"] += before.count(pattern2)

    return text, counts


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"Error: {input_path} not found")
        sys.exit(1)

    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else input_path.with_stem(input_path.stem + "_corrected")

    print(f"Reading: {input_path}")
    raw = input_path.read_text(encoding="utf-8")

    print("Applying DC→Cloud replacements...")
    corrected, counts = apply_replacements(raw)

    # Validate JSON
    try:
        data = json.loads(corrected)
        # Pretty-print for readability
        output = json.dumps(data, indent=2, ensure_ascii=False)
    except json.JSONDecodeError as e:
        print(f"Warning: JSON validation failed after replacements: {e}")
        print("Writing raw corrected text anyway — check for issues.")
        output = corrected

    output_path.write_text(output, encoding="utf-8")

    print(f"\nWritten: {output_path}")
    print(f"\n{'='*50}")
    print(f"REPLACEMENT SUMMARY")
    print(f"{'='*50}")
    print(f"  Custom field IDs:  {counts['custom_fields']:>4} replacements")
    print(f"  Project IDs:       {counts['project_ids']:>4} replacements")
    print(f"  Status IDs:        {counts['status_ids']:>4} replacements")
    print(f"  User IDs:          {counts['user_ids']:>4} replacements")
    print(f"  URLs:              {counts['urls']:>4} replacements")
    print(f"  Portal IDs:        {counts['portals']:>4} replacements")
    print(f"  {'─'*40}")
    print(f"  TOTAL:             {sum(counts.values()):>4} replacements")
    print()

    # Check for remaining DC references
    remaining_dc = []
    if "jira.daybreakgames.com" in corrected:
        remaining_dc.append("jira.daybreakgames.com (hardcoded URL)")
    if "jira-test.daybreakgames.com" in corrected:
        remaining_dc.append("jira-test.daybreakgames.com (hardcoded URL)")
    if "JIRAUSER" in corrected:
        # Find which ones remain
        remaining_jira = set(re.findall(r'JIRAUSER\d+', corrected))
        for j in remaining_jira:
            remaining_dc.append(f"{j} (unmapped JIRAUSER ID)")
    if "scriptdaylight" in corrected:
        remaining_dc.append("scriptdaylight (DC service account)")
    if "spelkey" in corrected:
        remaining_dc.append("spelkey (not migrated to Cloud)")

    if remaining_dc:
        print("⚠  REMAINING DC REFERENCES (manual review needed):")
        for ref in remaining_dc:
            print(f"   • {ref}")
        print()
    else:
        print("✓  No remaining DC references detected.\n")


if __name__ == "__main__":
    main()
