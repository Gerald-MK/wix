#!/usr/bin/env python3
"""
wix-termin-verschieben – Termin eines Klienten in Wix Bookings suchen und auf
einen neuen Tag / eine neue Uhrzeit verschieben.

Ablauf
  1. Buchungen des angegebenen Tages lesen (Query Extended Bookings)
  2. Nach Nachname filtern (genau, sonst Teilstring) – bei mehreren Treffern Auswahl
  3. Anzeigen: Terminbezeichnung, Nachname, Vorname, Start – Ende
  4. Neuen Tag + neuen Start abfragen (Dauer = Dauer des bisherigen Termins,
     d. h. der Wix-Leistung; mit --dauer überschreibbar)
  5. Termin verschieben (Reschedule Booking) – Klient wird NICHT benachrichtigt
  6. Antwort prüfen: neuer Start/Ende muss in der Buchung stehen

Aufruf
  wix-termin-verschieben.py                                  (fragt alles ab)
  wix-termin-verschieben.py Muster 27.11.
  wix-termin-verschieben.py Muster 27.11.2026 --neuer-tag 28.11. --neuer-start 14:00
  wix-termin-verschieben.py Muster morgen --neuer-tag übermorgen --neuer-start 9 --ja
  wix-termin-verschieben.py Muster 27.11. --nur-anzeigen     (nichts ändern)
  wix-termin-verschieben.py Muster 27.11. --site pt          (andere Wix-Site)

Datumsformate: 27.11.2026, 27.11.26, 27.11., 27.11, 2026-11-27, heute, morgen, übermorgen
Zeitformate:   14:00, 14.00, 1400, 14

Zugangsdaten (einmalig im macOS-Schlüsselbund ablegen)
  API-Key in die Zwischenablage kopieren, dann:
  security add-generic-password -U -s wix-praxis -a api_key -w "$(pbpaste)"
  ACHTUNG: Nicht "-w" ohne Wert verwenden – die verdeckte Abfrage schneidet
  nach 128 Zeichen ab, der Wix-Key ist aber länger.
  API-Key anlegen: https://manage.wix.com/account/api-keys
  Berechtigung: Wix Bookings (Buchungen lesen und verwalten), für die verwendete(n) Site(s)

Exit-Codes: 0 = ok, 1 = Abbruch/Fehler, 2 = vom Benutzer abgebrochen

Abhängigkeit: pip3 install requests
"""

import argparse
import re
import subprocess
import sys
import warnings
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# macOS-Python mit LibreSSL: urllib3-Hinweis unterdrücken (stört sonst in Raycast)
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import requests  # noqa: E402

# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------
KEYCHAIN_SERVICE = "wix-praxis"
KEYCHAIN_ACCOUNT = "api_key"
KEYCHAIN_BEFEHL = (f'security add-generic-password -U -s {KEYCHAIN_SERVICE} '
                   f'-a {KEYCHAIN_ACCOUNT} -w "$(pbpaste)"')

# Kurzname -> (Anzeigename, Site-ID). Site-IDs sind keine Geheimnisse.
SITES = {
    "psy":   ("PSY Gesundheit",       "e0dc29f6-5300-4cd6-81db-22fe8113b7ca"),
    "pt":    ("PT-Praxis NEU",        "a2915900-c392-48ed-b437-985a213cf2a7"),
    "check": ("PT Gesundheits-Check", "50f257fb-0f52-4138-a6d3-f884c1004fe2"),
}
DEFAULT_SITE = "psy"

TIMEZONE = "Europe/Vienna"      # Zeitzone der Wix-Buchungen (gleich wie Berlin)

# Öffentliche REST-Pfade (die /_api/-Varianten funktionieren mit API-Key nicht:
# "NO_METASITE_IN_CALL_CONTEXT")
URL_QUERY = "https://www.wixapis.com/bookings/bookings-reader/v2/extended-bookings/query"
URL_RESCHEDULE = "https://www.wixapis.com/bookings/v2/bookings/{id}/reschedule"

# Status, die verschoben werden können / angezeigt werden
STATUS_AKTIV = {"CONFIRMED", "PENDING", "WAITING_LIST"}
STATUS_DE = {
    "CONFIRMED": "bestätigt", "PENDING": "ausstehend", "WAITING_LIST": "Warteliste",
    "CANCELED": "storniert", "DECLINED": "abgelehnt", "CREATED": "angelegt",
}
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
TIMEOUT = 60


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
class Abbruch(Exception):
    """Fachlicher Abbruch mit Meldung."""


