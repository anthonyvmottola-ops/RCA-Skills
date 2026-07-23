#!/usr/bin/env python3
"""
discover_rca_permission_sets.py
================================
Reads the standard Permission Sets and Permission Set Licenses that actually
exist in a Revenue Cloud Advanced (RCA) org, and tags each Permission Set with
a best-guess functional area (Product Catalog, Pricing, Transaction/Order
Capture, Contracts, Fulfillment/DRO, Billing, ...) using a keyword heuristic.

This exists because there is no reliable static list of RCA's standard
Permission Set API names to hardcode — Salesforce doesn't publish one in a
machine-readable form, and label/API names vary by org (which modules are
licensed, package version, etc). So every run queries the live org directly:

  SELECT Id, Name, NamespacePrefix, Label, Description, Type, IsOwnedByProfile, LicenseId
  FROM PermissionSet WHERE IsOwnedByProfile = false

  SELECT Id, DeveloperName, MasterLabel, PermissionSetLicenseKey FROM PermissionSetLicense

IMPORTANT — use `metadata_name`, not `name`: confirmed by testing against a
live RCA org, the large majority of RCA's standard permission sets are
namespaced (`NamespacePrefix = "force"` in every org observed so far, since
they ship as part of Salesforce's own managed packaging for these features).
A `PermissionSetGroup`'s `<permissionSets>`/`<mutingPermissionSets>` elements
must reference the namespaced form (`force__SomePermissionSet`) — the bare
`PermissionSet.Name` fails deploy with "permission set names are invalid".
This script always emits both: `name` (raw SObject field, for display/lookup)
and `metadata_name` (namespace-qualified, for actual use in generated
metadata) — always pass `metadata_name` values into
`build_permission_set_group.py`'s spec.

The functional-area tag and the "risky default" flag are heuristics meant to
speed up a human's review during /build-rca-permission-set-group — not a
source of truth. Always confirm the actual permission set contents (Setup >
Permission Sets > <name> > View Summary) before assigning broadly.

Authentication uses `sf org display` — no passwords stored.

Usage:
  python discover_rca_permission_sets.py [--org <alias>] [--json] [--area <substr>]

Flags:
  --org   -o   sf CLI org alias. Omit to use the default authenticated org.
  --json       Emit machine-readable JSON instead of a human-readable table.
  --area       Only show permission sets whose functional area matches this
               substring (case-insensitive), e.g. --area billing

Requirements:
  pip install requests
"""

import json
import subprocess
import sys
import argparse
import logging
from typing import Dict, List, Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not found. Run:  pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Functional-area / risky-default heuristics (research-derived, not authoritative)
# ---------------------------------------------------------------------------
AREA_KEYWORDS: Dict[str, List[str]] = {
    "Product Catalog":                 ["catalog", "product discovery", "product configuration", "classification"],
    "Pricing":                         ["pricing", "rate management"],
    "Transaction / Quote & Order Capture": ["quote", "order capture", "checkout", "assetiz",
                                             "calculate price", "calculate tax", "place order"],
    "Contracts / CLM":                 ["contract", "clause", "obligation"],
    "Fulfillment / DRO":               ["fulfillment", "dro ", "orchestrat"],
    "Billing":                         ["billing", "invoice", "credit memo", "tax", "accounts receivable"],
    "Business Rules Engine":           ["business rule", "decision"],
    "Context Service":                 ["context service"],
    "Ship and Debit":                  ["ship and debit", "channel revenue"],
    "Partner":                         ["partner"],
}

# Permission sets flagged by external research as shipping with risky defaults
# (e.g. Manage Flows / Run Flows enabled even for non-admin personas). Matched
# as a case-insensitive substring against Label.
KNOWN_RISKY_DEFAULTS: Dict[str, str] = {
    "dro admin":            "Reported to ship with Manage Flows enabled by default — consider a muting permission set if this persona shouldn't have it.",
    "fulfillment designer": "Reported to ship with Run Flows enabled by default — consider a muting permission set if this persona shouldn't have it.",
}


def guess_areas(label: str, description: str) -> List[str]:
    haystack = f"{label} {description}".lower()
    matches = [area for area, keywords in AREA_KEYWORDS.items()
               if any(kw in haystack for kw in keywords)]
    return matches or ["Uncategorized"]


def guess_risky_default(label: str) -> Optional[str]:
    lowered = label.lower()
    for needle, note in KNOWN_RISKY_DEFAULTS.items():
        if needle in lowered:
            return note
    return None


