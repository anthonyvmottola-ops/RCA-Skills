# /clone-product

Clone an existing product or bundle from the org snapshot into a new YAML entry,
change whatever fields you need, then optionally upload the clone to Salesforce.

Works for both standalone products and bundles (including their full group/component trees).

---

## Overview

The fastest way to create a product that closely resembles an existing one. Instead of
describing everything from scratch, you start from a known record, change what's different,
confirm, and push.

Accepts a product or bundle code as an argument:
- `/clone-product LP001` — clone Laptop directly
- `/clone-product BUNDLE-ENT-001` — clone Enterprise Suite bundle
- `/clone-product` — search for the source interactively

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Start a fresh session (silent)

**Locate the scripts directory** (same priority order as all RCA skills):
1. Read `CLAUDE.md` → `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory

Set `SCRIPTS_DIR` and `CATALOG_PATH` to `<SCRIPTS_DIR>/rca_session.yaml`.

Clear the session catalog silently:

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

---

### STEP 0b — Load the org snapshot (silent)

**Determine the project root** and set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

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
print(f'SNAPSHOT_FOUND|{meta.get(\"products_count\",0)}|{meta.get(\"bundles_count\",0)}|{meta.get(\"last_synced\",\"unknown\")}')

selling_models = snap.get('selling_models', [])
psm_summary = '; '.join(
    f\"{m.get('name','')}|{m.get('type','?')}|{m.get('pricing_term_unit') or 'n/a'}\"
    for m in selling_models
) or ''
print(f'SELLING_MODELS|{psm_summary}')
"
```

If `NO_SNAPSHOT`:
> "No org snapshot found. Run `/sync-rca-org` first — the snapshot is required to find
> the source product."
Stop.

Store all snapshot data in memory: `products`, `bundles`, `selling_models`.

---

### STEP 1 — Select the source record

**If a code was passed as argument:**
- Search `products` and `bundles` lists for exact `code` match (case-insensitive).
- If found: proceed. If not found: fuzzy-match by name (substring) and ask to confirm.

**If no argument:**
Ask:
> "Which product or bundle do you want to clone? Give me the code or part of the name."

Search the snapshot. If multiple matches, show a numbered list. If one match, confirm it.

Detect type: if the record has `groups` key → it's a **bundle**. Otherwise → **product**.

Store `SOURCE_TYPE` (`product` or `bundle`) and the full source record.

---

### STEP 2 — Show the source record

Display the full current state so the user knows what they're starting from.

**For a product:**
```
Cloning from: LP001 — Laptop (product)
─────────────────────────────────────────────────────────────
  Family:      Hardware
  UOM:         —
  Active:      ✓
  Description: Battery- or AC-powered personal computer (PC)…
  Catalog:     —
  PSMs:        One-Time
  Prices:      Standard Price Book → $1,049.00
               Nonprofit → $699.00
─────────────────────────────────────────────────────────────
What would you like to change in the clone?
```

**For a bundle:**
```
Cloning from: BUNDLE-ENT-001 — Enterprise Suite (bundle)
─────────────────────────────────────────────────────────────
  Family:   Software  │  UOM: Each  │  Active: ✓
  PSMs:     Annual Termed
  Price:    Standard Price Book → $0.00
  Groups:
    1. Core Platform  (req 1–1)
       └── PLAT-ENT-001  Enterprise Platform License  [req, default]
    2. Implementation Services  (opt 1–1)
       └── IMPL-STD-001   Standard Implementation     [opt, default]
       └── IMPL-PREM-001  Premium Implementation      [opt]
    3. Support Tier  (opt 1–1)
       └── SUPP-GOLD-001  Gold Support     [opt, default]
       └── SUPP-PLAT-001  Platinum Support [opt]
─────────────────────────────────────────────────────────────
What would you like to change in the clone?
```

---

### STEP 3 — Interview: what to change

Present the change menu. The user can name specific fields or say "just the name and code":

