#!/usr/bin/env python3
"""
update_rca_catalog.py
=====================
Adds or updates one product or bundle in the YAML catalog file.
Called by Claude Code after collecting all required info through the
/describe-rca-product natural language conversation.

Usage:
  python update_rca_catalog.py --json <product.json> [--catalog rca_session.yaml] [--overwrite] [--fresh]

Flags:
  --json      -j   JSON file describing the product or bundle (required).
  --catalog   -c   YAML catalog to update. Default: rca_catalog.yaml
  --overwrite      Replace an existing entry with the same code instead of skipping.
  --fresh          Clear the catalog before writing (use at the start of each new session).

JSON Input Schema
-----------------
Standalone product:
{
  "type": "product",
  "product": {
    "code":        "PROD-001",          // required
    "name":        "My Product",        // required
    "description": "...",
    "family":      "Software",
    "active":      true,
    "uom":         "Each",
    "sku":         "SKU-001"
  },
  "psm_options": ["Annual Termed", "Monthly Evergreen"],
  "pricebook_entries": [
    { "pricebook": "Standard Price Book", "price": 10000.00, "currency": "USD" }
  ],
  "classification": "Software",         // optional — ProductClassification Name or Code
  "catalog":  "Main Product Catalog",   // optional — ProductCatalog Name
  "category": "Software > Platform",    // optional — ProductCategory Name or "Parent > Child" path
  "catalogs": [                          // optional — define/extend the catalog hierarchy
    {
      "name": "Main Product Catalog", "code": "MAIN",
      "categories": [
        { "name": "Software", "code": "SW",
          "categories": [{ "name": "Platform", "code": "SW-PLAT" }] }
      ]
    }
  ],
  "attributes": [                        // optional
    {
      "name":              "Edition",    // AttributeDefinition Name (required)
      "data_type":         "Picklist",  // optional — needed only when creating a new AttributeDefinition
      "attr_code":         "EDITION",  // optional — AttributeDefinition.Code
      "default_value":     "Standard", // optional
      "required":          false,       // optional
      "sequence":          1,           // optional
      "hidden":            false,       // optional
      "read_only":         false,       // optional
      "help_text":         "",          // optional

      // Picklist-specific — include when data_type == "Picklist"
      "picklist_name":      "Edition Values",  // AttributePicklist.Name (looked up or created)
      "picklist_code":      "EDITION_PL",      // optional — AttributePicklist.Code
      "picklist_data_type": "Text",            // optional — AttributePicklist.DataType, default "Text"
      "picklist_values": [
        { "value": "Standard",     "code": "STD", "sequence": 1, "is_default": true  },
        { "value": "Professional", "code": "PRO", "sequence": 2 },
        { "value": "Enterprise",   "code": "ENT", "sequence": 3, "display_value": "Enterprise Edition" }
      ]
    }
  ]
}

Bundle — same as above, plus:
{
  "type": "bundle",
  "product": { ... },
  "psm_options": [...],
  "pricebook_entries": [{ "pricebook": "Standard Price Book", "price": 0.00, "currency": "USD" }],
  "classification": "Software",
  "attributes": [...],
  "groups": [
    {
      "name":           "Core Platform",
      "code":           "CORE",
      "min_selections": 1,
      "max_selections": 1,
      "sequence":       1,
      "components": [
        { "code": "PROD-001", "min_qty": 1, "max_qty": 1, "required": true, "default": true, "sequence": 1 }
      ]
    }
  ]
}
"""

import json
import os
import sys
import argparse
import logging
from typing import Dict, List, Any

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

EMPTY_CATALOG: Dict[str, Any] = {"catalogs": [], "products": [], "bundles": []}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_catalog(path: str) -> Dict:
    if not os.path.isfile(path):
        log.info("Catalog not found — will create: %s", path)
        return {"catalogs": [], "products": [], "bundles": []}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("catalogs", [])
    data.setdefault("products", [])
    data.setdefault("bundles", [])
    return data


