"""
A config.yaml betöltése, ellenőrzése és mentése – a beállító webapp (webapp.py) háttere.

A mentés atomikus (ideiglenes fájl + csere), és előtte biztonsági másolat készül
`<config>.bak` néven. A PyYAML nem őrzi meg a megjegyzéseket, ezért a webappból
mentett fájlban csak egy fejléc-megjegyzés marad.
"""

import copy
import os
import re
import shutil
from typing import Any, Dict, List

import yaml
from pydantic import ValidationError

from models import SearchConfig

IATA_RE = re.compile(r"^[A-Z]{3}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
TRIP_MODES = ["daytrip", "multiday"]

HEADER = (
    "# Flight Finder – Konfiguráció\n"
    "# Ezt a fájlt a beállító webapp (webapp.py) írja; a kulcsok leírása: config.yaml.example\n\n"
)

# A SearchConfig által ellenőrzött kulcsok a search blokkban
SEARCH_MODEL_KEYS = [
    "origin", "morning_before", "evening_after", "search_days", "currency",
    "max_price", "trip_mode", "min_nights", "max_nights",
]


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_codes(text: str) -> List[str]:
    """'bcn, BGY stn' → ['BCN', 'BGY', 'STN'] (vessző, szóköz vagy sortörés választ el)."""
    return [part.upper() for part in re.split(r"[\s,;]+", text or "") if part]


def parse_lines(text: str) -> List[str]:
    """Soronként (vagy vesszővel) megadott értékek listája, üresek nélkül."""
    return [part.strip() for part in re.split(r"[\n,;]+", text or "") if part.strip()]


def validate(config: Dict[str, Any]) -> List[str]:
    """A hibák listája magyarul; üres lista = menthető."""
    errors: List[str] = []
    search = config.get("search", {})
    airlines = config.get("airlines", {})
    email = config.get("email", {})
    logging_cfg = config.get("logging", {})
    rate_limit = config.get("rate_limit", {})

    try:
        SearchConfig(**{k: search[k] for k in SEARCH_MODEL_KEYS if k in search})
    except ValidationError as e:
        for err in e.errors():
            field = ".".join(["search"] + [str(part) for part in err["loc"]])
            errors.append(f"{field}: {err['msg']}")

    def check_codes(label: str, codes: List[str]) -> None:
        bad = [c for c in codes or [] if not IATA_RE.match(str(c))]
        if bad:
            errors.append(f"{label}: érvénytelen IATA kód: {', '.join(map(str, bad))}")

    check_codes("Kiindulási repülőtér", [search.get("origin", "")])
    check_codes("Célállomások", search.get("destinations"))
    check_codes("Kizárt célállomások", search.get("exclude_destinations"))

    def check_currency(label: str, value: Any) -> None:
        if not IATA_RE.match(str(value or "")):
            errors.append(f"{label}: a pénznem 3 nagybetűs kód legyen (pl. EUR)")

    check_currency("Keresés pénzneme", search.get("currency"))
    for name, airline in airlines.items():
        if isinstance(airline, dict) and "currency" in airline:
            check_currency(f"{name} pénzneme", airline.get("currency"))

    wizzair = airlines.get("wizzair", {})
    check_codes("Wizz Air célállomások", wizzair.get("destinations"))
    if wizzair.get("enabled") and not wizzair.get("destinations"):
        errors.append("Wizz Air: engedélyezve van, de nincs megadva célállomás")
    if wizzair.get("enabled") and (wizzair.get("request_delay") or 0) < 1:
        errors.append("Wizz Air: a kérések közti várakozás legalább 1 másodperc legyen")

    if not any(isinstance(a, dict) and a.get("enabled") for a in airlines.values()):
        errors.append("Legalább egy légitársaságot engedélyezni kell")

    recipients = email.get("recipient_emails") or []
    for address in [email.get("sender_email")] + list(recipients):
        if address and not EMAIL_RE.match(str(address)):
            errors.append(f"Email: érvénytelen cím: {address}")
    if email.get("enabled"):
        if not email.get("brevo_api_key"):
            errors.append("Email: engedélyezve van, de nincs Brevo API kulcs")
        if not email.get("sender_email"):
            errors.append("Email: engedélyezve van, de nincs feladó cím")
        if not recipients:
            errors.append("Email: engedélyezve van, de nincs címzett")

    if logging_cfg.get("level") not in LOG_LEVELS:
        errors.append(f"Naplózás: a szint ezek egyike legyen: {', '.join(LOG_LEVELS)}")
    # A webappban nincs belépés, ezért a logok csak a projekt mappáján belülre mutathatnak
    for key, label in (("log_file", "eredmény-log"), ("debug_log_file", "diagnosztikai log")):
        if key not in logging_cfg:
            continue
        log_path = str(logging_cfg.get(key) or "").strip()
        if not log_path:
            errors.append(f"Naplózás: a(z) {label} útvonala nem lehet üres")
        elif os.path.isabs(log_path) or ".." in log_path.replace("\\", "/").split("/"):
            errors.append(f"Naplózás: a(z) {label} relatív útvonal legyen a projekt mappáján belül")

    if (rate_limit.get("request_delay") or 0) < 0:
        errors.append("Rate limit: a várakozás nem lehet negatív")

    return errors


def _flatten(value: Any, prefix: str = "") -> Dict[str, Any]:
    if isinstance(value, dict):
        flat: Dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def describe_changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """Ember által olvasható változáslista; az API kulcs értéke soha nem jelenik meg."""
    old_flat, new_flat = _flatten(old), _flatten(new)
    changes = []
    for key in sorted(set(old_flat) | set(new_flat)):
        before, after = old_flat.get(key), new_flat.get(key)
        if before == after:
            continue
        if "api_key" in key:
            changes.append(f"{key}: (módosítva)")
        else:
            changes.append(f"{key}: {before!r} → {after!r}")
    return changes


def save_config(path: str, config: Dict[str, Any]) -> None:
    """Atomikus mentés; a meglévő fájlról `<path>.bak` másolat készül, a jogosultságok megmaradnak."""
    body = yaml.safe_dump(copy.deepcopy(config), sort_keys=False, allow_unicode=True, default_flow_style=False)
    tmp_path = f"{path}.tmp"

    mode = 0o600
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        shutil.copy2(path, f"{path}.bak")

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(HEADER + body)
    os.replace(tmp_path, path)
