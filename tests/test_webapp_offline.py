"""
Offline tesztek a beállító webappra (config_store + Streamlit AppTest) – hálózati hívás nélkül.

Futtatás: python -m pytest tests/test_webapp_offline.py -v
"""

import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest
import yaml

import config_store

BASE = {
    "search": {
        "origin": "BUD", "morning_before": 9, "evening_after": 18, "search_days": 30,
        "currency": "EUR", "max_price": 100, "trip_mode": "daytrip", "min_nights": 2,
        "max_nights": 4, "destinations": [], "exclude_destinations": [],
    },
    "airlines": {
        "ryanair": {"enabled": True, "currency": "EUR"},
        "wizzair": {"enabled": True, "currency": "EUR", "destinations": ["FCO"], "request_delay": 3, "max_price": 150},
        "easyjet": {"enabled": False, "currency": "EUR"},
    },
    "email": {
        "enabled": True, "brevo_api_key": "xkeysib-SECRET", "sender_email": "me@example.com",
        "sender_name": "Flight Finder", "recipient_emails": ["you@example.com"],
    },
    "logging": {"level": "INFO", "log_file": "logs/flight_finder.log", "max_log_size_mb": 10, "backup_count": 5},
    "rate_limit": {"request_delay": 1.5, "max_retries": 3},
    "custom_key": {"keep": "me"},
}


def write_config(tmp_path, config=BASE):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(path)


def changed(**sections):
    import copy
    config = copy.deepcopy(BASE)
    for section, values in sections.items():
        config[section].update(values)
    return config


def test_parse_helpers():
    assert config_store.parse_codes("bcn, BGY\nstn;  mxp") == ["BCN", "BGY", "STN", "MXP"]
    assert config_store.parse_codes("") == []
    assert config_store.parse_lines("a@x.hu\n\n b@y.hu , c@z.hu") == ["a@x.hu", "b@y.hu", "c@z.hu"]


def test_valid_config_has_no_errors():
    assert config_store.validate(BASE) == []


def test_validation_errors():
    def errors_for(config):
        return " | ".join(config_store.validate(config))

    assert "trip_mode" in errors_for(changed(search={"trip_mode": "multi"}))
    assert "min_nights" in errors_for(changed(search={"min_nights": 5, "max_nights": 2}))
    assert "BARCELONA" in errors_for(changed(search={"destinations": ["BCN", "BARCELONA"]}))
    assert "nincs megadva célállomás" in errors_for(changed(airlines={"wizzair": {"enabled": True, "destinations": []}}))
    assert "nincs címzett" in errors_for(changed(email={"recipient_emails": []}))
    assert "érvénytelen cím" in errors_for(changed(email={"recipient_emails": ["not-an-email"]}))
    assert "relatív útvonal" in errors_for(changed(logging={"log_file": "/home/user/.bashrc"}))
    assert "relatív útvonal" in errors_for(changed(logging={"log_file": "logs/../../.bashrc"}))
    assert "Legalább egy légitársaságot" in errors_for(changed(airlines={
        "ryanair": {"enabled": False}, "wizzair": {"enabled": False},
    }))


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


# ── A Streamlit app végigkattintva ──

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


def test_app_never_renders_api_key(tmp_path, monkeypatch):
    app, _ = run_app(tmp_path, monkeypatch)
    assert not app.exception
    rendered = [str(e.value) for e in list(app.text_input) + list(app.text_area)]
    assert not any("xkeysib" in value for value in rendered)
    assert field(app.text_input, "Brevo API kulcs").value == ""


def test_app_saves_changes_and_keeps_api_key(tmp_path, monkeypatch):
    app, path = run_app(tmp_path, monkeypatch)

    field(app.number_input, "Max összár").set_value(120.0)
    field(app.text_area, "Wizz Air célállomások (IATA kódok)").set_value("fco, nap")
    field(app.text_area, "Címzettek (soronként egy email cím)").set_value("you@example.com\nother@example.com")
    field(app.button, "Mentés").click().run()

    assert not app.exception
    assert "Mentve" in app.success[0].value
    saved = config_store.load_config(path)
    assert saved["search"]["max_price"] == 120
    assert saved["airlines"]["wizzair"]["destinations"] == ["FCO", "NAP"]
    assert saved["email"]["recipient_emails"] == ["you@example.com", "other@example.com"]
    assert saved["email"]["brevo_api_key"] == "xkeysib-SECRET"
    assert saved["custom_key"] == {"keep": "me"}
    assert config_store.validate(saved) == []


def test_app_rejects_invalid_values_without_saving(tmp_path, monkeypatch):
    app, path = run_app(tmp_path, monkeypatch)
    before = open(path, encoding="utf-8").read()

    field(app.text_area, "Célállomások (IATA kódok)").set_value("BCN, BARCELONA")
    field(app.button, "Mentés").click().run()

    assert not app.exception
    assert "BARCELONA" in app.error[0].value
    assert open(path, encoding="utf-8").read() == before
