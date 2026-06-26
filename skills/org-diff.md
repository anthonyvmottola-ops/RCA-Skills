# /org-diff

Compare two RCA org snapshots to identify what differs between environments before
promoting products. Reports missing products, price changes, PSM mismatches, bundle
structure differences, and selling model gaps. No Salesforce API calls — pure snapshot
comparison.

---

## Overview

Use this before `/promote-rca-products` to see exactly what needs to move and whether
the target org is ready to receive it.

Typical use: compare a dev snapshot to a sandbox snapshot before a promote.

```
/org-diff --target /path/to/target/.rca/org-snapshot.yaml
/org-diff --target ../sandbox/.rca/org-snapshot.yaml --include EPL-001,ADEM-002
/org-diff --target ../prod/.rca/org-snapshot.yaml --codes-only
/org-diff --source /path/to/dev/.rca/org-snapshot.yaml --target /path/to/prod/.rca/org-snapshot.yaml
```

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Collect arguments

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the current project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory

Set `SCRIPTS_DIR` to the first directory that contains `diff_org_snapshots.py`.

If `diff_org_snapshots.py` cannot be found, stop:
> "Cannot locate `diff_org_snapshots.py`. Check that `~/tools/rca-product-creator/` exists
> and that the `/org-diff` skill has been installed."

**Resolve `--source`:** if not provided, use `.rca/org-snapshot.yaml` in the current
project root (same directory that contains `CLAUDE.md` or `.git/`). If that file does
not exist, stop:
> "No source snapshot found at `.rca/org-snapshot.yaml`. Run `/sync-rca-org` first,
> or pass `--source <path>` explicitly."

**Resolve `--target`:** required. If not provided, ask:
> "Which snapshot should I compare against? Provide the path to the target org's
> `.rca/org-snapshot.yaml` (e.g. `../sandbox-project/.rca/org-snapshot.yaml`)."

**Resolve `--include`:** optional. Pass through to the script unchanged if provided.

**Resolve `--codes-only`:** optional flag. Pass through to the script if present.

---

### STEP 1 — Confirm scope

Before running anything, show:

```
Org Diff
───────────────────────────────────────────────────
Source:  .rca/org-snapshot.yaml
Target:  <target_path>
Filter:  EPL-001, BUNDLE-ENT-001         ← if --include used
         All products and bundles         ← if no filter
───────────────────────────────────────────────────
```

Ask:
> "Ready to run the diff? (yes / cancel)"

Only continue on explicit confirmation.

---

### STEP 2 — Check prerequisites

```bash
python --version
python -c "import yaml; print('pyyaml OK')"
```

No Salesforce CLI check is needed — this skill makes no API calls.

If `pyyaml` is missing, stop:
> "`pyyaml` is not installed. Run `pip install pyyaml` and try again."

---

### STEP 3 — Run the diff script

```bash
python <SCRIPTS_DIR>/diff_org_snapshots.py \
  --source <source_path> \
  --target <target_path> \
  [--include <codes>] \
  [--codes-only] \
  --format text
```

Show all output verbatim.

If the script exits non-zero, surface the error prominently:
> "Diff failed: `<error output>`"
Stop — do not continue to STEP 4.

---

### STEP 4 — Interpret results and suggest next steps

After the script completes successfully, read the output and:

**If "No differences found":**
> "Snapshots are identical — no differences found between source and target."

**If there are items in "MISSING IN TARGET":**
Build a ready-to-run promote command. If the missing list is ≤ 20 codes, include
`--include <comma-separated codes>`. If more than 20 codes are missing, omit `--include`
(promote everything):

> "These products are in the source but not in the target. To promote them, run:
>
> `/promote-rca-products --source <source_path> --target-org <alias> --include CODE1,CODE2,...`
>
> Replace `<alias>` with the target org alias (e.g. `sandbox`, `uat`, `production`)."

**If there are items in "SELLING MODEL DIFFERENCES" with source-only entries:**
> "⚠ The following PSMs exist in the source but not in the target org:
> `<names>`.
> These must be created in the target org before `/promote-rca-products` can succeed —
> its pre-flight check will flag them. Create them via Setup > Revenue > Selling Models
> in the target org, then re-run `/sync-rca-org` on the target before promoting."

**If `--codes-only` was used and field/price/PSM diffs were reported:**
> "Field-level, price, and PSM details were suppressed. Re-run without `--codes-only`
> to see full diff details."

---

### STEP 5 — Offer next actions

After reporting results, offer:

```
What next?
  - promote  — run /promote-rca-products to push missing products to the target org
  - health   — run /catalog-health to audit the source snapshot for issues
  - detail   — re-run /org-diff without --codes-only for full field-level diff
  - done     — nothing more
```

For **promote**: print the ready-to-run `/promote-rca-products` command from STEP 4.
Do NOT invoke it inline — let the user run it so it goes through its own confirmation
and dry-run steps.

For **health**: invoke `/catalog-health` using the source snapshot.

For **detail**: re-run the diff without `--codes-only` using the same source and target.

---

## Notes

- All matching between source and target is done by `code` (the ProductCode field), not
  by Salesforce record ID — IDs are org-specific and differ between environments.
- Products or bundles with a blank or missing `code` field are skipped in the diff.
- The diff is symmetric in what it reports but asymmetric in direction: "missing in target"
  means the source has it and the target doesn't — these are the candidates for promotion.
  "new in target" means the target has extra products not in the source — these are not
  promoted away; they are just noted for awareness.
- PSM options comparison is set-based (order doesn't matter).
- Price comparison uses direct float equality as stored in the YAML. Tiny floating-point
  differences (e.g. 875.0 vs 875.0000001) are unlikely but possible; if they appear,
  they are harmless to re-promote (upsert is idempotent).
