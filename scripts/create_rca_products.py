#!/usr/bin/env python3
"""
RCA Product Creator
===================
Reads a YAML product catalog and creates the corresponding Salesforce
Revenue Cloud Advanced (ARM) records via REST API:

  0. ProductCatalog + ProductCategory  (catalogs section — hierarchy)
  1. Product2                    (products + bundles sections)
  1a. ProductSellingModel        (new_selling_models section — create missing PSMs)
  2. ProductSellingModelOption   (psm_options per product/bundle)
  3. PricebookEntry              (pricebook_entries per product/bundle)
  4. ProductGroup                (bundles → groups)
  5. ProductRelatedComponent     (bundles → groups → components)
  6. AttributePicklist + AttributePicklistValue + AttributeDefinition
  7. ProductClassification       (create if missing, link attr defs via
                                  ProductClassificationAttr, set Product2.BasedOnId)
  8. CatalogProduct              (link Product2 → ProductCategory)

Authentication uses `sf org display` — no passwords stored.

Usage:
  python create_rca_products.py [--catalog rca_catalog.yaml] [--org <alias>] [--dry-run]

Flags:
  --catalog  -c   Path to the YAML catalog file. Default: rca_catalog.yaml
  --org      -o   sf CLI org alias. Omit to use the default authenticated org.
  --dry-run       Preview every record that WOULD be created without touching the org.

Requirements:
  pip install requests pyyaml
"""

import json
import re
import subprocess
import sys
import os
import argparse
import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Salesforce REST client
# ---------------------------------------------------------------------------
class SalesforceClient:
    def __init__(self, access_token: str, instance_url: str,
                 api_version: str = "62.0", dry_run: bool = False,
                 create_only: bool = False, upsert: bool = False):
        self.instance_url = instance_url.rstrip("/")
        self.base_url = f"{self.instance_url}/services/data/v{api_version}"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run
        self.create_only = create_only
        self.upsert = upsert

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

    def create(self, sobject: str, payload: Dict) -> str:
        if self.dry_run:
            log.info("[DRY-RUN] Would create %s: %s", sobject, json.dumps(payload))
            return f"DRY-{sobject}-{abs(hash(json.dumps(payload, sort_keys=True))) % 10**9:09d}"
        resp = requests.post(f"{self.base_url}/sobjects/{sobject}",
                             headers=self.headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"Create {sobject} failed [{resp.status_code}]: {resp.text}\n"
                f"Payload: {json.dumps(payload)}"
            )
        record_id: str = resp.json()["id"]
        log.info("Created %-35s  Id: %s", sobject, record_id)
        return record_id

    def update(self, sobject: str, record_id: str, payload: Dict) -> None:
        if self.dry_run:
            log.info("[DRY-RUN] Would update %s %s: %s", sobject, record_id, json.dumps(payload))
            return
        if self.create_only:
            log.info("[CREATE-ONLY] Skipping update %s %s", sobject, record_id)
            return
        resp = requests.patch(f"{self.base_url}/sobjects/{sobject}/{record_id}",
                              headers=self.headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"Update {sobject} {record_id} failed [{resp.status_code}]: {resp.text}"
            )
        log.info("Updated %-35s  Id: %s", sobject, record_id)

    def delete(self, sobject: str, record_id: str) -> None:
        if self.dry_run:
            log.info("[DRY-RUN] Would delete %s %s", sobject, record_id)
            return
        if self.create_only:
            log.info("[CREATE-ONLY] Skipping delete %s %s", sobject, record_id)
            return
        resp = requests.delete(f"{self.base_url}/sobjects/{sobject}/{record_id}",
                               headers=self.headers, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"Delete {sobject} {record_id} failed [{resp.status_code}]: {resp.text}"
            )
        log.info("Deleted %-35s  Id: %s", sobject, record_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def load_catalog(path: str) -> Dict:
    if not os.path.isfile(path):
        log.error("Catalog file not found: %s", path)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f)
    if not catalog:
        log.error("Catalog is empty: %s", path)
        sys.exit(1)
    return catalog


def normalize_entry(entry: Dict) -> Dict:
    """Normalise a catalog entry to consistent key names."""
    return {
        "code":              str(entry.get("code", "")).strip(),
        "name":              str(entry.get("name", "")).strip(),
        "description":       str(entry.get("description", "")).strip(),
        "family":            str(entry.get("family", "")).strip(),
        "active":            entry.get("active", True),
        "uom":               str(entry.get("uom", "")).strip(),
        "sku":               str(entry.get("sku", "")).strip(),
        "psm_options":       entry.get("psm_options", []),
        "pricebook_entries": entry.get("pricebook_entries", []),
        "groups":            entry.get("groups", []),
        "classification":    str(entry.get("classification", "")).strip(),
        "attributes":        entry.get("attributes", []),
        "catalog":           str(entry.get("catalog", "")).strip(),
        "category":          str(entry.get("category", "")).strip(),
    }


