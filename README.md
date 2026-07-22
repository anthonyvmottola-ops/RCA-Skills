# RCA Skills — Salesforce Revenue Cloud Advanced Dev Tooling

Claude Code skills and Python scripts for automating product setup and pricing in Salesforce Revenue Cloud Advanced (RCA/ARM).

---

## Contents

```
skills/       Claude Code slash commands — drop into .claude/commands/
scripts/      Python automation scripts
templates/    YAML catalog templates
```

---

## Skills (`skills/`)

### CPQ Migration

| Skill | Invocation | Description |
|---|---|---|
| `cpq-rca-health.md` | `/cpq-rca-health` | Migration health check — reads the org snapshot and reports `managed_by` status (rca/cpq/both/neither) for every product, flags mid-migration items, and recommends next steps. No live org connection required. |
| `convert-cpq-to-rca.md` | `/convert-cpq-to-rca` | Converts Salesforce CPQ bundles and products to RCA YAML — fully conversational, prompts for source/target org and product selection. Respects `managed_by` tagging to skip already-migrated products in same-org mode. |

### Authoring & Upload

| Skill | Invocation | Description |
|---|---|---|
| `describe-rca-product.md` | `/describe-rca-product` | Conversational product intake — describe a product in natural language, Claude builds the YAML and optionally uploads |
| `create-rca-products.md` | `/create-rca-products` | Upload `rca_session.yaml` to Salesforce (dry-run first, then confirm) |
| `describe-price-adjustment.md` | `/describe-price-adjustment` | Conversational intake for price adjustments — Volume, Attribute-Based, and Bundle-Based schedules |

### Lookup & Audit (snapshot-only, no API calls)

| Skill | Invocation | Description |
|---|---|---|
| `find-product.md` | `/find-product [term]` | Search the org snapshot by name, code, family, catalog, or keyword — compact table with full detail on demand |
| `catalog-health.md` | `/catalog-health [scope]` | Audit the snapshot for 9 issue types: missing prices, missing PSMs, blank codes, duplicate codes, $0 prices, inactive bundle components, and more |
| `bundle-breakdown.md` | `/bundle-breakdown [code]` | Full component tree with per-component prices, default/min/max configuration totals, and active adjustment schedule callouts |

### Editing

| Skill | Invocation | Description |
|---|---|---|
| `update-price.md` | `/update-price [code price]` | Update one or more PricebookEntry records by product code + pricebook — resolves ID via SOQL, patches UnitPrice, confirms before/after |
| `clone-product.md` | `/clone-product [code]` | Clone an existing product or bundle from the snapshot, modify fields and group/component structure, then upload — works for both products and bundles |

### Pricing Procedures (Metadata API, not REST)

Pricing Procedures are `ExpressionSetDefinition` metadata (nested XML, like a Flow) — not a simple SObject. These two skills work via `sf project retrieve`/`deploy` instead of REST record CRUD, and always write to a new **Draft** version so the live Active version is never touched in place.

| Skill | Invocation | Description |
|---|---|---|
| `describe-pricing-procedure.md` | `/describe-pricing-procedure "<name>"` | Read-only: retrieve and render a Pricing Procedure's steps, `customElement` parameters, and versions. Collapses plumbing/structural steps and non-Active versions by default (`--full`/`--step <name>` show everything unfiltered). |
| `update-pricing-procedure.md` | `/update-pricing-procedure "<name>"` | Edit an existing step's field/parameter, or add a new step cloned from an existing one (never authored from scratch). Consults `pricing_procedure_step_catalog.yaml` before interviewing. Always clones into a new Draft version; dry-run then deploy. |

### Org Sync & Promotion

| Skill | Invocation | Description |
|---|---|---|
| `sync-rca-org.md` | `/sync-rca-org` | Sync org state to a local `.rca/org-snapshot.yaml` snapshot. Tags every product with `managed_by` (rca/cpq/both/neither) — required by all lookup/audit skills and by `/convert-cpq-to-rca` in same-org mode |
| `org-diff.md` | `/org-diff --target <path>` | Compare two org snapshots before promoting — shows products missing in target, price deltas, PSM mismatches, bundle structure diffs, and selling model gaps. No API calls; pure snapshot comparison. |
| `promote-rca-products.md` | `/promote-rca-products` | Promote products from a source snapshot to a target org (upsert). Pre-flight checks for PSMs and pricebooks; always dry-runs first. |

