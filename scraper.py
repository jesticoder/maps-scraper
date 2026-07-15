"""
Hannover Handwerker Lead Finder
================================
Findet Handwerksbetriebe ohne Website oder mit veralteter Website.
Quellen: Google Places API, Gelbe Seiten Scraper, Wayback Machine

v2 — Concurrent: Website-Checks laufen parallel via ThreadPoolExecutor
"""

import json
import time
import socket
import urllib.request
import urllib.parse
import urllib.error
import re
import csv
import os
import zlib
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional
from dotenv import load_dotenv
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')


# ─────────────────────────────────────────────
# KONFIGURATION — hier anpassen
# ─────────────────────────────────────────────
load_dotenv()
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
# PageSpeed Insights läuft im selben Google-Cloud-Projekt — Key kann per
# PAGESPEED_API_KEY überschrieben werden, fällt sonst auf den Places-Key zurück.
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY") or GOOGLE_PLACES_API_KEY

BRANCHEN = [
    "Elektriker",
    "Klempner",
    "Installateur",
    "Maler",
    "Fliesenleger",
    "Dachdecker",
    "Zimmermann",
    "Schreiner",
    "Heizungsbauer",
    "Sanitär",
    "Trockenbau",
    "Fenster und Türen",
    "Gartenbau",
    "Reinigungsfirma",
    "Schlüsseldienst",
]

ZIELSTADT = "Hannover"
# Dateiname folgt automatisch der Stadt — verhindert, dass ein Lauf für eine neue
# Stadt (z.B. Hildesheim) die bereits gesammelten Leads einer anderen Stadt überschreibt.
_stadt_slug = ZIELSTADT.lower().replace(" ", "_")
OUTPUT_FILE = f"leads_{_stadt_slug}.csv"
OUTPUT_JSON = f"leads_{_stadt_slug}.json"

# Score-Schwelle: Leads mit Score >= X gelten als "heiß"
HOT_LEAD_SCORE = 70

# Anzahl gleichzeitiger Website-Checks pro Branche
MAX_WORKERS = 10

# PageSpeed Insights (SEO-/Performance-Score von Google selbst) pro Website abfragen?
# Kostenlos, aber mit ~3-10s Antwortzeit der langsamste Einzelcheck — bei Bedarf abschalten.
PRUEFE_PAGESPEED = True

# Unterhalb dieser Wortzahl im sichtbaren Text gilt eine Seite als "Thin Content"
MIN_WOERTER_CONTENT = 200

# Wie viele Bytes vom HTML pro Seite gelesen werden. Muss groß genug sein, um Footer-
# Inhalte (Impressum/Datenschutz-Links, JSON-LD) zu erreichen — nicht nur den <head>.
# Bei Themes/Baukasten-Systemen mit voluminösem Inline-JS/JSON vor dem eigentlichen
# Content (z.B. Avada/WordPress mit base64-Blobs, oder Wix' Thunderbolt-Renderer, der
# oft 200KB+ Preload-Daten VOR dem <title>-Tag ausliefert) landen Title, Meta-Description
# und Footer sonst außerhalb des gelesenen Bereichs — beobachtet bei einer 1MB+ Wix-Seite,
# wo Impressum/Datenschutz erst bei Byte ~660.000 auftauchten.
MAX_HTML_BYTES = 3_000_000


# ─────────────────────────────────────────────
# DATENMODELL
# ─────────────────────────────────────────────
@dataclass
class Lead:
    name: str
    branche: str
    telefon: str = ""
    email: str = ""
    adresse: str = ""
    website: str = ""

    # Website-Analyse
    hat_website: bool = False
    website_erreichbar: bool = False
    website_alter_jahre: Optional[int] = None
    hat_ssl: bool = False
    ladezeit_ms: Optional[int] = None
    ist_mobilfreundlich: Optional[bool] = None
    wayback_datum: str = ""

    # SEO-Analyse
    seo_titel_vorhanden: bool = False
    seo_meta_beschreibung_vorhanden: bool = False
    seo_noindex: bool = False
    seo_thin_content: bool = False
    seo_impressum_vorhanden: bool = False
    seo_datenschutz_vorhanden: bool = False
    seo_strukturierte_daten_vorhanden: bool = False
    hat_kontaktmoeglichkeit: bool = False
    website_baukasten: str = ""
    pagespeed_seo_score: Optional[int] = None
    pagespeed_performance_score: Optional[int] = None

    # Google-Daten
    google_place_id: str = ""
    google_rating: float = 0.0
    google_bewertungen: int = 0
    gbp_fotos_anzahl: Optional[int] = None
    gbp_kategorien: str = ""

    # Scoring
    score: int = 0
    score_gruende: list = field(default_factory=list)

    # Meta
    gefunden_am: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    status: str = "neu"  # neu, kontaktiert, kein_interesse, kunde


