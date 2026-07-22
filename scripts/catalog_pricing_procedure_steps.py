#!/usr/bin/env python3
"""
catalog_pricing_procedure_steps.py
====================================
Builds a reference catalog of every actionType/stepType this org's Pricing
Procedures actually use: the customElement parameter shape for each
actionType (name/type/direction/example values), the operator/valueType
vocabulary actually seen in advancedCondition blocks (plus the full static
Salesforce enum for reference), and the sequencing/parentStep conventions
this org follows.

Why this exists: building a new step (via /update-pricing-procedure) means
knowing what parameters a given actionType expects and how conditions/tiers
actually work. Before this script, that meant re-reading raw XML each time
a new actionType came up. This script queries every ExpressionSetDefinition
in the org, retrieves and parses each one, filters to genuine Pricing
Procedures (interfaceSourceType == "PricingProcedure" — NOT queryable via
SOQL directly; ExpressionSetDefinition only exposes a "Type" column, which
is unpopulated, so this must be checked per-record after retrieval), and
writes one reference file covering all of them.

Usage:
  python catalog_pricing_procedure_steps.py [--org <alias>] [--output <path>]

Reuses read_pricing_procedure.py's SalesforceClient/retrieval/parsing rather
than duplicating it — requires that file in the same directory.

Requirements:
  pip install requests pyyaml
  Salesforce CLI (`sf`) installed and authenticated against the target org.
"""

import json
import os
import sys
import argparse
import logging
import subprocess
from collections import Counter, defaultdict
from typing import Dict, List, Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: 'pyyaml' not found. Run:  pip install pyyaml")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from read_pricing_procedure import (
    SalesforceClient, get_sf_credentials, find_project_root, parse_expression_set_definition,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# Static Salesforce facts (Metadata API Developer Guide, ExpsSetConditionOperator /
# ExpsSetValueType enums) — these don't change per-org; "observed_*" below is what
# THIS org actually uses, which may be a subset.
KNOWN_OPERATORS = ["Contains", "DoesNotContain", "Equals", "NotEquals", "GreaterThan",
                   "GreaterThanOrEquals", "LessThan", "LessThanOrEquals", "IsNull", "IsNotNull"]
KNOWN_VALUE_TYPES = ["Literal", "Parameter", "Formula", "Lookup", "Picklist"]

SEQUENCING_NOTE = (
    "sequenceNumber is often a shared wave/tier marker, NOT a unique per-step ordinal — "
    "confirmed live: Rev_Mgmt_Default_Pricing_Procedure2 has 22 different steps sharing "
    "tier 1 (gating filters), 22 sharing tier 2 (per-line calcs), etc. ListGroup container "
    "steps get their own number in a separate, seemingly execution-irrelevant range (observed "
    "5-25 there) — before inserting a new step, check `sequencing_by_procedure` below for the "
    "TARGET procedure/version: 'tiered' means use an explicit sequence_number matching the "
    "right tier (never shift-renumber); 'sequential' means normal unique-ordinal insertion "
    "(after_step + shift) is safe."
)
PARENT_STEP_NOTE = (
    "A ListGroup step is a named container with no parentStep of its own. Sibling steps "
    "(typically one AdvancedListFilter gate + one or more BusinessKnowledgeModel calc steps) "
    "reference it via parentStep=<container name> to form a conditional group — the group's "
    "members presumably only execute when its filter step's condition passes."
)

MAX_EXAMPLE_VALUES = 5


def list_all_definitions(sf: SalesforceClient) -> List[Dict]:
    return sf.query_tooling("SELECT DeveloperName, MasterLabel FROM ExpressionSetDefinition")


def retrieve_all(project_root: str, developer_names: List[str], org_alias: Optional[str]) -> None:
    cmd = ["sf", "project", "retrieve", "start", "--json"]
    for name in developer_names:
        cmd += ["--metadata", f"ExpressionSetDefinition:{name}"]
    if org_alias:
        cmd += ["--target-org", org_alias]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("sf CLI returned non-JSON on retrieve: %s", result.stdout or result.stderr)
        sys.exit(1)
    if result.returncode != 0 or data.get("status", 1) != 0:
        log.error("Retrieve failed: %s", data.get("message", result.stderr))
        sys.exit(1)


def xml_path_for(project_root: str, developer_name: str) -> str:
    return os.path.join(project_root, "force-app", "main", "default", "expressionSetDefinition",
                         f"{developer_name}.expressionSetDefinition-meta.xml")


