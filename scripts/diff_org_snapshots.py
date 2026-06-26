#!/usr/bin/env python3
"""diff_org_snapshots.py — Compare two RCA org-snapshot.yaml files.

Usage:
  python diff_org_snapshots.py --source <path> --target <path> [options]

Options:
  --source PATH       Source snapshot (default: .rca/org-snapshot.yaml in CWD)
  --target PATH       Target snapshot (required)
  --include CODES     Comma-separated product/bundle codes to scope the diff
  --format text|json  Output format (default: text)
  --codes-only        Suppress per-field detail; show code lists and counts only
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Copied verbatim from update_rca_catalog.py — kept self-contained
# ---------------------------------------------------------------------------
def _diff_entries(old: Dict, new: Dict) -> List[str]:
    """Return human-readable lines describing field-level changes between two dicts."""
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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
ProductMap = Dict[str, Dict]  # code → entry dict with item_type injected


@dataclass
class PriceDelta:
    code: str
    name: str
    item_type: str
    pricebook: str
    currency: str
    source_price: Optional[float]
    target_price: Optional[float]


@dataclass
class PSMMismatch:
    code: str
    name: str
    item_type: str
    only_in_source: List[str]
    only_in_target: List[str]


@dataclass
class FieldChange:
    code: str
    name: str
    item_type: str
    changes: List[Tuple[str, Any, Any]]  # (field_name, source_val, target_val)


@dataclass
class BundleDiff:
    code: str
    name: str
    changes: List[str]  # human-readable lines


@dataclass
class SellingModelDiff:
    name: str
    only_in_source: bool
    only_in_target: bool
    field_changes: List[Tuple[str, Any, Any]]


@dataclass
class DiffResult:
    source_meta: Dict
    target_meta: Dict
    missing_in_target: List[str] = field(default_factory=list)
    new_in_target: List[str] = field(default_factory=list)
    price_deltas: List[PriceDelta] = field(default_factory=list)
    psm_mismatches: List[PSMMismatch] = field(default_factory=list)
    field_changes: List[FieldChange] = field(default_factory=list)
    bundle_structure_diffs: List[BundleDiff] = field(default_factory=list)
    selling_model_diffs: List[SellingModelDiff] = field(default_factory=list)

    @property
    def has_any_diff(self) -> bool:
        return any([
            self.missing_in_target, self.new_in_target, self.price_deltas,
            self.psm_mismatches, self.field_changes, self.bundle_structure_diffs,
            self.selling_model_diffs,
        ])

    @property
    def total_issues(self) -> int:
        return (len(self.missing_in_target) + len(self.new_in_target) +
                len(self.price_deltas) + len(self.psm_mismatches) +
                len(self.field_changes) + len(self.bundle_structure_diffs) +
                len(self.selling_model_diffs))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_snapshot(path: str) -> Dict:
    """Load and validate a snapshot YAML. Exits on failure."""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        print(f"ERROR: Snapshot file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(expanded) as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"ERROR: Failed to parse YAML at {path}: {e}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(data, dict) or "meta" not in data:
        print(
            f"ERROR: {path} does not look like an org snapshot "
            "(missing 'meta' key). Run /sync-rca-org to generate one.",
            file=sys.stderr,
        )
        sys.exit(1)
    return data


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------
def build_product_map(
    snapshot: Dict, include_codes: Optional[Set[str]] = None
) -> ProductMap:
    """Return code → entry dict for all products and bundles.

    Injects 'item_type' = 'product' | 'bundle' into each entry copy.
    Entries with blank/missing codes are skipped.
    """
    result: ProductMap = {}
    for item_type, section in [("product", "products"), ("bundle", "bundles")]:
        for entry in snapshot.get(section) or []:
            code = (entry.get("code") or "").strip()
            if not code:
                continue
            if include_codes and code not in include_codes:
                continue
            copy = dict(entry)
            copy["item_type"] = item_type
            result[code] = copy
    return result


def build_selling_model_map(snapshot: Dict) -> Dict[str, Dict]:
    """Return name → selling_model dict."""
    result = {}
    for sm in snapshot.get("selling_models") or []:
        name = (sm.get("name") or "").strip()
        if name:
            result[name] = sm
    return result


# ---------------------------------------------------------------------------
# Diff functions
# ---------------------------------------------------------------------------
_PRODUCT_FIELDS = ["name", "family", "active", "uom", "catalog", "category", "managed_by"]
_SKIP_FIELDS = {"sf_id", "pricebook_entries", "psm_options", "groups", "item_type", "code"}


def diff_products(
    source_map: ProductMap,
    target_map: ProductMap,
) -> Tuple[List[str], List[str], List[FieldChange]]:
    """Return (missing_in_target, new_in_target, field_changes)."""
    source_codes = set(source_map)
    target_codes = set(target_map)

    missing = sorted(source_codes - target_codes)
    new = sorted(target_codes - source_codes)

    field_changes: List[FieldChange] = []
    for code in sorted(source_codes & target_codes):
        s = source_map[code]
        t = target_map[code]
        changes = []
        for f in _PRODUCT_FIELDS:
            sv = s.get(f)
            tv = t.get(f)
            if sv != tv:
                changes.append((f, sv, tv))
        if changes:
            field_changes.append(FieldChange(
                code=code,
                name=s.get("name", code),
                item_type=s.get("item_type", "product"),
                changes=changes,
            ))

    return missing, new, field_changes


def diff_prices(
    source_map: ProductMap,
    target_map: ProductMap,
) -> List[PriceDelta]:
    """Compare pricebook_entries for products that exist in both maps."""
    deltas: List[PriceDelta] = []
    for code in sorted(set(source_map) & set(target_map)):
        s = source_map[code]
        t = target_map[code]
        s_entries = {
            (e.get("pricebook", ""), e.get("currency", "USD")): e.get("price")
            for e in (s.get("pricebook_entries") or [])
        }
        t_entries = {
            (e.get("pricebook", ""), e.get("currency", "USD")): e.get("price")
            for e in (t.get("pricebook_entries") or [])
        }
        all_keys = sorted(set(s_entries) | set(t_entries))
        for (pb, currency) in all_keys:
            sp = s_entries.get((pb, currency))
            tp = t_entries.get((pb, currency))
            # TODO: add abs(sp - tp) < 0.001 tolerance if float precision causes false positives
            if sp != tp:
                deltas.append(PriceDelta(
                    code=code,
                    name=s.get("name", code),
                    item_type=s.get("item_type", "product"),
                    pricebook=pb,
                    currency=currency,
                    source_price=sp,
                    target_price=tp,
                ))
    return deltas


def diff_psm_options(
    source_map: ProductMap,
    target_map: ProductMap,
) -> List[PSMMismatch]:
    """Compare psm_options sets for products that exist in both maps."""
    mismatches: List[PSMMismatch] = []
    for code in sorted(set(source_map) & set(target_map)):
        s = source_map[code]
        t = target_map[code]
        s_psms = set(s.get("psm_options") or [])
        t_psms = set(t.get("psm_options") or [])
        if s_psms == t_psms:
            continue
        mismatches.append(PSMMismatch(
            code=code,
            name=s.get("name", code),
            item_type=s.get("item_type", "product"),
            only_in_source=sorted(s_psms - t_psms),
            only_in_target=sorted(t_psms - s_psms),
        ))
    return mismatches


def diff_bundle_structure(
    source_map: ProductMap,
    target_map: ProductMap,
) -> List[BundleDiff]:
    """Compare bundle groups/components for bundles that exist in both maps."""
    diffs: List[BundleDiff] = []
    both = set(source_map) & set(target_map)
    for code in sorted(both):
        s = source_map[code]
        t = target_map[code]
        if s.get("item_type") != "bundle" or not (s.get("groups") or t.get("groups")):
            continue

        lines: List[str] = []
        s_groups = {g.get("name", ""): g for g in (s.get("groups") or [])}
        t_groups = {g.get("name", ""): g for g in (t.get("groups") or [])}

        for gname in sorted(set(s_groups) - set(t_groups)):
            lines.append(f"  - Group '{gname}': (only in source — missing from target)")
        for gname in sorted(set(t_groups) - set(s_groups)):
            lines.append(f"  + Group '{gname}': (only in target — not in source)")

        for gname in sorted(set(s_groups) & set(t_groups)):
            sg = s_groups[gname]
            tg = t_groups[gname]
            # Compare group-level structural fields (strip ids and components)
            sg_clean = {k: v for k, v in sg.items() if k not in ("sf_id", "components", "code")}
            tg_clean = {k: v for k, v in tg.items() if k not in ("sf_id", "components", "code")}
            group_lines = _diff_entries(sg_clean, tg_clean)
            if group_lines:
                lines.append(f"  Group '{gname}':")
                lines.extend(f"    {gl}" for gl in group_lines)

            # Compare components within the group
            s_comps = {c.get("code", ""): c for c in (sg.get("components") or []) if c.get("code")}
            t_comps = {c.get("code", ""): c for c in (tg.get("components") or []) if c.get("code")}
            for ccode in sorted(set(s_comps) - set(t_comps)):
                lines.append(f"  Group '{gname}' / component {ccode}: (only in source)")
            for ccode in sorted(set(t_comps) - set(s_comps)):
                lines.append(f"  Group '{gname}' / component {ccode}: (only in target)")
            for ccode in sorted(set(s_comps) & set(t_comps)):
                sc = {k: v for k, v in s_comps[ccode].items() if k != "sf_id"}
                tc = {k: v for k, v in t_comps[ccode].items() if k != "sf_id"}
                comp_lines = _diff_entries(sc, tc)
                if comp_lines:
                    lines.append(f"  Group '{gname}' / component {ccode}:")
                    lines.extend(f"    {cl}" for cl in comp_lines)

        if lines:
            diffs.append(BundleDiff(code=code, name=s.get("name", code), changes=lines))
    return diffs


def diff_selling_models(
    source_psm_map: Dict[str, Dict],
    target_psm_map: Dict[str, Dict],
) -> List[SellingModelDiff]:
    """Compare selling_models sections between two snapshots."""
    result: List[SellingModelDiff] = []
    _SM_FIELDS = ["type", "pricing_term", "pricing_term_unit", "status"]

    for name in sorted(set(source_psm_map) - set(target_psm_map)):
        result.append(SellingModelDiff(name=name, only_in_source=True, only_in_target=False, field_changes=[]))
    for name in sorted(set(target_psm_map) - set(source_psm_map)):
        result.append(SellingModelDiff(name=name, only_in_source=False, only_in_target=True, field_changes=[]))
    for name in sorted(set(source_psm_map) & set(target_psm_map)):
        s = source_psm_map[name]
        t = target_psm_map[name]
        changes = [(f, s.get(f), t.get(f)) for f in _SM_FIELDS if s.get(f) != t.get(f)]
        if changes:
            result.append(SellingModelDiff(name=name, only_in_source=False, only_in_target=False, field_changes=changes))
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_diff(
    source_path: str,
    target_path: str,
    include_codes: Optional[Set[str]],
) -> DiffResult:
    source = load_snapshot(source_path)
    target = load_snapshot(target_path)

    source_map = build_product_map(source, include_codes)
    target_map = build_product_map(target, include_codes)
    source_psm = build_selling_model_map(source)
    target_psm = build_selling_model_map(target)

    missing, new, field_changes = diff_products(source_map, target_map)
    price_deltas = diff_prices(source_map, target_map)
    psm_mismatches = diff_psm_options(source_map, target_map)
    bundle_diffs = diff_bundle_structure(source_map, target_map)
    sm_diffs = diff_selling_models(source_psm, target_psm)

    return DiffResult(
        source_meta=source.get("meta", {}),
        target_meta=target.get("meta", {}),
        missing_in_target=missing,
        new_in_target=new,
        price_deltas=price_deltas,
        psm_mismatches=psm_mismatches,
        field_changes=field_changes,
        bundle_structure_diffs=bundle_diffs,
        selling_model_diffs=sm_diffs,
    )


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def _fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "(no entry)"
    return f"${p:,.2f}"


def format_text_report(result: DiffResult, codes_only: bool = False) -> str:
    sm = result.source_meta
    tm = result.target_meta

    s_label = f"{sm.get('org','?')}  ·  synced {str(sm.get('last_synced','?'))[:10]}  ·  {sm.get('products_count',0)} products, {sm.get('bundles_count',0)} bundles"
    t_label = f"{tm.get('org','?')}  ·  synced {str(tm.get('last_synced','?'))[:10]}  ·  {tm.get('products_count',0)} products, {tm.get('bundles_count',0)} bundles"

    width = max(len(s_label), len(t_label)) + 14
    bar = "─" * width

    summary_parts = [
        f"{len(result.missing_in_target)} missing",
        f"{len(result.new_in_target)} new",
        f"{len(result.price_deltas)} price delta{'s' if len(result.price_deltas) != 1 else ''}",
        f"{len(result.psm_mismatches)} PSM mismatch{'es' if len(result.psm_mismatches) != 1 else ''}",
        f"{len(result.field_changes)} field change{'s' if len(result.field_changes) != 1 else ''}",
        f"{len(result.bundle_structure_diffs)} bundle diff{'s' if len(result.bundle_structure_diffs) != 1 else ''}",
        f"{len(result.selling_model_diffs)} selling model diff{'s' if len(result.selling_model_diffs) != 1 else ''}",
    ]

    lines = [
        f"┌{'─' * width}┐",
        f"│  Org Snapshot Diff{' ' * (width - 19)}│",
        f"│  Source: {s_label}{' ' * (width - 10 - len(s_label))}│",
        f"│  Target: {t_label}{' ' * (width - 10 - len(t_label))}│",
        f"├{'─' * width}┤",
        f"│  Summary: {', '.join(summary_parts[:3])},{' ' * (width - 11 - len(', '.join(summary_parts[:3])) - 1)}│",
        f"│           {', '.join(summary_parts[3:])}{' ' * (width - 11 - len(', '.join(summary_parts[3:])) )}│",
        f"└{'─' * width}┘",
        "",
    ]

    def section(title: str, count: int, body_lines: List[str]) -> List[str]:
        if count == 0:
            return []
        out = [f"{title}  ({count})", bar]
        out.extend(body_lines)
        out.append("")
        return out

    # MISSING IN TARGET
    missing_lines = []
    for code in result.missing_in_target:
        entry = None  # we don't have the source_map here but we can get name via DiffResult
        missing_lines.append(f"  • {code}")
    lines.extend(section("MISSING IN TARGET", len(result.missing_in_target), missing_lines))

    # NEW IN TARGET
    new_lines = [f"  • {code}" for code in result.new_in_target]
    lines.extend(section("NEW IN TARGET", len(result.new_in_target), new_lines))

    # PRICE DELTAS
    if result.price_deltas:
        lines.append(f"PRICE DELTAS  ({len(result.price_deltas)})")
        lines.append(bar)
        if codes_only:
            codes = sorted({d.code for d in result.price_deltas})
            lines.append(f"  Affected: {', '.join(codes)}")
        else:
            current_code = None
            for d in result.price_deltas:
                if d.code != current_code:
                    lines.append(f"  {d.code}  {d.name}   [{d.item_type}]")
                    current_code = d.code
                lines.append(f"    {d.pricebook} / {d.currency}:  source {_fmt_price(d.source_price)}  →  target {_fmt_price(d.target_price)}")
        lines.append("")

    # PSM MISMATCHES
    if result.psm_mismatches:
        lines.append(f"PSM MISMATCHES  ({len(result.psm_mismatches)})")
        lines.append(bar)
        if codes_only:
            codes = [m.code for m in result.psm_mismatches]
            lines.append(f"  Affected: {', '.join(codes)}")
        else:
            for m in result.psm_mismatches:
                lines.append(f"  {m.code}  {m.name}   [{m.item_type}]")
                if m.only_in_source:
                    lines.append(f"    missing from target:  {', '.join(m.only_in_source)}")
                if m.only_in_target:
                    lines.append(f"    extra in target:      {', '.join(m.only_in_target)}")
        lines.append("")

    # FIELD CHANGES
    if result.field_changes:
        lines.append(f"FIELD CHANGES  ({len(result.field_changes)})")
        lines.append(bar)
        if codes_only:
            codes = [fc.code for fc in result.field_changes]
            lines.append(f"  Affected: {', '.join(codes)}")
        else:
            for fc in result.field_changes:
                lines.append(f"  {fc.code}  {fc.name}   [{fc.item_type}]")
                for fname, sv, tv in fc.changes:
                    lines.append(f"    ~ {fname}:")
                    lines.append(f"        source: {sv!r}")
                    lines.append(f"        target: {tv!r}")
        lines.append("")

    # BUNDLE STRUCTURE DIFFS
    if result.bundle_structure_diffs:
        lines.append(f"BUNDLE STRUCTURE DIFFS  ({len(result.bundle_structure_diffs)})")
        lines.append(bar)
        if codes_only:
            codes = [bd.code for bd in result.bundle_structure_diffs]
            lines.append(f"  Affected: {', '.join(codes)}")
        else:
            for bd in result.bundle_structure_diffs:
                lines.append(f"  {bd.code}  {bd.name}")
                lines.extend(bd.changes)
        lines.append("")

    # SELLING MODEL DIFFERENCES
    if result.selling_model_diffs:
        lines.append(f"SELLING MODEL DIFFERENCES  ({len(result.selling_model_diffs)})")
        lines.append(bar)
        for sd in result.selling_model_diffs:
            if sd.only_in_source:
                lines.append(f"  {sd.name}: (only in source — target org is missing this PSM)")
            elif sd.only_in_target:
                lines.append(f"  {sd.name}: (only in target — not present in source)")
            else:
                lines.append(f"  {sd.name}:")
                for fname, sv, tv in sd.field_changes:
                    lines.append(f"    ~ {fname}:  source {sv!r}  →  target {tv!r}")
        lines.append("")

    if not result.has_any_diff:
        lines.append("No differences found — snapshots are identical.")

    return "\n".join(lines)


def format_json_report(result: DiffResult) -> str:
    def _sm_diff_to_dict(sd: SellingModelDiff) -> Dict:
        return {
            "name": sd.name,
            "only_in_source": sd.only_in_source,
            "only_in_target": sd.only_in_target,
            "field_changes": [{"field": f, "source": s, "target": t} for f, s, t in sd.field_changes],
        }

    def _fc_to_dict(fc: FieldChange) -> Dict:
        return {
            "code": fc.code, "name": fc.name, "item_type": fc.item_type,
            "changes": [{"field": f, "source": s, "target": t} for f, s, t in fc.changes],
        }

    data = {
        "source_meta": result.source_meta,
        "target_meta": result.target_meta,
        "missing_in_target": result.missing_in_target,
        "new_in_target": result.new_in_target,
        "price_deltas": [asdict(d) for d in result.price_deltas],
        "psm_mismatches": [asdict(m) for m in result.psm_mismatches],
        "field_changes": [_fc_to_dict(fc) for fc in result.field_changes],
        "bundle_structure_diffs": [asdict(bd) for bd in result.bundle_structure_diffs],
        "selling_model_diffs": [_sm_diff_to_dict(sd) for sd in result.selling_model_diffs],
    }
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _find_default_source() -> Optional[str]:
    """Walk up from CWD looking for .rca/org-snapshot.yaml."""
    here = os.getcwd()
    for _ in range(5):
        candidate = os.path.join(here, ".rca", "org-snapshot.yaml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two RCA org-snapshot.yaml files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source", help="Source snapshot path (default: .rca/org-snapshot.yaml)")
    parser.add_argument("--target", required=True, help="Target snapshot path")
    parser.add_argument("--include", help="Comma-separated product/bundle codes to diff")
    parser.add_argument("--format", choices=["text", "json"], default="text", dest="fmt")
    parser.add_argument("--codes-only", action="store_true", help="Suppress per-field detail rows")
    args = parser.parse_args()

    source_path = args.source
    if not source_path:
        source_path = _find_default_source()
        if not source_path:
            print(
                "ERROR: No source snapshot found. Pass --source <path> or run /sync-rca-org first.",
                file=sys.stderr,
            )
            sys.exit(1)

    include_codes: Optional[Set[str]] = None
    if args.include:
        include_codes = {c.strip() for c in args.include.split(",") if c.strip()}

    result = run_diff(source_path, args.target, include_codes)

    if args.fmt == "json":
        print(format_json_report(result))
    else:
        print(format_text_report(result, codes_only=args.codes_only))


if __name__ == "__main__":
    main()
