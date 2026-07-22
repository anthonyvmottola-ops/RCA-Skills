# /update-pricing-procedure

Edit an existing Pricing Procedure in Salesforce Revenue Cloud Advanced (ARM): change
values on existing steps, or add new steps cloned from an existing one. Always writes
to a **new Draft version** — the currently Active version is never touched in place.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp update-pricing-procedure.md /path/to/your/sf-project/.claude/commands/update-pricing-procedure.md

---

## Overview

Pricing Procedures are `ExpressionSetDefinition` metadata, not a simple SObject —
editing one means patching nested XML (steps, each with a `customElement` block of
named parameters) and redeploying it via the Metadata API. This command handles that
mechanically so the conversation stays about *what* to change, not *how*.

**Scope of what this command can do (v1):**
- Change a scalar field on an existing step (e.g. its `description`).
- Change the value/type/direction of a named `customElement` parameter on an existing
  step (e.g. flip `IsRealTime`, point `LookUpName`/`LookUpId` at a different decision
  table, change a `Literal` value).
- Add a brand-new step by **cloning an existing step as a template** and overriding
  specific fields/parameters, inserted at a chosen position in the sequence.

**Out of scope (v1)** — hand-authoring a step with a wholly new `stepType`/`actionType`
wiring from scratch (e.g. a brand-new `Condition` or `DecisionTable` step with no
existing analog to clone). Getting that wiring wrong is how a procedure silently stops
pricing correctly, so this command only ever clones something Salesforce already knows
how to run. If you need that, edit it directly in the Pricing Procedure Builder.

This command depends on `/describe-pricing-procedure`'s retrieval script
(`read_pricing_procedure.py`) for STEP 0-1 — it does not re-implement retrieval.

Invocation:
```
/update-pricing-procedure "<procedure name>"
```

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Resolve org, scripts dir, and project root (silent)

Same resolution as `/describe-pricing-procedure`:
1. Project root = directory containing `CLAUDE.md` or `sfdx-project.json`, walking up.
2. `SCRIPTS_DIR` = first of: `CLAUDE.md` → `## RCA Tools / Scripts:`, `~/tools/rca-product-creator/`, cwd, `rca-product-creator/` subdir — whichever contains `read_pricing_procedure.py`.
3. Org alias = `--org` flag → `CLAUDE.md` → `## RCA Tools / Default org alias:` → `myorg`.

If no procedure name was passed, ask for it.

---

### STEP 1 — Retrieve and show current state

```bash
python <SCRIPTS_DIR>/read_pricing_procedure.py --name "<procedure name>" --org <alias> --json
```

Parse the JSON output (it includes `developer_name` and `xml_path` — keep both for
later steps). Render the outline for the user the same way `/describe-pricing-procedure`
does, so they're looking at the real current state before describing changes.

Handle "not found" / "multiple matches" errors exactly as `/describe-pricing-procedure`
does (STEP 1 of that command).

Note which version is `Active` — that's what edits will be cloned from by default.

---

### STEP 1b — Consult the step catalog (silent)

Before interviewing, check `<PROJECT_ROOT>/.rca/pricing_procedure_step_catalog.yaml`.
If present, it already answers "what parameters does actionType X need" and "is this
procedure's sequenceNumber tiered or sequential" — both questions that otherwise require
re-parsing raw XML from scratch every time. Load it into memory for use during STEP 2/3.

**If missing, or if it predates a recent change** (e.g. you just deployed a new step and
want it reflected): regenerate it —

```bash
python <SCRIPTS_DIR>/catalog_pricing_procedure_steps.py --org <alias>
```

This scans every `ExpressionSetDefinition` in the org (not just the one being edited),
filters to genuine Pricing Procedures, and rebuilds the whole catalog — cheap enough to
re-run per session rather than trying to patch it incrementally.

---

### STEP 2 — Interview for changes

Ask:
> "What do you want to change? You can:
> - **Edit a step** — e.g. \"change the LookUpName on ListPrice to X\"
> - **Add a step** — e.g. \"add a step like ListPrice called Y that does Z\"
>
> Describe it in plain language — I'll map it to the specific step/parameter."

Use the step catalog (STEP 1b) to answer without guessing: which `actionType` an
existing step uses, its full known parameter list (names/types/directions/example
values), which existing steps are good clone templates for a similar addition, and —
critically — whether the target version's `sequencing_by_procedure` entry says
"tiered" (use an explicit `sequence_number` matching the right tier, never shift) or
"sequential" (normal `after_step` shift-insertion is safe). Getting this wrong is
exactly the mistake this catalog exists to prevent — see Practitioner Notes in the KB
doc for the real incident that motivated it.

