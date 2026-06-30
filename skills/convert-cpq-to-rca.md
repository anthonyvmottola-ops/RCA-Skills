# /convert-cpq-to-rca

Convert Salesforce CPQ product and bundle definitions to RCA YAML catalog format, then
optionally upload to Salesforce Revenue Cloud Advanced via `/create-rca-products`.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp convert-cpq-to-rca.md /path/to/your/sf-project/.claude/commands/convert-cpq-to-rca.md

---

## Overview

Queries CPQ objects from Salesforce, maps them to the RCA YAML schema using the confirmed
CPQ → RCA field mapping, resolves ProductSellingModel names from the org snapshot, shows a
confirmation table with any flagged issues, writes to `rca_session.yaml`, then optionally
runs an upload.

Handles standalone products, bundles (including component products), and CPQ configuration
attributes. Attributes require manual completion if they need new AttributeDefinitions —
the skill detects them and walks through the extra fields.

---

## Invocation

```
/convert-cpq-to-rca [--product-code CODE] [--bundle-code CODE] [--all] [--source-org ALIAS] [--org ALIAS]
```

All flags are optional. When invoked with no arguments, the skill opens a short
conversational intake to collect org setup and product selection interactively.
Flags bypass the intake for scripted or power-user workflows.

| Flag | Behavior |
|------|----------|
| `--product-code CODE` | Convert one standalone product by ProductCode |
| `--bundle-code CODE` | Convert one bundle + all its component products |
| `--all` | Convert all active CPQ products and bundles |
| `--source-org ALIAS` | sf CLI alias for the CPQ org (default: prompted or same as `--org`) |
| `--org ALIAS` | sf CLI alias for the RCA target org (default: from CLAUDE.md) |

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Start a fresh session

**Determine the project root** — the directory containing `CLAUDE.md`, walking up from the current directory. If not found, use the current directory.

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory
4. `rca-product-creator/` subdirectory of current directory

Set `SCRIPTS_DIR` to the first directory that contains `update_rca_catalog.py`.
Set `CATALOG_PATH` to the `Session catalog:` value from `CLAUDE.md` (resolved relative to project root); if not found, fall back to `<PROJECT_ROOT>/.rca/rca_session.yaml`.

**Clear the session catalog** (run silently before anything else):

```bash
python -c "
import yaml, os
path = '<CATALOG_PATH>'
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
with open(path, 'w') as f:
    f.write('# RCA Session Catalog — CPQ conversion\n')
    f.write('# Upload: python create_rca_products.py --catalog rca_session.yaml\n\n')
    yaml.dump({'products': [], 'bundles': []}, f, default_flow_style=False)
print('Session catalog cleared:', path)
"
```

Do not show this step to the user.

---

### STEP 0b — Load the org snapshot (silent)

**Determine the project root** — the directory containing `CLAUDE.md` or `.git/`,
whichever is found first walking up from the current working directory.
Set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

```bash
python -c "
import yaml, sys, os
from datetime import datetime, timezone

path = '<SNAPSHOT_PATH>'
if not os.path.isfile(path):
    print('NO_SNAPSHOT')
    sys.exit(0)

with open(path) as f:
    snap = yaml.safe_load(f)

meta = snap.get('meta', {})
synced = meta.get('last_synced', '')
age_days = None
if synced:
    try:
        dt = datetime.fromisoformat(synced.replace('Z', '+00:00'))
        age_days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        pass

selling_models = snap.get('selling_models', [])
psm_summary = '; '.join(
    f\"{m.get('name','')} ({m.get('type','?')}/{m.get('pricing_term_unit') or 'n/a'})\"
    for m in selling_models
) or '(none)'

existing_codes = set()
for p in snap.get('products', []):
    if p.get('code'): existing_codes.add(p['code'])
for b in snap.get('bundles', []):
    if b.get('code'): existing_codes.add(b['code'])

print(f'SNAPSHOT_FOUND|{age_days}|{psm_summary}')
print(f'EXISTING_CODES|{\"|\".join(existing_codes)}')
"
```

**If `NO_SNAPSHOT`:** Store `SNAPSHOT_LOADED = false`.
**If `SNAPSHOT_FOUND`:** Store `SNAPSHOT_LOADED = true`, the full `selling_models` list,
and `EXISTING_CODES` set in memory.

Do not show anything to the user from this step.

---

