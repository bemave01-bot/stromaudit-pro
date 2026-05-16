// api/run-actor.js
// Tussenlaag: ontvangt formulierdata van danke.html
// en stuurt deze door naar Apify — API token blijft onzichtbaar.

export default async function handler(req, res) {

  // Alleen POST toegestaan
  if (req.method !== "POST") {
    return res.status(405).json({ error: "Method not allowed" });
  }

  // Token en Actor ID uit Vercel omgevingsvariabelen (de kluis)
  const APIFY_TOKEN    = process.env.APIFY_TOKEN;
  const APIFY_ACTOR_ID = process.env.APIFY_ACTOR_ID;

  if (!APIFY_TOKEN || !APIFY_ACTOR_ID) {
    return res.status(500).json({ error: "Serverconfiguratie ontbreekt." });
  }

  let input;
  try {
    input = typeof req.body === "string" ? JSON.parse(req.body) : req.body;
  } catch {
    return res.status(400).json({ error: "Ongeldige invoer." });
  }

  try {
    // ── Run starten bij Apify ──
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
      return res.status(502).json({ error: "Geen run-ID ontvangen van Apify." });
    }

    // ── Wachten tot run klaar is (max 120 seconden) ──
    const maxWait = 120;
    const interval = 3;
    let elapsed = 0;

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

        // Rapport-URL opbouwen — token zit server-side, niet in de HTML
        const reportUrl = `https://api.apify.com/v2/key-value-stores/${storeId}/records/audit_report.html`;

        // Metadata ophalen voor resultaatscherm
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
        return res.status(502).json({ error: `Run mislukt: ${status}` });
      }
    }

    return res.status(504).json({ error: "Timeout: rapport duurde te lang." });

  } catch (err) {
    return res.status(500).json({ error: err.message || "Onbekende serverfout." });
  }
}
