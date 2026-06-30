# /describe-rca-product

Collect a product or bundle description in natural language, fill in any missing required
fields through a short interview, write the entry to the YAML catalog, then optionally
upload to Salesforce Revenue Cloud Advanced (ARM) immediately.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp describe-rca-product.md /path/to/your/sf-project/.claude/commands/describe-rca-product.md

---

## Overview

The conversational front-end to the RCA workflow. Instead of editing YAML by hand,
the user describes what they want, Claude Code extracts the structure, asks targeted
follow-up questions for anything missing, then writes to `rca_session.yaml` and
optionally fires the upload.

Each invocation starts a fresh session — `rca_session.yaml` is cleared at STEP 0 so
only products described in the current conversation are written and uploaded.
If the user describes multiple products in one conversation (via the Step 8 loop),
all of them accumulate in the same session file.

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

**Clear the session catalog** (run silently before asking the first question):

```bash
python -c "
import yaml, os
path = '<CATALOG_PATH>'
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
with open(path, 'w') as f:
    f.write('# RCA Session Catalog — cleared at session start\n')
    f.write('# Upload: python create_rca_products.py --catalog rca_session.yaml\n\n')
    yaml.dump({'products': [], 'bundles': []}, f, default_flow_style=False)
print('Session catalog cleared:', path)
"
```

Do not show this step to the user.

---

### STEP 0b — Load the org snapshot (silent)

Use the project root determined in STEP 0.
Set `SNAPSHOT_PATH` to `<PROJECT_ROOT>/.rca/org-snapshot.yaml`.

Check whether `SNAPSHOT_PATH` exists:

```bash
python -c "
import yaml, sys, os
from datetime import datetime, timezone, timedelta

path = '<SNAPSHOT_PATH>'
if not os.path.isfile(path):
    print('NO_SNAPSHOT')
    sys.exit(0)

with open(path) as f:
    snap = yaml.safe_load(f)

meta = snap.get('meta', {})
synced = meta.get('last_synced', '')
n_prod = meta.get('products_count', 0)
n_bund = meta.get('bundles_count', 0)
n_cat  = meta.get('catalogs_count', 0)

# Check age
age_days = None
if synced:
    try:
        dt = datetime.fromisoformat(synced.replace('Z', '+00:00'))
        age_days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        pass

# Extract catalog names and codes
catalogs = snap.get('catalogs', [])
cat_summary = '; '.join(
    f\"{c.get('name','')} ({c.get('code','?')}){': ' + ', '.join(cat.get('name','') for cat in c.get('categories',[]))[:60] if c.get('categories') else ''}\"
    for c in catalogs
) or '(none)'

# Extract selling models
selling_models = snap.get('selling_models', [])
n_psm = len(selling_models)
psm_summary = '; '.join(
    f\"{m.get('name','')} ({m.get('type','?')}/{m.get('pricing_term_unit') or 'n/a'})\"
    for m in selling_models
) or '(none)'

print(f'SNAPSHOT_FOUND|{n_prod}|{n_bund}|{n_cat}|{age_days}|{cat_summary}')
print(f'SELLING_MODELS|{n_psm}|{psm_summary}')
"
```

**If `NO_SNAPSHOT`:** Store `SNAPSHOT_LOADED = false`. Do not mention this to the user
yet — only surface it if catalog placement comes up in STEP 4b.

**If `SNAPSHOT_FOUND`:** Store `SNAPSHOT_LOADED = true` and the parsed snapshot data
in memory for use in STEP 3 (selling model resolution) and STEP 4b (catalog placement):
- catalog names, codes, and category trees
- product codes and bundle codes
- full `selling_models` list — each entry has `name`, `type`, `pricing_term`, `pricing_term_unit`

- If `age_days` > 7: note internally that the snapshot is stale (will remind user at STEP 8).
- Do not show anything to the user from this step.

---

### STEP 1 — Invite the description

Ask:
> "Describe the product or bundle you want to create. Tell me:
> - What it's called and what it does
> - Whether it's a standalone product or a bundle
> - How it's priced (one-time, monthly, annual?)
> - The list price
> - If a bundle: what products it includes, which are required vs optional, and any quantity rules
>
> Just describe it naturally — I'll ask follow-up questions for anything I need."

