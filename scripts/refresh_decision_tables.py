#!/usr/bin/env python3
"""
Decision Table Refresher for Salesforce Revenue Cloud Advanced.

Triggers a refresh of one or more RCA decision tables via the
refreshDecisionTable standard action and polls until complete.

Usable standalone or imported by other scripts:

  # Standalone:
  python refresh_decision_tables.py --tables pricebook,attribute --org myorg

  # Imported (pass a SalesforceClient with .base_url, .headers, .query()):
  from refresh_decision_tables import refresh_tables
  refresh_tables(sf, ["pricebook", "attribute"])
"""

from __future__ import annotations

import logging
import time
import requests

log = logging.getLogger(__name__)

# Maps short key → decision table DeveloperName + display label + Salesforce Id.
# Ids are org-specific; the DeveloperName is the canonical identifier used in the API call.
TABLES: dict[str, dict] = {
    "pricebook": {
        "developer_name": "Price_Book_Entry_Decision_Table_v2",
        "label": "Price Book Entries V2",
        "sf_id": "0lDg70000000rnWEAQ",
    },
    "attribute": {
        "developer_name": "Attribute_Based_Adjustment_Decision_Table",
        "label": "Attribute Discount Entries",
        "sf_id": "0lDg70000000rnaEAA",
    },
    "volume": {
        "developer_name": "Price_Adjustment_Tier_Decision_Table",
        "label": "Volume Discount Entries",
        "sf_id": "0lDg70000000rnYEAQ",
    },
    "tier": {
        "developer_name": "Tiered_Adjustment_Tier_Decision_Table",
        "label": "Tiered Adjustment Entries",
        "sf_id": "0lDg70000000rnZEAQ",
    },
    "bundle": {
        "developer_name": "Bundle_Based_Adjustment_Decision_Table",
        "label": "Bundle Based Adjustment Entries",
        "sf_id": "0lDg70000000rnbEAA",
    },
}

POLL_INTERVAL_SEC = 2
POLL_MAX_ATTEMPTS = 15  # 30 seconds max per table


def refresh_tables(sf, table_keys: list[str], dry_run: bool = False) -> None:
    """
    Refresh the given decision tables.

    Args:
        sf: SalesforceClient instance (must expose .base_url, .headers, .query()).
        table_keys: list of keys from TABLES (e.g. ["pricebook", "attribute"]).
        dry_run: if True, log intent but make no API calls.
    """
    if not table_keys:
        return

    log.info("─── Decision Table Refresh ────────────────────────────")

    for key in table_keys:
        if key not in TABLES:
            log.warning("  Unknown table key '%s' — skipping (valid keys: %s)", key, ", ".join(TABLES))
            continue

        table = TABLES[key]
        dev_name = table["developer_name"]
        label = table["label"]
        sf_id = table["sf_id"]

        if dry_run:
            log.info("  [dry-run] Would refresh: %s", label)
            continue

        log.info("  Refreshing: %s …", label)

        payload = {"inputs": [{"DecisionTableApiName": dev_name}]}
        try:
            resp = requests.post(
                f"{sf.base_url}/actions/standard/refreshDecisionTable",
                headers=sf.headers,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            log.error("  ✗ HTTP error refreshing '%s': %s", label, exc)
            continue

        if resp.status_code != 200:
            log.error("  ✗ Refresh failed for '%s': HTTP %d — %s", label, resp.status_code, resp.text[:200])
            continue

        result = resp.json()
        if not result or not result[0].get("isSuccess"):
            errors = result[0].get("errors") if result else None
            log.error("  ✗ Action returned failure for '%s': %s", label, errors)
            continue

        # Poll until Completed or Failed (~2s typical)
        for _ in range(POLL_MAX_ATTEMPTS):
            time.sleep(POLL_INTERVAL_SEC)
            try:
                records = sf.query(
                    f"SELECT RefreshStatus, RefreshFailureReason "
                    f"FROM DecisionTable WHERE Id = '{sf_id}'"
                )
            except Exception as exc:
                log.warning("  ⚠ Polling error for '%s': %s", label, exc)
                break

            if not records:
                log.warning("  ⚠ No DecisionTable record found for id '%s'", sf_id)
                break

            status = records[0].get("RefreshStatus", "")
            if status == "Completed":
                log.info("  ✓ %s — refreshed", label)
                break
            if status == "Failed":
                reason = records[0].get("RefreshFailureReason") or "unknown"
                log.error("  ✗ Refresh failed for '%s': %s", label, reason)
                break
        else:
            log.warning("  ⚠ Refresh timed out for '%s' — check org manually", label)

    log.info("───────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _make_minimal_sf(access_token: str, instance_url: str, api_version: str = "63.0"):
    """Minimal SF client for standalone use — mirrors the interface expected by refresh_tables."""

    class _MinimalSF:
        def __init__(self):
            self.instance_url = instance_url.rstrip("/")
            self.base_url = f"{self.instance_url}/services/data/v{api_version}"
            self.headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }

        def query(self, soql: str) -> list:
            resp = requests.get(
                f"{self.base_url}/query",
                headers=self.headers,
                params={"q": soql},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            records = data.get("records", [])
            while data.get("nextRecordsUrl"):
                resp = requests.get(
                    f"{self.instance_url}{data['nextRecordsUrl']}",
                    headers=self.headers,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                records.extend(data.get("records", []))
            return records

    return _MinimalSF()


def main() -> None:
    import argparse
    import json
    import subprocess

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Refresh RCA decision tables in Salesforce.")
    parser.add_argument(
        "--tables",
        required=True,
        metavar="KEYS",
        help=(
            "Comma-separated table keys to refresh, or 'all'. "
            f"Valid keys: {', '.join(TABLES)}."
        ),
    )
    parser.add_argument("--org", default="myorg", help="Salesforce org alias (default: myorg)")
    parser.add_argument("--dry-run", action="store_true", help="Log intent without making API calls")
    args = parser.parse_args()

    keys: list[str]
    if args.tables.strip().lower() == "all":
        keys = list(TABLES.keys())
    else:
        keys = [k.strip() for k in args.tables.split(",") if k.strip()]

    result = subprocess.run(
        ["sf", "org", "display", "--target-org", args.org, "--json"],
        capture_output=True, text=True, check=True,
    )
    creds = json.loads(result.stdout)["result"]
    sf = _make_minimal_sf(creds["accessToken"], creds["instanceUrl"])

    refresh_tables(sf, keys, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
