#!/usr/bin/env python3
"""Tiny local server for the finance app.

It keeps the comfortable HTML UI, but removes the repetitive file-picking flow:
the server reads Movimientos de Cuenta*.xls from the project folder, imports new
transactions into finanzas-data.json, and saves category edits immediately.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "finanzas-data.json"
HOST = "0.0.0.0"
PORT = 8765

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


def canonical_category(value: Any) -> str:
    text = str(value or "").strip()
    key = normalize_text(text)
    aliases = {
        "": "Sin categoría",
        "sin categoria": "Sin categoría",
        "cafe": "Café",
        "compras": "Compras",
        "entretenimiento": "Entretenimiento",
        "suscripciones": "Suscripciones",
        "ayes": "Ayes",
    }
    return aliases.get(key, text[:1].upper() + text[1:] if text else "Sin categoría")


def canonical_account_name(value: str) -> str:
    key = normalize_text(value)
    if key == "nomina":
        return "Nómina"
    if key == "cuentas":
        return "Cuentas"
    if key == "cristina":
        return "Cristina"
    if key == "credito":
        return ""
    return value.strip() or "Sin cuenta"


def normalize_account_id(value: Any) -> str | None:
    text = str(value or "")
    key = normalize_text(text)
    if key in {"account cristina", "hist cristina", "bank 00730100590788939851", "cristina", "cristina 9851"}:
        return "account_cristina"
    if key in {"account nomina", "hist nomina", "bank 00730100510793829162", "nomina", "nomina 9162"}:
        return "account_nomina"
    if key in {"account cuentas", "hist cuentas", "bank 00730100580789699253", "cuentas", "cuentas 9253"}:
        return "account_cuentas"
    if key in {"account credito", "hist credito", "credito"}:
        return None
    return text or None


def account_label(account_id: str | None) -> str | None:
    for profile in ACCOUNT_PROFILES.values():
        if profile["id"] == account_id:
            return profile["label"]
    return None


def account_holder(account_id: str | None, fallback: str = "") -> str:
    for profile in ACCOUNT_PROFILES.values():
        if profile["id"] == account_id:
            return profile["titular"]
    key = normalize_text(fallback)
    if "paz" in key or "cristina" in key:
        return "Cristina"
    if "perez" in key or "nicolas" in key or key in {"nomina", "cuentas"}:
        return "Nicolás"
    return fallback or "Sin titular"


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
    profile = ACCOUNT_PROFILES.get(label, {"id": f"account_{normalize_text(label)}", "label": label, "titular": account_holder(None, titular)})
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
            amount_cents = parse_spanish_cents(cells[3])
            if amount_cents is None:
                continue
            tx = {
                **account,
                "operationDate": parse_bank_date(cells[0]),
                "valueDate": parse_bank_date(cells[1]),
                "concept": cells[2],
                "amountCents": amount_cents,
                "balanceCents": parse_spanish_cents(cells[4]),
                "movementType": "Ingreso" if amount_cents >= 0 else "Egreso",
                "source": {"type": "bank-html", "file": path.name},
            }
            tx["id"] = stable_id(tx)
            transactions.append(tx)
    return transactions


def empty_data() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "metadata": {"primaryReportDate": "valueDate", "currency": "EUR"},
        "categories": [{"name": "Sin categoría", "aliases": ["Sin categoría"], "transactionCount": 0}],
        "accounts": [],
        "categoryRules": [],
        "transactions": [],
    }


def load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        save_data(empty_data())
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save_data(data: dict[str, Any]) -> None:
    sync_derived_lists(data)
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_derived_lists(data: dict[str, Any]) -> None:
    categories: dict[str, dict[str, Any]] = {}
    for category in data.get("categories", []):
        name = canonical_category(category.get("name"))
        categories[name] = {
            "name": name,
            "aliases": sorted(set(category.get("aliases", []) + [name])),
            "transactionCount": 0,
        }

    accounts: dict[str, dict[str, Any]] = {}

    normalized_transactions = []
    for tx in data.get("transactions", []):
        account_id = normalize_account_id(tx.get("accountId") or tx.get("accountLabel") or tx.get("accountName"))
        if account_id is None:
            continue
        label = account_label(account_id) or tx.get("accountLabel") or tx.get("accountName")
        tx["accountId"] = account_id
        tx["accountName"] = label
        tx["accountLabel"] = label
        tx["titular"] = account_holder(account_id, tx.get("titular") or "")
        tx["category"] = canonical_category(tx.get("category"))
        normalized_transactions.append(tx)
        categories.setdefault(tx["category"], {"name": tx["category"], "aliases": [tx["category"]], "transactionCount": 0})
        categories[tx["category"]]["transactionCount"] += 1
        if tx.get("accountId"):
            accounts.setdefault(
                tx["accountId"],
                {
                    "id": tx["accountId"],
                    "label": tx.get("accountLabel") or tx.get("accountName") or tx.get("titular") or "Sin cuenta",
                    "titular": tx.get("titular") or tx.get("accountName") or "Sin titular",
                    "accountNumber": tx.get("accountNumber"),
                    "last4": tx.get("accountLast4"),
                    "description": tx.get("accountDescription") or "",
                    "transactionCount": 0,
                },
            )
            accounts[tx["accountId"]]["transactionCount"] += 1

    categories.setdefault("Sin categoría", {"name": "Sin categoría", "aliases": ["Sin categoría"], "transactionCount": 0})
    data["transactions"] = normalized_transactions
    data["categories"] = sorted(categories.values(), key=lambda item: item["name"])
    data["accounts"] = sorted(accounts.values(), key=lambda item: str(item.get("label") or item.get("id")))


def extract_merchant(concept: str) -> str | None:
    text = re.sub(r"\s+", " ", concept or "").strip()
    patterns = [
        r"COMPRA EN\s+(.+?)(?:,\s*CON LA TARJETA| EL \d{4}-\d{2}-\d{2}|$)",
        r"RECIBO\s+(.+?)(?:\s+N[º°]|\s+NO\s+RECIBO|\s+REF\.?|$)",
        r"TRANSFERENCIA(?:\s+INMEDIATA)?\s+(?:A FAVOR DE|DE)\s+(.+?)(?:\s+CONCEPTO|,\s*CONCEPTO|$)",
        r"BIZUM\s+(?:A FAVOR DE|DE)\s+(.+?)(?:\s+CONCEPTO|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            merchant = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
            if merchant:
                return merchant[:80]
    return " ".join(text.split()[:4])[:80] if text else None


def build_exact_category_map(data: dict[str, Any]) -> dict[str, tuple[str, float]]:
    grouped: dict[str, Counter[str]] = {}
    for tx in data.get("transactions", []):
        if tx.get("categoryStatus") != "confirmed" or tx.get("category") == "Sin categoría":
            continue
        key = normalize_text(tx.get("concept"))
        if not key:
            continue
        grouped.setdefault(key, Counter())[tx["category"]] += 1
    result = {}
    for key, counts in grouped.items():
        total = sum(counts.values())
        category, hits = counts.most_common(1)[0]
        confidence = hits / total
        if confidence >= 0.85:
            result[key] = (category, confidence)
    return result


def infer_category(tx: dict[str, Any], data: dict[str, Any], exact_map: dict[str, tuple[str, float]]) -> dict[str, Any]:
    exact = exact_map.get(normalize_text(tx.get("concept")))
    if exact:
        return {
            "category": exact[0],
            "categoryStatus": "confirmed",
            "categoryConfidence": 0.98,
            "categorySource": "exact",
        }

    normalized_concept = normalize_text(tx.get("concept"))
    best_rule = None
    for rule in data.get("categoryRules", []):
        rule_account = normalize_account_id(rule.get("accountId")) if rule.get("accountId") else None
        if rule_account and rule_account != tx.get("accountId"):
            continue
        pattern = rule.get("normalizedPattern") or normalize_text(rule.get("pattern"))
        if pattern and pattern in normalized_concept:
            account_bonus = 1000 if rule_account else 0
            score = account_bonus + len(pattern) + float(rule.get("confidence", 0))
            if best_rule is None or score > best_rule["score"]:
                best_rule = {**rule, "pattern": pattern, "score": score}
    if best_rule:
        return {
            "category": canonical_category(best_rule["category"]),
            "categoryStatus": "suggested",
            "categoryConfidence": max(0.5, min(0.97, float(best_rule.get("confidence", 0.8)))),
            "categorySource": best_rule.get("source") or "rule",
        }
    return {
        "category": "Sin categoría",
        "categoryStatus": "pending",
        "categoryConfidence": 0,
        "categorySource": "none",
    }


def add_rule_for_transaction(data: dict[str, Any], tx: dict[str, Any], category: str) -> None:
    merchant = extract_merchant(tx.get("concept", ""))
    normalized = normalize_text(merchant)
    if not merchant or len(normalized) < 4:
        return
    account_id = normalize_account_id(tx.get("accountId"))
    for rule in data.get("categoryRules", []):
        same_pattern = (rule.get("normalizedPattern") or normalize_text(rule.get("pattern"))) == normalized
        same_account = (normalize_account_id(rule.get("accountId")) if rule.get("accountId") else None) == account_id
        if same_pattern and same_account:
            rule["category"] = category
            rule["confidence"] = 0.95
            rule["hits"] = int(rule.get("hits", 0)) + 1
            rule["source"] = "manual"
            rule["accountId"] = account_id
            rule["accountName"] = account_label(account_id)
            return
    rule_key = f"{account_id or ''}|{normalized}"
    data.setdefault("categoryRules", []).append(
        {
            "id": "rule_" + fnv1a_32(rule_key),
            "kind": "contains",
            "field": "concept",
            "pattern": merchant,
            "normalizedPattern": normalized,
            "accountId": account_id,
            "accountName": account_label(account_id),
            "category": category,
            "confidence": 0.95,
            "hits": 1,
            "source": "manual",
        }
    )


def upsert_rule(data: dict[str, Any], payload: dict[str, Any]) -> None:
    pattern = str(payload.get("pattern") or "").strip()
    normalized = normalize_text(pattern)
    if len(normalized) < 2:
        raise ValueError("Escribe el texto de la regla.")
    category = canonical_category(payload.get("category"))
    if category == "Sin categoría":
        raise ValueError("Elige una categoría.")
    confidence = max(0.5, min(1, float(payload.get("confidence") or 0.95)))
    account_id = normalize_account_id(payload.get("accountId")) if payload.get("accountId") else None
    rule_id = payload.get("id")
    for rule in data.setdefault("categoryRules", []):
        same_pattern = (rule.get("normalizedPattern") or normalize_text(rule.get("pattern"))) == normalized
        same_account = (normalize_account_id(rule.get("accountId")) if rule.get("accountId") else None) == account_id
        if rule.get("id") == rule_id or (same_pattern and same_account):
            rule["pattern"] = pattern
            rule["normalizedPattern"] = normalized
            rule["field"] = payload.get("field") or "concept"
            rule["accountId"] = account_id
            rule["accountName"] = account_label(account_id)
            rule["category"] = category
            rule["confidence"] = confidence
            rule["source"] = rule.get("source") or "manual"
            return
    rule_key = f"{account_id or ''}|{normalized}"
    data["categoryRules"].append(
        {
            "id": "rule_" + fnv1a_32(rule_key),
            "kind": "contains",
            "field": payload.get("field") or "concept",
            "pattern": pattern,
            "normalizedPattern": normalized,
            "accountId": account_id,
            "accountName": account_label(account_id),
            "category": category,
            "confidence": confidence,
            "hits": 0,
            "source": "manual",
        }
    )


def apply_rules_to_pending(data: dict[str, Any]) -> int:
    exact_map = build_exact_category_map(data)
    changed = 0
    for tx in data.get("transactions", []):
        if tx.get("categoryStatus") == "confirmed":
            continue
        inferred = infer_category(tx, data, exact_map)
        if inferred["category"] != "Sin categoría":
            if tx.get("category") != inferred["category"] or tx.get("categoryStatus") != inferred["categoryStatus"]:
                changed += 1
            tx.update(inferred)
    return changed


def import_bank_folder() -> tuple[dict[str, Any], dict[str, Any]]:
    data = load_data()
    files = sorted(ROOT.glob("Movimientos de Cuenta*.xls"))
    existing_ids = {tx["id"] for tx in data.get("transactions", [])}
    batch_ids: set[str] = set()
    exact_map = build_exact_category_map(data)
    imported: list[dict[str, Any]] = []
    duplicates = 0
    errors = 0
    parsed_files = []

    for path in files:
        try:
            parsed = parse_bank_file(path)
            parsed_files.append({"file": path.name, "count": len(parsed)})
            for tx in parsed:
                if tx["id"] in existing_ids or tx["id"] in batch_ids:
                    duplicates += 1
                    continue
                tx.update(infer_category(tx, data, exact_map))
                imported.append(tx)
                batch_ids.add(tx["id"])
        except Exception:
            errors += 1

    data.setdefault("transactions", []).extend(imported)
    save_data(data)
    summary = {
        "files": parsed_files,
        "newCount": len(imported),
        "duplicateCount": duplicates,
        "pendingCount": sum(1 for tx in imported if tx.get("categoryStatus") != "confirmed"),
        "errorCount": errors,
    }
    return data, summary


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/finanzas.html"
            return super().do_GET()
        if path == "/api/data":
            return self.send_json(load_data())
        if path == "/api/import-bank":
            data, summary = import_bank_folder()
            return self.send_json({"data": data, "summary": summary})
        return super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/import-bank":
            data, summary = import_bank_folder()
            return self.send_json({"data": data, "summary": summary})
        if path == "/api/category":
            payload = self.read_json()
            data = load_data()
            tx_id = payload.get("id")
            category = canonical_category(payload.get("category"))
            save_rule = bool(payload.get("saveRule", True))
            updated = False
            for tx in data.get("transactions", []):
                if tx.get("id") == tx_id:
                    tx["category"] = category
                    tx["categoryStatus"] = "confirmed"
                    tx["categoryConfidence"] = 1
                    tx["categorySource"] = "manual"
                    if save_rule:
                        add_rule_for_transaction(data, tx, category)
                    updated = True
                    break
            if not updated:
                return self.send_json({"error": "Movimiento no encontrado"}, HTTPStatus.NOT_FOUND)
            save_data(data)
            return self.send_json(data)
        if path == "/api/rule":
            try:
                payload = self.read_json()
                data = load_data()
                upsert_rule(data, payload)
                save_data(data)
                return self.send_json(data)
            except ValueError as error:
                return self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        if path == "/api/rule/delete":
            payload = self.read_json()
            data = load_data()
            rule_id = payload.get("id")
            before = len(data.get("categoryRules", []))
            data["categoryRules"] = [rule for rule in data.get("categoryRules", []) if rule.get("id") != rule_id]
            if len(data["categoryRules"]) == before:
                return self.send_json({"error": "Regla no encontrada"}, HTTPStatus.NOT_FOUND)
            save_data(data)
            return self.send_json(data)
        if path == "/api/rules/apply":
            data = load_data()
            changed = apply_rules_to_pending(data)
            save_data(data)
            return self.send_json({"data": data, "changed": changed})
        return self.send_json({"error": "Ruta no encontrada"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    if not DATA_FILE.exists():
        raise SystemExit("No existe finanzas-data.json. Ejecuta scripts/build_initial_data.py primero.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Finanzas familiares: http://127.0.0.1:{PORT}")
    print(f"En otros dispositivos de la misma red: http://<IP-del-Mac>:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
