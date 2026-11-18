"""Compatibility analysis for combining NEXUS industry packs in one company chart.

This module is intentionally read-only: it reports conflicting starter-chart
codes and posting-role collisions. It never seeds, renumbers, or changes accounts.
"""
from collections import defaultdict

from chart_templates import (
    ACTIVITY_ACCOUNT_ROLES,
    ACTIVITY_LABELS,
    ACTIVITY_POSTING_ACCOUNTS,
    ACTIVITY_TAX_ACCOUNT_ROLES,
    CHARTS,
)

# Aggregate/reporting roles do not assign an account to a single posting purpose.
_IGNORED_ROLES = {
    "quick_assets",
    "current_assets",
    "current_liabilities",
}

_ROLE_FAMILIES = {
    "cash": "cash",
    "receivables": "receivables",
    "contract_assets": "contract_assets",
    "payables": "payables",
    "grni": "grni",
    "customer_advances": "customer_advances",
    "inventory": "inventory",
    "direct_cost": "direct_cost",
    "retained_earnings": "equity",
    "interbranch_transfers": "interbranch_transfer_asset",
}


def _normalized_activities(activities):
    selected = list(dict.fromkeys(str(value or "").strip() for value in activities))
    if not selected:
        raise ValueError("اختر نشاطًا واحدًا على الأقل.")
    unknown = [value for value in selected if value not in CHARTS]
    if unknown:
        raise ValueError("أنشطة غير مدعومة: " + ", ".join(unknown))
    return selected


def _posting_role_families(activity):
    assignments = defaultdict(set)
    details = defaultdict(list)

    def add(code, family, role):
        if code in (None, ""):
            return
        code = str(code)
        assignments[code].add(family)
        details[code].append({"activity": activity, "role": role, "family": family})

    for role, codes in ACTIVITY_ACCOUNT_ROLES[activity].items():
        if role in _IGNORED_ROLES or role not in _ROLE_FAMILIES:
            continue
        for code in codes:
            add(code, _ROLE_FAMILIES[role], role)

    posting_families = {
        "ar": "receivables",
        "revenue": "revenue",
        "vat": "output_vat",
        "inventory": "inventory",
        "purchase_inventory": "inventory",
        "cogs": "direct_cost",
        "supplier_ap": "payables",
        "input_vat": "input_vat",
        "stocktake_shortage": "stock_adjustment_expense",
        "stocktake_surplus": "stock_adjustment_expense",
        "grni": "grni",
        "interbranch_transfer": "interbranch_transfer_asset",
        "stock_scrap_expense": "direct_cost",
        "internal_stock_use": "internal_stock_use_expense",
    }
    for role, family in posting_families.items():
        add(ACTIVITY_POSTING_ACCOUNTS[activity].get(role), family, role)

    for role, family in (("output_vat", "output_vat"), ("input_vat", "input_vat")):
        add(ACTIVITY_TAX_ACCOUNT_ROLES[activity].get(role), family, role)

    return assignments, details


def analyze_activity_pack_compatibility(activities):
    """Return a read-only report of starter-chart and posting-role collisions.

    A company may still combine these activities after each conflicting role is
    mapped to an appropriate existing or newly created detail account. This
    function deliberately does not invent account codes or perform that mapping.
    """
    selected = _normalized_activities(activities)
    chart_rows = defaultdict(list)
    role_families = defaultdict(set)
    role_details = defaultdict(list)

    for activity in selected:
        for code, name, account_type, parent_code, is_group in CHARTS[activity]:
            chart_rows[str(code)].append({
                "activity": activity,
                "activity_label": ACTIVITY_LABELS[activity],
                "name": name,
                "account_type": account_type,
                "parent_code": str(parent_code) if parent_code is not None else None,
                "is_group": bool(is_group),
            })
        families, details = _posting_role_families(activity)
        for code, values in families.items():
            role_families[code].update(values)
            role_details[code].extend(details[code])

    chart_conflicts = []
    for code, rows in sorted(chart_rows.items()):
        # A pack is internally consistent by design; this report is specifically
        # about collisions introduced when two or more activity packs share a chart.
        # Do not classify repeated definitions inside one starter pack as a
        # cross-activity incompatibility.
        if len({row["activity"] for row in rows}) < 2:
            continue
        signatures = {
            (row["name"], row["account_type"], row["parent_code"], row["is_group"])
            for row in rows
        }
        if len(signatures) > 1:
            chart_conflicts.append({"account_code": code, "definitions": rows})

    role_conflicts = []
    for code, families in sorted(role_families.items()):
        if len(families) > 1:
            role_conflicts.append({
                "account_code": code,
                "role_families": sorted(families),
                "assignments": role_details[code],
            })

    return {
        "activities": selected,
        "activity_labels": [ACTIVITY_LABELS[activity] for activity in selected],
        "compatible": not chart_conflicts and not role_conflicts,
        "chart_conflict_count": len(chart_conflicts),
        "role_conflict_count": len(role_conflicts),
        "chart_conflicts": chart_conflicts,
        "role_conflicts": role_conflicts,
        "requires_account_mapping": bool(chart_conflicts or role_conflicts),
    }
