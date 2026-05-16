// api/run-actor.js
// Zwischenschicht: empfängt Formulardaten von danke.html
// und leitet diese an Apify weiter — API-Token bleibt serverseitig verborgen.

export default async function handler(req, res) {

  // Nur POST erlaubt
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Methode nicht erlaubt." });
  }

  // Token und Actor-ID aus Vercel-Umgebungsvariablen
  const APIFY_TOKEN    = process.env.APIFY_TOKEN;
  const APIFY_ACTOR_ID = process.env.APIFY_ACTOR_ID;

  if (!APIFY_TOKEN || !APIFY_ACTOR_ID) {
    return res.status(500).json({ error: "Serverkonfiguration unvollständig." });
  }

  let input;
  try {
    input = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
  } catch {
    return res.status(400).json({ error: "Ungültige Eingabedaten." });
  }

  try {
    // ── Apify-Run starten ──
    const startResp = await fetch(
      `https://api.apify.com/v2/acts/${APIFY_ACTOR_ID}/runs?token=${APIFY_TOKEN}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(input),
      }
    );

    if (!startResp.ok) {
      const err = await startResp.json().catch(() => ({}));
      return res.status(502).json({ error: err?.error?.message || `Apify HTTP ${startResp.status}` });
    }

    const startData = await startResp.json();
    const runId = startData?.data?.id;

    if (!runId) {
      return res.status(502).json({ error: "Keine Run-ID von Apify erhalten." });
    }

    // ── Warten bis Run abgeschlossen (max. 120 Sekunden) ──
    const maxWait  = 120;
    const interval = 3;
    let elapsed    = 0;

    while (elapsed < maxWait) {
      await new Promise(r => setTimeout(r, interval * 1000));
      elapsed += interval;

      const statusResp = await fetch(
        `https://api.apify.com/v2/actor-runs/${runId}?token=${APIFY_TOKEN}`
      );
      const statusData = await statusResp.json();
      const status = statusData?.data?.status;

      if (status === "SUCCEEDED") {
        const storeId = statusData.data.defaultKeyValueStoreId;

        // Bericht-URL zusammenstellen — Token bleibt serverseitig
        const reportUrl = `https://api.apify.com/v2/key-value-stores/${storeId}/records/audit_report.html`;

        // Metadaten für die Ergebnisanzeige abrufen
        let meta = {};
        try {
          const outResp = await fetch(
            `https://api.apify.com/v2/key-value-stores/${storeId}/records/OUTPUT?token=${APIFY_TOKEN}`
          );
          meta = await outResp.json();
        } catch (_) {}

        return res.status(200).json({ reportUrl, meta });
      }

      if (["FAILED", "ABORTED", "TIMED-OUT"].includes(status)) {
        return res.status(502).json({ error: `Berichterstellung fehlgeschlagen: ${status}` });
      }
    }

    return res.status(504).json({ error: "Timeout: Berichterstellung hat zu lange gedauert." });

  } catch (err) {
    return res.status(500).json({ error: err.message || "Unbekannter Serverfehler." });
  }
}
