# ⚡ StromAudit Pro – Deutsche Energie-Compliance & ESG Pre-Audit Engine

> **Der umfassendste automatisierte Stromkosten-Audit für Deutschland.**  
> Live-Marktpreise · Alle PLZ · Vollkostenberechnung · CSRD/ESRS E1 · Audit-Ready Report

---

## 🎯 Was ist StromAudit Pro?

**StromAudit Pro** ist eine vollautomatische **Pre-Audit Engine** für den deutschen Energiemarkt. Sie berechnet auf Basis offizieller Behördendaten, aktueller Live-Börsenpreise und gesetzlich verankerten Tarifen die vollständigen Stromkosten eines Unternehmensstandorts – und liefert einen **audit-ready HTML-Bericht**, der direkt dem Wirtschaftsprüfer oder Steuerberater übergeben werden kann.

**Kein anderes Tool auf dem Markt kombiniert:**
- Live EPEX Spot Day-Ahead Preise (Bundesnetzagentur SMARD API)
- Alle deutschen PLZ (00–99) mit regionalem Netzbetreiber-Mapping
- Vollständige gesetzliche Umlagenstruktur 2026 (KWKG, §19 StromNEV, Offshore, Konzessionsabgabe)
- §9b StromStG Vergünstigung für das Produzierende Gewerbe
- CSRD/ESRS E1-konformer Scope-2-CO₂-Bericht
- Compliance-Checkliste nach EDL-G, ISO 50001, KAV

---

## 🏆 Warum StromAudit Pro?

| Merkmal | Einfache Rechner | StromAudit Pro |
|---|---|---|
| Marktpreis | Statisch / veraltet | **Live (SMARD Bundesnetzagentur)** |
| PLZ-Abdeckung | Teilweise | **Alle deutschen PLZ (00–99)** |
| Kostentrennung | Nur Arbeitspreis | **Arbeitspreis + Leistungspreis + MSB** |
| Umlagen 2026 | Pauschal | **KWKG + Offshore + §19 StromNEV (Kat. A/B/C)** |
| §9b StromStG | Nicht berücksichtigt | **Automatisch berechnet** |
| ESG / CO₂ | Fehlt | **Scope 2, ESRS E1, GHG Protocol** |
| Compliance | Fehlt | **EDL-G, ISO 50001, CSRD, KAV** |
| Output | Rohdaten | **Audit-Ready HTML-Report + JSON** |
| Für Wirtschaftsprüfer | Nicht geeignet | **Direkt verwendbar** |

---

## 🎯 Zielgruppe

- **Energieberater** – Erstmaterialisierung für Kundenanalysen
- **Steuerberater & Wirtschaftsprüfer** – §9b-Entlastungsberechnung, prüfungsreife Dokumentation
- **CFOs & Controlling-Abteilungen** – Budgetplanung Energiekosten
- **ESG-Manager & Nachhaltigkeitsbeauftragte** – CSRD/ESRS E1 Scope-2-Daten
- **SaaS-Plattformen** – API-Integration für ESG, Immobilien, Controlling
- **Unternehmen ab 30.000 kWh/Jahr** – EDL-G-Pflichtprüfung, individuelle Netzentgelte

---

## 📊 Was berechnet der Actor konkret?

### 1. Strombeschaffungskosten
- **Live Day-Ahead Marktpreis** (EPEX Spot via SMARD Bundesnetzagentur, stündliche Auflösung)
- 7-Tage-Durchschnitt für Stabilitätsbewertung
- Beschaffungspreis inkl. Vertriebsmarge (10%)

### 2. Vollständige Netzentgelt- und Umlagestruktur 2026
| Komponente | Tarif 2026 | Rechtsgrundlage |
|---|---|---|
| Netzentgelt variabel | Regional (PLZ-basiert) | §21 EnWG / BNetzA |
| KWKG-Umlage | **0,446 ct/kWh** | KWKG 2016/2020 |
| Offshore-Netzumlage | **0,941 ct/kWh** | §17f EnWG |
| §19 StromNEV Aufschlag (Kat. A) | **1,559 ct/kWh** | §19 Abs.2 StromNEV |
| §19 StromNEV (Kat. B >1 Mio kWh) | **0,050 ct/kWh** | §19 Abs.2 StromNEV |
| §19 StromNEV (Kat. C prod. Gewerbe) | **0,025 ct/kWh** | §19 Abs.2 StromNEV |
| Konzessionsabgabe Sondervertragsktd. | **0,11 ct/kWh** | §2 KAV |
| Stromsteuer Regelsatz | **2,050 ct/kWh** | §3 StromStG |
| Stromsteuer §9b (prod. Gewerbe) | **0,050 ct/kWh** | §9b Abs.2a StromStG |
| Leistungspreis | **80 €/kW/Jahr** (Ø DE) | §21 EnWG |
| Mehrwertsteuer | **19%** | §12 UStG |

