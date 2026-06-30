# /promote-rca-products

Promotes RCA product configs from a source (org snapshot or catalog YAML) to a
target Salesforce org. Creates new records and updates changed ones (upsert). Supports
optional code filtering to cherry-pick specific products or bundles.

---

## Overview

Typical use: push product config from a dev org snapshot to a sandbox or production org
as part of a Dev → Sandbox → Prod pipeline.

The command:
1. Reads the source snapshot (stripping org-specific IDs so they don't carry over)
2. Optionally filters to specific product/bundle codes
3. Pre-flight checks that required PSMs and pricebooks exist in the target org
4. Dry-runs first, then asks for confirmation before the live promote

---

## Invocation

```
/promote-rca-products [--source <path>] --target-org <alias> [--include CODE1,CODE2] [--dry-run]
```

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Collect arguments

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the current project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory

Set `SCRIPTS_DIR` to the first directory that contains `promote_rca_products.py`.

**Determine the project root** — the directory containing `CLAUDE.md`, walking up from the current directory. If not found, use the current directory.

**Resolve `--source`:** if not provided, check in order:
1. `<PROJECT_ROOT>/.rca/org-snapshot.yaml`
2. `<PROJECT_ROOT>/.rca/rca_session.yaml`
3. `<PROJECT_ROOT>/.rca/rca_catalog.yaml`

Use the first one that exists.

**Resolve `--target-org`:** required — if not provided, ask:
> "Which org alias should I promote to? (e.g. sandbox, uat, production)"

**Resolve `--include`:** optional. If not provided, all products and bundles in the
source will be promoted.

---

### STEP 1 — Confirm promotion scope

Before running anything, show:

```
Promote summary
───────────────────────────────────────────────────
Source:      .rca/org-snapshot.yaml  (144 products, 21 bundles)
Target org:  sandbox
Filter:      EPL-001, BUNDLE-ENT-001                  ← if --include used
             All products and bundles                  ← if no filter
───────────────────────────────────────────────────
```

Ask:
> "Ready to proceed? (yes / cancel)"

Only continue on explicit confirmation.

---

### STEP 2 — Check prerequisites

```bash
sf --version
sf org display --target-org <target-org>
python --version
python -c "import requests, yaml; print('dependencies OK')"
```

Report the target Instance URL so the user can confirm it's the right org.

---

### STEP 3 — Dry run

Always dry-run first:

```bash
python <SCRIPTS_DIR>/promote_rca_products.py \
  --source <source_path> \
  --target-org <target_org> \
  [--include <codes>] \
  --dry-run \
  [--api-version <ver>]
```

Show all output. The script prints:
- Pre-flight check results (✓ / ✗ for each PSM and custom pricebook)
- A warning + confirmation prompt if prerequisites are missing
- The full dry-run output from `create_rca_products.py`

After the dry run completes, ask:
> "Dry run complete. Ready to promote to `<target-org>`? (yes / cancel)"

Only proceed on explicit confirmation.

---

### STEP 4 — Live promote

```bash
python <SCRIPTS_DIR>/promote_rca_products.py \
  --source <source_path> \
  --target-org <target_org> \
  [--include <codes>] \
  [--api-version <ver>]
```

Show all output. Surface any `ERROR` lines prominently.

---

### STEP 5 — Post-promote

After a successful promote:

> "✓ Promote complete → `<target-org>`
>
> 💡 Run `/sync-rca-org --org <target-org>` from the target project to update its
> snapshot with the newly promoted records."

---

## Common Scenarios

### Promote everything from the current org snapshot

```
/promote-rca-products --target-org sandbox
```

Uses `.rca/org-snapshot.yaml` as source, promotes all products and bundles.

### Promote specific products only

```
/promote-rca-products --target-org sandbox --include EPL-001,PS-001
```

### Promote a bundle (auto-includes its component products)

```
/promote-rca-products --target-org sandbox --include BUNDLE-ENT-001
```

The script automatically adds the bundle's component product codes so the bundle
can be fully assembled in the target org.

### Promote from the session catalog instead of the snapshot

```
/promote-rca-products --source ~/tools/rca-product-creator/rca_session.yaml --target-org sandbox
```

### Preview only (no changes)

```
/promote-rca-products --target-org sandbox --dry-run
```

---

## Notes

- The promote always runs in **upsert mode** — existing records are updated if their
  config fields differ from the source, not just skipped.
- Salesforce record IDs (`sf_id` fields in the snapshot) are stripped before promote —
  they are org-specific and cannot transfer between orgs.
- PSMs (ProductSellingModels) and custom pricebooks must be pre-created in the target
  org. The pre-flight check will flag any that are missing.
- Bundle components are always auto-included when a bundle code is in `--include` — you
  don't need to list them separately.
- `create_rca_products.py --upsert` is what performs the actual upsert; this command is
  the orchestration layer on top of it.
