#!/usr/bin/env python3
"""
read_pricing_procedure.py
==========================
Reads a Pricing Procedure from Salesforce Revenue Cloud Advanced and prints a
simplified, human-readable structure of its steps/variables/versions.

Pricing Procedures are NOT a simple SObject like Product2 — they're represented
in Salesforce as `ExpressionSetDefinition` / `ExpressionSetDefinitionVersion`
metadata (nested XML, like a Flow). This script:

  Step 0  Resolve the procedure's DeveloperName via a Tooling API SOQL lookup
  Step 1  Retrieve the real metadata XML via `sf project retrieve start`
  Step 2  Parse the XML into a simplified structure and print it

Called by /describe-pricing-procedure, and by /update-pricing-procedure (which
uses the same retrieval/parsing as its STEP 0-1 before collecting edits).

Authentication uses `sf org display` — no passwords stored.

Usage:
  python read_pricing_procedure.py --name "<label or DeveloperName>" [--org <alias>] [--json] [--full] [--step NAME]

Flags:
  --name  -n   Pricing Procedure label or DeveloperName (required).
  --org   -o   sf CLI org alias. Omit to use the default authenticated org.
  --json       Print the simplified structure as JSON instead of a readable outline.
               The JSON includes "xml_path" and "developer_name" so callers
               (e.g. patch_pricing_procedure.py) can locate the retrieved file.
               Never filtered — always the complete parsed structure.
  --full       Human outline only: show every step individually (no collapsing
               structural steps) with every parameter and flag, unfiltered.
  --step NAME  Human outline only: expand full unfiltered detail for just this
               one step; every other step stays in the compact summary line.

Default outline rendering (no --full/--step) is intentionally lossy for
readability: steps with no business-meaningful config (e.g. AdvancedListFilter
container/filter plumbing) collapse into one summary line, and a fixed set of
near-universal boilerplate parameters/flags are hidden (see NOISY_* below).
Nothing is hidden from --json or --full — use those before building a patch.

Requirements:
  pip install requests pyyaml
  Salesforce CLI (`sf`) installed and authenticated against the target org.
"""

import json
import re
import subprocess
import sys
import os
import argparse
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any, Tuple

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not found. Run:  pip install requests pyyaml")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
ET.register_namespace("", METADATA_NS)


