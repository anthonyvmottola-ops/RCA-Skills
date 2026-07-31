#!/usr/bin/env python3
"""
create_context_field.py
========================
Creates a custom field via the Tooling API, then wires it into Revenue Cloud
Advanced's Sales Transaction Context Definition — a Context Attribute + its
Context Tag on the correct Context Node, and a Context Attribute Mapping per
target object (Quote/QuoteLineItem/Order/OrderItem) — so Pricing Procedures,
Constraint Models, and Apex context hooks can read/write the field.

Why this discovers the live Context Definition instead of hardcoding a name:
confirmed live against a real org that MULTIPLE ContextDefinitions can exist
extended from the same standard `SalesTransactionContext__stdctx` (e.g. one
customer-named one and one unused decoy), and only the one actually
referenced by a live Pricing Procedure is the one that matters. This script
always resolves it via `ExpressionSetDefinitionContextDefinition` — never by
matching a DeveloperName/MasterLabel pattern.

Why Context* records (ContextAttribute / ContextTag / ContextAttributeMapping)
are NEVER updated, only created: Context Definitions are append-only while
their version is active — removing or changing a node/attribute/mapping
requires deactivating the whole ContextDefinitionVersion, which cascades to
every Pricing Procedure/Constraint Model depending on it. An "update" is
functionally almost as risky as a delete for anything already live, so this
script only ever creates new Context* records and WARNS (never patches) on
any drift between an existing record and the catalog.

Why `ContextNodeMapping.Object` drives resolution instead of `ContextMapping.
Title`: matching directly on the real target SObject name (e.g. "QuoteLineItem")
is robust to org customization — a `ContextMapping` could theoretically be
renamed, but the `Object` value on its child `ContextNodeMapping` rows must
always equal the real API name to function at all.

Explicitly NOT related to and NOT reusing: `create_rca_products.py`'s
AttributeCategory / AttributePicklist / AttributeDefinition /
ProductClassificationAttr machinery. That is the product-catalog attribute
system (Product2/bundle configuration attributes) — a completely different
object model from Context Definition attributes. Nothing in this script
references those objects.

Three platform behaviors below were NOT documented anywhere found during
research and were only discovered by attempting real live creates and
comparing the result against a known-working standard example:

1. `ContextAttribute.Title` / `ContextTag.Title` must carry the standard
   custom-artifact `__c` suffix in an extended Context Definition, or the
   create fails with INVALID_API_INPUT. Handled via `derive_api_name()`.
2. `ContextAttribute.IsKey`/`IsValue` do NOT mean "this attribute holds a
   key/value" in the plain-English sense — they flag the KEY/VALUE columns
   of a *transposed* attribute pair on a transposable node (e.g.
   `AttributeKey`/`AttributeValue` on `SalesTransactionItemAttribute`).
   Setting `IsValue: true` on a normal direct attribute makes the platform
   reject the create with "Parent node should be transposable" even though
   the target node (e.g. `SalesTransactionItem`) legitimately accepts new
   direct attributes when this flag is left `false`, matching every
   standard attribute there (`Discount`, `UnitPrice`, ...).
3. A working `ContextAttributeMapping` is not enough on its own — every
   live, functioning mapping also has exactly one child
   `ContextAttrHydrationDetail` (`ObjectName` + `QueryAttribute`, mirroring
   the mapping's own object/field). Without it, `ContextAttributeMapping`
   creates successfully with no error, but the mapping never appears in
   Setup's Map Data builder. This object doesn't even appear in
   `ContextAttributeMapping`'s own field list — it's a separate child
   SObject, `ContextAttrHydrationDetail`, discovered only by describing
   every "Context"-named SObject in the org and comparing a known-good
   mapping's full record graph against one this script had just created.

Authentication uses `sf org display` — no passwords stored.

Usage:
  python create_context_field.py --catalog context_fields.yaml [--org <alias>] --discover-only
  python create_context_field.py --catalog context_fields.yaml [--org <alias>] --dry-run
  python create_context_field.py --catalog context_fields.yaml [--org <alias>]

Flags:
  --catalog                 Path to the YAML catalog file (context_fields: section)
  --org, -o                 sf CLI org alias. Omit to use the default authenticated org.
  --discover-only           Resolve and print the live Context Definition/Node/Mapping
                            chain for every catalog entry. Makes zero writes.
  --dry-run                 Preview every record that WOULD be created (field + context
                            wiring) without touching the org.
  --context-definition-id   Explicit override — skip auto-discovery and use this
                            ContextDefinition Id directly. Use when discovery reports
                            more than one live candidate.

Requirements:
  pip install requests pyyaml
"""

