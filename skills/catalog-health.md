# /catalog-health

Audit the local org snapshot for common RCA catalog problems: missing prices, missing PSM
options, blank codes, duplicate codes, and bundle integrity issues. No API calls — works
entirely from `.rca/org-snapshot.yaml`.

---

## Overview

Runs a set of checks across all products and bundles in the snapshot and produces a
grouped issue report. Use this after a sync to catch problems before they cause upload
failures, pricing gaps, or broken quotes.

Optionally accepts a filter argument to check a specific catalog, family, or product code:
- `/catalog-health` — check everything
- `/catalog-health Romet` — check only products in the Romet Product Catalog
- `/catalog-health LP001` — check a single product by code

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Load the org snapshot

**Determine the project root** — the directory containing `CLAUDE.md` or `.git/`,
whichever is found first walking up from the current working directory.
Set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

```bash
python -c "
import yaml, sys, os

path = '<SNAPSHOT_PATH>'
if not os.path.isfile(path):
    print('NO_SNAPSHOT')
    sys.exit(0)

with open(path) as f:
    snap = yaml.safe_load(f)

meta = snap.get('meta', {})
print(f'LOADED|{meta.get(\"products_count\",0)}|{meta.get(\"bundles_count\",0)}|{meta.get(\"last_synced\",\"unknown\")}')
"
```

If `NO_SNAPSHOT`:
> "No org snapshot found at `.rca/org-snapshot.yaml`. Run `/sync-rca-org` first."
Stop.

Store the full snapshot in memory.

---

### STEP 1 — Determine scope

If an argument was passed, resolve it:
- Matches a catalog name (case-insensitive): filter to products/bundles where `catalog` equals that value.
- Matches a product code exactly: filter to just that one record.
- Matches a family name: filter to that family.
- No argument: check all products and bundles.

Store the filtered working set as `items_to_check` (list of dicts, each tagged with `type`: `product` or `bundle`).

---

### STEP 2 — Run all checks

Run every check below against `items_to_check`. Collect findings into a list of issues,
each with: `severity` (ERROR / WARNING / INFO), `check_name`, `code`, `name`, `detail`.

---

#### CHECK 1 — Missing pricebook entry (ERROR)
An active product or bundle with no `pricebook_entries` at all.
- **Exclude** bundles priced at 0.00 with the word "bundle" in the name or with `groups` defined —
  $0 bundle price is normal when pricing flows from components.
- **Exclude** inactive records (`active: false`).

Issue: `"No pricebook entry — product has no list price in any pricebook."`

---

#### CHECK 2 — $0 price on active standalone product (WARNING)
An active product (not a bundle) with a Standard Price Book entry where `price` = 0.0.

Issue: `"Standard Price Book price is $0.00 — intentional or missing?"`

---

#### CHECK 3 — Missing PSM option (ERROR)
An active product or bundle with no `psm_options` list (or an empty list).
- **Exclude** products that appear only as bundle components (i.e. have no catalog assignment
  and no pricebook entry — they may be component-only records).

Issue: `"No selling model options — product cannot be quoted."`

---

#### CHECK 4 — No catalog assignment (INFO)
An active product or bundle with no `catalog` field.
- This is informational, not an error — some products are intentionally uncategorized.
- Flag it so the user can decide.

Issue: `"Not assigned to any catalog — won't appear in Transaction Line Editor browse."`

---

#### CHECK 5 — Blank or missing product code (ERROR)
A product or bundle where `code` is missing, an empty string, or only whitespace.

Issue: `"Product code is blank — upload will fail or create a record with no code."`

---

#### CHECK 6 — Duplicate product codes (ERROR)
Two or more products/bundles sharing the same non-blank code value.

Check across the entire snapshot (not just the filtered set) so you catch cross-catalog
duplicates. Only report records that are within the filtered scope, but note if the
duplicate is in a different catalog.

Issue: `"Duplicate code — also used by '<other name>' (<other sf_id>)."`

---

#### CHECK 7 — Inactive product referenced as a bundle component (WARNING)
A product where `active: false` that appears as a `code` in any bundle's `groups.components`.

Issue: `"Inactive product is a component in bundle '<bundle name>' (<bundle code>)."`

---

#### CHECK 8 — Bundle component code not found in snapshot (WARNING)
A bundle component `code` that does not match any product or bundle in the snapshot.

This may mean the component exists in the org but wasn't captured in the snapshot, or
the component code is a typo.

Issue: `"Component code '<code>' not found in snapshot — may need a /sync-rca-org refresh."`

---

#### CHECK 9 — Multiple Standard Price Book entries for the same product (WARNING)
A product or bundle with more than one `pricebook_entries` entry where `pricebook` = "Standard Price Book".

This is a data anomaly — the org may have duplicate PricebookEntry records.

Issue: `"Multiple Standard Price Book entries (<N> found) — possible duplicate PricebookEntry records in org."`

---

### STEP 3 — Display the report

**Header:**
```
┌──────────────────────────────────────────────────────────────────┐
│  RCA Catalog Health Report                                        │
│  Snapshot: <last_synced>   Scope: <All / Catalog: X / Code: Y>   │
│  Checked: <N> products, <M> bundles                              │
└──────────────────────────────────────────────────────────────────┘
```

**If no issues found:**
```
✓ All clear — no issues found across <N> records.
```

**If issues found**, group by severity, then by check name:

```
ERRORS (3)
──────────────────────────────────────────────────────────────────
Missing PSM option (2 records)
  • PLAT-ENT-001  Enterprise Platform License
      No selling model options — product cannot be quoted.
  • GW001         Google Workspace
      No selling model options — product cannot be quoted.

Blank product code (1 record)
  • (blank)  Kyocera Thermal Compound Premium  [SF: 01tg7000000cu9jAAA]
      Product code is blank — upload will fail or create a record with no code.

WARNINGS (2)
──────────────────────────────────────────────────────────────────
$0 price on active product (2 records)
  • CHG-001   Charger        Standard Price Book: $0.00
  • CTRL-001  Controller     Standard Price Book: $0.00

INFO (5)
──────────────────────────────────────────────────────────────────
No catalog assignment (5 records)
  • LP001    Laptop
  • MO001    Monitor
  • DS001    Desktop
  • KB001    Keyboard
  • P001     Printer
  … and 0 more
```

**Summary line:**
```
Summary: <E> error(s), <W> warning(s), <I> info item(s) across <N> records checked.
```

---

### STEP 4 — Offer next actions

```
What would you like to do?
  - fix   — walk through fixing a specific issue (I'll guide you)
  - export — show all issues as a plain list you can copy
  - done  — nothing
```

**fix**: Ask which issue to fix. For each issue type, give the appropriate remediation:
- Missing PSM → suggest running `/describe-rca-product` and updating the product, or ask if they want to add PSMs now.
- $0 price → suggest running `/update-price <code>`.
- No catalog → suggest running `/describe-rca-product` for catalog placement, or use the clone flow.
- Blank code → advise editing the product in Setup directly (cannot fix via API without an Id).
- Duplicate code → show both records and advise which to keep or rename.
- Inactive component → advise either reactivating the product or removing it from the bundle.
- Missing component → suggest running `/sync-rca-org` to refresh, or verify the code in Setup.

**export**: Print a plain-text list of all issues, one per line:
```
ERROR | CHG-001 | Charger | $0 price on active product
WARNING | …
```