### Installation

Copy the skill files you want into your project's `.claude/commands/` directory:

```bash
cp skills/*.md /path/to/your/sf-project/.claude/commands/
```

Or copy globally so they're available in every project:

```bash
cp skills/*.md ~/.claude/commands/
```

---

## Scripts (`scripts/`)

| Script | Description |
|---|---|
| `create_rca_products.py` | Creates Product2, PSM options, PricebookEntries, bundle groups, classifications, and attributes from YAML. Supports `IsQuantityEditable` and `QuantityScaleMethod` on bundle components. Auto-refreshes the **Price Book Entries V2** decision table when PricebookEntry records are created or updated. |
| `create_price_adjustments.py` | Creates PriceAdjustmentSchedule records and child adjustments (Volume/Tier, Attribute-Based, Bundle-Based). Auto-refreshes the relevant decision tables after each run. |
| `refresh_decision_tables.py` | Refreshes RCA decision tables via the `refreshDecisionTable` standard action. Used automatically by the create scripts; also callable standalone: `python refresh_decision_tables.py --tables pricebook,attribute --org myorg`. |
| `sync_org_snapshot.py` | Queries the org and writes a full snapshot YAML to `.rca/org-snapshot.yaml`. Tags every product and bundle with `managed_by: rca\|cpq\|both\|neither` based on whether CPQ (`SBQQ__*`) and/or RCA (`ProductSellingModelOption`) records exist. |
| `update_rca_catalog.py` | Merges a single-product JSON payload into the session YAML catalog. Supports `is_quantity_editable` and `quantity_scale_method` on bundle components; omits `min_qty`/`max_qty` when null (no false defaults). |
| `update_rca_adjustments.py` | Merges a single adjustment JSON payload into the session adjustments YAML (used by `/describe-price-adjustment`) |
| `promote_rca_products.py` | Copies product records from a source org to a target org (upsert). Pre-flight checks for missing PSMs and pricebooks in the target. |
| `diff_org_snapshots.py` | Compares two `.rca/org-snapshot.yaml` files — reports missing products, price deltas, PSM mismatches, bundle structure diffs, and selling model gaps. No API calls; match by `code` (org-agnostic). CLI: `--source`, `--target`, `--include`, `--format text\|json`, `--codes-only`. |
| `read_pricing_procedure.py` | Resolves a Pricing Procedure's DeveloperName via Tooling API, retrieves its real metadata via `sf project retrieve start`, parses it into a readable structure. Shared by both Pricing Procedure skills — never re-implemented. |
| `patch_pricing_procedure.py` | Clones a Pricing Procedure version into a new Draft, applies step edits (`set_field`/`set_parameter`/`set_condition`) and/or clone-based step additions, writes the patched XML plus a unified diff (no git dependency). |
| `catalog_pricing_procedure_steps.py` | Scans every `ExpressionSetDefinition` in the org, filters to genuine Pricing Procedures, and writes `.rca/pricing_procedure_step_catalog.yaml` — every `actionType`'s parameter shape, the operator/valueType vocabulary, and whether each procedure's `sequenceNumber` is a tiered/shared marker or a unique ordinal. |

### Requirements

```bash
pip install requests pyyaml
```

Authentication uses `sf org display` — no passwords stored. Requires the [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) and an authenticated org alias.

---

## Templates (`templates/`)

| File | Description |
|---|---|
| `rca_catalog.yaml` | Starter product catalog — copy to your org project's `.rca/rca_catalog.yaml` and edit |
| `rca_session.yaml` | Empty session buffer — copy to your org project's `.rca/rca_session.yaml` |
| `rca_adjustments.yaml` | Example price adjustments catalog — Volume, Attribute, and Bundle schedule examples |

All three files are org-specific and should live in your project's `.rca/` directory (excluded from org-project version control via `.gitignore`). The templates here are starter copies only.

---

## Workflow

