"""
Offline tesztek a beállító webappra (config_store + Streamlit AppTest) – hálózati hívás nélkül.

Futtatás: python -m pytest tests/test_webapp_offline.py -v
"""

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import copy

import pytest
import yaml

import config_store

# Régi szerkezetű fájl (rate_limit, légitársaságonkénti currency, wizzair.request_delay):
# a normalize()-nak ezt is az egységes szerkezetre kell hoznia
OLD_FILE = {
    "search": {
        "origin": "BUD", "morning_before": 9, "evening_after": 18, "search_days": 30,
        "currency": "EUR", "max_price": 100, "trip_mode": "daytrip", "min_nights": 2,
        "max_nights": 4, "destinations": [], "exclude_destinations": [],
    },
    "airlines": {
        "ryanair": {"enabled": True, "currency": "EUR"},
        "wizzair": {"enabled": True, "currency": "EUR", "destinations": ["FCO"], "request_delay": 4, "max_price": 150},
        "easyjet": {"enabled": False, "currency": "EUR"},
    },
    "email": {
        "enabled": True, "brevo_api_key": "xkeysib-SECRET", "sender_email": "me@example.com",
        "sender_name": "Flight Finder", "recipient_emails": ["you@example.com"],
    },
    "logging": {"level": "INFO", "log_file": "logs/flight_finder.log", "max_log_size_mb": 10, "backup_count": 5},
    "rate_limit": {"request_delay": 1.5, "max_retries": 2},
    "custom_key": {"keep": "me"},
}
BASE = config_store.normalize(OLD_FILE)


def write_config(tmp_path, config=OLD_FILE):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(path)


def changed(**sections):
    config = copy.deepcopy(BASE)
    for section, values in sections.items():
        for key, value in values.items():
            if isinstance(value, dict) and isinstance(config[section].get(key), dict):
                config[section][key].update(value)
            else:
                config[section][key] = value
    return config


def test_parse_helpers():
    assert config_store.parse_codes("bcn, BGY\nstn;  mxp") == ["BCN", "BGY", "STN", "MXP"]
    assert config_store.parse_codes("") == []
    assert config_store.parse_lines("a@x.hu\n\n b@y.hu , c@z.hu") == ["a@x.hu", "b@y.hu", "c@z.hu"]


def test_normalize_migrates_old_keys():
    airlines = BASE["airlines"]
    # A Ryanair tempója a régi rate_limit blokkból jön, a Google-é a régi Wizz-kulcsból
    assert airlines["ryanair"] == {
        "enabled": True, "max_price": None, "destinations": "all", "request_delay": 1.5, "max_retries": 2,
    }
    assert BASE["google_flights"] == {"request_delay": 4}
    assert "rate_limit" not in BASE
    assert airlines["wizzair"] == {"enabled": True, "destinations": ["FCO"], "max_price": 150}
    # Minden légitársaság ugyanazokkal a kulcsokkal szerepel; az újak kikapcsolva, alap listával
    assert list(airlines) == list(config_store.AIRLINE_LABELS)
    assert airlines["easyjet"]["enabled"] is False and "LGW" in airlines["easyjet"]["destinations"]
    assert airlines["norwegian"] == {"enabled": False, "max_price": None, "destinations": ["CPH", "OSL", "ARN"]}
    assert not any("currency" in airline for airline in airlines.values())
    assert BASE["custom_key"] == {"keep": "me"}
    # Idempotens, és a bemenetet nem módosítja
    assert config_store.normalize(BASE) == BASE
    assert "rate_limit" in OLD_FILE


def test_valid_config_has_no_errors():
    assert config_store.validate(BASE) == []


def test_validation_errors():
    def errors_for(config):
        return " | ".join(config_store.validate(config))

    assert "trip_mode" in errors_for(changed(search={"trip_mode": "multi"}))
    assert "min_nights" in errors_for(changed(search={"min_nights": 5, "max_nights": 2}))
    assert "BARCELONA" in errors_for(changed(search={"destinations": ["BCN", "BARCELONA"]}))
    assert "easyJet: be van kapcsolva, de nincs kiválasztva célállomás" in errors_for(
        changed(airlines={"easyjet": {"enabled": True, "destinations": []}})
    )
    assert "Eurowings célállomások" in errors_for(changed(airlines={"eurowings": {"destinations": ["KÖLN"]}}))
    assert "nincs címzett" in errors_for(changed(email={"recipient_emails": []}))
    assert "érvénytelen cím" in errors_for(changed(email={"recipient_emails": ["not-an-email"]}))
    assert "relatív útvonal" in errors_for(changed(logging={"log_file": "/home/user/.bashrc"}))
    assert "relatív útvonal" in errors_for(changed(logging={"log_file": "logs/../../.bashrc"}))
    assert "Legalább egy légitársaságot" in errors_for(changed(airlines={
        "ryanair": {"enabled": False}, "wizzair": {"enabled": False},
    }))