---

### STEP 2 — Extract fields from the description

Silently build a draft. Extract as many fields as possible:

**Product fields:**
- `code` — mentioned as a code/SKU/ID; generate one if not mentioned (see rules below)
- `name` — the product name (required)
- `description` — what it does
- `family` — Software, Services, Support, Hardware, etc.
- `uom` — Each, Hours, Users, Seats, etc.
- `active` — assume true unless told otherwise
- `classification` — ProductClassification Name or Code (e.g. "Software", "ECOM"); **required when attributes are present**, omit otherwise
- `attributes` — list of AttributeDefinition names the product should expose; omit if not mentioned

**PSM options** — map pricing language to a `(type, unit)` intent; resolve to an actual
PSM name in STEP 3 using the snapshot's `selling_models` list.

| User says | SellingModelType | PricingTermUnit |
|-----------|-----------------|-----------------|
| one-time / upfront / perpetual / implementation / professional services | `OneTime` | — |
| monthly / month-to-month (no end date) | `Evergreen` | `Months` |
| quarterly (no end date) | `Evergreen` | `Quarterly` |
| semi-annual (no end date) | `Evergreen` | `Semi-Annual` |
| annual / yearly (no end date, auto-renews) | `Evergreen` | `Annual` |
| monthly contract / billed monthly with term | `TermDefined` | `Months` |
| quarterly contract | `TermDefined` | `Quarterly` |
| annual contract / 1-year term / termed annual | `TermDefined` | `Annual` |
| 2-year / 3-year term | `TermDefined` | `Annual` (ask for PricingTerm value) |

- Collect one `(type, unit)` intent per billing model mentioned. Do not write a PSM name yet.
- If "annual" is ambiguous (could be evergreen or termed): defer and ask in STEP 3.
- If multiple billing models mentioned, collect all intents.

**Pricebook entries:**
- Always include `Standard Price Book`
- Extract `price` from any mention ("$48k", "$10,000/year", "48000")
- `currency` defaults to USD unless stated
- Bundle price defaults to 0.00

**Bundle groups** (if type = bundle):
- Group name — infer from context ("Core", "Add-ons", "Services", "Support Tier")
- `required: true` → "required", "mandatory", "must include"
- `required: false` → "optional", "add-on", "can choose"
- `default: true` → "default", "pre-selected", "included by default"
- Min/max quantities → only include `min_qty`/`max_qty` in the JSON if the user explicitly mentions them; omit otherwise and Salesforce will use its own defaults

**Never ask about fields you can already infer.**

---

### STEP 3 — Interview for missing required fields

Ask only for what's missing — batch all questions into a single message.

**Required fields:**

| Field | Required? | Default |
|-------|-----------|---------|
| `code` | Yes | Auto-generate from name initials |
| `name` | Yes | Must ask |
| `price` (non-bundles) | Yes | Must ask |
| PSM option (at least one) | Yes | Resolve from snapshot — see PSM Resolution below |
| Bundle group name | Yes (bundles) | Infer from context |
| Component `code` | Yes (bundles) | Must ask if not mentioned |
| `classification` | **Required if attributes present** | Check org for existing; must ask |
| `attributes` | No | Omit if not mentioned |

**PSM Resolution — resolving billing intent to a selling model name:**

For each `(type, unit)` intent collected in STEP 2, find the matching `ProductSellingModel`
and store its exact `name` in `psm_options`.

**If `SNAPSHOT_LOADED = true`:** search `selling_models` from the snapshot.

1. For `OneTime` intent: find any entry where `type = "OneTime"`.
   - One match → auto-select. Confirm: *"I'll use `[name]` for one-time billing."*
   - Multiple → list them and ask the user to pick.

2. For `Evergreen` or `TermDefined` intent: filter by both `type` and `pricing_term_unit`.
   - One match → auto-select. Confirm: *"I'll use `[name]` ([type], [unit])."*
   - Multiple → list and ask.
   - No match → fall through to "no match" handling below.

