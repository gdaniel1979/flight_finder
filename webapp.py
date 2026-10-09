"""
Flight Finder – beállító webapp (Streamlit).

A config.yaml összes beállítása szerkeszthető benne; a mentett értékeket a
következő futás (cron vagy kézi `main.py`) már használja.

Futtatás:
    streamlit run webapp.py --server.port 8503 --server.address 127.0.0.1

Nincs belépés: aki eléri a portot, az szerkesztheti a beállításokat. A Brevo API
kulcs ezért soha nem jelenik meg, a logfájlok pedig csak a projekt mappáján belülre
állíthatók (lásd config_store.validate).
"""

import copy
import os

import streamlit as st

import config_store

CONFIG_PATH = os.environ.get(
    "FLIGHT_FINDER_CONFIG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
)

st.set_page_config(page_title="Flight Finder beállítások", page_icon="✈️", layout="centered")

# Kompakt elrendezés: kisebb térközök és alacsonyabb mezők, mint a Streamlit alapértéke
st.markdown(
    """
    <style>
    header[data-testid="stHeader"] { height: 0; min-height: 0; }
    .block-container { padding-top: 1rem; padding-bottom: 1rem; max-width: 860px; }
    [data-testid="stVerticalBlock"] { gap: 0.4rem; }
    [data-testid="stHorizontalBlock"] { gap: 0.6rem; }
    [data-testid="stForm"] { padding: 0.6rem 0.9rem 0.8rem; }
    [data-testid="stWidgetLabel"] { min-height: 0; margin-bottom: 0.1rem; }
    [data-testid="stWidgetLabel"] p { font-size: 0.82rem; }
    [data-testid="stRadio"] > div { margin-top: -0.3rem; }
    [data-testid="stTabs"] [data-baseweb="tab-list"] { gap: 1rem; }
    [data-testid="stTabs"] [data-baseweb="tab"] { height: 2.2rem; padding-top: 0; padding-bottom: 0; }
    [data-testid="stRadio"] [role="radiogroup"] { gap: 0.8rem; }
    [data-testid="stCaptionContainer"] { margin-bottom: 0; }
    h3 { padding: 0 0 0.1rem; font-size: 1.3rem; }
    [data-testid="stMarkdownContainer"] hr { margin: 0.3rem 0 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Beállítások ──

try:
    config = config_store.load_config(CONFIG_PATH)
except FileNotFoundError:
    st.error(f"A konfigurációs fájl nem található: `{CONFIG_PATH}`")
    st.stop()
except Exception as e:
    st.error(f"A konfigurációs fájl nem olvasható: {e}")
    st.stop()

search = config.get("search", {})
airlines = config.get("airlines", {})
ryanair = airlines.get("ryanair", {})
wizzair = airlines.get("wizzair", {})
easyjet = airlines.get("easyjet", {})
email = config.get("email", {})
logging_cfg = config.get("logging", {})
rate_limit = config.get("rate_limit", {})


st.markdown("### ✈️ Flight Finder beállítások")
st.caption("A mentett beállításokat a következő futás már használja.")

saved_message = st.session_state.pop("saved_message", None)
if saved_message:
    st.success(saved_message)

with st.form("settings"):
    tab_search, tab_airlines, tab_email, tab_system = st.tabs(
        ["Keresés", "Légitársaságok", "Email", "Rendszer"]
    )

    with tab_search:
        col1, col2, col3, col4 = st.columns(4)
        origin = col1.text_input("Kiindulás (IATA)", value=search.get("origin", "BUD"), max_chars=3)
        currency = col2.text_input("Pénznem", value=search.get("currency", "EUR"), max_chars=3)
        search_days = col3.number_input(
            "Napok előre", min_value=1, max_value=90, step=1,
            value=int(search.get("search_days", 30)), help="Hány napra előre keressen.",
        )
        max_price = col4.number_input(
            "Max összár", value=float(search["max_price"]) if search.get("max_price") is not None else None,
            min_value=0.0, step=5.0, placeholder="nincs limit",
            help="Oda-vissza összár. Üresen hagyva nincs árlimit. A Wizz Airnek külön limit adható a Légitársaságok fülön.",
        )

        col1, col2, col3, col4 = st.columns(4)
        morning_before = col1.number_input(
            "Odaút ennyi óra előtt", min_value=0, max_value=12, step=1,
            value=int(search.get("morning_before", 9)),
        )
        evening_after = col2.number_input(
            "Visszaút ennyi óra után", min_value=12, max_value=23, step=1,
            value=int(search.get("evening_after", 18)),
        )
        min_nights = col3.number_input(
            "Min. éjszaka", min_value=1, max_value=30, step=1, value=int(search.get("min_nights", 2)),
            help="Csak többnapos módban számít.",
        )
        max_nights = col4.number_input(
            "Max. éjszaka", min_value=1, max_value=30, step=1, value=int(search.get("max_nights", 4)),
            help="Csak többnapos módban számít.",
        )

        mode_labels = {"daytrip": "Egynapos (reggel oda, este vissza)", "multiday": "Többnapos"}
        current_mode = search.get("trip_mode", "daytrip")
        trip_mode = st.radio(
            "Út típusa", options=config_store.TRIP_MODES, format_func=mode_labels.get, horizontal=True,
            index=config_store.TRIP_MODES.index(current_mode) if current_mode in config_store.TRIP_MODES else 0,
        )

        col1, col2 = st.columns(2)
        destinations_text = col1.text_area(
            "Célállomások (IATA kódok)", value=", ".join(search.get("destinations") or []), height=68,
            help="Vesszővel vagy szóközzel elválasztva. Üresen hagyva az összes elérhető célállomásra keres.",
        )
        exclude_text = col2.text_area(
            "Kizárt célállomások (IATA kódok)", value=", ".join(search.get("exclude_destinations") or []),
            height=68, help="Ezeket mindig kihagyja a keresésből.",
        )

    with tab_airlines:
        col1, col2, col3 = st.columns([2, 1, 1], vertical_alignment="bottom")
        ryanair_enabled = col1.checkbox("Ryanair keresés bekapcsolva", value=bool(ryanair.get("enabled", True)))
        ryanair_currency = col2.text_input(
            "Ryanair pénznem", value=ryanair.get("currency", search.get("currency", "EUR")), max_chars=3,
        )

        st.divider()
        wizzair_enabled = st.checkbox(
            "Wizz Air keresés bekapcsolva", value=bool(wizzair.get("enabled", False)),
            help="A Wizz járatokat a Google Flights-ról olvassa, útvonalanként és naponként külön kéréssel. "
                 "Tartsd rövidre a listát: 8 célállomás 30 napra kb. 15 perc.",
        )
        wizzair_destinations_text = st.text_area(
            "Wizz Air célállomások (IATA kódok)", value=", ".join(wizzair.get("destinations") or []), height=68,
        )
        col1, col2, col3 = st.columns(3)
        wizzair_currency = col1.text_input("Wizz Air pénznem", value=wizzair.get("currency", "EUR"), max_chars=3)
        wizzair_max_price = col2.number_input(
            "Wizz Air max összár",
            value=float(wizzair["max_price"]) if wizzair.get("max_price") is not None else None,
            min_value=0.0, step=5.0, placeholder="általános limit",
            help="Üresen hagyva a Keresés fülön megadott limit érvényes.",
        )
        wizzair_delay = col3.number_input(
            "Várakozás kérések között (mp)", min_value=1.0, max_value=60.0, step=0.5,
            value=float(wizzair.get("request_delay", 3)),
        )

        st.divider()
        easyjet_enabled = st.checkbox(
            "easyJet keresés bekapcsolva", value=bool(easyjet.get("enabled", False)), disabled=True,
            help="Még nincs megvalósítva.",
        )

    with tab_email:
        col1, col2 = st.columns(2, vertical_alignment="bottom")
        email_enabled = col1.checkbox("Email értesítés bekapcsolva", value=bool(email.get("enabled", False)))
        has_key = bool(email.get("brevo_api_key"))
        new_api_key = col2.text_input(
            "Brevo API kulcs", value="", type="password",
            placeholder="be van állítva – üresen hagyva marad" if has_key else "nincs beállítva",
            help="A jelenlegi kulcs biztonsági okból nem jelenik meg. Üresen hagyva változatlan marad.",
        )
        col1, col2 = st.columns(2)
        sender_email = col1.text_input("Feladó email címe", value=email.get("sender_email", ""))
        sender_name = col2.text_input("Feladó neve", value=email.get("sender_name", "Flight Finder"))
        recipients_text = st.text_area(
            "Címzettek (soronként egy email cím)",
            value="\n".join(email.get("recipient_emails") or []), height=90,
        )

    with tab_system:
        current_level = str(logging_cfg.get("level", "INFO")).upper()
        col1, col2, col3 = st.columns([1, 2, 2])
        log_level = col1.selectbox(
            "Naplózási szint", options=config_store.LOG_LEVELS,
            index=config_store.LOG_LEVELS.index(current_level) if current_level in config_store.LOG_LEVELS else 1,
        )
        log_file = col2.text_input("Eredmény-log fájl", value=logging_cfg.get("log_file", "logs/flight_finder.log"))
        debug_log_file = col3.text_input(
            "Diagnosztikai log fájl", value=logging_cfg.get("debug_log_file", "logs/flight_finder_debug.log"),
        )
        col1, col2, col3 = st.columns(3)
        max_result_runs = col1.number_input(
            "Megőrzött futások száma",
            value=int(logging_cfg["max_result_runs"]) if logging_cfg.get("max_result_runs") is not None
            else (None if "max_result_runs" in logging_cfg else 365),
            min_value=1, step=1, placeholder="mind",
            help="Ennyi legutóbbi futás marad az eredmény-logban. Üresen hagyva mind megmarad.",
        )
        max_log_size_mb = col2.number_input(
            "Diagnosztikai log mérete (MB)", min_value=1, max_value=1000, step=1,
            value=int(logging_cfg.get("max_log_size_mb", 10)),
        )
        backup_count = col3.number_input(
            "Megőrzött régi logok", min_value=0, max_value=100, step=1,
            value=int(logging_cfg.get("backup_count", 5)),
        )

        st.divider()
        col1, col2, col3 = st.columns(3)
        request_delay = col1.number_input(
            "Ryanair: várakozás kérések között (mp)", min_value=0.0, max_value=60.0, step=0.1,
            value=float(rate_limit.get("request_delay", 0.8)),
        )
        max_retries = col2.number_input(
            "Ryanair: újrapróbálkozások", min_value=0, max_value=10, step=1,
            value=int(rate_limit.get("max_retries", 3)),
        )

    submitted = st.form_submit_button("Mentés", type="primary")

if submitted:
    # A nem szerkesztett (ismeretlen) kulcsok változatlanul megmaradnak
    new_config = copy.deepcopy(config)

    new_config.setdefault("search", {}).update({
        "origin": origin.strip().upper(),
        "morning_before": int(morning_before),
        "evening_after": int(evening_after),
        "search_days": int(search_days),
        "currency": currency.strip().upper(),
        "max_price": max_price,
        "trip_mode": trip_mode,
        "min_nights": int(min_nights),
        "max_nights": int(max_nights),
        "destinations": config_store.parse_codes(destinations_text),
        "exclude_destinations": config_store.parse_codes(exclude_text),
    })

    new_airlines = new_config.setdefault("airlines", {})
    new_airlines.setdefault("ryanair", {}).update({
        "enabled": ryanair_enabled,
        "currency": ryanair_currency.strip().upper(),
    })
    new_airlines.setdefault("wizzair", {}).update({
        "enabled": wizzair_enabled,
        "currency": wizzair_currency.strip().upper(),
        "destinations": config_store.parse_codes(wizzair_destinations_text),
        "request_delay": float(wizzair_delay),
        "max_price": wizzair_max_price,
    })

    new_email = new_config.setdefault("email", {})
    new_email.update({
        "enabled": email_enabled,
        "sender_email": sender_email.strip(),
        "sender_name": sender_name.strip(),
        "recipient_emails": config_store.parse_lines(recipients_text),
    })
    if new_api_key.strip():
        new_email["brevo_api_key"] = new_api_key.strip()

    new_config.setdefault("logging", {}).update({
        "level": log_level,
        "log_file": log_file.strip(),
        "debug_log_file": debug_log_file.strip(),
        "max_result_runs": int(max_result_runs) if max_result_runs is not None else None,
        "max_log_size_mb": int(max_log_size_mb),
        "backup_count": int(backup_count),
    })
    new_config.setdefault("rate_limit", {}).update({
        "request_delay": float(request_delay),
        "max_retries": int(max_retries),
    })

    errors = config_store.validate(new_config)
    changes = config_store.describe_changes(config, new_config)

    if errors:
        st.error("A beállítások nem menthetők:\n\n" + "\n".join(f"- {e}" for e in errors))
    elif not changes:
        st.info("Nem változott semmi.")
    else:
        config_store.save_config(CONFIG_PATH, new_config)
        st.session_state["saved_message"] = "Mentve. Változások:\n\n" + "\n".join(f"- {c}" for c in changes)
        st.rerun()
