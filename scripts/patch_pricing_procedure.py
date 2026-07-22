#!/usr/bin/env python3
"""
patch_pricing_procedure.py
===========================
Applies a structured patch to a Pricing Procedure (ExpressionSetDefinition) metadata
file that was retrieved by read_pricing_procedure.py: edit existing steps' fields or
`customElement` parameter values, or add new steps cloned from an existing one with
overrides. The patch always targets a NEW Draft version cloned from a source version
— it never mutates an Active version in place, so live pricing is never touched until
someone deliberately activates the new version later.

Called by /update-pricing-procedure after the interview collects the desired changes.

Usage:
  python patch_pricing_procedure.py --xml <path> --patch <patch.json> \\
      [--developer-name <name>] [--org <alias>] [--deploy-dry-run] [--deploy]

Patch JSON schema
------------------
{
  "source_version": "1",              // optional — versionNumber to clone. Default: highest existing versionNumber
  "new_version_label": "V2 - fee fix",// optional — default: "<label> V<n> (draft)"

  "edits": [
    {"step_name": "ListPrice", "set_field": {"description": "Updated list price step"}},
    {"step_name": "ListPrice", "set_parameter": {"name": "IsRealTime", "value": "true"}}
  ],

  "additions": [
    {
      "like_step": "ListPrice",        // existing step in the source version to clone as a template
      "new_name": "ListPriceOverride", // required — must be unique within the version
      "new_label": "List Price Override",
      "sequence_number": 2,            // PREFERRED: set the tier directly, no other step is touched.
                                        // sequenceNumber is often a shared wave/tier marker, not a
                                        // unique ordinal (confirmed: some procedures have 20+ steps
                                        // sharing the same number) — this is always safe regardless.
      "after_step": "ListPrice",       // only used with sequence_number to pick XML insertion position
                                        // (cosmetic/diff-readability only); OR, if sequence_number is
                                        // omitted, falls back to shift-renumbering everything after it
                                        // — only safe when sequenceNumber is a genuine unique ordinal
                                        // in this version (verify first; see _renumber_and_insert).
      "set_field": {"description": "...", "parentStep": "SomeGroupStepName"},
      "set_parameter": [{"name": "LookUpName", "value": "Other Table"}],
      "set_condition": {                          // only meaningful on AdvancedListFilter-style steps
        "logic": "1 AND 2",                        // optional — defaults to "1 AND 2 AND ... AND N"
        "criteria": [
          {"operator": "GreaterThan", "sourceFieldName": "LineItemQuantity", "value": "50", "valueType": "Literal"}
        ]
      }
    }
  ]
}

`set_condition` replaces the step's whole <advancedCondition> block (used by
AdvancedListFilter-type gating steps) — never a partial edit, since criteria are
positional and conditionLogic references them by position ("1 AND 2"). Valid
`operator` values (Salesforce's ExpsSetConditionOperator enum): Contains,
DoesNotContain, Equals, NotEquals, GreaterThan, GreaterThanOrEquals, LessThan,
LessThanOrEquals, IsNull, IsNotNull. Valid `valueType`: Literal, Parameter,
Formula, Lookup, Picklist.

Scope note: this script only edits fields it models explicitly — top-level scalar
step fields, customElement <parameters> values by parameter name, and whole
advancedCondition blocks. It does not invent a brand-new step shape (a new
stepType/actionType wiring from scratch); `additions` always clones an existing
step and overrides it, which is far safer than hand-authoring the specific
input/output parameter wiring Salesforce expects
for a given actionType.

Requirements:
  Salesforce CLI (`sf`) installed and authenticated, for --deploy-dry-run/--deploy.
"""

from __future__ import annotations

import copy
import difflib
import json
import subprocess
import sys
import os
import argparse
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", METADATA_NS)


def _tag(name: str) -> str:
    return f"{{{METADATA_NS}}}{name}"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find_text(el: ET.Element, name: str) -> Optional[str]:
    child = el.find(_tag(name))
    return child.text if child is not None else None


def _set_or_create(el: ET.Element, name: str, value: str) -> None:
    child = el.find(_tag(name))
    if child is None:
        child = ET.SubElement(el, _tag(name))
    child.text = value


def _remove(el: ET.Element, name: str) -> None:
    child = el.find(_tag(name))
    if child is not None:
        el.remove(child)


