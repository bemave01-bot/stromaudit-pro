# ⚡ StromAudit Pro – Deutsche Energie-Compliance & ESG Pre-Audit Engine

> **Der umfassendste automatisierte Stromkosten-Audit für Deutschland.**  
> Live-Marktpreise · Alle PLZ · Vollkostenberechnung · CSRD/ESRS E1 · Audit-Ready Report · Niemals Absturz

---

## 🎯 Was ist StromAudit Pro?

**StromAudit Pro** ist eine vollautomatische **Pre-Audit Engine** für den deutschen Energiemarkt. Sie berechnet auf Basis offizieller Behördendaten, aktueller Live-Börsenpreise und gesetzlich verankerten Tarifen die vollständigen Stromkosten eines Unternehmensstandorts – und liefert einen **audit-ready HTML-Bericht**, der direkt dem Wirtschaftsprüfer oder Steuerberater übergeben werden kann.

**Datenquelle:** [SMARD – Bundesnetzagentur](https://www.smard.de) · Offizielle, kostenlose REST-API · Kein Scraping · Freie Nachnutzung gemäß DL-DE/BY-2-0.

---

## 🏆 Einzigartiges Merkmalset

| Merkmal | Einfache Rechner | StromAudit Pro |
|---|---|---|
| Marktpreis | Statisch | **Live SMARD (Bundesnetzagentur)** |
| PLZ-Abdeckung | Teilweise | **Alle deutschen PLZ (00–99)** |
| Kostentrennung | Nur Arbeitspreis | **Arbeitspreis + Leistungspreis + MSB** |
| Umlagen 2026 | Pauschal | **KWKG + Offshore + §19 StromNEV (Kat. A/B/C)** |
| §9b StromStG | Fehlt | **Automatisch berechnet + Entlastungsbetrag** |
| ESG / CO₂ | Fehlt | **Scope 2 · ESRS E1 · GHG Protocol** |
| Compliance | Fehlt | **EDL-G · ISO 50001 · CSRD · KAV** |
| Next Steps | Fehlt | **Terminierte Handlungsempfehlungen** |
| Eingabevalidierung | Kein Schutz | **Niemals Absturz – elegante Fehlermeldung** |
| Output | Rohdaten | **Audit-Ready HTML-Report + JSON** |
| Haftungsschutz | Minimal | **Vollständiger, expliziter Haftungsausschluss** |
| Zeitzone | UTC | **Europe/Berlin (MEZ/MESZ)** |

---

## 📂 Output & Bericht-Weitergabe

Nach jeder Ausführung stehen bereit:

| Output | Beschreibung |
|---|---|
| **`audit_report.html`** | Vollständiger Audit-Vorbereitungsbericht (Apify Key-Value Store) |
| **Dataset-Eintrag** | Alle Berechnungsdaten als JSON |
| **`OUTPUT`** | Direktzugriff auf Ergebnisse inkl. `report_url` |

### So gibt der Nutzer den Bericht weiter

**Option 1 – Als PDF drucken (empfohlen für Wirtschaftsprüfer):**
Der Bericht enthält einen **Drucken/PDF-Knopf** rechts oben. Klick → Browser-Druckdialog → "Als PDF speichern". Das PDF ist fertig für den Anhang im Jahresbericht.

**Option 2 – HTML-Datei herunterladen und per E-Mail senden:**
In Apify Console → Run → Key-Value Store → `audit_report.html` → Download. Als E-Mail-Anhang versenden. Der Empfänger öffnet die Datei lokal – vollständig offline, keine externen Abhängigkeiten.

**Option 3 – Direktlink (7 Tage gültig auf Free-Plan):**
Die `report_url` im OUTPUT-Feld ist ein direkter Link zur HTML-Datei. Auf dem kostenpflichtigen Apify-Plan ist die Aufbewahrungszeit bis zu 30 Tage.

**Empfehlung für Steuerberater/WP:** Option 1 (PDF) ist die professionellste Übergabeform.

---

## ⚙️ Wie funktioniert die SMARD-Anbindung?

SMARD (`smard.de`) ist die **offizielle Open-Data-Plattform der Bundesnetzagentur**. Es handelt sich um eine öffentliche JSON REST-API – **kein Website-Scraping**, keine Authentifizierung, keine Sperrgefahr. Die Daten werden gemäß [Datennutzungslizenz Deutschland DL-DE/BY-2-0](https://www.govdata.de/dl-de/by-2-0) frei zur Verfügung gestellt.

Technische Absicherung:
- Korrekte `User-Agent`-Header mit Kontaktangabe
- 3 automatische Retry-Versuche bei Netzwerkproblemen
- Fallback auf Ø-Referenzwert (89,30 EUR/MWh) – der Actor bricht **niemals** ab
- Fallback wird im Bericht sichtbar gekennzeichnet

---

## 🛡️ Eingabeschutz & Fehlerbehandlung

Der Actor **bricht niemals ab**. Bei fehlerhaften Eingaben:
- Validierung aller Pflichtfelder mit präzisen Fehlermeldungen
- Eleganter HTML-Fehlerbericht mit Korrekturanleitung
- Warnhinweise bei unplausiblen Werten (z.B. zu niedrige Volllaststunden)
- Automatische Fallback-Werte für optionale Felder

**Rate Limiting:** Der Actor ist auf max. 50 gleichzeitige Runs begrenzt (Apify-Konsoleneinstellung). Pro Actor-Run wird genau ein Bericht erstellt.

---

## 📋 Eingabeparameter

| Parameter | Typ | Pflicht | Standard | Beschreibung |
|---|---|---|---|---|
| `plz` | String | ✅ | — | 5-stellige deutsche PLZ |
| `jahresverbrauch_kwh` | Number | ✅ | — | Jahresverbrauch in kWh |
| `spitzenlast_kw` | Number | ✅ | — | Spitzenlast / Jahresleistungsmaximum in kW |
| `messstellenbetrieb_eur` | Number | ○ | 250 | Messstellenbetriebskosten €/Jahr |
| `is_producing` | Boolean | ○ | false | Produzierendes Gewerbe §9b StromStG |
| `unternehmen` | String | ○ | — | Unternehmensname (auf Bericht) |
| `anschrift` | String | ○ | — | Straße & Hausnummer (auf Bericht) |
| `berichtsjahr` | Integer | ○ | aktuell | Berichtsjahr (2020–2030) |

---

## 📊 Beispiel

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

---

## 🏗️ Architektur: Zwei Actors

### Actor 1: `stromaudit-pro` (dieser Actor)
On-Demand Rapport auf Abruf. Jede Ausführung = ein vollständiger Audit-Bericht.

### Actor 2: `stromaudit-daily` (separates Repo)
Täglicher Scheduler für Premium-Kunden. Läuft täglich um 07:00 Uhr (Europe/Berlin):
1. Holt einmal den SMARD-Marktpreis für den nächsten Tag
2. Liest aktive Kunden aus Airtable (Stripe-Zahler)
3. Versendet personalisierte Energie-Briefing-E-Mails via SendGrid
4. Rate-Limiting: max. 10 E-Mails gleichzeitig, 1 Sekunde Pause zwischen Batches

**Die beiden Actors sind unabhängig** – eigene GitHub-Repos, eigene Apify-Actors. Keine direkte API-Verbindung zwischen beiden. Sie teilen dieselbe Berechnungslogik, aber jeder Actor ist vollständig eigenständig.

---

## 💼 Verdienmodell (für Actor-Betreiber)

| Stufe | Preis | Leistung | Infrastruktur |
|---|---|---|---|
| **Basic** | $0,49/Bericht | On-Demand Audit | Apify Store (Pay-per-Event) |
| **Premium** | €149/Monat | Tägl. Energie-Briefing + monatl. Bericht | Stripe + Airtable + SendGrid + Apify |
| **Enterprise** | auf Anfrage | API-Integration Fabrikanlagen | Individuell |

---

## ⚖️ Angewendete Rechtsgrundlagen & Normen

- EnWG · StromStG §9b · KWKG 2016/2020 · StromNEV §19 Abs.2 · EEG 2023
- KAV §2 · UStG §12 · EU CSRD 2022/2464 / ESRS E1
- GHG Protocol Corporate Standard · ISO 50001:2018 · DIN EN 16247-1
- §8 EDL-G · EU Taxonomy Regulation 2020/852 Art.8 · UBA/IFEU Emissionsfaktoren

---

## ⚠️ Haftungsausschluss

StromAudit Pro ist ein vollautomatischer Aggregator öffentlicher Behördendaten. Der Betreiber ist kein Energieberater, Steuerberater oder Wirtschaftsprüfer. Keine Haftung für Berechnungsergebnisse oder darauf basierende Entscheidungen. Pre-Audit-Dokument – Endvalidierung durch zugelassenen WP/vBP oder Energieberater (§21 EDL-G) erforderlich.

---

© 2026 StromAudit Pro – Energieanalyse & ESG-Compliance für Deutschland