### Dev → Sandbox → Prod promotion
```
/sync-rca-org                →  sync dev org snapshot (.rca/org-snapshot.yaml)
/sync-rca-org --org sandbox  →  sync sandbox snapshot to a separate path
/org-diff --target ../sandbox-project/.rca/org-snapshot.yaml
                             →  see what's missing, changed, or diverged before promoting
/promote-rca-products --target-org sandbox
                             →  dry-run + confirm + upsert missing/changed products
/sync-rca-org --org sandbox  →  refresh sandbox snapshot after promote
```

### CPQ migration
```
/sync-rca-org                →  sync snapshot (populates managed_by on each product)
/cpq-rca-health              →  briefing: counts by status, mid-migration flags, next steps
/convert-cpq-to-rca          →  query CPQ org → map to RCA YAML → write rca_session.yaml
/create-rca-products         →  dry-run + upload converted products
/sync-rca-org                →  refresh snapshot (converted products now tagged managed_by: rca)
```

### Product authoring
```
/describe-rca-product        →  conversational intake → rca_session.yaml
/clone-product               →  clone existing product/bundle → rca_session.yaml
/create-rca-products         →  upload rca_session.yaml to Salesforce
/sync-rca-org                →  refresh .rca/org-snapshot.yaml after changes
```

### Pricing
```
/describe-price-adjustment   →  conversational intake → rca_adj_session.yaml
create_price_adjustments.py  →  upload price adjustments + auto-refresh decision tables
/update-price                →  patch a single PricebookEntry directly
refresh_decision_tables.py   →  manually trigger a decision table refresh anytime
```

### Lookup & audit (no org connection needed)
```
/sync-rca-org                →  build/refresh the local snapshot first
/find-product [term]         →  search products and bundles by any field
/bundle-breakdown [code]     →  component tree + pricing totals for a bundle
/catalog-health [scope]      →  audit for common data problems
```

### Pricing Procedures
```
catalog_pricing_procedure_steps.py  →  build/refresh the actionType + sequencing reference
/describe-pricing-procedure "<name>" →  inspect current steps/versions (read-only)
/update-pricing-procedure "<name>"   →  edit a step, or add one cloned from an existing step
                                     →  dry-run deploy, then confirm, then live deploy (new Draft version)
```

---

## Snapshot

The lookup and audit skills (`/find-product`, `/bundle-breakdown`, `/catalog-health`, `/clone-product`) all read from `.rca/org-snapshot.yaml` — a local YAML file that mirrors your org's product state. Run `/sync-rca-org` after any upload to keep it current.

The snapshot is org-specific and excluded from version control by `.gitignore`. It lives at:

```
<project-root>/.rca/org-snapshot.yaml
```

Each product and bundle entry in the snapshot carries a `managed_by` field (`rca`, `cpq`, `both`, or `neither`) that tracks which system owns it. This enables `/convert-cpq-to-rca` to show only unmigranted CPQ products when running in same-org mode.

`/update-pricing-procedure` reads a second, similarly local, similarly regeneratable reference file:

```
<project-root>/.rca/pricing_procedure_step_catalog.yaml
```

Built by `catalog_pricing_procedure_steps.py` — a point-in-time snapshot, not live-queried at interview time, so re-run it after any deploy that adds/changes Pricing Procedure steps.

---

## CLAUDE.md Setup

Add this to your Salesforce project's `CLAUDE.md` so Claude Code knows where the tools live:

```markdown
## RCA Tools
- **Scripts:** ~/tools/rca-product-creator/
- **Default org alias:** myorg
- **Session catalog:** .rca/rca_session.yaml
- **Master catalog:** .rca/rca_catalog.yaml
- **Adjustments catalog:** .rca/rca_adjustments.yaml
```

The catalog paths are **project-relative** — each org project has its own `.rca/` directory containing its own catalog, session, and adjustments files. The scripts in `~/tools/rca-product-creator/` are the shared engine; only the data lives per-org.

### Setting up a new org project

```bash
mkdir -p .rca
# Copy starter templates from the repo
cp templates/rca_catalog.yaml .rca/
cp templates/rca_session.yaml .rca/
cp templates/rca_adjustments.yaml .rca/
# Sync the org snapshot
/sync-rca-org
```

Add `.rca/org-snapshot.yaml` to `.gitignore` (org-specific, not meant for version control). The catalog files may be committed if you want to track product definitions in source control.
