# /update-price

Update one or more PricebookEntry records in Salesforce by product code + pricebook name.
Resolves the record ID via SOQL, patches the UnitPrice, and confirms the before/after values.

---

## Overview

Fills the gap between "just need to fix this one price" and doing a full catalog YAML
re-run. Works for any active product or bundle. Supports updating multiple products in
a single session.

Accepts arguments or runs interactively:
- `/update-price` — interview mode
- `/update-price LP001 1199.00` — update Standard Price Book for LP001 to $1,199.00
- `/update-price LP001 "Nonprofit" 749.00` — update a specific pricebook

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Determine org and load snapshot context (silent)

**Determine the project root** and **org alias** (same logic as all other RCA skills):
1. Read `CLAUDE.md` → `## RCA Tools / Default org alias:`
2. Fall back to `myorg`

**Load the snapshot** for code-to-name resolution and price context:

```bash
python -c "
import yaml, sys, os

path = '<project_root>/.rca/org-snapshot.yaml'
if not os.path.isfile(path):
    print('NO_SNAPSHOT')
    sys.exit(0)

with open(path) as f:
    snap = yaml.safe_load(f)

# Build a quick lookup: code -> {name, pricebook_entries}
all_items = snap.get('products', []) + snap.get('bundles', [])
for item in all_items:
    code = item.get('code','')
    name = item.get('name','')
    entries = item.get('pricebook_entries', [])
    for e in entries:
        print(f'ENTRY|{code}|{name}|{e.get(\"pricebook\",\"\")}|{e.get(\"price\",\"\")}')
"
```

Store snapshot price data in memory for showing "current price" context during the interview.
If `NO_SNAPSHOT`, still proceed — just skip the "current price" context.

---

### STEP 1 — Collect update targets

**If arguments were passed** — parse them:
- 2 args (`<code> <price>`): code + new price, pricebook defaults to "Standard Price Book"
- 3 args (`<code> <pricebook> <price>`): all three explicit
- Store as a list of pending updates: `[{code, pricebook, new_price}]`

**If no arguments** — interview:

Ask:
> "Which product do you want to update? Give me the product code (e.g. LP001)."

If the user gives a name instead of a code, look it up in the snapshot and confirm:
> "Found: LP001 — Laptop. Is that the one?"

Then ask:
> "Which pricebook? (default: Standard Price Book)"

Show available pricebooks for that product from the snapshot if known. Accept partial
names (e.g. "nonprofit" → "Nonprofit").

Then ask:
> "What's the new price?"

If the snapshot has a current price for this product+pricebook combination, show it:
> "Current price: $1,049.00. New price?"

Accept price in any format: `$1199`, `1199.00`, `1,199`, etc. Strip symbols and parse to float.

Confirm the target before querying:
> "Update LP001 (Laptop) — Standard Price Book from $1,049.00 → $1,199.00. Proceed?"

---

### STEP 2 — Resolve the PricebookEntry ID

For each update target, run a SOQL query to find the PricebookEntry record:

```bash
sf data query \
  --query "SELECT Id, UnitPrice, Pricebook2.Name, Product2.Name FROM PricebookEntry WHERE Product2.ProductCode = '<code>' AND Pricebook2.Name = '<pricebook>' AND IsActive = true" \
  --target-org <alias> \
  --json
```

**If zero results:**
> "No active PricebookEntry found for `<code>` in `<pricebook>`. Check that:
> - The product code is correct (try /find-product to look it up)
> - The pricebook name matches exactly (e.g. 'Standard Price Book', not 'Standard')
> - The product is active in the org"
Skip this update and continue to the next.

**If multiple results:**
The org has duplicate PricebookEntry records for this product+pricebook combination.
Warn the user:
> "⚠ Found <N> PricebookEntry records for `<code>` in `<pricebook>`. This is a data anomaly.
> I'll update the first one (Id: `<id>`). Run /catalog-health to investigate the duplicates."
Use the first result.

**If one result:** proceed.

Note the current `UnitPrice` from the query result for the confirmation message (more
reliable than the snapshot value).

---

### STEP 3 — Apply the update

```bash
sf data update record \
  --sobject PricebookEntry \
  --record-id <Id> \
  --values "UnitPrice=<new_price>" \
  --target-org <alias>
```

**On success:** show:
```
✓  LP001  Laptop — Standard Price Book
   $1,049.00  →  $1,199.00
```

**On failure:** show the sf CLI error verbatim and stop for this record. Do not continue
to other updates until the user acknowledges.

---

### STEP 4 — Offer another update

After each successful update, ask:
> "Update another product? (yes / no)"

If yes, loop back to STEP 1 (interview mode — no arguments to parse).

---

### STEP 5 — Remind to sync

After the session ends (user says no to "update another"), remind:
> "💡 Run `/sync-rca-org` to refresh the local snapshot with the updated prices."

---

## Notes

- Only `UnitPrice` is updated. If you need to change `CurrencyIsoCode` or create a
  new PricebookEntry from scratch, use `/describe-rca-product` or edit the catalog YAML.
- This command cannot create a new PricebookEntry — only update an existing one. If the
  product has no entry in the target pricebook, go via `/describe-rca-product`.
- Bundle prices are typically $0.00 intentionally. If you're setting a bundle price,
  confirm with the user that this is intentional and not a configuration error.
- For bulk price updates across many products, consider editing `rca_catalog.yaml` and
  re-running `/create-rca-products --upsert` instead.
