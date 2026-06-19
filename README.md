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

### Authoring & Upload

| Skill | Invocation | Description |
|---|---|---|
| `describe-rca-product.md` | `/describe-rca-product` | Conversational product intake — describe a product in natural language, Claude builds the YAML and optionally uploads |
| `create-rca-products.md` | `/create-rca-products` | Upload `rca_session.yaml` to Salesforce (dry-run first, then confirm) |
| `describe-price-adjustment.md` | `/describe-price-adjustment` | Conversational intake for price adjustments — Volume, Attribute-Based, and Bundle-Based schedules |
| `promote-rca-products.md` | `/promote-rca-products` | Promote products from one org to another |

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

### Org Sync

| Skill | Invocation | Description |
|---|---|---|
| `sync-rca-org.md` | `/sync-rca-org` | Sync org state to a local `.rca/org-snapshot.yaml` snapshot — required by all lookup/audit skills |

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
| `create_rca_products.py` | Creates Product2, PSM options, PricebookEntries, bundle groups, classifications, and attributes from YAML. Resolves component codes from the org for bundles referencing pre-existing products. Auto-refreshes the **Price Book Entries V2** decision table when PricebookEntry records are created or updated. |
| `create_price_adjustments.py` | Creates PriceAdjustmentSchedule records and child adjustments (Volume/Tier, Attribute-Based, Bundle-Based). Auto-refreshes the relevant decision tables after each run. |
| `refresh_decision_tables.py` | Refreshes RCA decision tables via the `refreshDecisionTable` standard action. Used automatically by the create scripts; also callable standalone: `python refresh_decision_tables.py --tables pricebook,attribute --org myorg`. |
| `sync_org_snapshot.py` | Queries the org and writes a full snapshot YAML to `.rca/org-snapshot.yaml` |
| `update_rca_catalog.py` | Merges a single-product JSON payload into the session YAML catalog (used by `/describe-rca-product` and `/clone-product`) |
| `update_rca_adjustments.py` | Merges a single adjustment JSON payload into the session adjustments YAML (used by `/describe-price-adjustment`) |
| `promote_rca_products.py` | Copies product records from a source org to a target org |

### Requirements

```bash
pip install requests pyyaml
```

Authentication uses `sf org display` — no passwords stored. Requires the [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) and an authenticated org alias.

---

## Templates (`templates/`)

| File | Description |
|---|---|
| `rca_adjustments.yaml` | Example catalog for `create_price_adjustments.py` — Volume, Attribute, and Bundle schedule examples |

Copy and edit for your own products. `rca_session.yaml` and `rca_catalog.yaml` are org-specific and are excluded from version control via `.gitignore`.

---

## Workflow

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

---

## Snapshot

The lookup and audit skills (`/find-product`, `/bundle-breakdown`, `/catalog-health`, `/clone-product`) all read from `.rca/org-snapshot.yaml` — a local YAML file that mirrors your org's product state. Run `/sync-rca-org` after any upload to keep it current.

The snapshot is org-specific and excluded from version control by `.gitignore`. It lives at:

```
<project-root>/.rca/org-snapshot.yaml
```

---

## CLAUDE.md Setup

Add this to your Salesforce project's `CLAUDE.md` so Claude Code knows where the tools live:

```markdown
## RCA Tools
- **Scripts:** ~/tools/rca-product-creator/
- **Default org alias:** myorg
- **Session catalog:** ~/tools/rca-product-creator/rca_session.yaml
- **Master catalog:** ~/tools/rca-product-creator/rca_catalog.yaml
```
