#!/usr/bin/env python3
"""
RCA Org Snapshot
================
Queries a Salesforce Revenue Cloud Advanced (ARM) org and writes a
.rca/org-snapshot.yaml file reflecting the current state of all RCA
products, bundles, catalogs, and categories — including Salesforce record IDs.

Usage:
  python sync_org_snapshot.py [--org <alias>] [--output <path>] [--api-version 62.0]

Flags:
  --org      -o  sf CLI org alias (omit to use default)
  --output   -p  Output path (default: .rca/org-snapshot.yaml in current dir)
  --api-version  Salesforce API version (default: 62.0)

Requirements:
  pip install requests pyyaml
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

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
# Salesforce REST client (read-only subset of create_rca_products.py)
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
        resp = requests.get(
            f"{self.base_url}/query",
            headers=self.headers,
            params={"q": soql},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        records: List[Dict] = data.get("records", [])
        while "nextRecordsUrl" in data:
            resp = requests.get(
                f"{self.instance_url}{data['nextRecordsUrl']}",
                headers=self.headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            records.extend(data.get("records", []))
        return records

    def query_safe(self, soql: str, label: str = "", silent: bool = False) -> List[Dict]:
        """Query with graceful error handling — returns [] on failure.

        Pass silent=True to suppress the warning (useful for probe queries where
        failure is expected, e.g. testing multi-currency field availability).
        """
        try:
            return self.query(soql)
        except Exception as exc:
            if not silent:
                tag = f" ({label})" if label else ""
                print(f"  ⚠  Query failed{tag}: {exc}", file=sys.stderr)
            return []


# ---------------------------------------------------------------------------
# Auth helper (mirrors create_rca_products.py)
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
# Helpers
# ---------------------------------------------------------------------------
def _managed_by(product_id: str, cpq_ids: set, psm_map: dict) -> str:
    """Classify a product as cpq / rca / both / neither based on detected records."""
    has_cpq = product_id in cpq_ids
    has_rca = product_id in psm_map
    if has_cpq and has_rca:
        return "both"
    if has_cpq:
        return "cpq"
    if has_rca:
        return "rca"
    return "neither"


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------
class OrgSnapshotter:
    def __init__(self, sf: SalesforceClient, org_alias: str):
        self.sf = sf
        self.org_alias = org_alias

    # ── Public entry point ────────────────────────────────────────────────
    def build(self) -> Dict:
        print("Querying org...")

        # 1. Catalogs
        print("  › ProductCatalog")
        catalog_recs = self.sf.query_safe(
            "SELECT Id, Name, Code FROM ProductCatalog ORDER BY Name",
            "ProductCatalog",
        )
        catalog_id_to_name: Dict[str, str] = {r["Id"]: r["Name"] for r in catalog_recs}

        # 2. Categories (flat list, built into tree later)
        print("  › ProductCategory")
        cat_recs = self.sf.query_safe(
            "SELECT Id, Name, Code, ParentCategoryId, CatalogId FROM ProductCategory ORDER BY Name",
            "ProductCategory",
        )
        # cat_map: id → {id, name, code, parent_id, catalog_id, path}
        cat_map: Dict[str, Dict] = {}
        for r in cat_recs:
            cat_map[r["Id"]] = {
                "id":         r["Id"],
                "name":       r.get("Name") or "",
                "code":       r.get("Code") or "",
                "parent_id":  r.get("ParentCategoryId"),
                "catalog_id": r.get("CatalogId") or "",
                "path":       "",  # computed below
            }

        # Compute full path for every category (walk up the parent chain)
        for cat_id in cat_map:
            cat_map[cat_id]["path"] = self._cat_path(cat_id, cat_map, set())

        # 3. Products (non-bundle)
        print("  › Product2 (products)")
        product_recs = self.sf.query_safe(
            "SELECT Id, Name, ProductCode, Description, Family, IsActive, "
            "StockKeepingUnit, QuantityUnitOfMeasure "
            "FROM Product2 WHERE Type != 'Bundle' ORDER BY ProductCode",
            "Product2 products",
        )

        # 4. Bundles
        print("  › Product2 (bundles)")
        bundle_recs = self.sf.query_safe(
            "SELECT Id, Name, ProductCode, Description, Family, IsActive, "
            "StockKeepingUnit, QuantityUnitOfMeasure "
            "FROM Product2 WHERE Type = 'Bundle' ORDER BY ProductCode",
            "Product2 bundles",
        )

        all_prod_recs = product_recs + bundle_recs
        all_prod_ids  = [r["Id"] for r in all_prod_recs]
        prod_code_map = {r["Id"]: (r.get("ProductCode") or "") for r in all_prod_recs}

        # 5. PSM options per product
        psm_map: Dict[str, List[str]] = {}  # product_id → [psm_name, ...]
        if all_prod_ids:
            print("  › ProductSellingModelOption")
            id_in = ", ".join(f"'{i}'" for i in all_prod_ids)
            psm_recs = self.sf.query_safe(
                f"SELECT Product2Id, ProductSellingModel.Name "
                f"FROM ProductSellingModelOption WHERE Product2Id IN ({id_in})",
                "ProductSellingModelOption",
            )
            for r in psm_recs:
                pid      = r["Product2Id"]
                psm_name = (r.get("ProductSellingModel") or {}).get("Name", "")
                if psm_name:
                    psm_map.setdefault(pid, []).append(psm_name)

        # 5a. CPQ product detection (silent — returns empty sets if CPQ not installed)
        cpq_ids: set = set()
        if all_prod_ids:
            print("  › CPQ detection (SBQQ objects)")
            feat_recs = self.sf.query_safe(
                "SELECT SBQQ__ConfiguredSKU__c FROM SBQQ__ProductFeature__c",
                silent=True,
            )
            cpq_ids.update(r["SBQQ__ConfiguredSKU__c"] for r in feat_recs
                           if r.get("SBQQ__ConfiguredSKU__c"))

            opt_recs = self.sf.query_safe(
                "SELECT SBQQ__ConfiguredSKU__c, SBQQ__OptionalSKU__c "
                "FROM SBQQ__ProductOption__c",
                silent=True,
            )
            for r in opt_recs:
                if r.get("SBQQ__ConfiguredSKU__c"):
                    cpq_ids.add(r["SBQQ__ConfiguredSKU__c"])
                if r.get("SBQQ__OptionalSKU__c"):
                    cpq_ids.add(r["SBQQ__OptionalSKU__c"])

            sub_recs = self.sf.query_safe(
                "SELECT Id FROM Product2 WHERE SBQQ__SubscriptionType__c != null",
                silent=True,
            )
            cpq_ids.update(r["Id"] for r in sub_recs)

            if cpq_ids:
                n = sum(1 for pid in all_prod_ids if pid in cpq_ids)
                print(f"    CPQ-managed products found: {n}")
            else:
                print("    CPQ not detected (SBQQ objects absent or empty)")

        # 6. Pricebook entries per product
        pbe_map: Dict[str, List[Dict]] = {}  # product_id → [{pricebook, price, currency}]
        if all_prod_ids:
            print("  › PricebookEntry")
            id_in = ", ".join(f"'{i}'" for i in all_prod_ids)
            # Probe for multi-currency support silently; fall back if the field
            # isn't available (single-currency orgs don't expose CurrencyIsoCode).
            pbe_recs = self.sf.query_safe(
                f"SELECT Product2Id, UnitPrice, CurrencyIsoCode, Pricebook2.Name "
                f"FROM PricebookEntry WHERE Product2Id IN ({id_in}) AND IsActive = true",
                silent=True,
            )
            if not pbe_recs:
                pbe_recs = self.sf.query_safe(
                    f"SELECT Product2Id, UnitPrice, Pricebook2.Name "
                    f"FROM PricebookEntry WHERE Product2Id IN ({id_in}) AND IsActive = true",
                    "PricebookEntry",
                )
            for r in pbe_recs:
                pid     = r["Product2Id"]
                pb_name = (r.get("Pricebook2") or {}).get("Name", "")
                if pb_name:
                    pbe_map.setdefault(pid, []).append({
                        "pricebook": pb_name,
                        "price":     float(r.get("UnitPrice") or 0),
                        "currency":  r.get("CurrencyIsoCode") or "USD",
                    })

        # 7. Catalog/category placement per product
        cat_link_map: Dict[str, Dict] = {}  # product_id → {catalog, category}
        if all_prod_ids:
            print("  › ProductCategoryProduct")
            id_in = ", ".join(f"'{i}'" for i in all_prod_ids)
            link_recs = self.sf.query_safe(
                f"SELECT ProductId, ProductCategoryId "
                f"FROM ProductCategoryProduct WHERE ProductId IN ({id_in})",
                "ProductCategoryProduct",
            )
            for r in link_recs:
                pid  = r["ProductId"]
                cid  = r.get("ProductCategoryId", "")
                if cid and cid in cat_map:
                    cat          = cat_map[cid]
                    catalog_name = catalog_id_to_name.get(cat["catalog_id"], "")
                    cat_link_map[pid] = {
                        "catalog":  catalog_name,
                        "category": cat["path"],
                    }

        # 8. Bundle component groups
        bundle_ids     = [r["Id"] for r in bundle_recs]
        group_map:     Dict[str, List[Dict]] = {}  # bundle_id → [group_dict, ...]
        group_id_info: Dict[str, Dict]       = {}  # group_id → mutable group dict

        if bundle_ids:
            print("  › ProductComponentGroup")
            id_in = ", ".join(f"'{i}'" for i in bundle_ids)
            group_recs = self.sf.query_safe(
                f"SELECT Id, Name, Code, ParentProductId, "
                f"MinBundleComponents, MaxBundleComponents, Sequence "
                f"FROM ProductComponentGroup WHERE ParentProductId IN ({id_in}) "
                f"ORDER BY ParentProductId, Sequence",
                "ProductComponentGroup",
            )
            for r in group_recs:
                bid   = r["ParentProductId"]
                gdata = {
                    "id":             r["Id"],
                    "name":           r.get("Name") or "",
                    "code":           r.get("Code") or "",
                    "min_selections": r.get("MinBundleComponents"),
                    "max_selections": r.get("MaxBundleComponents"),
                    "sequence":       int(r.get("Sequence") or 1),
                    "components":     [],
                }
                group_map.setdefault(bid, []).append(gdata)
                group_id_info[r["Id"]] = gdata

        # 9. Bundle component lines
        if group_id_info:
            print("  › ProductRelatedComponent")
            gid_in = ", ".join(f"'{i}'" for i in group_id_info)
            comp_recs = self.sf.query_safe(
                f"SELECT Id, ChildProductId, ProductComponentGroupId, "
                f"IsComponentRequired, IsDefaultComponent, Sequence, MinQuantity, MaxQuantity "
                f"FROM ProductRelatedComponent WHERE ProductComponentGroupId IN ({gid_in}) "
                f"ORDER BY ProductComponentGroupId, Sequence",
                "ProductRelatedComponent",
            )
            for r in comp_recs:
                gid  = r.get("ProductComponentGroupId", "")
                cpid = r.get("ChildProductId", "")
                if not gid or gid not in group_id_info:
                    continue
                comp: Dict[str, Any] = {
                    "code":     prod_code_map.get(cpid, cpid),
                    "sf_id":    r["Id"],
                    "required": bool(r.get("IsComponentRequired")),
                    "default":  bool(r.get("IsDefaultComponent")),
                    "sequence": int(r.get("Sequence") or 1),
                }
                if r.get("MinQuantity") is not None:
                    comp["min_qty"] = float(r["MinQuantity"])
                if r.get("MaxQuantity") is not None:
                    comp["max_qty"] = float(r["MaxQuantity"])
                group_id_info[gid]["components"].append(comp)

        # 10. ProductSellingModel records
        print("  › ProductSellingModel")
        psm_model_recs = self.sf.query_safe(
            "SELECT Id, Name, SellingModelType, PricingTerm, PricingTermUnit, Status "
            "FROM ProductSellingModel WHERE Status = 'Active' ORDER BY Name",
            "ProductSellingModel",
        )
        selling_models_out: List[Dict] = []
        for r in psm_model_recs:
            sm: Dict[str, Any] = {
                "name":   r.get("Name") or "",
                "sf_id":  r["Id"],
                "type":   r.get("SellingModelType") or "",
                "status": r.get("Status") or "",
            }
            if r.get("PricingTerm") is not None:
                sm["pricing_term"] = r["PricingTerm"]
            if r.get("PricingTermUnit"):
                sm["pricing_term_unit"] = r["PricingTermUnit"]
            selling_models_out.append(sm)

        # 11. PriceAdjustmentSchedule records
        print("  › PriceAdjustmentSchedule")
        pas_recs = self.sf.query_safe(
            "SELECT Id, Name, ScheduleType, AdjustmentMethod, IsActive, "
            "EffectiveFrom, EffectiveTo, Pricebook2Id, Pricebook2.Name "
            "FROM PriceAdjustmentSchedule ORDER BY ScheduleType, Name",
            "PriceAdjustmentSchedule",
        )
        price_adj_schedules_out: List[Dict] = []
        for r in pas_recs:
            pas: Dict[str, Any] = {
                "sf_id":         r["Id"],
                "name":          r.get("Name") or "",
                "schedule_type": r.get("ScheduleType") or "",
                "is_active":     bool(r.get("IsActive")),
            }
            if r.get("AdjustmentMethod"):
                pas["adjustment_method"] = r["AdjustmentMethod"]
            if r.get("EffectiveFrom"):
                pas["effective_from"] = r["EffectiveFrom"]
            if r.get("EffectiveTo"):
                pas["effective_to"] = r["EffectiveTo"]
            pb = (r.get("Pricebook2") or {}).get("Name")
            if pb:
                pas["pricebook"] = pb
            price_adj_schedules_out.append(pas)

        # ── Assemble snapshot dict ─────────────────────────────────────────
        print("Assembling snapshot...")

        catalogs_out = []
        for cr in catalog_recs:
            cat_entry: Dict[str, Any] = {"name": cr["Name"], "sf_id": cr["Id"]}
            if cr.get("Code"):
                cat_entry["code"] = cr["Code"]
            top_cats = self._build_cat_tree(cr["Id"], None, cat_map)
            if top_cats:
                cat_entry["categories"] = top_cats
            catalogs_out.append(cat_entry)

        products_out = []
        for r in product_recs:
            pid   = r["Id"]
            entry = self._base_entry(r, pid, cat_link_map, psm_map, pbe_map)
            entry["managed_by"] = _managed_by(pid, cpq_ids, psm_map)
            products_out.append(entry)

        bundles_out = []
        for r in bundle_recs:
            bid   = r["Id"]
            entry = self._base_entry(r, bid, cat_link_map, psm_map, pbe_map)
            if group_map.get(bid):
                entry["groups"] = self._build_groups(group_map[bid])
            entry["managed_by"] = _managed_by(bid, cpq_ids, psm_map)
            bundles_out.append(entry)

        all_out = products_out + bundles_out
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "meta": {
                "org":                        self.org_alias,
                "last_synced":                now,
                "products_count":             len(products_out),
                "bundles_count":              len(bundles_out),
                "catalogs_count":             len(catalogs_out),
                "selling_models_count":       len(selling_models_out),
                "price_adj_schedules_count":  len(price_adj_schedules_out),
                "cpq_only_count":             sum(1 for e in all_out if e.get("managed_by") == "cpq"),
                "rca_only_count":             sum(1 for e in all_out if e.get("managed_by") == "rca"),
                "both_count":                 sum(1 for e in all_out if e.get("managed_by") == "both"),
                "unmanaged_count":            sum(1 for e in all_out if e.get("managed_by") == "neither"),
            },
            "catalogs":                 catalogs_out,
            "selling_models":           selling_models_out,
            "price_adjustment_schedules": price_adj_schedules_out,
            "products":                 products_out,
            "bundles":                  bundles_out,
        }

    # ── Helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _cat_path(cat_id: str, cat_map: Dict, visited: set) -> str:
        """Return the full 'Parent > Child' path for a category."""
        if cat_id in visited or cat_id not in cat_map:
            return cat_map.get(cat_id, {}).get("name", cat_id)
        visited.add(cat_id)
        cat = cat_map[cat_id]
        if cat["parent_id"] and cat["parent_id"] in cat_map:
            return OrgSnapshotter._cat_path(cat["parent_id"], cat_map, visited) + " > " + cat["name"]
        return cat["name"]

    @staticmethod
    def _build_cat_tree(catalog_id: str, parent_id: Optional[str],
                        cat_map: Dict[str, Dict]) -> List[Dict]:
        """Recursively build a nested category list for one catalog."""
        children = sorted(
            [c for c in cat_map.values()
             if c["catalog_id"] == catalog_id and c["parent_id"] == parent_id],
            key=lambda c: c["name"],
        )
        result = []
        for c in children:
            node: Dict[str, Any] = {"name": c["name"], "sf_id": c["id"]}
            if c["code"]:
                node["code"] = c["code"]
            subs = OrgSnapshotter._build_cat_tree(catalog_id, c["id"], cat_map)
            if subs:
                node["categories"] = subs
            result.append(node)
        return result

    @staticmethod
    def _base_entry(r: Dict, pid: str,
                    cat_link_map: Dict, psm_map: Dict, pbe_map: Dict) -> Dict:
        """Build a base product/bundle entry dict from a Product2 record."""
        entry: Dict[str, Any] = {
            "code":   r.get("ProductCode") or "",
            "sf_id":  pid,
            "name":   r.get("Name") or "",
            "family": r.get("Family") or "",
            "active": bool(r.get("IsActive")),
        }
        if r.get("Description"):
            entry["description"] = r["Description"]
        if r.get("StockKeepingUnit"):
            entry["sku"] = r["StockKeepingUnit"]
        if r.get("QuantityUnitOfMeasure"):
            entry["uom"] = r["QuantityUnitOfMeasure"]
        link = cat_link_map.get(pid, {})
        if link.get("catalog"):
            entry["catalog"]  = link["catalog"]
            entry["category"] = link["category"]
        if psm_map.get(pid):
            entry["psm_options"] = psm_map[pid]
        if pbe_map.get(pid):
            entry["pricebook_entries"] = pbe_map[pid]
        return entry

    @staticmethod
    def _build_groups(groups: List[Dict]) -> List[Dict]:
        """Convert raw group dicts into the YAML-ready structure."""
        result = []
        for g in groups:
            gout: Dict[str, Any] = {
                "name":     g["name"],
                "sf_id":    g["id"],
                "sequence": g["sequence"],
            }
            if g["code"]:
                gout["code"] = g["code"]
            if g["min_selections"] is not None:
                gout["min_selections"] = int(g["min_selections"])
            if g["max_selections"] is not None:
                gout["max_selections"] = int(g["max_selections"])
            if g["components"]:
                gout["components"] = g["components"]
            result.append(gout)
        return result


# ---------------------------------------------------------------------------
# YAML writer with header comment
# ---------------------------------------------------------------------------
def write_snapshot(snapshot: Dict, output_path: str) -> None:
    abs_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    header = (
        "# RCA Org Snapshot — auto-generated, do not edit manually\n"
        "# Run /sync-rca-org to refresh\n"
        f"# Last synced: {snapshot['meta']['last_synced']}\n\n"
    )
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.dump(snapshot, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync RCA org state to a local YAML snapshot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--org",  "-o", metavar="ALIAS",
                        help="sf CLI org alias (omit for default)")
    parser.add_argument("--output", "-p", default=".rca/org-snapshot.yaml",
                        metavar="PATH",
                        help="Output path (default: .rca/org-snapshot.yaml)")
    parser.add_argument("--api-version", metavar="VER", default="62.0",
                        help="Salesforce API version (default: 62.0)")
    args = parser.parse_args()

    print(f"RCA Org Snapshot  →  {os.path.abspath(args.output)}")
    access_token, instance_url = get_sf_credentials(args.org)
    org_alias = args.org or "default"
    print(f"Connected to: {instance_url}\n")

    sf = SalesforceClient(access_token, instance_url, args.api_version)
    snapshot = OrgSnapshotter(sf, org_alias).build()
    write_snapshot(snapshot, args.output)

    m = snapshot["meta"]
    print(
        f"\n✓ Synced: {m['products_count']} products, {m['bundles_count']} bundles, "
        f"{m['catalogs_count']} catalogs, {m['selling_models_count']} selling models, "
        f"{m['price_adj_schedules_count']} price adjustment schedules"
        f"  →  {os.path.abspath(args.output)}"
    )


if __name__ == "__main__":
    main()
