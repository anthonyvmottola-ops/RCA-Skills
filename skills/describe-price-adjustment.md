# /describe-price-adjustment

Collect a price adjustment description in natural language, fill in any missing required
fields through a short interview, write the entry to the YAML adjustments file, then
optionally upload to Salesforce Revenue Cloud Advanced (ARM) immediately.

Supports all three adjustment types: **Volume/Tier**, **Attribute-Based**, **Bundle-Based**.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp describe-price-adjustment.md /path/to/your/sf-project/.claude/commands/describe-price-adjustment.md

---

## Overview

Conversational front-end to `create_price_adjustments.py`. The user describes a pricing
rule in natural language. Claude extracts the structure, asks targeted follow-up questions
for anything missing, writes to `rca_adj_session.yaml`, and optionally runs the upload.

Multiple adjustments described in one conversation accumulate in the same session file.
The session file is cleared at STEP 0 so each `/describe-price-adjustment` conversation
starts fresh.

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Start a fresh session

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the current project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory
4. `rca-product-creator/` subdirectory of current directory

Set `SCRIPTS_DIR` to the first directory that contains `update_rca_adjustments.py`.
Set `SESSION_PATH` to `<SCRIPTS_DIR>/rca_adj_session.yaml`.

**Clear the session file** (run silently before asking the first question):

```bash
python -c "
import yaml, os
path = '<SESSION_PATH>'
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
with open(path, 'w') as f:
    f.write('# RCA Price Adjustments Session — cleared at session start\n')
    f.write('# Upload: python create_price_adjustments.py --catalog rca_adj_session.yaml\n\n')
    yaml.dump({'price_adjustment_schedules': []}, f, default_flow_style=False)
print('Session file cleared:', path)
"
```

Do not show this step to the user.

---

### STEP 0b — Load org snapshot (silent)

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
products = snap.get('products', []) + snap.get('bundles', [])
psms = snap.get('selling_models', [])
schedules = snap.get('price_adjustment_schedules', [])

prod_summary = '; '.join(
    f\"{p.get('code','')} ({p.get('name','')})\"
    for p in products[:10]
)
psm_summary = '; '.join(
    f\"{m.get('name','')} ({m.get('type','?')}/{m.get('pricing_term_unit') or 'n/a'})\"
    for m in psms
)
sched_summary = '; '.join(
    f\"{s.get('schedule_type','')}:{s.get('name','')}\"
    for s in schedules
)

print(f'SNAPSHOT_FOUND|{len(products)}|{len(psms)}|{len(schedules)}')
print(f'PRODUCTS|{prod_summary}')
print(f'PSMS|{psm_summary}')
print(f'SCHEDULES|{sched_summary}')
"
```

**If `NO_SNAPSHOT`:** Store `SNAPSHOT_LOADED = false`. The interview will ask for product
codes and PSM names directly rather than presenting a list. Suggest running `/sync-rca-org`
at the end of the session.

**If `SNAPSHOT_FOUND`:** Store `SNAPSHOT_LOADED = true` and parse into memory:
- `PRODUCTS_MAP`: code → name, for display during interview
- `PSMS_LIST`: list of `{name, type, pricing_term_unit}` dicts
- `SCHEDULES_LIST`: list of `{schedule_type, name, is_active}` dicts — used to determine
  whether a Standard schedule already exists for a given type

Do not show anything to the user from this step.

---

### STEP 1 — Invite the description

Ask:

> "Describe the price adjustment you want to create. For example:
> - **Volume**: "Give 5% off when buying 2–4 Vanity bundles, 10% off for 5+"
> - **Attribute**: "Add a 10% upcharge when Countertop Material is Quartz"
> - **Bundle**: "Discount the Quartz component 15% when it's inside the Vanity bundle"
>
> Just describe it — I'll ask follow-up questions for anything I need."

---

### STEP 2 — Determine the schedule type

Identify the type from the description:

| User says | Type |
|-----------|------|
| volume, quantity, buy X get Y%, tier, break | `Volume` |
| attribute, option, feature, material, when [attribute] is [value] | `Attribute` |
| bundle, component, when inside, when part of | `Bundle` |

If unclear, ask:
> "What drives this discount — quantity purchased, a product attribute value, or the
> product appearing inside a specific bundle?"

---

### STEP 3 — Determine which schedule to use

**Check `SCHEDULES_LIST` for existing schedules of this type:**

- If the org has exactly one schedule of the identified type → default to using it
  (type-based matching, no `name` needed in YAML).
- If the org has multiple → present the list and ask:
  > "Your org has multiple [Type] schedules:
  >   1. Standard [Type] Based Adjustment (inactive — org default)
  >   2. [Name] (active)
  >   Which should this adjustment go into? Or type 'new' to create a named schedule."
- If the org has none → tell the user a new named schedule will be created; ask for a name.
- If `SNAPSHOT_LOADED = false` → ask:
  > "Should this go into an existing price adjustment schedule (if so, what's its name?)
  > or should I create a new one?"

**Decision recorded as:**
- `USE_TYPE_MATCH = true` — no name in YAML; `create_price_adjustments.py` will resolve
  by type from the snapshot at upload time.
- `USE_NAMED = "Schedule Name"` — explicit `name` field written to YAML.
- `CREATE_NEW = "New Schedule Name"` — new schedule will be created on upload.

---

### STEP 4 — Collect type-specific fields

Batch all questions into a single message. Only ask for what is missing.

---

#### Volume schedule fields

**Required:**
- Quantity tiers: lower_bound, upper_bound (null = open-ended), tier_type, tier_value
- Product: ask "Which product does this discount apply to? (ProductCode, or 'all products
  on the schedule')"
- PSM: "Which selling model? (e.g. One-Time, Annual)"

**Pricebook** (if creating a new schedule): "Which pricebook? (default: Standard Price Book)"

**Auto-detect from description:**
- "5% off for 2–4" → lower_bound: 2, upper_bound: 4, tier_type: AdjustmentPercentage, tier_value: 5
- "10% off for 5+" → lower_bound: 5, upper_bound: null, tier_type: AdjustmentPercentage, tier_value: 10
- "$50 off" → AdjustmentAmount
- "fixed price of $500" → OverrideAmount

**Sample interview (Volume):**
```
Here's what I captured:

  Type:    Volume discount
  Product: BUNDLE-VAN-001 (Vanity Bundle)
  PSM:     One-Time

  Tiers:
    Qty 2–4   →  5% off
    Qty 5+    →  10% off

