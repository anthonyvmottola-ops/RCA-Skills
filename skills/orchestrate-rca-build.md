# /orchestrate-rca-build

Takes a full set of requirements for an RCA build — products, bundles, price
adjustments, context fields, permission sets, pricing procedure changes, and
anything else this suite (or the sibling `Org QuickStart Project` tooling)
covers — decomposes them into a dependency-ordered plan across the existing
skills, confirms that plan with you once up front, then walks through
invoking each skill in turn until the whole thing is built.

**This is a sequencer, not an autopilot.** It never skips a skill's own
discovery/dry-run/confirmation steps — every gate `/create-rca-products`,
`/describe-price-adjustment`, `/create-context-field`, etc. already have
stays exactly as strict as when run by hand. What this adds is: the right
order, the right values carried between steps, and one plan-level review
before anything starts — never permission to skip anything downstream.
If a dispatched skill hits its own confirmation gate, an error, or the user
says "cancel" mid-skill, the orchestrator stops right there and reports
where it left off. It does not retry around a real gate or swallow an error
to keep moving.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp orchestrate-rca-build.md /path/to/your/sf-project/.claude/commands/orchestrate-rca-build.md

---

## Why sequencing, not autopilot

A single build session using this suite by hand has already surfaced, more
than once, exactly the kind of surprise that only shows up by actually
running a step and reading its output — not from planning on paper:

- A brand-new `ProductCategory.Code` collided with an unrelated, pre-existing
  category on a completely different catalog (Code is unique **org-wide**,
  not per-catalog — invisible until the create attempt 400s).
- A PricebookEntry couldn't be deleted because a real, pre-existing quote
  (nothing to do with the current build) already had a line item pinned to
  it — Salesforce correctly refused, and the only right move was to stop and
  ask, not force it through.
- Two separate, genuine script bugs (a schedule-name resolution bug that
  would have silently activated the wrong org-default schedule; a missing
  required `EffectiveFrom` on price adjustment tiers) were only visible in
  actual dry-run/live output — never from reading the YAML catalog.

An orchestrator confident enough to skip a skill's own checkpoints to
"complete it all in one go" would very likely have plowed through at least
one of those. This skill instead formalizes the discipline of running each
step by hand, in the right order, with full attention paid to its output —
just without you having to remember the order or re-explain context between
steps every time.

---

## What This Orchestrates

| Requirement shape | Skill(s) dispatched | Typical tier |
|---|---|---|
| New product/bundle from a description | `/describe-rca-product` → `/create-rca-products` | 1 — Foundation |
| New product/bundle, catalog already written | `/create-rca-products` | 1 — Foundation |
| Clone an existing product/bundle | `/clone-product` → `/create-rca-products` | 1 — Foundation |
| CPQ → RCA migration | `/cpq-rca-health` → `/convert-cpq-to-rca` → `/create-rca-products` | 1 — Foundation |
| Custom field feeding a Pricing Procedure/Constraint Model | `/create-context-field` | 2 — Attributes (parallel-safe with Tier 1) |
| Plain custom field, no pricing/context relevance | Org QuickStart's `/create-custom-fields` | 2 — Attributes (parallel-safe with Tier 1) |
| Volume/Attribute/Bundle price adjustment | `/describe-price-adjustment` | 3 — Pricing (needs Tier 1 products + a **current** snapshot) |
| Pricing Procedure step edit/addition | `/describe-pricing-procedure` → `/update-pricing-procedure` | 3 — Pricing (needs Tier 1/2 fields it will reference) |
| Permission Set Group / persona access | `/build-rca-permission-set-group` | 4 — Access (needs everything above it grants access to) |
| Promote to another org | `/org-diff` → `/promote-rca-products` | 5 — Promotion (run last, separately, never same pass as new dev-org work) |
| Audit / health check only, no writes | `/catalog-health`, `/bundle-breakdown`, `/find-product` | Any time — read-only, no ordering constraint |