# ---------------------------------------------------------------------------
# Step lookup / field & parameter edits
# ---------------------------------------------------------------------------
def _find_step(version_el: ET.Element, step_name: str) -> Optional[ET.Element]:
    for step in version_el.findall(_tag("steps")):
        if _find_text(step, "name") == step_name:
            return step
    return None


def _apply_set_field(step_el: ET.Element, fields: Dict[str, str], step_name: str) -> None:
    for key, value in fields.items():
        _set_or_create(step_el, key, str(value))
        log.info("  %s.%s = %s", step_name, key, value)


def _apply_set_parameter(step_el: ET.Element, param_edits: Any, step_name: str) -> None:
    if isinstance(param_edits, dict):
        param_edits = [param_edits]

    custom = step_el.find(_tag("customElement"))
    if custom is None:
        log.error("  Step '%s' has no customElement — cannot set parameter (nothing to edit)", step_name)
        sys.exit(1)

    available = []
    for p in param_edits:
        target_name = p["name"]
        match = None
        for param_el in custom.findall(_tag("parameters")):
            pname = _find_text(param_el, "name")
            available.append(pname)
            if pname == target_name:
                match = param_el
                break
        if match is None:
            log.error("  Step '%s' has no customElement parameter named '%s'.", step_name, target_name)
            log.error("  Available parameters: %s", ", ".join(sorted(set(available))))
            sys.exit(1)
        if "value" in p:
            _set_or_create(match, "value", str(p["value"]))
        if "type" in p:
            _set_or_create(match, "type", str(p["type"]))
        if "input" in p:
            _set_or_create(match, "input", str(p["input"]).lower())
        if "output" in p:
            _set_or_create(match, "output", str(p["output"]).lower())
        log.info("  %s.customElement.parameters[%s] updated", step_name, target_name)


def _apply_set_condition(step_el: ET.Element, condition: Dict[str, Any], step_name: str) -> None:
    """Replace this step's <advancedCondition> wholesale (the gating logic used
    by AdvancedListFilter-type steps — conditionLogic + an ordered list of
    criteria). There's no partial-edit path here: this always rebuilds the
    whole block, since a condition's criteria are positional (conditionLogic
    references them as "1 AND 2") and safer to author as a single coherent set
    than to patch individual criteria in place.

    condition schema:
      {"logic": "1 AND 2", "criteria": [
        {"operator": "GreaterThan", "sourceFieldName": "LineItemQuantity",
         "value": "50", "valueType": "Literal"}, ...
      ]}
    logic defaults to "1 AND 2 AND ... AND N" if omitted.
    """
    existing = step_el.find(_tag("advancedCondition"))
    if existing is not None:
        step_el.remove(existing)

    cond_el = ET.SubElement(step_el, _tag("advancedCondition"))
    criteria = condition.get("criteria", [])
    logic = condition.get("logic") or " AND ".join(str(i + 1) for i in range(len(criteria)))

    # Field order matches Salesforce's own alphabetical serialization —
    # conditionLogic before criteria.
    _set_or_create(cond_el, "conditionLogic", str(logic))
    for i, c in enumerate(criteria, start=1):
        crit_el = ET.SubElement(cond_el, _tag("criteria"))
        # alphabetical: operator, sequenceNumber, sourceFieldName, value, valueType
        _set_or_create(crit_el, "operator", str(c["operator"]))
        _set_or_create(crit_el, "sequenceNumber", str(c.get("sequenceNumber", i)))
        _set_or_create(crit_el, "sourceFieldName", str(c["sourceFieldName"]))
        if "value" in c and c["value"] is not None:
            _set_or_create(crit_el, "value", str(c["value"]))
        if "valueType" in c and c["valueType"] is not None:
            _set_or_create(crit_el, "valueType", str(c["valueType"]))
    log.info("  %s.advancedCondition replaced (%d criteria, logic=%r)", step_name, len(criteria), logic)


def _apply_overrides(step_el: ET.Element, spec: Dict[str, Any], step_name: str) -> None:
    if "set_field" in spec:
        _apply_set_field(step_el, spec["set_field"], step_name)
    if "set_parameter" in spec:
        _apply_set_parameter(step_el, spec["set_parameter"], step_name)
    if "set_condition" in spec:
        _apply_set_condition(step_el, spec["set_condition"], step_name)


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------
def _step_sequence(step_el: ET.Element) -> int:
    return int(_find_text(step_el, "sequenceNumber") or 0)