For each requested change, match it against the retrieved structure:

**Editing an existing step:**
- Identify the target step by name/label from the outline.
- If the user's change targets a top-level field (`description`, `label`) → build a
  `set_field` entry.
- If the user's change targets a value shown under that step's parameter list
  (the `customElement` parameters) → build a `set_parameter` entry with the exact
  parameter `name` from the outline and the new `value`.
- If the named parameter doesn't exist on that step, say so and list what parameters
  actually exist on it (from the retrieved structure) rather than guessing.

**Adding a new step:**
- Ask which existing step is the closest template ("Which existing step is most
  similar to what you want? I'll clone it and adjust from there.").
- Ask for the new step's name and label (must be unique in the version).
- Ask where it should go ("Insert it right after which existing step?").
- Ask which of the cloned template's parameters need different values, if any.

Accumulate everything into the patch structure:
```json
{
  "new_version_label": "<derived — see below>",
  "edits": [ {"step_name": "...", "set_field": {...}} , {"step_name": "...", "set_parameter": {"name": "...", "value": "..."}} ],
  "additions": [ {"like_step": "...", "new_name": "...", "new_label": "...", "after_step": "...", "set_field": {...}, "set_parameter": [...]} ]
}
```

For `new_version_label`, default to `"<procedure label> V<next number> - <short description of the change>"` and confirm it with the user.

---

### STEP 3 — Confirmation table

Show exactly what will change before writing anything:

```
┌──────────────────────────────────────────────────────────────┐
│ PRICING PROCEDURE UPDATE — pricingProcedure                  │
│ New version: V2 - fee lookup fix   (cloned from Active V1)   │
├──────────────────────────────────────────────────────────────┤
│ Edits                                                        │
│   ListPrice.customElement[LookUpName]                        │
│     "Price Book Entries V2"  →  "Other Table"                │
├──────────────────────────────────────────────────────────────┤
│ New steps                                                     │
│   ListPriceOverride  (cloned from ListPrice)                 │
│     inserted after ListPrice                                 │
│     customElement[LookUpName] = "Other Table"                │
└──────────────────────────────────────────────────────────────┘
```

Ask: **"Does everything look correct? yes / edit / cancel"**

If **edit**, ask what to change and loop back within STEP 2.

---

### STEP 4 — Apply the patch (writes the new Draft version locally)

Write the patch JSON to a temp file, then:

```bash
python <SCRIPTS_DIR>/patch_pricing_procedure.py \
  --xml "<xml_path from STEP 1>" \
  --patch /tmp/pricing_procedure_patch.json
```

This writes the new Draft version into the retrieved metadata file (in
`force-app/main/default/expressionSetDefinition/`) and prints a unified diff of exactly
what changed. **This only touches the local file — nothing is deployed to Salesforce
yet.**

Show the diff output to the user. If it doesn't match what STEP 3 promised, stop and
investigate rather than deploying.

**If the script errors** (step/parameter not found, duplicate step name, etc.): show
the error verbatim, go back to STEP 2 to correct the input, and re-run STEP 4 — do not
retry blindly.

---

### STEP 5 — Deploy

Always dry-run first:

```bash
python <SCRIPTS_DIR>/patch_pricing_procedure.py \
  --xml "<xml_path>" --patch /tmp/pricing_procedure_patch.json \
  --developer-name <developer_name> --org <alias> --deploy-dry-run
```

Note: since the patch already applied in STEP 4, re-running this with the same patch
file would try to clone the version again — instead, once STEP 4 has already written
the file, deploy directly via the `sf` CLI rather than re-invoking the patch script:

```bash
sf project deploy start --metadata "ExpressionSetDefinition:<developer_name>" --target-org <alias> --dry-run --json
```

Show the result. Ask:
> "Dry-run deploy succeeded. Deploy for real? (yes / cancel)"

Only on explicit confirmation:

```bash
sf project deploy start --metadata "ExpressionSetDefinition:<developer_name>" --target-org <alias> --json
```

Surface any `numberComponentErrors > 0` or failure messages verbatim.

---

### STEP 6 — Remind about activation and decision tables

The new version deploys as **Draft** — it does not affect live pricing until someone
activates it. Tell the user:
> "Deployed as a Draft version (V<n>). It won't affect live pricing until it's
> activated in Setup → Pricing Procedures → <name> → Versions. Activation isn't
> automated by this command yet — that's a manual step for now."

If any edited/added step's parameters reference a decision table (`LookUpName` /
`LookUpApiName` in its `customElement` parameters), remind:
> "This step reads from the '<LookUpName>' decision table. If you changed pricing
> data (not just the procedure), you may also need to refresh it:
> `python refresh_decision_tables.py --tables <key> --org <alias>`"