# ---------------------------------------------------------------------------
# Salesforce REST client (query-only — this script never mutates the org)
# ---------------------------------------------------------------------------
class SalesforceClient:
    def __init__(self, access_token: str, instance_url: str, api_version: str = "62.0"):
        self.instance_url = instance_url.rstrip("/")
        self.base_url = f"{self.instance_url}/services/data/v{api_version}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def query(self, soql: str) -> List[Dict]:
        resp = requests.get(f"{self.base_url}/query", headers=self.headers,
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


def get_sf_credentials(org_alias: Optional[str]) -> tuple:
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
# Discovery
# ---------------------------------------------------------------------------
def discover(sf: SalesforceClient) -> Dict:
    permission_sets = sf.query(
        "SELECT Id, Name, NamespacePrefix, Label, Description, Type, IsOwnedByProfile, LicenseId "
        "FROM PermissionSet WHERE IsOwnedByProfile = false"
    )

    try:
        licenses = sf.query(
            "SELECT Id, DeveloperName, MasterLabel, PermissionSetLicenseKey FROM PermissionSetLicense"
        )
    except requests.HTTPError as exc:
        log.warning("Could not query PermissionSetLicense (%s) — license cross-check skipped.", exc)
        licenses = []

    license_by_id = {lic["Id"]: lic for lic in licenses}

    results = []
    for ps in permission_sets:
        label = ps.get("Label") or ps.get("Name") or ""
        description = ps.get("Description") or ""
        license_id = ps.get("LicenseId")
        license_info = license_by_id.get(license_id) if license_id else None
        namespace = ps.get("NamespacePrefix")

        results.append({
            "name": ps.get("Name"),
            # This — NOT "name" above — is what must go in a PermissionSetGroup's
            # <permissionSets>/<mutingPermissionSets> elements. Confirmed by testing
            # against a live org: the vast majority of RCA's standard permission sets
            # are namespaced (NamespacePrefix = "force" in every org observed so far),
            # and the Metadata API rejects the bare Name with "permission set names
            # are invalid" if the namespace prefix is omitted.
            "metadata_name": f"{namespace}__{ps.get('Name')}" if namespace else ps.get("Name"),
            "namespace": namespace,
            "label": label,
            "description": description,
            "type": ps.get("Type"),
            "license_name": license_info.get("DeveloperName") if license_info else None,
            "license_label": license_info.get("MasterLabel") if license_info else None,
            "functional_areas": guess_areas(label, description),
            "risky_default_note": guess_risky_default(label),
        })

    results.sort(key=lambda r: (r["functional_areas"][0], r["label"] or ""))

    return {
        "permission_sets": results,
        "permission_set_licenses": [
            {"name": lic.get("DeveloperName"), "label": lic.get("MasterLabel"),
             "license_key": lic.get("PermissionSetLicenseKey")}
            for lic in licenses
        ],
    }


def print_table(data: Dict, area_filter: Optional[str]) -> None:
    by_area: Dict[str, List[Dict]] = {}
    for ps in data["permission_sets"]:
        if area_filter and not any(area_filter.lower() in a.lower() for a in ps["functional_areas"]):
            continue
        for area in ps["functional_areas"]:
            by_area.setdefault(area, []).append(ps)

    if not by_area:
        print("No permission sets matched." if area_filter else "No non-profile permission sets found in this org.")
        return

    for area in sorted(by_area):
        print(f"\n=== {area} ===")
        print(f"  {'Metadata Name (use this in a PermissionSetGroup)':50}  {'Label':35}  License")
        print(f"  {'-'*50}  {'-'*35}  {'-'*20}")
        for ps in by_area[area]:
            license_display = ps["license_name"] or "(none / not license-gated)"
            print(f"  {ps['metadata_name']:50}  {(ps['label'] or ''):35}  {license_display}")
            if ps["risky_default_note"]:
                print(f"    ⚠ {ps['risky_default_note']}")

    print(f"\nTotal permission sets: {sum(len(v) for v in by_area.values())} "
          f"(a permission set may be listed under more than one area)")
    print(f"Permission Set Licenses found: {len(data['permission_set_licenses'])}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover RCA-related Permission Sets and Permission Set Licenses in a live org",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--org", "-o", default=None, help="sf CLI org alias")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    parser.add_argument("--area", default=None, help="Filter to functional areas matching this substring")
    args = parser.parse_args()

    token, instance_url = get_sf_credentials(args.org)
    sf = SalesforceClient(token, instance_url)

    data = discover(sf)

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_table(data, args.area)


if __name__ == "__main__":
    main()