def _renumber_and_insert(version_el: ET.Element, new_step: ET.Element, after_step_el: ET.Element) -> None:
    """Insert new_step immediately after after_step_el, shifting every step
    with a STRICTLY GREATER sequenceNumber up by one.

    WARNING: sequenceNumber is not always a unique per-step ordinal — in
    procedures with parallel/branching structure (confirmed via live testing:
    Rev_Mgmt_Default_Pricing_Procedure2 has 22 different steps sharing
    sequenceNumber=1, 22 sharing =2, etc. — it's a wave/tier marker, not a
    unique order), shifting every step at or above the insertion point would
    incorrectly renumber many unrelated steps and corrupt their tier grouping.
    Only use this path when you've confirmed sequenceNumber is actually a
    unique ordinal in the target version (e.g. small/simple procedures like
    the 2-step example this repo started with). Prefer `sequence_number` in
    the addition spec — it sets the new step's tier directly and never
    touches any other step."""
    insert_at = _step_sequence(after_step_el) + 1

    for step in version_el.findall(_tag("steps")):
        seq = _step_sequence(step)
        if seq >= insert_at:
            _set_or_create(step, "sequenceNumber", str(seq + 1))

    _set_or_create(new_step, "sequenceNumber", str(insert_at))

    children = list(version_el)
    idx = children.index(after_step_el)
    version_el.insert(idx + 1, new_step)


def _place_explicit(version_el: ET.Element, new_step: ET.Element, sequence_number: int,
                     after_step_el: Optional[ET.Element]) -> None:
    """Set new_step's sequenceNumber directly — no shifting, no renumbering
    of any other step. This is the safe default: it matches how Salesforce
    itself stores steps that intentionally share a tier (e.g. multiple
    AdvancedListFilter gates all at sequenceNumber=1)."""
    _set_or_create(new_step, "sequenceNumber", str(sequence_number))
    children = list(version_el)
    idx = children.index(after_step_el) if after_step_el is not None else len(children)
    version_el.insert(idx + 1, new_step)


def _apply_addition(version_el: ET.Element, addition: Dict[str, Any]) -> None:
    like_step_name = addition["like_step"]
    new_name = addition["new_name"]

    if _find_step(version_el, new_name) is not None:
        log.error("A step named '%s' already exists in this version — choose a unique name.", new_name)
        sys.exit(1)

    template = _find_step(version_el, like_step_name)
    if template is None:
        log.error("Cannot clone — no existing step named '%s' in this version.", like_step_name)
        sys.exit(1)

    new_step = copy.deepcopy(template)
    _set_or_create(new_step, "name", new_name)
    _set_or_create(new_step, "label", addition.get("new_label", new_name))
    _apply_overrides(new_step, addition, new_name)

    if "sequence_number" in addition:
        after_name = addition.get("after_step")
        after_el = _find_step(version_el, after_name) if after_name else None
        _place_explicit(version_el, new_step, int(addition["sequence_number"]), after_el)
        log.info("Added step '%s' (cloned from '%s') at sequenceNumber=%s",
                  new_name, like_step_name, addition["sequence_number"])
    else:
        after_step_name = addition.get("after_step", like_step_name)
        after_step_el = _find_step(version_el, after_step_name)
        if after_step_el is None:
            log.error("Cannot insert — no existing step named '%s' to insert after.", after_step_name)
            sys.exit(1)
        _renumber_and_insert(version_el, new_step, after_step_el)
        log.info("Added step '%s' (cloned from '%s'), inserted after '%s' (shift-renumbered)",
                  new_name, like_step_name, after_step_name)


