"""
scripts/probe_xbet_sports.py — sonde jetable des sources d'odds gratuites.

Pourquoi : core/harvester.py SPORT_IDS n'interroge que 4 sports
(soccer/tennis/basketball/mma) sur le feed 1XBet/Melbet. Table tennis, volley,
handball et eSports n'ont donc AUCUNE source gratuite — ils dépendent à 100%
de la recherche web (Groq/Tavily), c'est-à-dire de la ressource qui meurt en
premier. Le 2026-08-02 ils étaient muets toute la soirée pour cette raison.

Deux pistes sont testées ici, et elles ne répondent PAS à la même question :

  1XBet/Melbet LineFeed — déjà câblé, JSON public, sans auth. Ne donne que le
  côté SOFT. Le prix Pinnacle reste à chercher par IA, donc le poste de coût
  principal survit.

  Oddspedia — agrégateur multi-bookmakers qui couvre les sports alternatifs ET
  cote Pinnacle. S'il est joignable, il fournit soft ET sharp dans le même
  appel, ce qui supprimerait la dépendance Groq pour la découverte de prix.
  Renvoie 403 (Cloudflare) depuis un sandbox : le seul moyen de savoir s'il
  répond aux runners GitHub, qui sont aussi des IP datacenter, est d'essayer.

Script et workflow (.github/workflows/probe_xbet.yml) sont à supprimer une
fois le relevé fait.
"""
import json
import sys
import urllib.error
import urllib.request

XBET_TPL = ("https://1xbet.com/LineFeed/Get1x2?sport={sid}"
            "&count=5&lng=en&mode=4&partner=157")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Ce qu'on cherche, pour repérer les IDs utiles dans le bruit.
WANTED = ("table tennis", "volleyball", "handball", "counter-strike", "cs2",
          "dota", "league of legends", "valorant", "e-sport", "esport", "mma",
          "ufc", "mixed martial")


def _get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


# ── Piste 1 : 1XBet/Melbet LineFeed ──────────────────────────────────
def probe_xbet(sid: int):
    try:
        _, body = _get(XBET_TPL.format(sid=sid))
        data = json.loads(body)
    except Exception:
        return None

    events = data.get("Value") or []
    if not events:
        return None
    first = events[0]
    # SN = nom du sport, O1/O2 = les deux camps.
    name = first.get("SN") or first.get("SE") or "?"
    return name, len(events), f"{first.get('O1', '?')} vs {first.get('O2', '?')}"


def run_xbet(lo: int, hi: int) -> list[tuple[int, str]]:
    print(f"\n{'=' * 72}\n1XBet/Melbet LineFeed — IDs {lo}..{hi}\n{'=' * 72}", flush=True)
    found = []
    for sid in range(lo, hi + 1):
        res = probe_xbet(sid)
        if res:
            name, n, sample = res
            found.append((sid, name))
            print(f"  sport={sid:<4} {name:<28} {n} events | {sample}", flush=True)
    print(f"\n  -> {len(found)} IDs actifs", flush=True)

    hits = [(s, n) for s, n in found if any(w in n.lower() for w in WANTED)]
    if hits:
        print("\n  SPORTS RECHERCHÉS — à câbler dans core/harvester.py SPORT_IDS :", flush=True)
        for sid, name in hits:
            print(f"    {sid}: {name}", flush=True)
    else:
        print("\n  Aucun sport recherché actif dans cette plage.", flush=True)
    return found


# ── Piste 2 : Oddspedia ──────────────────────────────────────────────
# Endpoints candidats. On ne sait pas lequel (s'il y en a un) répond sans
# navigateur : le but est précisément de le découvrir, pas de le supposer.
ODDSPEDIA_URLS = [
    "https://oddspedia.com/api/v1/getSports?geoCode=GB&lang=en",
    "https://oddspedia.com/api/v1/getSports",
    "https://api.oddspedia.com/api/v1/getSports",
    "https://oddspedia.com/api/v1/getMatchList?sport=table-tennis&lang=en&geoCode=GB",
    "https://oddspedia.com/",
]


def run_oddspedia() -> bool:
    print(f"\n{'=' * 72}\nOddspedia — joignable depuis un runner GitHub ?\n{'=' * 72}", flush=True)
    ok = False
    for url in ODDSPEDIA_URLS:
        try:
            status, body = _get(url, timeout=20)
            head = body[:300].replace("\n", " ")
            print(f"  {status}  {url}\n        {head}", flush=True)
            if status == 200 and body.lstrip()[:1] in "{[":
                ok = True
        except urllib.error.HTTPError as e:
            print(f"  {e.code}  {url}  ({e.reason})", flush=True)
        except Exception as e:
            print(f"  ---  {url}  {type(e).__name__}: {e}", flush=True)

    print(f"\n  -> JSON exploitable : {'OUI' if ok else 'NON'}", flush=True)
    if not ok:
        print("     Cloudflare bloque aussi les IP datacenter GitHub. Oddspedia\n"
              "     ne peut pas servir de source directe depuis Actions.", flush=True)
    return ok


def main() -> int:
    lo, hi = 1, 130
    if len(sys.argv) == 3:
        lo, hi = int(sys.argv[1]), int(sys.argv[2])

    run_oddspedia()
    run_xbet(lo, hi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
