#!/usr/bin/env python3
"""
update_rca_adjustments.py
=========================
Adds or updates one price adjustment schedule entry in the YAML adjustments file.
Called by Claude Code after collecting adjustment details via /describe-price-adjustment.

Usage:
  python update_rca_adjustments.py --json <adjustment.json> [--catalog rca_adj_session.yaml] [--fresh]

Flags:
  --json      -j   JSON file describing the schedule entry (required).
  --catalog   -c   YAML file to update. Default: rca_adj_session.yaml
  --fresh          Clear the file before writing (use at the start of each new session).

JSON Input Schema
-----------------
{
  "schedule_type": "Volume",          // required — Volume | Attribute | Bundle
  "name": "My Schedule",              // optional — if omitted, type-based matching is used at upload time
  "adjustment_method": "Range",       // optional — default: Range
  "is_active": true,                  // optional — default: true
  "pricebook": "Standard Price Book", // optional

  // Volume schedules — include "tiers" list:
  "tiers": [
    {
      "lower_bound": 5,
      "upper_bound": 10,              // null = open-ended
      "tier_type": "AdjustmentPercentage",  // AdjustmentPercentage | AdjustmentAmount | OverrideAmount
      "tier_value": 10.0,
      "product_code": "PROD-001",     // optional
      "psm_name": "One-Time"          // optional
    }
  ],

  // Attribute schedules — include "attribute_adjustments" list:
  "attribute_adjustments": [
    {
      "rule_name": "Red Color Upcharge",
      "usage_type": "Pricing",
      "conditions": [
        {
          "attribute": "Color",
          "product_code": "PROD-001",
          "operator": "equals",
          "value": "Red"
        }
      ],
      "adjustment": {
        "product_code": "PROD-001",
        "psm_name": "One-Time",
        "adjustment_type": "Percentage",  // Percentage | Amount | Override
        "adjustment_value": 10.0
      }
    }
  ],

  // Bundle schedules — include "bundle_adjustments" list:
  "bundle_adjustments": [
    {
      "product_code": "COMP-001",
      "psm_name": "One-Time",
      "parent_product_code": "BUNDLE-001",
      "parent_psm_name": "One-Time",
      "adjustment_type": "Percentage",
      "adjustment_value": 15.0
    }
  ]
}
"""

import json
import os
import sys
import argparse
import logging
from typing import Dict, List, Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: 'pyyaml' not found. Run:  pip install pyyaml")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

VALID_TYPES = {"Volume", "Attribute", "Bundle"}
VALID_TIER_TYPES = {"AdjustmentPercentage", "AdjustmentAmount", "OverrideAmount"}
VALID_ADJ_TYPES = {"Percentage", "Amount", "Override"}
VALID_OPERATORS = {
    "equals", "notequals", "lessthan", "lessorequal",
    "greaterthan", "greaterorequal", "existsin", "doesnotexistin",
}


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_file(path: str) -> Dict:
    if not os.path.isfile(path):
        log.info("File not found — will create: %s", path)
        return {"price_adjustment_schedules": []}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("price_adjustment_schedules", [])
    return data