### STEP 1 — Conversational intake

**If any flags were passed** (`--product-code`, `--bundle-code`, `--all`, `--source-org`,
`--org`), resolve them directly and skip to STEP 1c. Do not ask questions that flags
already answer.

---

#### STEP 1a — Org setup

**If `--source-org` and `--org` were both provided:** use them directly, skip to STEP 1c.

**If neither was provided**, ask:

> "First, a quick setup question — is CPQ in the **same Salesforce org** as your RCA
> environment, or are you converting from a **separate CPQ org**?
>
> - **same org** — CPQ and RCA co-exist in one org (I'll use your default RCA org for both)
> - **separate orgs** — you have a standalone CPQ org you're migrating from"

- **Same org**: set `SOURCE_ORG` = `RCA_ORG` = default org alias (from CLAUDE.md, or `myorg`).
- **Separate orgs**: ask in sequence:
  > "What's the sf CLI alias for your **CPQ source org**? (run `sf org list` to check)"
  Then:
  > "And the alias for your **RCA target org**? (press Enter for `myorg`)"
  Set `SOURCE_ORG` and `RCA_ORG` from the answers. Default `RCA_ORG` to `myorg` if blank.

**If only `--org` was provided** (no `--source-org`): ask:
> "Got the RCA target org (`<alias>`). Is CPQ in the same org, or a different one?"
- Same → `SOURCE_ORG` = `RCA_ORG` = `<alias>`
- Different → ask for the CPQ org alias, set `SOURCE_ORG`

**If only `--source-org` was provided** (no `--org`): use default org alias for `RCA_ORG`.

---

#### STEP 1b — What to convert

**If `--product-code`, `--bundle-code`, or `--all` was passed**: skip this step.

Ask:

> "What would you like to convert from `<SOURCE_ORG>`?
>
> - Type a **product name** and I'll search for it
> - Type a **product code** directly (e.g. `4KVIDEOCAM`)
> - Say **list** to browse all active products
> - Say **all** to convert every active product"

**If the user types a name (not recognisable as a code):**
Run a name search against the CPQ org:
```bash
sf data query \
  --query "SELECT Id, Name, ProductCode, Family, IsActive
           FROM Product2
           WHERE Name LIKE '%<term>%' AND IsActive = true
           ORDER BY Name LIMIT 20" \
  --target-org <SOURCE_ORG> --json
```
Show the results as a numbered list:
```
Found 4 matches:
  1. 4K Video Camera          (4KVIDEOCAM)  — Hardware
  2. 4K Video Camera Bundle   (4KVCAM-BDL)  — Hardware
  3. 4K Monitor               (4KMON-001)   — Hardware
  4. 4K Video Adapter         (4KVADAPT)    — Miscellaneous

Which would you like to convert? (enter a number, or multiple like 1,3)
```
Allow the user to pick one or several by number. For each selected product, detect
whether it is a bundle (has ProductOptions) during STEP 3.

**If the user says "list":**

**Same-org mode** (`SOURCE_ORG = RCA_ORG`): If the org snapshot is loaded, prefer
showing only products where `managed_by = "cpq"` — these are the unconverted CPQ
products. Present them as the default list. If the user says "show all", include
`managed_by = "both"` and `"neither"` products too, with a column showing their status.

**Separate-org mode** (`SOURCE_ORG ≠ RCA_ORG`): Run the `--all` query against
`SOURCE_ORG` (up to 50 records) and display the numbered table. No managed_by
filtering applies.

```
CPQ products available to convert (SOURCE_ORG, cpq-only):

  #  Code             Name                           Family     Status
  1  4KVIDEOCAM       4K Video Camera                Hardware   cpq
  2  CAMERAMIC        Camera Mount Microphone        Hardware   cpq
  3  PROAUDIOBOOMMIC  Pro Audio Boom Microphone      Misc       cpq
  4  VIDEOEDITOR      Video Editor License           Software   cpq

  (3 products already in RCA — say "show all" to see them)

Which would you like to convert? (numbers, a code, or "all")
```

Let the user select by number(s) or say "all".

**If the user types a code directly:** treat as a single `--product-code` selection.

**If the user says "all":** set mode to `--all`.

---

#### STEP 1c — Detect CPQ installation

Once `SOURCE_ORG` is resolved, verify CPQ is present:

```bash
sf data query \
  --query "SELECT COUNT() FROM SBQQ__ProductFeature__c LIMIT 1" \
  --target-org <SOURCE_ORG> --json 2>&1 | head -5
```

If this returns an error mentioning "sObject type" or "not found":
> "CPQ objects (SBQQ__*) are not present in `<SOURCE_ORG>`. Is that the right org?
> Run `sf org list` to check your connected orgs."
Stop and let the user correct the alias.

---

### STEP 2 — Query CPQ data

Run the queries below against `SOURCE_ORG`. Use `--target-org <SOURCE_ORG> --json` on
all queries. Collect results into memory.

#### 2a — Product2 records

**Single product/bundle, or a specific set selected during STEP 1b:**
```bash
sf data query \
  --query "SELECT Id, Name, ProductCode, Family, Description, IsActive,
           SBQQ__SubscriptionType__c, SBQQ__BillingType__c, SBQQ__BillingFrequency__c,
           SBQQ__SubscriptionTerm__c
           FROM Product2
           WHERE ProductCode = '<CODE>'" \
  --target-org <SOURCE_ORG> --json
```
If multiple codes were selected, use `WHERE ProductCode IN ('CODE1','CODE2',...)`.
If products were selected by Salesforce Id (from name search), use `WHERE Id IN (...)`.

**All-products mode:**
```bash
sf data query \
  --query "SELECT Id, Name, ProductCode, Family, Description, IsActive,
           SBQQ__SubscriptionType__c, SBQQ__BillingType__c, SBQQ__BillingFrequency__c,
           SBQQ__SubscriptionTerm__c
           FROM Product2
           WHERE IsActive = true
           ORDER BY ProductCode" \
  --target-org <SOURCE_ORG> --json
```

If all-products mode returns more than 50 products: show a count and pause:
> "Found N products. This will generate N YAML entries. Proceed? (yes / review list / cancel)"
If **review list**: print all `ProductCode — Name` pairs, then ask to confirm or filter.

#### 2b — Pricebook entries

For the products queried in 2a, fetch their pricebook entries. Build a comma-separated
list of Product2 Ids from the 2a results.

```bash
sf data query \
  --query "SELECT Id, Product2Id, Pricebook2.Name, UnitPrice, CurrencyIsoCode, IsActive
           FROM PricebookEntry
           WHERE Product2Id IN ('<ids>') AND IsActive = true
           ORDER BY Product2Id, Pricebook2.Name" \
  --target-org <source_alias> --json
```

Group results by `Product2Id` in memory.

#### 2c — Bundle features (ProductComponentGroup sources)

```bash
sf data query \
  --query "SELECT Id, Name, SBQQ__ConfiguredSKU__c, SBQQ__Number__c,
           SBQQ__MinOptionCount__c, SBQQ__MaxOptionCount__c
           FROM SBQQ__ProductFeature__c
           WHERE SBQQ__ConfiguredSKU__c IN ('<product2_ids>')" \
  --target-org <source_alias> --json
```

If the Id list is empty (single product mode with no bundle), skip this query.

**Note:** The parent bundle lookup field is `SBQQ__ConfiguredSKU__c`, NOT `SBQQ__Product__c`.
`SBQQ__Product__c` does not exist on `SBQQ__ProductFeature__c` in current CPQ versions.

#### 2d — Bundle options (ProductRelatedComponent sources)

```bash
sf data query \
  --query "SELECT Id, SBQQ__ConfiguredSKU__c, SBQQ__OptionalSKU__c, SBQQ__Feature__c,
           SBQQ__Number__c, SBQQ__MinQuantity__c, SBQQ__MaxQuantity__c,
           SBQQ__Required__c, SBQQ__Selected__c, SBQQ__Bundled__c,
           SBQQ__Type__c, SBQQ__QuantityEditable__c,
           SBQQ__OptionalSKU__r.ProductCode
           FROM SBQQ__ProductOption__c
           WHERE SBQQ__ConfiguredSKU__c IN ('<product2_ids>')" \
  --target-org <source_alias> --json
```

Group results by `SBQQ__ConfiguredSKU__c` (the bundle product's Id) in memory.

**Note:** The parent bundle lookup field is `SBQQ__ConfiguredSKU__c`, NOT `SBQQ__Product__c`.
`SBQQ__Product__c` does not exist on `SBQQ__ProductOption__c` in current CPQ versions.

#### 2e — Configuration attributes

```bash
sf data query \
  --query "SELECT Id, Name, SBQQ__Product__c, SBQQ__Feature__c,
           SBQQ__Required__c, SBQQ__UserDefined__c,
           SBQQ__Attribute__c, SBQQ__Attribute__r.Name
           FROM SBQQ__ConfigurationAttribute__c
           WHERE SBQQ__Product__c IN ('<product2_ids>')" \
  --target-org <source_alias> --json
```

Group results by `SBQQ__Product__c` in memory.

#### 2f — Collect component products not already in the query set

After 2d, check whether any `SBQQ__OptionalSKU__c` (child product Id) is not already
in the set of Product2 Ids from 2a. If any are missing, fetch them:

```bash
sf data query \
  --query "SELECT Id, Name, ProductCode, Family, Description, IsActive,
           SBQQ__SubscriptionType__c, SBQQ__BillingType__c, SBQQ__BillingFrequency__c,
           SBQQ__SubscriptionTerm__c
           FROM Product2
           WHERE Id IN ('<missing_ids>')" \
  --target-org <source_alias> --json
```

Merge these into the products list from 2a. Also run 2b and 2e for these new Ids.
These component products will appear as standalone entries in `products[]` in the
session catalog (unless they're also bundles themselves).

---

### STEP 3 — Classify products

After all queries, classify each Product2 record:

- **Bundle**: has one or more `SBQQ__ProductOption__c` records where `SBQQ__Product__c = Id`
  (i.e., it is the parent product in at least one option). Goes in `bundles[]`.
- **Standalone product**: no options where it is the parent. Goes in `products[]`.

A product can appear as a bundle AND also be a component of another bundle.
In that case, include it in both `bundles[]` (for its own structure) and reference its
`code` in the parent bundle's `components[]`.

**Bundle without features:** If a bundle has options but no `SBQQ__ProductFeature__c`
records, treat all its options as a single unnamed group. Generate a group name from the
bundle's name (e.g., "Enterprise Suite" → group name "Enterprise Suite Options").

---

### STEP 4 — Map CPQ fields to RCA YAML

For each product, build a draft YAML entry using the mappings below.

#### 4a — Core product fields

| CPQ Field | RCA Field | Notes |
|-----------|-----------|-------|
| `ProductCode` | `code` | Required |
| `Name` | `name` | Required |
| `Description` | `description` | Omit if blank |
| `Family` | `family` | Omit if blank |
| `IsActive` | `active` | Default: `true` |
| — | `uom` | Cannot be derived from CPQ; set to `"Each"` as default and flag for review |

#### 4b — PSM intent → PSM resolution

For each product, derive the PSM intent from CPQ billing fields:

| `SBQQ__SubscriptionType__c` | `SBQQ__BillingFrequency__c` | PSM Intent (type/unit) |
|-----------------------------|-----------------------------|-----------------------|
| `null` or blank | any | `OneTime / —` |
| `"One-time"` | any | `OneTime / —` |
| `"Subscription"` | `"Annual"` | `TermDefined / Annual` |
| `"Subscription"` | `"Monthly"` | `TermDefined / Months` |
| `"Subscription"` | `"Quarterly"` | `TermDefined / Quarterly` |
| `"Subscription"` | `"Semiannual"` | `TermDefined / Semi-Annual` |
| `"Subscription"` | `null` | `TermDefined / Annual` (assume annual; flag for review) |
| `"Evergreen"` | `"Annual"` | `Evergreen / Annual` |
| `"Evergreen"` | `"Monthly"` | `Evergreen / Months` |
| `"Evergreen"` | `"Quarterly"` | `Evergreen / Quarterly` |
| `"Evergreen"` | `"Semiannual"` | `Evergreen / Semi-Annual` |
| `"Evergreen"` | `null` | `Evergreen / Months` (assume monthly; flag for review) |
| `"Renewable"` | any | `TermDefined / <BillingFrequency>` (flag as assumed) |

**Resolve each intent to an actual PSM name** using the same logic as `/describe-rca-product` STEP 3:

**If `SNAPSHOT_LOADED = true`:** match against `selling_models` in the snapshot.

1. For `OneTime`: find entry where `type = "OneTime"`.
   - One match → auto-select. Note: *"Using `[name]` for one-time billing."*
   - Multiple → collect all — ask at STEP 5 confirmation.

2. For `Evergreen` or `TermDefined`: match by both `type` AND `pricing_term_unit`.
   - One match → auto-select.
   - Multiple → collect all — ask at STEP 5.
   - No match → mark as `UNMAPPED` and flag at STEP 5.

**If `SNAPSHOT_LOADED = false`:** run a live query before matching:
```bash
sf data query \
  --query "SELECT Id, Name, SellingModelType, PricingTermUnit FROM ProductSellingModel WHERE Status = 'Active' ORDER BY Name" \
  --target-org <alias> --json
```
Apply the same matching logic.

**If a product's PSM intent cannot be resolved** (no match in org): store `psm_name = null`,
mark status as `⚠ UNMAPPED PSM`, and surface at STEP 5.

**Store the resolved PSM name** (not the intent) in `psm_options` for each product.
If a product maps to multiple billing models (e.g., both annual and monthly variants),
and CPQ only has one billing frequency per product, use only the single resolved name.

#### 4c — Pricebook entries

For each pricebook entry from 2b:
```yaml
pricebook_entries:
  - pricebook: "<Pricebook2.Name>"
    price: <UnitPrice>
    currency: "<CurrencyIsoCode>"
```

Always place the entry where `Pricebook2.Name = "Standard Price Book"` first.
If the org is single-currency, the `currency` field is still included (defaults to `USD`).
If no pricebook entry exists for a product: set `price: 0.00` on Standard Price Book and
flag as `⚠ NO PRICEBOOK ENTRY`.

**Bundle price override:** Regardless of the CPQ pricebook price, set bundle pricebook
entries to `price: 0.00`. Note this in the confirmation table.

#### 4d — Bundle groups and components

For each bundle product, map its `SBQQ__ProductFeature__c` records to groups:

```yaml
groups:
  - name: "<SBQQ__ProductFeature__c.Name>"
    sequence: <SBQQ__Number__c>
    min_selections: <SBQQ__MinOptionCount__c>   # omit if null
    max_selections: <SBQQ__MaxOptionCount__c>   # omit if null
    components: [...]
```

Omit `min_selections` and `max_selections` if the CPQ field is null — Salesforce will use
its own defaults.

For each `SBQQ__ProductOption__c` belonging to that feature, add a component:

```yaml
components:
  - code: "<SBQQ__OptionalSKU__r.ProductCode>"
    sequence: <SBQQ__Number__c>
    required: <SBQQ__Required__c>
    default: <SBQQ__Selected__c>
    min_qty: <SBQQ__MinQuantity__c>              # omit entirely if null — do NOT default to 0 or 1
    max_qty: <SBQQ__MaxQuantity__c>              # omit entirely if null — do NOT default to 0 or 1
    is_quantity_editable: <SBQQ__QuantityEditable__c>   # false when CPQ field is false
    quantity_scale_method: "Proportional"        # when SBQQ__Type__c = "Component"
                                                 # omit when SBQQ__Type__c = "Accessory" or null
```

**`SBQQ__Type__c` mapping:**
| CPQ `SBQQ__Type__c` | YAML `quantity_scale_method` |
|---------------------|------------------------------|
| `"Component"` | `"Proportional"` |
| `"Accessory"` | omit (Salesforce default) |
| null | omit |

**`SBQQ__QuantityEditable__c` mapping:** Map directly to `is_quantity_editable`. Always
include this field — never omit it when it is present in the CPQ record.

**Min/Max Quantity:** Omit `min_qty` and `max_qty` entirely when the CPQ source fields are
null. Do NOT default to 0 or 1 — omitting them leaves the RCA field null, which is the
correct "no constraint" state. The `update_rca_catalog.py` script respects this: it only
writes the field to YAML if it is present in the input JSON.

**`SBQQ__Bundled__c` handling:** If `SBQQ__Bundled__c = true`, the component price is
included in the bundle price (not charged separately). Add a note in the confirmation
table: `bundled=true (component charged via bundle)`. This field (`price_includes_component`)
is not currently in the YAML schema — flag it for manual follow-up if the user's RCA
pricing procedure needs to handle it.

**Options without a feature (`SBQQ__Feature__c = null`):** Group them under a single
synthetic group named `"<Bundle Name> Options"` with sequence 1. Do not set min/max
selections.

#### 4e — Attributes

For each `SBQQ__ConfigurationAttribute__c` record in 2e:

```yaml
attributes:
  - name: "<SBQQ__Attribute__r.Name>"
    required: <SBQQ__Required__c>
```

**Omit `data_type`, `picklist_name`, and `picklist_values`** — CPQ configuration
attributes do not carry picklist definitions in their SOQL structure. The create script
will attempt to look up the attribute by name in the RCA org; if it does not exist,
the upload will fail.

Mark any product with attributes as `⚠ ATTRIBUTES: REVIEW NEEDED` in the confirmation
table. At STEP 5, for each such product ask the user:

> "`<Product Name>` has N CPQ configuration attributes: [list names]
>
> For each attribute, I need to know:
> - Does it already exist in your RCA org as an AttributeDefinition?
>   - **yes** — I'll leave out `data_type` so the script looks it up
>   - **no** — I need: data type (Text/Number/Boolean/Picklist), and if Picklist: picklist name and values
> - Is a ProductClassification needed? (required if any attributes are present)"

Collect answers and update the YAML entries accordingly. If the user says all attributes
already exist in the RCA org, leave `data_type` omitted for all of them and omit
`classification` (unless the user provides one).

**If attributes are present and a classification is required:** follow the classification
lookup logic from `/describe-rca-product` STEP 3:
- Check snapshot for `classifications` list, or run:
  ```bash
  sf data query \
    --query "SELECT Id, Name, Code FROM ProductClassification WHERE Status = 'Active' ORDER BY Name" \
    --target-org <alias> --json
  ```
- Present the list; ask the user which classification to assign. Store in `classification:`.

---

### STEP 5 — Show confirmation table

Before writing anything, display a full summary of all products to be converted.

**For one product (product or bundle):** show the detailed single-record summary format
from `/describe-rca-product` STEP 5.

**For multiple products:** show a compact summary table first, then list any flagged items:

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ CPQ → RCA CONVERSION SUMMARY                                                       │
│ Source org: <alias>  │  Target catalog: rca_session.yaml  │  N records             │
├────────────────────────────────────────────────────────────────────────────────────┤
│ # │ Code            │ Name                        │ Type    │ PSM              │ Status      │
├────────────────────────────────────────────────────────────────────────────────────┤
│ 1 │ EPL-001         │ Enterprise Platform License  │ Product │ Annual Termed    │ ✓ Ready     │
│ 2 │ BUNDLE-ES-001   │ Enterprise Suite             │ Bundle  │ Annual Termed    │ ✓ Ready     │
│ 3 │ PS-001          │ Professional Services        │ Product │ One-Time         │ ✓ Ready     │
│ 4 │ LEGACY-SUB-001  │ Legacy Subscription          │ Product │ —                │ ⚠ Unmapped PSM│
│ 5 │ CONF-PROD-001   │ Configurable Product         │ Product │ Annual Termed    │ ⚠ Attributes │
└────────────────────────────────────────────────────────────────────────────────────┘
```

**Status codes:**
| Symbol | Meaning |
|--------|---------|
| `✓ Ready` | All required fields mapped; no issues |
| `⚠ Unmapped PSM` | No org PSM matched the CPQ billing fields — PSM must be chosen |
| `⚠ Attributes` | Configuration attributes present — needs manual review (STEP 4e) |
| `⚠ No PBE` | No pricebook entry found in CPQ |
| `⚠ UOM` | Unit of measure defaulted to "Each" — confirm if different |
| `⚠ Already in org` | Code exists in org snapshot — create script will skip (idempotent) |
| `⚠ Already RCA` | `managed_by = "rca"` in snapshot — product already has RCA records; converting again will be a no-op but confirm intent |
| `⚠ Mid-migration` | `managed_by = "both"` in snapshot — product has both CPQ and RCA records; converting may create duplicates |

**For each `⚠` row**, resolve before writing:
- **Unmapped PSM**: list all active PSMs in the org (from snapshot or live query) and ask the user to choose one.
- **Attributes**: run the interview from STEP 4e for that product.
- **No PBE**: ask if a price should be set or if `0.00` is correct.

After resolving all flags, show a final "N ready, 0 issues" line.

Ask:
> "Ready to write N records to rca_session.yaml. **yes** to proceed, **edit [code]** to change a specific product, or **cancel** to stop."

---

### STEP 6 — Write to session catalog

For each product, construct the JSON payload (same format as `/describe-rca-product`
STEP 6 JSON Schema), then run:

```bash
python update_rca_catalog.py --json /tmp/rca_<code>.json --catalog <CATALOG_PATH>
```

Run one call per product/bundle. Show a progress line for each: `✓ EPL-001 — Enterprise Platform License`.

After all writes, confirm the total count:
> "Written N records to rca_session.yaml (M products, K bundles)."

---

### STEP 7 — Offer to upload immediately

Ask:
> "All records written. Upload to Salesforce now?
> - **yes** — dry-run first, then confirm
> - **dry-run** — preview only, no changes
> - **no** — stop here; upload later with `/create-rca-products`"

If yes or dry-run:
```bash
python create_rca_products.py --catalog <CATALOG_PATH> [--org <alias>] --dry-run
```

For live upload: always dry-run first, show output, then ask for final confirmation.

---

### STEP 8 — Offer to convert another

> "Done! Would you like to convert another product or bundle?"

If yes, restart from **STEP 1** — do NOT re-run STEP 0. Products from this session
accumulate in `rca_session.yaml` for a single upload.

**After the user declines (or after the last upload in this session):**
- If live upload ran: remind to run `/sync-rca-org`
- If `SNAPSHOT_LOADED = false`: suggest running `/sync-rca-org` to build a snapshot
- If snapshot is stale (> 7 days): mention it and suggest a refresh

---

## CPQ → RCA Field Mapping Reference

### Core Product Fields

| CPQ Field | RCA YAML Field | Notes |
|-----------|----------------|-------|
| `Product2.Name` | `name` | |
| `Product2.ProductCode` | `code` | |
| `Product2.Family` | `family` | |
| `Product2.Description` | `description` | |
| `Product2.IsActive` | `active` | |
| `PricebookEntry.UnitPrice` | `pricebook_entries[].price` | Bundle: always 0.00 |
| `PricebookEntry.Pricebook2.Name` | `pricebook_entries[].pricebook` | |
| `PricebookEntry.CurrencyIsoCode` | `pricebook_entries[].currency` | |

### PSM Mapping

| `SBQQ__SubscriptionType__c` | `SBQQ__BillingFrequency__c` | `SellingModelType` | `PricingTermUnit` |
|-----------------------------|-----------------------------|-------------------|------------------|
| null / blank / "One-time" | any | `OneTime` | — |
| "Subscription" | "Annual" | `TermDefined` | `Annual` |
| "Subscription" | "Monthly" | `TermDefined` | `Months` |
| "Subscription" | "Quarterly" | `TermDefined` | `Quarterly` |
| "Subscription" | "Semiannual" | `TermDefined` | `Semi-Annual` |
| "Evergreen" | "Annual" | `Evergreen` | `Annual` |
| "Evergreen" | "Monthly" | `Evergreen` | `Months` |
| "Evergreen" | "Quarterly" | `Evergreen` | `Quarterly` |

**Never hard-code a PSM name.** Always resolve against the org's actual `ProductSellingModel`
records via snapshot or live SOQL. The intent tuple `(type, unit)` is only an intermediate
step; only the resolved Name goes into the YAML.

### Bundle / Group Fields

| CPQ Field | RCA YAML Field | Notes |
|-----------|----------------|-------|
| `SBQQ__ProductFeature__c.Name` | `groups[].name` | |
| `SBQQ__ProductFeature__c.SBQQ__Number__c` | `groups[].sequence` | |
| `SBQQ__ProductFeature__c.SBQQ__MinOptionCount__c` | `groups[].min_selections` | Omit if null |
| `SBQQ__ProductFeature__c.SBQQ__MaxOptionCount__c` | `groups[].max_selections` | Omit if null |

### Bundle Component Fields

| CPQ Field | RCA YAML Field | Notes |
|-----------|----------------|-------|
| `SBQQ__ProductOption__c.SBQQ__OptionalSKU__r.ProductCode` | `components[].code` | |
| `SBQQ__ProductOption__c.SBQQ__Number__c` | `components[].sequence` | |
| `SBQQ__ProductOption__c.SBQQ__Required__c` | `components[].required` | |
| `SBQQ__ProductOption__c.SBQQ__Selected__c` | `components[].default` | |
| `SBQQ__ProductOption__c.SBQQ__MinQuantity__c` | `components[].min_qty` | Omit entirely if null — never default to 0 or 1 |
| `SBQQ__ProductOption__c.SBQQ__MaxQuantity__c` | `components[].max_qty` | Omit entirely if null — never default to 0 or 1 |
| `SBQQ__ProductOption__c.SBQQ__QuantityEditable__c` | `components[].is_quantity_editable` | Always include |
| `SBQQ__ProductOption__c.SBQQ__Type__c = "Component"` | `components[].quantity_scale_method: "Proportional"` | Omit if Accessory or null |
| `SBQQ__ProductOption__c.SBQQ__Bundled__c` | `price_includes_component` | Not in current YAML schema — flag for manual review |

### CPQ Object Field Name Corrections

| Object | Wrong field (fails SOQL) | Correct field |
|--------|--------------------------|---------------|
| `SBQQ__ProductFeature__c` | `SBQQ__Product__c` | `SBQQ__ConfiguredSKU__c` |
| `SBQQ__ProductOption__c` | `SBQQ__Product__c` | `SBQQ__ConfiguredSKU__c` |
| `SBQQ__ConfigurationAttribute__c` | `SBQQ__UserDefined__c` | field does not exist — omit |
| `PricebookEntry` (single-currency org) | `CurrencyIsoCode` | field absent — omit and default to USD |

---

## JSON Schema for STEP 6

Same schema as `/describe-rca-product`. Standalone product:

```json
{
  "type": "product",
  "product": {
    "code":        "EPL-001",
    "name":        "Enterprise Platform License",
    "description": "Full-access annual enterprise platform license",
    "family":      "Software",
    "active":      true,
    "uom":         "Each"
  },
  "psm_options": ["Annual Termed"],
  "pricebook_entries": [
    { "pricebook": "Standard Price Book", "price": 48000.00, "currency": "USD" }
  ],
  "classification": "Software"
}
```

Bundle:

```json
{
  "type": "bundle",
  "product": {
    "code":   "BUNDLE-ES-001",
    "name":   "Enterprise Suite",
    "family": "Software",
    "active": true
  },
  "psm_options": ["Annual Termed"],
  "pricebook_entries": [
    { "pricebook": "Standard Price Book", "price": 0.00, "currency": "USD" }
  ],
  "groups": [
    {
      "name": "Core Platform",
      "sequence": 1,
      "min_selections": 1,
      "max_selections": 1,
      "components": [
        { "code": "EPL-001", "required": true, "default": true, "sequence": 1 }
      ]
    },
    {
      "name": "Add-on Services",
      "sequence": 2,
      "components": [
        { "code": "PS-001", "required": false, "default": false, "sequence": 1, "min_qty": 1.0, "max_qty": 5.0 }
      ]
    }
  ]
}
```

Omit `min_selections`, `max_selections`, `min_qty`, `max_qty` when the CPQ source field is null.

---

## Notes

- **Bundle price is always 0.00.** CPQ bundle prices are ignored. If the user wants a
  non-zero bundle price, ask them to confirm — this is unusual in RCA.
- **UOM defaults to "Each".** CPQ does not store UOM in a standard field. Flag all UOM
  fields as needing review in the confirmation table unless the user specifies otherwise.
- **Attributes require a classification.** If any attributes are present, always collect
  a ProductClassification before writing. Without it, attributes are created in Salesforce
  but not assigned to the product.
- **Component products are included automatically.** When converting a bundle, all
  component products not already in the query set are fetched and added to `products[]`.
- **Existing org records are skipped.** The create script is idempotent — products whose
  code already exists in the org will be skipped. This is surfaced as `⚠ Already in org`
  in the confirmation table.
- **CPQ-only fields have no RCA equivalent.** CPQ fields like `SBQQ__ExcludeFromOpportunity__c`,
  `SBQQ__OptionLevel__c`, `SBQQ__OptionType__c` (Component/Accessory) do not map to RCA
  records. They are silently ignored. Note this if the user asks why a field is missing.
- **`SBQQ__Bundled__c` (`price_includes_component`)** is not in the current YAML schema.
  Surface it in the confirmation table as a manual follow-up item — the RCA pricing
  procedure must handle bundled component pricing separately.
- **`SBQQ__ProductOption__c.SBQQ__Type__c` (Component vs Accessory)** affects quantity
  scaling behavior in CPQ but has no direct RCA field equivalent. Flag products with
  Accessory-type options for manual review.
