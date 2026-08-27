// Chien de garde des crons GitHub Actions — Worker Cloudflare.
//
// POURQUOI : mesuré le 2026-08-27, le scheduler GitHub ne livrait qu'une
// fraction des crons du dépôt (closing_line ~5 % — 3 ticks sur ~63, l'audit
// de 12:00 UTC jamais tiré, un trou de 9 h sur scan.yml), alors que chaque
// run livré était vert. Ce Worker tourne toutes les 10 minutes et, pour
// chaque workflow surveillé, vérifie l'âge du dernier run : s'il dépasse le
// seuil (cadence nominale + marge), il déclenche un workflow_dispatch de
// rattrapage via l'API GitHub.
//
// CE QUE CE N'EST PAS : un poller GitHub de plus. La leçon du 2026-07-07
// (on_demand.yml, 288 déclenchements/jour qui étouffaient le scheduler)
// interdit d'ajouter des schedules GitHub ; ce Worker vit HORS de GitHub et
// ne dispatche que quand un cron a démontrablement raté — jamais en doublon
// d'un run frais. Les groupes `concurrency` des workflows sérialisent le cas
// limite (cron GitHub en retard qui atterrit juste après un rattrapage).
//
// INVARIANTS (gardés par tests/test_watchdog_worker.py) :
//  - chaque fichier surveillé existe et porte `workflow_dispatch:` ;
//  - chaque seuil est STRICTEMENT au-dessus de la cadence nominale du cron
//    le plus fréquent du workflow — le chien de garde ne peut donc jamais
//    tirer plus vite que le schedule qu'il supplée (closing_line garde son
//    invariant CLOSING_LINE_REFRESH_MIN) ;
//  - scan.yml n'est rattrapé qu'en mode `golden` (tick horaire GRATUIT :
//    sans OddsAPI il sort avant le Tier 2, mais porte purge, heartbeat,
//    REPRICE et la passe closing line). Rattraper standard/deep/guerrilla
//    doublerait la dépense des budgets journaliers des sources gratuites —
//    voir « L'arbitrage de cadence » (skill predator-pipeline) ;
//  - aucun secret dans ce fichier : le PAT est un secret du Worker
//    (WATCHDOG_PAT), posé par scripts/deploy_watchdog_worker.py.
//
// Déploiement : python scripts/deploy_watchdog_worker.py (idempotent).

const REPO = "ipotrading-bot/predator";

// stale_min = cadence nominale du cron + marge. Le Worker passe toutes les
// 10 min : le retard maximal de détection est stale_min + 10.
const WATCH = [
  { file: "scan.yml",         stale_min: 75,  inputs: { mode: "golden" } },
  { file: "closing_line.yml", stale_min: 25,  inputs: {} },
  { file: "audit.yml",        stale_min: 370, inputs: {} },
  { file: "reports.yml",      stale_min: 130, inputs: { report: "rapport" } },
];

async function gh(env, path, init = {}) {
  return fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      "Authorization": `Bearer ${env.WATCHDOG_PAT}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "predator-watchdog",
      ...(init.headers || {}),
    },
  });
}

async function tick(env) {
  for (const w of WATCH) {
    try {
      const r = await gh(
        env,
        `/repos/${REPO}/actions/workflows/${w.file}/runs?per_page=1&branch=main`,
      );
      if (!r.ok) {
        console.log(`${w.file}: liste des runs HTTP ${r.status}`);
        continue;
      }
      const data = await r.json();
      const last = (data.workflow_runs || [])[0];
      // Un run `queued`/`in_progress` compte : son created_at est posé, donc
      // un rattrapage déjà dispatché n'est jamais re-dispatché.
      const ageMin = last
        ? (Date.now() - Date.parse(last.created_at)) / 60000
        : Infinity;
      if (ageMin <= w.stale_min) {
        console.log(`${w.file}: frais (${ageMin.toFixed(0)} min)`);
        continue;
      }
      const d = await gh(
        env,
        `/repos/${REPO}/actions/workflows/${w.file}/dispatches`,
        { method: "POST", body: JSON.stringify({ ref: "main", inputs: w.inputs }) },
      );
      // 204 = dispatché. Tout autre code est loggé mais jamais retenté dans
      // le même tick : le prochain passage (10 min) refera le constat.
      console.log(
        `${w.file}: en retard de ${ageMin.toFixed(0)} min -> dispatch HTTP ${d.status}`,
      );
    } catch (e) {
      console.log(`${w.file}: ${e}`);
    }
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
  // Pas de surface HTTP : ce Worker n'a pas besoin de sous-domaine (les
  // crons Cloudflare s'exécutent sans URL publique — contrairement au
  // relais, qui lui doit être joignable).
  async fetch() {
    return new Response("predator-watchdog: cron only", { status: 404 });
  },
};
