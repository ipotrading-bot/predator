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
//  - la FRAÎCHEUR ne rattrape scan.yml qu'en `reprice` (gratuit : aucune clé
//    payante dans son pool) ; le scan payant `standard` passe par CRENEAUX,
//    dont les heures doivent être exactement celles du cron `standard` de
//    scripts/ci_scan_mode.py::CRON_MODES ;
//  - tout rattrapage nommé doit porter un run_name que scan.yml sait produire
//    (run-name), sinon le Worker ne verrait jamais son propre dispatch et
//    redispatcherait à chaque passage ;
//  - aucun secret dans ce fichier : le PAT est un secret du Worker
//    (WATCHDOG_PAT), posé par scripts/deploy_watchdog_worker.py.
//
// Déploiement : python scripts/deploy_watchdog_worker.py (idempotent).

const REPO = "ipotrading-bot/predator";

// FRAÎCHEUR — « plus rien depuis N minutes ». Valable seulement pour une
// cadence RÉGULIÈRE. stale_min = cadence nominale du cron + marge ; le Worker
// passe toutes les 10 min, donc le retard maximal de détection est
// stale_min + 10.
const WATCH = [
  { file: "scan.yml",         stale_min: 75,  inputs: { mode: "reprice" } },
  { file: "closing_line.yml", stale_min: 25,  inputs: {} },
  { file: "audit.yml",        stale_min: 190, inputs: {} },   // cadence 3 h (45 */3) depuis le 2026-09-05
  { file: "reports.yml",      stale_min: 130, inputs: { report: "rapport" } },
];

// CRÉNEAUX — pour une cadence IRRÉGULIÈRE, la fraîcheur ne marche pas. Le scan
// `standard` a des écarts de 2 h à 7 h (le trou de nuit 23:03→06:03) : un
// stale_min supérieur au plus petit écart (120 min) tirerait en pleine nuit,
// aux heures que le recalage du 2026-09-03 a précisément écartées. On compare
// donc au dernier créneau DÛ.
//
// Et on ne compte que les runs nommés `Scan standard` (run-name de scan.yml) :
// c'est là que se jouait l'angle mort du 2026-09-04. La surveillance par
// FICHIER voyait scan.yml éternellement frais — les ticks reprice horaires, y
// compris ceux que ce Worker dispatche lui-même, ne le laissent jamais
// vieillir de 75 min. Un cron `standard` perdu n'était donc pas seulement
// « non rattrapé » : il était invisible. Mesuré sur les 4 créneaux dus après
// le recalage : 19:03 livré +40 min, 21:03 jamais livré, 23:03 livré +1 h 47,
// 06:03 jamais livré — pendant que ~10 reprice/jour partaient, tous « sains ».
//
// DÉPENSE : rattraper un `standard` coûte un vrai scan (crédits OddsAPI +
// budgets gratuits). Le commentaire d'origine refusait ce rattrapage au nom de
// « l'arbitrage de cadence » du 2026-08-22 — arbitrage écrit quand le pool
// OddsAPI était MORT. Mesure du 2026-09-04 : 31 crédits/jour consommés pour
// une allocation de 118/j, titan007 à 12/500. DÉCISION OPÉRATEUR du
// 2026-09-04 : rattraper. Ne pas revenir en arrière sans une nouvelle mesure
// de consommation.
const CRENEAUX = [
  { file: "scan.yml", run_name: "Scan standard", minute: 3,
    hours: [6, 9, 11, 13, 16, 19, 21, 23], grace_min: 25,
    inputs: { mode: "standard" } },
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

/** Le dernier créneau dû (ms epoch UTC) à l'instant `now`, ou null. */
function dernierCreneauDu(hours, minute, now) {
  const d = new Date(now);
  const desc = [...hours].sort((a, b) => b - a);
  for (let jour = 0; jour < 2; jour++) {
    for (const h of desc) {
      const c = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - jour,
                         h, minute, 0, 0);
      if (c <= now) return c;
    }
  }
  return null;
}

async function creneau(env, w, now) {
  const du = dernierCreneauDu(w.hours, w.minute, now);
  if (du === null) return;
  const retard = (now - du) / 60000;
  const quand = new Date(du).toISOString().slice(11, 16);
  // Délai de grâce : GitHub livre souvent en retard. Dispatcher trop tôt
  // produirait DEUX scans payants pour un créneau que le cron honore encore.
  if (retard < w.grace_min) {
    console.log(`${w.file} ${w.run_name}: créneau ${quand} dû il y a ${retard.toFixed(0)} min — dans le délai de grâce`);
    return;
  }
  const r = await gh(env, `/repos/${REPO}/actions/workflows/${w.file}/runs?per_page=50&branch=main`);
  if (!r.ok) {
    console.log(`${w.file}: liste des runs HTTP ${r.status}`);
    return;
  }
  const data = await r.json();
  // Un rattrapage déjà dispatché porte le MÊME run-name : il honore le
  // créneau et empêche donc un second dispatch, `queued` compris.
  const livre = (data.workflow_runs || []).some(
    (run) => (run.name === w.run_name || run.display_title === w.run_name)
             && Date.parse(run.created_at) >= du);
  if (livre) {
    console.log(`${w.file} ${w.run_name}: créneau ${quand} honoré`);
    return;
  }
  const d = await gh(env, `/repos/${REPO}/actions/workflows/${w.file}/dispatches`,
                     { method: "POST", body: JSON.stringify({ ref: "main", inputs: w.inputs }) });
  console.log(`${w.file} ${w.run_name}: créneau ${quand} manqué de ${retard.toFixed(0)} min -> dispatch HTTP ${d.status}`);
}

async function tick(env) {
  const now = Date.now();
  for (const w of CRENEAUX) {
    try {
      await creneau(env, w, now);
    } catch (e) {
      console.log(`${w.file} ${w.run_name}: ${e}`);
    }
  }

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