import argparse
import json
import re
import subprocess
import sys
import os
import logging
from typing import Any, Dict, List, Optional

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
# Static SObject -> expected ContextNode / ContextMapping lookup.
#
# The "mapping" value is more than a preview hint: confirmed live that a
# single object (e.g. "QuoteLineItem") can appear under MORE THAN ONE
# ContextMapping at once — e.g. both "QuoteEntitiesMapping" (the general
# pricing/transaction mapping) and "QuoteToContractSlsTrxnMapping" (a
# narrower mapping used only by the Create Contract invocable action).
# Picking arbitrarily between them is wrong, so resolve_target_object()
# actively prefers whichever live ContextNodeMapping's parent ContextMapping
# Title matches this table's "mapping" value (the standard "<Family>
# EntitiesMapping" naming convention RCA ships with) before falling back to
# "first match" with a loud warning listing every alternative found.
#
# v1 ships only these four objects; an unknown object errors clearly rather
# than guessing at a node/mapping it hasn't verified exists.
# ---------------------------------------------------------------------------
OBJECT_NODE_LOOKUP: Dict[str, Dict[str, str]] = {
    "Quote":             {"node": "SalesTransaction",     "mapping": "QuoteEntitiesMapping"},
    "QuoteLineItem":     {"node": "SalesTransactionItem", "mapping": "QuoteEntitiesMapping"},
    "Order":             {"node": "SalesTransaction",     "mapping": "OrderEntitiesMapping"},
    "OrderItem":         {"node": "SalesTransactionItem", "mapping": "OrderEntitiesMapping"},
    # Confirmed live: Asset/AssetAction/AssetActionSource each sit under TWO
    # ContextMappings — their own "AssetEntitiesMapping" and the generic
    # default "SalesTransaction" mapping. Without this entry, resolution
    # would fall back to "first match found" (order not guaranteed) instead
    # of confidently picking AssetEntitiesMapping.
    "Asset":             {"node": "Asset",             "mapping": "AssetEntitiesMapping"},
    "AssetAction":       {"node": "AssetAction",       "mapping": "AssetEntitiesMapping"},
    "AssetActionSource": {"node": "AssetActionSource", "mapping": "AssetEntitiesMapping"},
    # Contract's own mapping is titled "ContractNodeMapping", not
    # "ContractEntitiesMapping" — confirmed live; the "<Family>EntitiesMapping"
    # naming convention isn't universal, which is exactly why this table
    # stores the literal confirmed title rather than deriving it from a
    # pattern.
    "Contract":          {"node": "Contract",          "mapping": "ContractNodeMapping"},
}


