# /sync-rca-org

Sync the current Salesforce org's RCA product state to a local `.rca/org-snapshot.yaml`
file in the current project directory. Creates the file on first run; overwrites it on
subsequent runs. Works for any org — use `--org <alias>` to target a different one.

---

## Overview

The snapshot captures every Product2 (products and bundles), ProductCatalog,
ProductCategory, ProductSellingModelOption, PricebookEntry, ProductComponentGroup,
and ProductRelatedComponent in the org. It includes Salesforce record IDs alongside
config fields so the file can be used for both reference and future direct-record
operations.

Run this:
- After your first `/describe-rca-product` session on a new project
- After `/create-rca-products` to record newly created products
- Any time you want to refresh the local view of what's in the org
- When setting up a new VSCode project for a different org

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Locate scripts and determine output path

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the current project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory
4. `rca-product-creator/` subdirectory of current directory

Set `SCRIPTS_DIR` to the first directory that contains `sync_org_snapshot.py`.

**Determine the project root** — the directory containing `CLAUDE.md` or `.git/`,
whichever is found first walking up from the current working directory.
If neither is found, use the current working directory.

Set `OUTPUT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

**Determine the org alias:**
1. If `--org <alias>` was passed, use that
2. Otherwise read the default org alias from `CLAUDE.md` (`## RCA Tools / Default org alias:`)
3. If not found, use `myorg`

---

### STEP 1 — Check prerequisites

```bash
sf --version
sf org display [--target-org <alias>]
python --version
python -c "import requests, yaml; print('dependencies OK')"
```

If `requests` or `pyyaml` are missing, install automatically:
```bash
pip install requests pyyaml
```

Report the Instance URL so the user can confirm it's the right org.

---

### STEP 2 — Run the sync script

```bash
python <SCRIPTS_DIR>/sync_org_snapshot.py \
  --org <alias> \
  --output <OUTPUT_PATH>
```

Show all script output verbatim. The script prints progress lines as it queries each
object and a final summary line on success:

```
✓ Synced: 9 products, 3 bundles, 2 catalogs  →  /path/to/.rca/org-snapshot.yaml
```

If the script exits non-zero, show the error and stop.

---

### STEP 3 — Display a summary table

After the script succeeds, read `.rca/org-snapshot.yaml` and display:

```
┌──────────────────────────────────────────────────────────────────┐
│  RCA Org Snapshot  ·  <org_alias>  ·  synced <last_synced>       │
├────────────────────────┬──────────────────────────────────────────┤
│  Catalogs              │  <catalog names, comma-separated>        │
│  Products              │  <N> total  ·  <code1>, <code2>, …       │
│  Bundles               │  <N> total  ·  <code1>, <code2>, …       │
├────────────────────────┼──────────────────────────────────────────┤
│  managed_by breakdown  │  RCA only: N  ·  CPQ only: N             │
│                        │  Both (mid-migration): N  ·  Neither: N  │
├────────────────────────┴──────────────────────────────────────────┤
│  Snapshot written to: .rca/org-snapshot.yaml                      │
└──────────────────────────────────────────────────────────────────┘
```

Truncate product/bundle code lists to the first 5 items followed by `…` if longer.

**`managed_by` field** — every product and bundle in the snapshot carries a `managed_by`
value indicating which system manages it:

| Value | Meaning |
|-------|---------|
| `rca` | Has `ProductSellingModelOption` records — managed by RCA |
| `cpq` | Has CPQ records (`SBQQ__ProductFeature__c`, `SBQQ__ProductOption__c`, or `SBQQ__SubscriptionType__c`) — managed by CPQ only |
| `both` | Has both RCA and CPQ records — mid-migration state |
| `neither` | Plain `Product2` with no CPQ or RCA enrichment |

If CPQ is not installed (SBQQ objects absent), all products will be tagged `rca` or
`neither`. The CPQ detection queries fail silently — no error is shown.

---

### STEP 4 — Git notice (first run only)

If a `.git/` directory exists in the project root AND `.rca/org-snapshot.yaml` is
**not** already in `.gitignore`, offer:

> "The snapshot file `.rca/org-snapshot.yaml` is not in `.gitignore`.
> Since it's auto-generated, you may want to ignore it — or commit it
> to version-control the org state over time.
>
> - **ignore** — add `.rca/org-snapshot.yaml` to `.gitignore`
> - **track** — leave it as-is so git picks it up
> - **skip** — do nothing for now"

Only ask this once per project (skip if `.rca/org-snapshot.yaml` already appears
in `.gitignore`).

---

## Notes

- The snapshot is always a **full overwrite** — it reflects the complete current org
  state, not a delta.
- The `sf_id` fields in the snapshot are for reference only; the create/describe
  commands do not use them for writes.
- If the org has no RCA products yet, the snapshot will contain empty `products`
  and `bundles` lists — that's valid and useful (it means the catalog names/codes
  are still captured).
- To target a different org: `/sync-rca-org --org <alias>` — the snapshot will be
  written to the same `.rca/org-snapshot.yaml` path (overwriting the previous org's
  data). If you work with multiple orgs in the same project, commit the snapshot
  between switches so you have a record of each.
