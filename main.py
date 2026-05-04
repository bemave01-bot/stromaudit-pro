"""
StromAudit Pro – Apify Actor
Deutsche Energie-Compliance & ESG Pre-Audit Engine
Version 2.0 | 2026 | Audit-Ready Report Generator
Gesetze: EnWG, StromStG §9b, KWKG, StromNEV §19, EEG, CSRD/ESRS E1, ISO 50001, GHG Protocol
"""

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone

import httpx
from apify import Actor

# ─────────────────────────────────────────────────────────────────────────────
# KONSTANTEN 2026 (Quellen: Übertragungsnetzbetreiber, Bundesnetzagentur)
# ─────────────────────────────────────────────────────────────────────────────
TARIFE_2026 = {
    # Umlagen (netto, ct/kWh → EUR/kWh)
    "kwkg_umlage":        0.00446,   # KWKG-Umlage 2026 (ÜNB, Okt 2025)
    "offshore_umlage":    0.00941,   # Offshore-Netzumlage 2026 §17f EnWG
    "stromnev19_umlage":  0.01559,   # Aufschlag bes. Netznutzung §19 StromNEV 2026 (Kat. A ≤1 Mio kWh)
    "stromnev19_b":       0.00050,   # Kat. B > 1 Mio kWh
    "stromnev19_c":       0.00025,   # Kat. C > 1 Mio kWh prod. Gewerbe
    # Steuern
    "stromsteuer":        0.02050,   # Regelsatz StromStG §3 (ct: 2,05)
    "stromsteuer_9b":     0.00050,   # § 9b Abs.2a StromStG prod. Gewerbe (EU-Min.)
    "mwst":               0.19,      # MwSt. § 12 UStG
    # Konzessionsabgabe Sondervertragskunden §2 KAV
    "konz_sonder":        0.0011,    # Sondervertragskunden (Gewerbe, GHD)
    # Leistungspreis Ø Deutschland (€/kW/Jahr) – für industrielle Abnahmestellen
    "leistungspreis_eur_kw": 80.0,
    # CO2-Emissionsfaktor Deutschland 2025 (IFEU/UBA location-based)
    "co2_faktor_g_kwh":   367.0,    # g CO2-Äq./kWh (Strommix DE 2025)
    # EnPI-Referenz (Bundesweiter Durchschnitt Gewerbe kWh/m²/Jahr)
    "enpi_referenz_kwh_m2": 120.0,
}

SMARD_FILTER_DAYAHEAD = 4169   # Day-Ahead Marktpreis DE (EPEX Spot)
SMARD_BASE = "https://www.smard.de/app/chart_data"
REPORT_VERSION = "2.0"
GESETZ_REFS = [
    "EnWG (Energiewirtschaftsgesetz)",
    "StromStG §9b (Spitzenausgleich prod. Gewerbe)",
    "KWKG 2016/2020 (Kraft-Wärme-Kopplungsgesetz)",
    "StromNEV §19 Abs.2 (Aufschlag bes. Netznutzung)",
    "EEG 2023 (Erneuerbare-Energien-Gesetz)",
    "KAV §2 (Konzessionsabgabenverordnung)",
    "EU CSRD 2022/2464 / ESRS E1 (Klimawandel, Scope 2)",
    "GHG Protocol Corporate Standard (Scope 1/2/3)",
    "ISO 50001:2018 (Energiemanagementsystem)",
    "DIN EN 16247-1 (Energieaudits)",
    "§ 8 EDL-G (Energiedienstleistungsgesetz) – Energieaudit-Pflicht",
    "EU Taxonomy Regulation 2020/852 Art.8",
]


# ─────────────────────────────────────────────────────────────────────────────
# PLZ-LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
def load_plz_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "plz_data.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_netz_info(plz: str, plz_db: dict) -> dict:
    prefix2 = plz[:2] if len(plz) >= 2 else "00"
    return plz_db.get(prefix2, plz_db["default"])