This table is a starting point, not a closed list — if a requirement doesn't
map cleanly to a row, say so explicitly in the plan (Step 1) rather than
forcing it into the nearest-looking category.

---

## Invocation

```
/orchestrate-rca-build [--requirements <path>] [--org <alias>]
```

If `--requirements` is a path to an existing doc (markdown or YAML), read it.
Otherwise, ask the user to describe everything they want built in this pass —
it's fine if it's a loose paragraph or a bullet list; Step 1 is what turns it
into a structured plan.

---

## Full Workflow — Follow Every Step in Order

### Step 0 — Collect requirements and org

Resolve `--org` the same way every other skill in this suite does: `CLAUDE.md`
`## RCA Tools / Default org alias:`, else ask. Resolve `--requirements`, or
ask the user to describe the full scope of what they want built.

### Step 1 — Decompose into a work-item plan

For each distinct requirement, produce one row:

| # | Requirement | Skill(s) | Tier | Depends on # |
|---|---|---|---|---|

Assign tiers using the table above. Within a tier, order is whatever's
sensible (e.g. alphabetical, or the order the user mentioned them); across
tiers, always Tier 1 → 2 → 3 → 4, with Tier 5 (promotion) always excluded
from the same pass as new dev-org authoring — if the user asked for both,
say so and treat promotion as a separate follow-up run after this one lands
and gets verified.

**Insert a snapshot refresh (`/sync-rca-org`) between Tier 1 and Tier 3 if
any Tier 3 item references a product/bundle created in Tier 1 of *this same
plan*.** Price-adjustment PSM/schedule resolution and pricing-procedure
lookups read from the local snapshot, not live — a stale snapshot from
before this run's own product creation is exactly the kind of gap that
silently resolves the wrong thing.

### Step 2 — Show the plan, confirm once

Print the full table. Ask:
> "This is the full build plan — N items across M tiers. Each item still
> runs through its own skill's normal discovery/dry-run/confirmation steps;
> this confirms the *plan*, not a blanket go-ahead to skip them. Proceed?
> (yes / edit / cancel)"

If **edit**, ask what to change and re-print the updated plan before
continuing. Only proceed to Step 3 on explicit **yes**.

### Step 3 — Execute one work item at a time

For each item in plan order:

