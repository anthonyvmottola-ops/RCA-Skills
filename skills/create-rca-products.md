# /create-rca-products

Uploads the RCA product catalog to Salesforce Revenue Cloud Advanced (ARM).
Reads from a single YAML catalog file and creates Product2, ProductSellingModelOption,
PricebookEntry, ProductGroup, and ProductRelatedComponent records.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp create-rca-products.md /path/to/your/sf-project/.claude/commands/create-rca-products.md

---

## What Gets Created

From `rca_session.yaml` (the current session catalog, or pass `--catalog` for a different file):

| Catalog Section | Salesforce Records Created                          |
|-----------------|-----------------------------------------------------|
| `catalogs[].name` | `ProductCatalog` (looked up by Name/Code, or created) |
| `catalogs[].categories` | `ProductCategory` records, recursive (looked up or created; supports nested hierarchies) |
| `products[].category` / `bundles[].category` | `CatalogProduct` junction record linking Product2 → ProductCategory |
| `products`      | Product2, ProductSellingModelOption, PricebookEntry |
| `bundles`       | Product2, ProductSellingModelOption, PricebookEntry, ProductComponentGroup, ProductRelatedComponent |
| `classification` (per entry) | Sets `Product2.BasedOnId` → `ProductClassification` (looked up by Name or Code) |
| `attributes[].picklist_name` | AttributePicklist (looked up by Name/Code, or created; `DataType` defaults to `"Text"`) |
| `attributes[].picklist_values` | AttributePicklistValue records linked to the AttributePicklist |
| `attributes[].data_type` | AttributeDefinition (looked up by Name/Code, or created when `data_type` is provided) |
| `attributes` (per entry) | ProductAttributeDefinition linking AttributeDefinition to the product |

---

## Invocation

```
/create-rca-products [--catalog <path>] [--org <alias>] [--dry-run]
```

If arguments are not provided, ask the user:
1. Path to the YAML catalog (default: `rca_catalog.yaml` in the current directory)
2. Which sf CLI org alias to target (or default if blank)

---

## Step-by-Step Workflow

### Step 0 — Collect arguments

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the current project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory
4. `rca-product-creator/` subdirectory of current directory

**Determine the project root** — the directory containing `CLAUDE.md`, walking up from the current directory. If not found, use the current directory.

If `--catalog` not provided, use the `Session catalog:` value from `CLAUDE.md` (resolved relative to project root); if not found, fall back to `<PROJECT_ROOT>/.rca/rca_session.yaml`.
If the session catalog is empty or missing, fall back to `<PROJECT_ROOT>/.rca/rca_catalog.yaml`.
If neither found, ask: "Where is your catalog file?"

If `--org` not provided, read the default org alias from `CLAUDE.md` (`## RCA Tools / Default org alias:`).
If no `CLAUDE.md`, ask: "Which org alias? (Leave blank for default)"

If `--org` not provided, ask: "Which org alias? (Leave blank for default)"

### Step 1 — Check prerequisites

```bash
sf --version
sf org display [--target-org <alias>]
python --version
python -c "import requests, yaml; print('dependencies OK')"
```

If `requests` or `pyyaml` missing, install automatically:
```bash
pip install requests pyyaml
```

Report the Instance URL so the user can confirm it's the right org.

### Step 1b — Check org snapshot for duplicate codes

**Determine the project root** — the directory containing `CLAUDE.md` or `.git/`,
whichever is found first walking up from the current working directory.
Set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

```bash
python -c "
import yaml, sys, os

snapshot_path = '<SNAPSHOT_PATH>'
catalog_path  = '<catalog_path>'

if not os.path.isfile(snapshot_path):
    print('NO_SNAPSHOT')
    sys.exit(0)

with open(snapshot_path) as f:
    snap = yaml.safe_load(f)
with open(catalog_path) as f:
    cat  = yaml.safe_load(f)

snap_codes = set()
for p in snap.get('products', []):
    if p.get('code'): snap_codes.add(p['code'])
for b in snap.get('bundles', []):
    if b.get('code'): snap_codes.add(b['code'])

session_codes = []
for p in cat.get('products', []):
    if p.get('code'): session_codes.append(p['code'])
for b in cat.get('bundles', []):
    if b.get('code'): session_codes.append(b['code'])

overlap = [c for c in session_codes if c in snap_codes]
if overlap:
    print('OVERLAP|' + '|'.join(overlap))
else:
    print('NO_OVERLAP')
"
```

**If `NO_SNAPSHOT`:** Skip silently — continue to Step 2.

**If `NO_OVERLAP`:** Skip silently — all codes are new.

**If `OVERLAP`:** Display a warning before proceeding:

