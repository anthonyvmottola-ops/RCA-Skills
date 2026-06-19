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

| Skill | Invocation | Description |
|---|---|---|
| `describe-rca-product.md` | `/describe-rca-product` | Conversational product intake — describe a product in natural language, Claude builds the YAML |
| `create-rca-products.md` | `/create-rca-products` | Upload `rca_session.yaml` to Salesforce (dry-run first, then confirm) |
| `sync-rca-org.md` | `/sync-rca-org` | Sync org state to a local `.rca/org-snapshot.yaml` snapshot |
| `promote-rca-products.md` | `/promote-rca-products` | Promote products from one org to another |

### Installation

Copy the skill files you want into your project's `.claude/commands/` directory:

```bash
cp skills/*.md /path/to/your/sf-project/.claude/commands/
```

---

## Scripts (`scripts/`)

| Script | Description |
|---|---|
| `create_rca_products.py` | Creates Product2, PSM options, PricebookEntries, bundle groups, classifications, and attributes from YAML |
| `create_price_adjustments.py` | Creates PriceAdjustmentSchedule records and child adjustments (Volume/Tier, Attribute-Based, Bundle-Based) |
| `sync_org_snapshot.py` | Queries the org and writes a full snapshot YAML to `.rca/org-snapshot.yaml` |
| `update_rca_catalog.py` | Merges a single-product JSON payload into the session YAML catalog |
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

```
/describe-rca-product        →  builds rca_session.yaml
/create-rca-products         →  uploads products to Salesforce
/sync-rca-org                →  refreshes .rca/org-snapshot.yaml
create_price_adjustments.py  →  adds pricing rules to uploaded products
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