def save_file(path: str, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("# ============================================================\n")
        f.write("# RCA Price Adjustments — Salesforce Revenue Cloud Advanced\n")
        f.write("# Edit directly or use /describe-price-adjustment in Claude Code\n")
        f.write("# Upload: python create_price_adjustments.py --catalog <this file>\n")
        f.write("# ============================================================\n\n")
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    log.info("File saved: %s", path)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate(data: Dict) -> List[str]:
    errors: List[str] = []
    stype = data.get("schedule_type", "")
    if stype not in VALID_TYPES:
        errors.append(f"schedule_type must be one of {sorted(VALID_TYPES)}, got: {stype!r}")
        return errors  # can't validate children without knowing type

    if stype == "Volume":
        tiers = data.get("tiers", [])
        if not tiers:
            errors.append("Volume schedule requires at least one entry in 'tiers'")
        for i, t in enumerate(tiers):
            if t.get("lower_bound") is None:
                errors.append(f"tiers[{i}].lower_bound is required")
            if t.get("tier_type") not in VALID_TIER_TYPES:
                errors.append(f"tiers[{i}].tier_type must be one of {sorted(VALID_TIER_TYPES)}")
            if t.get("tier_value") is None:
                errors.append(f"tiers[{i}].tier_value is required")

    elif stype == "Attribute":
        rules = data.get("attribute_adjustments", [])
        if not rules:
            errors.append("Attribute schedule requires at least one entry in 'attribute_adjustments'")
        for i, r in enumerate(rules):
            if not r.get("rule_name", "").strip():
                errors.append(f"attribute_adjustments[{i}].rule_name is required")
            for j, c in enumerate(r.get("conditions", [])):
                if not c.get("attribute", "").strip():
                    errors.append(f"attribute_adjustments[{i}].conditions[{j}].attribute is required")
                if c.get("operator", "").lower() not in VALID_OPERATORS:
                    errors.append(
                        f"attribute_adjustments[{i}].conditions[{j}].operator must be one of "
                        f"{sorted(VALID_OPERATORS)}"
                    )
                if c.get("value") is None:
                    errors.append(f"attribute_adjustments[{i}].conditions[{j}].value is required")
            adj = r.get("adjustment", {})
            if adj.get("adjustment_type") not in VALID_ADJ_TYPES:
                errors.append(
                    f"attribute_adjustments[{i}].adjustment.adjustment_type must be one of "
                    f"{sorted(VALID_ADJ_TYPES)}"
                )
            if adj.get("adjustment_value") is None:
                errors.append(f"attribute_adjustments[{i}].adjustment.adjustment_value is required")

    elif stype == "Bundle":
        items = data.get("bundle_adjustments", [])
        if not items:
            errors.append("Bundle schedule requires at least one entry in 'bundle_adjustments'")
        for i, b in enumerate(items):
            if not b.get("product_code", "").strip():
                errors.append(f"bundle_adjustments[{i}].product_code is required")
            if not b.get("parent_product_code", "").strip():
                errors.append(f"bundle_adjustments[{i}].parent_product_code is required")
            if b.get("adjustment_type") not in VALID_ADJ_TYPES:
                errors.append(
                    f"bundle_adjustments[{i}].adjustment_type must be one of {sorted(VALID_ADJ_TYPES)}"
                )
            if b.get("adjustment_value") is None:
                errors.append(f"bundle_adjustments[{i}].adjustment_value is required")

    return errors


# ---------------------------------------------------------------------------
# Build a clean schedule entry from the incoming JSON
# ---------------------------------------------------------------------------

def build_entry(data: Dict) -> Dict:
    stype = data["schedule_type"]
    entry: Dict[str, Any] = {"schedule_type": stype}

    name = str(data.get("name", "")).strip()
    if name:
        entry["name"] = name

    method = str(data.get("adjustment_method", "Range")).strip()
    if method:
        entry["adjustment_method"] = method

    entry["is_active"] = bool(data.get("is_active", True))

    pb = str(data.get("pricebook", "")).strip()
    if pb:
        entry["pricebook"] = pb

    if stype == "Volume":
        tiers = []
        for t in data.get("tiers", []):
            tier: Dict[str, Any] = {
                "lower_bound": float(t["lower_bound"]),
                "upper_bound": float(t["upper_bound"]) if t.get("upper_bound") is not None else None,
                "tier_type":   t["tier_type"],
                "tier_value":  float(t["tier_value"]),
            }
            if t.get("product_code"):
                tier["product_code"] = str(t["product_code"]).strip()
            if t.get("psm_name"):
                tier["psm_name"] = str(t["psm_name"]).strip()
            tiers.append(tier)
        entry["tiers"] = tiers

    elif stype == "Attribute":
        rules = []
        for r in data.get("attribute_adjustments", []):
            rule: Dict[str, Any] = {
                "rule_name":  str(r["rule_name"]).strip(),
                "usage_type": str(r.get("usage_type", "Pricing")).strip(),
            }
            conds = []
            for c in r.get("conditions", []):
                cond: Dict[str, Any] = {
                    "attribute": str(c["attribute"]).strip(),
                    "operator":  str(c["operator"]).lower().strip(),
                    "value":     c["value"],
                }
                if c.get("product_code"):
                    cond["product_code"] = str(c["product_code"]).strip()
                conds.append(cond)
            rule["conditions"] = conds

            adj = r.get("adjustment", {})
            adj_entry: Dict[str, Any] = {
                "adjustment_type":  str(adj["adjustment_type"]).strip(),
                "adjustment_value": float(adj["adjustment_value"]),
            }
            if adj.get("product_code"):
                adj_entry["product_code"] = str(adj["product_code"]).strip()
            if adj.get("psm_name"):
                adj_entry["psm_name"] = str(adj["psm_name"]).strip()
            rule["adjustment"] = adj_entry
            rules.append(rule)
        entry["attribute_adjustments"] = rules

    elif stype == "Bundle":
        items = []
        for b in data.get("bundle_adjustments", []):
            item: Dict[str, Any] = {
                "product_code":        str(b["product_code"]).strip(),
                "parent_product_code": str(b["parent_product_code"]).strip(),
                "adjustment_type":     str(b["adjustment_type"]).strip(),
                "adjustment_value":    float(b["adjustment_value"]),
            }
            if b.get("psm_name"):
                item["psm_name"] = str(b["psm_name"]).strip()
            if b.get("parent_psm_name"):
                item["parent_psm_name"] = str(b["parent_psm_name"]).strip()
            items.append(item)
        entry["bundle_adjustments"] = items

    return entry


# ---------------------------------------------------------------------------
# Merge into existing file
# ---------------------------------------------------------------------------

def _schedule_key(e: Dict) -> str:
    """Unique key for matching an existing schedule entry. Name + type if named; type only if not."""
    name = str(e.get("name", "")).strip()
    stype = str(e.get("schedule_type", "")).strip()
    return f"{stype}::{name}" if name else f"{stype}::"


def merge_entry(schedules: List[Dict], new_entry: Dict) -> str:
    """
    Merge new_entry into the schedules list.
    - If a schedule with the same key (type+name) exists, merge its child lists into it.
    - Otherwise, append as a new entry.
    Returns 'merged' or 'appended'.
    """
    key = _schedule_key(new_entry)
    stype = new_entry["schedule_type"]

    for existing in schedules:
        if _schedule_key(existing) == key:
            # Merge child lists — append items not already present (by rule_name / product_code)
            if stype == "Volume":
                existing.setdefault("tiers", [])
                for new_tier in new_entry.get("tiers", []):
                    dup = any(
                        t.get("lower_bound") == new_tier.get("lower_bound")
                        and t.get("product_code") == new_tier.get("product_code")
                        for t in existing["tiers"]
                    )
                    if not dup:
                        existing["tiers"].append(new_tier)
            elif stype == "Attribute":
                existing.setdefault("attribute_adjustments", [])
                existing_rule_names = {r.get("rule_name") for r in existing["attribute_adjustments"]}
                for rule in new_entry.get("attribute_adjustments", []):
                    if rule.get("rule_name") not in existing_rule_names:
                        existing["attribute_adjustments"].append(rule)
            elif stype == "Bundle":
                existing.setdefault("bundle_adjustments", [])
                existing_keys = {
                    (b.get("product_code"), b.get("parent_product_code"))
                    for b in existing["bundle_adjustments"]
                }
                for item in new_entry.get("bundle_adjustments", []):
                    if (item.get("product_code"), item.get("parent_product_code")) not in existing_keys:
                        existing["bundle_adjustments"].append(item)
            return "merged"

    schedules.append(new_entry)
    return "appended"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add or update a price adjustment schedule entry in the YAML file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", "-j", required=True, metavar="FILE",
                        help="JSON file describing the adjustment entry")
    parser.add_argument("--catalog", "-c", default="rca_adj_session.yaml", metavar="FILE",
                        help="YAML file to update (default: rca_adj_session.yaml)")
    parser.add_argument("--fresh", action="store_true",
                        help="Clear the file before writing (start a new session)")
    args = parser.parse_args()

    if args.fresh:
        save_file(args.catalog, {"price_adjustment_schedules": []})
        log.info("Session file cleared: %s", args.catalog)

    # Load JSON
    if not os.path.isfile(args.json):
        log.error("JSON file not found: %s", args.json)
        sys.exit(1)
    with open(args.json, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            log.error("Invalid JSON: %s", exc)
            sys.exit(1)

    # Validate
    errors = validate(data)
    if errors:
        log.error("Input validation failed:")
        for err in errors:
            log.error("  ✗ %s", err)
        sys.exit(1)

    new_entry = build_entry(data)
    file_data = load_file(args.catalog)
    schedules: List[Dict] = file_data["price_adjustment_schedules"]

    action = merge_entry(schedules, new_entry)
    save_file(args.catalog, file_data)

    stype = new_entry["schedule_type"]
    name  = new_entry.get("name", "(type-matched)")
    log.info("──────────────────────────────────────────────────")
    log.info("Adjustments file updated: %s", args.catalog)
    log.info("  Action:        %s", action)
    log.info("  Schedule type: %s", stype)
    log.info("  Name:          %s", name)
    if stype == "Volume":
        log.info("  Tiers:         %d", len(new_entry.get("tiers", [])))
    elif stype == "Attribute":
        log.info("  Rules:         %d", len(new_entry.get("attribute_adjustments", [])))
    elif stype == "Bundle":
        log.info("  Bundle items:  %d", len(new_entry.get("bundle_adjustments", [])))
    log.info("  Total schedules in file: %d", len(schedules))
    log.info("──────────────────────────────────────────────────")
    log.info("To upload: python create_price_adjustments.py --catalog %s",
             os.path.basename(args.catalog))


if __name__ == "__main__":
    main()