def esc(value: str) -> str:
    """Escape a value for safe interpolation into a SOQL string literal."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def ids_literal(ids: List[str]) -> str:
    return ", ".join(f"'{esc(i)}'" for i in ids)


def is_dry_id(value: str) -> bool:
    """True for the synthetic "DRY-<sobject>-<hash>" ids SalesforceClient.create
    returns in dry-run mode — never a real Salesforce Id, so it can't be used
    in a live SOQL query (400 Bad Request)."""
    return str(value).startswith("DRY-")


def derive_api_name(label: str, suffix: str = "", max_length: int = 40) -> str:
    """Normalizes a human-readable label into a valid Salesforce developer/API
    name: letters, digits, and underscores only, starting with a letter, no
    consecutive/trailing underscores, truncated to leave room for `suffix`
    (e.g. "__c"). Idempotent — safe on labels or already-formatted names."""
    if suffix and label.endswith(suffix):
        label = label[: -len(suffix)]
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", label.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "Field"
    if not cleaned[0].isalpha():
        cleaned = "X_" + cleaned
    available = max_length - len(suffix)
    if len(cleaned) > available:
        cleaned = cleaned[:available].rstrip("_")
    return cleaned + suffix


# ---------------------------------------------------------------------------
# Salesforce REST + Tooling client
# ---------------------------------------------------------------------------
class SalesforceClient:
    def __init__(self, access_token: str, instance_url: str,
                 api_version: str = "61.0", dry_run: bool = False):
        self.instance_url = instance_url.rstrip("/")
        self.rest_base = f"{self.instance_url}/services/data/v{api_version}"
        self.tooling_base = f"{self.rest_base}/tooling"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self.dry_run = dry_run

    def _query(self, base_url: str, soql: str) -> List[Dict]:
        resp = requests.get(f"{base_url}/query", headers=self.headers,
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

    def query(self, soql: str) -> List[Dict]:
        return self._query(self.rest_base, soql)

    def query_tooling(self, soql: str) -> List[Dict]:
        return self._query(self.tooling_base, soql)

    def _create(self, base_url: str, sobject: str, payload: Dict) -> str:
        if self.dry_run:
            log.info("[DRY-RUN] Would create %s: %s", sobject, json.dumps(payload))
            return f"DRY-{sobject}-{abs(hash(json.dumps(payload, sort_keys=True))) % 10**9:09d}"
        resp = requests.post(f"{base_url}/sobjects/{sobject}",
                              headers=self.headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"Create {sobject} failed [{resp.status_code}]: {resp.text}\n"
                f"Payload: {json.dumps(payload)}"
            )
        record_id: str = resp.json()["id"]
        log.info("Created %-30s Id: %s", sobject, record_id)
        return record_id

    def create(self, sobject: str, payload: Dict) -> str:
        return self._create(self.rest_base, sobject, payload)

    def create_tooling(self, sobject: str, payload: Dict) -> str:
        return self._create(self.tooling_base, sobject, payload)

    def _update(self, base_url: str, sobject: str, record_id: str, payload: Dict) -> None:
        if self.dry_run:
            log.info("[DRY-RUN] Would update %s %s: %s", sobject, record_id, json.dumps(payload))
            return
        resp = requests.patch(f"{base_url}/sobjects/{sobject}/{record_id}",
                               headers=self.headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(
                f"Update {sobject} {record_id} failed [{resp.status_code}]: {resp.text}"
            )
        log.info("Updated %-30s Id: %s", sobject, record_id)

    def update(self, sobject: str, record_id: str, payload: Dict) -> None:
        self._update(self.rest_base, sobject, record_id, payload)

    def update_tooling(self, sobject: str, record_id: str, payload: Dict) -> None:
        self._update(self.tooling_base, sobject, record_id, payload)


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


# ---------------------------------------------------------------------------
# Custom field creation (Tooling API) — ported from Org QuickStart Project's
# create_custom_fields.py. Not imported cross-repo (the two repos share zero
# code, only conventions) — this is a deliberate port, not a dependency.
# ---------------------------------------------------------------------------
def build_field_metadata(entry: Dict) -> Dict:
    field_type = entry["field_type_sf"]
    meta: Dict[str, Any] = {
        "label": entry["label"],
        "type": field_type,
        "description": entry.get("description", ""),
        "required": entry.get("required", False),
    }

    if field_type == "Text":
        meta["length"] = entry.get("length", 255)
    elif field_type == "TextArea":
        pass
    elif field_type == "LongTextArea":
        meta["length"] = entry.get("length", 32768)
        meta["visibleLines"] = entry.get("visible_lines", 5)
    elif field_type in ("Number", "Currency", "Percent"):
        meta["precision"] = entry.get("precision", 18)
        meta["scale"] = entry.get("scale", 2)
    elif field_type in ("Date", "DateTime", "Email", "Phone", "Url"):
        pass
    elif field_type == "Checkbox":
        meta["defaultValue"] = entry.get("default_value", False)
        meta.pop("required", None)  # Checkbox fields cannot be required
    elif field_type in ("Picklist", "MultiselectPicklist"):
        values = entry.get("picklist_values", [])
        meta["valueSet"] = {
            "restricted": entry.get("restricted", True),
            "valueSetDefinition": {
                "sorted": False,
                "value": [{"fullName": v, "default": False, "label": v} for v in values],
            },
        }
        if field_type == "MultiselectPicklist":
            meta["visibleLines"] = entry.get("visible_lines", 4)
    else:
        raise ValueError(f"Unsupported field_type_sf: {field_type}")

    return meta


def find_existing_custom_field(client: SalesforceClient, object_name: str,
                                developer_name: str) -> Optional[Dict]:
    recs = client.query_tooling(
        f"SELECT Id, Metadata FROM CustomField WHERE TableEnumOrId = '{esc(object_name)}' "
        f"AND DeveloperName = '{esc(developer_name)}'"
    )
    return recs[0] if recs else None


def merge_picklist_values(client: SalesforceClient, existing_field: Dict, entry: Dict,
                           full_field_api_name: str) -> None:
    """Adds catalog picklist_values missing from an already-existing field,
    never removing/reordering existing ones. PATCHing CustomField Metadata is
    full-replace, and the read shape (valueName, describe-only null fields)
    isn't valid to echo back on write — see create_custom_fields.py for the
    confirmed-live detail this logic is ported from."""
    metadata = existing_field.get("Metadata") or {}
    value_set = metadata.get("valueSet")
    if not value_set:
        log.warning("'%s' has no valueSet (not a picklist?) — skipping value merge",
                    full_field_api_name)
        return

    existing_raw_values = value_set.get("valueSetDefinition", {}).get("value", [])
    existing_defaults: Dict[str, bool] = {}
    existing_order: List[str] = []
    for v in existing_raw_values:
        name = v.get("valueName") or v.get("fullName")
        existing_order.append(name)
        existing_defaults[name] = v.get("default", False)

    catalog_names = set(entry.get("picklist_values", []))
    extra_names = [name for name in existing_order if name not in catalog_names]
    if extra_names:
        log.warning("%d existing value(s) on '%s' not in catalog (not modified): %s",
                    len(extra_names), full_field_api_name, ", ".join(extra_names))

    new_names = [v for v in entry.get("picklist_values", []) if v not in existing_defaults]
    if not new_names:
        log.info("Picklist values already up to date (%d value(s))", len(existing_raw_values))
        return

    merged_names = existing_order + new_names
    clean_metadata = {
        "label": metadata.get("label") or entry.get("label", full_field_api_name),
        "type": metadata.get("type") or entry["field_type_sf"],
        "description": metadata.get("description") or "",
        "required": metadata.get("required") or False,
        "valueSet": {
            "restricted": value_set.get("restricted", True),
            "valueSetDefinition": {
                "sorted": False,
                "value": [
                    {"fullName": name, "default": existing_defaults.get(name, False), "label": name}
                    for name in merged_names
                ],
            },
        },
    }
    client.update_tooling("CustomField", existing_field["Id"], {"Metadata": clean_metadata})
    log.info("Added %d new picklist value(s) on '%s': %s",
              len(new_names), full_field_api_name, ", ".join(new_names))


def resolve_permission_set_id_for_profile(client: SalesforceClient, profile_name: str) -> Optional[str]:
    recs = client.query(
        f"SELECT Id FROM PermissionSet WHERE Profile.Name = '{esc(profile_name)}' "
        f"AND IsOwnedByProfile = true"
    )
    return recs[0]["Id"] if recs else None


def resolve_permission_set_id_by_name(client: SalesforceClient, ps_name: str) -> Optional[str]:
    recs = client.query(f"SELECT Id FROM PermissionSet WHERE Name = '{esc(ps_name)}'")
    return recs[0]["Id"] if recs else None


def upsert_field_permissions(client: SalesforceClient, parent_id: str,
                              field_api_name: str, access: str) -> None:
    can_edit = access == "Edit"
    existing = client.query(
        f"SELECT Id, PermissionsRead, PermissionsEdit FROM FieldPermissions "
        f"WHERE ParentId = '{parent_id}' AND Field = '{esc(field_api_name)}'"
    )
    if existing:
        rec = existing[0]
        if rec["PermissionsRead"] and (rec["PermissionsEdit"] or not can_edit):
            log.info("FieldPermissions %s already %s+ on %s — skipping", field_api_name, access, parent_id)
            return
        client.update("FieldPermissions", rec["Id"], {
            "PermissionsRead": True,
            "PermissionsEdit": can_edit,
        })
        return
    client.create("FieldPermissions", {
        "ParentId": parent_id,
        "SobjectType": field_api_name.split(".")[0],
        "Field": field_api_name,
        "PermissionsRead": True,
        "PermissionsEdit": can_edit,
    })


def apply_field_visibility(client: SalesforceClient, field_api_name: str,
                            profiles: List[str], permission_sets: List[str],
                            access: str = "Edit") -> None:
    for profile_name in profiles or []:
        ps_id = resolve_permission_set_id_for_profile(client, profile_name)
        if not ps_id:
            log.warning("Profile '%s' not found — skipping field visibility", profile_name)
            continue
        upsert_field_permissions(client, ps_id, field_api_name, access)

    for ps_name in permission_sets or []:
        ps_id = resolve_permission_set_id_by_name(client, ps_name)
        if not ps_id:
            log.warning("Permission Set '%s' not found — skipping field visibility", ps_name)
            continue
        upsert_field_permissions(client, ps_id, field_api_name, access)


def create_field(client: SalesforceClient, entry: Dict, object_name: str) -> None:
    api_name = entry["api_name"]
    developer_name = api_name[:-3]
    full_field_api_name = f"{object_name}.{api_name}"

    existing = find_existing_custom_field(client, object_name, developer_name)
    if existing:
        log.info("CustomField exists — skipping create: %s (Id: %s)", full_field_api_name, existing["Id"])
        if entry["field_type_sf"] in ("Picklist", "MultiselectPicklist") and entry.get("picklist_values"):
            merge_picklist_values(client, existing, entry, full_field_api_name)
    else:
        metadata = build_field_metadata(entry)
        payload = {"FullName": full_field_api_name, "Metadata": metadata}
        client.create_tooling("CustomField", payload)

    visibility = entry.get("visibility") or {}
    apply_field_visibility(
        client, full_field_api_name,
        visibility.get("profiles", []), visibility.get("permission_sets", []),
        visibility.get("access", "Edit"),
    )


def verify_field_exists(client: SalesforceClient, object_name: str, developer_name: str) -> bool:
    """Used when create_field: false — the catalog claims the field already
    exists; confirm that before attempting to wire context to it."""
    return find_existing_custom_field(client, object_name, developer_name) is not None


# ---------------------------------------------------------------------------
# Live Context Definition discovery — see module docstring for why this
# never hardcodes a ContextDefinition name.
# ---------------------------------------------------------------------------
class DiscoveryError(Exception):
    pass


class AmbiguousContextDefinition(Exception):
    def __init__(self, candidates: List[Dict]):
        self.candidates = candidates
        super().__init__(
            "Multiple ContextDefinitions are each referenced by a live Pricing "
            "Procedure — ambiguous. Re-run with --context-definition-id <Id>."
        )


def discover_context_chain(client: SalesforceClient,
                            override_id: Optional[str] = None) -> Dict[str, Any]:
    if override_id:
        rows = client.query(
            f"SELECT Id, DeveloperName, MasterLabel FROM ContextDefinition WHERE Id = '{esc(override_id)}'"
        )
        if not rows:
            raise DiscoveryError(f"ContextDefinition Id '{override_id}' not found.")
        ctx_def = rows[0]
    else:
        live_rows = client.query(
            "SELECT ContextDefinitionId FROM ExpressionSetDefinitionContextDefinition"
        )
        candidate_ids = sorted({r["ContextDefinitionId"] for r in live_rows})
        if not candidate_ids:
            raise DiscoveryError(
                "No ContextDefinition is referenced by any "
                "ExpressionSetDefinitionContextDefinition row — no live Pricing "
                "Procedure is configured with a context yet. Configure one in "
                "Setup before running this skill."
            )
        candidates = client.query(
            f"SELECT Id, DeveloperName, MasterLabel, InheritedFrom FROM ContextDefinition "
            f"WHERE Id IN ({ids_literal(candidate_ids)})"
        )
        # ExpressionSetDefinitionContextDefinition links Pricing Procedures to
        # ANY kind of context (product discovery, rating discovery, sales
        # transaction, ...) — confirmed live that a real org can have several
        # unrelated live ContextDefinitions at once. This skill only targets
        # v1's Quote/QuoteLineItem/Order/OrderItem objects, which live
        # exclusively under the SalesTransactionContext family, so narrow to
        # that InheritedFrom prefix before treating >1 as truly ambiguous.
        sales_txn_candidates = [
            c for c in candidates
            if (c.get("InheritedFrom") or "").startswith("SalesTransactionContext")
        ]
        if sales_txn_candidates:
            candidates = sales_txn_candidates
        if not candidates:
            raise DiscoveryError(
                "No live ContextDefinition inherited from SalesTransactionContext "
                "was found among the ContextDefinitions referenced by a live "
                "Pricing Procedure. This skill only supports the Sales "
                "Transaction context family (Quote/QuoteLineItem/Order/OrderItem)."
            )
        if len(candidates) > 1:
            raise AmbiguousContextDefinition(candidates)
        ctx_def = candidates[0]

    ctx_def_id = ctx_def["Id"]

    versions = client.query(
        f"SELECT Id, VersionNumber FROM ContextDefinitionVersion "
        f"WHERE ContextDefinitionId = '{ctx_def_id}' AND IsActive = true"
    )
    if len(versions) != 1:
        raise DiscoveryError(
            f"Expected exactly one active ContextDefinitionVersion for "
            f"ContextDefinition '{ctx_def.get('DeveloperName')}' ({ctx_def_id}), "
            f"found {len(versions)}. Cannot safely proceed."
        )
    version = versions[0]

    nodes = client.query(
        f"SELECT Id, Title FROM ContextNode WHERE ContextDefinitionVersionId = '{version['Id']}'"
    )
    mappings = client.query(
        f"SELECT Id, Title FROM ContextMapping WHERE ContextDefinitionVersionId = '{version['Id']}'"
    )
    mapping_ids = [m["Id"] for m in mappings]
    node_mappings: List[Dict] = []
    if mapping_ids:
        node_mappings = client.query(
            f"SELECT Id, ContextMappingId, ContextNodeId, Object FROM ContextNodeMapping "
            f"WHERE ContextMappingId IN ({ids_literal(mapping_ids)})"
        )

    nodes_by_id = {n["Id"]: n["Title"] for n in nodes}
    mappings_by_id = {m["Id"]: m["Title"] for m in mappings}

    node_mapping_by_object: Dict[str, List[Dict]] = {}
    for nm in node_mappings:
        node_mapping_by_object.setdefault(nm["Object"], []).append(nm)

    return {
        "context_definition_id": ctx_def_id,
        "context_definition_name": ctx_def.get("DeveloperName") or ctx_def.get("MasterLabel"),
        "version_id": version["Id"],
        "version_number": version["VersionNumber"],
        "nodes_by_id": nodes_by_id,
        "mappings_by_id": mappings_by_id,
        "node_mapping_by_object": node_mapping_by_object,
    }


def resolve_target_object(discovery: Dict[str, Any], target_object: str) -> Dict[str, Any]:
    """Resolves a catalog target_object to its live ContextNodeMapping row.
    Raises DiscoveryError if the object isn't part of the active context
    version. When an object is reachable through more than one ContextMapping
    (confirmed live — e.g. QuoteLineItem sits under both QuoteEntitiesMapping
    and QuoteToContractSlsTrxnMapping), prefers the one whose ContextMapping
    Title matches OBJECT_NODE_LOOKUP's expected "<Family>EntitiesMapping"
    name, falling back to the first match with a loud warning listing every
    alternative if no such match exists."""
    matches = discovery["node_mapping_by_object"].get(target_object)
    if not matches:
        raise DiscoveryError(
            f"No ContextNodeMapping found for Object='{target_object}' under any "
            f"live ContextMapping in ContextDefinition "
            f"'{discovery['context_definition_name']}' — this object may not be "
            f"part of the active SalesTransactionContext version; check Setup or "
            f"add it there first."
        )

    expected = OBJECT_NODE_LOOKUP.get(target_object)
    row = None
    if len(matches) > 1:
        if expected:
            row = next(
                (m for m in matches
                 if discovery["mappings_by_id"].get(m["ContextMappingId"]) == expected["mapping"]),
                None,
            )
        if row is None:
            candidate_titles = [
                f"{discovery['mappings_by_id'].get(m['ContextMappingId'], '<unknown>')} ({m['Id']})"
                for m in matches
            ]
            row = matches[0]
            log.warning(
                "Object='%s' has %d ContextNodeMapping matches and none is "
                "titled '%s' — using the first found (%s). Candidates were: %s. "
                "Verify in Setup this is the intended mapping.",
                target_object, len(matches), expected["mapping"] if expected else "<n/a>",
                candidate_titles[0], "; ".join(candidate_titles),
            )
        else:
            log.info(
                "Object='%s' has %d ContextNodeMapping matches — selected "
                "'%s' (matches the standard EntitiesMapping naming convention).",
                target_object, len(matches), expected["mapping"],
            )
    else:
        row = matches[0]

    live_node_title = discovery["nodes_by_id"].get(row["ContextNodeId"], "<unknown>")
    live_mapping_title = discovery["mappings_by_id"].get(row["ContextMappingId"], "<unknown>")

    if expected and expected["node"] != live_node_title:
        log.warning(
            "Object='%s' resolved to ContextNode '%s' live, but the static "
            "lookup expected '%s' — proceeding with the LIVE value (org "
            "customization is possible; live data always wins).",
            target_object, live_node_title, expected["node"],
        )

    return {
        "context_node_mapping_id": row["Id"],
        "context_node_id": row["ContextNodeId"],
        "context_node_title": live_node_title,
        "context_mapping_id": row["ContextMappingId"],
        "context_mapping_title": live_mapping_title,
    }


def print_discovery_report(discovery: Dict[str, Any], entries: List[Dict]) -> None:
    print()
    print(f"ContextDefinition: {discovery['context_definition_name']} "
          f"({discovery['context_definition_id']})")
    print(f"Active Version: {discovery['version_number']} ({discovery['version_id']})")
    for entry in entries:
        api_name = entry.get("api_name") or derive_api_name(entry["label"], suffix="__c", max_length=40)
        targets = entry.get("context", {}).get("target_objects", [])
        print(f"\n=== {api_name} -> {', '.join(targets)} ===")
        for target in targets:
            try:
                resolved = resolve_target_object(discovery, target)
            except DiscoveryError as exc:
                print(f"  {target:<15} -> ERROR: {exc}")
                continue
            print(
                f"  {target:<15} -> ContextNode \"{resolved['context_node_title']}\" "
                f"({resolved['context_node_id']}) -> ContextMapping "
                f"\"{resolved['context_mapping_title']}\" ({resolved['context_mapping_id']}) "
                f"-> ContextNodeMapping {resolved['context_node_mapping_id']}"
            )


# ---------------------------------------------------------------------------
# Context Attribute / Tag / Mapping creation — strictly additive, never
# updates an existing Context* record (see module docstring: append-only).
# ---------------------------------------------------------------------------
def find_context_attribute(client: SalesforceClient, node_id: str, title: str) -> Optional[Dict]:
    recs = client.query(
        f"SELECT Id, DataType, FieldType, IsKey, IsValue FROM ContextAttribute "
        f"WHERE ContextNodeId = '{esc(node_id)}' AND Title = '{esc(title)}'"
    )
    return recs[0] if recs else None


def find_context_tag(client: SalesforceClient, attribute_id: str) -> Optional[Dict]:
    recs = client.query(
        f"SELECT Id, Title FROM ContextTag WHERE ContextAttributeId = '{esc(attribute_id)}'"
    )
    return recs[0] if recs else None


def find_context_attribute_mapping(client: SalesforceClient, node_mapping_id: str,
                                    input_attribute_name: str) -> Optional[Dict]:
    recs = client.query(
        f"SELECT Id, ContextAttributeId FROM ContextAttributeMapping "
        f"WHERE ContextNodeMappingId = '{esc(node_mapping_id)}' "
        f"AND ContextInputAttributeName = '{esc(input_attribute_name)}'"
    )
    return recs[0] if recs else None


def ensure_context_attribute(client: SalesforceClient, node_id: str, ctx: Dict,
                              stats: Dict[str, Any]) -> str:
    # Confirmed live: creating a NEW ContextAttribute on an extended Context
    # Definition fails with INVALID_API_INPUT ("the custom artifact name ...
    # must have an '__c' suffix in an extended context definition") unless
    # its Title carries the standard custom-artifact __c suffix — the same
    # convention as a custom field's API name. Not visible from read-only
    # discovery; only surfaced by attempting a real create. max_length=255
    # (generous, exact limit unconfirmed) so ordinary titles never truncate.
    title = derive_api_name(ctx["attribute_title"], suffix="__c", max_length=255)
    ctx["attribute_title"] = title  # normalize so ensure_context_tag's default matches
    existing = find_context_attribute(client, node_id, title)
    if existing:
        drift = []
        if existing.get("DataType") != ctx["data_type"]:
            drift.append(f"DataType: existing={existing.get('DataType')} catalog={ctx['data_type']}")
        if existing.get("FieldType") != ctx["field_type"]:
            drift.append(f"FieldType: existing={existing.get('FieldType')} catalog={ctx['field_type']}")
        if drift:
            log.warning(
                "DRIFT WARNING — ContextAttribute '%s' (Id: %s) already exists with "
                "different values than the catalog; NEVER updated automatically "
                "(Context* records are append-only-safe, not patch-safe): %s",
                title, existing["Id"], "; ".join(drift),
            )
            stats["drift_warnings"] += 1
        else:
            log.info("ContextAttribute exists — skipping create: %s (Id: %s)", title, existing["Id"])
        stats["skipped"] += 1
        return existing["Id"]

    attr_id = client.create("ContextAttribute", {
        "ContextNodeId": node_id,
        "Title": title,
        "DataType": ctx["data_type"],
        "FieldType": ctx.get("field_type", "input"),
        "IsKey": bool(ctx.get("is_key", False)),
        "IsValue": bool(ctx.get("is_value", False)),
    })
    stats["context_attributes_created"] += 1
    return attr_id


def ensure_context_tag(client: SalesforceClient, attribute_id: str, ctx: Dict,
                        stats: Dict[str, Any]) -> str:
    # Confirmed live: ContextTag.Title carries the same custom-artifact __c
    # suffix requirement as ContextAttribute.Title in an extended context
    # definition (see ensure_context_attribute). ctx["attribute_title"] is
    # already normalized by the time this runs, so the fallback default
    # below is already __c-suffixed; only an explicit tag_title needs it.
    tag_title = derive_api_name(ctx.get("tag_title") or ctx["attribute_title"],
                                 suffix="__c", max_length=255)
    # A dry-run ContextAttribute create returns a synthetic "DRY-..." id (not
    # a real Salesforce Id) — querying with it 400s. Skip the existence check
    # entirely in that case; client.create() below handles the dry-run log.
    existing = None if is_dry_id(attribute_id) else find_context_tag(client, attribute_id)
    if existing:
        if existing.get("Title") != tag_title:
            log.warning(
                "DRIFT WARNING — ContextTag on ContextAttribute %s already exists "
                "with Title='%s' but catalog requests '%s'; NEVER updated "
                "automatically (live Apex/pricing formulas may already reference "
                "the existing tag name).",
                attribute_id, existing.get("Title"), tag_title,
            )
            stats["drift_warnings"] += 1
        else:
            log.info("ContextTag exists — skipping create: %s", tag_title)
        stats["skipped"] += 1
        return existing["Id"]

    tag_id = client.create("ContextTag", {
        "ContextAttributeId": attribute_id,
        "Title": tag_title,
    })
    stats["context_tags_created"] += 1
    return tag_id


def ensure_context_attribute_mapping(client: SalesforceClient, node_mapping_id: str,
                                      attribute_id: str, input_attribute_name: str,
                                      stats: Dict[str, Any]) -> str:
    # node_mapping_id always comes from live discovery (real Id), but
    # attribute_id may be a dry-run synthetic "DRY-..." id — same guard as
    # ensure_context_tag above.
    existing = None if is_dry_id(attribute_id) else find_context_attribute_mapping(
        client, node_mapping_id, input_attribute_name)
    if existing:
        if existing.get("ContextAttributeId") != attribute_id:
            log.warning(
                "DRIFT WARNING — ContextAttributeMapping for '%s' under "
                "ContextNodeMapping %s already points at a different "
                "ContextAttributeId (%s) than expected (%s); NEVER updated "
                "automatically.",
                input_attribute_name, node_mapping_id,
                existing.get("ContextAttributeId"), attribute_id,
            )
            stats["drift_warnings"] += 1
        else:
            log.info("ContextAttributeMapping exists — skipping create: %s", input_attribute_name)
        stats["skipped"] += 1
        return existing["Id"]

    mapping_id = client.create("ContextAttributeMapping", {
        "ContextNodeMappingId": node_mapping_id,
        "ContextAttributeId": attribute_id,
        "ContextInputAttributeName": input_attribute_name,
    })
    stats["context_attribute_mappings_created"] += 1
    return mapping_id


def find_context_attr_hydration_detail(client: SalesforceClient, mapping_id: str) -> Optional[Dict]:
    recs = client.query(
        f"SELECT Id, ObjectName, QueryAttribute FROM ContextAttrHydrationDetail "
        f"WHERE ContextAttributeMappingId = '{esc(mapping_id)}'"
    )
    return recs[0] if recs else None


def ensure_context_attr_hydration_detail(client: SalesforceClient, mapping_id: str,
                                          object_name: str, query_attribute: str,
                                          stats: Dict[str, Any]) -> str:
    """Confirmed live: EVERY working ContextAttributeMapping has exactly one
    child ContextAttrHydrationDetail (ObjectName + QueryAttribute — e.g. the
    standard 'Discount' mapping has ObjectName='QuoteLineItem',
    QueryAttribute='Discount', mirroring ContextInputAttributeName). Without
    this record, the mapping doesn't appear in Setup's Map Data builder even
    though ContextAttributeMapping itself was created successfully — this is
    not visible from ContextAttributeMapping's own field list or any
    documentation found; only discovered by comparing against a live working
    example after the mapping silently didn't show up in Setup."""
    existing = None if is_dry_id(mapping_id) else find_context_attr_hydration_detail(client, mapping_id)
    if existing:
        if existing.get("ObjectName") != object_name or existing.get("QueryAttribute") != query_attribute:
            log.warning(
                "DRIFT WARNING — ContextAttrHydrationDetail on "
                "ContextAttributeMapping %s already exists with "
                "ObjectName='%s'/QueryAttribute='%s' but catalog expects "
                "'%s'/'%s'; NEVER updated automatically.",
                mapping_id, existing.get("ObjectName"), existing.get("QueryAttribute"),
                object_name, query_attribute,
            )
            stats["drift_warnings"] += 1
        else:
            log.info("ContextAttrHydrationDetail exists — skipping create: %s.%s",
                      object_name, query_attribute)
        stats["skipped"] += 1
        return existing["Id"]

    hydration_id = client.create("ContextAttrHydrationDetail", {
        "ContextAttributeMappingId": mapping_id,
        "ObjectName": object_name,
        "QueryAttribute": query_attribute,
    })
    stats["context_attr_hydration_details_created"] += 1
    return hydration_id