# ---------------------------------------------------------------------------
# Version cloning
# ---------------------------------------------------------------------------
def _derive_new_version(root: ET.Element, source_version_num: Optional[str],
                         new_label: Optional[str]) -> ET.Element:
    versions = root.findall(_tag("versions"))
    if not versions:
        log.error("This ExpressionSetDefinition has no <versions> to clone from.")
        sys.exit(1)

    by_num = {int(_find_text(v, "versionNumber") or 0): v for v in versions}
    max_num = max(by_num)

    if source_version_num is not None:
        source = by_num.get(int(source_version_num))
        if source is None:
            log.error("source_version '%s' not found. Existing versions: %s",
                      source_version_num, sorted(by_num))
            sys.exit(1)
    else:
        source = by_num[max_num]

    new_num = max_num + 1
    new_version = copy.deepcopy(source)

    source_full_name = _find_text(source, "fullName") or ""
    if "_V" in source_full_name:
        prefix = source_full_name.rsplit("_V", 1)[0]
    else:
        prefix = source_full_name or _find_text(root, "label") or "PricingProcedure"

    label = new_label or f"{_find_text(root, 'label')} V{new_num} (draft)"

    _set_or_create(new_version, "fullName", f"{prefix}_V{new_num}")
    _set_or_create(new_version, "versionNumber", str(new_num))
    _set_or_create(new_version, "label", label)
    _set_or_create(new_version, "status", "Draft")
    _set_or_create(new_version, "startDate",
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    _remove(new_version, "endDate")

    root.append(new_version)
    log.info("Cloned version %d -> new Draft version %d (%s)",
              int(_find_text(source, 'versionNumber')), new_num, f"{prefix}_V{new_num}")
    return new_version


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
def _deploy(developer_name: str, org_alias: Optional[str], dry_run: bool) -> None:
    cmd = ["sf", "project", "deploy", "start", "--metadata",
           f"ExpressionSetDefinition:{developer_name}", "--json"]
    if org_alias:
        cmd += ["--target-org", org_alias]
    if dry_run:
        cmd += ["--dry-run"]

    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("sf CLI returned non-JSON on deploy: %s", result.stdout or result.stderr)
        sys.exit(1)

    if result.returncode != 0 or data.get("status", 1) != 0:
        log.error("Deploy %s failed:", "dry-run" if dry_run else "")
        log.error(json.dumps(data.get("result", data), indent=2))
        sys.exit(1)

    log.info("Deploy %s succeeded.", "dry-run" if dry_run else "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch a retrieved Pricing Procedure (ExpressionSetDefinition) into a new Draft version",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--xml", required=True, help="Path to the retrieved .expressionSetDefinition-meta.xml")
    parser.add_argument("--patch", required=True, help="Path to the patch JSON file")
    parser.add_argument("--developer-name", default=None, help="DeveloperName, required for --deploy/--deploy-dry-run")
    parser.add_argument("--org", "-o", default=None, help="sf CLI org alias")
    parser.add_argument("--deploy-dry-run", action="store_true", help="Run `sf project deploy start --dry-run` after patching")
    parser.add_argument("--deploy", action="store_true", help="Run a live `sf project deploy start` after patching")
    args = parser.parse_args()

    if not os.path.isfile(args.xml):
        log.error("XML file not found: %s", args.xml)
        sys.exit(1)
    if not os.path.isfile(args.patch):
        log.error("Patch file not found: %s", args.patch)
        sys.exit(1)

    with open(args.patch, encoding="utf-8") as f:
        patch = json.load(f)

    with open(args.xml, encoding="utf-8") as f:
        original_text = f.read()

    tree = ET.parse(args.xml)
    root = tree.getroot()

    new_version = _derive_new_version(root, patch.get("source_version"), patch.get("new_version_label"))

    for edit in patch.get("edits", []):
        step_name = edit["step_name"]
        step_el = _find_step(new_version, step_name)
        if step_el is None:
            log.error("Edit target step '%s' not found in the cloned version.", step_name)
            sys.exit(1)
        _apply_overrides(step_el, edit, step_name)

    for addition in patch.get("additions", []):
        _apply_addition(new_version, addition)

    tree.write(args.xml, encoding="UTF-8", xml_declaration=False)
    with open(args.xml, "rb") as f:
        body = f.read()
    with open(args.xml, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n' + body)

    with open(args.xml, encoding="utf-8") as f:
        new_text = f.read()

    diff = list(difflib.unified_diff(
        original_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{args.xml} (before)",
        tofile=f"{args.xml} (after)",
    ))

    log.info("Patched XML written: %s", args.xml)
    if diff:
        print("".join(diff))
    else:
        log.warning("No textual difference detected — patch may not have applied as expected.")

    if args.deploy_dry_run or args.deploy:
        if not args.developer_name:
            log.error("--developer-name is required to deploy.")
            sys.exit(1)
        if args.deploy_dry_run:
            _deploy(args.developer_name, args.org, dry_run=True)
        if args.deploy:
            _deploy(args.developer_name, args.org, dry_run=False)


if __name__ == "__main__":
    main()
