"""
A config.yaml betöltése, egységesítése, ellenőrzése és mentése.

Ezt használja a kereső (main.py) és a beállító webapp (webapp.py) is, így a két
oldal ugyanazt a szerkezetet látja:

    search:          minden légitársaságra érvényes keresési feltételek
    airlines:        légitársaságonként: enabled, max_price (saját limit),
                     destinations ("all" = minden útvonal, vagy IATA kódok listája),
                     run_on ("daily" = minden futáskor, vagy a hét egy napja: "mon".."sun"),
                     a ryanairnél ezen felül request_delay, max_retries
    google_flights:  request_delay (közös a Google Flights-ról olvasott légitársaságokra)
    email, logging

A `normalize()` a régi kulcsokat (rate_limit, airlines.*.currency,
airlines.wizzair.request_delay) is erre a szerkezetre hozza.

A mentés atomikus (ideiglenes fájl + csere), és előtte biztonsági másolat készül
`<config>.bak` néven. A PyYAML nem őrzi meg a megjegyzéseket, ezért a webappból
mentett fájlban csak egy fejléc-megjegyzés marad.
"""

import copy
import os
import re
import shutil
from datetime import date
from typing import Any, Dict, List, Optional

import yaml
from pydantic import ValidationError

from models import SearchConfig
from scrapers.google_flights_scraper import (
    CITY_NAMES, GOOGLE_FLIGHTS_AIRLINES, REQUEST_DELAY as GOOGLE_REQUEST_DELAY,
)