---

### STEP 7 — Offer another change

> "Want to make another change to this procedure, or a different one?"

If yes, loop back to STEP 2 if same procedure (structure already in memory), or STEP 0
if a different one.

---

## Patch JSON Reference

See `patch_pricing_procedure.py`'s module docstring for the full schema. Summary:

| Key | Purpose |
|---|---|
| `source_version` | Which existing `versionNumber` to clone. Default: highest existing. |
| `new_version_label` | Label for the new Draft version. |
| `edits[].step_name` + `set_field` | Overwrite a top-level scalar field on an existing step. |
| `edits[].step_name` + `set_parameter` | Overwrite a named `customElement` parameter's value/type/direction. |
| `additions[].like_step` | Existing step to clone as a template. |
| `additions[].new_name` / `new_label` | Identity of the new step (must be unique). |
| `additions[].after_step` | Sequence position — inserted immediately after this step; later steps shift down. |
| `additions[].set_field` / `set_parameter` | Same override mechanics as `edits`, applied to the clone. |

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `No Pricing Procedure found matching '<name>'` | Wrong label or typo | Check Setup → Pricing Procedures for the exact label |
| `Multiple Pricing Procedures match` | Ambiguous partial name | Re-run with the exact `DeveloperName` shown |
| `Step '<name>' has no customElement — cannot set parameter` | Tried `set_parameter` on a step without one (e.g. a `Condition`/`DecisionTable` step) | Use `set_field` instead, or confirm the step actually has `customElement` in the STEP 1 outline |
| `Step '<name>' has no customElement parameter named '<x>'` | Parameter name typo | Use the exact name from the STEP 1 outline (script lists available names) |
| `A step named '<x>' already exists in this version` | Duplicate `new_name` | Pick a unique name |
| `sf CLI error` on deploy | Not authenticated | `sf org login web` |
| Deploy fails with an XML/schema parsing error | Element ordering in the patched file doesn't match what the Metadata API expects for that complex type | Open the file, compare against a step of the same `stepType`/`actionType` elsewhere in the same file, and reorder fields to match; then retry the dry-run |
| Deploy fails with `Make sure that the [X] tags for the [Y] variables in the <step> element is in the same parent hierarchy as the Line Item tag in the context definition` | **This is a per-procedure Context Definition issue, not a bug in the patch.** Confirmed by testing: some procedures reject *any* second version — even a byte-for-byte clone with zero edits — while others (e.g. the standard Revenue Management Default Pricing Procedure) accept new Draft versions fine. If this fires, it means *this specific procedure's* Context Definition has a validation gap for adding versions, independent of what you changed. | Before assuming your edit is the cause, isolate it: dry-run deploy a zero-edit clone (`patch_pricing_procedure.py --xml <file> --patch <(echo '{}')`) — if that alone fails, the procedure's Context Definition needs to be investigated separately (check the tag referenced in the error against its parent hierarchy in the Context Definition). Don't keep tweaking the patch content chasing this error; it won't help. |

---

## Notes

- Every run creates a **new Draft version** — it never edits the Active version in
  place. Multiple `/update-pricing-procedure` runs against the same procedure without
  activating in between will each clone from the same Active version, not from each
  other's drafts, unless you pass `source_version` pointing at the prior draft.
- Activating a version, deprecating an old one, and full from-scratch procedure
  authoring are not yet supported — see `/describe-pricing-procedure`'s "See Also"
  for what else exists today.
- If the project isn't under version control, there's no `git diff` safety net —
  rely on the unified diff `patch_pricing_procedure.py` prints in STEP 4, and consider
  committing the retrieved file before patching if you want an undo path.

---

## See Also

- `/describe-pricing-procedure` — read-only inspection (this command's STEP 0-1)
- `catalog_pricing_procedure_steps.py` — builds `.rca/pricing_procedure_step_catalog.yaml`
  (consulted in STEP 1b): every actionType's parameter shape, the operator/valueType
  vocabulary, and whether each procedure's sequencing is tiered or sequential
- `refresh_decision_tables.py` — refresh RCA decision tables referenced by a procedure's steps