def test_all_routes_marker():
    # "all": a Google Flights légitársaságnál az összes ismert útvonal, a Ryanairnél nincs szűkítés
    assert config_store.validate(changed(airlines={"wizzair": {"destinations": "all"}})) == []
    wizz_all = config_store.resolve_destinations("wizzair", "all")
    assert len(wizz_all) == 75 and "LTN" in wizz_all
    assert config_store.resolve_destinations("ryanair", "all") is None
    assert config_store.resolve_destinations("ryanair", ["STN"]) == ["STN"]
    assert config_store.google_route_count(changed(airlines={"wizzair": {"destinations": "all"}})) == 75
    assert config_store.destination_label("NAP") == "NAP (Naples)"
    assert config_store.destination_label("XXX") == "XXX"
    assert config_store.destination_label("STN", {"STN": "London"}) == "STN (London)"


def test_google_route_count_merges_shared_routes():
    config = changed(airlines={
        "wizzair": {"enabled": True, "destinations": ["FCO", "LGW"]},
        "easyjet": {"enabled": True, "destinations": ["LGW", "CDG"]},
        "jet2": {"enabled": False, "destinations": ["MAN"]},
    })
    assert config_store.google_route_count(config) == 3


def test_save_is_atomic_keeps_backup_and_mode(tmp_path):
    path = write_config(tmp_path)
    os.chmod(path, 0o600)
    original = open(path, encoding="utf-8").read()

    config_store.save_config(path, changed(search={"max_price": 120}))

    assert config_store.load_config(path)["search"]["max_price"] == 120
    assert config_store.load_config(path)["custom_key"] == {"keep": "me"}
    assert open(path + ".bak", encoding="utf-8").read() == original
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert not os.path.exists(path + ".tmp")


def test_describe_changes_never_shows_api_key():
    changes = config_store.describe_changes(
        BASE, changed(search={"max_price": 120}, email={"brevo_api_key": "xkeysib-NEW"})
    )
    text = " | ".join(changes)
    assert "search.max_price: 100 → 120" in text
    assert "email.brevo_api_key: (módosítva)" in text
    assert "SECRET" not in text and "NEW" not in text


def test_main_builds_scrapers_from_unified_config():
    import main
    from models import Airline

    config = changed(airlines={
        "ryanair": {"max_price": 90, "destinations": ["STN", "BGY"]},
        "easyjet": {"enabled": True, "destinations": ["LGW"], "max_price": 130},
    })
    scrapers = main.build_scrapers(config, "EUR")
    assert scrapers[0].only_destinations == {"STN", "BGY"}
    assert main.build_scrapers(BASE, "EUR")[0].only_destinations is None

    assert [s.airline for s in scrapers] == [Airline.RYANAIR, Airline.WIZZAIR, Airline.EASYJET]
    assert [s.max_price for s in scrapers] == [90, 150, 130]
    # A Google Flights-ról olvasott légitársaságok egy közös kliensen osztoznak
    assert scrapers[1]._client is scrapers[2]._client


# ── A Streamlit app végigkattintva ──

RYANAIR_ROUTES = {"STN": "London", "BGY": "Bergamo", "CIA": "Rome"}


@pytest.fixture(autouse=True)
def fake_ryanair_routes(monkeypatch):
    """A Ryanair útvonallistája hálózat nélkül."""
    import streamlit as st
    from scrapers.ryanair_scraper import RyanairScraper

    st.cache_data.clear()
    monkeypatch.setattr(RyanairScraper, "get_destination_names", lambda self, origin: dict(RYANAIR_ROUTES))