# ─────────────────────────────────────────────────────────────────────────────
# SMARD LIVE MARKTPREIS (Day-Ahead, Bundesnetzagentur)
# ─────────────────────────────────────────────────────────────────────────────
async def get_smard_dayahead_price() -> dict:
    """Holt den aktuellen Day-Ahead Marktpreis von SMARD (Bundesnetzagentur).
    Gibt EUR/kWh zurück + Timestamp + Quelle."""
    try:
        idx_url = f"{SMARD_BASE}/{SMARD_FILTER_DAYAHEAD}/DE/index_hour.json"
        async with httpx.AsyncClient(timeout=15.0) as client:
            r_idx = await client.get(idx_url)
            r_idx.raise_for_status()
            timestamps = r_idx.json().get("timestamps", [])
            if not timestamps:
                raise ValueError("Keine Timestamps von SMARD")

            # Letzter verfügbarer Timestamp
            latest_ts = sorted(timestamps)[-1]
            ts_url = (
                f"{SMARD_BASE}/{SMARD_FILTER_DAYAHEAD}/DE/"
                f"{SMARD_FILTER_DAYAHEAD}_DE_hour_{latest_ts}.json"
            )
            r_ts = await client.get(ts_url)
            r_ts.raise_for_status()
            series = r_ts.json().get("series", [])

            # Letzten gültigen Wert (nicht None)
            valid = [(ts, val) for ts, val in series if val is not None]
            if not valid:
                raise ValueError("Keine gültigen Preisdaten")

            last_ts_ms, last_price_eur_mwh = valid[-1]
            price_eur_kwh = round(last_price_eur_mwh / 1000, 5)
            price_dt = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)

            # 7-Tage Durchschnitt für Stabilität
            last_7d = [val for _, val in valid[-168:] if val is not None]
            avg_7d = round(sum(last_7d) / len(last_7d) / 1000, 5) if last_7d else price_eur_kwh

            return {
                "price_eur_kwh": price_eur_kwh,
                "avg_7d_eur_kwh": avg_7d,
                "timestamp_utc": price_dt.isoformat(),
                "source": "SMARD – Bundesnetzagentur (EPEX Spot Day-Ahead)",
                "filter_id": SMARD_FILTER_DAYAHEAD,
                "raw_eur_mwh": last_price_eur_mwh,
                "data_points_7d": len(last_7d),
            }
    except Exception as e:
        Actor.log.warning(f"SMARD-Abruf fehlgeschlagen: {e} – Fallback-Preis wird verwendet.")
        return {
            "price_eur_kwh": 0.0893,
            "avg_7d_eur_kwh": 0.0893,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "source": "Fallback – SMARD nicht erreichbar (Ø DE 2025: 89,32 EUR/MWh)",
            "filter_id": SMARD_FILTER_DAYAHEAD,
            "raw_eur_mwh": 89.32,
            "data_points_7d": 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# BERECHNUNGSMOTOR
# ─────────────────────────────────────────────────────────────────────────────
def berechne_stromkosten(
    jahresverbrauch_kwh: float,
    spitzenlast_kw: float,
    marktpreis_eur_kwh: float,
    netz_info: dict,
    messstellenbetrieb_eur: float,
    is_producing: bool,
    plz: str,
) -> dict:
    t = TARIFE_2026

    # ── Netzentgelt (regional) ──────────────────────────────────────────────
    net_var = netz_info["net_var"]   # EUR/kWh
    konz_abg = netz_info["konz_abg"] if jahresverbrauch_kwh < 30_000 else t["konz_sonder"]

    # ── Umlage Kategorie (§19 StromNEV) ────────────────────────────────────
    if jahresverbrauch_kwh > 1_000_000 and is_producing:
        stromnev = t["stromnev19_c"]
        stromnev_kategorie = "C (prod. Gewerbe, >1 Mio kWh)"
    elif jahresverbrauch_kwh > 1_000_000:
        stromnev = t["stromnev19_b"]
        stromnev_kategorie = "B (>1 Mio kWh)"
    else:
        stromnev = t["stromnev19_umlage"]
        stromnev_kategorie = "A (≤1 Mio kWh)"

    # ── Stromsteuer ─────────────────────────────────────────────────────────
    stromsteuer = t["stromsteuer_9b"] if is_producing else t["stromsteuer"]
    stromsteuer_entlastung = (t["stromsteuer"] - stromsteuer) * jahresverbrauch_kwh if is_producing else 0.0

    # ── Vollkostenberechnung pro kWh (netto) ───────────────────────────────
    # Beschaffung + Vertriebsmarge (10% auf Marktpreis)
    beschaffung = marktpreis_eur_kwh * 1.10

    arbeitspreis_netto = (
        beschaffung
        + net_var
        + konz_abg
        + t["kwkg_umlage"]
        + t["offshore_umlage"]
        + stromnev
        + stromsteuer
    )

    # ── Jahresarbeitspreis (netto) ──────────────────────────────────────────
    jahresarbeitspreis_netto = arbeitspreis_netto * jahresverbrauch_kwh

    # ── Leistungspreis (Jahresleistung × Leistungspreis) ────────────────────
    leistungskosten_netto = spitzenlast_kw * t["leistungspreis_eur_kw"]

    # ── Messstellenbetrieb ──────────────────────────────────────────────────
    msb = messstellenbetrieb_eur

    # ── Netto-Gesamtkosten ──────────────────────────────────────────────────
    netto_gesamt = jahresarbeitspreis_netto + leistungskosten_netto + msb

    # ── MwSt ────────────────────────────────────────────────────────────────
    mwst_betrag = netto_gesamt * t["mwst"]
    brutto_gesamt = netto_gesamt + mwst_betrag

    # ── Kostenaufschlüsselung (netto, EUR) ──────────────────────────────────
    aufschluesselung = {
        "beschaffung_vertrieb": round(beschaffung * jahresverbrauch_kwh, 2),
        "netzentgelt_variabel": round(net_var * jahresverbrauch_kwh, 2),
        "konzessionsabgabe": round(konz_abg * jahresverbrauch_kwh, 2),
        "kwkg_umlage": round(t["kwkg_umlage"] * jahresverbrauch_kwh, 2),
        "offshore_umlage": round(t["offshore_umlage"] * jahresverbrauch_kwh, 2),
        "stromnev_aufschlag": round(stromnev * jahresverbrauch_kwh, 2),
        "stromsteuer": round(stromsteuer * jahresverbrauch_kwh, 2),
        "leistungskosten": round(leistungskosten_netto, 2),
        "messstellenbetrieb": round(msb, 2),
        "mwst_19pct": round(mwst_betrag, 2),
    }

    # ── Prozentualer Anteil pro Kostenkomponente ────────────────────────────
    anteile = {k: round(v / brutto_gesamt * 100, 2) for k, v in aufschluesselung.items()}

    return {
        "arbeitspreis_netto_eur_kwh": round(arbeitspreis_netto, 5),
        "leistungspreis_eur_kw": t["leistungspreis_eur_kw"],
        "jahresarbeitspreis_netto_eur": round(jahresarbeitspreis_netto, 2),
        "leistungskosten_netto_eur": round(leistungskosten_netto, 2),
        "messstellenbetrieb_eur": round(msb, 2),
        "netto_gesamt_eur": round(netto_gesamt, 2),
        "mwst_eur": round(mwst_betrag, 2),
        "brutto_gesamt_eur": round(brutto_gesamt, 2),
        "stromsteuer_entlastung_9b_eur": round(stromsteuer_entlastung, 2),
        "stromnev_kategorie": stromnev_kategorie,
        "aufschluesselung": aufschluesselung,
        "anteile_pct": anteile,
        "tarife_angewendet": {
            "marktpreis_eur_kwh": round(marktpreis_eur_kwh, 5),
            "netzentgelt_variabel": net_var,
            "konzessionsabgabe": konz_abg,
            "kwkg_umlage": t["kwkg_umlage"],
            "offshore_umlage": t["offshore_umlage"],
            "stromnev19_umlage": stromnev,
            "stromsteuer": stromsteuer,
            "leistungspreis_eur_kw": t["leistungspreis_eur_kw"],
            "mwst_satz": t["mwst"],
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# CO2 / ESG (ESRS E1 / GHG Protocol Scope 2 – Location-Based)
# ─────────────────────────────────────────────────────────────────────────────
def berechne_esg(jahresverbrauch_kwh: float) -> dict:
    co2_kg = round(jahresverbrauch_kwh * TARIFE_2026["co2_faktor_g_kwh"] / 1000, 1)
    co2_tonnen = round(co2_kg / 1000, 3)
    co2_pro_mwh = round(TARIFE_2026["co2_faktor_g_kwh"] / 1000, 4)  # t/MWh

    # EU Taxonomy – Wirtschaftstätigkeit 4.9 (Strom Übertragung/Verteilung)
    eu_taxonomy_alignment = "Zu prüfen (Art. 8 EU-Taxonomie-VO 2020/852)"

    return {
        "scope": "Scope 2 – Location-based (GHG Protocol Corporate Standard)",
        "norm": "ESRS E1 (Klimawandel) / ISO 14064-1 / GHG Protocol",
        "emissionsfaktor_g_co2_kwh": TARIFE_2026["co2_faktor_g_kwh"],
        "emissionsfaktor_t_co2_mwh": co2_pro_mwh,
        "quelle_emissionsfaktor": "IFEU / Umweltbundesamt (UBA) – Strommix Deutschland 2025",
        "co2_footprint_kg": co2_kg,
        "co2_footprint_tonnen": co2_tonnen,
        "co2_aequivalent": "CO2-Äquivalente (CO2e) inkl. CH4 & N2O",
        "eu_taxonomy": eu_taxonomy_alignment,
        "csrd_relevant": True,
        "esrs_datenpunkte": ["E1-4 (Energieverbrauch)", "E1-5 (Emissionen Scope 2)", "E1-6 (Intensitätsrate)"],
        "intensitaetsrate_t_co2_mwh": co2_pro_mwh,
        "hinweis": (
            "Dieser Wert ist ein Pre-Audit Scope-2-Wert auf Basis des deutschen Strommix-Emissionsfaktors. "
            "Für die CSRD/ESRS-Berichterstattung ist zusätzlich ein marktbasierter Wert (Herkunftsnachweise) "
            "zu ermitteln. Die Endvalidierung obliegt einem zertifizierten Wirtschaftsprüfer."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE-CHECK (EDL-G, ISO 50001, CSRD)
# ─────────────────────────────────────────────────────────────────────────────
def pruefe_compliance(jahresverbrauch_kwh: float, spitzenlast_kw: float, is_producing: bool) -> dict:
    checks = []

    # § 8 EDL-G – Energieaudit-Pflicht (alle 4 Jahre, Nicht-KMU)
    edlg_pflicht = jahresverbrauch_kwh > 100_000
    checks.append({
        "norm": "§ 8 EDL-G (Energiedienstleistungsgesetz)",
        "beschreibung": "Energieaudit-Pflicht alle 4 Jahre für Nicht-KMU",
        "relevant": edlg_pflicht,
        "status": "Pflicht prüfen" if edlg_pflicht else "Nicht betroffen (unter Schwellenwert)",
        "empfehlung": "Zertifiziertes Energieaudit nach DIN EN 16247-1 beauftragen" if edlg_pflicht else None,
    })

    # ISO 50001 – Energiemanagementsystem
    iso_empfohlen = jahresverbrauch_kwh > 500_000
    checks.append({
        "norm": "ISO 50001:2018 (Energiemanagementsystem)",
        "beschreibung": "Empfohlen ab 500.000 kWh; Pflicht für Rechenzentren ≥300 kW ab 2026",
        "relevant": iso_empfohlen,
        "status": "Stark empfohlen" if iso_empfohlen else "Empfohlen",
        "empfehlung": "Implementierung eines EnMS nach ISO 50001 prüfen" if iso_empfohlen else None,
    })

    # § 9b StromStG – Spitzenausgleich prod. Gewerbe
    checks.append({
        "norm": "§ 9b Abs.2a StromStG (Stromsteuervergünstigung)",
        "beschreibung": "Reduzierter Stromsteuersatz für prod. Gewerbe (0,05 ct/kWh statt 2,05 ct/kWh)",
        "relevant": is_producing,
        "status": "Angewendet – Entlastung berechnet" if is_producing else "Nicht aktiviert",
        "empfehlung": "Antrag beim Hauptzollamt auf Jahresausgleich stellen" if is_producing else "Prüfen ob §9b anwendbar",
    })

    # §19 StromNEV – Individuelle Netzentgelte
    stromnev_pruefen = spitzenlast_kw >= 30 and jahresverbrauch_kwh >= 30_000
    checks.append({
        "norm": "§ 19 Abs.2 StromNEV (Individuelle Netzentgelte)",
        "beschreibung": "Reduzierte Netzentgelte bei ≥30 kW Spitzenlast und ≥30.000 kWh/Jahr",
        "relevant": stromnev_pruefen,
        "status": "Prüfung empfohlen" if stromnev_pruefen else "Nicht relevant",
        "empfehlung": "Antrag beim Netzbetreiber auf individuelle Netzentgelte stellen" if stromnev_pruefen else None,
    })

    # CSRD / ESRS E1
    csrd_relevant = jahresverbrauch_kwh > 500_000
    checks.append({
        "norm": "EU CSRD 2022/2464 / ESRS E1 (Klimawandel)",
        "beschreibung": "Nachhaltigkeitsberichterstattung für große Unternehmen; ab 2026/2027 schrittweise verpflichtend",
        "relevant": csrd_relevant,
        "status": "Daten für ESRS E1 bereitgestellt" if csrd_relevant else "Freiwillige Nutzung empfohlen",
        "empfehlung": "Scope-2-Emissionen in Nachhaltigkeitsbericht integrieren",
    })

    # KAV – Konzessionsabgabe Sondervertragskunden
    checks.append({
        "norm": "KAV §2 (Konzessionsabgabenverordnung)",
        "beschreibung": "Reduzierte Konzessionsabgabe bei Jahresverbrauch >30.000 kWh und 2× Spitzenlast >30 kW",
        "relevant": stromnev_pruefen,
        "status": "Sondervertragstarif angewendet" if stromnev_pruefen else "Haushaltstarif-Satz",
        "empfehlung": None,
    })

    return {
        "checks": checks,
        "compliance_score": sum(1 for c in checks if c["relevant"]) * 20,
        "hinweis": "Alle Angaben basieren auf Richtwerten. Verbindliche Compliance-Bestätigung durch Steuer- oder Energieberater erforderlich.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML AUDIT-REPORT GENERATOR
# ─────────────────────────────────────────────────────────────────────────────
def generiere_html_report(
    input_data: dict,
    markt_data: dict,
    netz_info: dict,
    kalkulation: dict,
    esg: dict,
    compliance: dict,
    pruf_nr: str,
    report_ts: str,
) -> str:
    plz = input_data.get("plz", "—")
    kwh = input_data.get("jahresverbrauch_kwh", 0)
    kw = input_data.get("spitzenlast_kw", 0)
    msb = input_data.get("messstellenbetrieb_eur", 0)
    prod = input_data.get("is_producing", False)
    bedrijfsnaam = input_data.get("unternehmen", "—")
    anschrift = input_data.get("anschrift", "—")
    berichtsjahr = input_data.get("berichtsjahr", datetime.now().year)

    aufsch = kalkulation["aufschluesselung"]
    tarife = kalkulation["tarife_angewendet"]

    # Compliance-Tabelle
    comp_rows = ""
    for c in compliance["checks"]:
        farbe = "#1a7a3c" if c["relevant"] else "#666"
        icon = "✅" if c["relevant"] else "○"
        emp = f"<br><small><em>{c['empfehlung']}</em></small>" if c["empfehlung"] else ""
        comp_rows += f"""
        <tr>
          <td><strong>{c['norm']}</strong><br><small>{c['beschreibung']}</small></td>
          <td style="color:{farbe}">{icon} {c['status']}{emp}</td>
        </tr>"""

    # Kostenaufschlüsselung-Tabelle
    aufsch_labels = {
        "beschaffung_vertrieb": "Strombeschaffung & Vertrieb",
        "netzentgelt_variabel": f"Netzentgelt variabel (§21 EnWG) – {netz_info['operator']}",
        "konzessionsabgabe": "Konzessionsabgabe (§2 KAV)",
        "kwkg_umlage": "KWKG-Umlage 2026",
        "offshore_umlage": "Offshore-Netzumlage 2026 (§17f EnWG)",
        "stromnev_aufschlag": f"Aufschlag bes. Netznutzung §19 StromNEV 2026 – Kat. {kalkulation['stromnev_kategorie'][:1]}",
        "stromsteuer": "Stromsteuer (§3 StromStG)" + (" – §9b Entlastung angewendet" if prod else ""),
        "leistungskosten": f"Leistungskosten (Spitzenlast {kw} kW × {tarife['leistungspreis_eur_kw']:.0f} €/kW)",
        "messstellenbetrieb": "Messstellenbetrieb (§21b EnWG)",
        "mwst_19pct": "Mehrwertsteuer 19% (§12 UStG)",
    }
    aufsch_rows = ""
    for key, label in aufsch_labels.items():
        val = aufsch.get(key, 0)
        pct = kalkulation["anteile_pct"].get(key, 0)
        aufsch_rows += f"""
        <tr>
          <td>{label}</td>
          <td class="num">{val:,.2f} €</td>
          <td class="num">{pct:.1f}%</td>
        </tr>"""

    # Gesetzesreferenzen
    gesetze_li = "".join(f"<li>{g}</li>" for g in GESETZ_REFS)

    # Marktpreis-Hinweis
    smard_hinweis = (
        f"Live Day-Ahead Marktpreis (SMARD, Stand: {markt_data['timestamp_utc'][:10]}, "
        f"Wert: {markt_data['raw_eur_mwh']:.2f} EUR/MWh). "
        f"7-Tage-Ø: {markt_data['avg_7d_eur_kwh']*1000:.2f} EUR/MWh. "
        f"Quelle: {markt_data['source']}"
    )

    # §9b-Hinweis
    entlastung_9b = kalkulation["stromsteuer_entlastung_9b_eur"]
    stg9b_block = ""
    if prod and entlastung_9b > 0:
        stg9b_block = f"""
        <div class="highlight-box green">
          <strong>§ 9b StromStG – Vergünstigung Produzierendes Gewerbe</strong><br>
          Angewendeter Steuersatz: 0,05 ct/kWh (statt 2,05 ct/kWh Regelsatz)<br>
          Steuerliche Entlastung: <strong>{entlastung_9b:,.2f} €/Jahr</strong><br>
          <small>Antrag auf Jahresausgleich beim zuständigen Hauptzollamt erforderlich (§ 9b Abs. 2a StromStG).</small>
        </div>"""

    # Individuelle Netzentgelt-Hinweis
    netz_hinweis = ""
    if kw >= 30 and kwh >= 30_000:
        netz_hinweis = f"""
        <div class="highlight-box blue">
          <strong>§ 19 Abs.2 StromNEV – Individuelle Netzentgelte</strong><br>
          Ihre Spitzenlast ({kw} kW) und Jahresverbrauch ({kwh:,.0f} kWh) erfüllen die Grundvoraussetzungen
          für reduzierte individuelle Netzentgelte beim Netzbetreiber <strong>{netz_info['operator']}</strong>.<br>
          <small>Antrag beim Netzbetreiber stellen – Einsparung kann erheblich sein.</small>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StromAudit Pro – Audit-Vorbereitungsbericht {pruf_nr}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', Arial, sans-serif; color: #222; background: #f5f6fa; font-size: 14px; }}
  .wrapper {{ max-width: 960px; margin: 0 auto; background: #fff; box-shadow: 0 0 24px rgba(0,0,0,.12); }}

  /* HEADER */
  .header {{ background: linear-gradient(135deg, #0a2540 0%, #1a4a7a 100%); color: #fff; padding: 36px 48px 28px; }}
  .header h1 {{ font-size: 26px; font-weight: 700; letter-spacing: .5px; }}
  .header .subtitle {{ font-size: 13px; opacity: .8; margin-top: 4px; }}
  .header-meta {{ display: flex; gap: 32px; margin-top: 20px; font-size: 12px; opacity: .85; flex-wrap: wrap; }}
  .header-meta div {{ display: flex; flex-direction: column; gap: 2px; }}
  .header-meta strong {{ font-size: 13px; color: #7ecef4; }}

  /* WATERMARK */
  .watermark-bar {{ background: #fffbe6; border-left: 4px solid #f0a500; padding: 10px 48px; font-size: 12px; color: #7a5800; }}

  /* SECTION */
  .section {{ padding: 28px 48px; border-bottom: 1px solid #eee; }}
  .section h2 {{ font-size: 16px; color: #0a2540; border-bottom: 2px solid #0a2540; padding-bottom: 6px; margin-bottom: 18px; text-transform: uppercase; letter-spacing: .5px; }}
  .section h3 {{ font-size: 14px; color: #1a4a7a; margin: 16px 0 10px; font-weight: 600; }}

  /* KPI GRID */
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 8px; }}
  .kpi {{ background: #f0f4ff; border: 1px solid #c5d5f0; border-radius: 8px; padding: 14px 16px; }}
  .kpi .label {{ font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: .4px; }}
  .kpi .value {{ font-size: 22px; font-weight: 700; color: #0a2540; margin: 4px 0 2px; }}
  .kpi .sub {{ font-size: 11px; color: #777; }}
  .kpi.green {{ background: #e8f8ee; border-color: #82c99a; }}
  .kpi.green .value {{ color: #1a7a3c; }}
  .kpi.orange {{ background: #fff3e0; border-color: #ffb74d; }}
  .kpi.orange .value {{ color: #e65100; }}

  /* TABLES */
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #0a2540; color: #fff; padding: 9px 12px; text-align: left; font-weight: 600; font-size: 12px; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #eee; vertical-align: top; }}
  tr:nth-child(even) td {{ background: #f9fafc; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tfoot td {{ background: #0a2540 !important; color: #fff; font-weight: 700; border: none; }}
  tfoot td.num {{ text-align: right; }}

  /* HIGHLIGHT BOXES */
  .highlight-box {{ border-radius: 6px; padding: 14px 16px; margin: 12px 0; font-size: 13px; }}
  .highlight-box.green {{ background: #e8f8ee; border-left: 4px solid #1a7a3c; }}
  .highlight-box.blue {{ background: #e3f0ff; border-left: 4px solid #1a4a7a; }}
  .highlight-box.yellow {{ background: #fffbe6; border-left: 4px solid #f0a500; }}
  .highlight-box.red {{ background: #fff0f0; border-left: 4px solid #c0392b; }}

  /* FOOTER */
  .footer {{ background: #0a2540; color: rgba(255,255,255,.7); padding: 20px 48px; font-size: 11px; line-height: 1.7; }}
  .footer strong {{ color: #fff; }}

  /* PRINT */
  @media print {{
    body {{ background: #fff; font-size: 12px; }}
    .wrapper {{ box-shadow: none; }}
    .section {{ padding: 18px 24px; }}
    .header {{ padding: 20px 24px; }}
  }}

  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
  .badge.green {{ background: #1a7a3c; color: #fff; }}
  .badge.grey {{ background: #888; color: #fff; }}
  .source-note {{ font-size: 11px; color: #888; margin-top: 6px; font-style: italic; }}
</style>
</head>
<body>
<div class="wrapper">

<!-- ═══════════════════════════════════════════════════ HEADER ══ -->
<div class="header">
  <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:12px;">
    <div>
      <h1>⚡ StromAudit Pro</h1>
      <div class="subtitle">Energie-Compliance &amp; ESG Pre-Audit Bericht – Deutschland</div>
    </div>
    <div style="text-align:right; font-size:11px; opacity:.8;">
      <div><strong style="font-size:14px; color:#7ecef4;">AUDIT-VORBEREITUNGSBERICHT</strong></div>
      <div>Dieser Bericht ist prüfungsbereit (audit-ready).</div>
      <div>Die Endvalidierung erfolgt durch den Wirtschaftsprüfer.</div>
    </div>
  </div>
  <div class="header-meta">
    <div><span>Prüfnummer</span><strong>{pruf_nr}</strong></div>
    <div><span>Erstellt am</span><strong>{report_ts[:10]}</strong></div>
    <div><span>Berichtsjahr</span><strong>{berichtsjahr}</strong></div>
    <div><span>Standort-PLZ</span><strong>{plz} ({netz_info.get('bundesland','—')})</strong></div>
    <div><span>Netzbetreiber</span><strong>{netz_info['operator']}</strong></div>
    <div><span>Version</span><strong>StromAudit Pro v{REPORT_VERSION}</strong></div>
  </div>
</div>

<!-- WATERMARK / DISCLAIMER-BALKEN -->
<div class="watermark-bar">
  ⚠️ <strong>Pre-Audit Dokument – Nicht geprüft:</strong>
  Dieser Bericht ist ein automatisch generierter Audit-Vorbereitungsbericht (Pre-Audit).
  Er basiert auf öffentlichen Marktdaten und standardisierten Berechnungsmodellen gemäß geltendem deutschen Recht und EU-Recht.
  Er ersetzt <strong>keine</strong> zertifizierte Energiefachprüfung. Die abschließende Validierung und Bestätigung obliegt einem zugelassenen Wirtschaftsprüfer (WP/vBP) oder Energieberater (§ 21 EDL-G).
  Marktpreis-Quelle: {smard_hinweis}
</div>

<!-- ═══════════════════════════════════════════════ STAMMDATEN ══ -->
<div class="section">
  <h2>1 · Stammdaten &amp; Eingabeparameter</h2>
  <table>
    <tr><td><strong>Unternehmen / Abnahmestelle</strong></td><td>{bedrijfsnaam}</td><td><strong>Anschrift / PLZ</strong></td><td>{anschrift} · {plz}</td></tr>
    <tr><td><strong>Berichtsjahr</strong></td><td>{berichtsjahr}</td><td><strong>Bundesland</strong></td><td>{netz_info.get('bundesland','—')}</td></tr>
    <tr><td><strong>Jahresverbrauch</strong></td><td>{kwh:,.0f} kWh</td><td><strong>Spitzenlast</strong></td><td>{kw} kW</td></tr>
    <tr><td><strong>Messstellenbetrieb</strong></td><td>{msb:,.2f} €/Jahr</td><td><strong>Prod. Gewerbe §9b StromStG</strong></td><td>{"Ja – Vergünstigung angewendet" if prod else "Nein"}</td></tr>
    <tr><td><strong>Netzbetreiber (PLZ-Prefix {plz[:2]})</strong></td><td colspan="3">{netz_info['operator']}</td></tr>
    <tr><td><strong>Day-Ahead Marktpreis</strong></td><td>{markt_data['raw_eur_mwh']:.2f} EUR/MWh</td><td><strong>7-Tage-Ø</strong></td><td>{markt_data['avg_7d_eur_kwh']*1000:.2f} EUR/MWh</td></tr>
  </table>
</div>

<!-- ════════════════════════════════════════════ KPI ÜBERSICHT ══ -->
<div class="section">
  <h2>2 · Kennzahlen-Übersicht (KPIs)</h2>
  <div class="kpi-grid">
    <div class="kpi orange">
      <div class="label">Brutto-Jahresgesamtkosten</div>
      <div class="value">{kalkulation['brutto_gesamt_eur']:,.0f} €</div>
      <div class="sub">inkl. 19% MwSt.</div>
    </div>
    <div class="kpi">
      <div class="label">Netto-Gesamtkosten</div>
      <div class="value">{kalkulation['netto_gesamt_eur']:,.0f} €</div>
      <div class="sub">excl. MwSt.</div>
    </div>
    <div class="kpi">
      <div class="label">Arbeitspreis (netto)</div>
      <div class="value">{kalkulation['arbeitspreis_netto_eur_kwh']*100:.3f} ct</div>
      <div class="sub">pro kWh</div>
    </div>
    <div class="kpi">
      <div class="label">Leistungskosten</div>
      <div class="value">{kalkulation['leistungskosten_netto_eur']:,.0f} €</div>
      <div class="sub">{kw} kW × {tarife['leistungspreis_eur_kw']:.0f} €/kW</div>
    </div>
    <div class="kpi green">
      <div class="label">CO₂-Footprint (Scope 2)</div>
      <div class="value">{esg['co2_footprint_tonnen']:.1f} t</div>
      <div class="sub">CO₂e (location-based, ESRS E1)</div>
    </div>
    <div class="kpi green">
      <div class="label">§9b StromStG Entlastung</div>
      <div class="value">{kalkulation['stromsteuer_entlastung_9b_eur']:,.0f} €</div>
      <div class="sub">{"Angewendet" if prod else "Nicht aktiviert"}</div>
    </div>
    <div class="kpi">
      <div class="label">Day-Ahead Marktpreis</div>
      <div class="value">{markt_data['raw_eur_mwh']:.1f}</div>
      <div class="sub">EUR/MWh (SMARD live)</div>
    </div>
    <div class="kpi">
      <div class="label">StromNEV Kategorie</div>
      <div class="value">{kalkulation['stromnev_kategorie'][:1]}</div>
      <div class="sub">{kalkulation['stromnev_kategorie']}</div>
    </div>
  </div>
</div>

<!-- ════════════════════════════════════════ KOSTENAUFSCHLÜSSELUNG ══ -->
<div class="section">
  <h2>3 · Vollständige Kostenaufschlüsselung 2026</h2>
  {stg9b_block}
  {netz_hinweis}
  <table>
    <thead>
      <tr><th>Kostenkomponente</th><th class="num">EUR/Jahr</th><th class="num">Anteil</th></tr>
    </thead>
    <tbody>{aufsch_rows}</tbody>
    <tfoot>
      <tr>
        <td><strong>GESAMT (Brutto inkl. 19% MwSt.)</strong></td>
        <td class="num"><strong>{kalkulation['brutto_gesamt_eur']:,.2f} €</strong></td>
        <td class="num"><strong>100%</strong></td>
      </tr>
    </tfoot>
  </table>

  <h3>Angewendete Tarife &amp; Gesetzliche Grundlagen (Stand: 2026)</h3>
  <table>
    <tr><th>Parameter</th><th>Wert</th><th>Rechtsgrundlage</th></tr>
    <tr><td>Strombeschaffung (Day-Ahead + Marge 10%)</td><td class="num">{tarife['marktpreis_eur_kwh']*100:.4f} ct/kWh (Basis) → {tarife['marktpreis_eur_kwh']*1.10*100:.4f} ct/kWh</td><td>EPEX Spot / EnWG §1</td></tr>
    <tr><td>Netzentgelt variabel</td><td class="num">{tarife['netzentgelt_variabel']*100:.3f} ct/kWh</td><td>§21 EnWG / Reg. BNetzA</td></tr>
    <tr><td>Konzessionsabgabe</td><td class="num">{tarife['konzessionsabgabe']*100:.3f} ct/kWh</td><td>§2 KAV</td></tr>
    <tr><td>KWKG-Umlage 2026</td><td class="num">{tarife['kwkg_umlage']*100:.3f} ct/kWh</td><td>KWKG 2016/2020</td></tr>
    <tr><td>Offshore-Netzumlage 2026</td><td class="num">{tarife['offshore_umlage']*100:.3f} ct/kWh</td><td>§17f EnWG</td></tr>
    <tr><td>Aufschlag bes. Netznutzung §19 StromNEV</td><td class="num">{tarife['stromnev19_umlage']*100:.3f} ct/kWh</td><td>§19 Abs.2 StromNEV</td></tr>
    <tr><td>Stromsteuer</td><td class="num">{tarife['stromsteuer']*100:.3f} ct/kWh {"(§9b-Satz)" if prod else "(Regelsatz)"}</td><td>§3 StromStG {" / §9b Abs.2a" if prod else ""}</td></tr>
    <tr><td>Leistungspreis</td><td class="num">{tarife['leistungspreis_eur_kw']:.2f} €/kW/Jahr</td><td>§21 EnWG (industrieller Tarif)</td></tr>
    <tr><td>Mehrwertsteuer</td><td class="num">{tarife['mwst_satz']*100:.0f}%</td><td>§12 UStG</td></tr>
  </table>
  <p class="source-note">Umlagen-Quelle: Übertragungsnetzbetreiber (ÜNB) – Veröffentlichung Oktober 2025 (amtlich). Netzentgelte: Bundesnetzagentur / regionaler Netzbetreiber.</p>
</div>

<!-- ══════════════════════════════════════════════════ ESG / CO₂ ══ -->
<div class="section">
  <h2>4 · ESG-Bericht · Scope-2-Emissionen (ESRS E1 / GHG Protocol)</h2>
  <div class="kpi-grid">
    <div class="kpi green">
      <div class="label">CO₂-Fußabdruck (Scope 2, location-based)</div>
      <div class="value">{esg['co2_footprint_tonnen']:.3f} t CO₂e</div>
      <div class="sub">{esg['co2_footprint_kg']:,.0f} kg</div>
    </div>
    <div class="kpi">
      <div class="label">Emissionsfaktor (UBA 2025)</div>
      <div class="value">{esg['emissionsfaktor_g_co2_kwh']:.0f} g</div>
      <div class="sub">CO₂e pro kWh</div>
    </div>
    <div class="kpi">
      <div class="label">Intensitätsrate</div>
      <div class="value">{esg['intensitaetsrate_t_co2_mwh']:.4f}</div>
      <div class="sub">t CO₂e/MWh</div>
    </div>
  </div>
  <table>
    <tr><th>Parameter</th><th>Wert</th></tr>
    <tr><td>Scope</td><td>{esg['scope']}</td></tr>
    <tr><td>Norm / Standard</td><td>{esg['norm']}</td></tr>
    <tr><td>Emissionsfaktor</td><td>{esg['emissionsfaktor_g_co2_kwh']} g CO₂e/kWh – {esg['quelle_emissionsfaktor']}</td></tr>
    <tr><td>Jahresverbrauch</td><td>{kwh:,.0f} kWh</td></tr>
    <tr><td>CO₂-Emissionen (Scope 2, location-based)</td><td><strong>{esg['co2_footprint_kg']:,.0f} kg ({esg['co2_footprint_tonnen']:.3f} t CO₂e)</strong></td></tr>
    <tr><td>EU-Taxonomie-Relevanz</td><td>{esg['eu_taxonomy']}</td></tr>
    <tr><td>CSRD/ESRS-Datenpunkte</td><td>{', '.join(esg['esrs_datenpunkte'])}</td></tr>
  </table>
  <div class="highlight-box yellow" style="margin-top:14px;">
    <strong>Hinweis für CSRD/ESRS E1-Berichterstattung:</strong><br>
    {esg['hinweis']}
  </div>
</div>

<!-- ══════════════════════════════════════════════ COMPLIANCE ══ -->
<div class="section">
  <h2>5 · Compliance-Checkliste &amp; Handlungsempfehlungen</h2>
  <table>
    <thead>
      <tr><th style="width:60%">Norm / Vorschrift</th><th>Status &amp; Empfehlung</th></tr>
    </thead>
    <tbody>{comp_rows}</tbody>
  </table>
  <p class="source-note" style="margin-top:10px;">{compliance['hinweis']}</p>
</div>

<!-- ══════════════════════════════════════════ GESETZESGRUNDLAGEN ══ -->
<div class="section">
  <h2>6 · Angewendete Rechtsgrundlagen &amp; Normen</h2>
  <ul style="columns: 2; column-gap: 32px; font-size: 13px; line-height: 2; padding-left: 18px;">{gesetze_li}</ul>
</div>

<!-- ══════════════════════════════════════════════════ FOOTER ══ -->
<div class="footer">
  <p>
    <strong>StromAudit Pro v{REPORT_VERSION}</strong> · Prüfnummer: {pruf_nr} · Erstellt: {report_ts} UTC<br>
    Datenquellen: SMARD Bundesnetzagentur (Day-Ahead Marktpreis) · ÜNB (Umlagen 2026) · UBA/IFEU (CO₂-Emissionsfaktoren) · BNetzA (Netzentgelte) · Eigene Berechnungsmodelle.<br>
    Marktpreis-Quelle: {markt_data['source']} | Zeitstempel: {markt_data['timestamp_utc']}<br><br>
    <strong>Haftungsausschluss:</strong>
    Alle Angaben in diesem Bericht basieren auf öffentlich verfügbaren Referenzdaten und standardisierten Annahmen zum Zeitpunkt der Berichterstellung.
    Die Berechnungen dienen der Orientierung und der Vorbereitung einer formalen Energiefachprüfung.
    Sie ersetzen keine steuerrechtliche oder energierechtliche Beratung.
    Für die Richtigkeit und Vollständigkeit der eingegebenen Verbrauchsdaten trägt der Auftraggeber die Verantwortung.
    © {datetime.now().year} StromAudit Pro – Energieanalyse für Deutschland.
  </p>
</div>

</div><!-- /wrapper -->
</body>
</html>"""
    return html


# ─────────────────────────────────────────────────────────────────────────────
# APIFY ACTOR MAIN
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    async with Actor:
        inp = await Actor.get_input() or {}

        # ── Eingaben normalisieren ──────────────────────────────────────────
        plz = str(inp.get("plz", "80331")).zfill(5)
        jahresverbrauch_kwh = float(inp.get("jahresverbrauch_kwh", 125000))
        spitzenlast_kw = float(inp.get("spitzenlast_kw", 45))
        messstellenbetrieb_eur = float(inp.get("messstellenbetrieb_eur", 250.0))
        is_producing = bool(inp.get("is_producing", True))
        unternehmen = inp.get("unternehmen", "—")
        anschrift = inp.get("anschrift", "—")
        berichtsjahr = int(inp.get("berichtsjahr", datetime.now().year))

        Actor.log.info(f"▶ StromAudit Pro gestartet | PLZ: {plz} | Verbrauch: {jahresverbrauch_kwh:,.0f} kWh")

        # ── PLZ-Daten laden ─────────────────────────────────────────────────
        plz_db = load_plz_data()
        netz_info = get_netz_info(plz, plz_db)
        Actor.log.info(f"   Netzbetreiber: {netz_info['operator']} ({netz_info.get('bundesland','?')})")

        # ── SMARD Marktpreis (live) ─────────────────────────────────────────
        Actor.log.info("   SMARD Marktpreisabruf (Bundesnetzagentur)…")
        markt_data = await get_smard_dayahead_price()
        Actor.log.info(f"   Marktpreis: {markt_data['raw_eur_mwh']:.2f} EUR/MWh ({markt_data['source']})")

        # ── Berechnung ──────────────────────────────────────────────────────
        kalkulation = berechne_stromkosten(
            jahresverbrauch_kwh, spitzenlast_kw,
            markt_data["price_eur_kwh"], netz_info,
            messstellenbetrieb_eur, is_producing, plz,
        )
        esg = berechne_esg(jahresverbrauch_kwh)
        compliance = pruefe_compliance(jahresverbrauch_kwh, spitzenlast_kw, is_producing)

        # ── Report-Metadaten ────────────────────────────────────────────────
        pruf_nr = f"SAP-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        report_ts = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "")

        # ── HTML Report generieren ──────────────────────────────────────────
        Actor.log.info("   HTML Audit-Report generieren…")
        html_report = generiere_html_report(
            inp, markt_data, netz_info,
            kalkulation, esg, compliance,
            pruf_nr, report_ts,
        )

        # ── Key-Value Store speichern ───────────────────────────────────────
        await Actor.set_value("audit_report.html", html_report, content_type="text/html")

        # ── Dataset-Eintrag ─────────────────────────────────────────────────
        store_id = Actor.get_env().get("default_key_value_store_id", "")
        report_url = f"https://api.apify.com/v2/key-value-stores/{store_id}/records/audit_report.html" if store_id else "—"

        dataset_entry = {
            "pruefnummer": pruf_nr,
            "erstellt_utc": report_ts,
            "berichtsjahr": berichtsjahr,
            "status": "AUDIT_READY",
            "plz": plz,
            "bundesland": netz_info.get("bundesland", "—"),
            "netzbetreiber": netz_info["operator"],
            "unternehmen": unternehmen,
            "eingabe": {
                "jahresverbrauch_kwh": jahresverbrauch_kwh,
                "spitzenlast_kw": spitzenlast_kw,
                "messstellenbetrieb_eur": messstellenbetrieb_eur,
                "is_producing_gewerbe": is_producing,
            },
            "marktdaten": {
                "dayahead_preis_eur_mwh": markt_data["raw_eur_mwh"],
                "dayahead_preis_eur_kwh": markt_data["price_eur_kwh"],
                "avg_7tage_eur_kwh": markt_data["avg_7d_eur_kwh"],
                "timestamp_utc": markt_data["timestamp_utc"],
                "quelle": markt_data["source"],
            },
            "kalkulation": {
                "arbeitspreis_netto_ct_kwh": round(kalkulation["arbeitspreis_netto_eur_kwh"] * 100, 4),
                "jahresarbeitspreis_netto_eur": kalkulation["jahresarbeitspreis_netto_eur"],
                "leistungskosten_netto_eur": kalkulation["leistungskosten_netto_eur"],
                "messstellenbetrieb_eur": kalkulation["messstellenbetrieb_eur"],
                "netto_gesamt_eur": kalkulation["netto_gesamt_eur"],
                "mwst_eur": kalkulation["mwst_eur"],
                "brutto_gesamt_eur": kalkulation["brutto_gesamt_eur"],
                "stromsteuer_entlastung_9b_eur": kalkulation["stromsteuer_entlastung_9b_eur"],
                "stromnev_kategorie": kalkulation["stromnev_kategorie"],
                "aufschluesselung": kalkulation["aufschluesselung"],
                "tarife_angewendet": kalkulation["tarife_angewendet"],
            },
            "esg": esg,
            "compliance": compliance,
            "report_url": report_url,
            "gesetzliche_grundlagen": GESETZ_REFS,
            "tool_version": f"StromAudit Pro v{REPORT_VERSION}",
        }

        await Actor.push_data(dataset_entry)
        await Actor.set_value("OUTPUT", dataset_entry)

        Actor.log.info(f"✅ Abgeschlossen | Brutto: {kalkulation['brutto_gesamt_eur']:,.2f} € | CO₂: {esg['co2_footprint_tonnen']:.2f} t | Report: {report_url}")


if __name__ == "__main__":
    asyncio.run(main())