A few things I still need:

1. Which pricebook? (Standard Price Book / other)
2. Should I use the "Standard Volume Based Adjustment" schedule, or create a
   named schedule for this?
```

---

#### Attribute schedule fields

**Required:**
- Attribute name: "Which attribute triggers the price change?" (AttributeDefinition Name)
- Attribute value: "What value triggers it?" (the condition value)
- Operator: default to `equals`; only ask if the description implies a range or exclusion
- Product the attribute belongs to: "Which product has this attribute?" (ProductCode)
- Adjustment: type (Percentage/Amount/Override), value, and which product gets adjusted

**Auto-detect:**
- "when X is Y" → operator: equals, attribute: X, value: Y
- "when X is not Y" → operator: notequals
- "when quantity > 10" → operator: greaterthan, value: 10

**Rule name auto-suggestion:** Generate from attribute + value, e.g. "Quartz Upcharge",
then confirm: "I'll name the rule 'Quartz Upcharge' — does that work?"

**Multiple conditions:** If the description implies AND logic, collect each condition
separately into the `conditions` list of the same rule.

**Check `IsPriceImpacting` prerequisite:** After collecting the attribute name, remind:
> "Note: The attribute '[name]' must have 'Is Price Impacting' enabled on its
> ProductClassificationAttr record before this adjustment can be uploaded. You can
> check/set this in Setup → Product Classifications → [classification] → Attributes."

**Sample interview (Attribute):**
```
Here's what I captured:

  Type:      Attribute-Based
  Rule:      "Vanity Quartz Upcharge"
  Condition: Countertop Material = Quartz  (on BUNDLE-VAN-001)
  Adjustment: +10% on BUNDLE-VAN-001 / One-Time

A few things I still need:

1. Should I use the "Standard Attribute Based Adjustment" schedule, or create
   a named schedule?
2. Reminder: "Countertop Material" must have Is Price Impacting = true on
   the Vanity classification before uploading. Is that already set?
```

---

#### Bundle schedule fields

**Required:**
- Component product: "Which component product gets the discount?" (ProductCode + PSM)
- Bundle parent: "Which bundle triggers the discount?" (ProductCode + PSM)
- Adjustment: type and value

**Single-level bundles only.** `RootBundleId` is automatically set equal to `ParentProductId`
by the upload script — do not ask about it.

**Sample interview (Bundle):**
```
Here's what I captured:

  Type:      Bundle-Based
  Component: QTZ-001 (Quartz) / One-Time  →  15% off
  Trigger:   When inside BUNDLE-VAN-001 (Vanity Bundle) / One-Time

1. Should I use the "Standard Bundle Based Adjustment" schedule, or create
   a named one?
