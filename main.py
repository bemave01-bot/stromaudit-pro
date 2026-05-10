"""
StromAudit Pro – Apify Actor (On-Demand Rapport)
Deutsche Energie-Compliance & ESG Pre-Audit Engine
Version 3.2 | 2026

Verbeteringen v3.1:
  - Rapport-hash (SHA-256, manipulatiebeveiliging)
  - HTML-escape alle user-inputs (XSS-preventie)
  - Marktprijs-caching (15 minuten, KV-Store)
  - Circuit-breaker SMARD (na 3 fouten direct fallback)
  - Configureerbare tarieven (CONFIG-dict)
  - Uitgebreide rapport-metadata (runtime, bronnen, versie)

Verbeteringen v3.5:
  - Punt 1: §9b blok vermeldt expliciet Zoll-Formular 1450
  - Punt 2: CO₂-beprijzing BEHG (€55/t 2026) berekend en getoond in ESG-sectie
  - Punt 3: Day-Ahead veilingtijden (00:00–23:59 uur) in header én voetnoot
  - Punt 4: Term "Hochlastzeitfenster" toegevoegd aan §19 StromNEV-advies
  - Punt 5: Prüfnummer + SHA-256 hash visueel prominent als kader met stempel-icoon

Verbeteringen v3.4:
  - Punt 4: Klikbare HZA-link (zoll.de) in §9b groene box én compliance-checklist
  - Punt 5: Urgentie-trigger ipv vaste datum – "Marktpreise ändern sich täglich"
  - Punt 8: Persoonlijke intro-blok met bedrijfsnaam, PLZ, Bundesland en berichtsjaar

Verbeteringen v3.3:
  - Benchmarking: Arbeitspreis vs. Bundesdurchschnitt vergleichbarer Betriebe (BDEW 2025)
  - Einsparpotenzial-Banner prominent bovenaan het rapport (direct zichtbaar)
  - 5-Jahres-Projektion in sectie 2b Einsparpotenziale
  - BENCHMARK-dict toegevoegd (configureerbaar referentiegegeven)

Verbeteringen v3.2:
  - SMARD timestamp-check verruimd (48u → 168u) zodat actuele data doorkomt
  - KPI-label conditioneel: "Fallback" bij SMARD-storing, "SMARD live" bij live data
  - Datenalter bij fallback: "Nicht verfügbar (Fallback aktiv)"
  - Fallback-prijs in rapport: Duitse notatie 89,30 EUR/MWh
  - Page-break-inside: avoid op tabellen, KPI-grid en boxes (print-optimalisatie)
  - Docstring gesynchroniseerd met REPORT_VERSION

Rechtsgrundlagen: EnWG, StromStG §9b, KWKG, StromNEV §19, EEG 2023,
KAV, UStG §12, EU CSRD 2022/2464, ESRS E1, GHG Protocol, ISO 50001:2018,
DIN EN 16247-1, §8 EDL-G, EU Taxonomy Regulation 2020/852

Datenquelle Marktpreise: SMARD – Bundesnetzagentur (öffentliche REST-API,
freie Nachnutzung gemäß DL-DE/BY-2-0).
"""

import asyncio
import hashlib
import html
import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from apify import Actor

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATIE (alle tarieven en parameters op één plek)
# ─────────────────────────────────────────────────────────────────────────────
REPORT_VERSION = "3.5"
TZ_BERLIN      = ZoneInfo("Europe/Berlin")

# SMARD
SMARD_FILTER_DA    = 4169
SMARD_BASE         = "https://www.smard.de/app/chart_data"
SMARD_TIMEOUT_S    = 20
SMARD_MAX_RETRIES  = 3
SMARD_RETRY_DELAY  = 2.0
SMARD_CACHE_KEY    = "smard_cache_v1"
SMARD_CACHE_TTL_S  = 900   # 15 minuten

# Circuit-breaker
CB_FAIL_KEY        = "smard_cb_failures"
CB_MAX_FAILURES    = 3
CB_RESET_AFTER_S   = 300   # 5 minuten

# Tarieven 2026 (ÜNB-Veröffentlichung Oktober 2025)
CONFIG = {
    # Umlagen EUR/kWh
    "kwkg_umlage":           0.00446,
    "offshore_umlage":       0.00941,
    "stromnev19_a":          0.01559,   # ≤ 1 Mio kWh
    "stromnev19_b":          0.00050,   # > 1 Mio kWh
    "stromnev19_c":          0.00025,   # > 1 Mio kWh + prod. Gewerbe
    # Stromsteuer EUR/kWh
    "stromsteuer":           0.02050,   # Regelsatz §3 StromStG
    "stromsteuer_9b":        0.00050,   # §9b Abs.2a prod. Gewerbe
    # MwSt
    "mwst":                  0.19,
    # Konzessionsabgabe EUR/kWh
    "konz_haushalt":         0.0166,    # < 30.000 kWh/Jahr
    "konz_sonder":           0.0011,    # ≥ 30.000 kWh (Sondervertrag)
    # Leistungspreis EUR/kW/Jahr (Ø DE, configureerbaar)
    "leistungspreis_eur_kw": 80.0,
    # Beschaffungmarge (10% op Day-Ahead)
    "beschaffungs_marge":    0.10,
    # CO₂-factor (UBA/IFEU Strommix DE 2025)
    "co2_faktor_g_kwh":      367.0,
    # CO₂-Preis BEHG 2026 (Brennstoffemissionshandelsgesetz)
    "behg_co2_preis_eur_t":  55.0,
    # Fallback-marktprijs als SMARD niet bereikbaar is
    "fallback_marktpreis_eur_mwh": 89.30,
    # Validatiegrenzen
    "min_kwh":               1_000,
    "max_kwh":               100_000_000,
    "min_kw":                0,
    "max_kw":                100_000,
    "min_msb":               0,
    "max_msb":               50_000,
}

# ─────────────────────────────────────────────────────────────────────────────
# BENCHMARK-DATEN (Bundesdurchschnitt vergleichbarer Betriebe)
# Quelle: BDEW Strompreisanalyse 2025 / BNetzA – Gewerbe < 100 MWh/Jahr
# ─────────────────────────────────────────────────────────────────────────────
BENCHMARK = {
    # Bundesdurchschnitt Arbeitspreis (netto, ct/kWh) – Gewerbe < 500 MWh
    "arbeitspreis_ct_kwh":    21.20,
    # Quelle
    "quelle": "BDEW Strompreisanalyse 2025 / BNetzA (Gewerbe ≤ 500 MWh)",
}

GESETZ_REFS = [
    "EnWG (Energiewirtschaftsgesetz)",
    "StromStG §9b (Spitzenausgleich prod. Gewerbe)",
    "KWKG 2016/2020 (Kraft-Wärme-Kopplungsgesetz)",
    "StromNEV §19 Abs.2 (Aufschlag bes. Netznutzung / individuelle Netzentgelte)",
    "EEG 2023 (Erneuerbare-Energien-Gesetz)",
    "KAV §2 (Konzessionsabgabenverordnung)",
    "UStG §12 (Mehrwertsteuer 19%)",
    "MsbG §20 (Messstellenbetriebsgesetz – Messstellenbetrieb)",
    "EU CSRD 2022/2464 / ESRS E1 (Klimawandel, Scope 2)",
    "GHG Protocol Corporate Standard (Scope 1/2/3)",
    "ISO 50001:2018 (Energiemanagementsystem)",
    "DIN EN 16247-1 (Energieaudits)",
    "§8 EDL-G (Energiedienstleistungsgesetz)",
    "EU Taxonomy Regulation 2020/852 Art.8",
    "IFEU/UBA Emissionsfaktoren Strommix Deutschland 2025",
    "BEHG (Brennstoffemissionshandelsgesetz) – CO₂-Preis 2026",
]


# ─────────────────────────────────────────────────────────────────────────────
# DUITSE GETALNOTATIE
# ─────────────────────────────────────────────────────────────────────────────
def de_num(value: float, decimals: int = 2) -> str:
    """125000.5 → '125.000,50' (Deutsche Notation)"""
    s = f"{value:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

def de_eur(v: float) -> str: return de_num(v, 2)
def de_kwh(v: float) -> str: return de_num(v, 0)
def de_ct(v: float, d: int = 4) -> str: return de_num(v, d)


# ─────────────────────────────────────────────────────────────────────────────
# HTML-ESCAPE (XSS-preventie voor alle user-inputs)
# ─────────────────────────────────────────────────────────────────────────────
def esc(value: str) -> str:
    """Escaped alle HTML-speciale tekens in user-inputs."""
    return html.escape(str(value), quote=True)


# ─────────────────────────────────────────────────────────────────────────────
# INPUT VALIDATIE
# ─────────────────────────────────────────────────────────────────────────────
class ValidationError(Exception):
    pass

