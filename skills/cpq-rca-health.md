# /cpq-rca-health

Report on the CPQ → RCA migration status of every product in the org snapshot and
recommend next steps. No live org connection required — reads `.rca/org-snapshot.yaml`
only. Run `/sync-rca-org` first to ensure the data is current.

---

## Invocation

```
/cpq-rca-health [--org <alias>]
```

`--org` is used only to locate the snapshot path if multiple org snapshots exist;
it does not trigger any live API calls.

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Locate the snapshot

**Determine the project root** — directory containing `CLAUDE.md` or `.git/`, walking up
from the current working directory. If neither is found, use CWD.

Set `SNAPSHOT_PATH` to `<project_root>/.rca/org-snapshot.yaml`.

If the file does not exist, stop and say:

> "No snapshot found at `<SNAPSHOT_PATH>`. Run `/sync-rca-org` to generate one first."

---

### STEP 1 — Parse managed_by breakdown

Run the following Python inline to extract the data:

```bash
python3 - <<'PYEOF'
import yaml, sys, os
from datetime import datetime, timezone

path = "<SNAPSHOT_PATH>"
with open(path) as f:
    snap = yaml.safe_load(f)

meta = snap.get("meta", {})
synced = meta.get("last_synced", "unknown")
org_alias = meta.get("org_alias", "unknown")

age_days = None
if synced != "unknown":
    try:
        dt = datetime.fromisoformat(synced.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).days
    except Exception:
        pass

buckets = {"rca": [], "cpq": [], "both": [], "neither": []}

for p in snap.get("products", []):
    mb = p.get("managed_by", "neither")
    buckets[mb].append({"code": p.get("code","?"), "name": p.get("name","?"), "type": "product"})

for b in snap.get("bundles", []):
    mb = b.get("managed_by", "neither")
    buckets[mb].append({"code": b.get("code","?"), "name": b.get("name","?"), "type": "bundle"})

import json
print(json.dumps({
    "org_alias": org_alias,
    "synced": synced,
    "age_days": age_days,
    "rca": buckets["rca"],
    "cpq": buckets["cpq"],
    "both": buckets["both"],
    "neither": buckets["neither"],
}))
PYEOF
```

Parse the JSON output into memory. Store:
- `org_alias`, `synced`, `age_days`
- `rca_items`, `cpq_items`, `both_items`, `neither_items` (each a list of `{code, name, type}`)

---

### STEP 2 — Display the health report

Show a header line, then the summary table:

```
CPQ → RCA Migration Health  ·  <org_alias>  ·  snapshot: <age_days> day(s) ago
```

If `age_days` > 7, add a warning: ⚠ Snapshot is over a week old — run `/sync-rca-org` for current data.

```
┌────────────────────────────────────────────────────────────────────────┐
│  Status              │  Products  │  Bundles  │  Items                 │
├────────────────────────────────────────────────────────────────────────┤
│  ✅ RCA only         │  N         │  N        │  fully migrated        │
│  🔄 Mid-migration    │  N         │  N        │  in both CPQ and RCA   │
│  📦 CPQ only         │  N         │  N        │  not yet migrated      │
│  ⚪ Neither          │  N         │  N        │  no CPQ or RCA config  │
├────────────────────────────────────────────────────────────────────────┤
│  Total               │  N         │  N        │                        │
└────────────────────────────────────────────────────────────────────────┘
```

Count products vs bundles separately within each bucket.

---

### STEP 3 — Detail sections

#### 🔄 Mid-migration (needs attention)

If `both_items` is non-empty, list them:

```
🔄 Mid-migration — these exist in both CPQ and RCA. Verify which system is authoritative
   and complete the migration before decommissioning CPQ.

   CODE           NAME                          TYPE
   ─────────────────────────────────────────────────
   <code>         <name>                        product/bundle
   ...
```

#### 📦 CPQ only — ready to migrate

If `cpq_items` is non-empty, list them:

```
📦 CPQ only — not yet in RCA. Use /convert-cpq-to-rca to migrate.

   CODE           NAME                          TYPE
   ─────────────────────────────────────────────────
   <code>         <name>                        product/bundle
   ...
```

If there are more than 15 CPQ-only items, show the first 10 and add:
`  … and N more. Run /convert-cpq-to-rca --all to see the full list.`

#### ⚪ Neither

If `neither_items` is non-empty, list them briefly:

```
⚪ Neither CPQ nor RCA — plain Product2 records with no billing config.
   These may be legacy records, internal SKUs, or placeholders.

   <code>, <code>, <code>, ...
```

Skip this section if the list is empty.

#### ✅ RCA only

If all products are RCA only (nothing in cpq or both), note:
```
✅ All configured products are fully on RCA — no CPQ migration needed.
```
Otherwise skip this section (the table already shows the count).

---

### STEP 4 — Recommendations

Based on the data, generate a prioritized "Next Steps" block. Use the logic below to
decide which recommendations to include (only include relevant ones):

**If `both_items` is non-empty:**
> 1. **Review mid-migration items first.** Products in both systems risk double-billing
>    or config drift. Decide which system owns each product, then either complete the
>    RCA setup and retire the CPQ config, or roll back and keep it in CPQ.

**If `cpq_items` is non-empty:**
> 2. **Migrate CPQ-only products.** Run `/convert-cpq-to-rca` — it will show only
>    unmigrated products and walk through the conversion interactively.
>    `  /convert-cpq-to-rca`

**If `age_days` is null or > 7:**
> 3. **Refresh the snapshot** before running `/convert-cpq-to-rca` so managed_by tags
>    are current. `  /sync-rca-org`

**If `neither_items` is non-empty:**
> 4. **Review plain Product2 records.** N products have no CPQ or RCA configuration.
>    If they're active SKUs, they may need PSM options and pricebook entries added.
>    Use `/describe-rca-product` or `/convert-cpq-to-rca` to configure them.

**If all items are `rca` (nothing in cpq, both, or neither):**
> Migration complete. All products are on RCA. Run `/sync-rca-org` periodically to
> keep the snapshot current after any org changes.

---

## Notes

- `managed_by` is populated by `sync_org_snapshot.py` using silent SBQQ queries.
  If CPQ is not installed in the org, all products will be `rca` or `neither`.
- A product can appear as `neither` if it has no `ProductSellingModelOption` and no
  SBQQ records — this is common for internal/service SKUs.
- The snapshot does not reflect live org changes made after the last `/sync-rca-org` run.
