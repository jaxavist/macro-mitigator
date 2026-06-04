# Atlassian DC-to-Cloud Migration Toolkit

Scripts for migrating Confluence and Jira from Data Center to Cloud. Covers Confluence macro conversion (shared-blocks, help-text, extra-table-properties, Aura tabs, legacy wrappers) and Jira automation ID remediation.

## Prerequisites

- Python 3.8+
- `requests` library (`pip install requests`)

## Environment Variables

### Confluence scripts

```bash
export CONFLUENCE_BASE_URL="https://your-site.atlassian.net"
export CONFLUENCE_EMAIL="your-email@company.com"
export CONFLUENCE_API_TOKEN="your-api-token"
```

### Jira automation scripts

```bash
export ATLASSIAN_EMAIL="your-email@company.com"
export ATLASSIAN_API_TOKEN="your-api-token"
```

---

## Confluence Migration Scripts

### Phase 0 — Diagnostics & Prep

| Script | Purpose |
|--------|---------|
| `phase0_legacy_unwrap.py` | Strips legacy-content migration wrappers, renames migrated Aura tab macros to Cloud-native equivalents. |
| `phase0_diagnose.py` | Isolates 500 errors on pages by testing legacy unwrap and Aura rename independently. |
| `phase0_diagnose2.py` | Tests whether `UNKNOWN_MEDIA_ID` nodes cause save failures; strips broken media refs. |
| `phase0_diagnose3.py` | Isolates which specific tab content child causes a 500 by removing them one at a time. |

### Phase 2 — Shared-Block to Excerpt Conversion

| Script | Purpose |
|--------|---------|
| `phase2_audit.py` | Maps all shared-block and include-shared-block macros across a space. Produces a dependency report with cross-references and orphan detection. |
| `phase2_step1_template_resources.py` | Creates child pages with Excerpts from Template Resources shared-blocks. |
| `phase2_step2_term_pages.py` | Batch converts term pages: replaces include-shared-block with excerpt-include, unwraps shared-blocks. |
| `phase2_step2_v2.py` | v2 of Step 2: also creates per-page child pages for each shared-block (not just Template Resources). |
| `phase2_step3_overview.py` | Converts the Glossary Overview and How-To page (special handling for cross-page includes). |

### Phase 3–4 — Remaining Macros

| Script | Purpose |
|--------|---------|
| `phase3_strip_extra_table.py` | Strips `extra-table-properties` wrappers, promoting table content into the parent macro. |
| `phase4_help_text.py` | Converts ScriptRunner `help-text` macros to native Expand macros. |

### Consolidated Migration

| Script | Purpose |
|--------|---------|
| `consolidated_migrate.py` | **Primary script.** Single-pass conversion of all DC macro patterns. One read, all transforms, one save per page. |

### Targeted Fixes

| Script | Purpose |
|--------|---------|
| `convert_aura_page.py` | Converts macros on Aura-heavy pages while preserving legacy-content wrappers. |
| `fix_home_links.py` | Replaces old DC URLs with Cloud URLs on the Glossary Home Page. |
| `fix_main_directory.py` | Converts button macros to inline links and fixes Live Search `spaceKey`. |
| `fix_placeholders.py` | Converts ADF placeholder nodes to visible italic text on excerpt child pages. |
| `fix_definitions_include.py` | Replaces `excerpt-include` for `_Definition(s)` with `Include Page` macro. |

### Admin & Verification

| Script | Purpose |
|--------|---------|
| `add_editor_to_restricted.py` | Adds the API user to edit restrictions on all restricted pages in a space. |
| `add_group_to_restricted.py` | Adds a group to edit restrictions on all restricted pages (preserves existing restrictions). |
| `verify_nbm.py` | Post-migration verification scanner. Reports macro state across one or more spaces. |
| `restore_page_version.py` | Restores a Confluence page to a specific prior version. |

---

## Jira Automation Remediation Scripts

| Script | Purpose |
|--------|---------|
| `compare_and_fix.py` | Compares custom fields, statuses, and project IDs between two Jira environments. Audits and fixes automation rules that reference wrong-environment IDs. |
| `audit_fix_automations.py` | Dynamically builds DC→Cloud field mapping by pulling fields from both APIs, then scans/fixes automation rules. |
| `fix_automation_ids.py` | Offline JSON fixer: applies all DC→Cloud ID replacements (fields, projects, statuses, users, URLs, portals) to an exported automation rules JSON. |
| `map_jsm_portal_urls.py` | Maps DC JSM portal/request-type URLs to their Cloud equivalents via REST API. |

---

## Usage Pattern

All scripts support `--dry-run` for safe previewing:

```bash
# Preview what would change
python consolidated_migrate.py --space-key GLOS --dry-run

# Run on a single page first
python consolidated_migrate.py --space-key GLOS --page-id 12345

# Full space run with output report
python consolidated_migrate.py --space-key GLOS --batch-size 20 --output-json results.json

# Verify results
python verify_nbm.py --space-keys GLOS --output-json verify_report.json
```