def run_app(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    path = write_config(tmp_path)
    monkeypatch.setenv("FLIGHT_FINDER_CONFIG", path)
    app = AppTest.from_file(os.path.join(ROOT, "webapp.py"), default_timeout=30)
    app.run()
    return app, path


def field(elements, label):
    matches = [e for e in elements if e.label == label]
    assert len(matches) == 1, f"{label}: {len(matches)} találat"
    return matches[0]


def test_app_shows_every_airline_with_the_same_settings(tmp_path, monkeypatch):
    app, _ = run_app(tmp_path, monkeypatch)
    assert not app.exception

    for label in config_store.AIRLINE_LABELS.values():
        field(app.checkbox, label)
        field(app.number_input, f"{label} saját max összár")
        field(app.multiselect, f"{label} célállomások")
    assert field(app.checkbox, "Wizz Air").value is True
    assert field(app.checkbox, "easyJet").value is False
    assert field(app.number_input, "Wizz Air saját max összár").value == 150
    assert field(app.number_input, "Ryanair saját max összár").value is None


def test_app_destination_pickers_list_routes_with_city_names(tmp_path, monkeypatch):
    app, _ = run_app(tmp_path, monkeypatch)

    wizz = field(app.multiselect, "Wizz Air célállomások")
    assert wizz.value == ["FCO"]
    assert wizz.options[0] == "Minden útvonal"
    assert "NAP (Naples)" in wizz.options and "LTN (London)" in wizz.options
    assert len(wizz.options) == 76   # 75 útvonal + "Minden útvonal"

    # A Ryanair listája az élő útvonalakból jön, és alapból minden útvonalra keres
    ryanair = field(app.multiselect, "Ryanair célállomások")
    assert ryanair.value == ["all"]
    assert ryanair.options == ["Minden útvonal", "BGY (Bergamo)", "STN (London)", "CIA (Rome)"]


def test_app_never_renders_api_key(tmp_path, monkeypatch):
    app, _ = run_app(tmp_path, monkeypatch)
    assert not app.exception
    rendered = [str(e.value) for e in list(app.text_input) + list(app.text_area)]
    assert not any("xkeysib" in value for value in rendered)
    assert field(app.text_input, "Brevo API kulcs").value == ""


def test_app_saves_changes_in_the_unified_layout(tmp_path, monkeypatch):
    app, path = run_app(tmp_path, monkeypatch)

    field(app.number_input, "Max összár").set_value(120.0)
    field(app.checkbox, "easyJet").set_value(True)
    field(app.multiselect, "easyJet célállomások").set_value(["LGW", "CDG"])
    field(app.multiselect, "Wizz Air célállomások").set_value(["FCO", "NAP"])
    field(app.multiselect, "Ryanair célállomások").set_value(["STN", "CIA"])
    field(app.number_input, "Ryanair saját max összár").set_value(90.0)
    field(app.text_area, "Címzettek (soronként egy email cím)").set_value("you@example.com\nother@example.com")
    field(app.button, "Mentés").click().run()

    assert not app.exception
    assert "Mentve" in app.success[0].value
    saved = yaml.safe_load(open(path, encoding="utf-8"))
    assert saved["search"]["max_price"] == 120
    assert saved["airlines"]["easyjet"] == {"enabled": True, "max_price": None, "destinations": ["LGW", "CDG"]}
    assert saved["airlines"]["wizzair"] == {"enabled": True, "destinations": ["FCO", "NAP"], "max_price": 150}
    assert saved["airlines"]["ryanair"] == {
        "enabled": True, "max_price": 90, "destinations": ["STN", "CIA"], "request_delay": 1.5, "max_retries": 2,
    }
    assert saved["google_flights"] == {"request_delay": 4}
    assert "rate_limit" not in saved
    assert saved["email"]["recipient_emails"] == ["you@example.com", "other@example.com"]
    assert saved["email"]["brevo_api_key"] == "xkeysib-SECRET"
    assert saved["custom_key"] == {"keep": "me"}
    assert config_store.validate(config_store.normalize(saved)) == []


def test_app_all_routes_choice_is_saved_and_warns_about_volume(tmp_path, monkeypatch):
    app, path = run_app(tmp_path, monkeypatch)

    # "Minden útvonal" + egy külön kód: a "minden" nyer
    field(app.multiselect, "Wizz Air célállomások").set_value(["all", "FCO"])
    field(app.button, "Mentés").click().run()

    assert not app.exception
    saved = yaml.safe_load(open(path, encoding="utf-8"))
    assert saved["airlines"]["wizzair"]["destinations"] == "all"
    assert "75 útvonal" in app.warning[0].value
    assert field(app.multiselect, "Wizz Air célállomások").value == ["all"]


def test_app_rejects_invalid_values_without_saving(tmp_path, monkeypatch):
    app, path = run_app(tmp_path, monkeypatch)
    before = open(path, encoding="utf-8").read()

    field(app.checkbox, "Eurowings").set_value(True)
    field(app.multiselect, "Eurowings célállomások").set_value([])
    field(app.button, "Mentés").click().run()

    assert not app.exception
    assert "Eurowings: be van kapcsolva, de nincs kiválasztva célállomás" in app.error[0].value
    assert open(path, encoding="utf-8").read() == before