class BenutzerAbbruch(Exception):
    """Benutzer hat bewusst abgebrochen."""


class WixFehler(Abbruch):
    """Fehlerantwort der Wix-API (HTTP-Status + Wix-Fehlercode)."""

    def __init__(self, text, status=None, code=None):
        super().__init__(text)
        self.status = status
        self.code = code


def keychain():
    """Liest den Wix-API-Key aus dem macOS-Schlüsselbund."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        raise Abbruch("Kommando 'security' nicht gefunden – das Script läuft nur unter macOS.")
    except subprocess.CalledProcessError:
        raise Abbruch(
            f"Schlüsselbund-Eintrag fehlt: Dienst '{KEYCHAIN_SERVICE}', Account '{KEYCHAIN_ACCOUNT}'.\n"
            f"   Key in die Zwischenablage kopieren, dann: {KEYCHAIN_BEFEHL}"
        )
    key = out.stdout.strip()
    if re.fullmatch(r"[0-9a-fA-F-]{36}", key):
        raise Abbruch(
            "Im Schlüsselbund steht eine ID (36 Zeichen), nicht der API-Key.\n"
            "   Der Wix-API-Key ist ein langer Text, der mit 'IST.' beginnt (wird nur beim Erstellen\n"
            "   angezeigt, sonst in Wix 'Key neu generieren').\n"
            f"   Key in die Zwischenablage kopieren, dann: {KEYCHAIN_BEFEHL}"
        )
    if len(key) <= 128:
        raise Abbruch(
            f"Der API-Key im Schlüsselbund ist nur {len(key)} Zeichen lang – vermutlich abgeschnitten\n"
            "   (die verdeckte Abfrage von 'security -w' nimmt max. 128 Zeichen).\n"
            f"   Key in die Zwischenablage kopieren, dann: {KEYCHAIN_BEFEHL}"
        )
    return key


def interaktiv():
    return sys.stdin.isatty()


def frage(text, pflicht=True):
    """Fragt einen Wert im Terminal ab. Ohne Terminal (z. B. Raycast) -> Abbruch."""
    if not interaktiv():
        raise Abbruch(f"Eingabe fehlt: {text.rstrip(': ')} (kein Terminal für Rückfragen)")
    while True:
        try:
            wert = input(text).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise BenutzerAbbruch()
        if wert or not pflicht:
            return wert


def parse_datum(text, heute=None):
    """Wandelt 27.11.2026 / 27.11. / 2026-11-27 / heute / morgen / übermorgen in ein date."""
    heute = heute or datetime.now(ZoneInfo(TIMEZONE)).date()
    t = text.strip().lower()
    relativ = {"heute": 0, "morgen": 1, "übermorgen": 2, "uebermorgen": 2}
    if t in relativ:
        return heute + timedelta(days=relativ[t])

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        j, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.?(\d{2}|\d{4})?", t)
        if not m:
            raise Abbruch(f"Datum nicht erkannt: '{text}' (z. B. 27.11.2026, 27.11., morgen)")
        d, mo = int(m.group(1)), int(m.group(2))
        if m.group(3):
            j = int(m.group(3))
            j += 2000 if j < 100 else 0
        else:
            # Ohne Jahr: nächstes Vorkommen ab heute (Dezember -> Januar springt ins neue Jahr)
            j = heute.year
            try:
                if date(j, mo, d) < heute - timedelta(days=31):
                    j += 1
            except ValueError:
                pass
    try:
        return date(j, mo, d)
    except ValueError:
        raise Abbruch(f"Ungültiges Datum: '{text}'")


def parse_zeit(text):
    """Wandelt 14:00 / 14.00 / 1400 / 14 in (stunde, minute)."""
    t = text.strip().lower().replace("uhr", "").strip()
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", t) or re.fullmatch(r"(\d{2})(\d{2})", t)
    if not m:
        raise Abbruch(f"Uhrzeit nicht erkannt: '{text}' (z. B. 14:00)")
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise Abbruch(f"Ungültige Uhrzeit: '{text}'")
    return h, mi


def iso_lokal(dt):
    """2026-11-27T13:00:00.000+01:00 – dasselbe Format, das Wix in Slots verwendet."""
    s = dt.strftime("%Y-%m-%dT%H:%M:%S.000%z")
    return s[:-2] + ":" + s[-2:]


def iso_utc(dt):
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def fmt_tag(dt):
    return f"{WOCHENTAGE[dt.weekday()]}, {dt.strftime('%d.%m.%Y')}"


def fmt_zeitraum(start, ende):
    tz = ZoneInfo(TIMEZONE)
    s, e = start.astimezone(tz), ende.astimezone(tz)
    if s.date() == e.date():
        return f"{fmt_tag(s)}, {s.strftime('%H:%M')} – {e.strftime('%H:%M')} Uhr"
    return f"{fmt_tag(s)} {s.strftime('%H:%M')} – {fmt_tag(e)} {e.strftime('%H:%M')} Uhr"


# --------------------------------------------------------------------------
# Wix-API
# --------------------------------------------------------------------------
class Wix:
    def __init__(self, api_key, site_id):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": api_key,
            "wix-site-id": site_id,
            "Content-Type": "application/json",
        })

    def post(self, url, body, schritt):
        try:
            r = self.s.post(url, json=body, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise Abbruch(f"{schritt}: Verbindung fehlgeschlagen – {e}")
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400 or data is None:
            code = None
            if isinstance(data, dict):
                code = ((data.get("details") or {}).get("applicationError") or {}).get("code")
            raise WixFehler(f"{schritt}: HTTP {r.status_code} – {wix_fehler(r, data)}",
                            status=r.status_code, code=code)
        return data

    def buchungen_am_tag(self, tag):
        """Alle Buchungen mit Start an diesem Tag (lokale Zeit), sortiert nach Start."""
        tz = ZoneInfo(TIMEZONE)
        von = datetime(tag.year, tag.month, tag.day, tzinfo=tz)
        bis = von + timedelta(days=1)
        filt = {"$and": [
            {"startDate": {"$gte": iso_utc(von)}},
            {"startDate": {"$lt": iso_utc(bis)}},
        ]}
        query = {"filter": filt, "sort": [{"fieldName": "startDate", "order": "ASC"}],
                 "cursorPaging": {"limit": 100}}
        alle = []
        for _ in range(20):
            data = self.post(URL_QUERY, {"query": query}, "Buchungen lesen")
            items = data.get("extendedBookings") or []
            alle += [e["booking"] for e in items if e.get("booking")]
            cursor = ((data.get("pagingMetadata") or {}).get("cursors") or {}).get("next")
            if not cursor or not items:
                break
            query = {"cursorPaging": {"limit": 100, "cursor": cursor}}
        return alle

    def verschieben(self, buchung, neu_start, neu_ende, erzwingen=False, richtlinie_ignorieren=False):
        alt = buchung["bookedEntity"]["slot"]
        slot = {
            "serviceId": alt["serviceId"],
            "scheduleId": alt["scheduleId"],
            "timezone": alt.get("timezone") or TIMEZONE,
            "startDate": iso_lokal(neu_start),
            "endDate": iso_lokal(neu_ende),
        }
        # Mitarbeiter/Ressource und Ort vom alten Termin übernehmen
        if alt.get("resource", {}).get("id"):
            slot["resource"] = {k: alt["resource"][k] for k in ("id", "name") if alt["resource"].get(k)}
        if alt.get("location"):
            slot["location"] = {k: alt["location"][k]
                                for k in ("id", "name", "formattedAddress", "locationType")
                                if alt["location"].get(k)}
        if alt.get("resourceSelections"):
            slot["resourceSelections"] = alt["resourceSelections"]

        body = {
            "slot": slot,
            "revision": buchung["revision"],
            "participantNotification": {"notifyParticipants": False},
        }
        flow = {}
        if erzwingen:
            flow["skipAvailabilityValidation"] = True
            richtlinie_ignorieren = True
        if richtlinie_ignorieren:
            # Admin-Override: Umbuchungsregel der Leistung (z. B. "bis 24 h vorher") ignorieren
            flow["ignoreReschedulePolicy"] = True
        if flow:
            body["flowControlSettings"] = flow

        url = URL_RESCHEDULE.format(id=buchung["id"])
        data = self.post(url, body, "Termin verschieben")
        neu = data.get("booking")
        if not neu:
            raise Abbruch(f"Termin verschieben: Antwort ohne Buchung – {str(data)[:300]}")
        return neu


def wix_fehler(r, data):
    """Kurze, lesbare Wix-Fehlermeldung inkl. Request-ID für den Support."""
    teile = []
    if isinstance(data, dict):
        teile.append(data.get("message") or "")
        app = (data.get("details") or {}).get("applicationError") or {}
        if app:
            teile.append(f"[{app.get('code', '')}] {app.get('description', '')}".strip())
    else:
        teile.append(r.text[:300])
    hinweis = {
        401: "API-Key ungültig oder abgelaufen?",
        403: "API-Key hat keine Berechtigung für Wix Bookings oder diese Site?",
        404: "Buchung/Site nicht gefunden?",
        409: "Buchung wurde inzwischen geändert – Script neu starten.",
    }.get(r.status_code)
    if hinweis:
        teile.append(hinweis)
    rid = r.headers.get("x-wix-request-id")
    if rid:
        teile.append(f"(Request-ID {rid})")
    return " ".join(t for t in teile if t)


# --------------------------------------------------------------------------
# Ablauf
# --------------------------------------------------------------------------
def name_von(b):
    c = b.get("contactDetails") or {}
    return (c.get("lastName") or "").strip(), (c.get("firstName") or "").strip()


def zeitraum_von(b):
    slot = (b.get("bookedEntity") or {}).get("slot") or {}
    start = b.get("startDate") or slot.get("startDate")
    ende = b.get("endDate") or slot.get("endDate")
    return parse_iso(start), parse_iso(ende)


def zeige(b, nr=None):
    nach, vor = name_von(b)
    start, ende = zeitraum_von(b)
    titel = (b.get("bookedEntity") or {}).get("title") or "(ohne Bezeichnung)"
    status = STATUS_DE.get(b.get("status"), b.get("status", ""))
    prefix = f"  [{nr}] " if nr is not None else "  "
    einr = " " * len(prefix)
    print(f"{prefix}Terminbezeichnung: {titel}")
    print(f"{einr}Name:              {nach}, {vor}")
    print(f"{einr}Termin:            {fmt_zeitraum(start, ende)}")
    if b.get("status") != "CONFIRMED":
        print(f"{einr}Status:            {status}")


def suche(buchungen, nachname):
    """Erst exakter Nachname, sonst Teilstring in Vor-/Nachname. Stornierte ausgeblendet."""
    aktiv = [b for b in buchungen if b.get("status") in STATUS_AKTIV]
    n = nachname.strip().lower()
    exakt = [b for b in aktiv if name_von(b)[0].lower() == n]
    if exakt:
        return exakt
    return [b for b in aktiv if n in " ".join(name_von(b)).lower()]


def waehle(treffer):
    if len(treffer) == 1:
        return treffer[0]
    print(f"\n{len(treffer)} passende Termine gefunden:\n")
    for i, b in enumerate(treffer, 1):
        zeige(b, i)
        print()
    if not interaktiv():
        raise Abbruch("Mehrere Treffer – bitte Vornamen ergänzen oder im Terminal auswählen.")
    while True:
        w = frage(f"Welcher Termin? [1–{len(treffer)}, q = abbrechen]: ")
        if w.lower() in ("q", "quit", "abbrechen"):
            raise BenutzerAbbruch()
        if w.isdigit() and 1 <= int(w) <= len(treffer):
            return treffer[int(w) - 1]


def bestaetigen(text):
    if not interaktiv():
        raise Abbruch("Bestätigung nötig, aber kein Terminal – mit --ja aufrufen.")
    return frage(f"{text} [j/N]: ", pflicht=False).lower() in ("j", "ja", "y", "yes")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Wix-Termin eines Klienten suchen und verschieben.",
        epilog="Ohne Argumente fragt das Script alles interaktiv ab.",
    )
    p.add_argument("nachname", nargs="?", help="Nachname des Klienten")
    p.add_argument("datum", nargs="?", help="Tag des bisherigen Termins (z. B. 27.11.2026, morgen)")
    p.add_argument("--neuer-tag", help="neuer Tag (z. B. 28.11.)")
    p.add_argument("--neuer-start", help="neue Startzeit (z. B. 14:00)")
    p.add_argument("--dauer", type=int,
                   help="Dauer in Minuten (Standard: wie bisheriger Termin, z. B. 50)")
    p.add_argument("--site", default=DEFAULT_SITE,
                   help=f"Wix-Site: {', '.join(SITES)} oder Site-ID (Standard {DEFAULT_SITE})")
    p.add_argument("--ja", action="store_true", help="ohne Rückfrage verschieben")
    p.add_argument("--nur-anzeigen", action="store_true", help="nur suchen und anzeigen, nichts ändern")
    p.add_argument("--erzwingen", action="store_true",
                   help="Buchungsrichtlinie und Wix-Verfügbarkeit ignorieren (Admin-Override)")
    a = p.parse_args(argv)

    # Site
    if a.site in SITES:
        site_name, site_id = SITES[a.site]
    elif re.fullmatch(r"[0-9a-f-]{36}", a.site):
        site_name, site_id = a.site, a.site
    else:
        raise Abbruch(f"Unbekannte Site '{a.site}' – erlaubt: {', '.join(SITES)} oder eine Site-ID")
    if a.dauer is not None and not 0 < a.dauer <= 600:
        raise Abbruch(f"Ungültige Dauer: {a.dauer} min")

    # 1. Eingaben: Nachname + Datum
    nachname = a.nachname or frage("Nachname des Klienten: ")
    tag = parse_datum(a.datum or frage("Datum des Termins (z. B. 27.11.): "))

    wix = Wix(keychain(), site_id)

    # 2. Suchen
    print(f"Suche „{nachname}“ am {fmt_tag(tag)} in {site_name} …")
    buchungen = wix.buchungen_am_tag(tag)
    treffer = suche(buchungen, nachname)
    if not treffer:
        raise Abbruch(f"Kein Termin für „{nachname}“ am {fmt_tag(tag)} gefunden "
                      f"({len(buchungen)} Buchung(en) an diesem Tag).")
    buchung = waehle(treffer)

    # 3. Anzeigen
    print()
    zeige(buchung)
    print()
    if a.nur_anzeigen:
        return 0

    # 4. Neuer Tag + Start
    neuer_tag = parse_datum(a.neuer_tag or frage("Neuer Tag (z. B. 28.11.): "))
    h, mi = parse_zeit(a.neuer_start or frage("Neuer Start (z. B. 14:00): "))
    tz = ZoneInfo(TIMEZONE)
    alt_start, alt_ende = zeitraum_von(buchung)
    dauer = a.dauer or int((alt_ende - alt_start).total_seconds() // 60)
    neu_start = datetime(neuer_tag.year, neuer_tag.month, neuer_tag.day, h, mi, tzinfo=tz)
    neu_ende = neu_start + timedelta(minutes=dauer)

    if neu_start == alt_start and neu_ende == alt_ende:
        raise Abbruch("Neuer Termin ist identisch mit dem bisherigen – nichts zu tun.")
    if neu_start < datetime.now(tz):
        print("⚠️  Achtung: Der neue Termin liegt in der Vergangenheit.")

    print(f"\n  bisher: {fmt_zeitraum(alt_start, alt_ende)}")
    print(f"  neu:    {fmt_zeitraum(neu_start, neu_ende)}  ({dauer} min)")
    print("  Klient wird NICHT benachrichtigt.")
    if a.erzwingen:
        print("  ⚠️  --erzwingen: Wix prüft weder Buchungsrichtlinie noch Verfügbarkeit.")
    print()
    if not a.ja and not bestaetigen("Termin verschieben?"):
        raise BenutzerAbbruch()

    # 5. Verschieben
    try:
        neu = wix.verschieben(buchung, neu_start, neu_ende, erzwingen=a.erzwingen)
    except WixFehler as e:
        if e.code != "BOOKING_POLICY_VIOLATION" or a.erzwingen:
            raise
        print("\n⚠️  Wix lehnt ab: Die Buchungsrichtlinie der Leistung erlaubt diese Umbuchung nicht")
        print("   (z. B. Umbuchen nur bis X Stunden vor dem Termin).")
        if not interaktiv():
            raise Abbruch("Buchungsrichtlinie verhindert die Umbuchung – mit --erzwingen aufrufen, "
                          "um sie als Praxisinhaber zu übergehen.")
        if not bestaetigen("Richtlinie als Praxisinhaber ignorieren und trotzdem verschieben?"):
            raise BenutzerAbbruch()
        neu = wix.verschieben(buchung, neu_start, neu_ende, richtlinie_ignorieren=True)

    # 6. Prüfen
    ist_start, ist_ende = zeitraum_von(neu)
    if ist_start != neu_start or ist_ende != neu_ende:
        raise Abbruch(f"Wix meldet einen anderen Termin als angefragt: "
                      f"{fmt_zeitraum(ist_start, ist_ende)} – bitte im Wix-Kalender prüfen.")
    print(f"✅ Verschoben: {fmt_zeitraum(ist_start, ist_ende)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BenutzerAbbruch:
        print("Abgebrochen – nichts geändert.")
        sys.exit(2)
    except Abbruch as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