def validiere_input(inp: dict) -> dict:
    errors   = []
    warnings = []

    # PLZ
    plz_raw = str(inp.get("plz", "")).strip()
    if not plz_raw:
        errors.append("'plz' (Postleitzahl) ist ein Pflichtfeld und fehlt.")
    elif not plz_raw.isdigit() or len(plz_raw) != 5:
        errors.append(f"'plz' muss eine 5-stellige Zahl sein. Eingabe: '{esc(plz_raw)}'")
        plz_raw = "00000"
    plz = plz_raw.zfill(5)

    # Jahresverbrauch
    try:
        kwh = float(str(inp.get("jahresverbrauch_kwh", 0)).strip())
        if kwh <= 0:
            errors.append("'jahresverbrauch_kwh' muss größer als 0 sein.")
        elif kwh < CONFIG["min_kwh"]:
            warnings.append(f"Sehr niedriger Jahresverbrauch ({de_kwh(kwh)} kWh). Bitte prüfen.")
        elif kwh > CONFIG["max_kwh"]:
            errors.append(f"'jahresverbrauch_kwh' unrealistisch hoch ({de_kwh(kwh)} kWh).")
    except (TypeError, ValueError):
        errors.append("'jahresverbrauch_kwh' muss eine Zahl sein.")
        kwh = 0.0

    # Spitzenlast
    try:
        kw = float(str(inp.get("spitzenlast_kw", 0)).strip())
        if kw < 0:
            warnings.append("'spitzenlast_kw' war negativ – auf 0 korrigiert.")
            kw = 0.0
        elif kw > CONFIG["max_kw"]:
            errors.append(f"'spitzenlast_kw' unrealistisch hoch ({de_num(kw,1)} kW).")
        if kw == 0:
            warnings.append("'spitzenlast_kw' ist 0 kW – Leistungskosten werden mit 0 berechnet.")
        # Plausibilitätscheck: Volllaststunden
        if kw > 0 and kwh > 0:
            vls = kwh / kw
            if vls < 100:
                warnings.append(
                    f"Niedriges Verhältnis kWh/kW ({de_num(vls,0)} Volllaststunden). "
                    "Bitte Spitzenlast prüfen – Schätzwerte beeinflussen §19-StromNEV erheblich."
                )
    except (TypeError, ValueError):
        errors.append("'spitzenlast_kw' muss eine Zahl sein.")
        kw = 0.0

    # Messstellenbetrieb
    try:
        msb = float(str(inp.get("messstellenbetrieb_eur", 250.0)).strip())
        if msb < 0:
            warnings.append("'messstellenbetrieb_eur' war negativ – auf 0 korrigiert.")
            msb = 0.0
        elif msb == 0:
            warnings.append("'messstellenbetrieb_eur' ist 0 € – bitte prüfen (Standardwert: 250 €).")
        elif msb > CONFIG["max_msb"]:
            warnings.append(f"'messstellenbetrieb_eur' erscheint sehr hoch ({de_eur(msb)} €).")
    except (TypeError, ValueError):
        warnings.append("'messstellenbetrieb_eur' ungültig – Standardwert 250,00 € wird verwendet.")
        msb = 250.0

    # §9b validatie: alleen geldig als bedrijf expliciet prod. Gewerbe is
    is_producing = bool(inp.get("is_producing", False))

    # Strings – gesaniteerd via esc() bij gebruik in HTML
    unternehmen      = str(inp.get("unternehmen",      "")).strip() or "Nicht angegeben"
    anschrift        = str(inp.get("anschrift",        "")).strip() or "Nicht angegeben"
    projekt_referenz = str(inp.get("projekt_referenz", "")).strip() or "—"

    # Berichtsjahr
    try:
        berichtsjahr = int(str(inp.get("berichtsjahr", datetime.now().year)).strip())
        if not (2015 <= berichtsjahr <= 2035):
            warnings.append(f"'berichtsjahr' {berichtsjahr} außerhalb 2015–2035 – aktuelles Jahr verwendet.")
            berichtsjahr = datetime.now().year
    except (TypeError, ValueError):
        warnings.append("'berichtsjahr' ungültig – aktuelles Jahr wird verwendet.")
        berichtsjahr = datetime.now().year

    if errors:
        raise ValidationError("\n".join(errors))

    return {
        "plz":              plz,
        "jahresverbrauch_kwh":  kwh,
        "spitzenlast_kw":       kw,
        "messstellenbetrieb_eur": msb,
        "is_producing":         is_producing,
        "unternehmen":          unternehmen,
        "anschrift":            anschrift,
        "projekt_referenz":     projekt_referenz,
        "berichtsjahr":         berichtsjahr,
        "_warnings":            warnings,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PLZ-LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
def load_plz_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "plz_data.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_netz_info(plz: str, plz_db: dict) -> dict:
    info = plz_db.get(plz[:2], plz_db.get("default", {
        "operator":  "Bundesweiter Durchschnitt (Fallback)",
        "net_var":   0.0950,
        "konz_abg":  0.0150,
        "bundesland": "Deutschland",
    }))
    return info


# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT-BREAKER SMARD
# ─────────────────────────────────────────────────────────────────────────────
class CircuitBreaker:
    """
    Na CB_MAX_FAILURES opeenvolgende SMARD-fouten wordt de circuit-breaker
    geopend en slaat de actor direct de fallback in voor CB_RESET_AFTER_S seconden.
    """
    def __init__(self):
        self._failures  = 0
        self._open_at   = None

    def is_open(self) -> bool:
        if self._open_at is None:
            return False
        if time.monotonic() - self._open_at > CB_RESET_AFTER_S:
            Actor.log.info("Circuit-breaker SMARD: reset na cooldown.")
            self._failures = 0
            self._open_at  = None
            return False
        return True

    def record_failure(self):
        self._failures += 1
        Actor.log.warning(f"Circuit-breaker SMARD: fout {self._failures}/{CB_MAX_FAILURES}")
        if self._failures >= CB_MAX_FAILURES:
            self._open_at = time.monotonic()
            Actor.log.error("Circuit-breaker SMARD: OPEN – directe fallback voor 5 minuten.")

    def record_success(self):
        self._failures = 0
        self._open_at  = None

_smard_cb = CircuitBreaker()


# ─────────────────────────────────────────────────────────────────────────────
# SMARD LIVE MARKTPREIS MET CACHING (Bundesnetzagentur – publieke REST-API)
# ─────────────────────────────────────────────────────────────────────────────
async def get_smard_price() -> dict:
    """
    1. Circuit-breaker check
    2. Cache check (Apify KV-Store, TTL 15 min)
    3. SMARD API (3 retries)
    4. Fallback
    """
    fallback = {
        "price_eur_kwh":    CONFIG["fallback_marktpreis_eur_mwh"] / 1000,
        "avg_7d_eur_kwh":   CONFIG["fallback_marktpreis_eur_mwh"] / 1000,
        "raw_eur_mwh":      CONFIG["fallback_marktpreis_eur_mwh"],
        "timestamp_utc":    datetime.now(timezone.utc).isoformat(),
        "timestamp_berlin": datetime.now(TZ_BERLIN).strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)"),
        "source":           f"Fallback (Ø DE 2025: {de_num(CONFIG['fallback_marktpreis_eur_mwh'],2)} EUR/MWh) – SMARD nicht erreichbar",
        "is_fallback":      True,
        "data_points_7d":   0,
        "cached":           False,
    }

    # ── 1. Circuit-breaker ──────────────────────────────────────────────────
    if _smard_cb.is_open():
        Actor.log.warning("Circuit-breaker open – SMARD overgeslagen, fallback gebruikt.")
        return fallback

    # ── 2. Cache check ──────────────────────────────────────────────────────
    try:
        cached = await Actor.get_value(SMARD_CACHE_KEY)
        if cached:
            age_s = time.time() - cached.get("cached_at", 0)
            if age_s < SMARD_CACHE_TTL_S:
                Actor.log.info(f"SMARD cache hit (leeftijd: {age_s:.0f}s)")
                cached["cached"] = True
                return cached
            else:
                Actor.log.info(f"SMARD cache verlopen ({age_s:.0f}s) – nieuw ophalen.")
    except Exception as e:
        Actor.log.warning(f"Cache lezen mislukt: {e}")

    # ── 3. SMARD API met retries ────────────────────────────────────────────
    headers = {
        "User-Agent": "StromAudit-Pro/3.1 (Energie-Compliance-Tool; Apify Store)",
        "Accept":     "application/json",
    }

    for attempt in range(1, SMARD_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(SMARD_TIMEOUT_S),
                headers=headers,
                follow_redirects=True,
            ) as client:
                # Index ophalen
                r_idx = await client.get(f"{SMARD_BASE}/{SMARD_FILTER_DA}/DE/index_hour.json")
                r_idx.raise_for_status()
                timestamps = sorted(r_idx.json().get("timestamps", []))
                if not timestamps:
                    raise ValueError("Geen timestamps in SMARD-index.")

                # Timestamp van index (kan 3-4 dagen oud zijn - normaal voor SMARD)
                latest_ts_ms = timestamps[-1]
                latest_dt    = datetime.fromtimestamp(latest_ts_ms / 1000, tz=timezone.utc)
                age_hours    = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600
                if age_hours > 168:  # Max 7 dagen - daarna echt stale
                    raise ValueError(f"SMARD-index te oud: {age_hours:.0f} uur")

                # Data-blok ophalen
                r_data = await client.get(
                    f"{SMARD_BASE}/{SMARD_FILTER_DA}/DE/"
                    f"{SMARD_FILTER_DA}_DE_hour_{latest_ts_ms}.json"
                )
                r_data.raise_for_status()
                series = r_data.json().get("series", [])
                valid  = [(ts, v) for ts, v in series if v is not None]
                if not valid:
                    raise ValueError("Geen geldige prijsdata in SMARD-serie.")

                last_ts_ms, last_price_mwh = valid[-1]
                price_kwh  = round(last_price_mwh / 1000, 5)
                price_dt   = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)

                # 7-daags gemiddelde
                vals_7d    = [v for _, v in valid[-168:]]
                avg_7d_kwh = round(sum(vals_7d) / len(vals_7d) / 1000, 5) if vals_7d else price_kwh

                result = {
                    "price_eur_kwh":    price_kwh,
                    "avg_7d_eur_kwh":   avg_7d_kwh,
                    "raw_eur_mwh":      last_price_mwh,
                    "timestamp_utc":    price_dt.isoformat(),
                    "timestamp_berlin": price_dt.astimezone(TZ_BERLIN).strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)"),
                    "source":           "SMARD – Bundesnetzagentur (EPEX Spot Day-Ahead DE)",
                    "is_fallback":      False,
                    "data_points_7d":   len(vals_7d),
                    "cached":           False,
                    "cached_at":        time.time(),
                    "data_age_hours":   round(age_hours, 1),
                }

                # Opslaan in cache
                try:
                    await Actor.set_value(SMARD_CACHE_KEY, result)
                except Exception:
                    pass

                _smard_cb.record_success()
                Actor.log.info(f"SMARD OK: {last_price_mwh:.2f} EUR/MWh (poging {attempt})")
                return result

        except Exception as e:
            Actor.log.warning(f"SMARD poging {attempt}/{SMARD_MAX_RETRIES}: {e}")
            _smard_cb.record_failure()
            if attempt < SMARD_MAX_RETRIES:
                await asyncio.sleep(SMARD_RETRY_DELAY)

    Actor.log.error("SMARD alle pogingen mislukt – fallback wordt gebruikt.")
    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# BERECHNUNGSMOTOR
# ─────────────────────────────────────────────────────────────────────────────
def berechne_stromkosten(data: dict, marktpreis_eur_kwh: float, netz_info: dict) -> dict:
    c    = CONFIG
    kwh  = data["jahresverbrauch_kwh"]
    kw   = data["spitzenlast_kw"]
    msb  = data["messstellenbetrieb_eur"]
    prod = data["is_producing"]

    net_var  = netz_info["net_var"]
    konz_abg = netz_info["konz_abg"] if kwh < 30_000 else c["konz_sonder"]

    # §19 StromNEV – categorie automatisch bepalen
    if kwh > 1_000_000 and prod:
        stromnev, stromnev_kat = c["stromnev19_c"], "C (prod. Gewerbe, >1 Mio kWh)"
    elif kwh > 1_000_000:
        stromnev, stromnev_kat = c["stromnev19_b"], "B (>1 Mio kWh)"
    else:
        stromnev, stromnev_kat = c["stromnev19_a"], "A (≤1 Mio kWh)"

    # §9b – validatie: alleen toepassen bij prod. Gewerbe
    stromsteuer   = c["stromsteuer_9b"] if prod else c["stromsteuer"]
    entlastung_9b = (c["stromsteuer"] - stromsteuer) * kwh if prod else 0.0

    # Beschaffung inclusief configureerbare marge
    beschaffung        = marktpreis_eur_kwh * (1 + c["beschaffungs_marge"])
    arbeitspreis_netto = (
        beschaffung + net_var + konz_abg
        + c["kwkg_umlage"] + c["offshore_umlage"]
        + stromnev + stromsteuer
    )

    jahresarbeit_netto = arbeitspreis_netto * kwh
    leistungskosten    = kw * c["leistungspreis_eur_kw"]
    netto_gesamt       = jahresarbeit_netto + leistungskosten + msb
    mwst_betrag        = netto_gesamt * c["mwst"]
    brutto_gesamt      = netto_gesamt + mwst_betrag

    aufsch = {
        "beschaffung_vertrieb": round(beschaffung * kwh, 2),
        "netzentgelt_variabel": round(net_var * kwh, 2),
        "konzessionsabgabe":    round(konz_abg * kwh, 2),
        "kwkg_umlage":          round(c["kwkg_umlage"] * kwh, 2),
        "offshore_umlage":      round(c["offshore_umlage"] * kwh, 2),
        "stromnev_aufschlag":   round(stromnev * kwh, 2),
        "stromsteuer":          round(stromsteuer * kwh, 2),
        "leistungskosten":      round(leistungskosten, 2),
        "messstellenbetrieb":   round(msb, 2),
        "mwst_19pct":           round(mwst_betrag, 2),
    }
    anteile = {
        k: round(v / brutto_gesamt * 100, 2) if brutto_gesamt else 0
        for k, v in aufsch.items()
    }

    return {
        "arbeitspreis_netto_eur_kwh":   round(arbeitspreis_netto, 5),
        "jahresarbeitspreis_netto_eur": round(jahresarbeit_netto, 2),
        "leistungskosten_netto_eur":    round(leistungskosten, 2),
        "messstellenbetrieb_eur":       round(msb, 2),
        "netto_gesamt_eur":             round(netto_gesamt, 2),
        "mwst_eur":                     round(mwst_betrag, 2),
        "brutto_gesamt_eur":            round(brutto_gesamt, 2),
        "stromsteuer_entlastung_9b_eur": round(entlastung_9b, 2),
        "stromnev_kategorie":           stromnev_kat,
        "aufschluesselung":             aufsch,
        "anteile_pct":                  anteile,
        "tarife": {
            "marktpreis_eur_kwh":       round(marktpreis_eur_kwh, 5),
            "beschaffung_inkl_marge":   round(beschaffung, 5),
            "marge_pct":                round(c["beschaffungs_marge"] * 100, 1),
            "netzentgelt_variabel":     net_var,
            "konzessionsabgabe":        konz_abg,
            "kwkg_umlage":              c["kwkg_umlage"],
            "offshore_umlage":          c["offshore_umlage"],
            "stromnev19_umlage":        stromnev,
            "stromsteuer":              stromsteuer,
            "leistungspreis_eur_kw":    c["leistungspreis_eur_kw"],
            "mwst_satz":                c["mwst"],
        },
    }