# ─────────────────────────────────────────────
# GOOGLE PLACES SUCHE
# ─────────────────────────────────────────────
def suche_google_places(branche: str, stadt: str, api_key: str) -> list[dict]:
    """Sucht Betriebe über Google Places Text Search API."""

    if api_key == "GOOGLE_PLACES_API_KEY" or not api_key:
        print(f"  ⚠  Kein API-Key — verwende Demo-Daten für '{branche}'")
        return _demo_daten(branche, stadt)

    query = urllib.parse.quote(f"{branche} {stadt}")
    url = (
        f"https://maps.googleapis.com/maps/api/place/textsearch/json"
        f"?query={query}&language=de&region=de&key={api_key}"
    )

    results = []
    page_token = None

    for _ in range(3):  # max 3 Seiten = 60 Ergebnisse
        page_url = url
        if page_token:
            page_url += f"&pagetoken={page_token}"

        try:
            with urllib.request.urlopen(page_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                results.extend(data.get("results", []))
                page_token = data.get("next_page_token")
                if not page_token:
                    break
                time.sleep(2)  # Google braucht kurze Pause vor next_page
        except Exception as e:
            print(f"  ✗ Google API Fehler: {e}")
            break

    return results


def _demo_daten(branche: str, stadt: str) -> list[dict]:
    """Realistische Demo-Daten wenn kein API-Key vorhanden."""
    import random
    random.seed(hash(branche))

    namen_muster = [
        f"{branche}meister {stadt}",
        f"Firma Müller {branche}",
        f"{branche}betrieb Schneider & Söhne",
        f"Hansen {branche}service",
        f"Meisterbetrieb Koch",
        f"{branche} Richter GmbH",
        f"Fachbetrieb Schulze",
        f"{branche} Hoffmann",
        f"Becker & Partner {branche}",
        f"{branche}arbeiten Weber",
    ]

    strassen = [
        "Hauptstraße", "Bahnhofstraße", "Marktplatz", "Kirchweg",
        "Industriestraße", "Gartenweg", "Lindenallee", "Bergstraße"
    ]

    ergebnisse = []
    anzahl = random.randint(6, 10)

    for i in range(anzahl):
        hat_website = random.random() > 0.45
        website_url = ""
        if hat_website:
            firma_slug = namen_muster[i % len(namen_muster)].lower().replace(" ", "-").replace("&", "und")
            firma_slug = re.sub(r'[^a-z0-9-]', '', firma_slug)[:30]
            website_url = f"https://www.{firma_slug}.de"

        ergebnisse.append({
            "name": namen_muster[i % len(namen_muster)],
            "formatted_address": f"{strassen[i % len(strassen)]} {random.randint(1, 99)}, {random.randint(30159, 30627)} {stadt}",
            "formatted_phone_number": f"+49 511 {random.randint(100000, 999999)}",
            "website": website_url,
            "place_id": f"DEMO_{branche}_{i}",
            "rating": round(random.uniform(3.5, 5.0), 1),
            "user_ratings_total": random.randint(3, 120),
        })

    return ergebnisse


def extrahiere_place_details(place_id: str, api_key: str) -> dict:
    """Holt Detailinfos (Telefon, Website, Fotos, Kategorien) für einen Place.
    Hinweis: 'photos' zählt zur teureren Places-Details-SKU (Atmosphere Data) —
    bei Kostendruck aus `fields` entfernen und gbp_fotos_anzahl bleibt None."""
    if not api_key or api_key == "GOOGLE_PLACES_API_KEY" or place_id.startswith("DEMO_"):
        return {}

    fields = "formatted_phone_number,website,formatted_address,photos,types"
    url = (
        f"https://maps.googleapis.com/maps/api/place/details/json"
        f"?place_id={place_id}&fields={fields}&language=de&key={api_key}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("result", {})
    except Exception:
        return {}


def pruefe_pagespeed(url: str, api_key: str) -> dict:
    """Fragt Google PageSpeed Insights (Lighthouse) für SEO- und Performance-Score ab.
    Kostenlos, aber die langsamste Einzelabfrage der Pipeline (~3-10s, teils mehr)."""
    r = {"pagespeed_seo_score": None, "pagespeed_performance_score": None}
    if not PRUEFE_PAGESPEED or not url:
        return r

    params = {"url": url, "strategy": "mobile", "category": ["seo", "performance"]}
    if api_key and api_key != "GOOGLE_PLACES_API_KEY":
        params["key"] = api_key
    psi_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{urllib.parse.urlencode(params, doseq=True)}"

    try:
        with urllib.request.urlopen(psi_url, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            kategorien = data.get("lighthouseResult", {}).get("categories", {})
            seo = kategorien.get("seo", {}).get("score")
            perf = kategorien.get("performance", {}).get("score")
            r["pagespeed_seo_score"] = round(seo * 100) if seo is not None else None
            r["pagespeed_performance_score"] = round(perf * 100) if perf is not None else None
    except Exception:
        pass  # Timeout/Quota/unerreichbare Seite — kein harter Fehler für den Lead
    return r


_BAUKASTEN_MARKER = [
    ("Jimdo", ("jimdo.com", "jimdocdn.com", "jimdo-server")),
    ("Wix", ("wixsite.com", "static.wixstatic.com", "parastorage.com", "wix.com website builder")),
    ("IONOS/1&1 MyWebsite", ("mywebsite-editor", "1und1 mywebsite", "ionos mywebsite", "homepage-baukasten.ionos")),
    ("STRATO Homepage-Baukasten", ("strato-homepage-baukasten", "sh-homepage-baukasten")),
]


def _erkenne_baukasten(inhalt: str) -> str:
    """Best-effort Fingerprint über bekannte Asset-Domains/Generator-Strings —
    erkennt nur verbreitete Baukasten-Systeme, kein Anspruch auf Vollständigkeit."""
    inhalt_l = inhalt.lower()
    for name, marker in _BAUKASTEN_MARKER:
        if any(m in inhalt_l for m in marker):
            return name
    return ""


# ─────────────────────────────────────────────
# WEBSITE-ANALYSE  (läuft im Thread-Pool)
# ─────────────────────────────────────────────
def analysiere_website(url: str) -> dict:
    """Prüft ob Website existiert, wie alt sie ist, Qualitäts- und SEO-Faktoren.
    HTTP-Check, Wayback-Abfrage und PageSpeed Insights laufen parallel via inner ThreadPoolExecutor."""
    result = {
        "erreichbar": False,
        "hat_ssl": False,
        "ladezeit_ms": None,
        "wayback_datum": "",
        "alter_jahre": None,
        "ist_mobilfreundlich": None,
        "fehler": "",
        "seo_titel_vorhanden": False,
        "seo_meta_beschreibung_vorhanden": False,
        "seo_noindex": False,
        "seo_thin_content": False,
        "seo_impressum_vorhanden": False,
        "seo_datenschutz_vorhanden": False,
        "seo_strukturierte_daten_vorhanden": False,
        "hat_kontaktmoeglichkeit": False,
        "website_baukasten": "",
        "pagespeed_seo_score": None,
        "pagespeed_performance_score": None,
        "email": "",
    }

    if not url:
        return result

    result["hat_ssl"] = url.startswith("https://")  # Fallback falls der Request unten fehlschlägt
    domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]

    # ── HTTP-Check und Wayback gleichzeitig starten ──────────────────────────
    def _http_check():
        r = {
            "erreichbar": False, "hat_ssl": None, "ladezeit_ms": None, "ist_mobilfreundlich": None, "fehler": "",
            "seo_titel_vorhanden": False, "seo_meta_beschreibung_vorhanden": False,
            "seo_noindex": False, "seo_thin_content": False,
            "seo_impressum_vorhanden": False, "seo_datenschutz_vorhanden": False,
            "seo_strukturierte_daten_vorhanden": False, "hat_kontaktmoeglichkeit": False,
            "website_baukasten": "", "email": "",
        }
        try:
            start = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                rohdaten = resp.read(MAX_HTML_BYTES)
                # Manche Server (z.B. hinter next-boost/aggressive Reverse-Proxies) schicken
                # gzip auch ohne passendes Accept-Encoding — decompressobj statt gzip.decompress,
                # da der Byte-Schnitt den Stream vor dem Ende abschneiden kann.
                if resp.info().get("Content-Encoding", "").lower() == "gzip":
                    try:
                        inhalt = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(rohdaten).decode("utf-8", errors="ignore")
                    except Exception:
                        # Manche Proxies deklarieren gzip, liefern aber unkomprimierten Text —
                        # Rohbytes direkt interpretieren statt alle SEO-Signale auf leer zu setzen.
                        inhalt = rohdaten.decode("utf-8", errors="ignore")
                else:
                    inhalt = rohdaten.decode("utf-8", errors="ignore")
                r["erreichbar"] = resp.status == 200
                # Google Places liefert oft die http://-URL, auch wenn die Seite auf https
                # weiterleitet — die tatsächliche Ziel-URL nach Redirects zählt, nicht der
                # ursprünglich abgefragte String.
                r["hat_ssl"] = resp.geturl().startswith("https://")
                r["ladezeit_ms"] = int((time.time() - start) * 1000)
                r["ist_mobilfreundlich"] = "viewport" in inhalt.lower()

                # On-Page-SEO-Signale aus dem bereits geladenen HTML — kein Extra-Request
                titel = re.search(r"<title[^>]*>(.*?)</title>", inhalt, re.IGNORECASE | re.DOTALL)
                r["seo_titel_vorhanden"] = bool(titel and titel.group(1).strip())

                meta_desc = re.search(
                    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
                    inhalt, re.IGNORECASE,
                )
                r["seo_meta_beschreibung_vorhanden"] = bool(meta_desc and meta_desc.group(1).strip())

                robots_meta = re.search(
                    r'<meta[^>]+name=["\']robots["\'][^>]+content=["\']([^"\']*)["\']',
                    inhalt, re.IGNORECASE,
                )
                r["seo_noindex"] = bool(robots_meta and "noindex" in robots_meta.group(1).lower())

                ohne_script_style = re.sub(
                    r"<(script|style)[^>]*>.*?</\1>", " ", inhalt, flags=re.IGNORECASE | re.DOTALL
                )
                text_only = re.sub(r"<[^>]+>", " ", ohne_script_style)
                r["seo_thin_content"] = len(text_only.split()) < MIN_WOERTER_CONTENT

                # E-Mail: zuerst mailto:-Links (zuverlässig), sonst Fließtext-Suche
                # im script/style-bereinigten Text (vermeidet Treffer in Tracking-JS o.ä.)
                mailto = re.search(r"mailto:([\w.+-]+@[\w.-]+\.\w{2,})", inhalt, re.IGNORECASE)
                if mailto:
                    r["email"] = mailto.group(1)
                else:
                    email_match = re.search(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b", text_only)
                    r["email"] = email_match.group(0) if email_match else ""

                # Pflichtangaben (TMG), strukturierte Daten, Baukasten-Fingerprint und
                # Kontaktmöglichkeit — alles aus dem bereits geladenen HTML, kein Extra-Request
                r["seo_impressum_vorhanden"] = "impressum" in inhalt.lower()
                r["seo_datenschutz_vorhanden"] = "datenschutz" in inhalt.lower()
                r["seo_strukturierte_daten_vorhanden"] = (
                    bool(re.search(r"application/ld\+json", inhalt, re.IGNORECASE))
                    and "schema.org" in inhalt.lower()
                )
                r["website_baukasten"] = _erkenne_baukasten(inhalt)
                r["hat_kontaktmoeglichkeit"] = bool(
                    re.search(r"<form\b", inhalt, re.IGNORECASE)
                    or re.search(r'href=["\']tel:', inhalt, re.IGNORECASE)
                    or mailto
                )
        except urllib.error.HTTPError as e:
            r["fehler"] = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            r["fehler"] = f"URL Fehler: {str(e.reason)[:50]}"
        except Exception as e:
            r["fehler"] = str(e)[:50]
        return r

    def _wayback_check():
        r = {"wayback_datum": "", "alter_jahre": None}
        try:
            wb_url = f"http://archive.org/wayback/available?url={domain}&timestamp=20050101"
            with urllib.request.urlopen(wb_url, timeout=6) as resp:
                wb_data = json.loads(resp.read().decode())
                snapshot = wb_data.get("archived_snapshots", {}).get("closest", {})
                if snapshot.get("available"):
                    ts = snapshot.get("timestamp", "")
                    if ts:
                        jahr = int(ts[:4])
                        r["wayback_datum"] = f"{ts[6:8]}.{ts[4:6]}.{ts[:4]}"
                        r["alter_jahre"] = datetime.now().year - jahr
        except Exception:
            pass
        return r

    def _pagespeed_check():
        return pruefe_pagespeed(url, PAGESPEED_API_KEY)

    with ThreadPoolExecutor(max_workers=3) as inner:
        f_http = inner.submit(_http_check)
        f_wb = inner.submit(_wayback_check)
        f_ps = inner.submit(_pagespeed_check)
        http_r = f_http.result()
        wb_r = f_wb.result()
        ps_r = f_ps.result()

    result.update(http_r)
    if result["hat_ssl"] is None:
        # Request komplett fehlgeschlagen (Timeout/DNS/o.ä.) — auf den ursprünglichen
        # String-Fallback zurückfallen, da wir keine tatsächliche Ziel-URL kennen.
        result["hat_ssl"] = url.startswith("https://")
    result.update(wb_r)
    result.update(ps_r)
    return result


# ─────────────────────────────────────────────
# LEAD-SCORING
# ─────────────────────────────────────────────
def berechne_score(lead: Lead) -> tuple[int, list[str]]:
    score = 0
    gruende = []

    if not lead.hat_website:
        score += 40
        gruende.append("Keine Website gefunden (+40)")
    elif not lead.website_erreichbar:
        score += 35
        gruende.append("Website nicht erreichbar (+35)")
    else:
        # Domain-Alter (Wayback) allein sagt nichts über den aktuellen Zustand der Seite
        # aus — eine alte Domain kann längst neu gebaut worden sein. Nur zusammen mit
        # einem echten Veraltet-Symptom werten, sonst verzerrt es den Score unnötig.
        website_wirkt_veraltet = (
            lead.ist_mobilfreundlich == False
            or not lead.hat_ssl
            or not lead.seo_titel_vorhanden
        )

        if lead.website_alter_jahre and lead.website_alter_jahre >= 8 and website_wirkt_veraltet:
            score += 25
            gruende.append(f"Website sehr alt ({lead.website_alter_jahre} Jahre) und wirkt veraltet (+25)")
        elif lead.website_alter_jahre and lead.website_alter_jahre >= 5 and website_wirkt_veraltet:
            score += 15
            gruende.append(f"Website veraltet ({lead.website_alter_jahre} Jahre) (+15)")

        if not lead.hat_ssl:
            score += 10
            gruende.append("Kein HTTPS (+10)")

        if lead.ist_mobilfreundlich == False:
            score += 15
            gruende.append("Nicht mobilfreundlich (+15)")

        if lead.ladezeit_ms and lead.ladezeit_ms > 3000:
            score += 10
            gruende.append(f"Sehr langsam ({lead.ladezeit_ms}ms) (+10)")
        elif lead.ladezeit_ms and lead.ladezeit_ms > 2000:
            score += 5
            gruende.append(f"Langsam ({lead.ladezeit_ms}ms) (+5)")

        # ── SEO-Signale — nur aussagekräftig wenn die Seite überhaupt lädt ──
        if lead.seo_noindex:
            score += 20
            gruende.append("Seite per noindex von Google ausgeschlossen! (+20)")

        if not lead.seo_titel_vorhanden:
            score += 10
            gruende.append("Kein Title-Tag (+10)")

        if not lead.seo_meta_beschreibung_vorhanden:
            score += 5
            gruende.append("Keine Meta-Description (+5)")

        if lead.seo_thin_content:
            score += 10
            gruende.append(f"Wenig Textinhalt / Thin Content (<{MIN_WOERTER_CONTENT} Wörter) (+10)")

        if not lead.seo_impressum_vorhanden:
            score += 15
            gruende.append("Kein Impressum gefunden — rechtliches Risiko (+15)")

        if not lead.seo_datenschutz_vorhanden:
            score += 10
            gruende.append("Keine Datenschutzerklärung gefunden (+10)")

        if not lead.seo_strukturierte_daten_vorhanden:
            score += 5
            gruende.append("Keine strukturierten Daten / schema.org (+5)")

        if lead.website_baukasten:
            score += 10
            gruende.append(f"Läuft auf Baukasten-System ({lead.website_baukasten}) (+10)")

        if not lead.hat_kontaktmoeglichkeit:
            score += 15
            gruende.append("Keine erkennbare Kontaktmöglichkeit (Formular/Tel-Link/E-Mail-Link) (+15)")

        if lead.pagespeed_seo_score is not None and lead.pagespeed_seo_score < 70:
            score += 15
            gruende.append(f"Schwacher PageSpeed-SEO-Score ({lead.pagespeed_seo_score}/100) (+15)")

        if lead.pagespeed_performance_score is not None and lead.pagespeed_performance_score < 50:
            score += 15
            gruende.append(f"Sehr schwache PageSpeed-Performance ({lead.pagespeed_performance_score}/100) (+15)")
        elif lead.pagespeed_performance_score is not None and lead.pagespeed_performance_score < 70:
            score += 5
            gruende.append(f"Mittelmäßige PageSpeed-Performance ({lead.pagespeed_performance_score}/100) (+5)")

    if lead.gbp_fotos_anzahl == 0:
        score += 5
        gruende.append("Kein Foto im Google-Unternehmensprofil (+5)")

    if lead.google_bewertungen >= 50:
        score += 10
        gruende.append(f"Viele Bewertungen ({lead.google_bewertungen}) (+10)")
    elif lead.google_bewertungen >= 20:
        score += 5
        gruende.append(f"Gute Aktivität ({lead.google_bewertungen} Bewertungen) (+5)")

    if lead.google_rating >= 4.5:
        score += 5
        gruende.append("Sehr gute Bewertung (+5)")

    if lead.telefon:
        score += 5
        gruende.append("Telefon bekannt (+5)")

    return min(score, 100), gruende


# ─────────────────────────────────────────────
# EINZELNEN PLACE → LEAD VERARBEITEN (Thread-safe)
# ─────────────────────────────────────────────
def verarbeite_place(place: dict, branche: str, api_key: str, bisheriger_status: dict) -> Lead:
    """Wird pro Betrieb in einem Worker-Thread aufgerufen."""
    name = place.get("name", "Unbekannt")
    place_id = place.get("place_id", "")

    # Details nachladen falls nötig (echter API-Key, kein Telefon im Rohdatensatz)
    if api_key and api_key != "GOOGLE_PLACES_API_KEY" and not place.get("formatted_phone_number"):
        details = extrahiere_place_details(place_id, api_key)
        place = {**place, **details}  # nicht in-place mutieren — thread-safe

    website_url = place.get("website", "")

    # fotos ist None wenn das Feld nie abgefragt wurde (Demo-Modus / kein API-Key),
    # und eine leere Liste wenn Google es abgefragt und kein Foto gefunden hat —
    # dieser Unterschied ist scoring-relevant, siehe berechne_score().
    fotos = place.get("photos")
    kategorien = place.get("types") or []

    lead = Lead(
        name=name,
        branche=branche,
        telefon=place.get("formatted_phone_number", ""),
        adresse=place.get("formatted_address", ""),
        website=website_url,
        google_place_id=place_id,
        google_rating=place.get("rating", 0.0),
        google_bewertungen=place.get("user_ratings_total", 0),
        gbp_fotos_anzahl=len(fotos) if fotos is not None else None,
        gbp_kategorien=", ".join(kategorien[:5]),
    )

    if website_url:
        lead.hat_website = True
        analyse = analysiere_website(website_url)
        lead.website_erreichbar = analyse["erreichbar"]
        lead.hat_ssl = analyse["hat_ssl"]
        lead.ladezeit_ms = analyse["ladezeit_ms"]
        lead.wayback_datum = analyse["wayback_datum"]
        lead.website_alter_jahre = analyse["alter_jahre"]
        lead.ist_mobilfreundlich = analyse["ist_mobilfreundlich"]
        lead.seo_titel_vorhanden = analyse["seo_titel_vorhanden"]
        lead.seo_meta_beschreibung_vorhanden = analyse["seo_meta_beschreibung_vorhanden"]
        lead.seo_noindex = analyse["seo_noindex"]
        lead.seo_thin_content = analyse["seo_thin_content"]
        lead.seo_impressum_vorhanden = analyse["seo_impressum_vorhanden"]
        lead.seo_datenschutz_vorhanden = analyse["seo_datenschutz_vorhanden"]
        lead.seo_strukturierte_daten_vorhanden = analyse["seo_strukturierte_daten_vorhanden"]
        lead.hat_kontaktmoeglichkeit = analyse["hat_kontaktmoeglichkeit"]
        lead.website_baukasten = analyse["website_baukasten"]
        lead.pagespeed_seo_score = analyse["pagespeed_seo_score"]
        lead.pagespeed_performance_score = analyse["pagespeed_performance_score"]
        lead.email = analyse["email"]
    else:
        lead.hat_website = False

    # Anruf-Fortschritt über Rescans hinweg erhalten — google_place_id ist über
    # Google-Läufe hinweg stabil, anders als der Zeilenindex in der CSV.
    if lead.google_place_id in bisheriger_status:
        lead.status = bisheriger_status[lead.google_place_id]

    lead.score, lead.score_gruende = berechne_score(lead)
    return lead


# ─────────────────────────────────────────────
# HAUPT-PIPELINE
# ─────────────────────────────────────────────
def verarbeite_branche(branche: str, stadt: str, api_key: str, bisheriger_status: dict) -> list[Lead]:
    """Führt die komplette Pipeline für eine Branche aus — Betriebe parallel."""
    print(f"\n{'='*50}")
    print(f"  Branche: {branche} in {stadt}")
    print(f"{'='*50}")

    rohdaten = suche_google_places(branche, stadt, api_key)
    gesamt = len(rohdaten)
    print(f"  → {gesamt} Betriebe gefunden — starte {min(MAX_WORKERS, gesamt)} parallele Checks")

    leads: list[Lead] = []
    fertig = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(verarbeite_place, place, branche, api_key, bisheriger_status): place.get("name", "?")
            for place in rohdaten
        }

        for future in as_completed(futures):
            name = futures[future]
            fertig += 1
            try:
                lead = future.result()
                leads.append(lead)
                hitze = "🔥" if lead.score >= HOT_LEAD_SCORE else ("⚠" if lead.score >= 40 else "❄")
                website_info = lead.website[:40] if lead.hat_website else "keine Website"
                print(f"  [{fertig:>2}/{gesamt}] {hitze} {lead.score:>3}/100  {name[:35]:<35}  {website_info}")
            except Exception as e:
                print(f"  [{fertig:>2}/{gesamt}] ✗ Fehler bei '{name}': {e}")

    return leads


def speichere_leads(alle_leads: list[Lead]):
    """Speichert Leads als CSV und JSON."""
    alle_leads.sort(key=lambda x: x.score, reverse=True)

    if alle_leads:
        fieldnames = list(asdict(alle_leads[0]).keys())
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for lead in alle_leads:
                row = asdict(lead)
                row["score_gruende"] = " | ".join(row["score_gruende"])
                writer.writerow(row)
        print(f"\n✅ CSV gespeichert: {OUTPUT_FILE}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump([asdict(l) for l in alle_leads], f, ensure_ascii=False, indent=2)
    print(f"✅ JSON gespeichert: {OUTPUT_JSON}")


def lade_vorherigen_status(pfad: str) -> dict:
    """Liest 'status' (z.B. 'angerufen') aus einer vorhandenen Output-CSV, damit ein
    Rescan (neuer Scraper-Lauf oder geänderte Branchen) den Anruf-Fortschritt nicht
    überschreibt. Keyed nach google_place_id, da dieser über Läufe hinweg stabil ist —
    anders als der Zeilenindex oder die Reihenfolge in der CSV."""
    status_map: dict = {}
    if not os.path.exists(pfad):
        return status_map
    try:
        with open(pfad, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                place_id = row.get("google_place_id", "")
                status = row.get("status", "")
                if place_id and status and status != "neu":
                    status_map[place_id] = status
    except Exception as e:
        print(f"  ⚠ Konnte vorherigen Status nicht laden ({pfad}): {e}")
    return status_map


def drucke_zusammenfassung(alle_leads: list[Lead]):
    heiss = [l for l in alle_leads if l.score >= HOT_LEAD_SCORE]
    ohne_website = [l for l in alle_leads if not l.hat_website]
    alte_website = [l for l in alle_leads if l.hat_website and l.website_alter_jahre and l.website_alter_jahre >= 5]
    noindex = [l for l in alle_leads if l.seo_noindex]

    print("\n" + "=" * 60)
    print("  ZUSAMMENFASSUNG")
    print("=" * 60)
    print(f"  Gesamt gefundene Betriebe:    {len(alle_leads)}")
    print(f"  🔥 Heiße Leads (Score≥{HOT_LEAD_SCORE}):   {len(heiss)}")
    print(f"  🌐 Ohne Website:               {len(ohne_website)}")
    print(f"  🕰  Website älter als 5 Jahre:  {len(alte_website)}")
    print(f"  🚫 Bei Google ausgeschlossen (noindex): {len(noindex)}")
    print()
    print("  TOP 5 LEADS:")
    print("  " + "-" * 56)
    for i, lead in enumerate(alle_leads[:5], 1):
        print(f"  {i}. {lead.name[:35]:<35} Score: {lead.score:>3}/100")
        print(f"     {lead.branche} | {lead.adresse[:40]}")
        print(f"     Tel: {lead.telefon or 'nicht bekannt'}")
        print()


# ─────────────────────────────────────────────
# EINSTIEGSPUNKT
# ─────────────────────────────────────────────
def main():
    print("\n" + "🔍 " * 20)
    print("  HANNOVER HANDWERKER LEAD FINDER  v2 — concurrent")
    print("  " + datetime.now().strftime("%d.%m.%Y %H:%M"))
    print("🔍 " * 20)

    api_key = GOOGLE_PLACES_API_KEY
    if not api_key or api_key == "GOOGLE_PLACES_API_KEY":
        print("\n⚠  HINWEIS: Kein Google API-Key gesetzt.")
        print("   Setze die Umgebungsvariable GOOGLE_PLACES_API_KEY")
        print("   Demo-Modus aktiv — realistische Testdaten werden verwendet.\n")

    bisheriger_status = lade_vorherigen_status(OUTPUT_FILE)
    if bisheriger_status:
        print(f"  ℹ  {len(bisheriger_status)} Anruf-Status aus vorhandener {OUTPUT_FILE} übernommen\n")

    alle_leads: list[Lead] = []
    try:
        for branche in BRANCHEN:
            t0 = time.time()
            leads = verarbeite_branche(branche, ZIELSTADT, api_key, bisheriger_status)
            alle_leads.extend(leads)
            speichere_leads(alle_leads)
            elapsed = time.time() - t0
            print(f"  💾 Zwischenstand: {len(alle_leads)} Leads  ({elapsed:.1f}s für diese Branche)")
    except KeyboardInterrupt:
        print("\n⚠ Abgebrochen — speichere bisherige Ergebnisse...")
        speichere_leads(alle_leads)
        print(f"✅ {len(alle_leads)} Leads gesichert.")

    drucke_zusammenfassung(alle_leads)

    print("\n✅ Fertig! Nächste Schritte:")
    print("   1. leads_hannover.csv in Airtable importieren")
    print("   2. Heiße Leads (Score ≥ 70) zuerst kontaktieren")
    print("   3. Cold-Email-Generator für die Top-Leads starten")


if __name__ == "__main__":
    main()