3. **Ambiguous "annual"** (type not clear from context): if both `Evergreen/Annual` and
   `TermDefined/Annual` exist in the snapshot, ask:
   > "Is this auto-renewing with no contract end date (evergreen), or a fixed-term
   > annual contract? Your org has both `[Evergreen name]` and `[TermDefined name]`."

**If `SNAPSHOT_LOADED = false`:** run a live query before matching:
```bash
sf data query --query "SELECT Id, Name, SellingModelType, PricingTermUnit FROM ProductSellingModel WHERE Status = 'Active' ORDER BY Name" --target-org <alias> --json
```
Apply the same matching logic against the results.

**If no match found** (after snapshot or live query):
Present the full list and ask:
> "No selling model matches [billing description]. Here are all active models in your org:
> [numbered list with type and unit]
> Pick one, or say **create** to define a new one."

If the user says **create**:
- Collect: Name, SellingModelType (`Evergreen` / `TermDefined` / `OneTime`),
  PricingTermUnit (`Months`, `Annual`, `Quarterly`, `Semi-Annual`), PricingTerm (default 1).
- Add a `new_selling_models` entry at the top level of the session YAML (before `products`/`bundles`).
- Use the chosen Name in `psm_options`.
- The create script's Step 1a will create the ProductSellingModel before linking options.

**Always store the resolved Name** (not the intent tuple) in `psm_options` in the YAML.

**Classification inference:**

**Rule: classification is REQUIRED whenever any attributes are present.** Attributes in Salesforce Revenue Cloud are linked to a product via its ProductClassification — without one, the attribute will not be assigned. Do not skip this step if attributes were collected.

- If the user explicitly names a classification (e.g. "classify as Hardware", "Software classification") — capture it directly.
- Otherwise, **look up existing classifications** before asking:

  **If `SNAPSHOT_LOADED = true` and snapshot contains a `classifications` key:**
  Present the list from the snapshot and suggest the closest name match.

  **Otherwise, run a live SOQL query:**
  ```bash
  sf data query --query "SELECT Id, Name, Code FROM ProductClassification WHERE Status = 'Active' ORDER BY Name" --target-org <alias> --json
  ```
  Parse the results and present the list. Try to auto-match by comparing classification names to the product family or name (e.g. product family "Hardware" → suggest a classification named "Hardware").

  **If the query fails or returns no results:** Ask the user to type the classification name manually.

- Ask (batch with other missing fields):
  > "Attributes require a ProductClassification. Here are the ones in your org: [list]. Which should this product use? (or type a new name to create one)"
- If **picking existing**: set `"classification"` to the exact Name from the org.
- If **creating new**: also collect a Code (auto-generate from name initials if blank) — the create script will create it.

**Attributes inference:**
- If the user mentions specific attributes (e.g. "it has a Color attribute", "expose the Edition field") — capture each as an entry in `attributes` with `name` = AttributeDefinition Name or Code
- Ask: "Does this product have any attributes that should be linked? List them by name."
- For each attribute, also ask:
  - Does this attribute already exist in the org, or do we need to create it?
  - If creating: what data type? (Text, Number, Boolean, **Picklist**)
  - If **Picklist**: what are the allowed values? (collect `value`, `code`, optional `display_value`, and which is the default)
  - Any default value, required flag, or display sequence?
- Picklist defaults: `picklist_data_type` = "Text" unless the user specifies otherwise

**Auto-generate `code` rules:**
- Take first letter of each significant word (skip: and, the, for, of, a, an)
- Uppercase, join without separator, append `-001`
- Bundles: prefix with `BUNDLE-`
- Examples: "Enterprise Platform License" → `EPL-001`, bundle "Enterprise Suite" → `BUNDLE-ES-001`
- Always confirm: "I'll use `EPL-001` as the code — does that work?"

**Sample interview message:**
```
Here's what I captured:

  Product: Enterprise Platform License (EPL-001)
  Type:    Standalone product
  Family:  Software
  Billing: [resolved PSM name(s) from org snapshot]
  Price:   $48,000 (Standard Price Book, USD)

A few things I still need:

1. Short description for the product record? (optional — press Enter to skip)
2. Unit of measure — Each, Seats, Users, or something else?
3. Any other pricebook entries? (e.g. Partner Price Book at a different price?)
```

