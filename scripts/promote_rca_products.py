#!/usr/bin/env python3
"""
RCA Promote Products
====================
Promotes RCA product configs from a source (org snapshot or catalog YAML) to a
target Salesforce org. Handles:
  - Snapshot ID stripping        (sf_id fields are org-specific — removed before deploy)
  - Optional code filtering      (--include CODE1,CODE2; bundles auto-carry components)
  - Pre-flight dependency check  (PSMs and custom pricebooks must exist in target)
  - Upsert deploy                (creates new + updates changed via create_rca_products.py)

Usage:
  python promote_rca_products.py \
    --source .rca/org-snapshot.yaml \
    --target-org sandbox \
    [--include CODE1,CODE2,BUNDLE-001] \
    [--dry-run] \
    [--api-version 62.0]

Requirements:
  pip install requests pyyaml
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Any, Set

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not found. Run:  pip install requests pyyaml")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("ERROR: 'pyyaml' not found. Run:  pip install requests pyyaml")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Auth helper (same pattern as create_rca_products.py)
# ---------------------------------------------------------------------------
def get_sf_credentials(org_alias: Optional[str]) -> tuple:
    cmd = ["sf", "org", "display", "--json"]
    if org_alias:
        cmd += ["--target-org", org_alias]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"ERROR: sf CLI returned non-JSON: {result.stdout}", file=sys.stderr)
        sys.exit(1)
    if result.returncode != 0 or data.get("status", 1) != 0:
        print(f"ERROR: sf CLI error: {data.get('message', result.stderr)}", file=sys.stderr)
        print("Run 'sf org login web' to authenticate first.", file=sys.stderr)
        sys.exit(1)
    info = data["result"]
    return info["accessToken"], info["instanceUrl"]


# ---------------------------------------------------------------------------
# Lightweight REST client for pre-flight queries
# ---------------------------------------------------------------------------
class SalesforceClient:
    def __init__(self, access_token: str, instance_url: str, api_version: str = "62.0"):
        self.instance_url = instance_url.rstrip("/")
        self.base_url = f"{self.instance_url}/services/data/v{api_version}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def query_safe(self, soql: str) -> List[Dict]:
        try:
            resp = requests.get(
                f"{self.base_url}/query",
                headers=self.headers,
                params={"q": soql},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("records", [])
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Phase 1 — Load and normalize source
# ---------------------------------------------------------------------------
def load_source(path: str) -> Dict:
    """Load a snapshot or catalog YAML and return a normalized catalog dict."""
    if not os.path.isfile(path):
        print(f"ERROR: Source file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        print(f"ERROR: Source file is empty: {path}", file=sys.stderr)
        sys.exit(1)

    # Snapshot has a 'meta' key; plain catalog does not
    is_snapshot = "meta" in data
    if is_snapshot:
        meta = data.get("meta", {})
        print(f"Source:  snapshot  (org={meta.get('org','?')}, "
              f"synced={meta.get('last_synced','?')})")
    else:
        print(f"Source:  catalog   ({path})")

    # Build clean catalog (drop meta, strip all sf_id fields)
    catalog = {
        "catalogs": data.get("catalogs", []),
        "products": data.get("products", []),
        "bundles":  data.get("bundles", []),
    }
    return _strip_ids(copy.deepcopy(catalog))


def _strip_ids(obj: Any) -> Any:
    """Recursively remove 'sf_id' keys from dicts."""
    if isinstance(obj, dict):
        return {k: _strip_ids(v) for k, v in obj.items() if k != "sf_id"}
    if isinstance(obj, list):
        return [_strip_ids(i) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Phase 2 — Filter by --include codes
# ---------------------------------------------------------------------------
def filter_catalog(catalog: Dict, include_codes: Set[str]) -> Dict:
    """Return a filtered catalog containing only the requested codes.

    For bundles in the include set, component product codes are automatically
    added so bundles always carry their dependencies.
    """
    if not include_codes:
        return catalog  # no filter — promote everything

    # Collect component codes for any included bundle
    component_codes: Set[str] = set()
    for bundle in catalog.get("bundles", []):
        if bundle.get("code") in include_codes:
            for group in bundle.get("groups", []):
                for comp in group.get("components", []):
                    if comp.get("code"):
                        component_codes.add(comp["code"])

    effective_codes = include_codes | component_codes

    filtered: Dict[str, Any] = {
        "catalogs": catalog.get("catalogs", []),  # always keep full catalog tree
        "products": [p for p in catalog.get("products", [])
                     if p.get("code") in effective_codes],
        "bundles":  [b for b in catalog.get("bundles", [])
                     if b.get("code") in include_codes],
    }

    auto_added = component_codes - include_codes
    if auto_added:
        print(f"  Auto-included component products: {', '.join(sorted(auto_added))}")

    return filtered


# ---------------------------------------------------------------------------
# Phase 3 — Pre-flight check on target org
# ---------------------------------------------------------------------------
def preflight_check(sf: SalesforceClient, catalog: Dict) -> List[str]:
    """Check that PSMs and custom pricebooks referenced in catalog exist in the target org.

    Returns a list of missing prerequisite names (empty = all clear).
    """
    all_entries = catalog.get("products", []) + catalog.get("bundles", [])

    psm_names: Set[str] = {
        psm for e in all_entries for psm in e.get("psm_options", []) if psm
    }
    custom_pb_names: Set[str] = {
        pbe.get("pricebook", "")
        for e in all_entries
        for pbe in e.get("pricebook_entries", [])
        if pbe.get("pricebook") and "standard" not in pbe.get("pricebook", "").lower()
    }

    missing: List[str] = []
    any_checks = bool(psm_names or custom_pb_names)

    if any_checks:
        print("\nPre-flight check:")

    for name in sorted(psm_names):
        safe = name.replace("'", "\\'")
        found = sf.query_safe(
            f"SELECT Id FROM ProductSellingModel WHERE Name = '{safe}' LIMIT 1"
        )
        status = "✓" if found else "✗ MISSING"
        print(f"  {status}  PSM: {name}")
        if not found:
            missing.append(f"ProductSellingModel '{name}'")

    for name in sorted(custom_pb_names):
        safe = name.replace("'", "\\'")
        found = sf.query_safe(
            f"SELECT Id FROM Pricebook2 WHERE Name = '{safe}' AND IsActive = true LIMIT 1"
        )
        status = "✓" if found else "✗ MISSING"
        print(f"  {status}  Pricebook: {name}")
        if not found:
            missing.append(f"Pricebook '{name}'")

    return missing


# ---------------------------------------------------------------------------
# Phase 4 — Write temp catalog and invoke create_rca_products.py
# ---------------------------------------------------------------------------
def run_promote(catalog: Dict, target_org: str, api_version: str,
                dry_run: bool, scripts_dir: str) -> int:
    """Write a temp catalog and invoke create_rca_products.py with --upsert."""
    create_script = os.path.join(scripts_dir, "create_rca_products.py")
    if not os.path.isfile(create_script):
        print(f"ERROR: create_rca_products.py not found at {create_script}", file=sys.stderr)
        sys.exit(1)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="rca_promote_",
        dir="/tmp", delete=False, encoding="utf-8"
    ) as tmp:
        yaml.dump(catalog, tmp, default_flow_style=False, allow_unicode=True, sort_keys=False)
        tmp_path = tmp.name

    cmd = [
        sys.executable, create_script,
        "--catalog", tmp_path,
        "--org", target_org,
        "--upsert",
        "--api-version", api_version,
    ]
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(cmd)
        return result.returncode
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote RCA product configs from a source snapshot/catalog to a target org",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", "-s", metavar="PATH",
                        help="Source snapshot or catalog YAML (default: .rca/org-snapshot.yaml)")
    parser.add_argument("--target-org", "-t", metavar="ALIAS", required=True,
                        help="sf CLI alias for the target org")
    parser.add_argument("--include", "-i", metavar="CODES",
                        help="Comma-separated product/bundle codes to promote (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only — no records created or updated")
    parser.add_argument("--api-version", metavar="VER", default="62.0",
                        help="Salesforce API version (default: 62.0)")
    args = parser.parse_args()

    # Resolve source path
    source_path = args.source
    if not source_path:
        candidates = [
            ".rca/org-snapshot.yaml",
            ".rca/rca_session.yaml",
            ".rca/rca_catalog.yaml",
        ]
        source_path = next((p for p in candidates if os.path.isfile(p)), None)
        if not source_path:
            print("ERROR: No source file found. Pass --source <path>.", file=sys.stderr)
            sys.exit(1)

    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    mode = "[DRY-RUN]" if args.dry_run else ""
    print(f"RCA Promote Products  {mode}")
    print(f"Target org: {args.target_org}")

    # ── Phase 1: Load source ──────────────────────────────────────────────
    catalog = load_source(source_path)
    total_products = len(catalog.get("products", []))
    total_bundles  = len(catalog.get("bundles", []))

    # ── Phase 2: Filter ───────────────────────────────────────────────────
    include_codes: Set[str] = set()
    if args.include:
        include_codes = {c.strip() for c in args.include.split(",") if c.strip()}

    if include_codes:
        print(f"\nFilter: {', '.join(sorted(include_codes))}")
        catalog = filter_catalog(catalog, include_codes)
    else:
        print("\nFilter: none (promoting all)")

    n_products = len(catalog.get("products", []))
    n_bundles  = len(catalog.get("bundles", []))
    print(f"Promoting: {n_products} product(s), {n_bundles} bundle(s)  →  {args.target_org}")

    if n_products == 0 and n_bundles == 0:
        print("Nothing to promote.")
        sys.exit(0)

    # ── Phase 3: Pre-flight check ─────────────────────────────────────────
    print(f"\nConnecting to target org '{args.target_org}'...")
    access_token, instance_url = get_sf_credentials(args.target_org)
    print(f"Connected to: {instance_url}")
    sf = SalesforceClient(access_token, instance_url, args.api_version)

    missing = preflight_check(sf, catalog)
    if missing:
        print(f"\n⚠  {len(missing)} prerequisite(s) not found in target org:")
        for m in missing:
            print(f"     • {m}")
        print("   These will be skipped during upload.")
        try:
            answer = input("\nContinue anyway? (yes / abort) > ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = "abort"
        if answer not in ("yes", "y"):
            print("Aborted.")
            sys.exit(0)
    else:
        if catalog.get("products") or catalog.get("bundles"):
            print("  All prerequisites found.")

    # ── Phase 4: Run create_rca_products.py ───────────────────────────────
    print(f"\n{'── DRY RUN ──' if args.dry_run else '── PROMOTING ──'}")
    rc = run_promote(catalog, args.target_org, args.api_version, args.dry_run, scripts_dir)

    # ── Phase 5: Summary ──────────────────────────────────────────────────
    if rc == 0:
        print(f"\n✓ Promote {'preview' if args.dry_run else 'complete'}  →  {args.target_org}")
        if not args.dry_run:
            print("💡 Run /sync-rca-org from the target project to refresh its snapshot.")
    else:
        print(f"\n✗ Promote finished with errors (exit code {rc})", file=sys.stderr)
        sys.exit(rc)


if __name__ == "__main__":
    main()
