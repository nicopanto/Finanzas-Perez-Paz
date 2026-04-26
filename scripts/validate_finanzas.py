#!/usr/bin/env python3
"""Validate the local finance app data contract against the provided files."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Gastos.xlsx"
DATA_FILE = ROOT / "finanzas-data.json"
BANK_FILES = [
    ROOT / "Movimientos de Cuenta.xls",
    ROOT / "Movimientos de Cuenta (1).xls",
    ROOT / "Movimientos de Cuenta (2).xls",
]


def strip_accents(value: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", value)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_text(value: Any) -> str:
    text = strip_accents(str(value or "")).lower()
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", text)
    text = re.sub(r"\b\d{10,}\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fnv1a_32(value: str) -> str:
    hash_value = 0x811C9DC5
    for char in value:
        hash_value ^= ord(char)
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return f"{hash_value:08x}"


def parse_spanish_cents(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value) * 100)
    text = str(value).replace("EUR", "").replace("\xa0", "").replace(" ", "").strip()
    if not text:
        return None
    has_comma = "," in text
    has_dot = "." in text
    if has_comma and has_dot:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif has_comma:
        text = text.replace(".", "").replace(",", ".")
    return round(float(text) * 100)


def parse_bank_date(value: str) -> str | None:
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", value.strip())
    if not match:
        return None
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"


def canonical_account_name(value: str) -> str:
    key = normalize_text(value)
    if key == "nomina":
        return "Nómina"
    if key == "cuentas":
        return "Cuentas"
    if key == "cristina":
        return "Cristina"
    if key == "credito":
        return "Crédito"
    return value.strip() or "Sin cuenta"


def account_profile(label: str) -> dict[str, str]:
    profiles = {
        "Cristina": {"id": "account_cristina", "label": "Cristina", "titular": "Cristina"},
        "Nómina": {"id": "account_nomina", "label": "Nómina", "titular": "Nicolás"},
        "Cuentas": {"id": "account_cuentas", "label": "Cuentas", "titular": "Nicolás"},
        "Crédito": {"id": "account_credito", "label": "Crédito", "titular": "Nicolás"},
    }
    return profiles.get(label, {"id": f"account_{normalize_text(label)}", "label": label, "titular": label})


def infer_account(meta: dict[str, str | None]) -> dict[str, str | None]:
    digits = re.sub(r"\D", "", meta.get("accountNumber") or "")
    last4 = digits[-4:] or None
    titular = (meta.get("titular") or "").strip()
    description = meta.get("accountDescription") or ""
    label = titular
    if re.search(r"CRISTINA|PAZ RODRIGUEZ", titular, flags=re.I):
        label = "Cristina"
    elif re.search(r"CORRIENTE", description, flags=re.I):
        label = "Cuentas"
    elif re.search(r"N[ÓO]MINA", description, flags=re.I):
        label = "Nómina"
    label = canonical_account_name(label)
    profile = account_profile(label)
    return {
        "accountId": profile["id"],
        "accountName": profile["label"],
        "accountLabel": profile["label"],
        "accountNumber": meta.get("accountNumber"),
        "accountLast4": last4,
        "accountDescription": description,
        "titular": profile["titular"],
    }


def stable_id(tx: dict[str, Any]) -> str:
    parts = [
        tx.get("accountId") or "",
        tx.get("operationDate") or "",
        tx.get("valueDate") or "",
        normalize_text(tx.get("concept") or ""),
        int(tx.get("amountCents") or 0),
        "" if tx.get("balanceCents") is None else int(tx["balanceCents"]),
    ]
    return "tx_" + fnv1a_32("|".join(str(part) for part in parts))


def row_cells(row_html: str) -> list[str]:
    values = []
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", row_html, flags=re.I | re.S):
        text = re.sub(r"<[^>]+>", "", cell)
        text = html.unescape(text).replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            values.append(text)
    return values


def parse_bank_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="latin1")
    meta: dict[str, str | None] = {
        "accountNumber": None,
        "accountDescription": None,
        "titular": None,
    }
    transactions: list[dict[str, Any]] = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.I | re.S):
        cells = row_cells(row)
        if len(cells) >= 2 and cells[0].startswith("Número de Cuenta"):
            meta["accountNumber"] = cells[1]
        elif len(cells) >= 2 and cells[0].startswith("Descripción"):
            meta["accountDescription"] = cells[1]
        elif len(cells) >= 2 and cells[0].startswith("Titular"):
            meta["titular"] = cells[1]
        elif len(cells) == 5 and parse_bank_date(cells[0]) and parse_bank_date(cells[1]):
            account = infer_account(meta)
            tx = {
                **account,
                "operationDate": parse_bank_date(cells[0]),
                "valueDate": parse_bank_date(cells[1]),
                "concept": cells[2],
                "amountCents": parse_spanish_cents(cells[3]),
                "balanceCents": parse_spanish_cents(cells[4]),
                "source": {"type": "bank-html", "file": path.name},
            }
            tx["id"] = stable_id(tx)
            transactions.append(tx)
    return transactions


def assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def expenses_by(transactions: list[dict[str, Any]], key: str) -> dict[str, int]:
    grouped: dict[str, int] = defaultdict(int)
    for tx in transactions:
        if tx["amountCents"] < 0:
            grouped[str(tx.get(key) or "Sin dato")] += abs(tx["amountCents"])
    return dict(grouped)


def validate_rules(data: dict[str, Any]) -> None:
    categories = {category["name"] for category in data["categories"]}
    account_ids = {account["id"] for account in data["accounts"]}
    rules = data.get("categoryRules", [])
    assert_true(len(rules) > 0, "rules rebuilt from categorized data")

    rule_ids = [rule.get("id") for rule in rules]
    assert_equal(len(rule_ids) - len(set(rule_ids)), 0, "duplicate rule ids")
    rule_keys = [
        (
            rule.get("accountId") or "",
            rule.get("normalizedPattern") or normalize_text(rule.get("pattern")),
        )
        for rule in rules
    ]
    assert_equal(len(rule_keys) - len(set(rule_keys)), 0, "duplicate rule account/pattern keys")
    assert_true(
        all(rule.get("category") in categories for rule in rules),
        "rule categories exist",
    )
    assert_true(
        all((not rule.get("accountId")) or rule.get("accountId") in account_ids for rule in rules),
        "rule account ids exist",
    )

    unmatched_learned_rules = []
    for rule in rules:
        if rule.get("source") == "manual":
            continue
        pattern = rule.get("normalizedPattern") or normalize_text(rule.get("pattern"))
        if not pattern:
            unmatched_learned_rules.append(rule.get("id"))
            continue
        matches = [
            tx
            for tx in data["transactions"]
            if tx.get("categoryStatus") == "confirmed"
            and tx.get("category") == rule.get("category")
            and (not rule.get("accountId") or tx.get("accountId") == rule.get("accountId"))
            and pattern in normalize_text(tx.get("concept"))
        ]
        if len(matches) < int(rule.get("hits") or 0):
            unmatched_learned_rules.append(rule.get("id"))
    assert_equal(unmatched_learned_rules, [], "learned rules match categorized transactions")


def main() -> None:
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    source_df = pd.read_excel(SOURCE, sheet_name="Data", engine="openpyxl")
    bank_transactions = [tx for path in BANK_FILES for tx in parse_bank_file(path)]

    assert_equal(parse_spanish_cents("-11,91"), -1191, "negative euro parsing")
    assert_equal(parse_spanish_cents("1.300,31"), 130031, "thousands euro parsing")
    historical = [tx for tx in data["transactions"] if tx.get("source", {}).get("type") == "historical-excel"]
    bank_saved = [tx for tx in data["transactions"] if tx.get("source", {}).get("type") == "bank-html"]
    historical_count = len(historical)
    non_credit_mask = source_df["Cuenta"].map(lambda value: canonical_account_name(str(value)) != "Crédito")
    source_non_credit_rows = {int(index) + 2 for index in source_df.index[non_credit_mask]}
    expected_historical_count = len(source_non_credit_rows)
    assert_equal(data.get("metadata", {}).get("sourceFile"), SOURCE.name, "active source workbook")
    assert_equal(data.get("metadata", {}).get("historicalRows"), expected_historical_count, "metadata historical rows")
    assert_true(len(data["transactions"]) >= expected_historical_count, "finance data transaction count")
    assert_equal(
        historical_count,
        expected_historical_count,
        "Data sheet transaction count",
    )
    source_rows = [tx.get("source", {}).get("row") for tx in historical]
    assert_equal(len(source_rows) - len(set(source_rows)), 0, "duplicate Data source rows")
    assert_equal(set(source_rows), source_non_credit_rows, "all non-credit Data source rows imported")
    assert_equal(
        sum(1 for tx in data["transactions"] if tx.get("accountName") == "Crédito"),
        0,
        "credit card records removed",
    )
    assert_equal({tx["titular"] for tx in data["transactions"]}, {"Cristina", "Nicolás"}, "canonical holders")
    assert_true(
        {tx["accountId"] for tx in data["transactions"]}.issubset(
            {"account_cristina", "account_nomina", "account_cuentas"}
        ),
        "canonical account origins",
    )
    assert_equal(
        len(data["transactions"]) - len({tx["id"] for tx in data["transactions"]}),
        0,
        "duplicate ids in finance data",
    )
    assert_equal(len(bank_transactions), 110, "bank transaction count")
    assert_equal(len(bank_transactions) - len({tx["id"] for tx in bank_transactions}), 0, "strict duplicate ids in batch")

    loose_keys = [
        "|".join(
            [
                tx["accountId"],
                tx["operationDate"],
                tx["valueDate"],
                normalize_text(tx["concept"]),
                str(tx["amountCents"]),
            ]
        )
        for tx in bank_transactions
    ]
    assert_equal(len(loose_keys) - len(set(loose_keys)), 3, "loose duplicate count")

    historical_signatures = {stable_id(tx) for tx in historical}
    saved_bank_signatures = {stable_id(tx) for tx in bank_saved}
    expected_saved_bank_signatures = {tx["id"] for tx in bank_transactions} - historical_signatures
    assert_equal(historical_signatures & saved_bank_signatures, set(), "no historical/bank source overlap")
    assert_equal(saved_bank_signatures, expected_saved_bank_signatures, "only non-overlapping bank rows preserved")

    existing_signatures = {stable_id(tx) for tx in data["transactions"]}
    first_import = [tx for tx in bank_transactions if tx["id"] not in existing_signatures]
    assert_equal(len(first_import), 0, "new rows first import after source refresh")
    existing_after_import = existing_signatures | {tx["id"] for tx in first_import}
    second_import = [tx for tx in bank_transactions if tx["id"] not in existing_after_import]
    assert_equal(len(second_import), 0, "new rows second import")

    exported = json.loads(json.dumps(data, ensure_ascii=False))
    assert_equal(len(exported["transactions"]), len(data["transactions"]), "roundtrip transactions")
    assert_equal(len(exported["categories"]), len(data["categories"]), "roundtrip categories")
    assert_equal(len(exported["categoryRules"]), len(data["categoryRules"]), "roundtrip rules")
    validate_rules(data)

    combined = data["transactions"] + first_import
    total_expenses = sum(abs(tx["amountCents"]) for tx in combined if tx["amountCents"] < 0)
    assert_equal(sum(expenses_by(combined, "category").values()), total_expenses, "category filter totals")
    assert_equal(sum(expenses_by(combined, "accountId").values()), total_expenses, "account filter totals")
    assert_equal(sum(expenses_by(combined, "titular").values()), total_expenses, "titular filter totals")
    month_totals: dict[str, int] = defaultdict(int)
    for tx in combined:
        if tx["amountCents"] < 0:
            month_totals[tx["valueDate"][:7]] += abs(tx["amountCents"])
    assert_equal(sum(month_totals.values()), total_expenses, "month filter totals")
    assert_true(DATA_FILE.stat().st_size > 100_000, "data file has content")

    print("All finance app checks passed")
    print(f"Total transactions: {len(data['transactions'])}")
    print(f"Historical transactions: {historical_count}")
    print(f"Preserved bank transactions: {len(bank_saved)}")
    print(f"Bank import transactions: {len(bank_transactions)}")
    print(f"First import new from current data: {len(first_import)}")
    print("Second import new: 0")


if __name__ == "__main__":
    main()