---

### STEP 4 — For bundles: confirm the component structure

If any component code is unknown, ask:
> "Does [product name] already exist in Salesforce with a ProductCode?
> If yes, what's the code? If no, I'll add it to the catalog too."

If it doesn't exist, collect its info (loop back through Steps 2–3 for it) before continuing.

---

### STEP 4b — Catalog placement (optional)

After the standard interview, ask:

> "Should this product be visible in a product catalog so sales reps can browse to it in the Transaction Line Editor?
> - **yes** — I'll show you the available catalogs and categories
> - **no** — skip catalog placement for now"

If **yes**:

**Source for catalog/category data** — choose based on snapshot availability:

#### If `SNAPSHOT_LOADED = true`:

Present the catalogs from the snapshot directly — **no live SOQL query needed**:

> "Here are the catalogs in your org (from local snapshot):"
>
> 1. Main Product Catalog (MAIN) — categories: Software, Services, Support
> 2. … (list all from snapshot)
> n. Create new catalog

User picks one or says "create new".

For the chosen catalog, present its categories from the snapshot's nested tree
(flatten to "Parent > Child" paths for display). User picks one, types a path, or
says "create new".

- If **create new category/catalog**: collect Name and Code as normal, then include
  the `catalogs` block in the JSON payload.
- If **picking existing**: do NOT include the `catalogs` block — the create script
  will resolve them by name from the org.

#### If `SNAPSHOT_LOADED = false`:

Fall back to live org queries as before:

1. **Query existing catalogs** from the org:
   ```bash
   sf data query --query "SELECT Id, Name, Code FROM ProductCatalog WHERE Status = 'Active' ORDER BY Name" [--target-org <alias>] --json
   ```
   Present the list. User picks one or says "create new".

   - If **create new**: ask for Name and Code (Code auto-generates from Name if blank:
     uppercase initials, e.g. "Main Product Catalog" → `MPC`).

2. **Query existing categories** within the chosen catalog:
   ```bash
   sf data query --query "SELECT Id, Name, Code, ParentCategory.Name FROM ProductCategory WHERE CatalogId = '<catalog_id>' AND Status = 'Active' ORDER BY Name" [--target-org <alias>] --json
   ```
   Present the flat list. User picks one, types a path ("Software > Platform Licenses"),
   or says "create new".

   - If **create new**: ask for Name and Code; ask if it belongs under a parent category
     (pick from the list above or "none" for top-level).
   - If the category is new and the catalog is also new, include the full `catalogs`
     block in the JSON payload so `update_rca_catalog.py` can write the hierarchy.

   **If the org query fails** (network error, permission, no results): ask the user to
   type the catalog name and category name manually. Note: "Make sure these match exactly
   what's in your org, or the upload step will create them."

3. After this session, suggest running `/sync-rca-org` so future sessions can skip
   these live queries.

**Capture as:**
- `"catalog"`: the ProductCatalog Name
- `"category"`: the ProductCategory Name (or "Parent > Child" path if nested)
- `"catalogs"`: full hierarchy block — only include if a new catalog or new category
  is being created

---

### STEP 5 — Show a confirmation table

Before writing anything, display a full summary:

```
┌──────────────────────────────────────────────────────┐
│ PRODUCT SUMMARY — ready to add to rca_session.yaml   │
├──────────────────────────────────────────────────────┤
│ Code:           EPL-001                              │
│ Name:           Enterprise Platform License          │
│ Family:         Software  │  UOM: Each               │
│ Active:         true                                 │
│ Classification: Software                (if set)     │
│ Catalog:        Main Product Catalog    (if set)     │
│ Category:       Software > Platform     (if set)     │
├──────────────────────────────────────────────────────┤
│ PSM Options                                          │
│   • Annual Termed                                    │
│   • Monthly Evergreen                                │
├──────────────────────────────────────────────────────┤
│ Pricebook Entries                                    │
│   • Standard Price Book   $48,000.00 USD             │
│   • Partner Price Book    $43,200.00 USD             │
├──────────────────────────────────────────────────────┤
│ Attributes                          (if any)         │
│   • Edition  [Picklist]  default: Standard           │
│       values: Standard (default), Professional, Ent  │
│   • Color    [Text]                                  │
└──────────────────────────────────────────────────────┘
```

