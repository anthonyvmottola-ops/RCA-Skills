# /describe-pricing-procedure

Read a Pricing Procedure from Salesforce Revenue Cloud Advanced (ARM) and render its
steps, step configuration, and variables in a human-readable form. Read-only — makes
no changes to the org or to any local file.

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp describe-pricing-procedure.md /path/to/your/sf-project/.claude/commands/describe-pricing-procedure.md

---

## Overview

Pricing Procedures aren't a simple record type like Product2 or PricebookEntry — in
Revenue Cloud Advanced they're `ExpressionSetDefinition` metadata: a named, ordered
list of steps (each with its own nested configuration), tied to a Context Definition.
This command retrieves the real metadata via the Salesforce CLI and prints it in a
readable outline instead of asking you to go dig through the Pricing Procedure Builder
or a raw XML file.

This is the read-only half of Pricing Procedure tooling. For editing steps or adding
new ones, use `/update-pricing-procedure` (which reuses this same retrieval logic).

Invocation:
```
/describe-pricing-procedure "<procedure name>"
```

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Resolve org, scripts dir, and project root (silent)

**Determine the project root** — the directory containing `CLAUDE.md` or `sfdx-project.json`,
walking up from the current directory. If not found, use the current directory.

**Locate the scripts directory** by checking in this order:
1. Read `CLAUDE.md` in the project root — use the path under `## RCA Tools / Scripts:`
2. `~/tools/rca-product-creator/`
3. Current directory
4. `rca-product-creator/` subdirectory of current directory

Set `SCRIPTS_DIR` to the first directory that contains `read_pricing_procedure.py`.

**Determine the org alias:**
1. `--org` flag if passed
2. `CLAUDE.md` → `## RCA Tools / Default org alias:`
3. Fall back to `myorg`

If no procedure name was passed as an argument, ask:
> "Which Pricing Procedure do you want to inspect? Give me its name (as shown in
> Setup → Pricing Procedures)."

---

### STEP 1 — Retrieve and parse

```bash
python <SCRIPTS_DIR>/read_pricing_procedure.py --name "<procedure name>" --org <alias>
```

This shells out to `sf project retrieve start` under the hood, pulling the real
`ExpressionSetDefinition` metadata into `force-app/main/default/expressionSetDefinition/`
in the current project (so it becomes real, git-tracked project metadata — same as any
other retrieved component). No org data is changed; retrieval is read-only.

**Default output is deliberately condensed** for procedures with many steps:
- A step collapses to a one-line summary only if it truly has no content beyond
  linkage/flag fields (verified against the real XML, not guessed from its type —
  e.g. `AdvancedListFilter` steps look structural but usually carry real gating
  logic in `advancedCondition` and are never collapsed; some `ListGroup` steps
  really are empty containers and do collapse).
- Non-Active versions (Inactive/Draft/Obsolete) collapse to a step/variable count
  instead of full detail, since the Active version is what's actually running.

If the user wants the full unfiltered dump, add `--full`. If they want to drill into
one specific step while keeping everything else compact, add `--step "<step name>"`.

**If the script errors with "No Pricing Procedure found matching '<name>'":**
> "I couldn't find a Pricing Procedure called '<name>' in `<alias>`. Check the exact
> label in Setup → Pricing Procedures, or try `/describe-pricing-procedure` with a
> partial name — I'll search for it."

**If the script errors with "Multiple Pricing Procedures match":**
Show the candidates it printed and ask the user to pick one, then re-run STEP 1 with
the exact `DeveloperName` it reported.

**If `sf` reports an authentication error:**
> "The org isn't authenticated. Run `sf org login web --alias <alias>` and try again."

---

### STEP 2 — Present the result

Relay the script's output as-is — it's already formatted as a readable outline:
- Procedure label, process type, interface source type, Context Definition
- Each version (version number, internal `fullName`, status, date range)
- Each step in sequence order: name, step type / action type, and its configuration
  (for `customElement`-based steps, each parameter's name, value, type, and
  input/output direction)
- Variables defined on the version

If the user asked a specific question about the procedure (e.g. "what pricebook does
this use?", "does it reference a decision table?"), answer it directly from the
retrieved structure rather than just dumping the full outline again.

---

### STEP 3 — Offer next steps

> "Want the full detail on a specific step, the unfiltered dump, one of the other
> versions expanded, another Pricing Procedure, or to make changes with
> `/update-pricing-procedure`?"

If another inspection is requested, loop back to STEP 0 (org/scripts dir resolution
can be skipped if already known this session).

---

## Notes

- This command never writes to Salesforce and never deploys anything. The only local
  side effect is the metadata retrieve, which lands in your project's normal source
  tree (`force-app/main/default/expressionSetDefinition/`). If the project is under
  version control, `git status`/`git diff` will show exactly what was pulled or changed.
- If a step's `stepType`/`actionType` combination isn't a `customElement` (e.g.
  `Condition`, `DecisionTable`, `SubExpression` — not yet seen in this org but valid
  per Salesforce's schema), the script still prints whatever fields exist on that step;
  it doesn't hardcode a specific step shape.
- For version-lifecycle questions (why there are multiple versions, which one is live),
  the `status` field is authoritative: `Active` is what's currently running pricing,
  `Draft`/`Inactive`/`Obsolete` are not.

---

## See Also

- `/update-pricing-procedure` — edit existing steps or add new ones to a procedure
- `refresh_decision_tables.py` — refresh the RCA decision tables a procedure's
  `DecisionTable`/`customElement` steps may reference
