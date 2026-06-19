#!/usr/bin/env python3
"""
RCA Price Adjustment Creator
============================
Reads a YAML adjustments catalog and creates Salesforce Revenue Cloud Advanced
price adjustment records via REST API:

  Step 0  Resolve Product2 codes and ProductSellingModel names to Salesforce Ids
  Step 1  PriceAdjustmentSchedule        (header per schedule entry)
  Step 2  PriceAdjustmentTier            (volume/tier schedules)
  Step 3  AttributeBasedAdjRule          (attribute schedules — rule container)
          AttributeAdjustmentCondition   (attribute schedules — conditions on the rule)
          AttributeBasedAdjustment       (attribute schedules — the price adjustment)
  Step 4  BundleBasedAdjustment          (bundle schedules)

Authentication uses `sf org display` — no passwords stored.

Usage:
  python create_price_adjustments.py [--catalog rca_adjustments.yaml] [--org <alias>] [--dry-run]

Flags:
  --catalog  -c   Path to the YAML adjustments file. Default: rca_adjustments.yaml
  --org      -o   sf CLI org alias. Omit to use the default authenticated org.
  --dry-run       Preview every record that WOULD be created without touching the org.
  --upsert        Update existing records when values have changed.
  --create-only   Create new records only; never update existing ones.

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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

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
# Salesforce REST client (same pattern as create_rca_products.py)
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
        log.info("Created %-40s  Id: %s", sobject, record_id)
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
        log.info("Updated %-40s  Id: %s", sobject, record_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


# Map AttributeDefinition.DataType → the Salesforce typed value field on AttributeAdjustmentCondition
_DTYPE_TO_CONDITION_FIELD: Dict[str, str] = {
    "Text":     "StringValue",
    "Picklist": "StringValue",
    "TextArea": "StringValue",
    "Number":   "DoubleValue",
    "Decimal":  "DoubleValue",
    "Double":   "DoubleValue",
    "Integer":  "IntegerValue",
    "Boolean":  "BooleanValue",
    "Date":     "DateValue",
    "DateTime": "DateTimeValue",
}


# ---------------------------------------------------------------------------
# Main creator
# ---------------------------------------------------------------------------
class PriceAdjustmentCreator:
    def __init__(self, sf: SalesforceClient, catalog: Dict):
        self.sf = sf
        self.schedules = catalog.get("price_adjustment_schedules", [])

        # Resolution maps built in Step 0
        self.product_id_map:   Dict[str, str] = {}  # product_code → Product2.Id
        self.psm_id_map:       Dict[str, str] = {}  # psm_name → ProductSellingModel.Id
        self.pricebook_id_map: Dict[str, str] = {}  # pricebook_name → Pricebook2.Id
        # Snapshot-loaded schedule maps for type-based matching
        self.snap_schedule_by_name: Dict[str, Dict] = {}   # name → {sf_id, schedule_type, ...}
        self.snap_schedule_by_type: Dict[str, List[Dict]] = {}  # schedule_type → [schedules]

        # Built during Step 1
        self.schedule_id_map: Dict[str, str] = {}        # schedule_name → PriceAdjustmentSchedule.Id
        self.schedule_wants_active: Dict[str, bool] = {}  # schedule_name → desired IsActive

        # Cache for AttributeDefinition data type lookups
        self.attr_def_map: Dict[str, Dict] = {}     # attr_name → {Id, DataType}

        self.stats: Dict[str, Any] = {
            "schedules":             0,
            "tiers":                 0,
            "adj_rules":             0,
            "adj_conditions":        0,
            "attr_adjustments":      0,
            "bundle_adjustments":    0,
            "skipped":               0,
            "errors":                [],
        }

    def run(self) -> None:
        if not self.schedules:
            log.warning("No price_adjustment_schedules found in catalog. Nothing to do.")
            return

        log.info("=" * 60)
        log.info("RCA Price Adjustment Creator%s", "  [DRY-RUN]" if self.sf.dry_run else "")
        log.info("Schedules to process: %d", len(self.schedules))
        log.info("=" * 60)

        self._step0_resolve()
        self._step1_schedules()
        self._step2_tiers()
        self._step3_attribute_adjustments()
        self._step4_bundle_adjustments()
        self._step5_activate_schedules()  # activate after child records exist
        self._summary()

    # ── Step 0: resolve product codes / PSM names / pricebooks to Ids ────────
    def _step0_resolve(self, snapshot_path: Optional[str] = None) -> None:
        log.info("Step 0 — Resolving products, PSMs, pricebooks, and schedules")

        # Load snapshot for schedule matching — check CWD first, then script-relative fallback
        snap_path = snapshot_path or os.path.join(os.getcwd(), ".rca", "org-snapshot.yaml")
        if not os.path.isfile(snap_path):
            snap_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", ".rca", "org-snapshot.yaml"
            )
        if os.path.isfile(snap_path):
            try:
                with open(snap_path, encoding="utf-8") as f:
                    snap = yaml.safe_load(f) or {}
                for s in snap.get("price_adjustment_schedules", []):
                    name = s.get("name", "")
                    stype = s.get("schedule_type", "")
                    if name:
                        self.snap_schedule_by_name[name] = s
                    if stype:
                        self.snap_schedule_by_type.setdefault(stype, []).append(s)
                log.info("  Snapshot schedules loaded: %d",
                         len(self.snap_schedule_by_name))
            except Exception as exc:
                log.warning("Could not load snapshot (%s) — will query org live", exc)
        else:
            log.info("  No snapshot found at %s — will query org live for schedules", snap_path)

        # Collect all referenced product codes
        product_codes: set = set()
        psm_names: set = set()
        pricebook_names: set = set()

        for sched in self.schedules:
            pb = str(sched.get("pricebook", "")).strip()
            if pb:
                pricebook_names.add(pb)

            for tier in sched.get("tiers", []):
                code = str(tier.get("product_code", "")).strip()
                psm  = str(tier.get("psm_name", "")).strip()
                if code: product_codes.add(code)
                if psm:  psm_names.add(psm)

            for aa in sched.get("attribute_adjustments", []):
                for cond in aa.get("conditions", []):
                    code = str(cond.get("product_code", "")).strip()
                    if code: product_codes.add(code)
                adj = aa.get("adjustment", {})
                code = str(adj.get("product_code", "")).strip()
                psm  = str(adj.get("psm_name", "")).strip()
                if code: product_codes.add(code)
                if psm:  psm_names.add(psm)

            for ba in sched.get("bundle_adjustments", []):
                for field in ("product_code", "parent_product_code"):
                    code = str(ba.get(field, "")).strip()
                    if code: product_codes.add(code)
                for field in ("psm_name", "parent_psm_name"):
                    psm = str(ba.get(field, "")).strip()
                    if psm: psm_names.add(psm)

        # Bulk-query Product2
        if product_codes:
            codes_in = ", ".join(f"'{c}'" for c in product_codes)
            for rec in self.sf.query(
                f"SELECT Id, ProductCode FROM Product2 WHERE ProductCode IN ({codes_in})"
            ):
                self.product_id_map[rec["ProductCode"]] = rec["Id"]
            missing = product_codes - set(self.product_id_map.keys())
            for m in missing:
                log.warning("Product2 not found for code: %s", m)

        # Bulk-query ProductSellingModel
        if psm_names:
            names_in = ", ".join(f"'{n}'" for n in psm_names)
            for rec in self.sf.query(
                f"SELECT Id, Name FROM ProductSellingModel WHERE Name IN ({names_in})"
            ):
                self.psm_id_map[rec["Name"]] = rec["Id"]
            missing = psm_names - set(self.psm_id_map.keys())
            for m in missing:
                log.warning("ProductSellingModel not found for name: %s", m)

        # Bulk-query Pricebook2
        if pricebook_names:
            names_in = ", ".join(f"'{n}'" for n in pricebook_names)
            for rec in self.sf.query(
                f"SELECT Id, Name FROM Pricebook2 WHERE Name IN ({names_in})"
            ):
                self.pricebook_id_map[rec["Name"]] = rec["Id"]
            missing = pricebook_names - set(self.pricebook_id_map.keys())
            for m in missing:
                log.warning("Pricebook2 not found for name: %s", m)

        log.info("  Products resolved: %d / %d", len(self.product_id_map), len(product_codes))
        log.info("  PSMs resolved:     %d / %d", len(self.psm_id_map), len(psm_names))
        log.info("  Pricebooks:        %d / %d", len(self.pricebook_id_map), len(pricebook_names))

    # ── Step 1: PriceAdjustmentSchedule ──────────────────────────────────────
    def _step1_schedules(self) -> None:
        log.info("Step 1 — PriceAdjustmentSchedule")

        # Authoritative live map: name → Id
        existing_live: Dict[str, str] = {}
        try:
            for rec in self.sf.query("SELECT Id, Name FROM PriceAdjustmentSchedule"):
                existing_live[rec["Name"]] = rec["Id"]
        except Exception:
            pass

        for sched in self.schedules:
            name       = str(sched.get("name", "")).strip()
            sched_type = str(sched.get("schedule_type", "")).strip()
            wants_active = bool(sched.get("is_active", True))

            resolved_id:   Optional[str] = None
            resolved_name: str = name  # the key used in schedule_id_map

            # 1. Match by exact name against live org
            if name and name in existing_live:
                resolved_id = existing_live[name]
                log.info("PriceAdjustmentSchedule matched by name — using: %s", name)
                self.stats["skipped"] += 1

            # 2. No name given (or name not found) → match by schedule_type from snapshot.
            #    Candidates are in alphabetical order (ORDER BY Name in sync query), so
            #    "Standard X" schedules naturally sort before product-specific ones.
            elif not resolved_id and sched_type and sched_type in self.snap_schedule_by_type:
                candidates = self.snap_schedule_by_type[sched_type]
                if len(candidates) > 1:
                    names = [c.get("name", "") for c in candidates]
                    log.warning("Multiple '%s' schedules in snapshot: %s — using first: %s",
                                sched_type, names, names[0])
                pick      = candidates[0]
                snap_name = pick.get("name", "")
                if snap_name and snap_name in existing_live:
                    resolved_id   = existing_live[snap_name]
                    resolved_name = snap_name
                    log.info("PriceAdjustmentSchedule matched by type '%s' — using: %s",
                             sched_type, snap_name)
                    self.stats["skipped"] += 1

            # 3. Create new — requires a name
            if not resolved_id:
                if not name:
                    msg = (f"No 'name' and no existing '{sched_type}' schedule found "
                           f"in snapshot — skipping entry")
                    log.error(msg)
                    self.stats["errors"].append(msg)
                    continue

                # Create inactive; EffectiveFrom required before child records can be added
                effective_from = (sched.get("effective_from") or
                                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                payload: Dict[str, Any] = {
                    "Name":             name,
                    "ScheduleType":     sched_type or "Volume",
                    "AdjustmentMethod": str(sched.get("adjustment_method", "Range")),
                    "IsActive":         False,
                    "EffectiveFrom":    effective_from,
                }
                pb_name = str(sched.get("pricebook", "")).strip()
                if pb_name and pb_name in self.pricebook_id_map:
                    payload["Pricebook2Id"] = self.pricebook_id_map[pb_name]
                if sched.get("effective_to"):
                    payload["EffectiveTo"] = sched["effective_to"]
                if sched.get("description"):
                    payload["Description"] = str(sched["description"])

                try:
                    resolved_id   = self.sf.create("PriceAdjustmentSchedule", payload)
                    resolved_name = name
                    self.stats["schedules"] += 1
                except RuntimeError as exc:
                    msg = f"PriceAdjustmentSchedule '{name}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)
                    continue

            # Annotate the YAML entry so child steps can read the Id directly
            sched["_resolved_schedule_id"]   = resolved_id
            sched["_resolved_schedule_name"] = resolved_name

            # Register under resolved name AND original YAML name (if different)
            self.schedule_id_map[resolved_name]       = resolved_id
            self.schedule_wants_active[resolved_name] = wants_active
            if name and name != resolved_name:
                self.schedule_id_map[name]      = resolved_id
                self.schedule_wants_active[name] = wants_active

    # ── Step 2: PriceAdjustmentTier (Volume schedules) ───────────────────────
    def _step2_tiers(self) -> None:
        volume_scheds = [s for s in self.schedules if str(s.get("schedule_type", "")).lower() == "volume"]
        if not volume_scheds:
            return
        log.info("Step 2 — PriceAdjustmentTier (Volume schedules: %d)", len(volume_scheds))

        for sched in volume_scheds:
            sched_name = sched.get("_resolved_schedule_name") or str(sched.get("name", "")).strip()
            sched_id   = sched.get("_resolved_schedule_id") or self.schedule_id_map.get(sched_name)
            if not sched_id:
                log.warning("No schedule Id for '%s' — skipping tiers", sched_name)
                continue

            tiers_spec = sched.get("tiers", [])
            if not tiers_spec:
                continue

            # Load existing tiers for this schedule
            existing_tiers: List[Dict] = []
            try:
                existing_tiers = self.sf.query(
                    f"SELECT Id, LowerBound, UpperBound, Product2Id, ProductSellingModelId, "
                    f"TierType, TierValue "
                    f"FROM PriceAdjustmentTier "
                    f"WHERE PriceAdjustmentScheduleId = '{sched_id}'"
                )
            except Exception:
                pass

            # Index by (LowerBound, UpperBound, Product2Id, PSMId)
            existing_index: Dict[tuple, Dict] = {}
            for t in existing_tiers:
                key = (
                    t.get("LowerBound"),
                    t.get("UpperBound"),
                    t.get("Product2Id"),
                    t.get("ProductSellingModelId"),
                )
                existing_index[key] = t

            for tier in tiers_spec:
                lower = tier.get("lower_bound")
                upper = tier.get("upper_bound")  # None = open-ended
                tier_type  = str(tier.get("tier_type", "AdjustmentPercentage"))
                tier_value = float(tier.get("tier_value", 0))

                prod_code = str(tier.get("product_code", "")).strip()
                psm_name  = str(tier.get("psm_name", "")).strip()
                prod_id   = self.product_id_map.get(prod_code) if prod_code else None
                psm_id    = self.psm_id_map.get(psm_name) if psm_name else None

                key = (lower, upper, prod_id, psm_id)

                if key in existing_index:
                    existing_rec = existing_index[key]
                    if self.sf.upsert:
                        changed: Dict[str, Any] = {}
                        if existing_rec.get("TierType") != tier_type:
                            changed["TierType"] = tier_type
                        if existing_rec.get("TierValue") != tier_value:
                            changed["TierValue"] = tier_value
                        if changed:
                            try:
                                self.sf.update("PriceAdjustmentTier", existing_rec["Id"], changed)
                            except RuntimeError as exc:
                                msg = f"PriceAdjustmentTier upsert '{sched_name}' {lower}-{upper}: {exc}"
                                log.error(msg)
                                self.stats["errors"].append(msg)
                        else:
                            log.info("PriceAdjustmentTier exists — skipping: %s  %s–%s",
                                     sched_name, lower, upper)
                            self.stats["skipped"] += 1
                    else:
                        log.info("PriceAdjustmentTier exists — skipping: %s  %s–%s",
                                 sched_name, lower, upper)
                        self.stats["skipped"] += 1
                    continue

                payload: Dict[str, Any] = {
                    "PriceAdjustmentScheduleId": sched_id,
                    "TierType":  tier_type,
                    "TierValue": tier_value,
                    "LowerBound": lower,
                }
                if upper is not None:
                    payload["UpperBound"] = upper
                if prod_id:
                    payload["Product2Id"] = prod_id
                if psm_id:
                    payload["ProductSellingModelId"] = psm_id
                if tier.get("effective_from"):
                    payload["EffectiveFrom"] = tier["effective_from"]
                if tier.get("effective_to"):
                    payload["EffectiveTo"] = tier["effective_to"]

                try:
                    self.sf.create("PriceAdjustmentTier", payload)
                    self.stats["tiers"] += 1
                except RuntimeError as exc:
                    msg = f"PriceAdjustmentTier '{sched_name}' {lower}-{upper}: {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

    # ── Step 3: Attribute-based adjustments ──────────────────────────────────
    def _step3_attribute_adjustments(self) -> None:
        attr_scheds = [s for s in self.schedules
                       if str(s.get("schedule_type", "")).lower() == "attribute"]
        if not attr_scheds:
            return
        log.info("Step 3 — Attribute adjustments (schedules: %d)", len(attr_scheds))

        for sched in attr_scheds:
            sched_name = sched.get("_resolved_schedule_name") or str(sched.get("name", "")).strip()
            sched_id   = sched.get("_resolved_schedule_id") or self.schedule_id_map.get(sched_name)
            if not sched_id:
                log.warning("No schedule Id for '%s' — skipping attribute adjustments", sched_name)
                continue

            for aa in sched.get("attribute_adjustments", []):
                self._process_attribute_adjustment(sched_name, sched_id, aa)

    def _process_attribute_adjustment(self, sched_name: str, sched_id: str, aa: Dict) -> None:
        rule_name = str(aa.get("rule_name", "")).strip()
        if not rule_name:
            log.error("attribute_adjustment missing 'rule_name' in schedule '%s'", sched_name)
            self.stats["errors"].append(f"Missing rule_name in schedule '{sched_name}'")
            return

        # ── 3a: AttributeBasedAdjRule ─────────────────────────────────────────
        rule_id: Optional[str] = None
        existing_rules = []
        try:
            safe = rule_name.replace("'", "\\'")
            existing_rules = self.sf.query(
                f"SELECT Id FROM AttributeBasedAdjRule WHERE Name = '{safe}' LIMIT 1"
            )
        except Exception:
            pass

        if existing_rules:
            rule_id = existing_rules[0]["Id"]
            log.info("AttributeBasedAdjRule exists — skipping: %s", rule_name)
            self.stats["skipped"] += 1
        else:
            usage_type  = str(aa.get("usage_type", "Pricing"))
            conditions  = aa.get("conditions", [])
            rule_payload: Dict[str, Any] = {
                "Name":      rule_name,
                "UsageType": usage_type,
            }
            try:
                rule_id = self.sf.create("AttributeBasedAdjRule", rule_payload)
                self.stats["adj_rules"] += 1
            except RuntimeError as exc:
                msg = f"AttributeBasedAdjRule '{rule_name}': {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)
                return

        # ── 3b: AttributeAdjustmentCondition (one per condition) ─────────────
        conditions = aa.get("conditions", [])
        for cond in conditions:
            self._process_adj_condition(rule_name, rule_id, cond)

        # ── 3c: AttributeBasedAdjustment ─────────────────────────────────────
        adj = aa.get("adjustment")
        if not adj:
            log.warning("No 'adjustment' block on rule '%s' — skipping", rule_name)
            return
        self._process_attr_based_adjustment(sched_name, sched_id, rule_name, rule_id, adj)

    def _process_adj_condition(self, rule_name: str, rule_id: str, cond: Dict) -> None:
        attr_name  = str(cond.get("attribute", "")).strip()
        prod_code  = str(cond.get("product_code", "")).strip()
        operator   = str(cond.get("operator", "equals"))
        value      = cond.get("value")

        if not attr_name:
            log.error("Condition missing 'attribute' on rule '%s'", rule_name)
            self.stats["errors"].append(f"Condition missing 'attribute' on rule '{rule_name}'")
            return

        # Resolve AttributeDefinition (with DataType auto-detect)
        attr_info = self._resolve_attr_def(attr_name)
        if not attr_info:
            log.error("AttributeDefinition not found: '%s'", attr_name)
            self.stats["errors"].append(f"AttributeDefinition not found: '{attr_name}'")
            return

        attr_def_id = attr_info["Id"]
        prod_id = self.product_id_map.get(prod_code) if prod_code else None

        # Check if this condition already exists
        existing_conds: List[Dict] = []
        try:
            prod_filter = f"AND ProductId = '{prod_id}'" if prod_id else ""
            existing_conds = self.sf.query(
                f"SELECT Id FROM AttributeAdjustmentCondition "
                f"WHERE AttributeBasedAdjRuleId = '{rule_id}' "
                f"AND AttributeDefinitionId = '{attr_def_id}' "
                f"AND Operator = '{operator}' "
                f"{prod_filter} LIMIT 1"
            )
        except Exception:
            pass

        if existing_conds:
            log.info("AttributeAdjustmentCondition exists — skipping: %s / %s %s",
                     rule_name, attr_name, operator)
            self.stats["skipped"] += 1
            return

        # Determine typed value field from DataType
        data_type  = attr_info.get("DataType", "Text")
        value_field = _DTYPE_TO_CONDITION_FIELD.get(data_type, "StringValue")

        payload: Dict[str, Any] = {
            "AttributeBasedAdjRuleId":  rule_id,
            "AttributeDefinitionId":    attr_def_id,
            "Operator":                 operator,
            value_field:                value,
        }
        if prod_id:
            payload["ProductId"] = prod_id

        try:
            self.sf.create("AttributeAdjustmentCondition", payload)
            self.stats["adj_conditions"] += 1
        except RuntimeError as exc:
            msg = f"AttributeAdjustmentCondition '{rule_name}/{attr_name}': {exc}"
            log.error(msg)
            self.stats["errors"].append(msg)

    def _process_attr_based_adjustment(self, sched_name: str, sched_id: str,
                                        rule_name: str, rule_id: str, adj: Dict) -> None:
        prod_code = str(adj.get("product_code", "")).strip()
        psm_name  = str(adj.get("psm_name", "")).strip()
        prod_id   = self.product_id_map.get(prod_code) if prod_code else None
        psm_id    = self.psm_id_map.get(psm_name) if psm_name else None

        # Check existing
        existing: List[Dict] = []
        try:
            prod_filter = f"AND ProductId = '{prod_id}'" if prod_id else ""
            psm_filter  = f"AND ProductSellingModelId = '{psm_id}'" if psm_id else ""
            existing = self.sf.query(
                f"SELECT Id FROM AttributeBasedAdjustment "
                f"WHERE PriceAdjustmentScheduleId = '{sched_id}' "
                f"AND AttributeBasedAdjRuleId = '{rule_id}' "
                f"{prod_filter} {psm_filter} LIMIT 1"
            )
        except Exception:
            pass

        if existing:
            log.info("AttributeBasedAdjustment exists — skipping: %s / %s", sched_name, rule_name)
            self.stats["skipped"] += 1
            if self.sf.upsert:
                changed: Dict[str, Any] = {}
                adj_type = str(adj.get("adjustment_type", "Percentage"))
                adj_val  = float(adj.get("adjustment_value", 0))
                # We don't re-query current values for this field here; just update always in upsert
                changed["AdjustmentType"]  = adj_type
                changed["AdjustmentValue"] = adj_val
                try:
                    self.sf.update("AttributeBasedAdjustment", existing[0]["Id"], changed)
                except RuntimeError as exc:
                    msg = f"AttributeBasedAdjustment upsert '{sched_name}/{rule_name}': {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)
            return

        # EffectiveFrom is required by Salesforce — default to now if not specified
        effective_from = adj.get("effective_from") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload: Dict[str, Any] = {
            "PriceAdjustmentScheduleId": sched_id,
            "AttributeBasedAdjRuleId":   rule_id,
            "AdjustmentType":            str(adj.get("adjustment_type", "Percentage")),
            "AdjustmentValue":           float(adj.get("adjustment_value", 0)),
            "EffectiveFrom":             effective_from,
        }
        if prod_id:
            payload["ProductId"] = prod_id
        if psm_id:
            payload["ProductSellingModelId"] = psm_id
        if adj.get("effective_to"):
            payload["EffectiveTo"] = adj["effective_to"]

        try:
            self.sf.create("AttributeBasedAdjustment", payload)
            self.stats["attr_adjustments"] += 1
        except RuntimeError as exc:
            msg = f"AttributeBasedAdjustment '{sched_name}/{rule_name}': {exc}"
            log.error(msg)
            self.stats["errors"].append(msg)

    # ── Step 4: Bundle-based adjustments ─────────────────────────────────────
    def _step4_bundle_adjustments(self) -> None:
        bundle_scheds = [s for s in self.schedules
                         if str(s.get("schedule_type", "")).lower() == "bundle"]
        if not bundle_scheds:
            return
        log.info("Step 4 — Bundle adjustments (schedules: %d)", len(bundle_scheds))

        for sched in bundle_scheds:
            sched_name = sched.get("_resolved_schedule_name") or str(sched.get("name", "")).strip()
            sched_id   = sched.get("_resolved_schedule_id") or self.schedule_id_map.get(sched_name)
            if not sched_id:
                log.warning("No schedule Id for '%s' — skipping bundle adjustments", sched_name)
                continue

            # Load existing bundle adjustments for this schedule
            existing_bundle: Dict[tuple, str] = {}
            try:
                for rec in self.sf.query(
                    f"SELECT Id, ProductId, ProductSellingModelId, ParentProductId "
                    f"FROM BundleBasedAdjustment "
                    f"WHERE PriceAdjustmentScheduleId = '{sched_id}'"
                ):
                    key = (
                        rec.get("ProductId"),
                        rec.get("ProductSellingModelId"),
                        rec.get("ParentProductId"),
                    )
                    existing_bundle[key] = rec["Id"]
            except Exception:
                pass

            for ba in sched.get("bundle_adjustments", []):
                prod_code        = str(ba.get("product_code", "")).strip()
                psm_name         = str(ba.get("psm_name", "")).strip()
                parent_code      = str(ba.get("parent_product_code", "")).strip()
                parent_psm_name  = str(ba.get("parent_psm_name", "")).strip()

                prod_id       = self.product_id_map.get(prod_code) if prod_code else None
                psm_id        = self.psm_id_map.get(psm_name) if psm_name else None
                parent_id     = self.product_id_map.get(parent_code) if parent_code else None
                parent_psm_id = self.psm_id_map.get(parent_psm_name) if parent_psm_name else None

                if not prod_id:
                    log.error("BundleBasedAdjustment: product '%s' not found — skipping", prod_code)
                    self.stats["errors"].append(
                        f"BundleBasedAdjustment: product '{prod_code}' not found in schedule '{sched_name}'"
                    )
                    continue
                if not parent_id:
                    log.error("BundleBasedAdjustment: parent_product '%s' not found — skipping", parent_code)
                    self.stats["errors"].append(
                        f"BundleBasedAdjustment: parent '{parent_code}' not found in schedule '{sched_name}'"
                    )
                    continue

                key = (prod_id, psm_id, parent_id)

                if key in existing_bundle:
                    log.info("BundleBasedAdjustment exists — skipping: %s / %s → %s",
                             sched_name, prod_code, parent_code)
                    self.stats["skipped"] += 1
                    continue

                payload: Dict[str, Any] = {
                    "PriceAdjustmentScheduleId": sched_id,
                    "ProductId":                 prod_id,
                    "ParentProductId":           parent_id,
                    "RootBundleId":              parent_id,   # default root = parent for single-level bundles
                    "AdjustmentType":            str(ba.get("adjustment_type", "Percentage")),
                    "AdjustmentValue":           float(ba.get("adjustment_value", 0)),
                }
                if psm_id:
                    payload["ProductSellingModelId"] = psm_id
                if parent_psm_id:
                    payload["ParentProductSellingModelId"] = parent_psm_id
                    payload["RootProductSellingModelId"]   = parent_psm_id
                if ba.get("effective_from"):
                    payload["EffectiveFrom"] = ba["effective_from"]
                if ba.get("effective_to"):
                    payload["EffectiveTo"] = ba["effective_to"]

                try:
                    self.sf.create("BundleBasedAdjustment", payload)
                    self.stats["bundle_adjustments"] += 1
                except RuntimeError as exc:
                    msg = f"BundleBasedAdjustment '{sched_name}' {prod_code}→{parent_code}: {exc}"
                    log.error(msg)
                    self.stats["errors"].append(msg)

    # ── Step 5: Activate schedules that want IsActive=True ───────────────────
    def _step5_activate_schedules(self) -> None:
        schedules_to_activate = [
            (name, sid) for name, sid in self.schedule_id_map.items()
            if self.schedule_wants_active.get(name, False)
        ]
        if not schedules_to_activate:
            return
        log.info("Step 5 — Activating %d schedule(s)", len(schedules_to_activate))
        for name, sched_id in schedules_to_activate:
            if sched_id.startswith("DRY-"):
                log.info("[DRY-RUN] Would activate PriceAdjustmentSchedule: %s", name)
                continue
            try:
                self.sf.update("PriceAdjustmentSchedule", sched_id, {"IsActive": True})
                log.info("Activated PriceAdjustmentSchedule: %s", name)
            except RuntimeError as exc:
                msg = f"Activate PriceAdjustmentSchedule '{name}': {exc}"
                log.error(msg)
                self.stats["errors"].append(msg)

    # ── Attribute definition lookup (with DataType) ───────────────────────────
    def _resolve_attr_def(self, attr_name: str) -> Optional[Dict]:
        if attr_name in self.attr_def_map:
            return self.attr_def_map[attr_name]
        safe = attr_name.replace("'", "\\'")
        recs = self.sf.query(
            f"SELECT Id, Name, DataType FROM AttributeDefinition WHERE Name = '{safe}' LIMIT 1"
        )
        if recs:
            self.attr_def_map[attr_name] = recs[0]
            return recs[0]
        return None

    # ── Summary ───────────────────────────────────────────────────────────────
    def _summary(self) -> None:
        log.info("=" * 60)
        log.info("SUMMARY%s", "  [DRY-RUN — no records written]" if self.sf.dry_run else "")
        log.info("  PriceAdjustmentSchedule    created: %d", self.stats["schedules"])
        log.info("  PriceAdjustmentTier        created: %d", self.stats["tiers"])
        log.info("  AttributeBasedAdjRule      created: %d", self.stats["adj_rules"])
        log.info("  AttributeAdjustmentCondition created: %d", self.stats["adj_conditions"])
        log.info("  AttributeBasedAdjustment   created: %d", self.stats["attr_adjustments"])
        log.info("  BundleBasedAdjustment      created: %d", self.stats["bundle_adjustments"])
        log.info("  Skipped (already exist):           %d", self.stats["skipped"])
        if self.stats["errors"]:
            log.info("  ERRORS: %d", len(self.stats["errors"]))
            for e in self.stats["errors"]:
                log.error("    %s", e)
        else:
            log.info("  No errors.")
        log.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create RCA Price Adjustment Schedules and child records from YAML"
    )
    parser.add_argument(
        "--catalog", "-c",
        default=os.path.join(os.path.dirname(__file__), "rca_adjustments.yaml"),
        help="Path to the YAML adjustments catalog (default: rca_adjustments.yaml)"
    )
    parser.add_argument("--org", "-o", default=None,
                        help="sf CLI org alias (default: currently authenticated org)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview all records without writing to Salesforce")
    parser.add_argument("--upsert", action="store_true",
                        help="Update existing records when values have changed")
    parser.add_argument("--create-only", action="store_true",
                        help="Create new records only; never update existing ones")
    parser.add_argument("--api-version", default="62.0",
                        help="Salesforce API version (default: 62.0)")
    args = parser.parse_args()

    log.info("Loading catalog: %s", args.catalog)
    catalog = load_catalog(args.catalog)

    log.info("Connecting to Salesforce org: %s", args.org or "(default)")
    access_token, instance_url = get_sf_credentials(args.org)
    log.info("Instance: %s", instance_url)

    sf = SalesforceClient(
        access_token=access_token,
        instance_url=instance_url,
        api_version=args.api_version,
        dry_run=args.dry_run,
        create_only=args.create_only,
        upsert=args.upsert,
    )

    creator = PriceAdjustmentCreator(sf, catalog)
    creator.run()


if __name__ == "__main__":
    main()