For bundles show the group/component tree:
```
│ Bundle Groups                                        │
│   Group 1: Core Platform  (required 1 of 1)         │
│     └── EPL-001  Enterprise Platform License  ✓ req │
│   Group 2: Add-ons  (optional 0–2)                  │
│     └── PS-001   Professional Services   optional   │
```

Ask:
> "Does everything look correct? **yes** to write to catalog, **edit** to change something, or **cancel**."

If **edit**, ask what to change and loop back.

---

### STEP 6 — Write to session catalog

Construct the JSON payload, save to `/tmp/rca_<code>.json`, then run:

```bash
python update_rca_catalog.py --json /tmp/rca_<code>.json --catalog <catalog_path>
```

`<catalog_path>` is always the `rca_session.yaml` path resolved in STEP 0.

Show the output. Confirm how many entries are now in the session catalog.

If `update_rca_catalog.py` is not in the current directory, look for it in the project root
or in `rca-product-creator/`.

---

### STEP 7 — Offer to upload immediately

Ask:
> "Added to session catalog. Upload to Salesforce now?
> - **yes** — dry-run first, then confirm
> - **dry-run** — preview only, no changes
> - **no** — stop here; upload later with `/create-rca-products`"

If yes or dry-run:
```bash
python create_rca_products.py --catalog <catalog_path> [--org <alias>] [--dry-run]
```

`<catalog_path>` is always the `rca_session.yaml` path resolved in STEP 0.

For live upload: always dry-run first, show output, then ask for final confirmation.

---

### STEP 8 — Offer another

> "Done! Would you like to describe another product or bundle?"

If yes, restart from **Step 1** — do NOT re-run Step 0. The session file is preserved
so all products described in this conversation accumulate together for upload.

**After the user declines (or after the last upload in this session):**

- If the user uploaded to Salesforce (live run, not dry-run only), remind:
  > "💡 Run `/sync-rca-org` to update the org snapshot with the products you just created."
- If `SNAPSHOT_LOADED = true` and the snapshot was stale (> 7 days old), also add:
  > "Your snapshot is over a week old — a sync will refresh catalog/category data for future sessions too."
- If `SNAPSHOT_LOADED = false`, add:
  > "You don't have an org snapshot yet. Run `/sync-rca-org` to build one — future sessions will use it for faster catalog lookups."

---

## JSON Schema for Step 6

```json
{
  "type": "product",
  "product": {
    "code":        "EPL-001",
    "name":        "Enterprise Platform License",
    "description": "Full-access annual enterprise platform license",
    "family":      "Software",
    "active":      true,
    "uom":         "Each",
    "sku":         ""
  },
  "psm_options": ["Annual Termed", "Monthly Evergreen"],
  "pricebook_entries": [
    { "pricebook": "Standard Price Book", "price": 48000.00, "currency": "USD" },
    { "pricebook": "Partner Price Book",  "price": 43200.00, "currency": "USD" }
  ],
  "classification": "Software",
  "catalog":  "Main Product Catalog",
  "category": "Software > Platform Licenses",
  "attributes": [
    {
      "name": "Edition",
      "data_type": "Picklist",
      "picklist_name": "Edition Values",
      "picklist_code": "EDITION_PL",
      "picklist_values": [
        { "value": "Standard",     "code": "STD", "sequence": 1, "is_default": true },
        { "value": "Professional", "code": "PRO", "sequence": 2 },
        { "value": "Enterprise",   "code": "ENT", "sequence": 3 }
      ],
      "default_value": "Standard",
      "required": false,
      "sequence": 1
    },
    {
      "name": "Color",
      "data_type": "Text"
    }
  ]
}
```

**Attribute rules:**
- Omit `"classification"` and `"attributes"` entirely if the user did not mention them — no empty strings or arrays.
- If an attribute already exists in the org, omit `data_type` and picklist fields — the script looks it up by name.
- If the attribute is new and `data_type` is `"Picklist"`, always include `picklist_name` and `picklist_values`.
- `picklist_data_type` defaults to `"Text"` on `AttributePicklist` — only include it if the user explicitly requests something else.
- `picklist_code` and `attr_code` are optional — omit unless the user provides them.

