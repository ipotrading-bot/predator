"""
scripts/probe_xbet_sports.py — sonde jetable des sources d'odds gratuites.

Pourquoi : core/harvester.py SPORT_IDS n'interroge que 4 sports
(soccer/tennis/basketball/mma). Table tennis, volley, handball et eSports
n'ont donc AUCUNE source gratuite et dépendent à 100% de la recherche web
(Groq/Tavily), c'est-à-dire de la ressource qui meurt en premier. Le
2026-08-02 ils étaient muets toute la soirée pour cette seule raison.

La sonde réutilise `core.harvester._fetch_from_book` au lieu de refaire ses
requêtes : une première version tapait `1xbet.com` en direct sans Referer et
concluait « 0 ID actif » alors que le harvester, lui, ramenait 6 matchs au même
moment (run 30768709368). Cloudflare redirige cet hôte ; seule la cascade des
6 URLs sur 3 hôtes de SOFT_BOOKS passe. Sonder par un chemin de code parallèle,
c'est sonder autre chose que la production.

Oddspedia est testé au passage : il agrège de nombreux books DONT Pinnacle, ce
qui supprimerait la dépendance Groq pour la découverte de prix — mais il est
derrière Cloudflare.

Script et workflow (.github/workflows/probe_xbet.yml) sont à supprimer une
fois le relevé fait.
"""
import json
import logging
import sys
import time
import urllib.error
import urllib.request

logging.basicConfig(level=logging.CRITICAL)  # le harvester loggue chaque échec

from core.harvester import SOFT_BOOKS, _fetch_from_book  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Ce qu'on cherche, pour repérer les IDs utiles dans le bruit.
WANTED = ("table tennis", "tabletennis", "volleyball", "handball",
          "counter-strike", "cs2", "dota", "league of legends", "valorant",
          "e-sport", "esport", "mma", "ufc", "mixed martial")


# ── Piste 1 : le feed déjà câblé, via le vrai chemin de code ─────────
def probe_sport(sport_id: int, books):
    """Renvoie (nom_du_sport, nb_events, exemple, book) ou None."""
    for book, (tpls, referer) in books:
        try:
            raw = _fetch_from_book(book, tpls, referer, sport_id)
        except Exception:
            continue
        if not raw:
            continue
        first = raw[0]
        # SN = nom du sport côté bookmaker, O1/O2 = les deux camps.
        name = first.get("SN") or first.get("SE") or "?"
        sample = f"{first.get('O1', '?')} vs {first.get('O2', '?')}"
        return name, len(raw), sample, book
    return None


def _pick_book():
    """Trouve le book qui répond, sur un sport connu pour être toujours actif.

    Renvoie None si AUCUN ne répond — et l'appelant doit alors abandonner tout
    de suite. Le run 30769689944 a passé 25 minutes à balayer 70 IDs alors que
    le football lui-même ne répondait déjà pas : chaque ID retentait 3 books ×
    6 URLs pour rien, et le job est mort sur le timeout sans imprimer une seule
    ligne exploitable. Un sport de contrôle muet ne dit rien sur les IDs, il
    dit que le feed est injoignable — presque sûrement parce que le balayage
    précédent (130 IDs × 3 books, 21:55 UTC) a fait limiter l'IP du runner.

    D'où les tentatives espacées : c'est un back-off, pas de l'insistance.
    """
    for attempt in range(3):
        for book, (tpls, referer) in SOFT_BOOKS.items():
            try:
                if _fetch_from_book(book, tpls, referer, 1):   # 1 = football
                    print(f"  book joignable : {book} "
                          f"(tentative {attempt + 1})", flush=True)
                    return [(book, SOFT_BOOKS[book])]
            except Exception:
                continue
        if attempt < 2:
            wait = 30 * (attempt + 1)
            print(f"  football muet sur les 3 books — nouvel essai dans {wait}s",
                  flush=True)
            time.sleep(wait)
    return None


def run_feed(lo: int, hi: int) -> None:
    print(f"\n{'=' * 74}\nFeed LineFeed (1xbet/melbet/22bet) — IDs {lo}..{hi}\n{'=' * 74}",
          flush=True)
    books = _pick_book()
    if books is None:
        print("\n  ABANDON — le feed est injoignable depuis ce runner. Ce n'est pas\n"
              "  une conclusion sur les IDs : le sport de contrôle (football, ID 1)\n"
              "  ne répond pas non plus, alors qu'il ramène des matchs dans les runs\n"
              "  guerrilla. Réessayer plus tard, sans balayage large entre-temps.",
              flush=True)
        return

    found = []
    for sid in range(lo, hi + 1):
        res = probe_sport(sid, books)
        if res:
            name, n, sample, book = res
            found.append((sid, name))
            print(f"  sport={sid:<4} {name:<26} {n:>3} events  [{book}]  {sample}",
                  flush=True)
        # Politesse : le balayage précédent a très probablement fait limiter
        # l'IP. Une seconde par ID coûte 40s sur la plage et protège le feed
        # dont dépendent les vrais scans.
        time.sleep(1)

    print(f"\n  -> {len(found)} IDs actifs", flush=True)
    if not found:
        print("     Aucun ID ne répond : le feed est injoignable depuis ce runner,\n"
              "     ce n'est PAS une conclusion sur les IDs eux-mêmes.", flush=True)
        return

    hits = [(s, n) for s, n in found if any(w in n.lower() for w in WANTED)]
    if hits:
        print("\n  SPORTS RECHERCHÉS — à câbler dans core/harvester.py SPORT_IDS :", flush=True)
        for sid, name in hits:
            print(f"    {sid}: {name}", flush=True)
    else:
        print("\n  Aucun sport recherché actif dans cette plage (carte de nuit ?).", flush=True)


# ── Piste 2 : Oddspedia ──────────────────────────────────────────────
ODDSPEDIA_URLS = [
    "https://oddspedia.com/api/v1/getSports?geoCode=GB&lang=en",
    "https://oddspedia.com/api/v1/getMatchList?sport=table-tennis&lang=en&geoCode=GB",
    "https://oddspedia.com/",
]


def run_oddspedia() -> bool:
    print(f"\n{'=' * 74}\nOddspedia — joignable depuis un runner GitHub ?\n{'=' * 74}",
          flush=True)
    ok = False
    for url in ODDSPEDIA_URLS:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://oddspedia.com/",
            })
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
            print(f"  {r.status}  {url}\n        {body[:200]}", flush=True)
            if r.status == 200 and body.lstrip()[:1] in "{[":
                ok = True
        except urllib.error.HTTPError as e:
            print(f"  {e.code}  {url}  ({e.reason})", flush=True)
        except Exception as e:
            print(f"  ---  {url}  {type(e).__name__}: {e}", flush=True)

    print(f"\n  -> JSON exploitable : {'OUI' if ok else 'NON'}", flush=True)
    if not ok:
        print("     Cloudflare bloque aussi les IP datacenter GitHub —\n"
              "     Oddspedia ne peut pas servir de source directe depuis Actions.",
              flush=True)
    return ok


def main() -> int:
    lo, hi = 1, 130
    if len(sys.argv) == 3:
        lo, hi = int(sys.argv[1]), int(sys.argv[2])
    run_oddspedia()
    run_feed(lo, hi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