```
You can change any of these fields (say what you want, or 'all' to go field by field):
  • Name and code       • Description
  • Family / UOM        • Prices
  • PSM options         • Catalog / category
  [Bundle only]:
  • Groups (add/remove a group, rename a group)
  • Components (add/remove a component, change required/default/qty)
```

**Rules:**
- Code is ALWAYS required to change — you cannot clone a product with the same code
  as the source. If the user doesn't specify a new code, auto-suggest one:
  append `-CLONE` to the source code (e.g. `LP001-CLONE`) and confirm.
- Name is strongly recommended to change. If the user keeps the same name, warn:
  > "⚠  Same name as the source — both records will appear identical in the org. Continue?"
- All other fields default to copying the source value if not changed.

Batch all questions into as few messages as possible. For simple clones ("just change the
name, code, and price"), collect all three in one shot.

---

### STEP 3b — PSM resolution

If the user changes PSM options, resolve them using the same logic as `/describe-rca-product`:

For each billing intent (e.g. "annual contract", "monthly"):
1. Map to `(SellingModelType, PricingTermUnit)` using the intent table below.
2. Search `selling_models` from the snapshot.
3. Auto-select if one match; list options if multiple; ask if none.

| User says | SellingModelType | PricingTermUnit |
|-----------|-----------------|-----------------|
| one-time / upfront / perpetual | `OneTime` | — |
| monthly / month-to-month (no end date) | `Evergreen` | `Months` |
| quarterly (no end date) | `Evergreen` | `Quarterly` |
| annual / yearly (no end date) | `Evergreen` | `Annual` |
| monthly contract / billed monthly with term | `TermDefined` | `Months` |
| annual contract / 1-year term | `TermDefined` | `Annual` |

**Never hard-code a PSM name.** Always resolve against the org snapshot.

---

### STEP 3c — Bundle group/component modifications (bundles only)

After header changes, present the current group tree and ask:

> "Groups and components — keep as-is, or modify?"

Options:
- **keep** — copy the entire group/component structure unchanged (still uses the same component codes).
- **modify** — enter the group editor.

**Group editor:**
Present numbered groups. For each change the user requests:

- **Rename group**: ask for the new group name. Group code auto-generates from new name.
- **Add group**: ask for name, min_selections, max_selections, sequence. Then ask for components (code, required, default, min_qty, max_qty).
- **Remove group**: confirm, then drop it from the clone.
- **Add component to group**: ask for product code. Check it exists in the snapshot — if not, warn: *"Code `X` not found in snapshot. It may still exist in the org — proceed anyway?"* Ask required (y/n), default (y/n), min_qty, max_qty (omit qty if not specified).
- **Remove component**: confirm and drop it.
- **Change required/default**: set the flag on the named component.
- **Change qty rules**: set min_qty / max_qty on the named component.

After modifications, show the updated tree for confirmation before proceeding.

**Component codes that don't exist in the snapshot:**
If a user-specified component code isn't in the snapshot, warn and offer two paths:
1. **Proceed anyway** — the code might exist in the org but wasn't captured; upload will succeed if it does.
2. **Add it first** — tell the user to run `/describe-rca-product` to create it, then come back.

---

### STEP 4 — Catalog placement

After all field changes, ask:
> "Should this clone be visible in a product catalog?
> - **yes** — choose a catalog and category
> - **same** — use the same catalog/category as the source (if it had one)
> - **no** — skip catalog placement"

If **same** and the source had no catalog: treat as **no**.

If **yes** or **same**:
Present catalogs from the snapshot (same logic as `/describe-rca-product` STEP 4b).
User picks catalog and category. If creating a new one, collect Name and Code.

---

### STEP 5 — Confirmation table

Display the full clone before writing anything. Show changed fields highlighted with `*`.

**Product:**
```
┌─────────────────────────────────────────────────────────────┐
│ CLONE SUMMARY — ready to add to rca_session.yaml            │
├─────────────────────────────────────────────────────────────┤
│ Code:        LP001-PRO         * (was: LP001)               │
│ Name:        Laptop Pro        * (was: Laptop)              │
│ SF ID:       (new record)                                   │
│ Family:      Hardware          (unchanged)                  │
│ UOM:         —                 (unchanged)                  │
│ Active:      true              (unchanged)                  │
│ Catalog:     —                 (unchanged)                  │
├─────────────────────────────────────────────────────────────┤
│ PSM Options                                                 │
│   • One-Time                   (unchanged)                  │
├─────────────────────────────────────────────────────────────┤
│ Pricebook Entries                                           │
│   • Standard Price Book  $1,399.00  * (was: $1,049.00)     │
│   • Nonprofit            $699.00    (unchanged)             │
└─────────────────────────────────────────────────────────────┘
```

**Bundle** — also show the group tree. Mark modified groups/components with `*`.

Ask:
> "Does everything look correct? **yes** to write to catalog, **edit** to change something, or **cancel**."

If **edit**, ask what to change and loop back to STEP 3.

---

### STEP 6 — Write to session catalog

Build the JSON payload using the same format as `/describe-rca-product` STEP 6.

Set `"type"` to `"product"` or `"bundle"` based on `SOURCE_TYPE`.

For bundles, include the full `"groups"` array with all components (modified or unchanged).

Save to `/tmp/rca_clone_<new_code>.json`, then run:

```bash
python <SCRIPTS_DIR>/update_rca_catalog.py \
  --json /tmp/rca_clone_<new_code>.json \
  --catalog <CATALOG_PATH>
```

Show the output. Confirm how many entries are now in the session catalog.

---

### STEP 7 — Offer to upload

Ask:
> "Added to session catalog. Upload to Salesforce now?
> - **yes** — dry-run first, then confirm
> - **dry-run** — preview only, no changes
> - **no** — stop here; upload later with `/create-rca-products`"

```bash
python <SCRIPTS_DIR>/create_rca_products.py \
  --catalog <CATALOG_PATH> \
  --org <alias> \
  [--dry-run]
```

Always dry-run first on **yes**. Show output. Ask for final confirmation before live run.

---

### STEP 8 — Offer another clone

> "Done! Would you like to clone another product or bundle?"

If yes, restart from **STEP 1** — do NOT re-run STEP 0. The session file accumulates
all clones for a single upload batch.

**After the last clone:**
- If uploaded live: remind > "💡 Run `/sync-rca-org` to update the snapshot."
- If not uploaded: remind > "Run `/create-rca-products` when you're ready to push."

---

## JSON Payload Format

Follows the same schema as `/describe-rca-product` STEP 6. Key points for clones:

- `"type"`: `"product"` or `"bundle"`
- All source fields are copied into the payload; only changed fields differ.
- Omit `sf_id` — the clone is always a new record.
- Omit `"classification"` and `"attributes"` unless the user explicitly adds them.
- Omit `"catalog"` and `"category"` if skipped.
- For bundles: `"groups"` array is always included (never omit even if unchanged — the
  create script needs it to build the group/component records).

**Bundle group code auto-generation** (when renaming a group):
Take first letter of each significant word, uppercase, join without separator.
Example: "North America Platform" → `NAP`.

---

## Notes

- The session catalog is cleared at STEP 0 — so each `/clone-product` session starts fresh.
  If you need to clone multiple products in one upload batch, stay in the same session
  (say yes to "clone another?" at STEP 8).
- You cannot clone into the same code as the source — Salesforce will reject a duplicate
  ProductCode on create.
- Component products referenced in a cloned bundle are NOT cloned — they remain the same
  org records. Only the bundle header and group/component structure is new.
- Cloning a bundle does NOT clone its price adjustment schedules. If the source bundle has
  active bundle-based adjustments, those schedules reference the source bundle's Id, not
  the clone's. Set up new schedules via `/describe-price-adjustment` after uploading.