**Catalog placement rules:**
- Omit `"catalog"` and `"category"` entirely if the user skips catalog placement — no empty strings.
- `"category"` can be a simple name (`"Software"`) or a `"Parent > Child"` path when categories share names across branches.
- Include the `"catalogs"` block **only** when a new catalog or new category is being created. Omit it entirely when referencing existing org records.

When including `"catalogs"` for new records:
```json
{
  "catalogs": [
    {
      "name": "Main Product Catalog",
      "code": "MAIN",
      "categories": [
        {
          "name": "Software",
          "code": "SW",
          "categories": [
            { "name": "Platform Licenses", "code": "SW-PLAT" }
          ]
        }
      ]
    }
  ]
}
```
Only include the nodes that are new — existing parent categories don't need to be redefined.

Bundle — set `"type": "bundle"` and add `"groups"`:

```json
{
  "type": "bundle",
  "product": { "code": "BUNDLE-ENT-001", "name": "Enterprise Suite", "family": "Software", "active": true },
  "psm_options": ["Annual Termed"],
  "pricebook_entries": [
    { "pricebook": "Standard Price Book", "price": 0.00, "currency": "USD" }
  ],
  "classification": "Software",
  "groups": [
    {
      "name": "Core Platform", "code": "CORE",
      "min_selections": 1, "max_selections": 1, "sequence": 1,
      "components": [
        { "code": "EPL-001", "required": true, "default": true, "sequence": 1 }
      ]
    },
    {
      "name": "Add-on Services", "code": "ADDONS",
      "min_selections": 0, "max_selections": 2, "sequence": 2,
      "components": [
        { "code": "PS-001", "required": false, "default": false, "sequence": 1, "min_qty": 1, "max_qty": 5 }
      ]
    }
  ]
}
```

---

## PSM Matching Reference

Selling model resolution uses `SellingModelType` and `PricingTermUnit` from the org snapshot,
not hard-coded names. Common mappings:

| User says | SellingModelType | PricingTermUnit | Typical org name |
|-----------|-----------------|-----------------|-----------------|
| one-time / upfront / perpetual | `OneTime` | — | varies |
| monthly / month-to-month | `Evergreen` | `Months` | varies |
| annual (auto-renewing) | `Evergreen` | `Annual` | varies |
| annual contract / 1-year term | `TermDefined` | `Annual` | varies |
| quarterly | `Evergreen` or `TermDefined` | `Quarterly` | varies |
| 2-year / 3-year term | `TermDefined` | `Annual` (PricingTerm=2 or 3) | ask org |

**Never hard-code a PSM name.** Always resolve against the org's actual `ProductSellingModel`
records via the snapshot or a live SOQL query. The name that ends up in `psm_options` must
exactly match what's in the org.

**`new_selling_models` YAML section** — only include when the user requests creation of a
new selling model that doesn't exist in the org:
```yaml
new_selling_models:
  - name: "Annual Evergreen"
    type: "Evergreen"
    pricing_term: 1
    pricing_term_unit: "Annual"
```
This section is processed by the create script's Step 1a before PSM options are linked.

---

## Notes

- Never write to the catalog without the user confirming the summary in Step 5.
- Always use `rca_session.yaml` — never `rca_catalog.yaml`. The session file is cleared at STEP 0 and only contains products from the current conversation.
- Bundle price is 0.00 by default — pricing flows from components. Mention this if the user tries to set a non-zero bundle price.
- `update_rca_catalog.py` is idempotent — it skips entries that already exist unless `--overwrite` is passed.
- Component products not in the catalog will be skipped at upload time unless they already exist in the org.
- The permanent archive (`rca_catalog.yaml`) is never touched by this command. It can be updated manually or by a separate merge step if a full history is needed.
- **Attributes require a classification.** If a product has attributes but no classification, the attributes will be created in Salesforce but not assigned to the product. Always collect a classification when attributes are present — never skip it.
