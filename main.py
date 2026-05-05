"""
StromAudit Pro – Apify Actor (On-Demand Rapport)
Deutsche Energie-Compliance & ESG Pre-Audit Engine
Version 3.0 | 2026

Rechtsgrundlagen: EnWG, StromStG §9b, KWKG, StromNEV §19, EEG 2023,
KAV, UStG §12, EU CSRD 2022/2464, ESRS E1, GHG Protocol, ISO 50001:2018,
DIN EN 16247-1, §8 EDL-G, EU Taxonomy Regulation 2020/852

Datenquelle Marktpreise: SMARD – Bundesnetzagentur (öffentliche REST-API,
keine Authentifizierung erforderlich, freie Nachnutzung gemäß §13 DLG).
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx
from apify import Actor

# ─────────────────────────────────────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
REPORT_VERSION    = "3.0"
TZ_BERLIN         = ZoneInfo("Europe/Berlin")
SMARD_FILTER_DA   = 4169          # EPEX Spot Day-Ahead DE
SMARD_BASE        = "https://www.smard.de/app/chart_data"
SMARD_TIMEOUT_S   = 15
SMARD_MAX_RETRIES = 3
SMARD_RETRY_DELAY = 2.0           # seconden tussen retries
MAX_RUNS_GUARD    = 50            # zachte waarschuwing bij meer dan N gelijktijdige aanroepen

TARIFE_2026 = {
    "kwkg_umlage":           0.00446,
    "offshore_umlage":       0.00941,
    "stromnev19_a":          0.01559,
    "stromnev19_b":          0.00050,
    "stromnev19_c":          0.00025,
    "stromsteuer":           0.02050,
    "stromsteuer_9b":        0.00050,
    "mwst":                  0.19,
    "konz_sonder":           0.0011,
    "leistungspreis_eur_kw": 80.0,
    "co2_faktor_g_kwh":      367.0,
}

GESETZ_REFS = [
    "EnWG (Energiewirtschaftsgesetz)",
    "StromStG §9b (Spitzenausgleich prod. Gewerbe)",
    "KWKG 2016/2020 (Kraft-Wärme-Kopplungsgesetz)",
    "StromNEV §19 Abs.2 (Aufschlag bes. Netznutzung)",
    "EEG 2023 (Erneuerbare-Energien-Gesetz)",
    "KAV §2 (Konzessionsabgabenverordnung)",
    "UStG §12 (Mehrwertsteuer 19%)",
    "EU CSRD 2022/2464 / ESRS E1 (Klimawandel, Scope 2)",
    "GHG Protocol Corporate Standard (Scope 1/2/3)",
    "ISO 50001:2018 (Energiemanagementsystem)",
    "DIN EN 16247-1 (Energieaudits)",
    "§8 EDL-G (Energiedienstleistungsgesetz)",
    "EU Taxonomy Regulation 2020/852 Art.8",
    "IFEU/UBA Emissionsfaktoren Strommix Deutschland 2025",
]


# ─────────────────────────────────────────────────────────────────────────────
# INPUT VALIDATIE
# ─────────────────────────────────────────────────────────────────────────────
class ValidationError(Exception):
    pass

def validiere_input(inp: dict) -> dict:
    """Validiert und normalisiert alle Eingabefelder. Wirft ValidationError bei kritischen Fehlern."""
    errors   = []
    warnings = []

    # PLZ
    plz_raw = str(inp.get("plz", "")).strip()
    if not plz_raw:
        errors.append("'plz' (Postleitzahl) ist ein Pflichtfeld und fehlt.")
    elif not plz_raw.isdigit() or len(plz_raw) != 5:
        errors.append(f"'plz' muss eine 5-stellige Zahl sein. Eingabe: '{plz_raw}'")
        plz_raw = "00000"
    plz = plz_raw.zfill(5)

    # Jahresverbrauch
    try:
        kwh = float(inp.get("jahresverbrauch_kwh", 0))
        if kwh <= 0:
            errors.append("'jahresverbrauch_kwh' muss größer als 0 sein.")
        elif kwh < 1000:
            warnings.append(f"Sehr niedriger Jahresverbrauch ({kwh:.0f} kWh). Bitte prüfen.")
        elif kwh > 100_000_000:
            errors.append(f"'jahresverbrauch_kwh' erscheint unrealistisch hoch ({kwh:,.0f} kWh). Maximum: 100.000.000.")
    except (TypeError, ValueError):
        errors.append("'jahresverbrauch_kwh' muss eine Zahl sein.")
        kwh = 0.0

    # Spitzenlast
    try:
        kw = float(inp.get("spitzenlast_kw", 0))
        if kw < 0:
            errors.append("'spitzenlast_kw' darf nicht negativ sein.")
        elif kw == 0:
            warnings.append("'spitzenlast_kw' ist 0 kW – Leistungskosten werden mit 0 berechnet.")
        elif kw > 100_000:
            errors.append(f"'spitzenlast_kw' erscheint unrealistisch hoch ({kw:,.0f} kW).")
        # Plausibilitätscheck: Verhältnis Spitzenlast zu Jahresverbrauch
        if kw > 0 and kwh > 0:
            volllaststunden = kwh / kw
            if volllaststunden < 100:
                warnings.append(
                    f"Niedriges kWh/kW-Verhältnis ({volllaststunden:.0f} Volllaststunden). "
                    f"Bitte Spitzenlast prüfen – Schätzung kann §19-StromNEV-Berechnung beeinflussen."
                )
    except (TypeError, ValueError):
        errors.append("'spitzenlast_kw' muss eine Zahl sein.")
        kw = 0.0

    # Messstellenbetrieb
    try:
        msb = float(inp.get("messstellenbetrieb_eur", 250.0))
        if msb < 0:
            warnings.append("'messstellenbetrieb_eur' ist negativ – wird auf 0 gesetzt.")
            msb = 0.0
        elif msb > 50_000:
            warnings.append(f"'messstellenbetrieb_eur' erscheint sehr hoch ({msb:,.2f} €). Bitte prüfen.")
    except (TypeError, ValueError):
        warnings.append("'messstellenbetrieb_eur' ungültig – Standardwert 250 € wird verwendet.")
        msb = 250.0

    # Booleans & Strings
    is_producing = bool(inp.get("is_producing", False))
    unternehmen  = str(inp.get("unternehmen", "")).strip() or "Nicht angegeben"
    anschrift    = str(inp.get("anschrift",   "")).strip() or "Nicht angegeben"
    try:
        berichtsjahr = int(inp.get("berichtsjahr", datetime.now().year))
        if not (2015 <= berichtsjahr <= 2035):
            warnings.append(f"'berichtsjahr' {berichtsjahr} außerhalb des erwarteten Bereichs (2015–2035).")
    except (TypeError, ValueError):
        warnings.append("'berichtsjahr' ungültig – aktuelles Jahr wird verwendet.")
        berichtsjahr = datetime.now().year

    if errors:
        raise ValidationError("\n".join(errors))

    return {
        "plz": plz,
        "jahresverbrauch_kwh": kwh,
        "spitzenlast_kw": kw,
        "messstellenbetrieb_eur": msb,
        "is_producing": is_producing,
        "unternehmen": unternehmen,
        "anschrift": anschrift,
        "berichtsjahr": berichtsjahr,
        "_warnings": warnings,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PLZ-LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
def load_plz_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "plz_data.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_netz_info(plz: str, plz_db: dict) -> dict:
    return plz_db.get(plz[:2], plz_db["default"])


# ─────────────────────────────────────────────────────────────────────────────
# SMARD LIVE MARKTPREIS (Bundesnetzagentur – öffentliche REST-API)
# ─────────────────────────────────────────────────────────────────────────────
async def get_smard_price() -> dict:
    """
    Ruft den aktuellen Day-Ahead Marktpreis von der SMARD-API der
    Bundesnetzagentur ab (https://www.smard.de).
    SMARD ist eine offizielle, kostenlose REST-API – kein Scraping.
    Freie Nachnutzung gemäß §13 Datennutzungslizenz Deutschland (DL-DE/BY-2-0).
    Retry-Logik: 3 Versuche mit 2 Sekunden Pause.
    """
    headers = {
        "User-Agent": "StromAudit-Pro/3.0 (Energie-Compliance-Tool; contact via Apify Store)",
        "Accept": "application/json",
    }

    for attempt in range(1, SMARD_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=SMARD_TIMEOUT_S,
                headers=headers,
                follow_redirects=True,
            ) as client:
                # Stap 1: index ophalen
                idx_url = f"{SMARD_BASE}/{SMARD_FILTER_DA}/DE/index_hour.json"
                r_idx = await client.get(idx_url)
                r_idx.raise_for_status()
                timestamps = sorted(r_idx.json().get("timestamps", []))
                if not timestamps:
                    raise ValueError("Keine Timestamps in SMARD-Index.")

                # Stap 2: laatste data-blok ophalen
                latest_ts = timestamps[-1]
                data_url = (
                    f"{SMARD_BASE}/{SMARD_FILTER_DA}/DE/"
                    f"{SMARD_FILTER_DA}_DE_hour_{latest_ts}.json"
                )
                r_data = await client.get(data_url)
                r_data.raise_for_status()
                series = r_data.json().get("series", [])

                valid = [(ts, v) for ts, v in series if v is not None]
                if not valid:
                    raise ValueError("Keine gültigen Preisdaten in SMARD-Serie.")

                last_ts_ms, last_price_mwh = valid[-1]
                price_kwh  = round(last_price_mwh / 1000, 5)
                price_dt   = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)

                # 7-daags gemiddelde (168 uur)
                last_7d    = [v for _, v in valid[-168:]]
                avg_7d_kwh = round(sum(last_7d) / len(last_7d) / 1000, 5) if last_7d else price_kwh

                return {
                    "price_eur_kwh":    price_kwh,
                    "avg_7d_eur_kwh":   avg_7d_kwh,
                    "raw_eur_mwh":      last_price_mwh,
                    "timestamp_utc":    price_dt.isoformat(),
                    "timestamp_berlin": price_dt.astimezone(TZ_BERLIN).strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)"),
                    "source":           "SMARD – Bundesnetzagentur (EPEX Spot Day-Ahead DE)",
                    "is_fallback":      False,
                    "data_points_7d":   len(last_7d),
                }

        except Exception as e:
            Actor.log.warning(f"SMARD Versuch {attempt}/{SMARD_MAX_RETRIES}: {e}")
            if attempt < SMARD_MAX_RETRIES:
                await asyncio.sleep(SMARD_RETRY_DELAY)

    # Fallback na alle retries
    Actor.log.warning("SMARD nicht erreichbar – Fallback-Preis wird verwendet.")
    return {
        "price_eur_kwh":    0.0893,
        "avg_7d_eur_kwh":   0.0893,
        "raw_eur_mwh":      89.30,
        "timestamp_utc":    datetime.now(timezone.utc).isoformat(),
        "timestamp_berlin": datetime.now(TZ_BERLIN).strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)"),
        "source":           "Fallback (Ø DE 2025: 89,30 EUR/MWh) – SMARD temporär nicht verfügbar",
        "is_fallback":      True,
        "data_points_7d":   0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# BERECHNUNGSMOTOR
# ─────────────────────────────────────────────────────────────────────────────
def berechne_stromkosten(data: dict, marktpreis_eur_kwh: float, netz_info: dict) -> dict:
    t   = TARIFE_2026
    kwh = data["jahresverbrauch_kwh"]
    kw  = data["spitzenlast_kw"]
    msb = data["messstellenbetrieb_eur"]
    prod = data["is_producing"]

    net_var   = netz_info["net_var"]
    konz_abg  = netz_info["konz_abg"] if kwh < 30_000 else t["konz_sonder"]

    # §19 StromNEV categorie
    if kwh > 1_000_000 and prod:
        stromnev, stromnev_kat = t["stromnev19_c"], "C (prod. Gewerbe, >1 Mio kWh)"
    elif kwh > 1_000_000:
        stromnev, stromnev_kat = t["stromnev19_b"], "B (>1 Mio kWh)"
    else:
        stromnev, stromnev_kat = t["stromnev19_a"], "A (≤1 Mio kWh)"

    stromsteuer   = t["stromsteuer_9b"] if prod else t["stromsteuer"]
    entlastung_9b = (t["stromsteuer"] - stromsteuer) * kwh if prod else 0.0

    beschaffung          = marktpreis_eur_kwh * 1.10
    arbeitspreis_netto   = beschaffung + net_var + konz_abg + t["kwkg_umlage"] + t["offshore_umlage"] + stromnev + stromsteuer
    jahresarbeit_netto   = arbeitspreis_netto * kwh
    leistungskosten      = kw * t["leistungspreis_eur_kw"]
    netto_gesamt         = jahresarbeit_netto + leistungskosten + msb
    mwst_betrag          = netto_gesamt * t["mwst"]
    brutto_gesamt        = netto_gesamt + mwst_betrag

    aufsch = {
        "beschaffung_vertrieb":   round(beschaffung * kwh, 2),
        "netzentgelt_variabel":   round(net_var * kwh, 2),
        "konzessionsabgabe":      round(konz_abg * kwh, 2),
        "kwkg_umlage":            round(t["kwkg_umlage"] * kwh, 2),
        "offshore_umlage":        round(t["offshore_umlage"] * kwh, 2),
        "stromnev_aufschlag":     round(stromnev * kwh, 2),
        "stromsteuer":            round(stromsteuer * kwh, 2),
        "leistungskosten":        round(leistungskosten, 2),
        "messstellenbetrieb":     round(msb, 2),
        "mwst_19pct":             round(mwst_betrag, 2),
    }
    anteile = {k: round(v / brutto_gesamt * 100, 2) if brutto_gesamt else 0 for k, v in aufsch.items()}

    return {
        "arbeitspreis_netto_eur_kwh":      round(arbeitspreis_netto, 5),
        "jahresarbeitspreis_netto_eur":    round(jahresarbeit_netto, 2),
        "leistungskosten_netto_eur":       round(leistungskosten, 2),
        "messstellenbetrieb_eur":          round(msb, 2),
        "netto_gesamt_eur":                round(netto_gesamt, 2),
        "mwst_eur":                        round(mwst_betrag, 2),
        "brutto_gesamt_eur":               round(brutto_gesamt, 2),
        "stromsteuer_entlastung_9b_eur":   round(entlastung_9b, 2),
        "stromnev_kategorie":              stromnev_kat,
        "aufschluesselung":                aufsch,
        "anteile_pct":                     anteile,
        "tarife": {
            "marktpreis_eur_kwh":          round(marktpreis_eur_kwh, 5),
            "beschaffung_inkl_marge":      round(beschaffung, 5),
            "netzentgelt_variabel":        net_var,
            "konzessionsabgabe":           konz_abg,
            "kwkg_umlage":                 t["kwkg_umlage"],
            "offshore_umlage":             t["offshore_umlage"],
            "stromnev19_umlage":           stromnev,
            "stromsteuer":                 stromsteuer,
            "leistungspreis_eur_kw":       t["leistungspreis_eur_kw"],
            "mwst_satz":                   t["mwst"],
        },
    }


def berechne_esg(kwh: float) -> dict:
    co2_kg    = round(kwh * TARIFE_2026["co2_faktor_g_kwh"] / 1000, 1)
    co2_t     = round(co2_kg / 1000, 3)
    intensity = round(TARIFE_2026["co2_faktor_g_kwh"] / 1000, 4)
    return {
        "scope":                    "Scope 2 – Location-based (GHG Protocol Corporate Standard)",
        "norm":                     "ESRS E1 / ISO 14064-1 / GHG Protocol",
        "emissionsfaktor_g_co2_kwh": TARIFE_2026["co2_faktor_g_kwh"],
        "quelle":                   "IFEU / Umweltbundesamt (UBA) – Strommix Deutschland 2025",
        "co2_footprint_kg":         co2_kg,
        "co2_footprint_tonnen":     co2_t,
        "intensitaetsrate_t_co2_mwh": intensity,
        "csrd_relevant":            True,
        "esrs_datenpunkte":         ["E1-4 (Energieverbrauch)", "E1-5 (Scope-2-Emissionen)", "E1-6 (Intensitätsrate)"],
        "eu_taxonomy":              "Zu prüfen (Art. 8 EU-Taxonomie-VO 2020/852)",
    }


def pruefe_compliance(kwh: float, kw: float, prod: bool) -> dict:
    heute       = datetime.now(TZ_BERLIN)
    checks      = []
    next_steps  = []

    # EDL-G §8
    edlg = kwh > 100_000
    checks.append({
        "norm": "§8 EDL-G", "titel": "Energieaudit-Pflicht (alle 4 Jahre, Nicht-KMU)",
        "relevant": edlg,
        "status": "⚠️ Pflicht prüfen" if edlg else "○ Nicht betroffen",
        "empfehlung": "DIN EN 16247-1 Audit beauftragen" if edlg else None,
    })
    if edlg:
        next_steps.append({"aktion": "Energieaudit-Angebot einholen (DIN EN 16247-1)", "frist": "Innerhalb 6 Monate"})

    # ISO 50001
    iso = kwh > 500_000
    checks.append({
        "norm": "ISO 50001:2018", "titel": "Energiemanagementsystem",
        "relevant": iso,
        "status": "⚠️ Stark empfohlen" if iso else "○ Empfohlen",
        "empfehlung": "EnMS nach ISO 50001 implementieren" if iso else None,
    })

    # §9b StromStG
    checks.append({
        "norm": "§9b StromStG", "titel": "Stromsteuervergünstigung prod. Gewerbe",
        "relevant": prod,
        "status": "✅ Angewendet" if prod else "○ Nicht aktiviert",
        "empfehlung": "Antrag beim Hauptzollamt (HZA) auf Jahresausgleich" if prod else "Prüfen ob §9b anwendbar",
    })
    if prod:
        jahresende = datetime(heute.year, 12, 31).strftime("%d.%m.%Y")
        next_steps.append({"aktion": "§9b StromStG Jahresausgleich beim zuständigen HZA beantragen", "frist": f"Bis {jahresende}"})

    # §19 StromNEV
    stromnev = kw >= 30 and kwh >= 30_000
    checks.append({
        "norm": "§19 Abs.2 StromNEV", "titel": "Individuelle Netzentgelte (ab 30 kW + 30.000 kWh)",
        "relevant": stromnev,
        "status": "⚠️ Prüfung empfohlen" if stromnev else "○ Nicht relevant",
        "empfehlung": "Antrag auf individuelle Netzentgelte beim Netzbetreiber stellen" if stromnev else None,
    })
    if stromnev:
        q_end = datetime(heute.year, (((heute.month - 1) // 3) + 1) * 3, 1) + timedelta(days=31)
        next_steps.append({"aktion": "Antrag individuelle Netzentgelte beim Netzbetreiber einreichen", "frist": f"Nächstes Quartal (ca. {q_end.strftime('%m/%Y')})"})

    # CSRD/ESRS E1
    csrd = kwh > 500_000
    checks.append({
        "norm": "EU CSRD / ESRS E1", "titel": "Nachhaltigkeitsberichterstattung Klimawandel",
        "relevant": True,
        "status": "✅ Scope-2-Daten bereitgestellt" if csrd else "○ Freiwillige Nutzung empfohlen",
        "empfehlung": "Scope-2-Wert in ESRS E1 Nachhaltigkeitsbericht integrieren",
    })
    if csrd:
        next_steps.append({"aktion": "Scope-2-Emissionen in CSRD/ESRS E1 Bericht aufnehmen", "frist": f"Berichtsjahr {heute.year + 1}"})

    # KAV
    checks.append({
        "norm": "§2 KAV", "titel": "Konzessionsabgabe Sondervertragskunden",
        "relevant": stromnev,
        "status": "✅ Sondervertragstarif angewendet" if stromnev else "○ Haushaltstarif-Satz",
        "empfehlung": None,
    })

    score = min(100, sum(20 for c in checks if c["relevant"]))
    return {"checks": checks, "next_steps": next_steps, "compliance_score": score}


# ─────────────────────────────────────────────────────────────────────────────
# HTML RAPPORT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def generiere_html(
    data: dict, markt: dict, netz: dict,
    kalk: dict, esg: dict, comp: dict,
    pruf_nr: str, now_berlin: datetime,
    warnings: list,
) -> str:

    kwh  = data["jahresverbrauch_kwh"]
    kw   = data["spitzenlast_kw"]
    prod = data["is_producing"]
    t    = kalk["tarife"]
    aufsch = kalk["aufschluesselung"]

    ts_display = now_berlin.strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)")

    # Warn-blok
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{w}</li>" for w in warnings)
        warn_html = f'<div class="box yellow"><strong>⚠️ Hinweise zur Dateneingabe:</strong><ul style="margin:6px 0 0 18px">{items}</ul></div>'

    # Fallback-waarschuwing
    fallback_html = ""
    if markt.get("is_fallback"):
        fallback_html = (
            '<div class="box red"><strong>⚠️ Marktpreis: Fallback-Wert</strong> – '
            'SMARD (Bundesnetzagentur) war zum Zeitpunkt der Berechnung temporär nicht erreichbar. '
            'Der Durchschnittswert 89,30 EUR/MWh (Ø DE 2025) wurde verwendet. '
            'Bitte Bericht bei Bedarf erneut abrufen.</div>'
        )

    # Kostenaufschlüsselung rijen
    labels = {
        "beschaffung_vertrieb":  f"Strombeschaffung & Vertrieb (Day-Ahead + 10% Marge)",
        "netzentgelt_variabel":  f"Netzentgelt variabel – {netz['operator']} (§21 EnWG)",
        "konzessionsabgabe":     "Konzessionsabgabe (§2 KAV)",
        "kwkg_umlage":           "KWKG-Umlage 2026",
        "offshore_umlage":       "Offshore-Netzumlage 2026 (§17f EnWG)",
        "stromnev_aufschlag":    f"§19 StromNEV 2026 – Kategorie {kalk['stromnev_kategorie'][:1]}",
        "stromsteuer":           "Stromsteuer (§3 StromStG)" + (" + §9b Entlastung" if prod else ""),
        "leistungskosten":       f"Leistungskosten ({kw} kW × {t['leistungspreis_eur_kw']:.0f} €/kW/Jahr)",
        "messstellenbetrieb":    "Messstellenbetrieb (§21b EnWG)",
        "mwst_19pct":            "Mehrwertsteuer 19% (§12 UStG)",
    }
    aufsch_rows = ""
    for k, label in labels.items():
        v   = aufsch.get(k, 0)
        pct = kalk["anteile_pct"].get(k, 0)
        aufsch_rows += f"<tr><td>{label}</td><td class='r'>{v:,.2f} €</td><td class='r'>{pct:.1f}%</td></tr>"

    # Compliance rijen
    comp_rows = ""
    for c in comp["checks"]:
        emp = f"<br><em style='font-size:11px'>{c['empfehlung']}</em>" if c["empfehlung"] else ""
        comp_rows += f"<tr><td><strong>{c['norm']}</strong> – {c['titel']}</td><td>{c['status']}{emp}</td></tr>"

    # Next Steps rijen
    ns_rows = ""
    for ns in comp["next_steps"]:
        ns_rows += f"<tr><td>{ns['aktion']}</td><td class='r'><strong>{ns['frist']}</strong></td></tr>"
    ns_block = ""
    if ns_rows:
        ns_block = f"""
        <h3>📅 Empfohlene nächste Schritte</h3>
        <table><thead><tr><th>Maßnahme</th><th>Empfohlene Frist</th></tr></thead>
        <tbody>{ns_rows}</tbody></table>"""

    # §9b blok
    blok_9b = ""
    if prod and kalk["stromsteuer_entlastung_9b_eur"] > 0:
        blok_9b = f"""
        <div class="box green">
          <strong>§9b StromStG – Vergünstigung Produzierendes Gewerbe</strong><br>
          Angewendeter Steuersatz: <strong>0,05 ct/kWh</strong> (statt 2,05 ct/kWh Regelsatz)<br>
          Steuerliche Entlastung: <strong>{kalk['stromsteuer_entlastung_9b_eur']:,.2f} €/Jahr</strong><br>
          <small>Jahresausgleich beim zuständigen Hauptzollamt beantragen (§9b Abs.2a StromStG). Frist: 31. Dezember des laufenden Jahres.</small>
        </div>"""

    # §19 blok
    blok_19 = ""
    if kw >= 30 and kwh >= 30_000:
        blok_19 = f"""
        <div class="box blue">
          <strong>§19 Abs.2 StromNEV – Individuelle Netzentgelte prüfen</strong><br>
          Spitzenlast {kw} kW und Jahresverbrauch {kwh:,.0f} kWh erfüllen die Grundvoraussetzungen
          für reduzierte Netzentgelte beim Netzbetreiber <strong>{netz['operator']}</strong>.<br>
          <small>Antrag beim Netzbetreiber stellen. Einsparungspotenzial kann erheblich sein.</small>
        </div>"""

    # Spitzenlast disclaimer
    spitzenlast_hinweis = f"""
    <div class="box yellow">
      <strong>⚠️ Hinweis zur Spitzenlast (Jahresleistungsmaximum)</strong><br>
      Die angegebene Spitzenlast von <strong>{kw} kW</strong> ist ein entscheidender Parameter.
      Schwankungen der tatsächlichen Spitzenlast im Jahresverlauf können die berechneten
      Leistungskosten und die §19-StromNEV-Umlage erheblich beeinflussen.
      Grundlage sollte stets die <em>gemessene Jahreshöchstlast</em> aus der Lastgangmessung sein,
      nicht eine Schätzung. Bei Unsicherheit: Netzbetreiber oder Energieberater konsultieren.
    </div>"""

    # Gesetzesrefs
    gesetze = "".join(f"<li>{g}</li>" for g in GESETZ_REFS)

    # ESG datapunten
    esrs_pts = ", ".join(esg["esrs_datenpunkte"])

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StromAudit Pro – {pruf_nr}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',Arial,sans-serif;font-size:13px;color:#1a1a1a;background:#f0f2f7}}
.wrap{{max-width:980px;margin:0 auto;background:#fff;box-shadow:0 2px 24px rgba(0,0,0,.13)}}

/* PRINT KNOP */
.print-bar{{background:#0a2540;padding:10px 48px;display:flex;align-items:center;justify-content:space-between}}
.print-bar span{{color:rgba(255,255,255,.7);font-size:12px}}
.btn-print{{background:#f0a500;color:#fff;border:none;padding:9px 22px;border-radius:4px;
  font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.3px}}
.btn-print:hover{{background:#d4920a}}

/* HEADER */
.hdr{{background:linear-gradient(135deg,#0a2540 0%,#1a4a7a 100%);color:#fff;padding:32px 48px 24px}}
.hdr h1{{font-size:24px;font-weight:700;letter-spacing:.4px}}
.hdr .sub{{font-size:12px;opacity:.75;margin-top:3px}}
.hdr-meta{{display:flex;flex-wrap:wrap;gap:24px;margin-top:18px;font-size:11px;opacity:.85}}
.hdr-meta div{{display:flex;flex-direction:column;gap:2px}}
.hdr-meta strong{{font-size:12px;color:#7ecef4}}
.badge-audit{{display:inline-block;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);
  border-radius:4px;padding:4px 12px;font-size:11px;margin-top:12px;letter-spacing:.3px}}

/* DISCLAIMER BAR */
.disc-bar{{background:#fffbe6;border-left:5px solid #f0a500;padding:10px 48px;font-size:11px;color:#6b4c00;line-height:1.6}}

/* SECTIONS */
.sec{{padding:24px 48px;border-bottom:1px solid #eaedf3}}
.sec h2{{font-size:14px;font-weight:700;color:#0a2540;text-transform:uppercase;
  letter-spacing:.6px;border-bottom:2px solid #0a2540;padding-bottom:5px;margin-bottom:16px}}
.sec h3{{font-size:13px;color:#1a4a7a;margin:18px 0 10px;font-weight:600}}

/* KPI GRID */
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:4px}}
.kpi{{background:#f0f4ff;border:1px solid #c5d5f0;border-radius:7px;padding:13px 15px}}
.kpi .lbl{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.4px}}
.kpi .val{{font-size:20px;font-weight:700;color:#0a2540;margin:3px 0 2px}}
.kpi .sub{{font-size:10px;color:#777}}
.kpi.grn{{background:#e8f8ee;border-color:#82c99a}}.kpi.grn .val{{color:#1a7a3c}}
.kpi.org{{background:#fff3e0;border-color:#ffb74d}}.kpi.org .val{{color:#c75000}}

/* TABLES */
table{{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:4px}}
th{{background:#0a2540;color:#fff;padding:8px 11px;text-align:left;font-size:11px;font-weight:600}}
td{{padding:7px 11px;border-bottom:1px solid #eaecf0;vertical-align:top}}
tr:nth-child(even) td{{background:#f8f9fc}}
td.r{{text-align:right;font-variant-numeric:tabular-nums}}
tfoot td{{background:#0a2540!important;color:#fff;font-weight:700;border:none}}

/* BOXES */
.box{{border-radius:6px;padding:13px 15px;margin:11px 0;font-size:12px;line-height:1.6}}
.box.green{{background:#e8f8ee;border-left:4px solid #1a7a3c}}
.box.blue{{background:#e3f0ff;border-left:4px solid #1a4a7a}}
.box.yellow{{background:#fffbe6;border-left:4px solid #f0a500}}
.box.red{{background:#fff0f0;border-left:4px solid #c0392b}}

/* FOOTER */
.ftr{{background:#0a2540;color:rgba(255,255,255,.65);padding:20px 48px;font-size:10.5px;line-height:1.8}}
.ftr strong{{color:#fff}}
.src{{font-size:10px;color:#999;margin-top:5px;font-style:italic}}

/* PRINT */
@media print{{
  body{{background:#fff}}
  .wrap{{box-shadow:none}}
  .print-bar,.btn-print{{display:none!important}}
  .sec{{padding:14px 24px}}
  .hdr{{padding:18px 24px}}
  .disc-bar{{padding:8px 24px}}
  .ftr{{padding:14px 24px}}
}}
</style>
</head>
<body>
<div class="wrap">

<!-- PRINT KNOP -->
<div class="print-bar">
  <span>📄 Audit-Vorbereitungsbericht · {pruf_nr} · Als PDF speichern oder drucken:</span>
  <button class="btn-print" onclick="window.print()">🖨️ Drucken / Als PDF speichern</button>
</div>

<!-- HEADER -->
<div class="hdr">
  <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px">
    <div>
      <h1>⚡ StromAudit Pro</h1>
      <div class="sub">Deutsche Energie-Compliance &amp; ESG Pre-Audit Engine v{REPORT_VERSION}</div>
      <div class="badge-audit">AUDIT-VORBEREITUNGSBERICHT · PRÜFUNGSBEREIT (PRE-AUDIT)</div>
    </div>
    <div style="text-align:right;font-size:11px;opacity:.8;line-height:1.7">
      <div>Prüfnummer: <strong style="color:#7ecef4">{pruf_nr}</strong></div>
      <div>Erstellt: <strong style="color:#7ecef4">{ts_display}</strong></div>
      <div>Berichtsjahr: <strong style="color:#7ecef4">{data['berichtsjahr']}</strong></div>
    </div>
  </div>
  <div class="hdr-meta">
    <div><span>Standort PLZ</span><strong>{data['plz']} · {netz.get('bundesland','—')}</strong></div>
    <div><span>Netzbetreiber</span><strong>{netz['operator']}</strong></div>
    <div><span>Marktpreis (SMARD live)</span><strong>{markt['raw_eur_mwh']:.2f} EUR/MWh</strong></div>
    <div><span>7-Tage-Ø</span><strong>{markt['avg_7d_eur_kwh']*1000:.2f} EUR/MWh</strong></div>
    <div><span>Datenstand</span><strong>{markt['timestamp_berlin']}</strong></div>
  </div>
</div>

<!-- DISCLAIMER -->
<div class="disc-bar">
  ⚠️ <strong>Audit-Vorbereitungsdokument (Pre-Audit) – Nicht geprüft:</strong>
  Dieser Bericht ist automatisch generiert auf Basis öffentlicher Behördendaten und standardisierter Berechnungsmodelle.
  Er ersetzt keine zertifizierte Energiefachprüfung (DIN EN 16247-1) und keine steuerrechtliche Beratung.
  Die abschließende Validierung obliegt einem zugelassenen Wirtschaftsprüfer (WP/vBP) oder Energieberater (§21 EDL-G).
  Marktpreis: {markt['source']}.
</div>

<!-- 0: WARNINGS & FALLBACK -->
{"<div class='sec'>" + warn_html + fallback_html + "</div>" if (warn_html or fallback_html) else ""}

<!-- 1: STAMMDATEN -->
<div class="sec">
<h2>1 · Stammdaten &amp; Eingabeparameter</h2>
<table>
  <tr><td><strong>Unternehmen</strong></td><td>{data['unternehmen']}</td>
      <td><strong>Anschrift / PLZ</strong></td><td>{data['anschrift']} · {data['plz']}</td></tr>
  <tr><td><strong>Bundesland</strong></td><td>{netz.get('bundesland','—')}</td>
      <td><strong>Netzbetreiber</strong></td><td>{netz['operator']}</td></tr>
  <tr><td><strong>Jahresverbrauch</strong></td><td>{kwh:,.0f} kWh</td>
      <td><strong>Spitzenlast</strong></td><td>{kw} kW</td></tr>
  <tr><td><strong>Messstellenbetrieb</strong></td><td>{data['messstellenbetrieb_eur']:,.2f} €/Jahr</td>
      <td><strong>Prod. Gewerbe §9b StromStG</strong></td><td>{"✅ Ja – Vergünstigung angewendet" if prod else "○ Nein"}</td></tr>
  <tr><td><strong>Berichtsjahr</strong></td><td>{data['berichtsjahr']}</td>
      <td><strong>§19 StromNEV Kategorie</strong></td><td>{kalk['stromnev_kategorie']}</td></tr>
</table>
</div>

<!-- 2: KPI's -->
<div class="sec">
<h2>2 · Energie-Kennzahlen (KPIs)</h2>
<div class="kpi-grid">
  <div class="kpi org"><div class="lbl">Brutto-Jahreskosten</div>
    <div class="val">{kalk['brutto_gesamt_eur']:,.0f} €</div><div class="sub">inkl. 19% MwSt.</div></div>
  <div class="kpi"><div class="lbl">Netto-Jahreskosten</div>
    <div class="val">{kalk['netto_gesamt_eur']:,.0f} €</div><div class="sub">excl. MwSt.</div></div>
  <div class="kpi"><div class="lbl">Arbeitspreis (netto)</div>
    <div class="val">{kalk['arbeitspreis_netto_eur_kwh']*100:.3f} ct</div><div class="sub">pro kWh</div></div>
  <div class="kpi"><div class="lbl">Leistungskosten</div>
    <div class="val">{kalk['leistungskosten_netto_eur']:,.0f} €</div><div class="sub">{kw} kW × {t['leistungspreis_eur_kw']:.0f} €/kW</div></div>
  <div class="kpi grn"><div class="lbl">CO₂-Footprint (Scope 2)</div>
    <div class="val">{esg['co2_footprint_tonnen']:.2f} t</div><div class="sub">CO₂e · ESRS E1 location-based</div></div>
  <div class="kpi grn"><div class="lbl">§9b Entlastung</div>
    <div class="val">{kalk['stromsteuer_entlastung_9b_eur']:,.0f} €</div>
    <div class="sub">{"Angewendet" if prod else "Nicht aktiviert"}</div></div>
  <div class="kpi"><div class="lbl">Day-Ahead Marktpreis</div>
    <div class="val">{markt['raw_eur_mwh']:.1f}</div><div class="sub">EUR/MWh (SMARD live)</div></div>
  <div class="kpi"><div class="lbl">Compliance-Score</div>
    <div class="val">{comp['compliance_score']}</div><div class="sub">/ 100 Punkte</div></div>
</div>
</div>

<!-- 3: KOSTENAUFSCHLÜSSELUNG -->
<div class="sec">
<h2>3 · Vollständige Kostenaufschlüsselung 2026</h2>
{blok_9b}{blok_19}{spitzenlast_hinweis}
<table>
  <thead><tr><th>Kostenkomponente</th><th class="r">EUR/Jahr</th><th class="r">Anteil</th></tr></thead>
  <tbody>{aufsch_rows}</tbody>
  <tfoot><tr>
    <td>GESAMT (Brutto inkl. 19% MwSt.)</td>
    <td class="r">{kalk['brutto_gesamt_eur']:,.2f} €</td>
    <td class="r">100%</td>
  </tr></tfoot>
</table>
<h3>Angewendete Tarifsätze 2026 (gesetzliche Grundlage)</h3>
<table>
  <tr><th>Parameter</th><th>Wert</th><th>Rechtsgrundlage</th></tr>
  <tr><td>Day-Ahead Marktpreis (SMARD)</td><td class="r">{t['marktpreis_eur_kwh']*1000:.2f} EUR/MWh</td><td>EPEX Spot / EnWG §1</td></tr>
  <tr><td>Beschaffung inkl. Vertriebsmarge (10%)</td><td class="r">{t['beschaffung_inkl_marge']*100:.4f} ct/kWh</td><td>Marktüblich</td></tr>
  <tr><td>Netzentgelt variabel</td><td class="r">{t['netzentgelt_variabel']*100:.3f} ct/kWh</td><td>§21 EnWG / BNetzA</td></tr>
  <tr><td>Konzessionsabgabe</td><td class="r">{t['konzessionsabgabe']*100:.3f} ct/kWh</td><td>§2 KAV</td></tr>
  <tr><td>KWKG-Umlage 2026</td><td class="r">{t['kwkg_umlage']*100:.3f} ct/kWh</td><td>KWKG 2016/2020</td></tr>
  <tr><td>Offshore-Netzumlage 2026</td><td class="r">{t['offshore_umlage']*100:.3f} ct/kWh</td><td>§17f EnWG</td></tr>
  <tr><td>§19 StromNEV Aufschlag</td><td class="r">{t['stromnev19_umlage']*100:.3f} ct/kWh</td><td>§19 Abs.2 StromNEV</td></tr>
  <tr><td>Stromsteuer</td><td class="r">{t['stromsteuer']*100:.3f} ct/kWh {"(§9b-Satz)" if prod else "(Regelsatz)"}</td><td>§3 StromStG{" / §9b" if prod else ""}</td></tr>
  <tr><td>Leistungspreis</td><td class="r">{t['leistungspreis_eur_kw']:.2f} €/kW/Jahr</td><td>§21 EnWG</td></tr>
  <tr><td>MwSt.</td><td class="r">{t['mwst_satz']*100:.0f}%</td><td>§12 UStG</td></tr>
</table>
<p class="src">Umlagen-Quelle: Übertragungsnetzbetreiber (ÜNB) – amtliche Veröffentlichung Oktober 2025.
Netzentgelte: Bundesnetzagentur (BNetzA) / regionaler Netzbetreiber. Marktpreis: SMARD Bundesnetzagentur (EPEX Spot Day-Ahead).</p>
</div>

<!-- 4: ESG -->
<div class="sec">
<h2>4 · ESG-Bericht · Scope-2-Emissionen (ESRS E1 / GHG Protocol)</h2>
<div class="kpi-grid">
  <div class="kpi grn"><div class="lbl">CO₂-Fußabdruck (Scope 2, location-based)</div>
    <div class="val">{esg['co2_footprint_tonnen']:.3f} t CO₂e</div>
    <div class="sub">{esg['co2_footprint_kg']:,.0f} kg</div></div>
  <div class="kpi"><div class="lbl">Emissionsfaktor (UBA 2025)</div>
    <div class="val">{esg['emissionsfaktor_g_co2_kwh']:.0f} g</div><div class="sub">CO₂e/kWh</div></div>
  <div class="kpi"><div class="lbl">Intensitätsrate</div>
    <div class="val">{esg['intensitaetsrate_t_co2_mwh']:.4f}</div><div class="sub">t CO₂e/MWh</div></div>
</div>
<table>
  <tr><th>Parameter</th><th>Wert</th></tr>
  <tr><td>Scope &amp; Methodik</td><td>{esg['scope']}</td></tr>
  <tr><td>Norm</td><td>{esg['norm']}</td></tr>
  <tr><td>Emissionsfaktor</td><td>{esg['emissionsfaktor_g_co2_kwh']} g CO₂e/kWh – {esg['quelle']}</td></tr>
  <tr><td>CO₂-Emissionen (Scope 2, location-based)</td><td><strong>{esg['co2_footprint_kg']:,.0f} kg ({esg['co2_footprint_tonnen']:.3f} t CO₂e)</strong></td></tr>
  <tr><td>EU-Taxonomie-Relevanz</td><td>{esg['eu_taxonomy']}</td></tr>
  <tr><td>CSRD/ESRS-Datenpunkte</td><td>{esrs_pts}</td></tr>
</table>
<div class="box yellow" style="margin-top:12px">
  <strong>Hinweis für CSRD/ESRS E1-Berichterstattung:</strong><br>
  Dieser Scope-2-Wert ist location-based (Strommix Deutschland 2025).
  Für vollständige CSRD-Konformität ist zusätzlich ein marktbasierter Wert
  (Herkunftsnachweise / Guarantees of Origin) zu ermitteln.
  Die Endvalidierung obliegt einem zugelassenen Wirtschaftsprüfer (WP/vBP).
</div>
</div>

<!-- 5: COMPLIANCE -->
<div class="sec">
<h2>5 · Compliance-Checkliste &amp; Handlungsempfehlungen</h2>
<table>
  <thead><tr><th style="width:58%">Norm / Vorschrift</th><th>Status &amp; Empfehlung</th></tr></thead>
  <tbody>{comp_rows}</tbody>
</table>
{ns_block}
<p class="src" style="margin-top:8px">Alle Angaben basieren auf Richtwerten. Verbindliche Compliance-Bestätigung durch Steuer- oder Energieberater erforderlich.</p>
</div>

<!-- 6: RECHTSGRUNDLAGEN -->
<div class="sec">
<h2>6 · Angewendete Rechtsgrundlagen &amp; Normen</h2>
<ul style="columns:2;column-gap:28px;padding-left:16px;line-height:2;font-size:12px">{gesetze}</ul>
</div>

<!-- 7: HAFTUNGSAUSSCHLUSS (VOLLSTÄNDIG) -->
<div class="sec">
<h2>7 · Vollständiger Haftungsausschluss &amp; Nutzungsbedingungen</h2>
<div class="box yellow">
<strong>Rechtlicher Status dieses Dokuments</strong><br>
Dieses Dokument ist ein automatisch generierter Audit-Vorbereitungsbericht (Pre-Audit Report).
Es handelt sich ausdrücklich <strong>nicht</strong> um ein geprüftes Gutachten, eine Steuerberatung,
eine Rechtsberatung oder eine Wirtschaftsprüferleistung im Sinne des WPO, StBerG oder RDG.
</div>
<div class="box red">
<strong>Haftungsausschluss des Betreibers</strong><br>
StromAudit Pro ist ein vollautomatischer Datenverarbeitungs- und Berechnungsdienst.
Der Betreiber dieses Dienstes ist ausschließlich <strong>Aggregator öffentlich zugänglicher Behördendaten</strong>
(SMARD/Bundesnetzagentur, Übertragungsnetzbetreiber, Umweltbundesamt, IFEU) und stellt
diese in strukturierter, berechneter Form dar.<br><br>
<strong>Der Betreiber:</strong>
<ul style="margin:6px 0 0 16px;line-height:1.9">
  <li>ist kein Energieberater, Steuerberater, Rechtsanwalt oder Wirtschaftsprüfer</li>
  <li>begründet durch diesen Dienst keine Beratungs-, Auskunfts- oder sonstige Vertragspflicht</li>
  <li>übernimmt <strong>keine Haftung</strong> für die Richtigkeit, Vollständigkeit oder Aktualität der Berechnungsergebnisse</li>
  <li>übernimmt <strong>keine Haftung</strong> für Entscheidungen, die auf Basis dieses Berichts getroffen werden</li>
  <li>übernimmt <strong>keine Haftung</strong> für Schäden jeglicher Art, die direkt oder indirekt aus der Nutzung entstehen</li>
  <li>ist nicht verantwortlich für Änderungen gesetzlicher Tarife, Umlagen oder Steuerregeln nach dem Erstellungsdatum</li>
</ul><br>
Für die Richtigkeit der eingegebenen Verbrauchsdaten (insbesondere Jahresverbrauch und Spitzenlast)
trägt der Auftraggeber / Nutzer die alleinige Verantwortung.<br><br>
Dieser Haftungsausschluss gilt gegenüber allen natürlichen und juristischen Personen,
uneingeschränkt und ohne Ausnahme, in dem nach geltendem Recht maximal zulässigen Umfang.
</div>
<p style="font-size:11px;color:#888;margin-top:10px">
Datenquellen: SMARD Bundesnetzagentur (DL-DE/BY-2-0) · ÜNB (amtlich, öffentlich) ·
UBA/IFEU (öffentlich) · BNetzA (öffentlich) · Eigene Berechnungsmodelle.
Alle Ausgangsdaten sind öffentlich und kostenfrei zugänglich.
</p>
</div>

<!-- FOOTER -->
<div class="ftr">
  <strong>StromAudit Pro v{REPORT_VERSION}</strong> · Prüfnummer: {pruf_nr}<br>
  Erstellt: {ts_display} · Berichtsjahr: {data['berichtsjahr']}<br>
  Marktpreis-Quelle: {markt['source']} | Datenstand: {markt['timestamp_berlin']}<br>
  Alle verwendeten Daten sind öffentlich zugängliche Behördendaten (SMARD, ÜNB, UBA, BNetzA).<br><br>
  <strong>Haftungsausschluss (Kurzform):</strong>
  Automatisch generierter Pre-Audit Bericht. Kein geprüftes Gutachten.
  Keine Haftung für Berechnungsergebnisse oder darauf basierende Entscheidungen.
  Der Betreiber ist ausschließlich Aggregator öffentlicher Behördendaten.
  Endvalidierung durch zugelassenen WP/vBP oder Energieberater (§21 EDL-G) erforderlich.<br>
  © {now_berlin.year} StromAudit Pro
</div>

</div>
<script>
// Automatisch printdialoog openen als URL-parameter ?print=1
if(new URLSearchParams(window.location.search).get('print')==='1') window.print();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# FOUT-RAPPORT (bij validatiefouten)
# ─────────────────────────────────────────────────────────────────────────────
def generiere_fehler_html(errors: str, pruf_nr: str, ts_display: str) -> str:
    error_items = "".join(f"<li>{e}</li>" for e in errors.split("\n") if e.strip())
    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="UTF-8">
<title>StromAudit Pro – Eingabefehler {pruf_nr}</title>
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
<div class="sub">Der Bericht konnte nicht erstellt werden. Bitte prüfen Sie Ihre Eingaben.</div>
<div class="errors"><strong>Folgende Fehler wurden festgestellt:</strong><ul>{error_items}</ul></div>
<div class="hint">
  <strong>Pflichtfelder:</strong><br>
  • <code>plz</code> – 5-stellige deutsche Postleitzahl (z.B. 80331)<br>
  • <code>jahresverbrauch_kwh</code> – Jahresverbrauch in kWh (z.B. 125000)<br>
  • <code>spitzenlast_kw</code> – Spitzenlast in kW (z.B. 45)<br><br>
  Optionale Felder: <code>messstellenbetrieb_eur</code>, <code>is_producing</code>,
  <code>unternehmen</code>, <code>anschrift</code>, <code>berichtsjahr</code>
</div>
<div class="meta">Ref: {pruf_nr} · {ts_display}</div>
</div></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# ACTOR MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    async with Actor:
        inp = await Actor.get_input() or {}
        now_utc    = datetime.now(timezone.utc)
        now_berlin = now_utc.astimezone(TZ_BERLIN)
        ts_display = now_berlin.strftime("%d.%m.%Y %H:%M Uhr (MEZ/MESZ)")
        pruf_nr    = f"SAP-{now_berlin.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"

        Actor.log.info(f"▶ StromAudit Pro v{REPORT_VERSION} gestartet | {ts_display}")

        # ── Validatie ──────────────────────────────────────────────────────
        try:
            data = validiere_input(inp)
        except ValidationError as ve:
            Actor.log.error(f"Eingabefehler: {ve}")
            err_html = generiere_fehler_html(str(ve), pruf_nr, ts_display)
            await Actor.set_value("audit_report.html", err_html, content_type="text/html")
            err_result = {"status": "INPUT_ERROR", "pruefnummer": pruf_nr, "fehler": str(ve)}
            await Actor.push_data(err_result)
            await Actor.set_value("OUTPUT", err_result)
            return  # nette afsluiting, geen crash

        warnings = data.pop("_warnings", [])
        for w in warnings:
            Actor.log.warning(f"Eingabe-Warnung: {w}")

        plz = data["plz"]
        Actor.log.info(f"   PLZ: {plz} | {data['jahresverbrauch_kwh']:,.0f} kWh | {data['spitzenlast_kw']} kW")

        # ── PLZ-lookup ─────────────────────────────────────────────────────
        plz_db = load_plz_data()
        netz   = get_netz_info(plz, plz_db)
        Actor.log.info(f"   Netzbetreiber: {netz['operator']}")

        # ── SMARD marktpreis ───────────────────────────────────────────────
        Actor.log.info("   SMARD Marktpreis abrufen…")
        markt = await get_smard_price()
        Actor.log.info(f"   Marktpreis: {markt['raw_eur_mwh']:.2f} EUR/MWh ({markt['source'][:40]}…)")

        # ── Berekeningen ───────────────────────────────────────────────────
        kalk = berechne_stromkosten(data, markt["price_eur_kwh"], netz)
        esg  = berechne_esg(data["jahresverbrauch_kwh"])
        comp = pruefe_compliance(data["jahresverbrauch_kwh"], data["spitzenlast_kw"], data["is_producing"])

        # ── HTML rapport ───────────────────────────────────────────────────
        Actor.log.info("   HTML Audit-Report generieren…")
        html = generiere_html(data, markt, netz, kalk, esg, comp, pruf_nr, now_berlin, warnings)
        await Actor.set_value("audit_report.html", html, content_type="text/html")

        # ── PPE: laad per rapport ──────────────────────────────────────────
        try:
            await Actor.charge(event_name="audit-report", count=1)
        except Exception:
            pass  # PPE niet geconfigureerd tijdens ontwikkeling – geen crash

        # ── Dataset & OUTPUT ───────────────────────────────────────────────
        store_id   = Actor.get_env().get("default_key_value_store_id", "")
        report_url = (
            f"https://api.apify.com/v2/key-value-stores/{store_id}/records/audit_report.html"
            if store_id else "—"
        )

        result = {
            "pruefnummer":    pruf_nr,
            "erstellt_berlin": ts_display,
            "erstellt_utc":   now_utc.isoformat(timespec="seconds"),
            "berichtsjahr":   data["berichtsjahr"],
            "status":         "AUDIT_READY",
            "plz":            plz,
            "bundesland":     netz.get("bundesland", "—"),
            "netzbetreiber":  netz["operator"],
            "unternehmen":    data["unternehmen"],
            "eingabe": {
                "jahresverbrauch_kwh":  data["jahresverbrauch_kwh"],
                "spitzenlast_kw":       data["spitzenlast_kw"],
                "messstellenbetrieb_eur": data["messstellenbetrieb_eur"],
                "is_producing":         data["is_producing"],
            },
            "marktdaten": {
                "dayahead_eur_mwh":  markt["raw_eur_mwh"],
                "dayahead_eur_kwh":  markt["price_eur_kwh"],
                "avg_7d_eur_kwh":    markt["avg_7d_eur_kwh"],
                "timestamp_berlin":  markt["timestamp_berlin"],
                "quelle":            markt["source"],
                "is_fallback":       markt["is_fallback"],
            },
            "kalkulation": {
                "arbeitspreis_ct_kwh":        round(kalk["arbeitspreis_netto_eur_kwh"] * 100, 4),
                "netto_gesamt_eur":           kalk["netto_gesamt_eur"],
                "brutto_gesamt_eur":          kalk["brutto_gesamt_eur"],
                "leistungskosten_eur":        kalk["leistungskosten_netto_eur"],
                "entlastung_9b_eur":          kalk["stromsteuer_entlastung_9b_eur"],
                "stromnev_kategorie":         kalk["stromnev_kategorie"],
                "aufschluesselung":           kalk["aufschluesselung"],
            },
            "esg": {
                "co2_kg":          esg["co2_footprint_kg"],
                "co2_tonnen":      esg["co2_footprint_tonnen"],
                "scope":           esg["scope"],
                "emissionsfaktor": esg["emissionsfaktor_g_co2_kwh"],
            },
            "compliance_score":      comp["compliance_score"],
            "eingabe_warnungen":     warnings,
            "report_url":            report_url,
            "tool_version":          f"StromAudit Pro v{REPORT_VERSION}",
        }

        await Actor.push_data(result)
        await Actor.set_value("OUTPUT", result)

        Actor.log.info(
            f"✅ Fertig | Brutto: {kalk['brutto_gesamt_eur']:,.2f} € | "
            f"CO₂: {esg['co2_footprint_tonnen']:.2f} t | "
            f"Score: {comp['compliance_score']}/100 | "
            f"Report: {report_url}"
        )


if __name__ == "__main__":
    asyncio.run(main())