def process_entry(client: SalesforceClient, entry: Dict, discovery: Dict[str, Any],
                   stats: Dict[str, Any]) -> None:
    api_name = derive_api_name(entry.get("api_name") or entry["label"], suffix="__c", max_length=40)
    entry["api_name"] = api_name
    developer_name = api_name[:-3]

    ctx = entry.get("context")
    target_objects = (ctx or {}).get("target_objects", [])
    if not ctx or not target_objects:
        raise ValueError(
            f"Entry '{api_name}' has no context.target_objects — this skill only "
            f"creates context-wired fields. Use /create-custom-fields instead for "
            f"a field with no Context Definition wiring."
        )

    print(f"\n=== {api_name} -> {', '.join(target_objects)} ===")

    # The SAME field (same api_name/label/type) is created on EVERY object in
    # target_objects — required so a single ContextAttributeMapping.
    # ContextInputAttributeName (= api_name) resolves to a real field on each
    # target object. Creating it on only one object while wiring a mapping for
    # another would silently produce a mapping that points at a field that
    # doesn't exist there.
    create_field_flag = entry.get("create_field", True)
    for object_name in target_objects:
        full_field_api_name = f"{object_name}.{api_name}"
        if create_field_flag:
            create_field(client, entry, object_name)
        else:
            if not client.dry_run and not verify_field_exists(client, object_name, developer_name):
                raise RuntimeError(
                    f"create_field: false for {full_field_api_name}, but no such "
                    f"field exists in the org. Set create_field: true or verify "
                    f"the api_name matches an existing field on every target object."
                )
            log.info("create_field: false — %s assumed to already exist", full_field_api_name)

    # Group target objects by their resolved ContextNodeId — a catalog entry
    # that mixes a header object (Quote) and a line object (QuoteLineItem)
    # must get two separate ContextAttribute/ContextTag pairs, one per node.
    resolved_by_object: Dict[str, Dict[str, Any]] = {}
    for target in target_objects:
        resolved_by_object[target] = resolve_target_object(discovery, target)

    node_groups: Dict[str, List[str]] = {}
    for target, resolved in resolved_by_object.items():
        node_groups.setdefault(resolved["context_node_id"], []).append(target)

    for node_id, targets_for_node in node_groups.items():
        attribute_id = ensure_context_attribute(client, node_id, ctx, stats)
        ensure_context_tag(client, attribute_id, ctx, stats)
        for target in targets_for_node:
            resolved = resolved_by_object[target]
            mapping_id = ensure_context_attribute_mapping(
                client, resolved["context_node_mapping_id"], attribute_id, api_name, stats,
            )
            ensure_context_attr_hydration_detail(client, mapping_id, target, api_name, stats)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a custom field and wire it into RCA's Sales Transaction Context Definition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--catalog", "-c", required=True)
    parser.add_argument("--org", "-o", default=None)
    parser.add_argument("--api-version", default="61.0")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-only", action="store_true",
                         help="Resolve and print the live context chain only; zero writes.")
    parser.add_argument("--context-definition-id", default=None,
                         help="Override auto-discovery with an explicit ContextDefinition Id.")
    args = parser.parse_args()

    catalog = load_catalog(args.catalog)
    entries = catalog.get("context_fields", [])
    if not entries:
        log.error("No 'context_fields' section found in catalog: %s", args.catalog)
        sys.exit(1)

    access_token, instance_url = get_sf_credentials(args.org)
    dry_run = args.dry_run or args.discover_only
    client = SalesforceClient(access_token, instance_url, args.api_version, dry_run=dry_run)

    log.info("Resolving live Context Definition chain...")
    try:
        discovery = discover_context_chain(client, override_id=args.context_definition_id)
    except AmbiguousContextDefinition as exc:
        log.error(str(exc))
        for c in exc.candidates:
            log.error("  candidate: %s (%s) — Id: %s", c.get("DeveloperName"), c.get("MasterLabel"), c["Id"])
        sys.exit(1)
    except DiscoveryError as exc:
        log.error(str(exc))
        sys.exit(1)

    print_discovery_report(discovery, entries)

    if args.discover_only:
        print("\n[--discover-only] No writes were made.")
        return

    stats: Dict[str, Any] = {
        "context_attributes_created": 0,
        "context_tags_created": 0,
        "context_attribute_mappings_created": 0,
        "context_attr_hydration_details_created": 0,
        "skipped": 0,
        "drift_warnings": 0,
    }

    errors = 0
    for entry in entries:
        try:
            process_entry(client, entry, discovery, stats)
        except (RuntimeError, DiscoveryError, ValueError) as exc:
            log.error("ERROR processing '%s': %s", entry.get("api_name") or entry.get("label"), exc)
            errors += 1

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}Done. {len(entries)} entr(y/ies) processed.")
    print(f"  ContextAttribute created:        {stats['context_attributes_created']}")
    print(f"  ContextTag created:              {stats['context_tags_created']}")
    print(f"  ContextAttributeMapping created: {stats['context_attribute_mappings_created']}")
    print(f"  ContextAttrHydrationDetail created: {stats['context_attr_hydration_details_created']}")
    print(f"  Skipped (already existed):       {stats['skipped']}")
    print(f"  Drift warnings:                  {stats['drift_warnings']}")
    print(f"  Errors:                          {errors}")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
