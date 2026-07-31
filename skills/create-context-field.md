# /create-context-field

Creates a custom field and wires it into Revenue Cloud Advanced's Sales
Transaction Context Definition — a Context Attribute + its Context Tag on
the correct Context Node, and a Context Attribute Mapping per target object
(Quote/QuoteLineItem/Order/OrderItem) — so Pricing Procedures, Constraint
Models, and Apex context hooks can read/write the field.

**Not related to product-catalog attributes.** This skill's `context_fields:`
catalog and its `ContextAttribute`/`ContextTag`/`ContextAttributeMapping`
records are a completely different object model from `/create-rca-products`'
`AttributePicklist`/`AttributeDefinition`/`ProductClassificationAttr`
machinery (product/bundle configuration attributes). Don't confuse the two —
neither this skill nor its script touches the other's objects.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp create-context-field.md /path/to/your/sf-project/.claude/commands/create-context-field.md

---

## Why this discovers the live Context Definition instead of hardcoding a name

Confirmed live against a real org: **multiple** `ContextDefinition` records
can exist extended from the same standard `SalesTransactionContext__stdctx`
(e.g. one actually in use, one unused decoy left over from an earlier setup
attempt). Only the one referenced by a live Pricing Procedure
(`ExpressionSetDefinitionContextDefinition`) is the one that matters. This
skill never guesses by `DeveloperName`/`MasterLabel` — it always resolves the
live one dynamically, and stops to ask if more than one live candidate is
found.

## Why Context* records are only ever created, never updated

Context Definitions are **append-only** while their version is active —
removing or changing an existing node/attribute/mapping requires
deactivating the whole `ContextDefinitionVersion`, which cascades to every
Pricing Procedure/Constraint Model depending on it. This skill treats every
`ContextAttribute`/`ContextTag`/`ContextAttributeMapping` write as
effectively permanent: it only ever creates new records, and prints a
**DRIFT WARNING** (never patches) if an existing record's key matches but its
values differ from the catalog.

---

## What Gets Created

| Catalog Section | Salesforce Records Created | API used |
|---|---|---|
| `context_fields[]` (one per object in `target_objects`) | `CustomField` | Tooling API |
| `context_fields[].visibility` | `FieldPermissions` | REST |
| `context_fields[].context` (once per unique `attribute_title` + resolved Context Node) | `ContextAttribute`, `ContextTag` | REST (plain SObject, not Tooling) |
| `context_fields[].context.target_objects[]` (once per object) | `ContextAttributeMapping`, `ContextAttrHydrationDetail` | REST (plain SObject) |