2. Which pricebook? (Standard Price Book / other)
```

---

### STEP 5 — PSM resolution

For each PSM name collected, resolve against the org:

**If `SNAPSHOT_LOADED = true`:** search `PSMS_LIST`:
1. `OneTime` intent (one-time, upfront, perpetual) → find entry where `type = "OneTime"`
2. `Evergreen`/`TermDefined` + unit → match both `type` and `pricing_term_unit`
3. Ambiguous → list options and ask

**If `SNAPSHOT_LOADED = false`:** ask for exact PSM name. Note: "Use the exact name as it
appears in Setup → Revenue → Selling Models."

Always confirm the resolved PSM name:
> "I'll use 'One-Time' for one-time billing."

---

### STEP 6 — Show confirmation table

Before writing anything, display a full summary.

**Volume example:**
```
┌──────────────────────────────────────────────────────┐
│ PRICE ADJUSTMENT SUMMARY — ready to write to YAML    │
├──────────────────────────────────────────────────────┤
│ Type:      Volume                                    │
│ Schedule:  Standard Volume Based Adjustment          │
│            (type-matched — existing org schedule)    │
├──────────────────────────────────────────────────────┤
│ Tiers                                                │
│   Qty 2–4    5%   BUNDLE-VAN-001 / One-Time         │
│   Qty 5+    10%   BUNDLE-VAN-001 / One-Time         │
└──────────────────────────────────────────────────────┘
```

**Attribute example:**
```
┌──────────────────────────────────────────────────────┐
│ PRICE ADJUSTMENT SUMMARY — ready to write to YAML    │
├──────────────────────────────────────────────────────┤
│ Type:      Attribute                                 │
│ Schedule:  Standard Attribute Based Adjustment       │
│            (type-matched — existing org schedule)    │
├──────────────────────────────────────────────────────┤
│ Rule: "Vanity Quartz Upcharge"  [Pricing]            │
│   Condition: Countertop Material = "Quartz"          │
│              on BUNDLE-VAN-001                       │
│   Adjustment: +10% on BUNDLE-VAN-001 / One-Time     │
│                                                      │
│ ⚠ Prerequisite: Countertop Material must have       │
│   Is Price Impacting = true on Vanity classification │
└──────────────────────────────────────────────────────┘
```

**Bundle example:**
```
┌──────────────────────────────────────────────────────┐
│ PRICE ADJUSTMENT SUMMARY — ready to write to YAML    │
├──────────────────────────────────────────────────────┤
│ Type:      Bundle                                    │
│ Schedule:  Standard Bundle Based Adjustment          │
│            (type-matched — existing org schedule)    │
├──────────────────────────────────────────────────────┤
│ Component:  QTZ-001 / One-Time  →  15% off          │
│ Trigger:    Inside BUNDLE-VAN-001 / One-Time         │
└──────────────────────────────────────────────────────┘
```

Ask: **"Does everything look correct? yes / edit / cancel"**

If **edit**, ask what to change and loop back to the relevant step.

---

### STEP 7 — Write to session file

Construct the JSON payload and save to `/tmp/rca_adj_<type>.json`, then run:

```bash
python <SCRIPTS_DIR>/update_rca_adjustments.py \
  --json /tmp/rca_adj_<type>.json \
  --catalog <SESSION_PATH>
```

Show the output. Confirm how many schedule entries are now in the session file.

---

### STEP 8 — Offer to upload immediately

Ask:
> "Added to session file. Upload to Salesforce now?
> - **yes** — dry-run first, then confirm
> - **dry-run** — preview only, no changes
> - **no** — stop here; upload later with `create_price_adjustments.py`"

If yes or dry-run, run from the **project root** (so the snapshot is found):

```bash
cd <project_root>
python <SCRIPTS_DIR>/create_price_adjustments.py \
  --catalog <SESSION_PATH> \
  [--org <alias>] \
  [--dry-run]
```

For live upload: always dry-run first, show output, then ask for final confirmation.

After a successful live upload, remind:
> "Done! Run `/sync-rca-org` to refresh the org snapshot so these schedules appear
> in future type-based matching."

---

### STEP 9 — Offer another

> "Done! Would you like to describe another price adjustment?"

If yes, restart from **STEP 1** — do NOT re-run STEP 0. The session file is preserved so
all adjustments described in this conversation accumulate together for a single upload.

---

## Idempotency Reference

The upload script is safe to re-run. Existing records are skipped by these keys:

| Object | Match key |
|---|---|
| PriceAdjustmentSchedule | Name (live org query) |
| PriceAdjustmentTier | (ScheduleId, LowerBound, UpperBound, Product2Id, PSMId) |
| AttributeBasedAdjRule | Name |
| AttributeAdjustmentCondition | (RuleId, AttributeDefinitionId, Operator) |
| AttributeBasedAdjustment | (ScheduleId, RuleId, ProductId, PSMId) |
| BundleBasedAdjustment | (ScheduleId, ProductId, PSMId, ParentProductId) |

---

## Known Prerequisites

- **`IsPriceImpacting = true`** must be set on `ProductClassificationAttr` before
  an attribute can be used in an `AttributeAdjustmentCondition`. Set in Setup →
  Product Classifications → (classification) → Attributes. Always remind the user
  during Step 4 for Attribute adjustments.

- **Org snapshot must be current** for type-based schedule matching. If the snapshot
  is missing or stale, the upload script falls back to requiring explicit schedule names.
  Prompt the user to run `/sync-rca-org` if `SNAPSHOT_LOADED = false`.

---

## See Also

- `/create-rca-products` — Upload products before adding adjustments
- `/sync-rca-org` — Refresh org snapshot (needed for schedule type matching)
- `rca_adjustments.yaml` — The YAML schema reference
