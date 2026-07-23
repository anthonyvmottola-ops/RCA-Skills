#!/usr/bin/env python3
"""
build_permission_set_group.py
==============================
Writes PermissionSetGroup metadata (and, if requested, companion muting
PermissionSet metadata) from a JSON spec, then optionally deploys it via the
Salesforce CLI. Called by /build-rca-permission-set-group after the discovery
+ persona interview has produced a concrete list of real permission set API
names (from discover_rca_permission_sets.py) to bundle.

This never invents permission set API names — the spec must reference
permission sets that were confirmed to exist in the target org.

Spec JSON schema
-----------------
{
  "groups": [
    {
      "api_name": "RCA_Sales_Rep",              // required — becomes the file name / group fullName
      "label": "RCA Sales Rep",                  // required
      "description": "Bundles standard RCA permission sets for the Sales Rep persona",
      "permission_sets": ["force__RevLifecycleManagementCreateOrderFromQuote", "force__ProductDiscoveryUser"],
      "muting": [                                 // optional
        {
          "api_name": "Mute_DRO_Admin_Flows",     // required if muting present — new muting PermissionSet's API name
          "label": "Mute DRO Admin Flow Access",
          "target_permission_set": "force__DfoAdminUser",  // documentation only — which member PS this is meant to offset
          "mute_user_permissions": ["ManageFlow", "RunFlow"]  // metadata enum names (NOT the "Permissions..." SObject field prefix)
        }
      ]
    }
  ]
}

IMPORTANT — `permission_sets` and `target_permission_set` values must be the
**namespace-qualified metadata name** (e.g. `force__DfoAdminUser`), not the
bare `PermissionSet.Name`. Confirmed by testing: nearly all of RCA's standard
permission sets carry `NamespacePrefix = "force"`, and a deploy fails with
"permission set names are invalid" if the prefix is omitted.
`discover_rca_permission_sets.py` emits exactly this value in its
`metadata_name` field — always source the spec from there, never from the
bare `name` field.

IMPORTANT — muting permission set XML shape is a best-effort scaffold, not a
confirmed-correct shape: Salesforce does not publish a machine-readable spec
for how a muting PermissionSet's metadata differs from a normal one beyond
"referenced via <mutingPermissionSets> in the PermissionSetGroup, with the
permissions to suppress listed explicitly as disabled". If a deploy fails with
a schema/validation error on a muting PermissionSet file, do NOT keep guessing
at the XML — instead, create one muting permission set manually in Setup
(Permission Set Groups > New > Muting Permission Set), retrieve it with:
  sf project retrieve start --metadata "PermissionSet:<name>" --target-org <alias>
and compare its real shape against what this script generated, then adjust.
This mirrors the exact troubleshooting pattern this repo already uses for
Pricing Procedure step XML (see update-pricing-procedure.md's Troubleshooting
table) — always prefer a real retrieved example over hand-authored XML when
the two disagree.

Usage:
  python build_permission_set_group.py --spec <spec.json> [--project-root <path>] \\
      [--org <alias>] [--deploy-dry-run] [--deploy]

Requirements:
  Salesforce CLI (`sf`) installed and authenticated, for --deploy-dry-run/--deploy.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import argparse
import logging
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"


# ---------------------------------------------------------------------------
# Project root resolution (same convention as read_pricing_procedure.py)
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


# ---------------------------------------------------------------------------
# XML generation
# ---------------------------------------------------------------------------
def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def render_permission_set_group_xml(group: Dict) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<PermissionSetGroup xmlns="{METADATA_NS}">',
        f'    <label>{_xml_escape(group["label"])}</label>',
    ]
    if group.get("description"):
        lines.append(f'    <description>{_xml_escape(group["description"])}</description>')
    for ps_name in group.get("permission_sets", []):
        lines.append(f'    <permissionSets>{_xml_escape(ps_name)}</permissionSets>')
    for muting in group.get("muting", []):
        lines.append(f'    <mutingPermissionSets>{_xml_escape(muting["api_name"])}</mutingPermissionSets>')
    lines.append('</PermissionSetGroup>')
    return "\n".join(lines) + "\n"


def render_muting_permission_set_xml(muting: Dict) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<PermissionSet xmlns="{METADATA_NS}">',
        f'    <label>{_xml_escape(muting["label"])}</label>',
    ]
    if muting.get("target_permission_set"):
        lines.append(f'    <description>Mutes permissions granted by {_xml_escape(muting["target_permission_set"])} within its Permission Set Group</description>')
    for perm_name in muting.get("mute_user_permissions", []):
        lines.append('    <userPermissions>')
        lines.append(f'        <enabled>false</enabled>')
        lines.append(f'        <name>{_xml_escape(perm_name)}</name>')
        lines.append('    </userPermissions>')
    lines.append('</PermissionSet>')
    return "\n".join(lines) + "\n"


def _write_with_diff(path: str, content: str) -> None:
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    if existing == content:
        log.info("No change: %s", path)
        return

    diff = list(difflib.unified_diff(
        existing.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f"{path} (before)",
        tofile=f"{path} (after)",
    ))
    log.info("Wrote: %s", path)
    if diff:
        print("".join(diff))


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------
def deploy(metadata_refs: List[str], org_alias: Optional[str], project_root: str, dry_run: bool) -> None:
    cmd = ["sf", "project", "deploy", "start", "--json"]
    for ref in metadata_refs:
        cmd += ["--metadata", ref]
    if org_alias:
        cmd += ["--target-org", org_alias]
    if dry_run:
        cmd += ["--dry-run"]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_root)
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
        description="Write PermissionSetGroup (+ muting PermissionSet) metadata and optionally deploy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--spec", required=True, help="Path to the group spec JSON file")
    parser.add_argument("--project-root", default=None, help="SFDX project root. Default: walk up from cwd for sfdx-project.json")
    parser.add_argument("--org", "-o", default=None, help="sf CLI org alias")
    parser.add_argument("--deploy-dry-run", action="store_true", help="Run `sf project deploy start --dry-run` after writing")
    parser.add_argument("--deploy", action="store_true", help="Run a live `sf project deploy start` after writing")
    args = parser.parse_args()

    if not os.path.isfile(args.spec):
        log.error("Spec file not found: %s", args.spec)
        sys.exit(1)

    with open(args.spec, encoding="utf-8") as f:
        spec = json.load(f)

    groups = spec.get("groups", [])
    if not groups:
        log.error("Spec has no 'groups' entries.")
        sys.exit(1)

    project_root = args.project_root or find_project_root(os.getcwd())
    psg_dir = os.path.join(project_root, "force-app", "main", "default", "permissionsetgroups")
    ps_dir = os.path.join(project_root, "force-app", "main", "default", "permissionsets")

    metadata_refs: List[str] = []

    for group in groups:
        api_name = group["api_name"]
        psg_path = os.path.join(psg_dir, f"{api_name}.permissionsetgroup-meta.xml")
        _write_with_diff(psg_path, render_permission_set_group_xml(group))
        metadata_refs.append(f"PermissionSetGroup:{api_name}")

        for muting in group.get("muting", []):
            muting_api_name = muting["api_name"]
            ps_path = os.path.join(ps_dir, f"{muting_api_name}.permissionset-meta.xml")
            _write_with_diff(ps_path, render_muting_permission_set_xml(muting))
            metadata_refs.append(f"PermissionSet:{muting_api_name}")
            log.warning(
                "Muting PermissionSet '%s' XML is a best-effort scaffold — see this script's "
                "module docstring if the deploy fails with a schema error.", muting_api_name
            )

    if args.deploy_dry_run or args.deploy:
        if args.deploy_dry_run:
            deploy(metadata_refs, args.org, project_root, dry_run=True)
        if args.deploy:
            deploy(metadata_refs, args.org, project_root, dry_run=False)


if __name__ == "__main__":
    main()
