/**
 * scripts/cloudflare_relay_worker.js — relais pour les sources filtrées par IP.
 *
 * POURQUOI CE FICHIER EXISTE
 * ---------------------------
 * odds.500.com n'est pas en panne : elle refuse les plages d'IP des runners
 * GitHub Actions. Mesuré le 2026-08-26 — HTTP 200 et 15 fixtures depuis un
 * poste de développement, `Connection refused` depuis Azure, à chaque run.
 * Aucune correction de code ne lève un blocage d'IP ; il faut sortir par une
 * autre adresse. Ce Worker est cette adresse.
 *
 * Il refait la requête depuis l'edge Cloudflare et rend la réponse telle
 * quelle. Côté Python, c'est `core/net.py` (mode RELAIS) qui réécrit l'URL.
 *
 * ⚠️ CE QUI N'EST PAS GARANTI : rien ne dit que 500.com accepte les adresses
 * de sortie de Cloudflare. Le blocage observé vise GitHub/Azure ; que l'edge
 * Cloudflare passe se VÉRIFIE (voir « Vérifier » plus bas), ça ne se suppose
 * pas. Si Cloudflare est bloqué aussi, il faudra un proxy à IP dédiée.
 *
 * DEUX GARDES, NON NÉGOCIABLES
 * -----------------------------
 * Un relais qui va chercher n'importe quelle URL pour n'importe qui EST un
 * proxy ouvert : il sera trouvé et utilisé pour autre chose, sur votre quota
 * et sous votre responsabilité. D'où :
 *   1. RELAY_TOKEN — secret partagé, comparé en temps constant ;
 *   2. ALLOWED_HOSTS — liste blanche stricte. Même avec le jeton, aucune
 *      autre destination n'est relayée.
 * Ne retirez ni l'une ni l'autre « pour tester ».
 *
 * L'ENCODAGE NE DOIT PAS ÊTRE TOUCHÉ
 * -----------------------------------
 * 500.com sert du GB18030, pas de l'UTF-8. On renvoie donc le CORPS BRUT
 * (`response.body`, un flux d'octets) sans jamais appeler `.text()` : lire en
 * texte le ferait décoder en UTF-8 et réencoder, et tous les noms d'équipes
 * chinois arriveraient en mojibake côté Python — une panne silencieuse qui
 * ressemblerait à un parseur cassé.
 *
 * DÉPLOIEMENT (une fois, ~5 minutes)
 * -----------------------------------
 *   npm install -g wrangler && wrangler login
 *   wrangler init predator-relay --yes
 *   # remplacer src/index.js par CE fichier, puis :
 *   wrangler secret put RELAY_TOKEN        # collez une valeur longue et aléatoire
 *   wrangler deploy
 *
 * Puis, côté Predator (les deux, en secrets GitHub ou dans app_secrets) :
 *   FREE_SOURCES_RELAY=https://predator-relay.<votre-sous-domaine>.workers.dev
 *   FREE_SOURCES_RELAY_TOKEN=<la même valeur que RELAY_TOKEN>
 *
 * VÉRIFIER — dans cet ordre, aucune étape n'est facultative
 * ---------------------------------------------------------
 *   1. curl -s -o /dev/null -w '%{http_code}\n' \
 *        -H "X-Relay-Token: <jeton>" \
 *        "https://…workers.dev?u=https%3A%2F%2F<hote-autorise>%2F"
 *      → 200 attendu. 403 = jeton faux ou hôte hors liste blanche.
 *        Un 502 ici veut dire que Cloudflare N'ARRIVE PAS à joindre l'amont :
 *        c'est le scénario « l'edge est bloqué aussi », il faut un vrai proxy.
 *   2. un run GitHub Actions                → SEUL juge qui compte : le poste
 *      de développement joint déjà l'amont sans relais, il ne prouve rien.
 *
 * COÛT : palier gratuit, 100 000 requêtes/jour.
 *
 * LISTE BLANCHE VIDE depuis le 2026-09-03 : odds.500.com et 7M, les hôtes
 * pour qui ce relais a été écrit, sont retirés du pipeline (mur anti-bot
 * EdgeOne sur 500.com, décision opérateur). Un relais sans hôte ne relaie
 * rien — c'est le comportement sûr. Le consommateur restant de core/net.py
 * est core/score_sources.py (ESPN, TheSportsDB, MLB) : si une de ces sources
 * se fait refuser depuis les runners, inscrire SON hôte ici et redéployer.
 */

const ALLOWED_HOSTS = new Set([
  // ex. "site.api.espn.com",  // core/score_sources.py — scoreboard
]);

// En-têtes de la réponse d'origine qu'on ne recopie PAS : ils décrivent le
// transport entre Cloudflare et l'origine, pas notre réponse. `content-encoding`
// en tête : `fetch` a déjà décompressé le corps, le réannoncer ferait échouer
// la décompression côté client.
const HOP_BY_HOP = new Set([
  "content-encoding", "content-length", "transfer-encoding",
  "connection", "keep-alive", "set-cookie",
]);

/** Comparaison à temps constant — une égalité `===` sur un secret fuit sa
 *  longueur et son préfixe par la durée de comparaison. */
function tokenOk(given, expected) {
  if (!expected) return false;                 // pas de secret => tout est refusé
  const a = new TextEncoder().encode(given || "");
  const b = new TextEncoder().encode(expected);
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET") {
      return new Response("method not allowed", { status: 405 });
    }
    if (!tokenOk(request.headers.get("X-Relay-Token"), env.RELAY_TOKEN)) {
      // Volontairement muet : ne pas dire si c'est le jeton ou l'hôte qui
      // cloche évite de transformer ce point d'entrée en oracle.
      return new Response("forbidden", { status: 403 });
    }

    const raw = new URL(request.url).searchParams.get("u");
    if (!raw) return new Response("missing u", { status: 400 });

    let target;
    try {
      target = new URL(raw);
    } catch {
      return new Response("bad u", { status: 400 });
    }
    if (target.protocol !== "https:" || !ALLOWED_HOSTS.has(target.hostname)) {
      return new Response("forbidden", { status: 403 });
    }

    let upstream;
    try {
      upstream = await fetch(target.toString(), {
        method: "GET",
        headers: {
          // On transmet le User-Agent HONNÊTE de Predator : le relais ne sert
          // pas à se déguiser, seulement à sortir par une autre adresse.
          "User-Agent": request.headers.get("User-Agent")
            || "PredatorPAIM/1.0 (private non-commercial sports-betting pipeline)",
          "Accept": request.headers.get("Accept") || "*/*",
        },
        redirect: "follow",
      });
    } catch (e) {
      // 502 = « Cloudflare n'a pas pu joindre l'origine ». Distinct d'un 403
      // (nous qui refusons) : ce code-là dit que l'edge est bloqué aussi.
      return new Response(`upstream unreachable: ${e}`, { status: 502 });
    }

    const headers = new Headers();
    for (const [k, v] of upstream.headers) {
      if (!HOP_BY_HOP.has(k.toLowerCase())) headers.set(k, v);
    }
    // Trace utile dans les logs de cron : confirme que la réponse a bien
    // transité par le relais, et non en direct.
    headers.set("X-Relay-By", "predator");

    // `upstream.body` = octets bruts. NE PAS passer par .text() (cf. en-tête).
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  },
};
