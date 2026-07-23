# /build-rca-permission-set-group

Assemble the standard Revenue Cloud Advanced (RCA) Permission Sets that already
exist in an org into one or more Permission Set Groups, one per persona
(Sales Rep, Pricing Manager, Contract Admin, DRO Admin, Billing Admin, ...).

---

## INSTALL THIS COMMAND
Copy to your Salesforce project's `.claude/commands/` directory:
  cp build-rca-permission-set-group.md /path/to/your/sf-project/.claude/commands/build-rca-permission-set-group.md

---

## Overview

RCA ships a large number of standard Permission Sets — one per module/feature
(Product Catalog Management, Salesforce Pricing, Quote & Order Capture,
Salesforce Contracts, Dynamic Revenue Orchestrator, Billing, ...). There is no
official, static "Persona X = Permission Sets A + B + C" blueprint to hardcode:
the exact API names and which modules are even licensed vary per org. So this
command never guesses names — it **discovers the real Permission Sets and
Permission Set Licenses live from the target org**, uses a keyword heuristic
only to suggest groupings, and always gets explicit confirmation before
writing or deploying anything.

**Scope of what this command can do (v1):**
- Discover real Permission Sets + Permission Set Licenses in the org.
- Interview for personas and build a candidate Permission Set list per persona
  from the discovery results.
- Flag known risky defaults (e.g. `Manage Flows`/`Run Flows` bundled into
  admin-sounding permission sets) and offer a muting Permission Set.
- Generate `PermissionSetGroup` metadata (+ muting `PermissionSet` metadata if
  requested) and deploy it (dry-run, then confirm, then live).

**Out of scope (v1)** — authoring brand-new custom Permission Sets from
scratch (this only bundles Permission Sets that already exist in the org),
and Profile-level changes.

Invocation:
```
/build-rca-permission-set-group
```

---

## Full Workflow — Follow Every Step in Order

---

### STEP 0 — Resolve org, scripts dir, and project root (silent)

1. Project root = directory containing `CLAUDE.md` or `sfdx-project.json`, walking up.
2. `SCRIPTS_DIR` = first of: `CLAUDE.md` → `## RCA Tools / Scripts:`, `~/tools/rca-product-creator/`, cwd, `rca-product-creator/` subdir — whichever contains `discover_rca_permission_sets.py`.
3. Org alias = `--org` flag → `CLAUDE.md` → `## RCA Tools / Default org alias:` → `myorg`.

---

### STEP 1 — Discover real Permission Sets and Licenses

```bash
python <SCRIPTS_DIR>/discover_rca_permission_sets.py --org <alias> --json
```

Parse the JSON and render it grouped by `functional_areas`, showing for each
Permission Set: `metadata_name`, label, whether its underlying license is
provisioned (`license_name`), and any `risky_default_note`. This is the org's
real state — don't substitute assumptions from general RCA knowledge for it.

**Always carry forward `metadata_name`, never the bare `name`.** Confirmed by
testing: nearly all of RCA's standard permission sets are namespaced
(`force__...`), and a deploy fails outright if the group spec uses the
unqualified name.

If the org has very few or zero non-profile permission sets returned, tell the
user this likely means the relevant RCA module license(s) aren't provisioned
yet, and stop here rather than building an empty/wrong group.

---

### STEP 2 — Interview for personas

Ask:
> "Which personas do you need Permission Set Groups for? Common ones for RCA:
> Product catalog admin, Pricing manager/designer, Sales rep, Sales ops rep,
> Contract admin/user/partner, DRO admin, Fulfillment designer/operator,
> Billing admin/ops/tax/AR — or just one comprehensive Admin group."

For each requested persona:
1. Propose a candidate Permission Set list by matching the persona's
   likely functional area(s) against STEP 1's discovery (e.g. "Sales Rep" →
   Transaction/Quote & Order Capture area; "Billing Admin" → Billing area).
2. Show the candidates and ask the user to confirm, add, or remove entries —
   the heuristic is a starting point, not the final answer.
3. Ask for the Permission Set Group's API name and label (default:
   `RCA_<Persona>` / `RCA <Persona>`).

Accumulate into the group spec:
```json
{
  "groups": [
    {
      "api_name": "RCA_Sales_Rep",
      "label": "RCA Sales Rep",
      "description": "...",
      "permission_sets": ["...", "..."]
    }
  ]
}
```

---

### STEP 3 — Check for known risky defaults

For every Permission Set added to any group in STEP 2, check its
`risky_default_note` from STEP 1's discovery output (e.g. DRO Admin User /
Fulfillment Designer commonly ship with `Manage Flows`/`Run Flows` enabled).

If any flagged set is included, ask:
> "`<permission set>` is known to ship with `<permission>` enabled by default,
> which this persona may not need. Add a muting permission set to suppress it
> within this group? (yes / no)"

If yes, add to that group's spec:
```json
"muting": [
  {
    "api_name": "Mute_<PermissionSet>_Flows",
    "label": "Mute <PermissionSet> Flow Access",
    "target_permission_set": "<PermissionSet>",
    "mute_user_permissions": ["ManageFlow", "RunFlow"]
  }
]
```