**`ContextAttrHydrationDetail` is required, not optional.** Confirmed live:
every working standard `ContextAttributeMapping` (e.g. `Discount` on
`QuoteLineItem`) has exactly one child `ContextAttrHydrationDetail` record
(`ObjectName` + `QueryAttribute`, mirroring the mapping's own object/field).
Without it, `ContextAttributeMapping` still creates successfully but the
mapping does **not** appear in Setup's Map Data builder and the value
doesn't actually hydrate — this isn't documented anywhere found during
research; it was only discovered by comparing a real create against a known
working example after a first live test silently "succeeded" but the
mapping wasn't visible in Setup.

Supported `field_type_sf` values: `Text`, `TextArea`, `LongTextArea`,
`Number`, `Currency`, `Percent`, `Date`, `DateTime`, `Checkbox`, `Email`,
`Phone`, `Url`, `Picklist`, `MultiselectPicklist`.

`target_objects` with a confirmed, live-verified mapping preference: `Quote`,
`QuoteLineItem` (-> `QuoteEntitiesMapping`), `Order`, `OrderItem` (->
`OrderEntitiesMapping`), `Asset`, `AssetAction`, `AssetActionSource` (->
`AssetEntitiesMapping`), `Contract` (-> `ContractNodeMapping`). This matters
because most objects in this org sit under **two** ContextMappings at once —
their own family mapping and the generic default `SalesTransaction` mapping
— so a confirmed preference is what keeps resolution from silently picking
the wrong one.

Any other object name is still resolved live via `ContextNodeMapping.Object`
— it isn't hardcoded to only these — but without a table entry, if that
object also sits under more than one ContextMapping, the script falls back
to "first match found" with a loud warning rather than a confident pick, and
you should verify the result in Setup. An object with **zero**
ContextNodeMapping matches anywhere in the active version raises a clear
`DiscoveryError` rather than guessing.

**The same field is created on every object in `target_objects`** — not just
one. A single `ContextAttributeMapping.ContextInputAttributeName` (the
field's api_name) must resolve to a real field on each target object; if it
only existed on one, the mapping for the other would point at nothing.

`api_name` is optional — `derive_api_name()` builds one from `label`
automatically, same as `/create-custom-fields`.

---

## Invocation

```
/create-context-field [--catalog <path>] [--org <alias>] [--discover-only] [--dry-run]
```

If arguments are not provided, ask the user:
1. Path to the YAML catalog (default: `context_fields.yaml`)
2. Which `sf` CLI org alias to target (or default if blank)

---

## Step-by-Step Workflow

### Step 0 — Collect arguments

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the current project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory
4. `rca-product-creator/` subdirectory of current directory

**Determine the project root** — the directory containing `CLAUDE.md`, walking up from the current directory. If not found, use the current directory.

Resolve `--catalog` → `CLAUDE.md` `## RCA Tools` catalog path (if one is designated for context fields) → `<PROJECT_ROOT>/.rca/context_fields.yaml` → ask.
Resolve `--org` → `CLAUDE.md` `## RCA Tools / Default org alias:` → ask.

### Step 1 — Check prerequisites

```bash
sf --version
sf org display [--target-org <alias>]
python3 --version
python3 -c "import requests, yaml; print('dependencies OK')"
```

If `requests` or `pyyaml` missing, install automatically: `pip install requests pyyaml`.

Report the Instance URL so the user can confirm it's the right org.

### Step 2 — Preview the catalog

```bash
python3 -c "
import yaml
with open('<catalog_path>') as f:
    cat = yaml.safe_load(f)
for e in cat.get('context_fields', []):
    api_name = e.get('api_name') or e['label'] + '  (derived from label)'
    targets = e.get('context', {}).get('target_objects', [])
    print(f'{api_name:35s}  {e[\"field_type_sf\"]:12s}  -> {targets}')
"
```

### Step 3 — Live Discovery (mandatory, makes zero writes, every run)

**Never assume this org is the same one from a previous run — always
execute this step fresh.** Nothing about the discovered chain is cached
anywhere (no state file, no reused Ids across invocations): every run
re-resolves the live `ContextDefinition`, its active version, and every
node/mapping from scratch against whichever org `--org` points to. The
`OBJECT_NODE_LOOKUP` mapping-preference table only stores Title strings
(`"QuoteEntitiesMapping"`, `"AssetEntitiesMapping"`, ...) from Salesforce's
standard naming convention — never record Ids — so it's portable across
orgs by name, not tied to one org's specific data. If a different org named
its extended mappings differently, resolution still won't guess wrong: it
falls back to "first match found" with a loud warning instead of silently
picking the table's expected title, so always read this step's output
before confirming, especially the first time this skill runs against a
new/unfamiliar org.

```bash
python3 create_context_field.py --catalog <catalog_path> [--org <alias>] --discover-only
```

This resolves the live `ContextDefinition` → active `ContextDefinitionVersion`
→ `ContextNode` → `ContextMapping` → `ContextNodeMapping` chain for every
entry and prints it, e.g.:

```
ContextDefinition: RevSalesTransactionContext (11Og70000005gZkEAI)
Active Version: 7 (11pg7000000hg7IAAQ)

=== Loyalty_Discount_Percent__c -> QuoteLineItem, OrderItem ===
  QuoteLineItem   -> ContextNode "SalesTransactionItem" (11og...) -> ContextMapping "QuoteEntitiesMapping" (11jg...) -> ContextNodeMapping 11bg...
  OrderItem       -> ContextNode "SalesTransactionItem" (11og...) -> ContextMapping "OrderEntitiesMapping" (11jg...) -> ContextNodeMapping 11bg...
```

If discovery reports an `AmbiguousContextDefinition` error listing more than
one candidate, ask the user which one is correct and re-run with
`--context-definition-id <Id>`.

Show this output and ask explicitly:
> "This is the exact ContextDefinition/Node/Mapping chain that will be
> written to. Context\* records are effectively permanent once created
> (append-only while the version is active). Confirm this is correct before
> continuing. (yes / cancel)"

Only proceed to Step 4 on explicit confirmation.

### Step 4 — Dry run

```bash
python3 create_context_field.py --catalog <catalog_path> [--org <alias>] --dry-run
```

Show the output (field creation + Context\* record preview, no writes). Ask:
> "Dry run complete. Ready to create this field and wire it into Context
> Definition in Salesforce? (yes / cancel)"

### Step 5 — Live apply

Because Context\* writes are effectively permanent, use a stronger
confirmation gate than this repo's other skills: show the exact counts from
the dry-run summary (`ContextAttribute created: N`, `ContextTag created: N`,
`ContextAttributeMapping created: N`) and ask the user to type back the total
count of Context\* records about to be created before proceeding.

```bash
python3 create_context_field.py --catalog <catalog_path> [--org <alias>]
```

Surface any ERROR or DRIFT WARNING lines clearly.

### Step 6 — Verify

```bash
sf data query --query "SELECT DeveloperName, TableEnumOrId FROM CustomField WHERE TableEnumOrId IN (<objects>) ORDER BY TableEnumOrId, DeveloperName" --use-tooling-api [--target-org <alias>]

sf data query --query "SELECT Id, Title, DataType, FieldType FROM ContextAttribute WHERE Title = '<attribute_title>'" [--target-org <alias>]

sf data query --query "SELECT ContextAttributeId, ContextNodeId, Title FROM ContextTag WHERE ContextAttributeId = '<id from above>'" [--target-org <alias>]

sf data query --query "SELECT Id, ContextNodeMappingId, ContextAttributeId, ContextInputAttributeName FROM ContextAttributeMapping WHERE ContextAttributeId = '<id>'" [--target-org <alias>]

sf data query --query "SELECT ContextAttributeMappingId, ObjectName, QueryAttribute FROM ContextAttrHydrationDetail WHERE ContextAttributeMappingId IN (<mapping ids from above>)" [--target-org <alias>]
```

Confirm `ContextAttributeMapping.ContextInputAttributeName` exactly equals
the created field's api_name, that a `ContextAttrHydrationDetail` row exists
for every mapping with matching `ObjectName`/`QueryAttribute` (its absence is
the one failure mode that produces no error but leaves the mapping invisible
in Setup), and that each `ContextNodeMappingId` matches
the Id printed during Step 3 for that specific target object — this catches
"wired to the wrong object" silently.

---

## Idempotency

Safe to re-run. Matched by:

| SObject | Match key | On drift |
|---|---|---|
| `CustomField` | `(TableEnumOrId, DeveloperName)` | Same as `/create-custom-fields`: picklist values merged/appended only, everything else left untouched |
| `ContextAttribute` | `(ContextNodeId, Title)` | **Never updated.** Prints a `DRIFT WARNING` naming the mismatched fields and existing record Id, then skips |
| `ContextTag` | `ContextAttributeId` (one tag per attribute observed live) | **Never updated.** `DRIFT WARNING` if the existing Title differs — live Apex/pricing formulas may already reference it |
| `ContextAttributeMapping` | `(ContextNodeMappingId, ContextInputAttributeName)` | **Never updated.** `DRIFT WARNING` if it already points at a different `ContextAttributeId` |
| `ContextAttrHydrationDetail` | `ContextAttributeMappingId` (one per mapping observed live) | **Never updated.** `DRIFT WARNING` if the existing `ObjectName`/`QueryAttribute` differs |
| `FieldPermissions` | `(ParentId, Field)` | Updated only if requested access is higher than current |

All four Context\* objects are strictly additive by design — never PATCHed,
because the append-only behavior of an active Context Definition version
makes even a well-intentioned update as risky as a delete for anything a
live Pricing Procedure already depends on.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `No ContextDefinition is referenced by any ExpressionSetDefinitionContextDefinition row` | No live Pricing Procedure is configured with a context yet | Configure/activate a Pricing Procedure with a Context Definition in Setup first |
| `AmbiguousContextDefinition` (2+ candidates) | Org has multiple ContextDefinitions each referenced by a different live Pricing Procedure | Re-run with `--context-definition-id <Id>` for the correct one |
| `No ContextNodeMapping found for Object='X'` | The target object isn't part of the active SalesTransactionContext version | Verify in Setup > Context Definitions that the object is mapped under the active version |
| `DRIFT WARNING` on ContextAttribute/ContextTag/ContextAttributeMapping | An existing Context\* record matches the key but has different values than the catalog | Inspect the existing record manually in Setup; either align the catalog or use a different `attribute_title`/`tag_title` |
| `create_field: false ... but no such field exists` | Catalog claims the field already exists on a target object, but it doesn't | Set `create_field: true`, or verify the `api_name` matches an existing field on every object in `target_objects` |
| `DUPLICATE_DEVELOPER_NAME` on CustomField | Field with same API name exists under a different label | Check Object Manager — idempotency matches by developer name only |
| `INSUFFICIENT_ACCESS` | Missing Revenue Cloud / Customize Application permission | Assign Revenue Cloud Admin (or System Administrator) to the authenticated user |
| `sf CLI error` | Not authenticated | `sf org login web` |
| Verify step shows a `ContextInputAttributeName` mismatch | Wrong field wired, or catalog changed mid-run | Re-run `--discover-only` to confirm the current chain, then re-apply — safe, since `ContextAttributeMapping` matches by `(ContextNodeMappingId, ContextInputAttributeName)` and won't duplicate |
| `the custom artifact name 'X' must have an '__c' suffix in an extended context definition` | `ContextAttribute.Title`/`ContextTag.Title` need the standard `__c` custom-artifact suffix in an extended Context Definition, same as a custom field's API name | Already handled automatically — `attribute_title`/`tag_title` are run through `derive_api_name()` before create. If you see this, it means a code path bypassed that normalization; report it |
| `Parent node should be transposable` | This is almost always a payload bug, not a real platform block — confirmed live that new attributes CAN be added directly to `SalesTransactionItem`/`SalesTransaction`. It happens when `context.is_key`/`context.is_value` is set `true` on a plain attribute: those flags mark the KEY/VALUE columns of a *transposed* key-value pair (e.g. `AttributeKey`/`AttributeValue` on the transposable `SalesTransactionItemAttribute` node) — every standard direct attribute (`Discount`, `UnitPrice`, ...) has both `false` | Set `is_key: false` and `is_value: false` in the catalog entry unless you are specifically building a transposed key-value attribute pair on a transposable node |
| No error, but the mapping doesn't appear in Setup > Context Definitions > \<definition\> > Map Data | `ContextAttributeMapping` was created successfully, but its required child `ContextAttrHydrationDetail` (`ObjectName`/`QueryAttribute`) is missing — this record type doesn't appear anywhere in the object's own field list or in documentation; it was only found by comparing against a working standard example | Already handled by the script (creates it automatically alongside the mapping). If seen on records made by an older copy of this script, just re-run — it backfills the missing `ContextAttrHydrationDetail` for any existing mapping idempotently |

---

## Notes

- This creates effectively-permanent org state. Test one field end-to-end
  against a dev/sandbox org before batch-catalog use.
- There is no supported "undo" short of manually deactivating the entire
  `ContextDefinitionVersion` in Setup — which cascades to every Pricing
  Procedure/Constraint Model using it. Don't attempt to work around a mistake
  by editing Context\* records directly; treat a wrong entry as a lesson for
  the next catalog run, not something to fix in place.
- `OBJECT_NODE_LOOKUP` has confirmed mapping preferences for `Quote`/
  `QuoteLineItem`/`Order`/`OrderItem`/`Asset`/`AssetAction`/
  `AssetActionSource`/`Contract`. `SalesAgreement` entities
  (`SalesAgreementEntitiesMapping`) also exist in this org but haven't been
  live-verified yet — adding them means checking, the same way as the others,
  which real object names sit under that mapping and whether any also
  collide with the generic default `SalesTransaction` mapping.
- `field_type_sf` (the Salesforce `CustomField` type) and `context.field_type`
  (the `ContextAttribute` `FieldType` picklist: input/output/inputoutput/
  aggregate) are unrelated concepts that happen to share the word "type" —
  kept as separate catalog keys deliberately.
- Only picklist **values** can be added to an already-existing field — label,
  type, required, description are never patched, same as `/create-custom-fields`.

---

## See Also

- `discover_rca_permission_sets.py` / `/build-rca-permission-set-group` — the
  precedent this skill's discovery-first, never-guess-by-name approach follows
- `create-custom-fields.md` (sibling `Org QuickStart Project` repo) — source
  of the `derive_api_name`/picklist-merge/visibility logic ported into
  `create_context_field.py`
- `create-rca-products.md` — a **different** attribute system
  (`AttributePicklist`/`AttributeDefinition`/`ProductClassificationAttr` for
  product-catalog configuration attributes); not used by this skill
