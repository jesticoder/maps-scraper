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
OUTPUT_FILE = "leads_hannover.csv"
OUTPUT_JSON = "leads_hannover.json"

# Score-Schwelle: Leads mit Score >= X gelten als "heiß"
HOT_LEAD_SCORE = 70

# Anzahl gleichzeitiger Website-Checks pro Branche
MAX_WORKERS = 10


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

    # Google-Daten
    google_place_id: str = ""
    google_rating: float = 0.0
    google_bewertungen: int = 0

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
    """Holt Detailinfos (Telefon, Website) für einen Place."""
    if not api_key or api_key == "GOOGLE_PLACES_API_KEY" or place_id.startswith("DEMO_"):
        return {}

    fields = "formatted_phone_number,website,formatted_address"
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


# ─────────────────────────────────────────────
# WEBSITE-ANALYSE  (läuft im Thread-Pool)
# ─────────────────────────────────────────────
def analysiere_website(url: str) -> dict:
    """Prüft ob Website existiert, wie alt sie ist und Qualitätsfaktoren.
    Wayback-Request läuft parallel zum HTTP-Check via inner ThreadPoolExecutor."""
    result = {
        "erreichbar": False,
        "hat_ssl": False,
        "ladezeit_ms": None,
        "wayback_datum": "",
        "alter_jahre": None,
        "ist_mobilfreundlich": None,
        "fehler": "",
    }

    if not url:
        return result

    result["hat_ssl"] = url.startswith("https://")
    domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]

    # ── HTTP-Check und Wayback gleichzeitig starten ──────────────────────────
    def _http_check():
        r = {"erreichbar": False, "ladezeit_ms": None, "ist_mobilfreundlich": None, "fehler": ""}
        try:
            start = time.time()
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                inhalt = resp.read(50000).decode("utf-8", errors="ignore")
                r["erreichbar"] = resp.status == 200
                r["ladezeit_ms"] = int((time.time() - start) * 1000)
                r["ist_mobilfreundlich"] = "viewport" in inhalt.lower()
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

    with ThreadPoolExecutor(max_workers=2) as inner:
        f_http = inner.submit(_http_check)
        f_wb = inner.submit(_wayback_check)
        http_r = f_http.result()
        wb_r = f_wb.result()

    result.update(http_r)
    result.update(wb_r)
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
        if lead.website_alter_jahre and lead.website_alter_jahre >= 8:
            score += 25
            gruende.append(f"Website sehr alt ({lead.website_alter_jahre} Jahre) (+25)")
        elif lead.website_alter_jahre and lead.website_alter_jahre >= 5:
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
def verarbeite_place(place: dict, branche: str, api_key: str) -> Lead:
    """Wird pro Betrieb in einem Worker-Thread aufgerufen."""
    name = place.get("name", "Unbekannt")
    place_id = place.get("place_id", "")

    # Details nachladen falls nötig (echter API-Key, kein Telefon im Rohdatensatz)
    if api_key and api_key != "GOOGLE_PLACES_API_KEY" and not place.get("formatted_phone_number"):
        details = extrahiere_place_details(place_id, api_key)
        place = {**place, **details}  # nicht in-place mutieren — thread-safe

    website_url = place.get("website", "")

    lead = Lead(
        name=name,
        branche=branche,
        telefon=place.get("formatted_phone_number", ""),
        adresse=place.get("formatted_address", ""),
        website=website_url,
        google_place_id=place_id,
        google_rating=place.get("rating", 0.0),
        google_bewertungen=place.get("user_ratings_total", 0),
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
    else:
        lead.hat_website = False

    lead.score, lead.score_gruende = berechne_score(lead)
    return lead


# ─────────────────────────────────────────────
# HAUPT-PIPELINE
# ─────────────────────────────────────────────
def verarbeite_branche(branche: str, stadt: str, api_key: str) -> list[Lead]:
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
            pool.submit(verarbeite_place, place, branche, api_key): place.get("name", "?")
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


def drucke_zusammenfassung(alle_leads: list[Lead]):
    heiss = [l for l in alle_leads if l.score >= HOT_LEAD_SCORE]
    ohne_website = [l for l in alle_leads if not l.hat_website]
    alte_website = [l for l in alle_leads if l.hat_website and l.website_alter_jahre and l.website_alter_jahre >= 5]

    print("\n" + "=" * 60)
    print("  ZUSAMMENFASSUNG")
    print("=" * 60)
    print(f"  Gesamt gefundene Betriebe:    {len(alle_leads)}")
    print(f"  🔥 Heiße Leads (Score≥{HOT_LEAD_SCORE}):   {len(heiss)}")
    print(f"  🌐 Ohne Website:               {len(ohne_website)}")
    print(f"  🕰  Website älter als 5 Jahre:  {len(alte_website)}")
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

    alle_leads: list[Lead] = []
    try:
        for branche in BRANCHEN:
            t0 = time.time()
            leads = verarbeite_branche(branche, ZIELSTADT, api_key)
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