# ---------------------------------------------------------------------------
# Salesforce REST + Tooling API client (same pattern as create_rca_products.py,
# extended with a Tooling API query — ExpressionSetDefinition is a Tooling
# API object, not queryable through the standard REST /query endpoint)
# ---------------------------------------------------------------------------
class SalesforceClient:
    def __init__(self, access_token: str, instance_url: str, api_version: str = "62.0"):
        self.instance_url = instance_url.rstrip("/")
        self.base_url = f"{self.instance_url}/services/data/v{api_version}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def query_tooling(self, soql: str) -> List[Dict]:
        resp = requests.get(f"{self.base_url}/tooling/query", headers=self.headers,
                            params={"q": soql}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records: List[Dict] = data.get("records", [])
        while "nextRecordsUrl" in data:
            resp = requests.get(f"{self.instance_url}{data['nextRecordsUrl']}",
                                headers=self.headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records", []))
        return records


def get_sf_credentials(org_alias: Optional[str]) -> Tuple[str, str]:
    cmd = ["sf", "org", "display", "--json"]
    if org_alias:
        cmd += ["--target-org", org_alias]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("sf CLI returned non-JSON: %s", result.stdout)
        sys.exit(1)
    if result.returncode != 0 or data.get("status", 1) != 0:
        log.error("sf CLI error: %s", data.get("message", result.stderr))
        log.error("Run 'sf org login web' to authenticate first.")
        sys.exit(1)
    info = data["result"]
    return info["accessToken"], info["instanceUrl"]


# ---------------------------------------------------------------------------
# Step 0 — resolve DeveloperName
# ---------------------------------------------------------------------------
def resolve_procedure(sf: SalesforceClient, name: str) -> Dict:
    """Resolve a Pricing Procedure by exact DeveloperName, exact MasterLabel, or
    fuzzy MasterLabel match. Exits with a clear message on 0 or >1 matches."""
    safe = name.replace("'", "\\'")

    rows = sf.query_tooling(
        f"SELECT Id, DeveloperName, MasterLabel FROM ExpressionSetDefinition "
        f"WHERE DeveloperName = '{safe}'"
    )
    if not rows:
        rows = sf.query_tooling(
            f"SELECT Id, DeveloperName, MasterLabel FROM ExpressionSetDefinition "
            f"WHERE MasterLabel = '{safe}'"
        )
    if not rows:
        rows = sf.query_tooling(
            f"SELECT Id, DeveloperName, MasterLabel FROM ExpressionSetDefinition "
            f"WHERE MasterLabel LIKE '%{safe}%'"
        )

    if not rows:
        log.error("No Pricing Procedure found matching '%s'.", name)
        log.error("Check Setup → Pricing Procedures for the exact label, or pass the DeveloperName.")
        sys.exit(1)

    if len(rows) > 1:
        log.error("Multiple Pricing Procedures match '%s':", name)
        for r in rows:
            log.error("  - %s  (DeveloperName: %s)", r["MasterLabel"], r["DeveloperName"])
        log.error("Re-run with the exact DeveloperName to disambiguate.")
        sys.exit(2)

    return rows[0]


# ---------------------------------------------------------------------------
# Step 1 — retrieve the real metadata via sf project retrieve
# ---------------------------------------------------------------------------
def find_project_root(start: str) -> str:
    current = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(current, "sfdx-project.json")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start)
        current = parent


def retrieve_metadata(project_root: str, developer_name: str, org_alias: Optional[str]) -> str:
    cmd = [
        "sf", "project", "retrieve", "start",
        "--metadata", f"ExpressionSetDefinition:{developer_name}",
        "--json",
    ]
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

    files = data.get("result", {}).get("inboundFiles") or data.get("result", {}).get("files") or []
    xml_files = [f for f in files if str(f.get("filePath", "")).endswith(".expressionSetDefinition-meta.xml")]

    if xml_files:
        return os.path.join(project_root, xml_files[0]["filePath"])

    # Fallback to the conventional sfdx source layout if the CLI didn't report file paths.
    # Note: Salesforce's directory name for this metadata type is singular.
    fallback = os.path.join(
        project_root, "force-app", "main", "default", "expressionSetDefinition",
        f"{developer_name}.expressionSetDefinition-meta.xml",
    )
    if os.path.isfile(fallback):
        return fallback

    log.error("Retrieve reported success but the metadata file could not be located.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Step 2 — parse the XML into a simplified structure
# ---------------------------------------------------------------------------
def _tag(name: str) -> str:
    return f"{{{METADATA_NS}}}{name}"


def _child_text(el: ET.Element, name: str) -> Optional[str]:
    child = el.find(_tag(name))
    return child.text if child is not None else None


KNOWN_STEP_FIELDS = {"name", "label", "stepType", "actionType", "sequenceNumber"}


def _elem_to_value(el: ET.Element) -> Any:
    """Recursively convert an element to plain text / dict / list-of-values.
    Repeated same-named children (e.g. multiple <parameters> under a
    <customElement>) become a list rather than silently overwriting each
    other — this is the actual shape RCA uses for step configuration, and
    the same generic conversion works for whatever other stepTypes/orgs
    throw at it without hand-modeling each one."""
    if len(el) == 0:
        return el.text

    by_tag: Dict[str, Any] = {}
    for child in el:
        local = child.tag.split("}")[-1]
        value = _elem_to_value(child)
        if local in by_tag:
            existing = by_tag[local]
            if isinstance(existing, list):
                existing.append(value)
            else:
                by_tag[local] = [existing, value]
        else:
            by_tag[local] = value
    return by_tag


def _extra_children(el: ET.Element, known: set) -> Dict[str, Any]:
    """Everything on this element not already pulled out explicitly above,
    fully recursed — so callers see the whole step config, not just the
    handful of fields this script hardcodes for display purposes."""
    extra: Dict[str, Any] = {}
    for child in el:
        local = child.tag.split("}")[-1]
        if local in known:
            continue
        value = _elem_to_value(child)
        if local in extra:
            existing = extra[local]
            if isinstance(existing, list):
                existing.append(value)
            else:
                extra[local] = [existing, value]
        else:
            extra[local] = value
    return extra


def simplify_step(step_el: ET.Element) -> Dict[str, Any]:
    return {
        "name": _child_text(step_el, "name"),
        "label": _child_text(step_el, "label"),
        "stepType": _child_text(step_el, "stepType"),
        "actionType": _child_text(step_el, "actionType"),
        "sequenceNumber": _child_text(step_el, "sequenceNumber"),
        "detail": _extra_children(step_el, KNOWN_STEP_FIELDS),
    }


def simplify_variable(var_el: ET.Element) -> Dict[str, Any]:
    return {
        "name": _child_text(var_el, "name"),
        "dataType": _child_text(var_el, "dataType"),
        "type": _child_text(var_el, "type"),
        "input": _child_text(var_el, "input"),
        "output": _child_text(var_el, "output"),
        "value": _child_text(var_el, "value"),
    }


def simplify_version(version_el: ET.Element) -> Dict[str, Any]:
    steps = sorted(
        (simplify_step(s) for s in version_el.findall(_tag("steps"))),
        key=lambda s: int(s["sequenceNumber"]) if s["sequenceNumber"] else 0,
    )
    return {
        "fullName": _child_text(version_el, "fullName"),
        "versionNumber": _child_text(version_el, "versionNumber"),
        "label": _child_text(version_el, "label"),
        "status": _child_text(version_el, "status"),
        "startDate": _child_text(version_el, "startDate"),
        "endDate": _child_text(version_el, "endDate"),
        "steps": steps,
        "variables": [simplify_variable(v) for v in version_el.findall(_tag("variables"))],
    }


def parse_expression_set_definition(xml_path: str) -> Dict[str, Any]:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    return {
        "label": _child_text(root, "label"),
        "description": _child_text(root, "description"),
        "processType": _child_text(root, "processType"),
        "interfaceSourceType": _child_text(root, "interfaceSourceType"),
        "contextDefinitions": _child_text(root, "contextDefinitions"),
        "versions": [simplify_version(v) for v in root.findall(_tag("versions"))],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
# These are hidden by default — confirmed via live testing across two real
# procedures (~140 steps total) to be constant boilerplate that never carried
# business meaning, not a guess. --full / --step always show everything
# regardless of these lists.
NOISY_FLAG_KEYS = {
    "hasNestedExplainability", "shouldExposExecPathMsgOnly",
    "shouldExposeConditionDetails", "shouldShowExplExternally", "resultIncluded",
}
NOISY_LITERAL_PARAMS = {
    "sectionCount", "selectedFunction", "IsRealTime",
    "IsPropagationEnabled", "HideWaterfall", "IsContractEnabled",
}
NOISY_LITERAL_PATTERN = re.compile(r"^sectionJsonString\d*$")

# A step is only collapsible if it has NO real content beyond linkage/flags —
# NOT based on actionType (confirmed wrong: AdvancedListFilter steps have no
# actionType at all but carry real gating logic in <advancedCondition>).
# ListGroup steps in this org genuinely are empty container markers with
# nothing but linkage fields — that's what actually makes something collapsible.
LINKAGE_ONLY_KEYS = NOISY_FLAG_KEYS | {"parentStep", "description"}


def _is_structural(step: Dict[str, Any]) -> bool:
    detail = step.get("detail") or {}
    return not any(k not in LINKAGE_ONLY_KEYS for k in detail)


def _render_parameters(params: Any, indent: str, full: bool) -> None:
    if isinstance(params, dict):
        params = [params]
    for p in params or []:
        name = p.get("name", "?")
        if not full and p.get("type") == "Literal" and (
            name in NOISY_LITERAL_PARAMS or NOISY_LITERAL_PATTERN.match(name)
        ):
            continue
        io = "in" if p.get("input") == "true" else ("out" if p.get("output") == "true" else "-")
        print(f"{indent}{name:<24} = {p.get('value')!s:<40} [{p.get('type')}, {io}]")


def _render_advanced_condition(cond: Any, indent: str) -> None:
    if isinstance(cond, list):
        for c in cond:
            _render_advanced_condition(c, indent)
        return
    logic = cond.get("conditionLogic")
    print(f"{indent}Condition{f' ({logic})' if logic else ''}:")
    criteria = cond.get("criteria")
    if isinstance(criteria, dict):
        criteria = [criteria]
    for c in sorted(criteria or [], key=lambda c: int(c.get("sequenceNumber") or 0)):
        field = c.get("sourceFieldName", "?")
        op = c.get("operator", "?")
        val = c.get("value")
        print(f"{indent}  {c.get('sequenceNumber', '?')}. {field} {op}" + (f" {val}" if val is not None else ""))


def _render_step_detail(detail: Dict[str, Any], indent: str, full: bool) -> None:
    custom = detail.get("customElement")
    if isinstance(custom, dict) and "parameters" in custom:
        _render_parameters(custom["parameters"], indent, full)

    advanced = detail.get("advancedCondition")
    if advanced is not None:
        _render_advanced_condition(advanced, indent)

    remaining = {k: v for k, v in detail.items() if k not in ("customElement", "advancedCondition")}
    if not full:
        remaining = {k: v for k, v in remaining.items() if k not in NOISY_FLAG_KEYS}
    flags = ", ".join(f"{k}={v}" for k, v in remaining.items() if v is not None and not isinstance(v, (dict, list)))
    if flags:
        print(f"{indent}({flags})")


def render_outline(structure: Dict[str, Any], full: bool = False, step_filter: Optional[str] = None) -> None:
    print(f"Pricing Procedure: {structure['label']}  ({structure['developer_name']})")
    print(f"Process type: {structure.get('processType')} | Interface source: {structure.get('interfaceSourceType')}")
    if structure.get("contextDefinitions"):
        print(f"Context Definition: {structure['contextDefinitions']}")
    if structure.get("description"):
        print(f"Description: {structure['description']}")
    print()

    for version in structure["versions"]:
        date_range = f"{version.get('startDate') or '?'} → {version.get('endDate') or '—'}"
        print(f"Version {version['versionNumber']} ({version.get('fullName')}) — {version['status']}  ({date_range})")

        if not full and not step_filter and version["status"] != "Active":
            print(f"  {len(version['steps'])} steps, {len(version['variables'])} variables "
                  f"— not Active, collapsed by default. Use --full to expand.")
            print()
            continue

        print(f"  Steps ({len(version['steps'])} total):")

        steps = version["steps"]
        found_filter_match = False
        i = 0
        while i < len(steps):
            step = steps[i]
            is_match = step_filter is not None and step["name"] == step_filter
            found_filter_match = found_filter_match or is_match

            if not is_match and not full and _is_structural(step):
                run_type = step["stepType"]
                run = [step]
                j = i + 1
                while j < len(steps) and not (step_filter is not None and steps[j]["name"] == step_filter) \
                        and _is_structural(steps[j]):
                    run.append(steps[j])
                    j += 1
                print(f"    [{len(run)} {run_type} steps hidden — plumbing, no business config. "
                      f"Use --full or --step <name> to inspect one.]")
                i = j
                continue

            print(f"    {step['sequenceNumber']:>2}. {step['name']:<20} [{step['stepType']}/{step['actionType']}]")
            show_detail = full or is_match or step_filter is None
            if show_detail:
                _render_step_detail(step["detail"], indent="        ", full=(full or is_match))
            i += 1

        if step_filter and not found_filter_match:
            print(f"    (no step named '{step_filter}' in this version)")

        if version["variables"]:
            print("  Variables:")
            for var in version["variables"]:
                io = "input" if var["input"] == "true" else ("output" if var["output"] == "true" else "internal")
                print(f"    {var['name']:<20} {var['dataType']:<10} {io}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read a Pricing Procedure (ExpressionSetDefinition) from Salesforce RCA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--name", "-n", required=True, help="Pricing Procedure label or DeveloperName")
    parser.add_argument("--org", "-o", default=None, help="sf CLI org alias")
    parser.add_argument("--api-version", default="62.0", help="Salesforce API version (default: 62.0)")
    parser.add_argument("--json", action="store_true", help="Print the simplified structure as JSON (always complete, never filtered)")
    parser.add_argument("--full", action="store_true", help="Outline only: show every step individually with every parameter/flag, unfiltered")
    parser.add_argument("--step", default=None, metavar="NAME", help="Outline only: expand full unfiltered detail for just this one step")
    parser.add_argument("--project-root", default=None, help="Override project root detection (must contain sfdx-project.json)")
    args = parser.parse_args()

    access_token, instance_url = get_sf_credentials(args.org)
    sf = SalesforceClient(access_token, instance_url, api_version=args.api_version)

    match = resolve_procedure(sf, args.name)
    developer_name = match["DeveloperName"]

    project_root = args.project_root or find_project_root(os.getcwd())
    xml_path = retrieve_metadata(project_root, developer_name, args.org)

    structure = parse_expression_set_definition(xml_path)
    structure["developer_name"] = developer_name
    structure["xml_path"] = xml_path

    if args.json:
        print(json.dumps(structure, indent=2))
    else:
        render_outline(structure, full=args.full, step_filter=args.step)


if __name__ == "__main__":
    main()