def save_catalog(path: str, catalog: Dict) -> None:
    # Drop empty catalogs list to keep the file clean when no catalog hierarchy is defined
    output = {k: v for k, v in catalog.items() if k != "catalogs" or v}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# ============================================================\n")
        f.write("# RCA Product Catalog — Salesforce Revenue Cloud Advanced (ARM)\n")
        f.write("# Edit directly or use /describe-rca-product in Claude Code\n")
        f.write("# Upload: python create_rca_products.py --catalog rca_catalog.yaml\n")
        f.write("# ============================================================\n\n")
        yaml.dump(output, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    log.info("Catalog saved: %s", path)


def validate_input(data: Dict) -> List[str]:
    errors = []
    if data.get("type") not in ("product", "bundle"):
        errors.append("'type' must be 'product' or 'bundle'")
    p = data.get("product", {})
    if not p.get("code", "").strip():
        errors.append("product.code is required")
    if not p.get("name", "").strip():
        errors.append("product.name is required")
    if data.get("type") == "bundle":
        if not data.get("groups"):
            errors.append("Bundle requires at least one group in 'groups'")
        else:
            for i, g in enumerate(data["groups"]):
                if not g.get("name", "").strip():
                    errors.append(f"groups[{i}].name is required")
                for j, c in enumerate(g.get("components", [])):
                    if not c.get("code", "").strip():
                        errors.append(f"groups[{i}].components[{j}].code is required")
    return errors


def build_catalog_entry(data: Dict) -> Dict:
    """Convert the incoming JSON structure to the YAML catalog format."""
    p = data["product"]
    entry: Dict[str, Any] = {
        "code": p["code"].strip(),
        "name": p["name"].strip(),
    }
    for field in ("description", "family", "uom", "sku"):
        val = str(p.get(field, "")).strip()
        if val:
            entry[field] = val
    entry["active"] = bool(p.get("active", True))

    # PSM options — accept both list-of-strings and list-of-dicts
    psm_raw = data.get("psm_options", [])
    psm_names = []
    for item in psm_raw:
        if isinstance(item, str):
            psm_names.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("ProductSellingModelName", item.get("name", "")).strip()
            if name:
                psm_names.append(name)
    if psm_names:
        entry["psm_options"] = psm_names

    # Pricebook entries — accept both old and new key names
    pbe_raw = data.get("pricebook_entries", [])
    pbes = []
    for item in pbe_raw:
        pb_name = (item.get("pricebook") or item.get("PricebookName", "")).strip()
        price   = float(item.get("price", item.get("UnitPrice", 0)))
        curr    = str(item.get("currency", item.get("CurrencyIsoCode", "USD"))).strip() or "USD"
        if pb_name:
            pbes.append({"pricebook": pb_name, "price": price, "currency": curr})
    if pbes:
        # Standard Price Book first
        pbes.sort(key=lambda x: 0 if "standard" in x["pricebook"].lower() else 1)
        entry["pricebook_entries"] = pbes

    # Classification
    classification = str(data.get("classification", "")).strip()
    if classification:
        entry["classification"] = classification

    # Catalog placement
    catalog_val = str(data.get("catalog", "")).strip()
    if catalog_val:
        entry["catalog"] = catalog_val
    category_val = str(data.get("category", "")).strip()
    if category_val:
        entry["category"] = category_val

    # Attributes (AttributeDefinition references, with optional picklist creation)
    attrs_raw = data.get("attributes", [])
    attrs = []
    for attr in attrs_raw:
        attr_name = str(attr.get("name") or attr.get("code", "")).strip()
        if not attr_name:
            continue
        attr_entry: Dict[str, Any] = {"name": attr_name}

        # Core AttributeDefinition fields
        if attr.get("data_type"):
            attr_entry["data_type"] = str(attr["data_type"]).strip()
        if attr.get("attr_code"):
            attr_entry["attr_code"] = str(attr["attr_code"]).strip()
        if attr.get("default_value") is not None:
            attr_entry["default_value"] = str(attr["default_value"])
        if attr.get("required") is not None:
            attr_entry["required"] = bool(attr["required"])
        if attr.get("sequence") is not None:
            attr_entry["sequence"] = int(attr["sequence"])
        if attr.get("hidden") is not None:
            attr_entry["hidden"] = bool(attr["hidden"])
        if attr.get("read_only") is not None:
            attr_entry["read_only"] = bool(attr["read_only"])
        if attr.get("help_text"):
            attr_entry["help_text"] = str(attr["help_text"])

        # Picklist sub-structure
        if attr.get("picklist_name"):
            attr_entry["picklist_name"] = str(attr["picklist_name"]).strip()
        if attr.get("picklist_code"):
            attr_entry["picklist_code"] = str(attr["picklist_code"]).strip()
        if attr.get("picklist_data_type"):
            attr_entry["picklist_data_type"] = str(attr["picklist_data_type"]).strip()

        pv_raw = attr.get("picklist_values", [])
        if pv_raw:
            pvs = []
            for seq_j, pv in enumerate(pv_raw, start=1):
                val = str(pv.get("value", "")).strip()
                if not val:
                    continue
                pv_entry: Dict[str, Any] = {
                    "value":    val,
                    "code":     str(pv.get("code", val)).strip(),
                    "sequence": int(pv.get("sequence", seq_j)),
                }
                if pv.get("display_value"):
                    pv_entry["display_value"] = str(pv["display_value"])
                if pv.get("is_default") is not None:
                    pv_entry["is_default"] = bool(pv["is_default"])
                if pv.get("abbreviation"):
                    pv_entry["abbreviation"] = str(pv["abbreviation"])
                pvs.append(pv_entry)
            if pvs:
                attr_entry["picklist_values"] = pvs

        attrs.append(attr_entry)
    if attrs:
        entry["attributes"] = attrs

    # Bundle groups
    if data.get("type") == "bundle":
        groups = []
        for seq_i, g in enumerate(data.get("groups", []), start=1):
            group: Dict[str, Any] = {
                "name":     g.get("name", "").strip(),
                "sequence": int(g.get("sequence", seq_i)),
            }
            if g.get("code", "").strip():
                group["code"] = g["code"].strip()
            if g.get("min_selections") is not None:
                group["min_selections"] = int(g["min_selections"])
            if g.get("max_selections") is not None:
                group["max_selections"] = int(g["max_selections"])

            components = []
            for seq_j, c in enumerate(g.get("components", []), start=1):
                comp: Dict[str, Any] = {
                    "code":     c.get("code", c.get("ComponentProductCode", "")).strip(),
                    "required": bool(c.get("required", c.get("ComponentIsRequired", False))),
                    "default":  bool(c.get("default",  c.get("ComponentIsDefault",  False))),
                    "sequence": int(c.get("sequence",  c.get("ComponentSequence",   seq_j))),
                }
                raw_min = c.get("min_qty", c.get("ComponentMinQty"))
                raw_max = c.get("max_qty", c.get("ComponentMaxQty"))
                if raw_min is not None:
                    comp["min_qty"] = float(raw_min)
                if raw_max is not None:
                    comp["max_qty"] = float(raw_max)
                if "is_quantity_editable" in c:
                    comp["is_quantity_editable"] = bool(c["is_quantity_editable"])
                if "quantity_scale_method" in c:
                    comp["quantity_scale_method"] = str(c["quantity_scale_method"])
                if comp["code"]:
                    components.append(comp)
            group["components"] = components
            groups.append(group)
        entry["groups"] = groups

    return entry


# ---------------------------------------------------------------------------
# Overwrite diff + confirmation
# ---------------------------------------------------------------------------
def _diff_entries(old: Dict, new: Dict) -> List[str]:
    """Return human-readable lines describing field-level changes between two catalog entries."""
    lines = []
    all_keys = sorted(set(old) | set(new))
    for key in all_keys:
        oval = old.get(key)
        nval = new.get(key)
        if oval == nval:
            continue
        if oval is None:
            lines.append(f"  + {key}: {nval!r}  (new field)")
        elif nval is None:
            lines.append(f"  - {key}: {oval!r}  (field removed)")
        else:
            lines.append(f"  ~ {key}:")
            lines.append(f"      was: {oval!r}")
            lines.append(f"      now: {nval!r}")
    return lines


def _confirm_overwrite(code: str, old_entry: Dict, new_entry: Dict,
                       skip_prompt: bool) -> bool:
    """Show a diff and optionally prompt the user. Returns True to proceed."""
    diff = _diff_entries(old_entry, new_entry)
    if not diff:
        log.info("Overwrite requested but no field changes detected for %s — skipping.", code)
        return False

    log.warning("══════════════════════════════════════════════════")
    log.warning("OVERWRITE REQUESTED — %s", code)
    log.warning("  Field changes:")
    for line in diff:
        log.warning(line)
    log.warning("══════════════════════════════════════════════════")

    if skip_prompt:
        log.info("--yes flag set — proceeding with overwrite.")
        return True

    print()
    while True:
        try:
            answer = input(
                "Confirm overwrite?\n"
                "  yes    — apply the changes shown above\n"
                "  cancel — leave the existing entry unchanged\n"
                "> "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = "cancel"

        if answer in ("yes", "y"):
            return True
        elif answer in ("cancel", "no", "n", ""):
            log.info("Overwrite cancelled — existing entry kept.")
            return False
        else:
            print("Please enter 'yes' or 'cancel'.")


# ---------------------------------------------------------------------------
# Catalog hierarchy merge helper
# ---------------------------------------------------------------------------
def _merge_catalogs(existing: List[Dict], incoming: List[Dict]) -> None:
    """Merge incoming catalog definitions into existing, matching by name. Recursive."""
    existing_by_name: Dict[str, Dict] = {
        str(c.get("name", "")).strip(): c for c in existing
    }
    for inc in incoming:
        name = str(inc.get("name", "")).strip()
        if not name:
            continue
        if name in existing_by_name:
            target = existing_by_name[name]
            # Merge fields that aren't already set
            for field in ("code", "status"):
                if inc.get(field) and not target.get(field):
                    target[field] = inc[field]
            # Recurse into categories
            if inc.get("categories"):
                target.setdefault("categories", [])
                _merge_catalogs(target["categories"], inc["categories"])
        else:
            existing.append(inc)
            existing_by_name[name] = inc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add or update a product/bundle in the YAML catalog",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", "-j", required=True, metavar="FILE",
                        help="JSON file describing the product or bundle")
    parser.add_argument("--catalog", "-c", default="rca_catalog.yaml", metavar="FILE",
                        help="YAML catalog to update (default: rca_catalog.yaml)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace existing entry with the same code (shows diff, prompts for confirmation)")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the overwrite confirmation prompt (use with --overwrite)")
    parser.add_argument("--fresh", action="store_true",
                        help="Clear the catalog before writing (start a new session)")
    args = parser.parse_args()

    # Fresh session: wipe catalog before writing
    if args.fresh:
        save_catalog(args.catalog, {"products": [], "bundles": []})
        log.info("Session catalog cleared: %s", args.catalog)

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
    errors = validate_input(data)
    if errors:
        log.error("Input validation failed:")
        for err in errors:
            log.error("  ✗ %s", err)
        sys.exit(1)

    entry_type = data["type"]   # "product" or "bundle"
    code = data["product"]["code"].strip()
    new_entry = build_catalog_entry(data)

    # Load existing catalog
    catalog = load_catalog(args.catalog)

    # Merge any catalogs block from the incoming JSON into the YAML catalog hierarchy
    incoming_catalogs: List[Dict] = data.get("catalogs", [])
    if incoming_catalogs:
        existing_catalogs: List[Dict] = catalog.setdefault("catalogs", [])
        _merge_catalogs(existing_catalogs, incoming_catalogs)
        log.info("Merged catalog hierarchy: %d top-level catalog(s)", len(existing_catalogs))

    section: List = catalog[f"{entry_type}s"]  # "products" or "bundles"

    # Check for existing entry with same code
    existing_idx = next(
        (i for i, e in enumerate(section) if str(e.get("code", "")).strip() == code),
        None,
    )
    if existing_idx is not None:
        if args.overwrite:
            old_entry = section[existing_idx]
            if _confirm_overwrite(code, old_entry, new_entry, skip_prompt=args.yes):
                section[existing_idx] = new_entry
                log.info("Overwrote existing %s in catalog: %s", entry_type, code)
            else:
                sys.exit(0)
        else:
            log.warning(
                "%s '%s' already exists in catalog — skipping. "
                "Use --overwrite to replace it.", entry_type.capitalize(), code
            )
            sys.exit(0)
    else:
        section.append(new_entry)
        log.info("Added %s to catalog: %s  (%s)", entry_type, code, new_entry.get("name", ""))

    save_catalog(args.catalog, catalog)

    # Print summary
    log.info("──────────────────────────────────────────────────")
    log.info("Catalog updated: %s", args.catalog)
    log.info("  Type:              %s", entry_type)
    log.info("  Code:              %s", code)
    log.info("  Name:              %s", new_entry.get("name", ""))
    log.info("  PSM options:       %s", ", ".join(new_entry.get("psm_options", [])) or "none")
    log.info("  Pricebook entries: %d", len(new_entry.get("pricebook_entries", [])))
    log.info("  Classification:    %s", new_entry.get("classification", "") or "none")
    log.info("  Catalog:           %s", new_entry.get("catalog", "") or "none")
    log.info("  Category:          %s", new_entry.get("category", "") or "none")
    log.info("  Attributes:        %d", len(new_entry.get("attributes", [])))
    if entry_type == "bundle":
        total_components = sum(len(g.get("components", [])) for g in new_entry.get("groups", []))
        log.info("  Groups:            %d  (%d components)", len(new_entry.get("groups", [])), total_components)
    log.info("──────────────────────────────────────────────────")
    log.info("To upload: python create_rca_products.py --catalog %s", os.path.basename(args.catalog))


if __name__ == "__main__":
    main()