*Quellen: Übertragungsnetzbetreiber (ÜNB) Veröffentlichung Oktober 2025, Bundesnetzagentur (BNetzA)*

### 3. ESG / CO₂-Bilanz (ESRS E1 / GHG Protocol Scope 2)
- **Emissionsfaktor:** 367 g CO₂e/kWh (UBA/IFEU Strommix Deutschland 2025)
- **Scope 2 – Location-based** (GHG Protocol Corporate Standard)
- ESRS-Datenpunkte: E1-4 (Energieverbrauch), E1-5 (Scope-2-Emissionen), E1-6 (Intensitätsrate)
- EU-Taxonomie-Hinweis (Art. 8 VO 2020/852)

### 4. Compliance-Checkliste
| Norm | Prüfung |
|---|---|
| §8 EDL-G | Energieaudit-Pflicht für Nicht-KMU |
| ISO 50001:2018 | Energiemanagementsystem-Empfehlung |
| §9b StromStG | Spitzenausgleich Produzierendes Gewerbe |
| §19 Abs.2 StromNEV | Individuelle Netzentgelte (ab 30 kW + 30.000 kWh) |
| EU CSRD / ESRS E1 | Nachhaltigkeitsberichtspflicht |
| §2 KAV | Konzessionsabgabe Sondervertragskunden |

---

## 📈 Beispiel

### Eingabe
```json
{
  "plz": "80331",
  "jahresverbrauch_kwh": 125000,
  "spitzenlast_kw": 45,
  "messstellenbetrieb_eur": 250,
  "is_producing": true,
  "unternehmen": "Muster GmbH",
  "berichtsjahr": 2026
}
```

### Ausgabe (gekürzt)
```json
{
  "pruefnummer": "SAP-20260504-A1B2C3D4",
  "status": "AUDIT_READY",
  "netzbetreiber": "SWM Infrastruktur (München)",
  "kalkulation": {
    "brutto_gesamt_eur": 29847.50,
    "arbeitspreis_netto_ct_kwh": 19.834,
    "stromsteuer_entlastung_9b_eur": 2500.00,
    "stromnev_kategorie": "A (≤1 Mio kWh)"
  },
  "esg": {
    "co2_footprint_tonnen": 45.875,
    "scope": "Scope 2 – Location-based (GHG Protocol)"
  },
  "report_url": "https://api.apify.com/v2/key-value-stores/.../records/audit_report.html"
}
```

---

## 📂 Output

Nach jeder Ausführung werden **automatisch** generiert:

| Output | Format | Beschreibung |
|---|---|---|
| `audit_report.html` | HTML | Vollständiger, druckbarer Audit-Vorbereitungsbericht |
| Dataset-Eintrag | JSON | Alle Berechnungsdaten maschinenlesbar |
| `OUTPUT` | JSON | Direktzugriff auf alle Ergebnisse |

### Der HTML Audit-Bericht enthält:
- ✅ Stammdaten & Eingabeparameter
- ✅ KPI-Übersicht (8 Kennzahlen)
- ✅ Vollständige Kostenaufschlüsselung (10 Komponenten + MwSt.)
- ✅ Angewendete Tarife & Gesetzesgrundlagen
- ✅ §9b-Entlastungsberechnung (wenn zutreffend)
- ✅ §19 StromNEV Hinweis (wenn zutreffend)
- ✅ ESG / Scope-2-CO₂-Bericht (ESRS E1)
- ✅ Compliance-Checkliste mit Handlungsempfehlungen
- ✅ Alle Rechtsgrundlagen
- ✅ Quellennachweise & Haftungshinweis

---

## ⚖️ Angewendete Rechtsgrundlagen & Normen

