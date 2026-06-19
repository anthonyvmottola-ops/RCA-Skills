# /find-product

Search the local org snapshot for products and bundles by name, code, family, catalog, or
any keyword. No API calls — works entirely from `.rca/org-snapshot.yaml`.

---

## Overview

Fast conversational lookup across all 150+ products and bundles in the snapshot.
Useful for: finding a product code before writing YAML, checking what price a SKU has,
confirming which PSMs are assigned, or browsing a catalog's contents.

Accepts a search term as an argument (`/find-product vanity`) or asks for one interactively.
Returns a compact table of matches; offer full detail on any single result.

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Load the org snapshot (silent)

**Determine the project root** — the directory containing `CLAUDE.md` or `.git/`,
whichever is found first walking up from the current working directory.
Set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

Load the snapshot:

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
print(f'LOADED|{meta.get(\"products_count\",0)}|{meta.get(\"bundles_count\",0)}|{meta.get(\"last_synced\",\"unknown\")}')
"
```

If `NO_SNAPSHOT`:
> "No org snapshot found at `.rca/org-snapshot.yaml`. Run `/sync-rca-org` first to build one."
Stop.

Store the full snapshot in memory — `products` list and `bundles` list — for searching.

---

### STEP 1 — Accept the search term

If the skill was invoked with an argument (e.g. `/find-product laptop`), use that as the
search term and skip asking.

Otherwise ask:
> "What are you looking for? You can search by product name, code, family, catalog, or any keyword."

---

### STEP 2 — Search the snapshot

Search term matching — case-insensitive, substring match against all of:
- `name`
- `code`
- `family`
- `description`
- `catalog`
- `category`

Search across both `products` and `bundles` lists. Tag each match with its type
(`product` or `bundle`).

**If zero matches:**
> "No products or bundles matched `<term>`. Try a shorter term, a product family name
> (e.g. Hardware, Software, Services), or a catalog name (e.g. Romet, Starline, Bathroom)."

**If exactly one match:** skip the results table and go straight to STEP 4 (full detail).

**If more than 20 matches:** suggest a more specific term:
> "Found <N> matches — that's a broad search. Here are the first 10:
> [table]
> Try a more specific term to narrow it down, or say **all** to see everything."

---

### STEP 3 — Display results table

Format as a compact markdown table. Columns:

| # | Type | Code | Name | Family | Catalog | Price (Std PB) | PSMs |
|---|------|------|------|--------|---------|----------------|------|

- **Type**: `product` or `bundle`
- **Price (Std PB)**: the UnitPrice from pricebook_entries where `pricebook` = "Standard Price Book".
  Show `—` if no entry, `$0.00` if explicitly zero.
- **PSMs**: comma-separated list of psm_options values. Truncate to first 2 + `…` if more than 2.
- **Catalog**: show `catalog > category` if both present; `catalog` alone if no category; `—` if neither.
- Truncate long names to 35 characters with `…`.

Example:
```
Found 4 matches for "laptop":

#  Type     Code      Name                               Family    Price (Std PB)  PSMs
1  product  LP001     Laptop                             Hardware  $1,049.00       One-Time
2  bundle   LB001     Laptop Basic Bundle                Hardware  $1,100.00       One-Time
3  bundle   LPB001    Laptop Pro Bundle                  Hardware  $1,150.00       One-Time
4  bundle   LCP001    Laptop Care Package                Service   $84.97/mo       Term Based…

Type a number to see full details, or search again with a new term.
```

For recurring PSMs (Evergreen/TermDefined), append `/mo`, `/yr`, `/qtr` to the price based
on the PSM's `pricing_term_unit` if it can be inferred from the PSM name or selling models
in the snapshot. Omit the suffix if ambiguous.

---

### STEP 4 — Full detail view

When the user picks a number (or there was exactly one match), display full details:

**For a product:**
```
┌──────────────────────────────────────────────────────────────┐
│ PRODUCT DETAIL                                               │
├──────────────────────────────────────────────────────────────┤
│ Code:        LP001                                           │
│ Name:        Laptop                                          │
│ SF ID:       01tg7000000cu9XAAQ                             │
│ Family:      Hardware    │  UOM: —     │  Active: ✓          │
│ Description: Battery- or AC-powered personal computer (PC)… │
│ Catalog:     —                                               │
├──────────────────────────────────────────────────────────────┤
│ Selling Model Options                                        │
│   • One-Time                                                 │
├──────────────────────────────────────────────────────────────┤
│ Pricebook Entries                                            │
│   • Standard Price Book   $1,049.00 USD                     │
│   • Nonprofit             $699.00 USD                        │
└──────────────────────────────────────────────────────────────┘
```

**For a bundle**, also show the group/component tree:
```
├──────────────────────────────────────────────────────────────┤
│ Bundle Groups                                                │
│   Group 1: Included Accessories  (required 2 of 2)          │
│     └── HDPH-001  Headphones            req  default        │
│     └── CHG-001   Charger               req  default        │
└──────────────────────────────────────────────────────────────┘
```

For each component code, look up the component's name and Standard Price Book price from
the snapshot's products/bundles lists and show them inline.

---

### STEP 5 — Offer next actions

After showing detail, offer:
> "What next?
> - **search** — search again
> - **clone** — clone this product (opens `/clone-product`)
> - **health** — run a health check on this product
> - **done** — nothing"

**clone**: Tell the user to run `/clone-product <code>` — do not attempt to invoke it inline.
**health**: Run the single-product health check (check for $0 price, missing PSM, missing catalog)
and report inline.

---

## Search Tips

If the user's term matches no results, suggest:
- Searching by family: `Software`, `Hardware`, `Services`, `Support`
- Searching by catalog: `Romet`, `Starline`, `Bathroom`, `Salesforce Services`, `Upright`
- Searching by PSM type: `one-time`, `monthly`, `annual`
- Using a shorter fragment of the name
