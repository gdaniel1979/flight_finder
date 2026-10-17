"""
Brevo email értesítő modul.

Három stratégiát próbál:
1. brevo-python v4 SDK (legújabb)
2. sib-api-v3-sdk (régi, de elterjedt)
3. Közvetlen HTTP hívás a Brevo API-ra (mindig működik)

Használat:
    notifier = EmailNotifier(
        api_key="xkeysib-...",
        sender_email="me@example.com",
        sender_name="Flight Finder",
        recipient_emails=["you@example.com"],
    )
    notifier.send_day_trips(trips)
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

import requests

from models import DayTrip

logger = logging.getLogger(__name__)


def _check_brevo_v4() -> bool:
    try:
        from brevo import Brevo
        return True
    except Exception:
        return False


def _check_sib_v3() -> bool:
    try:
        import sib_api_v3_sdk
        return True
    except Exception:
        return False


class EmailNotifier:
    """Brevo email értesítő."""

    def __init__(
        self,
        api_key: str,
        sender_email: str,
        recipient_emails: List[str],
        sender_name: str = "Flight Finder",
    ):
        self.api_key = api_key
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.recipient_emails = recipient_emails
        self.logger = logging.getLogger("email")

        self._brevo_v4 = _check_brevo_v4()
        self._sib_v3 = _check_sib_v3()

        if self._brevo_v4:
            self.logger.info("Email: brevo-python v4 SDK")
        elif self._sib_v3:
            self.logger.info("Email: sib-api-v3-sdk")
        else:
            self.logger.info("Email: közvetlen HTTP API")

    def send_day_trips(
        self,
        trips: List[DayTrip],
        warning: Optional[str] = None,
        note: Optional[str] = None,
    ) -> bool:
        """
        Járatpárok küldése emailben.

        Args:
            warning: ha a keresés részben/egészben sikertelen volt, ez a szöveg
                     kiemelve megjelenik a levélben
            note: tájékoztató megjegyzés a levél alján (pl. a ma nem keresett légitársaságok)

        Returns:
            True ha sikeres, False ha nem
        """
        if not trips:
            subject = self._build_empty_subject(warning)
            html = self._build_empty_html(warning)
        else:
            subject = self._build_subject(trips)
            html = self._build_html(trips, warning)

        if note:
            html = html.replace(
                "</div>\n</body>",
                f'<p style="color:#888;font-size:12px;margin-top:24px;">{note}</p>\n</div>\n</body>',
            )
        return self._send_email(subject, html)

    def _send_email(self, subject: str, html_content: str) -> bool:
        """Email küldése a rendelkezésre álló módszerrel."""
        if self._brevo_v4:
            try:
                return self._send_via_brevo_v4(subject, html_content)
            except Exception as e:
                self.logger.warning(f"brevo v4 hiba, fallback: {e}")

        if self._sib_v3:
            try:
                return self._send_via_sib_v3(subject, html_content)
            except Exception as e:
                self.logger.warning(f"sib v3 hiba, fallback: {e}")

        return self._send_via_http(subject, html_content)

    # ── brevo-python v4 ──

    def _send_via_brevo_v4(self, subject: str, html_content: str) -> bool:
        from brevo import Brevo
        from brevo.transactional_emails import (
            SendTransacEmailRequestSender,
            SendTransacEmailRequestToItem,
        )

        client = Brevo(api_key=self.api_key)
        client.transactional_emails.send_transac_email(
            sender=SendTransacEmailRequestSender(
                email=self.sender_email,
                name=self.sender_name,
            ),
            to=[SendTransacEmailRequestToItem(email=e) for e in self.recipient_emails],
            subject=subject,
            html_content=html_content,
        )
        self.logger.info(f"Email elküldve (brevo v4) → {', '.join(self.recipient_emails)}")
        return True

    # ── sib-api-v3-sdk ──

    def _send_via_sib_v3(self, subject: str, html_content: str) -> bool:
        import sib_api_v3_sdk
        from sib_api_v3_sdk.rest import ApiException

        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key["api-key"] = self.api_key
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )
        email = sib_api_v3_sdk.SendSmtpEmail(
            sender={"email": self.sender_email, "name": self.sender_name},
            to=[{"email": e} for e in self.recipient_emails],
            subject=subject,
            html_content=html_content,
        )
        api_instance.send_transac_email(email)
        self.logger.info(f"Email elküldve (sib v3) → {', '.join(self.recipient_emails)}")
        return True

    # ── Közvetlen HTTP ──

    def _send_via_http(self, subject: str, html_content: str) -> bool:
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": self.api_key,
        }
        payload = {
            "sender": {"name": self.sender_name, "email": self.sender_email},
            "to": [{"email": e} for e in self.recipient_emails],
            "subject": subject,
            "htmlContent": html_content,
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            self.logger.info(f"Email elküldve (HTTP) → {', '.join(self.recipient_emails)}")
            return True
        except requests.RequestException as e:
            self.logger.error(f"Email küldés sikertelen: {e}")
            if hasattr(e, "response") and e.response is not None:
                self.logger.error(f"Válasz: {e.response.text[:500]}")
            return False

    # ── Email tartalom ──

    def _build_subject(self, trips: List[DayTrip]) -> str:
        cheapest = min(
            (t for t in trips if t.total_price is not None),
            key=lambda t: t.total_price,
            default=None,
        )
        if cheapest:
            return (
                f"Flight Finder - {len(trips)} járatpár - "
                f"Legjobb: {cheapest.outbound.destination_city or cheapest.outbound.destination} "
                f"{cheapest.total_price:.0f} {cheapest.outbound.currency}"
            )
        return f"Flight Finder - {len(trips)} járatpár találva"

    def _build_empty_subject(self, warning: Optional[str] = None) -> str:
        today = datetime.now().strftime("%Y-%m-%d")
        if warning:
            return f"Flight Finder - hibás futás, nincs találat ({today})"
        return f"Flight Finder - nincs találat ({today})"

    @staticmethod
    def _warning_html(warning: Optional[str]) -> str:
        if not warning:
            return ""
        return (
            '<p style="padding:10px;background:#fff4e5;border-left:4px solid #e67e22;'
            f'color:#8a4b00;font-size:14px;">Figyelem: {warning}</p>'
        )

    def _build_empty_html(self, warning: Optional[str] = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if warning:
            message = "Nem találtunk a feltételeknek megfelelő járatpárt, de a keresés nem futott le hibátlanul."
        else:
            message = "Ma nem volt a feltételeknek megfelelő járatpár. A keresés rendben lefutott."
        return self._page(f"""
            <h2 style="margin:0 0 4px 0;color:#1a73e8;font-size:20px;">Napi járatpárok</h2>
            <p style="margin:0 0 12px 0;color:#666;font-size:13px;">Generálva: {now}</p>
            {self._warning_html(warning)}
            <p style="font-size:15px;">{message}</p>""")

    @staticmethod
    def _page(body: str) -> str:
        """
        A levél váza. Mobilon a viewport és a keskeny, nem törő cellák tartják egyben a
        táblázatot; a format-detection és az x-apple-data-detectors szabály megakadályozza,
        hogy az iOS Mail a dátumokat és járatszámokat aláhúzott linkké alakítsa.
        """
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="format-detection" content="telephone=no, date=no, address=no, email=no">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<style>
  a[x-apple-data-detectors] {{ color: inherit !important; text-decoration: none !important;
    font-size: inherit !important; font-family: inherit !important; font-weight: inherit !important; }}
</style>
</head>
<body style="margin:0;padding:12px 8px;font-family:Arial,Helvetica,sans-serif;color:#333;">
<div style="max-width:640px;margin:0 auto;">
{body}
</div>
</body>
</html>
"""

    # Rövid magyar napnevek a dátum mellé (date.weekday() sorrendben)
    WEEKDAYS_HU = ["H", "K", "Sze", "Cs", "P", "Szo", "V"]

    @classmethod
    def _date_label(cls, trip: DayTrip) -> str:
        """'11.06. P' – egynapos útnál egy sor; többnaposnál két dátum és az éjszakák száma."""
        def short(day) -> str:
            return f"{day.month:02d}.{day.day:02d}.&nbsp;{cls.WEEKDAYS_HU[day.weekday()]}"

        if trip.nights > 0:
            return (
                f"{short(trip.trip_date)}<br>{short(trip.return_date)}"
                f"<br><span style='color:#888;font-size:11px;font-weight:normal;'>{trip.nights} éj</span>"
            )
        return short(trip.trip_date)

    @staticmethod
    def _airline_label(trip: DayTrip) -> str:
        """A járatpár légitársasága; ha az oda- és visszaút eltér, mindkettő (két sorban)."""
        out_name, in_name = trip.outbound.airline.value, trip.inbound.airline.value
        return out_name if out_name == in_name else f"{out_name}<br>{in_name}"

    @staticmethod
    def _leg_label(flight) -> str:
        """Indulási idő, alatta kisebben a járatszám, ha ismert (a Google Flights nem adja meg)."""
        departure = flight.departure_time.strftime("%H:%M")
        if not flight.flight_number:
            return departure
        return (
            f"{departure}<br><span style='color:#888;font-size:11px;font-weight:normal;'>"
            f"{flight.flight_number}</span>"
        )

    @staticmethod
    def _price_label(trip: DayTrip) -> str:
        if trip.total_price is None:
            return "N/A"
        currency = trip.outbound.currency
        symbol = {"EUR": "€", "GBP": "£", "USD": "$"}.get(currency)
        amount = f"{trip.total_price:.2f}"
        return f"{amount}&nbsp;{symbol}" if symbol else f"{amount}&nbsp;{currency}"

    def _build_html(self, trips: List[DayTrip], warning: Optional[str] = None) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Csoportosítás célállomás szerint. Kulcs: (város, reptér_kód)
        # A város az elsődleges rendezési szempont, a reptér_kód csak disambiguáló.
        groups: dict = defaultdict(list)
        for trip in trips:
            o = trip.outbound
            city = o.destination_city or "?"
            airport = o.destination
            groups[(city, airport)].append(trip)

        # Célvárosok ábécé sorrendben (case-insensitive)
        sorted_keys = sorted(groups.keys(), key=lambda k: (k[0].lower(), k[1]))

        # Keskeny, nem törő cellák: így a táblázat egy telefon szélességébe is belefér
        cell = "padding:6px 4px;border-bottom:1px solid #eee;white-space:nowrap;vertical-align:top;"
        head = "padding:6px 4px;text-align:left;font-size:12px;color:#555;white-space:nowrap;"

        sections = ""
        for city, airport in sorted_keys:
            group_trips = groups[(city, airport)]

            # Dátum, azon belül ár szerint növekvő (ismeretlen ár a végén)
            group_trips_sorted = sorted(
                group_trips,
                key=lambda t: (
                    t.trip_date, t.return_date,
                    t.total_price if t.total_price is not None else float("inf"),
                ),
            )

            # Legolcsóbb ár megkeresése (None-ok kizárva)
            priced = [t for t in group_trips_sorted if t.total_price is not None]
            min_price = min((t.total_price for t in priced), default=None)

            rows = ""
            for trip in group_trips_sorted:
                # Holtverseny esetén minden legolcsóbb sor zöld; N/A sor soha nem zöld
                is_cheapest = (
                    min_price is not None
                    and trip.total_price is not None
                    and trip.total_price == min_price
                )
                row_style = (
                    "color:#1b8a3a;font-weight:bold;" if is_cheapest else "color:#333;"
                )

                rows += f"""
                <tr style="{row_style}">
                    <td style="{cell}">{self._date_label(trip)}</td>
                    <td style="{cell}">{self._airline_label(trip)}</td>
                    <td style="{cell}">{self._leg_label(trip.outbound)}</td>
                    <td style="{cell}">{self._leg_label(trip.inbound)}</td>
                    <td style="{cell}text-align:right;">{self._price_label(trip)}</td>
                </tr>"""

            sections += f"""
            <div style="margin:16px 0;padding:10px 8px;background:#fafafa;border-left:4px solid #1a73e8;border-radius:4px;">
                <h3 style="margin:0 0 8px 0;color:#1a73e8;font-size:17px;">
                    {city} <span style="color:#666;font-weight:normal;">({airport})</span>
                </h3>
                <table role="presentation" cellpadding="0" cellspacing="0" width="100%"
                       style="width:100%;border-collapse:collapse;font-size:13px;background:#fff;">
                    <thead>
                        <tr style="background:#f0f0f0;">
                            <th style="{head}">Dátum</th>
                            <th style="{head}">Légitárs.</th>
                            <th style="{head}">Oda</th>
                            <th style="{head}">Vissza</th>
                            <th style="{head}text-align:right;">Összár</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>
            </div>"""

        return self._page(f"""
            <h2 style="margin:0 0 4px 0;color:#1a73e8;font-size:20px;">Napi járatpárok</h2>
            <p style="margin:0 0 8px 0;color:#666;font-size:13px;">Generálva: {now} | Találatok: {len(trips)} | Célállomások: {len(sorted_keys)}</p>
            {self._warning_html(warning)}
            {sections}""")