1. Announce which item is starting (e.g. "Item 3/9 — AV Inventory Matching
   rooftop volume discount → `/describe-price-adjustment`").
2. Dispatch the mapped skill via the `Skill` tool, carrying forward whatever
   context it needs (product codes just created, field names just wired,
   etc.) — don't make the user re-describe something already established
   earlier in this same plan.
3. Let that skill run its **entire** normal workflow, including every
   discovery/dry-run/confirmation step it would ask for standalone. Read its
   actual output — errors, warnings, and drift notices are exactly the kind
   of thing that only shows up here, not in the plan.
4. If the skill errors, the user cancels, or something looks wrong in its
   output that wasn't anticipated in the plan: **stop**. Report which items
   completed, which one failed/stopped and why, and which remain. Ask
   whether to fix-and-resume, skip this item and continue, or stop entirely.
   Never silently skip a failed item and continue as if it succeeded.
5. On success, mark the item done in the visible checklist (Step 4) and move
   to the next.

### Step 4 — Track progress visibly

After every item (success, skip, or failure), reprint a compact checklist:

```
[x] 1. AutoVerify products (7) + bundle          → /create-rca-products
[x] 2. Sync snapshot                             → /sync-rca-org
[x] 3. Rooftop volume discount on AV-IM-001       → /describe-price-adjustment
[ ] 4. Account lookup context field on QLI        → /create-context-field
[ ] 5. Sales Rep / Pricing Manager permission sets → /build-rca-permission-set-group
```

This is the whole progress-tracking mechanism — no separate state file is
needed for a single uninterrupted conversation. See **Resuming after an
interruption** below for the one case a file is worth writing.

### Step 5 — Final summary

Once every item is done (or the user has decided to stop), give a final
summary: what was built, what's still pending (if anything was skipped), and
whether a final `/sync-rca-org` is warranted (yes, if anything in Tiers 1–3
wrote to the org since the last sync).

---

## Resuming after an interruption

If the conversation is likely to be interrupted or resumed much later (a
large plan, or the user says so explicitly), write the confirmed plan from
Step 2 to `.rca/orchestrator_plan.yaml` before Step 3 begins, and update its
per-item `status` (`pending`/`done`/`skipped`/`failed`) as Step 3 progresses:

```yaml
plan:
  - item: "AutoVerify products (7) + bundle"
    skill: create-rca-products
    tier: 1
    status: done
  - item: "Rooftop volume discount on AV-IM-001"
    skill: describe-price-adjustment
    tier: 3
    status: pending
```

A fresh session invoking `/orchestrate-rca-build` should check for this file
first — if found and it has `pending`/`failed` items, offer to resume from
there instead of re-decomposing requirements from scratch. Delete or archive
the file once every item is `done`; it isn't meant to accumulate across
unrelated builds the way `.rca/rca_session.yaml` does.

---

## What This Does NOT Do

- **Does not grant itself authority to skip a dispatched skill's own gates.**
  If `/create-context-field` wants you to type back a Context\* record count,
  or `/create-rca-products` wants a dry-run confirmed, that still happens —
  this skill's own Step 2 confirmation is an *additional* plan-level check,
  not a replacement for any of them.
- **Does not invent catalog content you didn't ask for.** If a requirement is
  ambiguous (a demo discount with no stated tiers/percentages, a field with
  no stated type), the dispatched skill's own interview handles that
  ambiguity the normal way — the orchestrator doesn't pre-guess it during
  decomposition just to keep the plan moving.
- **Does not run promotion (Tier 5) in the same pass as new authoring.**
  Promoting a build to another org is a distinct, separately-reviewed action
  — always its own follow-up run, after the dev-org build is verified.
- **Does not retry past a real error.** A failed item stops the plan there;
  fixing the underlying issue (a script bug, a bad catalog value, an org
  constraint) is the same manual process it would be if you'd hit it running
  that skill standalone.

---

## Troubleshooting

| Situation | What to do |
|---|---|
| A Tier 3 item can't resolve a product/PSM created earlier in this same plan | Check whether the Tier 1→3 snapshot refresh (Step 1) was actually inserted and run — this is the single most common ordering mistake |
| A dispatched skill's dry-run shows something unexpected (wrong schedule picked, wrong PSM resolved, etc.) | Stop there, same as running that skill standalone — do not proceed to the next item until it's resolved |
| The plan has an item that doesn't map to any existing skill | Say so explicitly in Step 1's plan output; don't force it into the nearest row of the mapping table |
| User wants to add a requirement mid-execution | Treat it as a new item appended to the plan (re-run Step 1 for just the new item, re-show the updated plan, confirm again before it's added to Step 3's queue) |

---

## Notes

- This skill has no scripts of its own and makes no direct API/org calls —
  every actual write happens inside a dispatched skill. Its only jobs are
  decomposition, ordering, confirmation, sequencing, and visible progress.
- Read the **Workflow** section of this repo's `README.md` first if you
  haven't — it already documents the proven manual sequences (CPQ migration,
  product authoring, promotion, etc.) that this skill's Tier table formalizes.
- Keep the dependency table (Step 1) in sync with this repo's `README.md`
  Workflow section as new skills are added — they're meant to describe the
  same ordering knowledge in two forms (prose walkthrough vs. structured
  tiers), not drift apart.

---

## See Also

- `README.md` — the Workflow section this skill's tier ordering formalizes
- `create-rca-products.md` / `describe-price-adjustment.md` /
  `create-context-field.md` / `build-rca-permission-set-group.md` — the
  individual skills this one dispatches; each keeps its own full gate set
- `sync-rca-org.md` — the snapshot refresh inserted between tiers whenever a
  later tier depends on something a same-plan earlier tier just created