def collect_step_shapes(structure: Dict, procedure_name: str, action_catalog: Dict,
                         observed_operators: set, observed_value_types: set) -> None:
    for version in structure["versions"]:
        for step in version["steps"]:
            action_type = step.get("actionType")
            step_type = step.get("stepType")
            key = action_type or f"(no actionType: {step_type})"
            entry = action_catalog.setdefault(key, {
                "step_type": step_type,
                "seen_in": set(),
                "parameters": {},
            })
            # A set, not a list — the same step name recurs once per version
            # (e.g. a procedure with 4 versions repeats every step 4x), and
            # what matters here is which DISTINCT steps use this actionType,
            # not a per-version occurrence count.
            entry["seen_in"].add(f"{procedure_name}.{step['name']}")

            detail = step.get("detail", {})
            custom = detail.get("customElement")
            if isinstance(custom, dict) and "parameters" in custom:
                params = custom["parameters"]
                if isinstance(params, dict):
                    params = [params]
                for p in params:
                    pname = p.get("name")
                    direction = "in" if p.get("input") == "true" else ("out" if p.get("output") == "true" else "?")
                    pentry = entry["parameters"].setdefault(pname, {"type": set(), "direction": set(), "example_values": set()})
                    pentry["type"].add(p.get("type"))
                    pentry["direction"].add(direction)
                    if p.get("value") is not None and len(pentry["example_values"]) < MAX_EXAMPLE_VALUES:
                        pentry["example_values"].add(str(p["value"]))

            advanced = detail.get("advancedCondition")
            if advanced is not None:
                conds = advanced if isinstance(advanced, list) else [advanced]
                for cond in conds:
                    criteria = cond.get("criteria")
                    if isinstance(criteria, dict):
                        criteria = [criteria]
                    for c in criteria or []:
                        if c.get("operator"):
                            observed_operators.add(c["operator"])
                        if c.get("valueType"):
                            observed_value_types.add(c["valueType"])


def sequencing_summary(structure: Dict) -> Dict[str, str]:
    out = {}
    for version in structure["versions"]:
        counts = Counter(int(s["sequenceNumber"]) for s in version["steps"] if s["sequenceNumber"])
        max_sharing = max(counts.values()) if counts else 0
        out[f"v{version['versionNumber']} ({version['status']})"] = (
            "tiered (numbers shared across multiple steps)" if max_sharing > 1
            else "sequential (each number used once)"
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Catalog this org's Pricing Procedure step/action-type vocabulary",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--org", "-o", default=None, help="sf CLI org alias")
    parser.add_argument("--api-version", default="62.0", help="Salesforce API version (default: 62.0)")
    parser.add_argument("--output", default=None,
                         help="Output path (default: <project_root>/.rca/pricing_procedure_step_catalog.yaml)")
    parser.add_argument("--project-root", default=None, help="Override project root detection")
    args = parser.parse_args()

    access_token, instance_url = get_sf_credentials(args.org)
    sf = SalesforceClient(access_token, instance_url, api_version=args.api_version)
    project_root = args.project_root or find_project_root(os.getcwd())

    all_defs = list_all_definitions(sf)
    log.info("Found %d ExpressionSetDefinition record(s) in the org", len(all_defs))
    if not all_defs:
        log.warning("Nothing to catalog.")
        return

    developer_names = [d["DeveloperName"] for d in all_defs]
    retrieve_all(project_root, developer_names, args.org)

    action_catalog: Dict[str, Any] = {}
    observed_operators, observed_value_types = set(), set()
    pricing_procedures: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    sequencing: Dict[str, Dict[str, str]] = {}

    for d in all_defs:
        name = d["DeveloperName"]
        xml_path = xml_path_for(project_root, name)
        if not os.path.isfile(xml_path):
            log.warning("Retrieved file missing for %s, skipping", name)
            continue

        structure = parse_expression_set_definition(xml_path)
        source_type = structure.get("interfaceSourceType")
        if source_type != "PricingProcedure":
            log.info("Skipping %s (interfaceSourceType=%s, not a Pricing Procedure)", name, source_type)
            skipped.append({"developer_name": name, "interface_source_type": source_type})
            continue

        pricing_procedures.append({"developer_name": name, "label": structure.get("label")})
        collect_step_shapes(structure, name, action_catalog, observed_operators, observed_value_types)
        sequencing[name] = sequencing_summary(structure)

    for entry in action_catalog.values():
        entry["seen_in"] = sorted(entry["seen_in"])
        for pentry in entry["parameters"].values():
            pentry["type"] = sorted(pentry["type"])
            pentry["direction"] = sorted(pentry["direction"])
            pentry["example_values"] = sorted(pentry["example_values"])

    catalog = {
        "meta": {
            "pricing_procedures_cataloged": pricing_procedures,
            "other_expression_sets_skipped": skipped,
            "total_action_types": len(action_catalog),
        },
        "known_operators": KNOWN_OPERATORS,
        "known_value_types": KNOWN_VALUE_TYPES,
        "observed_operators": sorted(observed_operators),
        "observed_value_types": sorted(observed_value_types),
        "sequencing_note": SEQUENCING_NOTE,
        "parent_step_note": PARENT_STEP_NOTE,
        "sequencing_by_procedure": sequencing,
        "action_types": action_catalog,
    }

    output_path = args.output or os.path.join(project_root, ".rca", "pricing_procedure_step_catalog.yaml")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("# ============================================================\n")
        f.write("# Pricing Procedure step/action-type catalog — Salesforce RCA\n")
        f.write("# Regenerate: python catalog_pricing_procedure_steps.py --org <alias>\n")
        f.write("# Consult before building a new /update-pricing-procedure addition —\n")
        f.write("# tells you what parameters an actionType needs and whether this\n")
        f.write("# procedure's sequenceNumber is tiered or sequential.\n")
        f.write("# ============================================================\n\n")
        yaml.dump(catalog, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    log.info("─────────────────────────────────────────────────")
    log.info("Catalog written: %s", output_path)
    log.info("Pricing Procedures cataloged: %s", ", ".join(p["developer_name"] for p in pricing_procedures))
    log.info("Other expression sets skipped (not Pricing Procedures): %s",
              ", ".join(s["developer_name"] for s in skipped) or "(none)")
    log.info("Distinct action types: %d", len(action_catalog))
    log.info("─────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