> ⚠️  The following product codes already appear in the org snapshot:
> `CODE-1`, `CODE-2`, …
>
> The create script is idempotent — it will **skip** these records (no changes made).
> This is safe; just confirming you're aware.
>
> Continue? **(yes / cancel)**

Only proceed on explicit confirmation.

### Step 2 — Preview the catalog

Show the user what's in the catalog before touching anything:

```bash
python -c "
import yaml
with open('<catalog_path>') as f:
    cat = yaml.safe_load(f)
products = cat.get('products', [])
bundles  = cat.get('bundles', [])
print(f'Products ({len(products)}):')
for p in products:
    psms = ', '.join(p.get('psm_options', []))
    pbes = len(p.get('pricebook_entries', []))
    print(f'  {p[\"code\"]:20s}  {p[\"name\"]:40s}  PSMs: {psms}  PBEs: {pbes}')
print(f'Bundles ({len(bundles)}):')
for b in bundles:
    groups     = b.get('groups', [])
    components = sum(len(g.get('components',[])) for g in groups)
    print(f'  {b[\"code\"]:20s}  {b[\"name\"]:40s}  {len(groups)} groups, {components} components')
"
```

### Step 3 — Dry run

ALWAYS run dry-run first:

```bash
python create_rca_products.py --catalog <catalog_path> [--org <alias>] --dry-run
```

Show the output. Ask:
> "Dry run complete. Ready to upload to Salesforce? (yes / cancel)"

Only proceed on explicit confirmation.

### Step 4 — Live upload

```bash
python create_rca_products.py --catalog <catalog_path> [--org <alias>]
```

Surface any ERROR lines clearly.

### Step 5 — Verify

After upload, confirm key records exist:

```bash
sf data query --query "SELECT ProductCode, Name, IsActive FROM Product2 WHERE ProductCode IN (<codes>) ORDER BY ProductCode" [--target-org <alias>]

sf data query --query "SELECT Name, ParentProduct.ProductCode, Sequence FROM ProductGroup WHERE ParentProduct.ProductCode IN (<bundle_codes>) ORDER BY ParentProduct.ProductCode, Sequence" [--target-org <alias>]
```

After showing the verification results, remind the user:

> "💡 Run `/sync-rca-org` to refresh the org snapshot with your newly created products."

---

## Idempotency

Safe to re-run. Existing records are skipped, matched by:
- ProductCatalog → `Name` (then `Code`) — skip if found
- ProductCategory → `(Name, CatalogId, ParentCategoryId)` — skip if found
- CatalogProduct → `(Product2Id, ProductCategoryId)` — skip if found
- Product2 → `ProductCode`
- PSM Options → `(Product2Id, ProductSellingModelId)`
- PricebookEntry → `(Product2Id, Pricebook2Id, CurrencyIsoCode)`
- ProductComponentGroup → `(Name, ParentProductId)`
- ProductRelatedComponent → `(ParentProductId, ChildProductId, ProductComponentGroupId)`
- Classification → `Product2.BasedOnId` already equals the target `ProductClassification.Id`
- AttributePicklist → `Name` (then `Code`) — skip if found
- AttributePicklistValue → `(PicklistId, Code)` — skip if found
- AttributeDefinition → `Name` (then `Code`) — skip if found; created only when `data_type` is given
- ProductAttributeDefinition → `(Product2Id, AttributeDefinitionId)`

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `sf CLI error` | Not authenticated | `sf org login web` |
| `ProductSellingModel 'X' not found` | PSM missing | Create in Setup › Revenue › Selling Models |
| `Pricebook 'X' not found` | Pricebook missing/inactive | Create/activate in Setup › Pricebooks |
| `Create ProductComponentGroup failed [400]` | Field version mismatch | Remove `min_selections`/`max_selections` from that group in the YAML |
| `ProductClassification 'X' not found` | Classification missing or inactive | Check Name/Code in Setup › Product Classifications |
| `AttributeDefinition 'X' not found` | Attribute not yet created | Add `data_type` (and `picklist_name`/`picklist_values` if Picklist) to the catalog entry so the script creates it |
| `INSUFFICIENT_ACCESS` | Missing permissions | Assign Revenue Cloud Admin permission set |
| `ProductCategoryProduct … failed [400]` | Field or permission issue | Check that `ProductCategoryId` and `ProductId` are correct; assign Revenue Cloud Admin permission set |
| `ProductCategory 'X' not found` | Category missing or name mismatch | Check Name in Setup › Product Catalog Management, or add it to the `catalogs` section in the YAML |

---

## Notes

- ProductSellingModels must exist before running — the script looks them up by name.
- Bundle `price: 0.00` is intentional — pricing flows through components via pricing procedures.
- To inspect available fields: `sf sobject describe --sobject ProductGroup --json`
