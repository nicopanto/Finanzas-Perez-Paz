#!/usr/bin/env python3
"""Sync the shared JSON data from the categorized historical workbook."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Gastos.xlsx"
OUTPUT = ROOT / "finanzas-data.json"

CATEGORY_ALIASES = {
    "ayes": "Ayes",
    "cafe": "Café",
    "compras": "Compras",
    "entretenimiento": "Entretenimiento",
    "suscripciones": "Suscripciones",
}

ACCOUNT_ALIASES = {
    "nomina": "Nómina",
    "cuentas": "Cuentas",
    "cristina": "Cristina",
    "credito": "Crédito",
}

ACCOUNT_PROFILES = {
    "Cristina": {"id": "account_cristina", "label": "Cristina", "titular": "Cristina"},
    "Nómina": {"id": "account_nomina", "label": "Nómina", "titular": "Nicolás"},
    "Cuentas": {"id": "account_cuentas", "label": "Cuentas", "titular": "Nicolás"},
}


def strip_accents(value: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", value)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = strip_accents(str(value)).lower()
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    text = re.sub(r"\b\d{10,}\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_category(value: Any) -> str | None:
    text = str(value).strip() if value is not None and not pd.isna(value) else ""
    if not text:
        return None
    key = normalize_text(text)
    return CATEGORY_ALIASES.get(key, text[:1].upper() + text[1:])


def canonical_account(value: Any) -> str:
    text = str(value).strip() if value is not None and not pd.isna(value) else ""
    key = normalize_text(text)
    return ACCOUNT_ALIASES.get(key, text[:1].upper() + text[1:] if text else "Sin cuenta")


def account_profile(value: Any) -> dict[str, str]:
    account_name = canonical_account(value)
    if account_name in ACCOUNT_PROFILES:
        return ACCOUNT_PROFILES[account_name]
    account_id = "account_" + normalize_text(account_name).replace(" ", "_")
    return {"id": account_id, "label": account_name, "titular": account_name}


def is_credit_account(value: Any) -> bool:
    return normalize_text(value) in {"account credito", "hist credito", "credito"}


def parse_date(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def money_to_cents(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(round(float(value) * 100))
    text = str(value).strip().replace("EUR", "").replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        text = text.replace(".", "").replace(",", ".")
    return int(round(float(text) * 100))


def fnv1a_32(value: str) -> str:
    hash_value = 0x811C9DC5
    for char in value:
        hash_value ^= ord(char)
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return f"{hash_value:08x}"


def transaction_id(parts: list[Any]) -> str:
    return "tx_" + fnv1a_32("|".join(str(part) for part in parts))


def extract_merchant(concept: str) -> str | None:
    text = " ".join(str(concept).split())
    patterns = [
        r"COMPRA EN\s+(.+?)(?:,\s*CON LA TARJETA| EL \d{4}-\d{2}-\d{2}|$)",
        r"RECIBO\s+(.+?)(?:\s+N[º°]|\s+NO\s+RECIBO|\s+REF\.?|$)",
        r"TRANSFERENCIA(?:\s+INMEDIATA)?\s+(?:A FAVOR DE|DE)\s+(.+?)(?:\s+CONCEPTO|,\s*CONCEPTO|$)",
        r"BIZUM\s+(?:A FAVOR DE|DE)\s+(.+?)(?:\s+CONCEPTO|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            merchant = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
            if merchant:
                return merchant[:80]
    words = text.split()
    return " ".join(words[:4])[:80] if words else None


def build_rules(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    display_name: dict[tuple[str, str], str] = {}
    account_name: dict[tuple[str, str], str] = {}
    for tx in transactions:
        category = tx.get("category")
        if not category or tx.get("categoryStatus") == "pending":
            continue
        merchant = extract_merchant(tx["concept"])
        key = normalize_text(merchant)
        if len(key) < 4:
            continue
        group_key = (tx.get("accountId") or "", key)
        grouped[group_key][category] += 1
        display_name.setdefault(group_key, merchant)
        account_name.setdefault(group_key, tx.get("accountLabel") or tx.get("accountName") or "")

    rules = []
    for (account_id, key), counts in grouped.items():
        total = sum(counts.values())
        category, hits = counts.most_common(1)[0]
        confidence = hits / total
        if hits < 2 or confidence < 0.7:
            continue
        rule_key = f"{account_id}|{key}"
        rules.append(
            {
                "id": "rule_" + fnv1a_32(rule_key),
                "kind": "contains",
                "field": "concept",
                "pattern": display_name[(account_id, key)],
                "normalizedPattern": key,
                "accountId": account_id or None,
                "accountName": account_name[(account_id, key)] or None,
                "category": category,
                "confidence": round(min(0.97, confidence), 2),
                "hits": int(hits),
                "source": "learned",
            }
        )
    return sorted(rules, key=lambda item: (-item["confidence"], -item["hits"], item["pattern"]))[:500]


def transactions_from_workbook() -> tuple[list[dict[str, Any]], Counter[str], defaultdict[str, set[str]], Counter[str]]:
    df = pd.read_excel(SOURCE, sheet_name="Data", engine="openpyxl")

    transactions: list[dict[str, Any]] = []
    used_ids: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    category_aliases: defaultdict[str, set[str]] = defaultdict(set)
    account_counts: Counter[str] = Counter()

    for index, row in df.iterrows():
        account_name = canonical_account(row.get("Cuenta"))
        if account_name == "Crédito":
            continue
        profile = account_profile(account_name)
        account_id = profile["id"]
        operation_date = parse_date(row.get("Fecha Operación REAL")) or parse_date(row.get("Fecha Operación"))
        value_date = parse_date(row.get("Fecha valor REAL")) or parse_date(row.get("Fecha Valor"))
        concept = str(row.get("Concepto") or "").strip()
        amount_cents = money_to_cents(row.get("Importe"))
        balance_cents = money_to_cents(row.get("Saldo"))
        category = canonical_category(row.get("Tipo"))

        if not operation_date or not value_date or not concept or amount_cents is None:
            continue

        key_parts = [
            account_id,
            operation_date,
            value_date,
            normalize_text(concept),
            amount_cents,
            balance_cents if balance_cents is not None else "",
        ]
        base_id = transaction_id(key_parts)
        used_ids[base_id] += 1
        tx_id = base_id if used_ids[base_id] == 1 else f"{base_id}_{used_ids[base_id]}"

        if category:
            category_counts[category] += 1
            original_category = str(row.get("Tipo")).strip()
            category_aliases[category].add(original_category)

        account_counts[account_name] += 1
        transactions.append(
            {
                "id": tx_id,
                "accountId": account_id,
                "accountName": profile["label"],
                "accountLabel": profile["label"],
                "accountNumber": None,
                "accountLast4": None,
                "accountDescription": "Histórico",
                "titular": profile["titular"],
                "operationDate": operation_date,
                "valueDate": value_date,
                "concept": concept,
                "amountCents": amount_cents,
                "balanceCents": balance_cents,
                "category": category or "Sin categoría",
                "categoryStatus": "confirmed" if category else "pending",
                "categoryConfidence": 1 if category else 0,
                "movementType": row.get("Tipo de movimiento")
                if isinstance(row.get("Tipo de movimiento"), str)
                else ("Ingreso" if amount_cents > 0 else "Egreso"),
                "source": {
                    "type": "historical-excel",
                    "file": SOURCE.name,
                    "sheet": "Data",
                    "row": int(index) + 2,
                },
            }
        )

    return transactions, category_counts, category_aliases, account_counts


def merge_rules(learned_rules: list[dict[str, Any]], existing_rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {
        (rule.get("accountId") or "") + "|" + (rule.get("normalizedPattern") or normalize_text(rule.get("pattern"))): rule
        for rule in learned_rules
    }
    for rule in existing_rules:
        if rule.get("source") != "manual":
            continue
        pattern_key = rule.get("normalizedPattern") or normalize_text(rule.get("pattern"))
        if not pattern_key:
            continue
        account_id = normalize_account_id(rule.get("accountId"))
        key = f"{account_id or ''}|{pattern_key}"
        by_key[key] = {
            **rule,
            "id": "rule_" + fnv1a_32(key),
            "field": rule.get("field") or "concept",
            "normalizedPattern": pattern_key,
            "accountId": account_id,
            "accountName": account_label(account_id),
            "category": canonical_category(rule.get("category")) or "Sin categoría",
            "source": "manual",
        }
    return sorted(
        by_key.values(),
        key=lambda item: (
            0 if item.get("source") == "manual" else 1,
            -float(item.get("confidence") or 0),
            -int(item.get("hits") or 0),
            item.get("pattern") or "",
        ),
    )


def normalize_account_id(value: Any) -> str | None:
    text = str(value or "")
    key = normalize_text(text)
    if key in {"account cristina", "hist cristina", "bank 00730100590788939851", "cristina"}:
        return "account_cristina"
    if key in {"account nomina", "hist nomina", "bank 00730100510793829162", "nomina"}:
        return "account_nomina"
    if key in {"account cuentas", "hist cuentas", "bank 00730100580789699253", "cuentas"}:
        return "account_cuentas"
    if key in {"account credito", "hist credito", "credito"}:
        return None
    return text or None


def account_label(account_id: str | None) -> str | None:
    for profile in ACCOUNT_PROFILES.values():
        if profile["id"] == account_id:
            return profile["label"]
    return None


def transaction_base_id(tx: dict[str, Any], account_id: str | None = None) -> str:
    normalized_account = account_id or normalize_account_id(tx.get("accountId") or tx.get("accountName") or tx.get("accountLabel")) or ""
    key_parts = [
        normalized_account,
        tx.get("operationDate") or "",
        tx.get("valueDate") or "",
        normalize_text(tx.get("concept")),
        tx.get("amountCents") or 0,
        tx.get("balanceCents") if tx.get("balanceCents") is not None else "",
    ]
    return transaction_id(key_parts)


def normalize_transaction(tx: dict[str, Any], used_ids: Counter[str], seen_base_ids: set[str] | None = None) -> dict[str, Any] | None:
    account_id = normalize_account_id(tx.get("accountId") or tx.get("accountName") or tx.get("accountLabel"))
    if account_id is None:
        if is_credit_account(tx.get("accountId")) or is_credit_account(tx.get("accountName")) or is_credit_account(tx.get("accountLabel")):
            return None
        return tx
    label = account_label(account_id) or tx.get("accountLabel") or tx.get("accountName")
    profile = next((profile for profile in ACCOUNT_PROFILES.values() if profile["id"] == account_id), None)
    titular = profile["titular"] if profile else ("Cristina" if account_id == "account_cristina" else "Nicolás")
    tx = {**tx}
    tx["accountId"] = account_id
    tx["accountName"] = label
    tx["accountLabel"] = label
    tx["titular"] = titular
    base_id = transaction_base_id(tx, account_id)
    if seen_base_ids is not None:
        if base_id in seen_base_ids:
            return None
        seen_base_ids.add(base_id)
    used_ids[base_id] += 1
    tx["id"] = base_id if used_ids[base_id] == 1 else f"{base_id}_{used_ids[base_id]}"
    return tx


def sync_derived_lists(transactions: list[dict[str, Any]], category_aliases: defaultdict[str, set[str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    category_counts: Counter[str] = Counter()
    account_map: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        category = canonical_category(tx.get("category")) or "Sin categoría"
        tx["category"] = category
        category_counts[category] += 1
        category_aliases[category].add(category)
        account_id = tx.get("accountId")
        if account_id:
            account_map.setdefault(
                account_id,
                {
                    "id": account_id,
                    "label": tx.get("accountLabel") or tx.get("accountName") or tx.get("titular") or "Sin cuenta",
                    "titular": tx.get("titular") or tx.get("accountName") or "Sin titular",
                    "accountNumber": tx.get("accountNumber"),
                    "last4": tx.get("accountLast4"),
                    "description": tx.get("accountDescription") or "",
                    "transactionCount": 0,
                },
            )
            account_map[account_id]["transactionCount"] += 1

    category_counts.setdefault("Sin categoría", 0)
    category_aliases["Sin categoría"].add("Sin categoría")
    categories = [
        {
            "name": name,
            "aliases": sorted(category_aliases[name] | {name}),
            "transactionCount": int(count),
        }
        for name, count in sorted(category_counts.items(), key=lambda item: item[0])
    ]
    accounts = sorted(account_map.values(), key=lambda item: str(item.get("label") or item.get("id")))
    return categories, accounts


def main() -> None:
    historical, _category_counts, category_aliases, _account_counts = transactions_from_workbook()
    existing = json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else {}
    used_ids = Counter(tx["id"] for tx in historical)
    seen_base_ids = {transaction_base_id(tx, tx.get("accountId")) for tx in historical}
    kept_transactions = []
    for tx in existing.get("transactions", []):
        if tx.get("source", {}).get("type") == "historical-excel":
            continue
        normalized = normalize_transaction(tx, used_ids, seen_base_ids)
        if normalized is not None:
            kept_transactions.append(normalized)
    transactions = historical + kept_transactions
    categories, accounts = sync_derived_lists(transactions, category_aliases)
    learned_rules = build_rules(transactions)
    rules = merge_rules(learned_rules, existing.get("categoryRules", []))
    output = {
        "schemaVersion": 1,
        "generatedAt": existing.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "sourceFile": SOURCE.name,
            "primaryReportDate": "valueDate",
            "currency": "EUR",
            "historicalRows": len(historical),
            "preservedNonHistoricalRows": len(kept_transactions),
        },
        "categories": categories,
        "accounts": accounts,
        "categoryRules": rules,
        "transactions": transactions,
    }

    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Wrote {OUTPUT.name}: {len(transactions)} transactions "
        f"({len(historical)} from Data, {len(kept_transactions)} preserved), "
        f"{len(categories)} categories, {len(rules)} rules"
    )


if __name__ == "__main__":
    main()
