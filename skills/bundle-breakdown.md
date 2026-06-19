# /bundle-breakdown

Show a bundle's full component tree with per-component prices, a rolled-up total for
default-selected components, and any active price adjustment schedules that reference
the bundle. No API calls — works entirely from `.rca/org-snapshot.yaml`.

---

## Overview

Useful for quoting sanity checks, validating bundle discount math, and understanding
what a customer actually pays for a given bundle configuration.

Accepts a bundle code or name as an argument, or lists all bundles if none is given:
- `/bundle-breakdown` — list all bundles and ask which to inspect
- `/bundle-breakdown BUNDLE-ENT-001` — show that bundle directly
- `/bundle-breakdown enterprise` — fuzzy-match by name and show it

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Load the org snapshot

**Determine the project root** — the directory containing `CLAUDE.md` or `.git/`,
whichever is found first walking up from the current working directory.
Set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

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
print(f'LOADED|{meta.get(\"bundles_count\",0)}|{meta.get(\"last_synced\",\"unknown\")}')
"
```

If `NO_SNAPSHOT`:
> "No org snapshot found at `.rca/org-snapshot.yaml`. Run `/sync-rca-org` first."
Stop.

Store in memory:
- `bundles` list
- `products` list (for component lookups)
- `price_adjustment_schedules` list (for discount detection)

---

### STEP 1 — Select a bundle

**If an argument was passed:**
- Exact code match (case-insensitive): use it directly.
- No exact match: fuzzy-match against bundle `name` (substring, case-insensitive).
  - One match → use it.
  - Multiple matches → show a numbered list and ask.
  - No match → say so and offer the full bundle list.

**If no argument:**
Show a numbered list of all bundles from the snapshot:

```
Bundles in snapshot (<N> total):

#   Code              Name                             Catalog
1   BUNDLE-ENT-001    Enterprise Suite                 —
2   BUNDLE-SMB-001    SMB Starter Bundle               —
3   BUNDLE-SMS-001    Smart Meter Solution             Romet Product Catalog
4   BUNDLE-VAN-001    Vanity                           Bathroom
…

Which bundle would you like to break down? (enter a number or code)
```

---

### STEP 2 — Look up component prices

For each component in every group of the selected bundle, look up the component record
in the snapshot's `products` and `bundles` lists by matching `code`.

For each component, collect:
- `name` (from snapshot record, or `(not in snapshot)` if not found)
- `std_price`: UnitPrice from `pricebook_entries` where `pricebook` = "Standard Price Book"
  (null if no entry or not found)
- `active`: true/false (null if not found in snapshot)
- `psm_options`: list from the component record (empty if not found)

---

### STEP 3 — Identify active price adjustment schedules

Scan `price_adjustment_schedules` in the snapshot. Flag any schedule where:
- `is_active: true`
- `schedule_type` = `Bundle` (Bundle-based adjustments apply to this bundle's components)

Also flag any `Attribute` or `Volume` schedules that are active — note these apply to
components, not the bundle itself, but are worth showing for context.

For each active Bundle schedule, look up its name and note it.

---

### STEP 4 — Display the breakdown

**Bundle header:**
```
┌──────────────────────────────────────────────────────────────────────┐
│ BUNDLE: Enterprise Suite                                              │
│ Code:   BUNDLE-ENT-001   │  SF ID: 01tg700000M7N4rAAF               │
│ Family: Software          │  UOM: Each  │  Active: ✓                 │
│ Catalog: —                                                            │
│ PSMs:   Annual Termed                                                 │
│ Bundle Price (Std PB): $0.00  (pricing flows from components)        │
└──────────────────────────────────────────────────────────────────────┘
```

If the bundle has a non-zero Standard Price Book price, note it without the
"pricing flows from components" note.

**Component tree** — one section per group:

```
GROUP 1: Core Platform  (min 1 – max 1, required)
  ┌─────────────────────────────────────────────────────────────────┐
  │ # │ Code         │ Name                        │ Req │ Def │ Price (Std PB) │
  ├─────────────────────────────────────────────────────────────────┤
  │ 1 │ PLAT-ENT-001 │ Enterprise Platform License │ ✓   │ ✓   │ $48,000.00     │
  └─────────────────────────────────────────────────────────────────┘
  Default subtotal: $48,000.00

GROUP 2: Implementation Services  (min 1 – max 1, optional)
  ┌─────────────────────────────────────────────────────────────────┐
  │ # │ Code         │ Name                        │ Req │ Def │ Price (Std PB) │
  ├─────────────────────────────────────────────────────────────────┤
  │ 1 │ IMPL-STD-001 │ Standard Implementation     │     │ ✓   │ $5,000.00      │
  │ 2 │ IMPL-PREM-001│ Premium Implementation      │     │     │ $15,000.00     │
  └─────────────────────────────────────────────────────────────────┘
  Default subtotal: $5,000.00  (IMPL-STD-001 is default)

GROUP 3: Support Tier  (min 1 – max 1, optional)
  ┌─────────────────────────────────────────────────────────────────┐
  │ # │ Code         │ Name               │ Req │ Def │ Price (Std PB) │
  ├─────────────────────────────────────────────────────────────────┤
  │ 1 │ SUPP-GOLD-001│ Gold Support       │     │ ✓   │ $9,600.00      │
  │ 2 │ SUPP-PLAT-001│ Platinum Support   │     │     │ $19,200.00     │
  └─────────────────────────────────────────────────────────────────┘
  Default subtotal: $9,600.00  (SUPP-GOLD-001 is default)
```

**Rolled-up total:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEFAULT CONFIGURATION TOTAL:  $62,600.00 USD
  (sum of default-selected components at Standard Price Book prices)

MAX CONFIGURATION TOTAL:      $72,200.00 USD
  (if highest-priced option selected in each group)

MIN CONFIGURATION TOTAL:      $53,000.00 USD
  (if lowest-priced option selected in each group)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Total calculation rules:**
- Default total: sum of components where `default: true` (one per group if max_selections=1)
- If a group has min_selections=0 and no defaults, it contributes $0 to the default total
- If any component price is null (not in snapshot), note it: `* price unknown — not in snapshot`
- If the bundle itself has a non-zero price, add it to all totals and note it

**Active price adjustment schedules:**
```
Active Price Adjustments
  ⚡ Vanity Bundle Component Discount  (Bundle, Range)  — may reduce component prices
     Effective: 2026-06-19
     Note: Exact discount amounts require querying the org — run /describe-price-adjustment for details.
```

If no active bundle schedules: omit this section entirely.

**Components not found in snapshot:**
```
⚠  Component code 'URS-RIG-XX' is not in the snapshot — run /sync-rca-org to refresh,
   or verify the code exists in Setup.
```

---

### STEP 5 — Offer next actions

```
What next?
  - another  — break down a different bundle
  - health   — run a health check on this bundle
  - clone    — clone this bundle (run /clone-product BUNDLE-ENT-001)
  - done     — nothing
```

For **clone**: tell the user to run `/clone-product <code>` — do not invoke it inline.
For **health**: run the single-record checks from `/catalog-health` on this bundle inline
and report any issues.