def berechne_esg(kwh: float) -> dict:
    co2_kg    = round(kwh * CONFIG["co2_faktor_g_kwh"] / 1000, 1)
    co2_t     = round(co2_kg / 1000, 3)
    intensity = round(CONFIG["co2_faktor_g_kwh"] / 1000, 3)
    behg_preis_eur_t = CONFIG["behg_co2_preis_eur_t"]
    behg_kosten_eur  = round(co2_t * behg_preis_eur_t, 2)
    return {
        "scope":                     "Scope 2 – Location-based (GHG Protocol Corporate Standard)",
        "norm":                      "ESRS E1 / ISO 14064-1 / GHG Protocol",
        "emissionsfaktor_g_co2_kwh": CONFIG["co2_faktor_g_kwh"],
        "quelle":                    "IFEU / Umweltbundesamt (UBA) – Strommix Deutschland 2025",
        "co2_footprint_kg":          co2_kg,
        "co2_footprint_tonnen":      co2_t,
        "intensitaetsrate_t_co2_mwh": intensity,
        "csrd_relevant":             True,
        "esrs_datenpunkte":          [
            "E1-4 (Energieverbrauch)",
            "E1-5 (Scope-2-Emissionen)",
            "E1-6 (Intensitätsrate)",
        ],
        "eu_taxonomy": "Zu prüfen (Art. 8 EU-Taxonomie-VO 2020/852)",
        "behg_co2_preis_eur_t":    behg_preis_eur_t,
        "behg_co2_kosten_eur":     behg_kosten_eur,
    }


def pruefe_compliance(kwh: float, kw: float, prod: bool) -> dict:
    heute      = datetime.now(TZ_BERLIN)
    checks     = []
    next_steps = []

    edlg = kwh > 100_000
    checks.append({
        "norm":       "§8 EDL-G",
        "titel":      "Energieaudit-Pflicht (alle 4 Jahre, Nicht-KMU)",
        "relevant":   edlg,
        "status":     "⚠️ Handlungsbedarf: Audit nach DIN EN 16247-1 einleiten" if edlg else "○ Nicht betroffen (Schwellenwert nicht erreicht)",
        "empfehlung": "DIN EN 16247-1 Audit beauftragen" if edlg else None,
    })
    if edlg:
        next_steps.append({"aktion": "Energieaudit-Angebot einholen (DIN EN 16247-1)", "frist": "Innerhalb 6 Monate"})

    iso = kwh > 500_000
    checks.append({
        "norm":       "ISO 50001:2018",
        "titel":      "Energiemanagementsystem",
        "relevant":   iso,
        "status":     "⚠️ Stark empfohlen (>500.000 kWh)" if iso else "○ Empfohlen",
        "empfehlung": "EnMS nach ISO 50001 implementieren" if iso else None,
    })

    checks.append({
        "norm":       "§9b StromStG",
        "titel":      "Stromsteuervergünstigung Produzierendes Gewerbe",
        "relevant":   prod,
        "status":     "✅ Angewendet – Entlastungsbetrag berechnet" if prod else "○ Nicht aktiviert – prüfen ob §9b anwendbar",
        "empfehlung": "Antrag beim Hauptzollamt (HZA) auf Jahresausgleich stellen" if prod else "Beim Steuerberater prüfen ob §9b anwendbar",
    })
    if prod:
        frist_9b = f"31.12.{heute.year}"
        next_steps.append({"aktion": "§9b StromStG Jahresausgleich beim zuständigen HZA beantragen", "frist": f"Bis {frist_9b}"})

    stromnev_basis = kw >= 30 and kwh >= 30_000
    vls = kwh / kw if kw > 0 else 0
    stromnev_vls   = vls >= 7000  # §19 Abs.2 Satz 1 StromNEV: ≥7000 Volllaststunden
    stromnev_status = (
        "⚠️ Grundvoraussetzungen (≥30 kW, ≥30.000 kWh) erfüllt – jedoch Benutzungsdauer "
        f"(ca. {de_num(vls,0)} Volllaststunden) unter 7.000 h/Jahr: §19 Abs.2 Satz 1 nur bei "
        "atypischer Netznutzung (§19 Abs.2 Satz 2) anwendbar. Netzbetreiber prüfen lassen."
        if stromnev_basis and not stromnev_vls else
        "⚠️ Alle Voraussetzungen §19 Abs.2 Satz 1 erfüllt – Antrag beim Netzbetreiber empfohlen"
        if stromnev_basis and stromnev_vls else
        "○ Schwellenwert nicht erreicht (< 30 kW oder < 30.000 kWh)"
    )
    checks.append({
        "norm":       "§19 Abs.2 StromNEV",
        "titel":      "Individuelle Netzentgelte (ab 30 kW + 30.000 kWh + ≥7.000 Volllaststunden)",
        "relevant":   stromnev_basis,
        "status":     stromnev_status,
        "empfehlung": (
            "Antrag auf individuelle Netzentgelte beim Netzbetreiber stellen (§19 Abs.2 Satz 1 StromNEV)"
            if stromnev_basis and stromnev_vls else
            "Netzbetreiber kontaktieren zur Prüfung atypischer Netznutzung (§19 Abs.2 Satz 2 StromNEV) – kein Standardantrag möglich bei < 7.000 Volllaststunden"
            if stromnev_basis and not stromnev_vls else None
        ),   
    })     
    if stromnev_basis and stromnev_vls:
       next_steps.append({"aktion": "Antrag individuelle Netzentgelte beim Netzbetreiber einreichen", "frist": "Nächstes Quartal"})

    csrd = kwh > 500_000
    checks.append({
        "norm":       "EU CSRD / ESRS E1",
        "titel":      "Nachhaltigkeitsberichterstattung Klimawandel",
        "relevant":   True,
        "status":     "✅ Scope-2-Daten bereitgestellt" if csrd else "○ Freiwillige Nutzung empfohlen",
        "empfehlung": "Scope-2-Emissionen in ESRS E1 Nachhaltigkeitsbericht integrieren",
    })
    if csrd:
        next_steps.append({"aktion": "Scope-2-Emissionen in CSRD/ESRS E1 Bericht aufnehmen", "frist": f"Berichtsjahr {heute.year + 1}"})

    checks.append({
        "norm":       "§2 KAV",
        "titel":      "Konzessionsabgabe Sondervertragskunden",
        "relevant":   stromnev_basis,
        "status":     "✅ Sondervertragstarif (0,11 ct/kWh) angewendet" if stromnev_basis else "○ Haushaltstarif-Satz angewendet",
        "empfehlung": None,
    })

    score = min(100, sum(20 for c in checks if c["relevant"]))
    return {"checks": checks, "next_steps": next_steps, "compliance_score": score}