- **EnWG** – Energiewirtschaftsgesetz
- **StromStG §9b** – Spitzenausgleich Produzierendes Gewerbe
- **KWKG 2016/2020** – Kraft-Wärme-Kopplungsgesetz
- **StromNEV §19 Abs.2** – Aufschlag für besondere Netznutzung
- **EEG 2023** – Erneuerbare-Energien-Gesetz
- **KAV §2** – Konzessionsabgabenverordnung
- **UStG §12** – Umsatzsteuergesetz (19% MwSt.)
- **EU CSRD 2022/2464 / ESRS E1** – Corporate Sustainability Reporting Directive (Klimawandel, Scope 2)
- **GHG Protocol Corporate Standard** – Scope 1/2/3 Emissionsberechnung
- **ISO 50001:2018** – Energiemanagementsystem
- **DIN EN 16247-1** – Energieaudits
- **§8 EDL-G** – Energiedienstleistungsgesetz (Energieaudit-Pflicht)
- **EU Taxonomy Regulation 2020/852 Art.8** – Taxonomie-Verordnung

---

## ⚙️ Technische Details

| Merkmal | Detail |
|---|---|
| **Marktpreis-Quelle** | SMARD (Bundesnetzagentur) – kostenlose offizielle API |
| **Marktdaten** | EPEX Spot Day-Ahead, stündliche Auflösung, Filter 4169 |
| **PLZ-Abdeckung** | Alle deutschen Postleitzahl-Präfixe 00–99 |
| **Umlagen** | Offiziell 2026 (ÜNB-Veröffentlichung Okt. 2025) |
| **Emissionsfaktor** | UBA/IFEU Strommix Deutschland 2025 (367 g CO₂e/kWh) |
| **Antwortzeit** | ~3–8 Sekunden (inkl. SMARD-Abruf) |
| **Externe APIs** | Nur SMARD (Bundesnetzagentur) – keine kostenpflichtigen Dienste |
| **Architektur** | Stateless Apify Actor, Python 3.11 |
| **Output-Format** | JSON (Dataset) + HTML (Key-Value Store) |

---

## ⚠️ Wichtige Hinweise (Positionierung & Haftung)

**Dieser Bericht ist ein Audit-Vorbereitungsdokument (Pre-Audit).**

Er dient als:
1. Fundierte Vorbereitung für die formale Energiefachprüfung
2. Strukturierte Datenbasis für Steuerberater, Energieberater und Wirtschaftsprüfer
3. Entscheidungsgrundlage für Budgetplanung und ESG-Reporting
4. CSRD/ESRS E1 Rohdatenlieferant

**Er ersetzt nicht:**
- Eine zertifizierte Energieprüfung nach DIN EN 16247-1
- Einen steuerrechtlich geprüften Bescheid gemäß §9b StromStG
- Ein formales Energiemanagementsystem nach ISO 50001
- Die Endvalidierung durch einen zugelassenen Wirtschaftsprüfer (WP/vBP)

---

## 🚀 Starten

```bash
# Direkt über die Apify Plattform
apify run stromaudit-pro

# Oder über die API
curl -X POST https://api.apify.com/v2/acts/[actor-id]/runs \
  -H "Authorization: Bearer [API_TOKEN]" \
  -d '{"plz":"80331","jahresverbrauch_kwh":125000,"spitzenlast_kw":45,"is_producing":true}'
```

---

## 📋 Eingabeparameter

| Parameter | Typ | Pflicht | Beschreibung |
|---|---|---|---|
| `plz` | String | ✅ | 5-stellige Postleitzahl |
| `jahresverbrauch_kwh` | Number | ✅ | Jahresverbrauch in kWh |
| `spitzenlast_kw` | Number | ✅ | Spitzenlast / Jahresleistungsmaximum in kW |
| `messstellenbetrieb_eur` | Number | ○ | Messstellenbetriebskosten €/Jahr (Standard: 250) |
| `is_producing` | Boolean | ○ | Produzierendes Gewerbe §9b StromStG (Standard: false) |
| `unternehmen` | String | ○ | Unternehmensname (erscheint auf Report) |
| `anschrift` | String | ○ | Straße & Hausnummer (erscheint auf Report) |
| `berichtsjahr` | Integer | ○ | Berichtsjahr (Standard: aktuelles Jahr) |

---

## ⚖️ Haftungsausschluss

Alle Berechnungen basieren auf öffentlich verfügbaren Referenzdaten und standardisierten Annahmen. Die Ergebnisse dienen ausschließlich zur Orientierung und zur Vorbereitung von Analysen. Keine Haftung für Vollständigkeit oder Richtigkeit. Die verbindliche Prüfung obliegt Fachleuten.

---

© 2026 StromAudit Pro – Energieanalyse & ESG-Compliance für Deutschland