# ---------------------------------------------------------------------------
# Main creator
# ---------------------------------------------------------------------------
class RCAProductCreator:
    def __init__(self, sf: SalesforceClient, catalog: Dict):
        self.sf = sf
        self.catalog = catalog  # raw dict — needed for catalogs section in Step 0
        self.products: List[Dict] = [normalize_entry(p) for p in catalog.get("products", [])]
        self.bundles:  List[Dict] = [normalize_entry(b) for b in catalog.get("bundles", [])]
        self.all_entries = self.products + self.bundles
        self.bundle_codes: set = {b["code"] for b in self.bundles if b["code"]}

        self.product_id_map:        Dict[str, str] = {}
        self.psm_id_map:            Dict[str, str] = {}
        self.pricebook_id_map:      Dict[str, str] = {}
        self.group_id_map:          Dict[str, str] = {}
        self.classification_id_map: Dict[str, str] = {}
        self.picklist_id_map:       Dict[str, str] = {}  # picklist_name → AttributePicklist.Id
        self.attr_def_id_map:       Dict[str, str] = {}

        self.catalog_id_map:    Dict[str, str] = {}  # catalog_name → ProductCatalog.Id
        self.category_id_map:   Dict[str, str] = {}  # path "A > B" and bare name → ProductCategory.Id

        self.attr_category_id_map:     Dict[str, str] = {}  # category_name → AttributeCategory.Id
        self.attr_category_assignment: Dict[str, str] = {}  # attr_name → category_name

        self.stats: Dict[str, Any] = {
            "catalogs_created": 0, "categories_created": 0, "catalog_products": 0,
            "products": 0, "products_updated": 0,
            "psm_options": 0,
            "pricebook_entries": 0, "pricebook_entries_updated": 0,
            "product_groups": 0, "components": 0, "components_updated": 0,
            "classifications_created": 0, "classifications": 0,
            "classification_attrs": 0,
            "attr_categories_created": 0,
            "picklists": 0, "picklist_values": 0, "attr_defs_created": 0,
            "psm_created": 0,
            "skipped": 0, "errors": [],
        }

    def run(self) -> None:
        self._step_preflight()
        self._step0_catalogs()
        self._step1_products()
        self._step_selling_models()
        self._step2_psm_options()
        self._step3_pricebook_entries()
        self._step4_bundles()
        self._step5_attributes()
        self._step6_classifications()
        self._step7_catalog_products()
        self._print_summary()

    # ── Pre-flight: audit existing records that would be UPDATED ────────
    def _step_preflight(self) -> None:
        """Query existing Product2 records and surface any that would be modified.

        Prompts the user to confirm, skip updates, or cancel before any org
        changes are made. Skipped in dry-run (audit shown, no prompt).
        """
        codes = [e["code"] for e in self.all_entries if e["code"]]
        if not codes:
            return

        escaped = ", ".join(f"'{c}'" for c in codes)
        existing = self.sf.query(
            f"SELECT Id, ProductCode, Name, Type, BasedOnId "
            f"FROM Product2 WHERE ProductCode IN ({escaped})"
        )
        if not existing:
            return  # nothing exists yet — all creates, no updates possible

        existing_map = {r["ProductCode"]: r for r in existing}

        # Resolve current classification names for any existing BasedOnId values
        existing_class_ids = {
            r["BasedOnId"] for r in existing if r.get("BasedOnId")
        }
        existing_class_names: Dict[str, str] = {}
        if existing_class_ids and not self.sf.dry_run:
            id_list = ", ".join(f"'{i}'" for i in existing_class_ids)
            for rec in self.sf.query(
                f"SELECT Id, Name FROM ProductClassification WHERE Id IN ({id_list})"
            ):
                existing_class_names[rec["Id"]] = rec["Name"]

        # Build list of pending updates
        updates = []
        for entry in self.all_entries:
            code = entry["code"]
            if code not in existing_map:
                continue
            rec = existing_map[code]

            # Type → Bundle
            if code in self.bundle_codes and rec.get("Type") != "Bundle":
                updates.append({
                    "code":    code,
                    "field":   "Type",
                    "current": rec.get("Type") or "(none)",
                    "new":     "Bundle",
                })

            # BasedOnId (classification) — only flag when overwriting a DIFFERENT value
            if entry.get("classification"):
                current_id   = rec.get("BasedOnId") or ""
                current_name = existing_class_names.get(current_id, current_id)
                target_name  = entry["classification"]
                if current_id and current_name != target_name:
                    updates.append({
                        "code":    code,
                        "field":   "Classification (BasedOnId)",
                        "current": current_name,
                        "new":     target_name,
                    })

        if not updates:
            return  # no modifications needed — safe to proceed

        # ── Display audit ──────────────────────────────────────────────
        log.warning("══════════════════════════════════════════════════")
        log.warning("PRE-FLIGHT AUDIT — %d existing record(s) would be MODIFIED:",
                    len(updates))
        log.warning("  %-25s  %-35s  %-20s  ->  %s",
                    "ProductCode", "Field", "Current Value", "New Value")
        log.warning("  %s", "-" * 90)
        for u in updates:
            log.warning("  %-25s  %-35s  %-20s  ->  %s",
                        u["code"], u["field"], u["current"], u["new"])
        log.warning("══════════════════════════════════════════════════")

        if self.sf.dry_run:
            log.info("[DRY-RUN] No prompt in dry-run mode — updates shown above would be applied on a live run.")
            return

        if self.sf.create_only:
            log.info("[CREATE-ONLY] Updates above will be skipped.")
            return

        # ── Interactive prompt ─────────────────────────────────────────
        print()
        while True:
            try:
                answer = input(
                    "Proceed?\n"
                    "  yes            — run all creates AND the updates listed above\n"
                    "  skip-updates   — run creates only, leave existing records untouched\n"
                    "  cancel         — abort without making any changes\n"
                    "> "
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                answer = "cancel"

            if answer in ("yes", "y"):
                log.info("Confirmed — proceeding with creates and updates.")
                break
            elif answer in ("skip-updates", "skip"):
                self.sf.create_only = True
                log.info("Updates skipped — running in create-only mode.")
                break
            elif answer in ("cancel", "no", "n", ""):
                log.info("Aborted by user.")
                sys.exit(0)
            else:
                print("Please enter 'yes', 'skip-updates', or 'cancel'.")

    # ── Step 0: ProductCatalog + ProductCategory ─────────────────────────
    def _step0_catalogs(self) -> None:
        catalog_defs = self.catalog.get("catalogs", [])

        # Also collect any catalog/category names referenced directly on entries
        # so we can resolve org-existing catalogs even if not defined in the YAML.
        ref_catalog_names: set = set()
        for entry in self.all_entries:
            if entry.get("catalog"):
                ref_catalog_names.add(entry["catalog"].strip())

        if not catalog_defs and not ref_catalog_names:
            return

        log.info("──────────────────────────────────────────────────")
        log.info("STEP 0 › ProductCatalog + ProductCategory")

        # ── Phase A: resolve/create top-level ProductCatalog records ──────
        catalog_names_to_resolve: set = {
            str(c.get("name", "")).strip()
            for c in catalog_defs
            if c.get("name", "").strip()
        } | ref_catalog_names

        for name in catalog_names_to_resolve:
            if name in self.catalog_id_map:
                continue
            safe = name.replace("'", "\\'")
            recs = self.sf.query(
                f"SELECT Id FROM ProductCatalog WHERE Name = '{safe}' LIMIT 1"
            )
            if not recs:
                # Try by Code field (catalog_defs may carry a code)
                code = next(
                    (str(c.get("code", "")).strip() for c in catalog_defs
                     if c.get("name", "").strip() == name),
                    ""
                )
                if code:
                    safe_code = code.replace("'", "\\'")
                    recs = self.sf.query(
                        f"SELECT Id FROM ProductCatalog WHERE Code = '{safe_code}' LIMIT 1"
                    )
            if recs:
                self.catalog_id_map[name] = recs[0]["Id"]
                log.info("ProductCatalog exists — using: %s", name)
            else:
                # Build creation payload from catalog_defs entry, or minimal defaults
                defn = next(
                    (c for c in catalog_defs if c.get("name", "").strip() == name),
                    {}
                )
                payload: Dict[str, Any] = {"Name": name}
                if defn.get("code"):
                    payload["Code"] = str(defn["code"]).strip()
                try:
                    cat_id = self.sf.create("ProductCatalog", payload)
                    self.catalog_id_map[name] = cat_id
                    self.stats["catalogs_created"] += 1
                except RuntimeError as exc:
                    msg = f"ProductCatalog '{name}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

        # ── Phase B: recursively resolve/create ProductCategory records ───
        for defn in catalog_defs:
            cat_name = str(defn.get("name", "")).strip()
            if not cat_name or cat_name not in self.catalog_id_map:
                continue
            catalog_id = self.catalog_id_map[cat_name]
            self._resolve_categories(
                defn.get("categories", []),
                catalog_id=catalog_id,
                parent_id=None,
                path_prefix="",
            )

        # ── Phase C: resolve any remaining category references from entries ─
        # For categories referenced on products that weren't in the catalogs block,
        # attempt a name-based lookup in the org.
        for entry in self.all_entries:
            cat_ref = str(entry.get("category", "")).strip()
            if not cat_ref or cat_ref in self.category_id_map:
                continue
            catalog_name = str(entry.get("catalog", "")).strip()
            catalog_id   = self.catalog_id_map.get(catalog_name, "")

            # Skip org query when working with synthetic dry-run IDs
            if catalog_id.startswith("DRY-"):
                log.info("[DRY-RUN] Category '%s' assumed resolved via dry-run create.", cat_ref)
                continue

            safe = cat_ref.replace("'", "\\'")
            if catalog_id:
                recs = self.sf.query(
                    f"SELECT Id FROM ProductCategory "
                    f"WHERE Name = '{safe}' AND CatalogId = '{catalog_id}' LIMIT 1"
                )
            else:
                recs = self.sf.query(
                    f"SELECT Id FROM ProductCategory WHERE Name = '{safe}' LIMIT 1"
                )
            if recs:
                self.category_id_map[cat_ref] = recs[0]["Id"]
                log.info("ProductCategory resolved from org: %s", cat_ref)
            else:
                log.warning(
                    "ProductCategory '%s' not found in org or catalogs section — "
                    "CatalogProduct link will be skipped for products referencing it.", cat_ref
                )

    def _resolve_categories(
        self,
        categories: List[Dict],
        catalog_id: str,
        parent_id: Optional[str],
        path_prefix: str,
    ) -> None:
        """Recursively resolve or create ProductCategory records, building the path map."""
        for cat in categories:
            name = str(cat.get("name", "")).strip()
            if not name:
                continue

            path = f"{path_prefix} > {name}" if path_prefix else name
            safe = name.replace("'", "\\'")

            # In dry-run mode, synthetic IDs (DRY-*) can't be used in SOQL.
            # Skip the lookup and fall through to the dry-run create path.
            is_synthetic_id = catalog_id.startswith("DRY-") or (
                parent_id is not None and parent_id.startswith("DRY-")
            )

            # Look up in org (only when IDs are real)
            recs = []
            if not is_synthetic_id:
                if parent_id:
                    recs = self.sf.query(
                        f"SELECT Id FROM ProductCategory "
                        f"WHERE Name = '{safe}' AND CatalogId = '{catalog_id}' "
                        f"AND ParentCategoryId = '{parent_id}' LIMIT 1"
                    )
                else:
                    recs = self.sf.query(
                        f"SELECT Id FROM ProductCategory "
                        f"WHERE Name = '{safe}' AND CatalogId = '{catalog_id}' "
                        f"AND ParentCategoryId = null LIMIT 1"
                    )

            if recs:
                cat_id = recs[0]["Id"]
                log.info("ProductCategory exists — using: %s", path)
            else:
                payload: Dict[str, Any] = {
                    "Name":      name,
                    "CatalogId": catalog_id,
                }
                if cat.get("code"):
                    payload["Code"] = str(cat["code"]).strip()
                if parent_id:
                    payload["ParentCategoryId"] = parent_id

                try:
                    cat_id = self.sf.create("ProductCategory", payload)
                    self.stats["categories_created"] += 1
                except RuntimeError as exc:
                    msg = f"ProductCategory '{path}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)
                    continue

            # Register under both the full path and the bare name.
            # In dry-run the cat_id is a synthetic DRY-* value — that's fine,
            # it lets Step 7 log what would be linked without an org round-trip.
            self.category_id_map[path] = cat_id
            if name not in self.category_id_map:
                self.category_id_map[name] = cat_id

            # Recurse into child categories
            self._resolve_categories(
                cat.get("categories", []),
                catalog_id=catalog_id,
                parent_id=cat_id,
                path_prefix=path,
            )

    # ── Step 1: Product2 ────────────────────────────────────────────────
    def _step1_products(self) -> None:
        log.info("──────────────────────────────────────────────────")
        log.info("STEP 1 › Creating Product2 records (%d total)", len(self.all_entries))
        if not self.all_entries:
            return

        codes = [e["code"] for e in self.all_entries if e["code"]]
        # Also include component codes referenced in bundles so product_id_map
        # covers components that aren't themselves in the current catalog file.
        component_codes = [
            str(comp.get("code", "")).strip()
            for b in self.bundles
            for g in b.get("groups", [])
            for comp in g.get("components", [])
            if comp.get("code")
        ]
        all_codes = list(dict.fromkeys(codes + component_codes))  # dedup, preserve order
        if all_codes:
            escaped = ", ".join(f"'{c}'" for c in all_codes)
            for rec in self.sf.query(
                f"SELECT Id, ProductCode, Type, Name, Description, Family, IsActive, "
                f"QuantityUnitOfMeasure, StockKeepingUnit "
                f"FROM Product2 WHERE ProductCode IN ({escaped})"
            ):
                code = rec["ProductCode"]
                self.product_id_map[code] = rec["Id"]

                # Always fix Type → Bundle if needed
                if code in self.bundle_codes and rec.get("Type") != "Bundle":
                    try:
                        self.sf.update("Product2", rec["Id"], {"Type": "Bundle"})
                        log.info("Set Type=Bundle on existing Product2: %s", code)
                    except RuntimeError as exc:
                        log.error("Could not set Type=Bundle on %s: %s", code, exc)

                if self.sf.upsert:
                    # Find the catalog entry for this code and diff updatable fields
                    entry = next((e for e in self.all_entries if e["code"] == code), None)
                    if entry:
                        field_map = [
                            ("name",        "Name"),
                            ("description", "Description"),
                            ("family",      "Family"),
                            ("uom",         "QuantityUnitOfMeasure"),
                            ("sku",         "StockKeepingUnit"),
                        ]
                        update_payload: Dict[str, Any] = {}
                        for src, dest in field_map:
                            new_val = entry.get(src) or ""
                            cur_val = rec.get(dest) or ""
                            if new_val and new_val != cur_val:
                                update_payload[dest] = new_val
                        cur_active = bool(rec.get("IsActive"))
                        new_active = bool(entry.get("active", True))
                        if new_active != cur_active:
                            update_payload["IsActive"] = new_active
                        if update_payload:
                            try:
                                self.sf.update("Product2", rec["Id"], update_payload)
                                self.stats["products_updated"] += 1
                                log.info("Updated Product2 %s: %s", code,
                                         list(update_payload.keys()))
                            except RuntimeError as exc:
                                msg = f"Update Product2 {code}: {exc}"
                                log.error(msg)
                                self.stats["errors"].append(msg)
                        else:
                            log.info("Product2 up to date — no changes: %s", code)
                            self.stats["skipped"] += 1
                else:
                    log.info("Already exists — skipping Product2: %s", code)
                    self.stats["skipped"] += 1

        for entry in self.all_entries:
            code = entry["code"]
            if not code:
                log.warning("Entry missing 'code' — skipped: %s", entry.get("name"))
                continue
            if code in self.product_id_map:
                continue

            payload: Dict[str, Any] = {
                "ProductCode": code,
                "Name": entry["name"] or code,
                "IsActive": bool(entry["active"]),
            }
            if code in self.bundle_codes:
                payload["Type"] = "Bundle"
                payload["ConfigureDuringSale"] = "Allowed"
            for src, dest in [("description", "Description"), ("family", "Family"),
                               ("uom", "QuantityUnitOfMeasure"), ("sku", "StockKeepingUnit")]:
                if entry.get(src):
                    payload[dest] = entry[src]

            try:
                pid = self.sf.create("Product2", payload)
                self.product_id_map[code] = pid
                self.stats["products"] += 1
            except RuntimeError as exc:
                msg = f"Product2 {code}: {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)

    # ── Step 1a: ProductSellingModel (create missing PSMs) ──────────────
    def _step_selling_models(self) -> None:
        """Create ProductSellingModel records listed in new_selling_models.

        Matches by name first, then by (SellingModelType + PricingTermUnit).
        Creates only if neither lookup finds an existing record.
        Populates psm_id_map so _step2_psm_options can resolve names normally.
        """
        new_psms = self.catalog.get("new_selling_models", [])
        if not new_psms:
            return

        log.info("──────────────────────────────────────────────────")
        log.info("STEP 1a › ProductSellingModel records (%d to resolve)", len(new_psms))

        for psm_def in new_psms:
            name             = str(psm_def.get("name", "")).strip()
            psm_type         = str(psm_def.get("type", "")).strip()
            pricing_term     = psm_def.get("pricing_term", 1)
            pricing_term_unit = str(psm_def.get("pricing_term_unit", "")).strip()

            if not name:
                log.warning("new_selling_models entry missing 'name' — skipping: %s", psm_def)
                continue
            if name in self.psm_id_map:
                continue

            # 1. Lookup by name
            safe = name.replace("'", "\\'")
            recs = self.sf.query(
                f"SELECT Id FROM ProductSellingModel WHERE Name = '{safe}' LIMIT 1"
            )
            if recs:
                log.info("ProductSellingModel exists — using: %s", name)
                self.psm_id_map[name] = recs[0]["Id"]
                self.stats["skipped"] += 1
                continue

            # 2. Fallback: match by SellingModelType + PricingTermUnit
            if psm_type and pricing_term_unit:
                safe_type = psm_type.replace("'", "\\'")
                safe_unit = pricing_term_unit.replace("'", "\\'")
                recs = self.sf.query(
                    f"SELECT Id, Name FROM ProductSellingModel "
                    f"WHERE SellingModelType = '{safe_type}' "
                    f"AND PricingTermUnit = '{safe_unit}' "
                    f"AND Status = 'Active' LIMIT 1"
                )
                if recs:
                    found_name = recs[0].get("Name", name)
                    log.info(
                        "ProductSellingModel matched by type+unit: '%s' → '%s'",
                        name, found_name,
                    )
                    self.psm_id_map[name]       = recs[0]["Id"]
                    self.psm_id_map[found_name] = recs[0]["Id"]
                    self.stats["skipped"] += 1
                    continue

            # 3. Create
            payload: Dict[str, Any] = {"Name": name, "Status": "Active"}
            if psm_type:
                payload["SellingModelType"] = psm_type
            if pricing_term is not None:
                payload["PricingTerm"] = pricing_term
            if pricing_term_unit:
                payload["PricingTermUnit"] = pricing_term_unit
            try:
                psm_id = self.sf.create("ProductSellingModel", payload)
                self.psm_id_map[name] = psm_id
                self.stats["psm_created"] += 1
            except RuntimeError as exc:
                msg = f"ProductSellingModel '{name}': {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)

    # ── Step 2: ProductSellingModelOption ───────────────────────────────
    def _step2_psm_options(self) -> None:
        log.info("──────────────────────────────────────────────────")
        log.info("STEP 2 › Creating ProductSellingModelOption records")

        all_psm_names = list({
            psm for entry in self.all_entries
            for psm in entry.get("psm_options", [])
            if isinstance(psm, str) and psm.strip()
        })
        for name in all_psm_names:
            if name in self.psm_id_map:
                continue  # already resolved by _step_selling_models
            recs = self.sf.query(
                f"SELECT Id FROM ProductSellingModel WHERE Name = '{name}' LIMIT 1"
            )
            if recs:
                self.psm_id_map[name] = recs[0]["Id"]
            else:
                log.warning(
                    "ProductSellingModel '%s' not found — "
                    "create it in Setup › Revenue › Selling Models first.", name
                )

        existing_options: set = set()
        if self.product_id_map and not self.sf.dry_run:
            prod_ids = ", ".join(f"'{v}'" for v in self.product_id_map.values())
            for rec in self.sf.query(
                f"SELECT Product2Id, ProductSellingModelId "
                f"FROM ProductSellingModelOption WHERE Product2Id IN ({prod_ids})"
            ):
                existing_options.add((rec["Product2Id"], rec["ProductSellingModelId"]))

        for entry in self.all_entries:
            code = entry["code"]
            if code not in self.product_id_map:
                continue
            prod_id = self.product_id_map[code]

            for psm_name in entry.get("psm_options", []):
                if not isinstance(psm_name, str) or not psm_name.strip():
                    continue
                if psm_name not in self.psm_id_map:
                    self.stats["skipped"] += 1
                    continue
                psm_id = self.psm_id_map[psm_name]
                if (prod_id, psm_id) in existing_options:
                    log.info("PSM option exists — skipping: %s / %s", code, psm_name)
                    self.stats["skipped"] += 1
                    continue
                try:
                    self.sf.create("ProductSellingModelOption",
                                   {"Product2Id": prod_id, "ProductSellingModelId": psm_id})
                    self.stats["psm_options"] += 1
                    existing_options.add((prod_id, psm_id))
                except RuntimeError as exc:
                    msg = f"PSM Option {code}/{psm_name}: {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

    # ── Step 3: PricebookEntry ───────────────────────────────────────────
    def _step3_pricebook_entries(self) -> None:
        log.info("──────────────────────────────────────────────────")
        log.info("STEP 3 › Creating PricebookEntry records")

        all_pb_names = list({
            pbe.get("pricebook", "")
            for entry in self.all_entries
            for pbe in entry.get("pricebook_entries", [])
            if pbe.get("pricebook", "").strip()
        })
        for name in all_pb_names:
            if name.lower() in ("standard price book", "standard pricebook"):
                recs = self.sf.query("SELECT Id FROM Pricebook2 WHERE IsStandard = true LIMIT 1")
            else:
                safe = name.replace("'", "\\'")
                recs = self.sf.query(
                    f"SELECT Id FROM Pricebook2 WHERE Name = '{safe}' AND IsActive = true LIMIT 1"
                )
            if recs:
                self.pricebook_id_map[name] = recs[0]["Id"]
            else:
                log.warning("Pricebook '%s' not found — entries will be skipped.", name)

        # existing_pbe: (prod_id, pb_id, currency) → (pbe_id, current_price)
        # Stored as dict so upsert mode can look up the record Id and current price.
        existing_pbe: Dict[tuple, tuple] = {}
        multi_currency = False
        if self.product_id_map and not self.sf.dry_run:
            prod_ids = ", ".join(f"'{v}'" for v in self.product_id_map.values())
            try:
                for rec in self.sf.query(
                    f"SELECT Id, Product2Id, Pricebook2Id, CurrencyIsoCode, UnitPrice "
                    f"FROM PricebookEntry WHERE Product2Id IN ({prod_ids})"
                ):
                    key = (rec["Product2Id"], rec["Pricebook2Id"], rec["CurrencyIsoCode"])
                    existing_pbe[key] = (rec["Id"], float(rec.get("UnitPrice") or 0))
                multi_currency = True
            except Exception:
                for rec in self.sf.query(
                    f"SELECT Id, Product2Id, Pricebook2Id, UnitPrice "
                    f"FROM PricebookEntry WHERE Product2Id IN ({prod_ids})"
                ):
                    key = (rec["Product2Id"], rec["Pricebook2Id"], "USD")
                    existing_pbe[key] = (rec["Id"], float(rec.get("UnitPrice") or 0))

        for entry in self.all_entries:
            code = entry["code"]
            if code not in self.product_id_map:
                continue
            prod_id = self.product_id_map[code]

            pbes = sorted(
                entry.get("pricebook_entries", []),
                key=lambda x: 0 if "standard" in x.get("pricebook", "").lower() else 1,
            )
            for pbe in pbes:
                pb_name  = pbe.get("pricebook", "").strip()
                currency = str(pbe.get("currency", "USD")).strip() or "USD"
                if not pb_name or pb_name not in self.pricebook_id_map:
                    self.stats["skipped"] += 1
                    continue
                pb_id   = self.pricebook_id_map[pb_name]
                pbe_key = (prod_id, pb_id, currency)
                new_price = float(pbe.get("price", 0))

                if pbe_key in existing_pbe:
                    pbe_id, cur_price = existing_pbe[pbe_key]
                    if self.sf.upsert and new_price != cur_price:
                        try:
                            self.sf.update("PricebookEntry", pbe_id, {"UnitPrice": new_price})
                            self.stats["pricebook_entries_updated"] += 1
                            log.info("Updated PricebookEntry %s / %s: %.2f → %.2f",
                                     code, pb_name, cur_price, new_price)
                        except RuntimeError as exc:
                            msg = f"Update PricebookEntry {code}/{pb_name}: {exc}"
                            log.error(msg)
                            self.stats["errors"].append(msg)
                    else:
                        log.info("PricebookEntry exists — skipping: %s / %s", code, pb_name)
                        self.stats["skipped"] += 1
                    continue

                payload: Dict[str, Any] = {
                    "Product2Id":   prod_id,
                    "Pricebook2Id": pb_id,
                    "UnitPrice":    new_price,
                    "IsActive":     True,
                }
                if multi_currency:
                    payload["CurrencyIsoCode"] = currency
                try:
                    self.sf.create("PricebookEntry", payload)
                    self.stats["pricebook_entries"] += 1
                    existing_pbe[pbe_key] = (f"NEW-{code}", new_price)
                except RuntimeError as exc:
                    msg = f"PricebookEntry {code}/{pb_name}: {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

    # ── Step 4: ProductGroup + ProductRelatedComponent ───────────────────
    def _step4_bundles(self) -> None:
        log.info("──────────────────────────────────────────────────")
        log.info("STEP 4 › Creating Bundle structures (%d bundles)", len(self.bundles))
        if not self.bundles:
            return

        # Resolve the bundle-component relationship type once
        bundle_rel_type_id: Optional[str] = None
        try:
            recs = self.sf.query(
                "SELECT Id FROM ProductRelationshipType "
                "WHERE Name = 'Bundle to Bundle Component Relationship' LIMIT 1"
            )
            if recs:
                bundle_rel_type_id = recs[0]["Id"]
            else:
                log.warning("ProductRelationshipType 'Bundle to Bundle Component Relationship' "
                            "not found — components will fail to create.")
        except Exception as exc:
            log.warning("Could not query ProductRelationshipType: %s", exc)

        for bundle in self.bundles:
            bundle_code = bundle["code"]
            if bundle_code not in self.product_id_map:
                log.warning("Bundle %s not in product map — skipping groups.", bundle_code)
                continue
            bundle_id = self.product_id_map[bundle_code]

            for group in bundle.get("groups", []):
                group_name = str(group.get("name", "")).strip()
                group_code = str(group.get("code", "")).strip()
                group_key  = f"{bundle_code}::{group_name}"

                if not group_name:
                    continue

                safe_gn  = group_name.replace("'", "\\'")
                try:
                    existing = [] if self.sf.dry_run else self.sf.query(
                        f"SELECT Id, MinBundleComponents, MaxBundleComponents, Sequence "
                        f"FROM ProductComponentGroup "
                        f"WHERE Name = '{safe_gn}' AND ParentProductId = '{bundle_id}' LIMIT 1"
                    )
                except Exception:
                    existing = []
                if existing:
                    pg_id = existing[0]["Id"]
                    self.group_id_map[group_key] = pg_id
                    if self.sf.upsert:
                        grp_update: Dict[str, Any] = {}
                        if (group.get("min_selections") is not None and
                                int(group["min_selections"]) != (existing[0].get("MinBundleComponents") or 0)):
                            grp_update["MinBundleComponents"] = int(group["min_selections"])
                        if (group.get("max_selections") is not None and
                                int(group["max_selections"]) != (existing[0].get("MaxBundleComponents") or 0)):
                            grp_update["MaxBundleComponents"] = int(group["max_selections"])
                        if int(group.get("sequence", 1)) != (existing[0].get("Sequence") or 1):
                            grp_update["Sequence"] = int(group.get("sequence", 1))
                        if grp_update:
                            try:
                                self.sf.update("ProductComponentGroup", pg_id, grp_update)
                                log.info("Updated ProductComponentGroup %s / %s: %s",
                                         bundle_code, group_name, list(grp_update.keys()))
                            except RuntimeError as exc:
                                log.error("Update ProductComponentGroup %s/%s: %s",
                                          bundle_code, group_name, exc)
                        else:
                            log.info("ProductComponentGroup up to date — no changes: %s / %s",
                                     bundle_code, group_name)
                            self.stats["skipped"] += 1
                    else:
                        log.info("ProductComponentGroup exists — skipping: %s / %s", bundle_code, group_name)
                        self.stats["skipped"] += 1
                else:
                    payload: Dict[str, Any] = {
                        "Name":            group_name,
                        "ParentProductId": bundle_id,
                        "Sequence":        int(group.get("sequence", 1)),
                    }
                    if group_code:
                        payload["Code"] = f"{bundle_code}-{group_code}"[:80]
                    if group.get("min_selections") is not None:
                        payload["MinBundleComponents"] = int(group["min_selections"])
                    if group.get("max_selections") is not None:
                        payload["MaxBundleComponents"] = int(group["max_selections"])

                    try:
                        pg_id = self.sf.create("ProductComponentGroup", payload)
                        self.group_id_map[group_key] = pg_id
                        self.stats["product_groups"] += 1
                    except RuntimeError as exc:
                        msg = f"ProductComponentGroup '{group_name}' for {bundle_code}: {exc}"
                        log.error(msg)
                        self.stats["errors"].append(msg)
                        continue

                for seq_j, comp in enumerate(group.get("components", []), start=1):
                    comp_code = str(comp.get("code", "")).strip()
                    if not comp_code or comp_code not in self.product_id_map:
                        log.warning("Component '%s' not found — skipping.", comp_code)
                        self.stats["skipped"] += 1
                        continue

                    comp_id = self.product_id_map[comp_code]
                    pg_id   = self.group_id_map.get(group_key)
                    if not pg_id:
                        continue

                    try:
                        existing_comp = [] if self.sf.dry_run else self.sf.query(
                            f"SELECT Id, IsComponentRequired, IsDefaultComponent, "
                            f"Sequence, MinQuantity, MaxQuantity "
                            f"FROM ProductRelatedComponent "
                            f"WHERE ParentProductId = '{bundle_id}' "
                            f"AND ChildProductId = '{comp_id}' "
                            f"AND ProductComponentGroupId = '{pg_id}' LIMIT 1"
                        )
                    except Exception:
                        existing_comp = []
                    if existing_comp:
                        if self.sf.upsert:
                            ec = existing_comp[0]
                            comp_update: Dict[str, Any] = {}
                            if bool(comp.get("required", False)) != bool(ec.get("IsComponentRequired")):
                                comp_update["IsComponentRequired"] = bool(comp.get("required", False))
                            if bool(comp.get("default", False)) != bool(ec.get("IsDefaultComponent")):
                                comp_update["IsDefaultComponent"] = bool(comp.get("default", False))
                            if int(comp.get("sequence", seq_j)) != (ec.get("Sequence") or seq_j):
                                comp_update["Sequence"] = int(comp.get("sequence", seq_j))
                            if float(comp.get("min_qty", 0)) != float(ec.get("MinQuantity") or 0):
                                comp_update["MinQuantity"] = float(comp.get("min_qty", 0))
                            if float(comp.get("max_qty", 0)) != float(ec.get("MaxQuantity") or 0):
                                comp_update["MaxQuantity"] = float(comp.get("max_qty", 0))
                            if comp_update:
                                try:
                                    self.sf.update("ProductRelatedComponent", ec["Id"], comp_update)
                                    self.stats["components_updated"] += 1
                                    log.info("Updated component %s › %s: %s",
                                             bundle_code, comp_code, list(comp_update.keys()))
                                except RuntimeError as exc:
                                    msg = f"Update component {comp_code} in {bundle_code}: {exc}"
                                    log.error(msg)
                                    self.stats["errors"].append(msg)
                            else:
                                log.info("Component up to date — no changes: %s › %s",
                                         bundle_code, comp_code)
                                self.stats["skipped"] += 1
                        else:
                            log.info("Component exists — skipping: %s › %s", bundle_code, comp_code)
                            self.stats["skipped"] += 1
                        continue

                    try:
                        comp_payload: Dict[str, Any] = {
                            "ParentProductId":         bundle_id,
                            "ChildProductId":          comp_id,
                            "ProductComponentGroupId": pg_id,
                            "IsComponentRequired":     bool(comp.get("required", False)),
                            "IsDefaultComponent":      bool(comp.get("default", False)),
                            "Sequence":                int(comp.get("sequence", seq_j)),
                        }
                        comp_payload["IsQuantityEditable"] = True
                        comp_payload["MinQuantity"] = float(comp.get("min_qty", 0))
                        comp_payload["MaxQuantity"] = float(comp.get("max_qty", 0))
                        if bundle_rel_type_id:
                            comp_payload["ProductRelationshipTypeId"] = bundle_rel_type_id
                        self.sf.create("ProductRelatedComponent", comp_payload)
                        self.stats["components"] += 1
                    except RuntimeError as exc:
                        msg = f"Component {comp_code} in {bundle_code}/{group_name}: {exc}"
                        log.error(msg)
                        self.stats["errors"].append(msg)

    # ── Step 6: ProductClassification (create → link attrs → link product) ─
    def _step6_classifications(self) -> None:
        entries_with_class = [e for e in self.all_entries if e.get("classification")]
        if not entries_with_class:
            return
        log.info("──────────────────────────────────────────────────")
        log.info("STEP 6 › ProductClassification — create, link attributes, link products (%d)",
                 len(entries_with_class))

        # ── Phase A: resolve or create each unique ProductClassification ──
        unique_names = list({e["classification"] for e in entries_with_class})
        for name in unique_names:
            safe = name.replace("'", "\\'")
            recs = self.sf.query(
                f"SELECT Id FROM ProductClassification WHERE Name = '{safe}' AND Status = 'Active' LIMIT 1"
            )
            if not recs:
                recs = self.sf.query(
                    f"SELECT Id FROM ProductClassification WHERE Code = '{safe}' AND Status = 'Active' LIMIT 1"
                )
            if recs:
                self.classification_id_map[name] = recs[0]["Id"]
                log.info("ProductClassification exists — using: %s", name)
            else:
                try:
                    code = re.sub(r"[^A-Z0-9_]", "_", name.upper())[:40]
                    class_id = self.sf.create("ProductClassification",
                                              {"Name": name, "Code": code, "Status": "Active"})
                    self.classification_id_map[name] = class_id
                    self.stats["classifications_created"] += 1
                except RuntimeError as exc:
                    msg = f"ProductClassification '{name}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

        # ── Phase B: link AttributeDefinitions → ProductClassification ───
        # Build map: classification_name → {attr_name: sequence}
        class_attr_map: Dict[str, Dict[str, int]] = {}
        for entry in entries_with_class:
            cls = entry["classification"]
            if cls not in class_attr_map:
                class_attr_map[cls] = {}
            for seq_j, attr in enumerate(entry.get("attributes", []), start=1):
                attr_name = (attr.get("name") or attr.get("code", "")).strip()
                if attr_name and attr_name not in class_attr_map[cls]:
                    class_attr_map[cls][attr_name] = seq_j

        for cls_name, attr_seq in class_attr_map.items():
            if cls_name not in self.classification_id_map:
                continue
            class_id = self.classification_id_map[cls_name]

            # Dict: attr_def_id → (record_id, current_category_id)
            existing_links: Dict[str, tuple] = {}
            if not self.sf.dry_run:
                try:
                    for rec in self.sf.query(
                        f"SELECT Id, AttributeDefinitionId, AttributeCategoryId "
                        f"FROM ProductClassificationAttr "
                        f"WHERE ProductClassificationId = '{class_id}'"
                    ):
                        existing_links[rec["AttributeDefinitionId"]] = (
                            rec["Id"], rec.get("AttributeCategoryId")
                        )
                except Exception:
                    pass  # object may not exist yet in the org schema

            for attr_name, seq in attr_seq.items():
                if attr_name not in self.attr_def_id_map:
                    log.warning("AttributeDefinition '%s' not resolved — skipping link to %s.",
                                attr_name, cls_name)
                    continue
                attr_def_id = self.attr_def_id_map[attr_name]

                # Resolve attribute category for this attr (if any)
                attr_cat_name = self.attr_category_assignment.get(attr_name, "")
                attr_cat_id   = self.attr_category_id_map.get(attr_cat_name) if attr_cat_name else None

                if attr_def_id in existing_links:
                    link_id, cur_cat_id = existing_links[attr_def_id]
                    if self.sf.upsert and attr_cat_id and attr_cat_id != cur_cat_id:
                        # AttributeCategoryId is not patchable — delete and recreate
                        try:
                            self.sf.delete("ProductClassificationAttr", link_id)
                            new_payload: Dict[str, Any] = {
                                "Name":                    attr_name,
                                "ProductClassificationId": class_id,
                                "AttributeDefinitionId":   attr_def_id,
                                "Sequence":                seq,
                                "AttributeCategoryId":     attr_cat_id,
                            }
                            self.sf.create("ProductClassificationAttr", new_payload)
                            log.info("Replaced classification attr with category: %s / %s → %s",
                                     cls_name, attr_name, attr_cat_name)
                            self.stats["classification_attrs"] += 1
                            existing_links[attr_def_id] = (f"NEW-{attr_name}", attr_cat_id)
                        except RuntimeError as exc:
                            msg = f"Replace ProductClassificationAttr {cls_name}/{attr_name}: {exc}"
                            log.error(msg)
                            self.stats["errors"].append(msg)
                    else:
                        log.info("ProductClassificationAttr exists — skipping: %s / %s",
                                 cls_name, attr_name)
                        self.stats["skipped"] += 1
                    continue

                cls_attr_payload: Dict[str, Any] = {
                    "Name":                    attr_name,
                    "ProductClassificationId": class_id,
                    "AttributeDefinitionId":   attr_def_id,
                    "Sequence":                seq,
                }
                if attr_cat_id:
                    cls_attr_payload["AttributeCategoryId"] = attr_cat_id

                try:
                    self.sf.create("ProductClassificationAttr", cls_attr_payload)
                    self.stats["classification_attrs"] += 1
                    existing_links[attr_def_id] = (f"NEW-{attr_name}", attr_cat_id)
                except RuntimeError as exc:
                    msg = f"ProductClassificationAttr {cls_name}/{attr_name}: {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

        # ── Phase C: set Product2.BasedOnId ───────────────────────────────
        for entry in entries_with_class:
            code           = entry["code"]
            classification = entry["classification"]
            if code not in self.product_id_map:
                continue
            if classification not in self.classification_id_map:
                self.stats["skipped"] += 1
                continue

            prod_id  = self.product_id_map[code]
            class_id = self.classification_id_map[classification]

            if not self.sf.dry_run:
                existing = self.sf.query(
                    f"SELECT BasedOnId FROM Product2 WHERE Id = '{prod_id}' LIMIT 1"
                )
                if existing and existing[0].get("BasedOnId") == class_id:
                    log.info("Classification already set — skipping: %s / %s", code, classification)
                    self.stats["skipped"] += 1
                    continue

            try:
                self.sf.update("Product2", prod_id, {"BasedOnId": class_id})
                self.stats["classifications"] += 1
            except RuntimeError as exc:
                msg = f"Classification link {code}/{classification}: {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)

    # ── Step 5: Attributes (Picklist → AttributePicklistValue → AttributeDefinition)
    def _step5_attributes(self) -> None:
        entries_with_attrs = [(e, e["attributes"]) for e in self.all_entries if e.get("attributes")]
        if not entries_with_attrs:
            return
        log.info("──────────────────────────────────────────────────")
        log.info("STEP 5 › Attributes — picklists and definitions")

        # Flatten all unique attribute specs keyed by name for phases A–D
        attr_specs: Dict[str, Dict] = {}
        for _, attrs in entries_with_attrs:
            for attr in attrs:
                name = (attr.get("name") or attr.get("code", "")).strip()
                if name and name not in attr_specs:
                    attr_specs[name] = attr

        # ── Phase 0: AttributeCategory — resolve or create ───────────
        # Collect assignments: attr_name → category_name
        category_codes: Dict[str, str] = {}  # category_name → optional Code from YAML
        for attr_name, attr in attr_specs.items():
            cat_name = str(attr.get("category", "")).strip()
            if cat_name:
                self.attr_category_assignment[attr_name] = cat_name
                if attr.get("category_code"):
                    category_codes[cat_name] = str(attr["category_code"]).strip()

        unique_categories = list({v for v in self.attr_category_assignment.values()})
        if unique_categories:
            log.info("  Attribute categories to resolve: %d", len(unique_categories))
        for cat_name in unique_categories:
            if cat_name in self.attr_category_id_map:
                continue
            safe = cat_name.replace("'", "\\'")
            recs = self.sf.query(
                f"SELECT Id FROM AttributeCategory WHERE Name = '{safe}' LIMIT 1"
            )
            if recs:
                self.attr_category_id_map[cat_name] = recs[0]["Id"]
                log.info("AttributeCategory exists — using: %s", cat_name)
                self.stats["skipped"] += 1
            else:
                code = category_codes.get(cat_name) or re.sub(r"[^A-Z0-9_]", "_", cat_name.upper())[:40]
                try:
                    cat_id = self.sf.create("AttributeCategory", {"Name": cat_name, "Code": code})
                    self.attr_category_id_map[cat_name] = cat_id
                    self.stats["attr_categories_created"] += 1
                except RuntimeError as exc:
                    msg = f"AttributeCategory '{cat_name}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

        # ── Phase A: AttributePicklist ────────────────────────────────
        for attr_name, attr in attr_specs.items():
            pl_name = str(attr.get("picklist_name", "")).strip()
            if not pl_name:
                continue
            if pl_name in self.picklist_id_map:
                continue  # already resolved in a prior loop iteration

            safe = pl_name.replace("'", "\\'")
            recs = self.sf.query(
                f"SELECT Id FROM AttributePicklist WHERE Name = '{safe}' LIMIT 1"
            )
            if not recs and attr.get("picklist_code"):
                safe_code = str(attr["picklist_code"]).replace("'", "\\'")
                recs = self.sf.query(
                    f"SELECT Id FROM AttributePicklist WHERE Code = '{safe_code}' LIMIT 1"
                )
            if recs:
                self.picklist_id_map[pl_name] = recs[0]["Id"]
                log.info("AttributePicklist exists — skipping: %s", pl_name)
                self.stats["skipped"] += 1
            else:
                payload: Dict[str, Any] = {
                    "Name":     pl_name,
                    "Status":   "Active",
                    "DataType": str(attr.get("picklist_data_type", "Text")).strip() or "Text",
                }
                if attr.get("picklist_code"):
                    payload["Code"] = str(attr["picklist_code"]).strip()
                else:
                    # Salesforce requires Code; auto-derive from name if not supplied
                    auto_code = re.sub(r"[^A-Z0-9_]", "_", pl_name.upper())[:40]
                    payload["Code"] = auto_code
                try:
                    pl_id = self.sf.create("AttributePicklist", payload)
                    self.picklist_id_map[pl_name] = pl_id
                    self.stats["picklists"] += 1
                except RuntimeError as exc:
                    msg = f"AttributePicklist '{pl_name}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

        # ── Phase B: AttributePicklistValue ───────────────────────────
        for attr_name, attr in attr_specs.items():
            pl_name = str(attr.get("picklist_name", "")).strip()
            if not pl_name or pl_name not in self.picklist_id_map:
                continue
            pl_id = self.picklist_id_map[pl_name]
            values = attr.get("picklist_values", [])
            if not values:
                continue

            existing_codes: set = set()
            if not self.sf.dry_run:
                for rec in self.sf.query(
                    f"SELECT Code FROM AttributePicklistValue WHERE PicklistId = '{pl_id}'"
                ):
                    existing_codes.add(str(rec.get("Code", "")).strip())

            for seq_j, pv in enumerate(values, start=1):
                val   = str(pv.get("value", "")).strip()
                code  = str(pv.get("code",  val)).strip()
                if not val:
                    continue
                if code in existing_codes:
                    log.info("AttributePicklistValue exists — skipping: %s / %s", pl_name, code)
                    self.stats["skipped"] += 1
                    continue
                pv_payload: Dict[str, Any] = {
                    "PicklistId": pl_id,
                    "Name":       val,
                    "Value":      val,
                    "Code":       code,
                    "Status":     "Active",
                    "Sequence":   float(pv.get("sequence", seq_j)),
                    "IsDefault":  bool(pv.get("is_default", False)),
                }
                if pv.get("display_value"):
                    pv_payload["DisplayValue"] = str(pv["display_value"])
                if pv.get("abbreviation"):
                    pv_payload["Abbreviation"] = str(pv["abbreviation"])
                try:
                    self.sf.create("AttributePicklistValue", pv_payload)
                    self.stats["picklist_values"] += 1
                    existing_codes.add(code)
                except RuntimeError as exc:
                    msg = f"AttributePicklistValue {pl_name}/{code}: {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

        # ── Phase C: AttributeDefinition — look up, or create if spec given
        for attr_name, attr in attr_specs.items():
            safe = attr_name.replace("'", "\\'")
            recs = self.sf.query(
                f"SELECT Id, DataType FROM AttributeDefinition WHERE Name = '{safe}' AND IsActive = true LIMIT 1"
            )
            if not recs:
                recs = self.sf.query(
                    f"SELECT Id, DataType FROM AttributeDefinition WHERE Code = '{safe}' AND IsActive = true LIMIT 1"
                )
            if recs:
                self.attr_def_id_map[attr_name] = recs[0]["Id"]
                log.info("AttributeDefinition exists — skipping: %s", attr_name)
                self.stats["skipped"] += 1
                continue

            # Not found — create only if caller provided enough spec
            pl_name   = str(attr.get("picklist_name", "")).strip()
            data_type = str(attr.get("data_type", "")).strip()
            if pl_name:
                data_type = "Picklist"
            if not data_type:
                log.warning(
                    "AttributeDefinition '%s' not found and no data_type given — skipping.", attr_name
                )
                continue

            ad_payload: Dict[str, Any] = {
                "Name":     attr_name,
                "Label":    attr_name,
                "DataType": data_type,
                "IsActive": True,
            }
            if attr.get("attr_code"):
                ad_payload["Code"] = str(attr["attr_code"]).strip()
            if attr.get("default_value") is not None:
                ad_payload["DefaultValue"] = str(attr["default_value"])
            if pl_name and pl_name in self.picklist_id_map:
                ad_payload["PicklistId"] = self.picklist_id_map[pl_name]

            try:
                ad_id = self.sf.create("AttributeDefinition", ad_payload)
                self.attr_def_id_map[attr_name] = ad_id
                self.stats["attr_defs_created"] += 1
            except RuntimeError as exc:
                msg = f"AttributeDefinition '{attr_name}': {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)

    # ── Step 7: ProductCategoryProduct (link Product2 → ProductCategory) ─
    def _step7_catalog_products(self) -> None:
        entries_with_category = [e for e in self.all_entries if e.get("category")]
        if not entries_with_category:
            return

        log.info("──────────────────────────────────────────────────")
        log.info("STEP 7 › ProductCategoryProduct — linking products to categories (%d)",
                 len(entries_with_category))

        # Pre-fetch existing ProductCategoryProduct records for all products in scope
        existing_links: set = set()  # (ProductId, ProductCategoryId)
        any_synthetic = any(v.startswith("DRY-") for v in self.category_id_map.values())
        if self.product_id_map and not self.sf.dry_run and not any_synthetic:
            prod_ids = ", ".join(f"'{v}'" for v in self.product_id_map.values())
            try:
                for rec in self.sf.query(
                    f"SELECT ProductId, ProductCategoryId "
                    f"FROM ProductCategoryProduct WHERE ProductId IN ({prod_ids})"
                ):
                    existing_links.add((rec["ProductId"], rec["ProductCategoryId"]))
            except Exception as exc:
                log.warning("Could not pre-fetch ProductCategoryProduct records: %s", exc)

        for entry in entries_with_category:
            code    = entry["code"]
            cat_ref = entry["category"]   # name or "Parent > Child" path

            if code not in self.product_id_map:
                continue

            # Resolve category — try exact key first, then suffix match for bare name
            category_id = self.category_id_map.get(cat_ref) or self.category_id_map.get(
                next((k for k in self.category_id_map if k.endswith(f"> {cat_ref}") or k == cat_ref), ""),
                ""
            )
            if not category_id:
                log.warning(
                    "Category '%s' not resolved — skipping ProductCategoryProduct for %s.",
                    cat_ref, code
                )
                self.stats["skipped"] += 1
                continue

            prod_id = self.product_id_map[code]
            if (prod_id, category_id) in existing_links:
                log.info("ProductCategoryProduct exists — skipping: %s / %s", code, cat_ref)
                self.stats["skipped"] += 1
                continue

            # ProductCategoryProduct only needs ProductId + ProductCategoryId (CatalogId is derived)
            payload: Dict[str, Any] = {
                "ProductId":         prod_id,
                "ProductCategoryId": category_id,
            }

            try:
                self.sf.create("ProductCategoryProduct", payload)
                self.stats["catalog_products"] += 1
                existing_links.add((prod_id, category_id))
            except RuntimeError as exc:
                msg = f"ProductCategoryProduct {code}/{cat_ref}: {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)

    # ── Summary ──────────────────────────────────────────────────────────
    def _print_summary(self) -> None:
        s = self.stats
        log.info("══════════════════════════════════════════════════")
        log.info("SUMMARY")
        log.info("  ProductCatalog records created:      %d", s["catalogs_created"])
        log.info("  ProductCategory records created:     %d", s["categories_created"])
        log.info("  CatalogProduct links created:        %d", s["catalog_products"])
        log.info("  Product2 created:                   %d", s["products"])
        log.info("  Product2 updated:                   %d", s["products_updated"])
        log.info("  ProductSellingModel created:        %d", s["psm_created"])
        log.info("  PSM Options created:                %d", s["psm_options"])
        log.info("  PricebookEntry records created:     %d", s["pricebook_entries"])
        log.info("  PricebookEntry records updated:     %d", s["pricebook_entries_updated"])
        log.info("  ProductGroup records created:       %d", s["product_groups"])
        log.info("  ProductRelatedComponent records:    %d", s["components"])
        log.info("  ProductRelatedComponent updated:    %d", s["components_updated"])
        log.info("  ProductClassification created:       %d", s["classifications_created"])
        log.info("  AttributeCategory records created:   %d", s["attr_categories_created"])
        log.info("  Classification attrs linked:         %d", s["classification_attrs"])
        log.info("  Classification links set:            %d", s["classifications"])
        log.info("  AttributePicklist records created:   %d", s["picklists"])
        log.info("  AttributePicklistValue records:      %d", s["picklist_values"])
        log.info("  AttributeDefinition records created: %d", s["attr_defs_created"])
        log.info("  Records skipped (already exist):    %d", s["skipped"])
        if s["errors"]:
            log.error("  Errors (%d):", len(s["errors"]))
            for err in s["errors"]:
                log.error("    ✗ %s", err)
        else:
            log.info("  ✓ No errors")
        log.info("══════════════════════════════════════════════════")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create RCA products from a YAML catalog",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--catalog", "-c", default="rca_catalog.yaml",
                        metavar="FILE", help="YAML catalog file (default: rca_catalog.yaml)")
    parser.add_argument("--org", "-o", metavar="ALIAS", help="sf CLI org alias")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview only — no records created or updated")
    parser.add_argument("--create-only", action="store_true",
                        help="Create new records only — never update existing ones")
    parser.add_argument("--upsert", action="store_true",
                        help="Update existing records when config fields have changed (default: skip)")
    parser.add_argument("--api-version", metavar="VER", default="62.0",
                        help="Salesforce API version (default: 62.0)")
    args = parser.parse_args()

    if args.upsert and args.create_only:
        log.error("--upsert and --create-only are mutually exclusive.")
        sys.exit(1)
    mode_tag = "[DRY-RUN MODE]" if args.dry_run else "[UPSERT MODE]" if args.upsert else "[CREATE-ONLY MODE]" if args.create_only else ""
    log.info("RCA Product Creator  %s", mode_tag)
    log.info("Catalog: %s", os.path.abspath(args.catalog))

    catalog = load_catalog(args.catalog)
    n_products = len(catalog.get("products", []))
    n_bundles  = len(catalog.get("bundles", []))
    log.info("Loaded %d products, %d bundles", n_products, n_bundles)

    access_token, instance_url = get_sf_credentials(args.org)
    log.info("Connected to: %s", instance_url)

    sf = SalesforceClient(access_token, instance_url, args.api_version,
                          args.dry_run, args.create_only, args.upsert)
    RCAProductCreator(sf, catalog).run()


if __name__ == "__main__":
    main()