# ─────────────────────────────────────────────────────────────────────────────
# RAPPORT-HASH (SHA-256, manipulatiebeveiliging)
# ─────────────────────────────────────────────────────────────────────────────
def bereken_rapport_hash(data: dict, kalk: dict, esg: dict, pruf_nr: str) -> str:
    """
    Berekent een SHA-256 hash van de kerngegevens van het rapport.
    De hash verandert als invoer of berekeningen worden gemanipuleerd.
    Dient als Manipulationsschutz voor audit-doeleinden.
    """
    kern = {
        "pruefnummer":      pruf_nr,
        "plz":              data["plz"],
        "kwh":              data["jahresverbrauch_kwh"],
        "kw":               data["spitzenlast_kw"],
        "brutto":           kalk["brutto_gesamt_eur"],
        "netto":            kalk["netto_gesamt_eur"],
        "co2_kg":           esg["co2_footprint_kg"],
        "is_producing":     data["is_producing"],
        "berichtsjahr":     data["berichtsjahr"],
    }
    kern_str = json.dumps(kern, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(kern_str.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# HTML RAPPORT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def generiere_html(
    data: dict, markt: dict, netz: dict,
    kalk: dict, esg: dict, comp: dict,
    pruf_nr: str, rapport_hash: str,
    now_berlin: datetime, warnings: list,
    runtime_ms: int,
) -> str:

    kwh    = data["jahresverbrauch_kwh"]
    kw     = data["spitzenlast_kw"]
    prod   = data["is_producing"]
    t      = kalk["tarife"]
    aufsch = kalk["aufschluesselung"]

    ts_display = now_berlin.strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)")

    # ── Benchmarking ────────────────────────────────────────────────────────
    arbeitspreis_ct    = kalk["arbeitspreis_netto_eur_kwh"] * 100
    bench_ct           = BENCHMARK["arbeitspreis_ct_kwh"]
    bench_delta_pct    = ((arbeitspreis_ct - bench_ct) / bench_ct) * 100
    bench_above        = bench_delta_pct > 0
    bench_sign         = "+" if bench_above else ""
    bench_color        = "#c0392b" if bench_above else "#1a7a3c"
    bench_arrow        = "▲" if bench_above else "▼"
    bench_html = (
        f'<span style="color:{bench_color};font-weight:700">'
        f'{bench_arrow} {bench_sign}{de_num(bench_delta_pct, 1)}% gegenüber Bundesdurchschnitt'
        f'</span> ({de_ct(bench_ct)} ct/kWh · {esc(BENCHMARK["quelle"])})'
    )

    # ── Gesamtes Einsparpotenzial (für Header-Banner) ───────────────────────
    einspar_9b_val          = kalk["stromsteuer_entlastung_9b_eur"] if prod else 0.0
    peak_shaving_val        = round(kalk["leistungskosten_netto_eur"] * 0.10, 0)
    einspar_gesamt          = einspar_9b_val + peak_shaving_val
    einspar_5j              = einspar_gesamt * 5

    # ── Waarschuwingen ──────────────────────────────────────────────────────
    warn_html = ""
    if warnings:
        items    = "".join(f"<li>{esc(w)}</li>" for w in warnings)
        warn_html = f'<div class="box yellow"><strong>⚠️ Hinweise zur Dateneingabe:</strong><ul style="margin:6px 0 0 18px">{items}</ul></div>'

    fallback_html = ""
    if markt.get("is_fallback"):
        fallback_html = (
            '<div class="box red"><strong>⚠️ Marktpreis: Fallback-Wert aktiv</strong> – '
            'SMARD (Bundesnetzagentur) war zum Zeitpunkt der Berechnung nicht erreichbar. '
            f'Referenzwert {de_num(CONFIG["fallback_marktpreis_eur_mwh"],2)} EUR/MWh (Ø DE 2025) wurde verwendet. '
            'Bitte Bericht erneut abrufen sobald SMARD wieder verfügbar ist.</div>'
        )

    cached_html = ""
    if markt.get("cached"):
        cached_html = (
            '<div class="box blue" style="font-size:11px">ℹ️ Marktpreis aus Cache '
            f'(max. {SMARD_CACHE_TTL_S//60} Minuten alt) – SMARD wird nicht bei jedem Aufruf abgerufen '
            'um Serverlast zu reduzieren.</div>'
        )

    # ── Kostenaufschlüsselung ───────────────────────────────────────────────
    labels = {
        "beschaffung_vertrieb": f"Strombeschaffung & Vertrieb (Day-Ahead + {t['marge_pct']:.0f}% Marge)",
        "netzentgelt_variabel": f"Netzentgelt variabel – {esc(netz['operator'])} (§21 EnWG)",
        "konzessionsabgabe":    "Konzessionsabgabe (§2 KAV)",
        "kwkg_umlage":          "KWKG-Umlage 2026",
        "offshore_umlage":      "Offshore-Netzumlage 2026 (§17f EnWG)",
        "stromnev_aufschlag":   f"§19 StromNEV-Umlage 2026 – Kategorie {kalk['stromnev_kategorie'][:1]} (Letztverbraucherumlage)",
        "stromsteuer":          "Stromsteuer (§3 StromStG)" + (" + §9b Entlastung" if prod else ""),
        "leistungskosten":      f"Leistungskosten ({de_num(kw,1)} kW × {t['leistungspreis_eur_kw']:.0f} €/kW/Jahr)",
        "messstellenbetrieb":   "Messstellenbetrieb (§20 MsbG)",
        "mwst_19pct":           "Mehrwertsteuer 19% (§12 UStG)",
    }
    aufsch_rows = ""
    for k, label in labels.items():
        v   = aufsch.get(k, 0)
        pct = kalk["anteile_pct"].get(k, 0)
        aufsch_rows += f"<tr><td>{label}</td><td class='r'>{de_eur(v)} €</td><td class='r'>{de_num(pct,1)}%</td></tr>"

    # ── Pie-chart SVG (kostenverdeling) ────────────────────────────────────
    pie_colors = ["#1a4a7a","#2e7d32","#f0a500","#c0392b","#7b1fa2","#00796b","#455a64","#e65100","#37474f","#5d4037"]
    pie_items  = [(labels[k], aufsch.get(k, 0), kalk["anteile_pct"].get(k, 0)) for k in labels]
    pie_items  = [(l, v, p) for l, v, p in pie_items if v > 0]
    brutto     = kalk["brutto_gesamt_eur"] or 1
    cx, cy, r  = 110, 110, 95
    angle      = -90.0
    pie_slices = ""
    legend_html = ""
    import math
    for i, (label, val, pct) in enumerate(pie_items):
        sweep  = (val / brutto) * 360
        a1r    = math.radians(angle)
        a2r    = math.radians(angle + sweep)
        lf     = 1 if sweep > 180 else 0
        x1, y1 = cx + r * math.cos(a1r), cy + r * math.sin(a1r)
        x2, y2 = cx + r * math.cos(a2r), cy + r * math.sin(a2r)
        color  = pie_colors[i % len(pie_colors)]
        pie_slices += f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {lf},1 {x2:.1f},{y2:.1f} Z" fill="{color}" stroke="#fff" stroke-width="1.5"/>'
        legend_html += (
            f'<div style="display:flex;align-items:center;gap:7px;margin:3px 0">'
            f'<span style="display:inline-block;width:12px;height:12px;background:{color};border-radius:2px;flex-shrink:0"></span>'
            f'<span style="font-size:11px;color:#333"><strong>{pct:.1f}%</strong> – {label}</span>'
            f'</div>'
        )
        angle += sweep
    pie_chart = f"""
    <div style="margin:18px 0 8px;page-break-inside:avoid;break-inside:avoid">
      <div style="font-size:12px;font-weight:600;color:#0a2540;margin-bottom:10px">Kostenverdeling (Anteil Brutto)</div>
      <div style="display:flex;gap:28px;align-items:flex-start;flex-wrap:wrap">
        <svg viewBox="0 0 220 220" xmlns="http://www.w3.org/2000/svg" style="width:180px;height:180px;flex-shrink:0">
          {pie_slices}
          <circle cx="{cx}" cy="{cy}" r="42" fill="white"/>
          <text x="{cx}" y="{cy-7}" text-anchor="middle" font-size="12" font-weight="700" fill="#0a2540">{de_num(brutto/1000,1)}k</text>
          <text x="{cx}" y="{cy+10}" text-anchor="middle" font-size="10" fill="#555">EUR Brutto</text>
        </svg>
        <div style="flex:1;min-width:200px;padding-top:4px">{legend_html}</div>
      </div>
    </div>"""
    comp_rows = ""
    for c in comp["checks"]:
        # §9b: injecteer klikbare HZA-link als empfehlung
        if c["norm"] == "§9b StromStG" and c["relevant"]:
            emp = (
                "<br><em style='font-size:11px'>"
                + esc(c["empfehlung"])
                + " – <a href='https://www.zoll.de/DE/Fachthemen/Steuern/Verbrauchsteuern/"
                "Energieerzeugnisse-Strom/Entlastungen/Strom/strom_node.html' "
                "target='_blank' rel='noopener' style='color:#1a4a7a;font-weight:700'>"
                "Zum Antragsformular HZA →</a></em>"
            )
        else:
            emp = f"<br><em style='font-size:11px'>{esc(c['empfehlung'])}</em>" if c["empfehlung"] else ""
        comp_rows += f"<tr><td><strong>{esc(c['norm'])}</strong> – {esc(c['titel'])}</td><td>{esc(c['status'])}{emp}</td></tr>"

    ns_rows = ""
    for ns in comp["next_steps"]:
        ns_rows += f"<tr><td>{esc(ns['aktion'])}</td><td class='r'><strong>{esc(ns['frist'])}</strong></td></tr>"
    ns_block = ""
    if ns_rows:
        ns_block = f"""
        <h3>Empfohlene nächste Schritte</h3>
        <table><thead><tr><th>Maßnahme</th><th>Empfohlene Frist</th></tr></thead>
        <tbody>{ns_rows}</tbody></table>"""

    # ── Informatie-blokken ──────────────────────────────────────────────────
    blok_9b = ""
    if prod and kalk["stromsteuer_entlastung_9b_eur"] > 0:
        blok_9b = f"""
        <div class="box green">
          <strong>§9b StromStG – Vergünstigung Produzierendes Gewerbe</strong><br>
          Angewendeter Steuersatz: <strong>0,0500 ct/kWh</strong> (statt 2,0500 ct/kWh Regelsatz)<br>
          Steuerliche Entlastung: <strong>{de_eur(kalk['stromsteuer_entlastung_9b_eur'])} €/Jahr</strong><br>
          <small>Jahresausgleich beim zuständigen Hauptzollamt beantragen (§9b Abs.2a StromStG). Frist: 31. Dezember des laufenden Jahres.<br>
          📋 <strong>Einzureichendes Formular: Zoll-Formular 1450</strong> (Antrag auf Erlass/Erstattung/Vergütung der Energiesteuer / Stromsteuer für Unternehmen des Produzierenden Gewerbes).</small><br>
          <a href="https://www.zoll.de/DE/Fachthemen/Steuern/Verbrauchsteuern/Energieerzeugnisse-Strom/Entlastungen/Strom/strom_node.html"
             target="_blank" rel="noopener"
             style="display:inline-block;margin-top:7px;color:#1a7a3c;font-weight:700;font-size:12px;text-decoration:underline">
            ✅ Jahresausgleich beantragen: Zum Antragsformular HZA (zoll.de) →
          </a>
        </div>"""

    blok_19 = ""
    if kw >= 30 and kwh >= 30_000:
        blok_19 = f"""
        <div class="box blue">
          <strong>§19 Abs.2 StromNEV – Hinweis zu Netzentgelten und Umlage</strong><br>
          <strong>§19-Umlage (Kostenposition):</strong> In dieser Kalkulation ist die gesetzliche §19-StromNEV-Umlage
          als Kostenkomponente enthalten (Kategorie {esc(kalk['stromnev_kategorie'])}). Diese Umlage zahlen
          <em>alle</em> Letztverbraucher zur Finanzierung individueller Netzentgelte für Großabnehmer.<br><br>
          <strong>Individuelle Netzentgelte (§19 Abs.2 Satz 1 StromNEV):</strong>
          Die Grundvoraussetzungen Spitzenlast ≥ 30 kW und Jahresverbrauch ≥ 30.000 kWh sind mit
          {de_num(kw,1)} kW bzw. {de_kwh(kwh)} kWh erfüllt. <strong>Zusätzlich</strong> ist gemäß
          §19 Abs.2 Satz 1 StromNEV erforderlich, dass die Jahresbenutzungsdauer ≥ 7.000 Volllaststunden
          beträgt <em>oder</em> eine atypische Netznutzung (§19 Abs.2 Satz 2 StromNEV) vorliegt.
          Bei {de_kwh(kwh)} kWh / {de_num(kw,1)} kW ergibt sich eine rechnerische Benutzungsdauer von
          ca. {de_num(kwh/kw if kw > 0 else 0, 0)} Volllaststunden – eine Prüfung durch den
          Netzbetreiber ist daher zwingend erforderlich, bevor ein Antrag gestellt wird.<br>
          <small>Antrag beim Netzbetreiber: Einsparungspotenzial nur bei nachgewiesener Voraussetzungserfüllung.</small><br>
          <small>💡 <strong>Tipp:</strong> Da die Benutzungsdauer unter 7.000 h liegt, ist der Weg über <strong>atypische Netznutzung (§19 Abs.2 Satz 2 StromNEV)</strong> die einzige Option.
          Fragen Sie Ihren Netzbetreiber nach den <strong>Hochlastzeitfenstern</strong> (HLZ) des lokalen Netzes –
          nur wer nachweislich außerhalb dieser Fenster verbraucht, kann individuelle Netzentgelte beantragen.</small>
        </div>
        <div class="box red" style="font-size:12px">
          <strong>⚠️ Benutzungsdauer &lt; 7.000 h → nur atypische Netznutzung möglich</strong><br>
          §19 Abs.2 Satz 1 StromNEV greift erst ab ≥ 7.000 Volllaststunden.
          Bei ca. {de_num(kwh/kw if kw > 0 else 0, 0)} h ist §19 Abs.2 Satz 2 (atypische Netznutzung) der einzige
          mögliche Antragsweg – vorherige Abstimmung mit dem Netzbetreiber zwingend erforderlich.
        </div>"""

    spitzenlast_hinweis = f"""
    <div class="box yellow">
      <strong>⚠️ Hinweis zur Spitzenlast (Jahresleistungsmaximum)</strong><br>
      Die angegebene Spitzenlast von <strong>{de_num(kw,1)} kW</strong> ist ein entscheidender Parameter.
      Schwankungen der tatsächlichen Spitzenlast im Jahresverlauf können die berechneten
      Leistungskosten und die §19-StromNEV-Umlage erheblich beeinflussen.
      Grundlage sollte stets die <em>gemessene Jahreshöchstlast</em> aus der Lastgangmessung sein,
      nicht eine Schätzung. Bei Unsicherheit: Netzbetreiber oder Energieberater konsultieren.
    </div>"""

    gesetze  = "".join(f"<li>{esc(g)}</li>" for g in GESETZ_REFS)
    esrs_pts = ", ".join(esg["esrs_datenpunkte"])

    # ── Metadata-sectie ─────────────────────────────────────────────────────
    meta_cached  = "Ja (aus Cache)" if markt.get("cached") else "Nein (Live-Abruf)"
    meta_fallback = "⚠️ Ja – Fallback-Wert" if markt.get("is_fallback") else "✅ Nein – Live-Daten"
    data_age     = f"{markt.get('data_age_hours', '—')} Stunden" if not markt.get("is_fallback") else "Nicht verfügbar (Fallback aktiv)"

    # ── Einsparpotenziale ───────────────────────────────────────────────────
    peak_shaving_theoretisch = peak_shaving_val
    einspar_9b   = einspar_9b_val
    einspar_rows = ""
    if prod and einspar_9b > 0:
        einspar_rows += f"""<tr><td>§9b StromStG – Stromsteuerentlastung</td>
          <td class="r"><strong>{de_eur(einspar_9b)} €/Jahr</strong></td>
          <td>Bereits angewendet – Jahresausgleich beim HZA beantragen</td></tr>"""
    einspar_rows += f"""<tr><td>Peak-Shaving – Spitzenlastreduzierung (theoretisch)</td>
          <td class="r">{de_eur(peak_shaving_theoretisch)} €/Jahr</td>
          <td>Schätzung bei 10% Reduzierung ({de_num(kw,1)} → {de_num(kw*0.9,1)} kW) – abhängig vom Lastprofil</td></tr>
        <tr><td>§19 Abs.2 StromNEV – Individuelle Netzentgelte</td>
          <td class="r">Prüfung erforderlich</td>
          <td>Grundvoraussetzungen teilweise erfüllt – Benutzungsdauer durch Netzbetreiber prüfen lassen</td></tr>"""
    blok_einspar = f"""
<div class="sec">
<h2>2b · Einsparpotenziale (Übersicht)</h2>
<table>
  <thead><tr><th>Maßnahme</th><th class="r">Potenzial</th><th>Hinweis</th></tr></thead>
  <tbody>{einspar_rows}</tbody>
</table>
<div class="box blue" style="margin-top:10px;font-size:12px">
  <strong>📅 Mehrjahresprojektion:</strong> Bei gleichbleibendem Verbrauch und konsequenter
  Umsetzung aller Maßnahmen ergibt sich über <strong>5 Jahre</strong> ein kumuliertes
  Einsparpotenzial von <strong style="font-size:14px;color:#1a4a7a">{de_eur(einspar_5j)} €</strong>
  (§9b + Peak-Shaving, ohne §19-StromNEV-Potenzial).
</div>
<p class="src">Peak-Shaving-Wert ist eine theoretische Schätzung ohne Gewähr. §19-Potenzial nur nach Netzbetreiberprüfung realisierbar. Mehrjahresprojektion ohne Preissteigerungen berechnet.</p>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="X-Content-Type-Options" content="nosniff">
<meta name="robots" content="noindex,nofollow">
<title>StromAudit Pro – {esc(pruf_nr)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',Arial,sans-serif;font-size:13px;color:#1a1a1a;background:#f0f2f7}}
.wrap{{max-width:980px;margin:0 auto;background:#fff;box-shadow:0 2px 24px rgba(0,0,0,.13)}}
.print-bar{{background:#0a2540;padding:10px 48px;display:flex;align-items:center;justify-content:space-between}}
.print-bar span{{color:rgba(255,255,255,.7);font-size:12px}}
.btn-print{{background:#f0a500;color:#fff;border:none;padding:9px 22px;border-radius:4px;font-size:13px;font-weight:700;cursor:pointer}}
.btn-print:hover{{background:#d4920a}}
.hdr{{background:linear-gradient(135deg,#0a2540 0%,#1a4a7a 100%);color:#fff;padding:32px 48px 24px}}
.hdr h1{{font-size:24px;font-weight:700}}
.hdr .sub{{font-size:12px;opacity:.75;margin-top:3px}}
.hdr-meta{{display:flex;flex-wrap:wrap;gap:24px;margin-top:18px;font-size:11px;opacity:.85}}
.hdr-meta div{{display:flex;flex-direction:column;gap:2px}}
.hdr-meta strong{{font-size:12px;color:#7ecef4}}
.badge-audit{{display:inline-block;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);border-radius:4px;padding:4px 12px;font-size:11px;margin-top:12px}}
.disc-bar{{background:#fffbe6;border-left:5px solid #f0a500;padding:10px 48px;font-size:11px;color:#6b4c00;line-height:1.6}}
.sec{{padding:24px 48px;border-bottom:1px solid #eaedf3;page-break-inside:avoid}}
.sec h2{{font-size:14px;font-weight:700;color:#0a2540;text-transform:uppercase;letter-spacing:.6px;border-bottom:2px solid #0a2540;padding-bottom:5px;margin-bottom:16px}}
.sec h3{{font-size:13px;color:#1a4a7a;margin:18px 0 10px;font-weight:600}}
.kpi-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:4px}}
.kpi{{background:#f0f4ff;border:1px solid #c5d5f0;border-radius:7px;padding:10px 13px;page-break-inside:avoid}}
.kpi .lbl{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.4px}}
.kpi .val{{font-size:18px;font-weight:700;color:#0a2540;margin:2px 0 1px}}
.kpi .sub{{font-size:10px;color:#777}}
.kpi.grn{{background:#e8f8ee;border-color:#82c99a}}.kpi.grn .val{{color:#1a7a3c}}
.kpi.org{{background:#fff3e0;border-color:#ffb74d}}.kpi.org .val{{color:#c75000}}
.kpi.tuv{{background:#0a2540;border-color:#0a2540;text-align:center}}
.tuv-seal{{font-size:28px;font-weight:900;color:#f0a500;line-height:1.1;margin:4px 0 2px}}
.tuv-seal span{{font-size:13px;color:rgba(255,255,255,.6);font-weight:400}}
.kpi.tuv .lbl{{color:rgba(255,255,255,.7)}}
.kpi.tuv .sub{{color:rgba(255,255,255,.5)}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:4px}}
th{{background:#0a2540;color:#fff;padding:8px 11px;text-align:left;font-size:11px;font-weight:600}}
td{{padding:7px 11px;border-bottom:1px solid #eaecf0;vertical-align:top}}
tr:nth-child(even) td{{background:#f8f9fc}}
td.r{{text-align:right;font-variant-numeric:tabular-nums}}
tfoot td{{background:#0a2540!important;color:#fff;font-weight:700;border:none}}
.box{{border-radius:6px;padding:13px 15px;margin:11px 0;font-size:12px;line-height:1.6}}
.box.green{{background:#e8f8ee;border-left:4px solid #1a7a3c}}
.box.blue{{background:#e3f0ff;border-left:4px solid #1a4a7a}}
.box.yellow{{background:#fffbe6;border-left:4px solid #f0a500}}
.box.red{{background:#fff0f0;border-left:4px solid #c0392b}}
.tbl-wrap{{page-break-inside:avoid;break-inside:avoid}}
.hash-box{{font-family:monospace;font-size:10px;background:#f5f5f5;border:1px solid #ddd;border-radius:4px;padding:8px 12px;word-break:break-all;color:#555;margin-top:8px}}
.savings-banner{{background:linear-gradient(135deg,#0d3b1a 0%,#1a7a3c 100%);padding:18px 48px 14px;color:#fff}}
.savings-banner-inner{{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px;margin-bottom:10px}}
.savings-main{{display:flex;align-items:center;gap:16px}}
.savings-icon{{font-size:36px;line-height:1}}
.savings-title{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;opacity:.8;margin-bottom:2px}}
.savings-amount{{font-size:28px;font-weight:800;color:#7eeaa0;line-height:1.1}}
.savings-sub{{font-size:11px;opacity:.8;margin-top:4px}}
.savings-5y{{text-align:right}}
.savings-5y-label{{font-size:11px;text-transform:uppercase;letter-spacing:.08em;opacity:.7;margin-bottom:2px}}
.savings-5y-amount{{font-size:22px;font-weight:800;color:#ffd166}}
.savings-5y-sub{{font-size:10px;opacity:.65}}
.savings-bench{{font-size:11.5px;opacity:.85;border-top:1px solid rgba(255,255,255,.2);padding-top:10px}}
.savings-bench strong{{color:#fff}}
.intro-block{{background:#f7f9ff;border-left:4px solid #1a4a7a;padding:14px 48px;font-size:13px;line-height:1.7;color:#1a1a1a}}
.ftr{{background:#0a2540;color:rgba(255,255,255,.65);padding:20px 48px;font-size:10.5px;line-height:1.8}}
.ftr strong{{color:#fff}}
.src{{font-size:10px;color:#999;margin-top:5px;font-style:italic}}
@media print{{
  body{{background:#fff}}
  .wrap{{box-shadow:none}}
  .print-bar,.btn-print{{display:none!important}}
  .sec{{padding:14px 24px;page-break-inside:avoid}}
  .hdr{{padding:18px 24px}}
  .disc-bar{{padding:8px 24px}}
  .ftr{{padding:14px 24px}}
  table{{page-break-inside:avoid}}
  .kpi-grid{{page-break-inside:avoid}}
  .kpi{{page-break-inside:avoid}}
  .box{{page-break-inside:avoid}}
  h2,h3{{page-break-after:avoid}}
}}
</style>
</head>
<body>
<div class="wrap">

<div class="print-bar">
  <span>📄 Audit-Vorbereitungsbericht · {esc(pruf_nr)} · Als PDF speichern:</span>
  <button class="btn-print" onclick="window.print()">🖨️ Drucken / Als PDF speichern</button>
</div>

<div class="hdr">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px">
    <div>
      <h1>⚡ StromAudit Pro</h1>
      <div class="sub">Deutsche Energie-Compliance &amp; ESG Pre-Audit Engine v{REPORT_VERSION}</div>
      <div class="badge-audit">AUDIT-VORBEREITUNGSBERICHT · PRÜFUNGSBEREIT (PRE-AUDIT)</div>
    </div>
    <div style="text-align:right;font-size:11px;opacity:.8;line-height:1.7">
      <div>Prüfnummer: <strong style="color:#7ecef4">{esc(pruf_nr)}</strong></div>
      <div>Erstellt: <strong style="color:#7ecef4">{ts_display}</strong></div>
      <div>Berichtsjahr: <strong style="color:#7ecef4">{data['berichtsjahr']}</strong></div>
    </div>
  </div>
  <div class="hdr-meta">
    <div><span>Standort PLZ</span><strong>{esc(data['plz'])} · {esc(netz.get('bundesland','—'))}</strong></div>
    <div><span>Netzbetreiber</span><strong>{esc(netz['operator'])}</strong></div>
    <div><span>Marktpreis (SMARD)</span><strong>{de_num(markt['raw_eur_mwh'],2)} EUR/MWh</strong></div>
    <div><span>7-Tage-Ø</span><strong>{de_num(markt['avg_7d_eur_kwh']*1000,2)} EUR/MWh</strong></div>
    <div><span>Marktpreis gültig für</span><strong>{esc(markt['timestamp_berlin'].split(' ')[0])} (00:00 – 23:59 Uhr)</strong></div>
  </div>
</div>

<div class="disc-bar">
  ⚠️ <strong>Audit-Vorbereitungsdokument (Pre-Audit) – Nicht geprüft:</strong>
  Automatisch generiert auf Basis öffentlicher Behördendaten. Ersetzt keine zertifizierte
  Energiefachprüfung (DIN EN 16247-1) und keine steuerrechtliche Beratung.
  Endvalidierung durch zugelassenen WP/vBP oder Energieberater (§21 EDL-G).
  Marktpreis: {esc(markt['source'])}.
</div>

<div class="savings-banner">
  <div class="savings-banner-inner">
    <div class="savings-main">
      <span class="savings-icon">💡</span>
      <div>
        <div class="savings-title">Identifiziertes Einsparpotenzial</div>
        <div class="savings-amount">bis zu {de_eur(einspar_gesamt)} €/Jahr</div>
        <div class="savings-sub">
          §9b StromStG: <strong>{de_eur(einspar_9b_val)} €</strong> &nbsp;·&nbsp;
          Peak-Shaving (–10%): <strong>{de_eur(peak_shaving_val)} €</strong>
        </div>
      </div>
    </div>
    <div class="savings-5y">
      <div class="savings-5y-label">5-Jahres-Projektion</div>
      <div class="savings-5y-amount">{de_eur(einspar_5j)} €</div>
      <div class="savings-5y-sub">bei gleichbleibendem Verbrauch</div>
    </div>
  </div>
  <div class="savings-bench">
    Ihr Arbeitspreis: <strong>{de_ct(arbeitspreis_ct)} ct/kWh</strong> &nbsp;·&nbsp; {bench_html}
  </div>
</div>

<div class="intro-block">
  <strong>Sehr geehrte Damen und Herren von {esc(data['unternehmen'])},</strong><br>
  auf Basis Ihrer Angaben für den Standort <strong>{esc(data['plz'])} · {esc(netz.get('bundesland','—'))}</strong>
  haben wir folgende Ergebnisse und Optimierungspotenziale für das Berichtsjahr
  <strong>{data['berichtsjahr']}</strong> ermittelt.
  Der Bericht basiert auf einem Jahresverbrauch von <strong>{de_kwh(kwh)} kWh</strong>
  und einer Spitzenlast von <strong>{de_num(kw,1)} kW</strong>.
</div>

{"<div class='sec'>" + warn_html + fallback_html + cached_html + "</div>" if (warn_html or fallback_html or cached_html) else ""}

<div class="sec">
<h2>1 · Stammdaten &amp; Eingabeparameter</h2>
<table>
  <tr><td><strong>Unternehmen</strong></td><td>{esc(data['unternehmen'])}</td>
      <td><strong>Anschrift / PLZ</strong></td><td>{esc(data['anschrift'])} · {esc(data['plz'])}</td></tr>
  <tr><td><strong>Bundesland</strong></td><td>{esc(netz.get('bundesland','—'))}</td>
      <td><strong>Netzbetreiber</strong></td><td>{esc(netz['operator'])}</td></tr>
  <tr><td><strong>Jahresverbrauch</strong></td><td>{de_kwh(kwh)} kWh</td>
      <td><strong>Spitzenlast</strong></td><td>{de_num(kw,1)} kW</td></tr>
  <tr><td><strong>Messstellenbetrieb (§20 MsbG)</strong></td><td>{de_eur(data['messstellenbetrieb_eur'])} €/Jahr</td>
      <td><strong>Prod. Gewerbe §9b StromStG</strong></td><td>{"✅ Ja – Vergünstigung angewendet" if prod else "○ Nein"}</td></tr>
  <tr><td><strong>Berichtsjahr</strong></td><td>{data['berichtsjahr']}</td>
      <td><strong>§19 StromNEV Kategorie</strong></td><td>{esc(kalk['stromnev_kategorie'])}</td></tr>
  <tr><td><strong>Projekt-Referenz</strong></td><td colspan="3"><strong>{esc(data['projekt_referenz'])}</strong></td></tr>
</table>
</div>

<div class="sec">
<h2>2 · Energie-Kennzahlen (KPIs)</h2>
<div class="tbl-wrap">
<div class="kpi-grid">
  <div class="kpi org"><div class="lbl">Brutto-Jahreskosten</div>
    <div class="val">{de_kwh(kalk['brutto_gesamt_eur'])} €</div><div class="sub">inkl. 19% MwSt.</div></div>
  <div class="kpi"><div class="lbl">Netto-Jahreskosten</div>
    <div class="val">{de_kwh(kalk['netto_gesamt_eur'])} €</div><div class="sub">excl. MwSt.</div></div>
  <div class="kpi"><div class="lbl">Arbeitspreis (netto)</div>
    <div class="val">{de_ct(kalk['arbeitspreis_netto_eur_kwh']*100)} ct/kWh</div><div class="sub">inkl. Netzentgelte, Umlagen, Stromsteuer, Beschaffung</div></div>
  <div class="kpi"><div class="lbl">Leistungskosten</div>
    <div class="val">{de_kwh(kalk['leistungskosten_netto_eur'])} €</div>
    <div class="sub">{de_num(kw,1)} kW × {t['leistungspreis_eur_kw']:.0f} €/kW</div></div>
  <div class="kpi grn"><div class="lbl">CO₂-Footprint (Scope 2)</div>
    <div class="val">{de_num(esg['co2_footprint_tonnen'],2)} t</div><div class="sub">CO₂e · ESRS E1 location-based</div></div>
  <div class="kpi grn"><div class="lbl">§9b Entlastung</div>
    <div class="val">{de_kwh(kalk['stromsteuer_entlastung_9b_eur'])} €</div>
    <div class="sub">{"Angewendet" if prod else "Nicht aktiviert"}</div></div>
  <div class="kpi"><div class="lbl">Day-Ahead Marktpreis</div>
    <div class="val">{de_num(markt['raw_eur_mwh'],1)}</div>
    <div class="sub">{'Fallback – SMARD nicht erreichbar' if markt.get('is_fallback') else 'EUR/MWh (SMARD live)'}</div></div>
  <div class="kpi tuv"><div class="lbl">Compliance-Score</div>
    <div class="tuv-seal">{comp['compliance_score']}<span>/100</span></div>
    <div class="sub">Prüfbereitschaft</div></div>
</div>
</div>
<div class="box {'red' if bench_above else 'green'}" style="margin-top:10px;font-size:12px">
  <strong>📊 Benchmark:</strong> Ihr Arbeitspreis ({de_ct(arbeitspreis_ct)} ct/kWh) liegt
  <strong style="color:{bench_color}">{bench_sign}{de_num(bench_delta_pct,1)}%
  {'über' if bench_above else 'unter'} dem Bundesdurchschnitt</strong>
  vergleichbarer Betriebe ({de_ct(bench_ct)} ct/kWh · {esc(BENCHMARK['quelle'])}).
  {'Optimierungspotenzial vorhanden – Vergleich Stromangebote empfohlen.' if bench_above else 'Ihr Tarif ist wettbewerbsfähig.'}
</div>

{blok_einspar}

<div class="sec">
<h2>3 · Vollständige Kostenaufschlüsselung 2026</h2>
{blok_9b}{blok_19}{spitzenlast_hinweis}
<div class="tbl-wrap">
<table>
  <thead><tr><th>Kostenkomponente</th><th class="r">EUR/Jahr</th><th class="r">Anteil</th></tr></thead>
  <tbody>{aufsch_rows}</tbody>
  <tfoot><tr>
    <td>GESAMT (Brutto inkl. 19% MwSt.)</td>
    <td class="r">{de_eur(kalk['brutto_gesamt_eur'])} €</td>
    <td class="r">100%</td>
  </tr></tfoot>
</table>
{pie_chart}
</div>
<div class="tbl-wrap">
<h3>Angewendete Tarifsätze 2026 (gesetzliche Grundlage)</h3>
<table>
  <tr><th>Parameter</th><th>Wert</th><th>Rechtsgrundlage</th></tr>
  <tr><td>Day-Ahead Marktpreis (SMARD)</td><td class="r">{de_num(t['marktpreis_eur_kwh']*1000,2)} EUR/MWh</td><td>EPEX Spot / EnWG §1</td></tr>
  <tr><td>Beschaffung inkl. Marge ({t['marge_pct']:.0f}% – Schätzwert, vertragsabhängig)</td><td class="r">{de_ct(t['beschaffung_inkl_marge']*100,4)} ct/kWh</td><td>Marktüblich (keine gesetzliche Vorgabe)</td></tr>
  <tr><td>Netzentgelt variabel</td><td class="r">{de_ct(t['netzentgelt_variabel']*100)} ct/kWh</td><td>§21 EnWG / BNetzA</td></tr>
  <tr><td>Konzessionsabgabe</td><td class="r">{de_ct(t['konzessionsabgabe']*100)} ct/kWh</td><td>§2 KAV</td></tr>
  <tr><td>KWKG-Umlage 2026</td><td class="r">{de_ct(t['kwkg_umlage']*100)} ct/kWh</td><td>KWKG 2016/2020</td></tr>
  <tr><td>Offshore-Netzumlage 2026</td><td class="r">{de_ct(t['offshore_umlage']*100)} ct/kWh</td><td>§17f EnWG</td></tr>
  <tr><td>§19 StromNEV Aufschlag</td><td class="r">{de_ct(t['stromnev19_umlage']*100)} ct/kWh</td><td>§19 Abs.2 StromNEV</td></tr>
  <tr><td>Stromsteuer</td><td class="r">{de_ct(t['stromsteuer']*100)} ct/kWh {"(§9b-Satz)" if prod else "(Regelsatz)"}</td><td>§3 StromStG{" / §9b" if prod else ""}</td></tr>
  <tr><td>Leistungspreis (Schätzwert – regional variabel)</td><td class="r">{de_eur(t['leistungspreis_eur_kw'])} €/kW/Jahr</td><td>§21 EnWG / BNetzA (regionaler NNB)</td></tr>
  <tr><td>Messstellenbetrieb (§20 MsbG)</td><td class="r">{de_eur(data['messstellenbetrieb_eur'])} €/Jahr</td><td>§20 MsbG / Messstellenvertrag</td></tr>
  <tr><td>MwSt.</td><td class="r">{de_num(t['mwst_satz']*100,0)}%</td><td>§12 UStG</td></tr>
</table>
<p class="src">Umlagen-Quelle: Übertragungsnetzbetreiber (ÜNB) – amtliche Veröffentlichung Oktober 2025.
Netzentgelte: Bundesnetzagentur (BNetzA) / regionaler Netzbetreiber. Marktpreis: SMARD Bundesnetzagentur.<br>
<strong>Hinweis Day-Ahead-Preis:</strong> Der angezeigte Marktpreis ist der gewichtete Durchschnitt der EPEX Spot Day-Ahead-Auktion und gilt für die gesamte Kalenderdag ({esc(markt['timestamp_berlin'].split(' ')[0])}, 00:00 – 23:59 Uhr).<br>
<strong>Hinweis Leistungspreis:</strong> Der verwendete Leistungspreis von {de_eur(CONFIG['leistungspreis_eur_kw'])} €/kW/Jahr ist ein
Schätzwert (regionaler Durchschnitt). Der tatsächliche Leistungspreis ist dem Netzentgelttarif des
zuständigen Netzbetreibers zu entnehmen (§21 EnWG / BNetzA-Veröffentlichung).<br>
<strong>Hinweis Beschaffungsmarge:</strong> Die angesetzte Marge von {CONFIG['beschaffungs_marge']*100:.0f}% auf den Day-Ahead-Marktpreis
ist eine Schätzung. Der tatsächliche Lieferantenpreis ergibt sich aus dem individuellen Stromliefervertrag.</p>
</div>
</div>

<div class="sec">
<h2>4 · ESG-Bericht · Scope-2-Emissionen (ESRS E1 / GHG Protocol)</h2>
<div class="tbl-wrap">
<div class="kpi-grid">
  <div class="kpi grn"><div class="lbl">CO₂-Fußabdruck (Scope 2, location-based)</div>
    <div class="val">{de_num(esg['co2_footprint_tonnen'],2)} t CO₂e</div>
    <div class="sub">{de_kwh(esg['co2_footprint_kg'])} kg</div></div>
  <div class="kpi"><div class="lbl">Emissionsfaktor (UBA 2025)</div>
    <div class="val">{de_num(esg['emissionsfaktor_g_co2_kwh'],0)} g</div><div class="sub">CO₂e/kWh</div></div>
  <div class="kpi"><div class="lbl">Intensitätsrate</div>
    <div class="val">{de_num(esg['intensitaetsrate_t_co2_mwh'],3)}</div><div class="sub">t CO₂e/MWh</div></div>
</div>
<table>
  <thead><tr><th>Parameter</th><th>Wert</th></tr></thead>
  <tbody>
  <tr><td>Scope &amp; Methodik</td><td>{esc(esg['scope'])}</td></tr>
  <tr><td>Norm</td><td>{esc(esg['norm'])}</td></tr>
  <tr><td>Emissionsfaktor</td><td>{de_num(esg['emissionsfaktor_g_co2_kwh'],0)} g CO₂e/kWh – {esc(esg['quelle'])}</td></tr>
  <tr><td>CO₂-Emissionen (Scope 2, location-based)</td><td><strong>{de_kwh(esg['co2_footprint_kg'])} kg ({de_num(esg['co2_footprint_tonnen'],2)} t CO₂e)</strong></td></tr>
  <tr><td>EU-Taxonomie-Relevanz</td><td>{esc(esg['eu_taxonomy'])}</td></tr>
  <tr><td>CSRD/ESRS-Datenpunkte</td><td>{esc(esrs_pts)}</td></tr>
  </tbody>
</table>
<div class="box blue" style="margin-top:12px">
  <strong>💶 CO₂-Beprijzing (BEHG 2026)</strong><br>
  Auf Basis des deutschen CO₂-Preises gemäß <strong>Brennstoffemissionshandelsgesetz (BEHG)</strong>
  von <strong>{de_eur(CONFIG['behg_co2_preis_eur_t'])} €/t</strong> (Tarif 2026) ergibt sich für Ihren
  Scope-2-Fußabdruck von <strong>{de_num(esg['co2_footprint_tonnen'],2)} t CO₂e</strong>
  eine implizite CO₂-Kostenbelastung von
  <strong style="font-size:13px;color:#1a4a7a">{de_eur(esg['behg_co2_kosten_eur'])} €/Jahr</strong>.<br>
  <small>Hinweis: Der BEHG-CO₂-Preis betrifft primär Brennstoffe (Wärme/Verkehr). Strom wird über ETS1 bepreist.
  Diese Berechnung zeigt die vollständige CO₂-Kostenrelevanz Ihres Energieprofils für ESG-/CSRD-Reporting.
  Quelle: §10 BEHG – Festpreisphase 2026.</small>
</div>
<div class="box yellow" style="margin-top:12px">
  <strong>Hinweis für CSRD/ESRS E1-Berichterstattung:</strong><br>
  Dieser Scope-2-Wert ist location-based (Strommix Deutschland 2025).
  Marktbasierter Scope-2-Wert (Herkunftsnachweise / Guarantees of Origin) nicht im Lieferumfang enthalten –
  für vollständige CSRD-Konformität zusätzlich zu ermitteln.
  Endvalidierung durch zugelassenen Wirtschaftsprüfer (WP/vBP).
</div>
</div>
</div>

<div class="sec">
<h2>5 · Compliance-Checkliste &amp; Handlungsempfehlungen</h2>
<div class="tbl-wrap">
<table>
  <thead><tr><th style="width:58%">Norm / Vorschrift</th><th>Status &amp; Empfehlung</th></tr></thead>
  <tbody>{comp_rows}</tbody>
</table>
</div>
{ns_block}
<p class="src" style="margin-top:8px">Alle Angaben basieren auf Richtwerten. Verbindliche Compliance-Bestätigung durch Steuer- oder Energieberater erforderlich.</p>
<div class="box yellow" style="margin-top:10px;font-size:12px">
  <strong>⏱️ Aktualität dieses Berichts:</strong>
  Marktpreise ändern sich täglich – dieser Bericht spiegelt den Stand vom {ts_display} wider.
  Eine erneute Prüfung wird empfohlen, sobald sich Ihr <strong>Verbrauch</strong>,
  Ihre <strong>Spitzenlast</strong> oder der <strong>Marktpreis</strong> wesentlich ändert.
  Erstellen Sie jederzeit einen aktualisierten Bericht mit denselben Eingabeparametern.
</div>
</div>

<div class="sec">
<h2>6 · Angewendete Rechtsgrundlagen &amp; Normen</h2>
<ul style="columns:2;column-gap:28px;padding-left:16px;line-height:2;font-size:12px">{gesetze}</ul>
</div>

<div class="sec">
<h2>7 · Rapport-Metadata &amp; Integrität</h2>
<div class="tbl-wrap">
<table>
  <thead><tr><th>Parameter</th><th>Wert</th></tr></thead>
  <tbody>
  <tr><td>Prüfnummer</td><td><strong>{esc(pruf_nr)}</strong></td></tr>
  <tr><td>Erstellt (Europe/Berlin)</td><td>{ts_display}</td></tr>
  <tr><td>Berichtsjahr</td><td>{data['berichtsjahr']}</td></tr>
  <tr><td>Tool-Version</td><td>StromAudit Pro v{REPORT_VERSION}</td></tr>
  <tr><td>Berechnungszeit</td><td>{runtime_ms} ms</td></tr>
  <tr><td>SMARD-Datenquelle</td><td>{esc(markt['source'])}</td></tr>
  <tr><td>SMARD-Datenstand</td><td>{esc(markt['timestamp_berlin'])}</td></tr>
  <tr><td>SMARD-Datenpunkte (7 Tage)</td><td>{markt['data_points_7d']}</td></tr>
  <tr><td>Marktpreis aus Cache</td><td>{meta_cached}</td></tr>
  <tr><td>Fallback-Modus</td><td>{meta_fallback}</td></tr>
  <tr><td>Datenalter SMARD</td><td>{data_age}</td></tr>
  <tr><td>CO₂-Faktor-Quelle</td><td>{esc(esg['quelle'])}</td></tr>
  <tr><td>Netzentgelt-Quelle</td><td>BNetzA / ÜNB Oktober 2025 (amtlich)</td></tr>
  </tbody>
</table>
</div>
<h3>🔐 Rapport-Hash (Manipulationsschutz / SHA-256)</h3>
<p style="font-size:12px;margin-bottom:6px">
  Der folgende Hash wurde aus den Kerndaten dieses Berichts (PLZ, Verbrauch, Spitzenlast,
  Gesamtkosten, CO₂-Wert, Prüfnummer) berechnet. Jede Änderung der Berechnungsdaten
  erzeugt einen anderen Hash – dies dient als Manipulationsschutz für Audit-Zwecke.
</p>
<div style="border:2px solid #0a2540;border-radius:8px;padding:16px 20px;background:#f0f4ff;margin:12px 0;display:flex;align-items:center;gap:18px;flex-wrap:wrap">
  <div style="font-size:36px;line-height:1">🔏</div>
  <div style="flex:1;min-width:180px">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#555;margin-bottom:3px">Prüfnummer (Audit-ID)</div>
    <div style="font-size:16px;font-weight:800;color:#0a2540;letter-spacing:.04em">{esc(pruf_nr)}</div>
    <div style="font-size:10px;color:#777;margin-top:6px;text-transform:uppercase;letter-spacing:.04em">SHA-256 Integritäts-Hash</div>
    <div style="font-family:monospace;font-size:10px;background:#fff;border:1px solid #c5d5f0;border-radius:4px;padding:6px 10px;word-break:break-all;color:#333;margin-top:3px">{rapport_hash}</div>
  </div>
</div>
</div>

<div class="sec">
<h2>8 · Vollständiger Haftungsausschluss &amp; Nutzungsbedingungen</h2>
<div class="box yellow">
<strong>Rechtlicher Status dieses Dokuments</strong><br>
Dieses Dokument ist ein automatisch generierter Audit-Vorbereitungsbericht (Pre-Audit Report).
Es handelt sich ausdrücklich <strong>nicht</strong> um ein geprüftes Gutachten, eine Steuerberatung,
eine Rechtsberatung oder eine Wirtschaftsprüferleistung im Sinne des WPO, StBerG oder RDG.
</div>
<div class="box red">
<strong>Haftungsausschluss des Betreibers</strong><br>
StromAudit Pro ist ein vollautomatischer Datenverarbeitungs- und Berechnungsdienst.
Der Betreiber ist ausschließlich <strong>Aggregator öffentlich zugänglicher Behördendaten</strong>
(SMARD/Bundesnetzagentur, ÜNB, Umweltbundesamt, IFEU) und stellt diese in strukturierter Form dar.<br><br>
<strong>Der Betreiber:</strong>
<ul style="margin:6px 0 0 16px;line-height:1.9">
  <li>ist kein Energieberater, Steuerberater, Rechtsanwalt oder Wirtschaftsprüfer</li>
  <li>begründet durch diesen Dienst keine Beratungs-, Auskunfts- oder sonstige Vertragspflicht</li>
  <li>übernimmt <strong>keinerlei Haftung</strong> für die Richtigkeit, Vollständigkeit oder Aktualität der Ergebnisse</li>
  <li>übernimmt <strong>keinerlei Haftung</strong> für Entscheidungen, die auf Basis dieses Berichts getroffen werden</li>
  <li>übernimmt <strong>keinerlei Haftung</strong> für Schäden jeglicher Art aus der Nutzung dieses Dienstes</li>
  <li>ist nicht verantwortlich für Änderungen gesetzlicher Tarife nach dem Erstellungsdatum</li>
  <li>speichert keine personenbezogenen Daten (DSGVO-konform)</li>
</ul><br>
Für die Richtigkeit der Eingabedaten trägt der Nutzer die alleinige Verantwortung.
Dieser Ausschluss gilt im maximal nach geltendem Recht zulässigen Umfang.
</div>
<p style="font-size:11px;color:#888;margin-top:10px">
Alle Ausgangsdaten sind öffentlich zugängliche Behördendaten (DL-DE/BY-2-0).
SMARD: smard.de · ÜNB: netztransparenz.de · UBA: umweltbundesamt.de · BNetzA: bundesnetzagentur.de
</p>
</div>

<div class="ftr">
  <strong>StromAudit Pro v{REPORT_VERSION}</strong> · Prüfnummer: {esc(pruf_nr)}<br>
  Erstellt: {ts_display} · Berichtsjahr: {data['berichtsjahr']} · Laufzeit: {runtime_ms} ms<br>
  SHA-256: {rapport_hash[:32]}…<br>
  Marktpreis: {esc(markt['source'])} | Stand: {esc(markt['timestamp_berlin'])}<br><br>
  <strong>Haftungsausschluss:</strong> Automatisch generierter Pre-Audit Bericht. Kein geprüftes Gutachten.
  Keine Haftung. Betreiber ist ausschließlich Aggregator öffentlicher Behördendaten.
  Endvalidierung durch WP/vBP oder Energieberater (§21 EDL-G) erforderlich.<br>
  © {now_berlin.year} StromAudit Pro
</div>

</div>
<script>
if(new URLSearchParams(window.location.search).get('print')==='1') window.print();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# FOUT-RAPPORT
# ─────────────────────────────────────────────────────────────────────────────
def generiere_fehler_html(errors: str, pruf_nr: str, ts_display: str) -> str:
    items = "".join(f"<li>{esc(e)}</li>" for e in errors.split("\n") if e.strip())
    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8">
<title>StromAudit Pro – Eingabefehler</title>
<style>
body{{font-family:'Segoe UI',Arial,sans-serif;background:#f0f2f7;display:flex;
  justify-content:center;align-items:center;min-height:100vh;margin:0}}
.card{{background:#fff;border-radius:8px;box-shadow:0 2px 20px rgba(0,0,0,.12);
  max-width:640px;width:100%;padding:40px;text-align:center}}
.icon{{font-size:52px;margin-bottom:12px}}
h1{{color:#c0392b;font-size:20px;margin-bottom:8px}}
.sub{{color:#666;font-size:13px;margin-bottom:20px}}
.errors{{background:#fff0f0;border-left:4px solid #c0392b;border-radius:4px;
  padding:14px 16px;text-align:left;font-size:13px;margin-bottom:20px}}
.errors ul{{margin:8px 0 0 16px;line-height:1.9}}
.hint{{background:#e3f0ff;border-left:4px solid #1a4a7a;border-radius:4px;
  padding:12px 16px;font-size:12px;text-align:left}}
.meta{{font-size:10px;color:#aaa;margin-top:20px}}
</style></head>
<body><div class="card">
<div class="icon">⚡❌</div>
<h1>StromAudit Pro – Eingabefehler</h1>
<div class="sub">Der Bericht konnte nicht erstellt werden. Bitte Eingaben prüfen.</div>
<div class="errors"><strong>Festgestellte Fehler:</strong><ul>{items}</ul></div>
<div class="hint">
  <strong>Pflichtfelder:</strong><br>
  • <code>plz</code> – 5-stellige deutsche Postleitzahl (z.B. 80331)<br>
  • <code>jahresverbrauch_kwh</code> – Jahresverbrauch in kWh (z.B. 125000)<br>
  • <code>spitzenlast_kw</code> – Spitzenlast in kW (z.B. 45)<br><br>
  Optional: <code>messstellenbetrieb_eur</code> · <code>is_producing</code> ·
  <code>unternehmen</code> · <code>anschrift</code> · <code>projekt_referenz</code> · <code>berichtsjahr</code>
</div>
<div class="meta">Ref: {esc(pruf_nr)} · {esc(ts_display)}</div>
</div></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# ACTOR MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    async with Actor:
        t_start    = time.monotonic()
        inp        = await Actor.get_input() or {}
        now_utc    = datetime.now(timezone.utc)
        now_berlin = now_utc.astimezone(TZ_BERLIN)
        ts_display = now_berlin.strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)")
        pruf_nr    = f"SAP-{now_berlin.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

        Actor.log.info(f"▶ StromAudit Pro v{REPORT_VERSION} | {ts_display} | Run: {pruf_nr}")

        # ── Validatie ──────────────────────────────────────────────────────
        try:
            data = validiere_input(inp)
        except ValidationError as ve:
            Actor.log.error(f"Validatiefout: {ve}")
            err_html = generiere_fehler_html(str(ve), pruf_nr, ts_display)
            await Actor.set_value("audit_report.html", err_html, content_type="text/html")
            err_result = {
                "status":     "INPUT_ERROR",
                "pruefnummer": pruf_nr,
                "fehler":     str(ve),
            }
            await Actor.push_data(err_result)
            await Actor.set_value("OUTPUT", err_result)
            return

        warnings = data.pop("_warnings", [])
        for w in warnings:
            Actor.log.warning(f"Input-waarschuwing: {w}")

        Actor.log.info(
            f"   PLZ: {data['plz']} | "
            f"{de_kwh(data['jahresverbrauch_kwh'])} kWh | "
            f"{de_num(data['spitzenlast_kw'],1)} kW | "
            f"§9b: {data['is_producing']}"
        )

        # ── PLZ-lookup ─────────────────────────────────────────────────────
        plz_db = load_plz_data()
        netz   = get_netz_info(data["plz"], plz_db)
        Actor.log.info(f"   Netzbetreiber: {netz['operator']} ({netz.get('bundesland','?')})")

        # ── SMARD marktprijs (met caching + circuit-breaker) ───────────────
        Actor.log.info("   SMARD marktprijs ophalen…")
        markt = await get_smard_price()
        Actor.log.info(
            f"   Prijs: {de_num(markt['raw_eur_mwh'],2)} EUR/MWh | "
            f"Fallback: {markt['is_fallback']} | Cache: {markt.get('cached', False)}"
        )

        # ── Berekeningen ───────────────────────────────────────────────────
        kalk = berechne_stromkosten(data, markt["price_eur_kwh"], netz)
        esg  = berechne_esg(data["jahresverbrauch_kwh"])
        comp = pruefe_compliance(
            data["jahresverbrauch_kwh"],
            data["spitzenlast_kw"],
            data["is_producing"],
        )

        # ── Rapport-hash ───────────────────────────────────────────────────
        rapport_hash = bereken_rapport_hash(data, kalk, esg, pruf_nr)
        Actor.log.info(f"   Hash: {rapport_hash[:16]}…")

        # ── Laagtijd ───────────────────────────────────────────────────────
        runtime_ms = round((time.monotonic() - t_start) * 1000)

        # ── HTML rapport ───────────────────────────────────────────────────
        Actor.log.info("   HTML rapport genereren…")
        html_report = generiere_html(
            data, markt, netz, kalk, esg, comp,
            pruf_nr, rapport_hash, now_berlin, warnings, runtime_ms,
        )
        await Actor.set_value("audit_report.html", html_report, content_type="text/html")

        # ── PPE ────────────────────────────────────────────────────────────
        try:
            await Actor.charge(event_name="audit-report", count=1)
        except Exception:
            pass

        # ── Dataset & OUTPUT ───────────────────────────────────────────────
        store_id   = Actor.get_env().get("default_key_value_store_id", "")
        report_url = (
            f"https://api.apify.com/v2/key-value-stores/{store_id}/records/audit_report.html"
            if store_id else "—"
        )

        result = {
            "pruefnummer":      pruf_nr,
            "rapport_hash":     rapport_hash,
            "erstellt_berlin":  ts_display,
            "erstellt_utc":     now_utc.isoformat(timespec="seconds"),
            "berichtsjahr":     data["berichtsjahr"],
            "status":           "AUDIT_READY",
            "runtime_ms":       runtime_ms,
            "plz":              data["plz"],
            "bundesland":       netz.get("bundesland", "—"),
            "netzbetreiber":    netz["operator"],
            "unternehmen":      data["unternehmen"],
            "projekt_referenz": data["projekt_referenz"],
            "eingabe": {
                "jahresverbrauch_kwh":    data["jahresverbrauch_kwh"],
                "spitzenlast_kw":         data["spitzenlast_kw"],
                "messstellenbetrieb_eur": data["messstellenbetrieb_eur"],
                "is_producing":           data["is_producing"],
            },
            "marktdaten": {
                "dayahead_eur_mwh":  markt["raw_eur_mwh"],
                "dayahead_eur_kwh":  markt["price_eur_kwh"],
                "avg_7d_eur_kwh":    markt["avg_7d_eur_kwh"],
                "timestamp_berlin":  markt["timestamp_berlin"],
                "quelle":            markt["source"],
                "is_fallback":       markt["is_fallback"],
                "cached":            markt.get("cached", False),
            },
            "kalkulation": {
                "arbeitspreis_ct_kwh":   round(kalk["arbeitspreis_netto_eur_kwh"] * 100, 4),
                "netto_gesamt_eur":      kalk["netto_gesamt_eur"],
                "brutto_gesamt_eur":     kalk["brutto_gesamt_eur"],
                "leistungskosten_eur":   kalk["leistungskosten_netto_eur"],
                "entlastung_9b_eur":     kalk["stromsteuer_entlastung_9b_eur"],
                "stromnev_kategorie":    kalk["stromnev_kategorie"],
                "aufschluesselung":      kalk["aufschluesselung"],
                "config_verwendet":      {
                    "marge_pct":             CONFIG["beschaffungs_marge"] * 100,
                    "leistungspreis_eur_kw": CONFIG["leistungspreis_eur_kw"],
                    "mwst":                  CONFIG["mwst"] * 100,
                },
            },
            "esg": {
                "co2_kg":          esg["co2_footprint_kg"],
                "co2_tonnen":      esg["co2_footprint_tonnen"],
                "scope":           esg["scope"],
                "emissionsfaktor": esg["emissionsfaktor_g_co2_kwh"],
                "csrd_relevant":   esg["csrd_relevant"],
            },
            "compliance_score":    comp["compliance_score"],
            "next_steps_count":    len(comp["next_steps"]),
            "eingabe_warnungen":   warnings,
            "report_url":          report_url,
            "tool_version":        f"StromAudit Pro v{REPORT_VERSION}",
        }

        await Actor.push_data(result)
        await Actor.set_value("OUTPUT", result)

        Actor.log.info(
            f"✅ Klaar | {runtime_ms}ms | "
            f"Brutto: {de_eur(kalk['brutto_gesamt_eur'])} € | "
            f"CO₂: {de_num(esg['co2_footprint_tonnen'],2)} t | "
            f"Score: {comp['compliance_score']}/100"
        )


if __name__ == "__main__":
    asyncio.run(main())