Only mute the specific permission(s) actually flagged — don't mute
speculatively.

---

### STEP 4 — Confirmation table

Show exactly what will be created before writing anything:

```
┌──────────────────────────────────────────────────────────────┐
│ PERMISSION SET GROUP — RCA Sales Rep (RCA_Sales_Rep)          │
├──────────────────────────────────────────────────────────────┤
│ Permission Sets                                               │
│   Quote_and_Order_Capture_Sales_Rep                           │
│   Product_Discovery_User                                      │
├──────────────────────────────────────────────────────────────┤
│ Muting                                                        │
│   (none)                                                      │
└──────────────────────────────────────────────────────────────┘
```

Repeat per group if multiple personas were requested. Ask:
**"Does everything look correct? yes / edit / cancel"**

If **edit**, go back to STEP 2/3 for the relevant group.

---

### STEP 5 — Write metadata (no deploy yet)

Write the group spec to a temp file, then:

```bash
python <SCRIPTS_DIR>/build_permission_set_group.py \
  --spec /tmp/rca_permission_set_groups.json \
  --project-root "<PROJECT_ROOT>"
```

This writes `force-app/main/default/permissionsetgroups/<api_name>.permissionsetgroup-meta.xml`
(and any muting `PermissionSet` metadata under `force-app/main/default/permissionsets/`)
and prints a unified diff. **Nothing is deployed to Salesforce yet.**

Show the diff to the user. If it doesn't match what STEP 4 promised, stop and
investigate rather than deploying.

---

### STEP 6 — Deploy

Always dry-run first:

```bash
python <SCRIPTS_DIR>/build_permission_set_group.py \
  --spec /tmp/rca_permission_set_groups.json --project-root "<PROJECT_ROOT>" \
  --org <alias> --deploy-dry-run
```

**If a muting Permission Set fails with a schema/validation error**, do not
keep tweaking the generated XML blindly — see the "Muting metadata shape"
note in `build_permission_set_group.py`'s module docstring: create one muting
permission set manually in Setup, retrieve it, and compare shapes.

Show the result. Ask:
> "Dry-run deploy succeeded. Deploy for real? (yes / cancel)"

Only on explicit confirmation:

```bash
python <SCRIPTS_DIR>/build_permission_set_group.py \
  --spec /tmp/rca_permission_set_groups.json --project-root "<PROJECT_ROOT>" \
  --org <alias> --deploy
```

Surface any `numberComponentErrors > 0` or failure messages verbatim.

---

### STEP 7 — Smoke test (optional)

A freshly deployed `PermissionSetGroup` starts with `status = Updating` —
Salesforce recalculates its effective permissions asynchronously, so it won't
immediately reflect in Setup. Tell the user this before testing.

Offer to assign the new group to one test user via SOQL + REST (query the
`PermissionSetGroup.Id`, then `POST /sobjects/PermissionSetAssignment` with
`{"AssigneeId": "<user id>", "PermissionSetGroupId": "<psg id>"}`), and remind
them to use **View Summary** on the group in Setup to confirm the net
effective permissions — especially that any muted permission is actually
suppressed — before assigning it broadly.

---

## Notes

- Group membership suggestions in STEP 2 come from a keyword heuristic over
  this org's real discovered Permission Sets — **not** an official Salesforce
  blueprint. Always have the user confirm each group's contents.
- Permission Set **Licenses** are a prerequisite, not something this command
  assigns — if a persona's needed module isn't provisioned (no matching
  license in STEP 1's output), that's a licensing/setup gap to resolve first,
  not something to work around by including an unlicensed permission set.
- Muting Permission Set XML is best-effort — see `build_permission_set_group.py`.
- This command only bundles existing Permission Sets; it does not author new
  custom Permission Sets or touch Profiles.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| Discovery returns an empty/near-empty list | The relevant RCA module license(s) aren't provisioned in this org yet | Provision the license in Setup before building groups for that persona |
| `INSUFFICIENT_ACCESS` on deploy | The deploying user lacks rights to manage Permission Set Groups | Use an admin-authenticated `sf` session |
| Muting `PermissionSet` deploy fails with a schema/validation error | The auto-generated muting XML shape doesn't match what Salesforce expects (unconfirmed from docs — see script docstring) | Create one muting permission set manually in Setup, retrieve it (`sf project retrieve start --metadata "PermissionSet:<name>"`), and compare/adjust the generated file to match |
| Group still shows `status: Updating` right after deploy | Recalculation is asynchronous | Wait, then re-check in Setup or via a `PermissionSetGroup` query |
| `sf CLI error` on deploy | Not authenticated | `sf org login web` |

---

## See Also

- `discover_rca_permission_sets.py` — read-only discovery of real Permission Sets + Licenses in the org, with functional-area and risky-default heuristics
- `build_permission_set_group.py` — writes `PermissionSetGroup` (+ muting `PermissionSet`) metadata and deploys
- `create-rca-products.md` — the original manual troubleshooting note ("assign Revenue Cloud Admin permission set") that this command supersedes with real discovery