IATA_RE = re.compile(r"^[A-Z]{3}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
ALL_ROUTES = "all"   # airlines.<kulcs>.destinations értéke: a légitársaság minden útvonala

# airlines.<kulcs>.run_on: mikor keressen az adott légitársaságra (a kulcsok sorrendje
# a date.weekday() szerinti: "mon" = 0)
DAILY = "daily"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
RUN_ON_LABELS = {
    DAILY: "Naponta", "mon": "Hétfőnként", "tue": "Keddenként", "wed": "Szerdánként",
    "thu": "Csütörtökönként", "fri": "Péntekenként", "sat": "Szombatonként", "sun": "Vasárnaponként",
}
TRIP_MODES = ["daytrip", "multiday"]

# Minden kereshető légitársaság: config kulcs → megjelenített név (a Ryanair az első)
AIRLINE_LABELS = {"ryanair": "Ryanair"}
AIRLINE_LABELS.update({key: spec["airline"].value for key, spec in GOOGLE_FLIGHTS_AIRLINES.items()})

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
    """A config betöltése az egységes szerkezetben (lásd normalize)."""
    with open(path, "r", encoding="utf-8") as f:
        return normalize(yaml.safe_load(f) or {})


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Az egységes szerkezetre hozza a configot; a régi kulcsokat átemeli, a hiányzó
    légitársaságokat kikapcsolva, az alapértelmezett célállomásaikkal veszi fel.
    Idempotens, a bemenetet nem módosítja.
    """
    config = copy.deepcopy(raw)
    airlines = config.setdefault("airlines", {})
    old_rate_limit = config.pop("rate_limit", None) or {}
    google = config.setdefault("google_flights", {})

    ryanair = airlines.setdefault("ryanair", {})
    ryanair.setdefault("enabled", True)
    ryanair.setdefault("max_price", None)
    ryanair.setdefault("destinations", ALL_ROUTES)
    ryanair.setdefault("run_on", DAILY)
    ryanair.setdefault("request_delay", old_rate_limit.get("request_delay", 0.8))
    ryanair.setdefault("max_retries", old_rate_limit.get("max_retries", 3))

    old_wizz_delay = (airlines.get("wizzair") or {}).pop("request_delay", None)
    google.setdefault("request_delay", old_wizz_delay if old_wizz_delay is not None else GOOGLE_REQUEST_DELAY)

    for key, spec in GOOGLE_FLIGHTS_AIRLINES.items():
        airline = airlines.setdefault(key, {})
        airline.setdefault("enabled", False)
        airline.setdefault("max_price", None)
        airline.setdefault("run_on", DAILY)
        if airline.get("destinations") is None:
            airline["destinations"] = list(spec["destinations"])

    # A pénznem egységes (search.currency): légitársaságonként eltérő pénznemnél
    # az árlimitek nem lennének összehasonlíthatók
    for airline in airlines.values():
        if isinstance(airline, dict):
            airline.pop("currency", None)

    # A légitársaságok mindig ugyanabban a sorrendben, az ismeretlen kulcsok a végén
    ordered = {key: airlines[key] for key in AIRLINE_LABELS if key in airlines}
    ordered.update({key: value for key, value in airlines.items() if key not in ordered})
    config["airlines"] = ordered
    return config


def resolve_destinations(airline_key: str, value: Any) -> Optional[List[str]]:
    """
    A configban tárolt destinations értékből a ténylegesen keresendő lista.
    "all": a Google Flights légitársaságoknál az összes ismert útvonal, a Ryanairnél
    None (nincs szűkítés – minden útvonalát az API adja).
    """
    if value == ALL_ROUTES:
        spec = GOOGLE_FLIGHTS_AIRLINES.get(airline_key)
        return list(spec["routes"]) if spec else None
    return list(value or [])


def runs_on(airline: Dict[str, Any], day: date) -> bool:
    """Keres-e a légitársaságra az adott napon futó keresés (a run_on ütemezés szerint)."""
    run_on = airline.get("run_on", DAILY)
    return run_on == DAILY or run_on == WEEKDAYS[day.weekday()]


def destination_label(code: str, names: Optional[Dict[str, str]] = None) -> str:
    """'NAP (Naples)' – városnév a megadott szótárból vagy az ismert repülőterek közül."""
    city = (names or {}).get(code) or CITY_NAMES.get(code)
    return f"{code} ({city})" if city else code


def parse_codes(text: str) -> List[str]:
    """'bcn, BGY stn' → ['BCN', 'BGY', 'STN'] (vessző, szóköz vagy sortörés választ el)."""
    return [part.upper() for part in re.split(r"[\s,;]+", text or "") if part]


def parse_lines(text: str) -> List[str]:
    """Soronként (vagy vesszővel) megadott értékek listája, üresek nélkül."""
    return [part.strip() for part in re.split(r"[\n,;]+", text or "") if part.strip()]


def validate(config: Dict[str, Any]) -> List[str]:
    """A hibák listája magyarul; üres lista = menthető. Az egységes szerkezetet várja."""
    errors: List[str] = []
    search = config.get("search", {})
    airlines = config.get("airlines", {})
    google = config.get("google_flights", {})
    email = config.get("email", {})
    logging_cfg = config.get("logging", {})

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

    if not IATA_RE.match(str(search.get("currency") or "")):
        errors.append("Pénznem: 3 nagybetűs kód legyen (pl. EUR)")

    for key, label in AIRLINE_LABELS.items():
        airline = airlines.get(key) or {}
        if (airline.get("max_price") or 0) < 0:
            errors.append(f"{label}: az árlimit nem lehet negatív")
        if airline.get("run_on", DAILY) not in RUN_ON_LABELS:
            errors.append(f"{label}: ismeretlen keresési gyakoriság: {airline.get('run_on')}")
        destinations = airline.get("destinations")
        if destinations != ALL_ROUTES:
            check_codes(f"{label} célállomások", destinations)
            if airline.get("enabled") and not destinations:
                errors.append(f"{label}: be van kapcsolva, de nincs kiválasztva célállomás")

    if not any((airlines.get(key) or {}).get("enabled") for key in AIRLINE_LABELS):
        errors.append("Legalább egy légitársaságot be kell kapcsolni")

    ryanair = airlines.get("ryanair") or {}
    if (ryanair.get("request_delay") or 0) < 0:
        errors.append("Ryanair: a várakozás nem lehet negatív")
    if (google.get("request_delay") or 0) < 1:
        errors.append("Google Flights: a kérések közti várakozás legalább 1 másodperc legyen")

    recipients = email.get("recipient_emails") or []
    for address in [email.get("sender_email")] + list(recipients):
        if address and not EMAIL_RE.match(str(address)):
            errors.append(f"Email: érvénytelen cím: {address}")
    if email.get("enabled"):
        if not email.get("brevo_api_key"):
            errors.append("Email: be van kapcsolva, de nincs Brevo API kulcs")
        if not email.get("sender_email"):
            errors.append("Email: be van kapcsolva, de nincs feladó cím")
        if not recipients:
            errors.append("Email: be van kapcsolva, de nincs címzett")

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

    return errors


def google_route_count(config: Dict[str, Any], day: Optional[date] = None) -> int:
    """
    Hány különböző útvonalat kérdez le a Google Flights-ról a bekapcsolt légitársaságokhoz.
    `day` megadásával csak az aznap ütemezett légitársaságok számítanak.
    """
    routes = set()
    for key in GOOGLE_FLIGHTS_AIRLINES:
        airline = (config.get("airlines") or {}).get(key) or {}
        if airline.get("enabled") and (day is None or runs_on(airline, day)):
            routes.update(resolve_destinations(key, airline.get("destinations")) or [])
    return len(routes)


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
