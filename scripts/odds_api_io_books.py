#!/usr/bin/env python3
"""
scripts/odds_api_io_books.py — le SECOND slot bookmaker d'odds-api.io.

POURQUOI CE SCRIPT EXISTE
-------------------------
Le côté SOFT est le goulot de tout le pipeline : le moteur ne peut pas
calculer un edge sur un match dont il n'a qu'un côté. Le plan gratuit
odds-api.io autorise DEUX bookmakers ; un seul (1xbet) est sélectionné.

Mesuré le 2026-08-27, scan de 20:00 :

    odds-api.io[soccer]: 8 matchs (0 avec prix sharp) / 60 à venir | books=1xbet

Huit matchs cotés sur soixante à venir. 1xbet ne price ni le NCAA, ni les
équipes réserves, ni plusieurs championnats mineurs — et refuser un match
faute de prix soft coûte autant qu'une source morte.

Le code n'a RIEN à changer pour en profiter : `core/odds_api_io.selected_
bookmakers()` interroge `/v3/bookmakers/selected` à l'exécution. Poser le
second slot suffit, et il est gratuit.

⛔ LE CHOIX DU SECOND BOOK N'EST PAS NEUTRE. MelBet est de la même famille
que 1xbet : mêmes lignes, aucune diversification, on paierait un slot pour
recopier le premier. Ce script ÉCARTE donc la famille 1xbet par défaut et
classe les candidats par couverture réelle du slate, pas par ordre
alphabétique. Un book à lignes indépendantes ajoute des matchs ET un vrai
line shopping — un meilleur prix exécutable sur le MÊME pari est un edge
honnête, pas l'artefact qu'A6 a supprimé.

USAGE
-----
    python scripts/odds_api_io_books.py list              # ce qui est posé + ce qui est disponible
    python scripts/odds_api_io_books.py suggest           # classe les candidats par couverture
    python scripts/odds_api_io_books.py set <book>        # pose le second slot (DEMANDE confirmation)
    python scripts/odds_api_io_books.py clear             # remet la sélection à zéro

La clé est lue comme partout ailleurs (`core/secret_store`) : `app_secrets`
d'abord, puis l'environnement. Aucune valeur de secret n'est imprimée.

⚠️ `set` et `clear` MODIFIENT LE COMPTE FOURNISSEUR, pas ce dépôt. Ils sont
donc interactifs et refusent d'agir sans confirmation explicite.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

import requests
from dotenv import load_dotenv

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

# Même contrat que scripts/ops.py : `.env` à la racine (gitignoré) ou
# l'environnement. En CI les secrets sont déjà posés, load_dotenv est un no-op.
load_dotenv()

from core import odds_api_io as oai          # noqa: E402
from core.secret_store import get_secret     # noqa: E402

# Familles à ne PAS proposer en second slot : mêmes lignes que le premier.
# Une liste tenue à la main est une liste qui diverge — celle-ci reste
# minuscule et sa raison d'être est écrite juste au-dessus.
FAMILLE_1XBET = {"1xbet", "melbet", "1xbit", "betwinner"}

# Sports interrogés pour mesurer la couverture. Le foot porte 100 % des
# signaux du système (INCIDENTS.md) : c'est lui qui tranche.
SPORTS_TEST = ("soccer",)

# Plafond de sondes : au-delà, on dépense le budget des scans pour choisir
# comment les nourrir.
MAX_SONDES = 12


def _key() -> str:
    k = get_secret("ODDS_API_IO_KEY")
    if not k:
        sys.exit("ODDS_API_IO_KEY absente (app_secrets ou environnement).")
    return k


def _bookmakers_disponibles(key: str) -> list[str]:
    status, body = oai._get("bookmakers", key, {})
    if status != 200 or body is None:
        sys.exit(f"impossible de lister les bookmakers (HTTP {status}).")
    if isinstance(body, dict):
        body = body.get("bookmakers") or body.get("data") or []
    noms = []
    for b in body or []:
        nom = b.get("key") or b.get("name") if isinstance(b, dict) else b
        if nom:
            noms.append(str(nom))
    return noms


def _slate_de_reference(key: str, cap: int | None = None) -> list[str]:
    """Identifiants des matchs à venir, tous candidats confondus.

    Chargé UNE fois : mesurer chaque book sur un slate différent ne
    comparerait rien. `pending` seulement — les statuts live/settled n'ont
    rien à faire dans une mesure pré-match, comme dans le scan.
    """
    from datetime import datetime, timedelta, timezone
    # `/odds/multi` n'accepte que MULTI_BATCH identifiants ; la valeur vit
    # dans core/odds_api_io, la recopier ici la ferait diverger.
    cap = cap or oai.MULTI_BATCH
    now = datetime.now(timezone.utc)
    ids: list[str] = []
    for sport in SPORTS_TEST:
        slug = oai.SPORTS[sport][0]
        status, body = oai._get("events", key, {
            "sport": slug,
            "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": str(cap),
        })
        if status != 200 or not isinstance(body, list):
            continue
        ids += [str(e.get("id")) for e in body
                if str(e.get("status", "")).lower() == "pending" and e.get("id")]
    return ids[:cap]


def cmd_list(key: str) -> None:
    poses = oai.selected_bookmakers(key, force=True)
    print(f"Slots posés ({len(poses)}/2) : {', '.join(poses) or '(aucun)'}")
    if len(poses) >= 2:
        print("Les deux slots sont pris — rien à gagner ici.")
    dispo = _bookmakers_disponibles(key)
    print(f"\n{len(dispo)} bookmakers proposés par le plan :")
    for nom in sorted(dispo):
        marque = ""
        if nom in poses:
            marque = "  ← posé"
        elif nom.lower() in FAMILLE_1XBET:
            marque = "  ← famille 1xbet, aucune diversification"
        print(f"  {nom}{marque}")


def cmd_suggest(key: str, candidats: list[str], marge: bool = False) -> None:
    """Classe les candidats NOMMÉS par NOMBRE DE MATCHS réellement cotés.

    Une couverture annoncée ne vaut rien : ce qui compte est le nombre de
    matchs du slate que le book price VRAIMENT. On le mesure, on ne le
    suppose pas — c'est la leçon de « 4 événements sur 10 reviennent sans
    aucune cote » avec 1xbet.

    ⚠️ UNE REQUÊTE PAR CANDIDAT, sur le budget de 400/jour que les scans se
    partagent. Le plan propose 276 books : les mesurer tous coûterait 70 % de
    la journée et affamerait le moteur pour choisir comment le nourrir. Les
    candidats sont donc NOMMÉS explicitement et plafonnés à MAX_SONDES.
    """
    poses = oai.selected_bookmakers(key, force=True)
    dispo = _bookmakers_disponibles(key)
    if not candidats:
        print("`suggest` attend les books à mesurer (une requête chacun, "
              f"budget partagé de {oai.DAILY_BUDGET}/jour).\n")
        print(f"{len(dispo)} books proposés — `list` les affiche tous. Exemple :")
        print("  python scripts/odds_api_io_books.py suggest bet365 pinnacle williamhill")
        return

    inconnus = [c for c in candidats if c not in dispo]
    if inconnus:
        sys.exit(f"inconnus du plan : {', '.join(inconnus)} (voir `list`).")
    if [c for c in candidats if c in poses]:
        sys.exit("un candidat est DÉJÀ posé — mesurer ce qu'on a ne dit rien "
                 "de ce qu'on gagnerait.")
    ecartes = [c for c in candidats if c.lower() in FAMILLE_1XBET]
    if ecartes:
        print(f"⚠️ {', '.join(ecartes)} : famille 1xbet, mêmes lignes que le "
              f"slot déjà posé — mesuré quand même, mais sans intérêt.\n")
    if len(candidats) > MAX_SONDES:
        sys.exit(f"{len(candidats)} candidats pour un plafond de {MAX_SONDES} : "
                 f"c'est le budget des scans qu'on dépenserait.")
    deja = oai.daily_quota.spent(oai.QUOTA_BUCKET)
    print(f"Budget odds-api.io : {deja}/{oai.DAILY_BUDGET} déjà dépensées "
          f"aujourd'hui, cette mesure en coûte {len(candidats)}.")
    if deja + len(candidats) > oai.DAILY_BUDGET and not marge:
        sys.exit(f"cette mesure dépasserait le budget que les scans se "
                 f"partagent ({oai.DAILY_BUDGET}/jour). Le plan réel est plus "
                 f"large — la différence est une marge de sûreté délibérée. "
                 f"`--marge` pour l'entamer sciemment (geste d'opérateur, "
                 f"quelques requêtes), sinon réessayer demain.")

    # Le slate de référence est chargé UNE fois et sert à tous les candidats :
    # comparer des books sur des slates différents ne comparerait rien.
    evenements = _slate_de_reference(key)
    if not evenements:
        sys.exit("aucun match à venir à mesurer.")
    print(f"\nSlate de référence : {len(evenements)} matchs à venir "
          f"({', '.join(SPORTS_TEST)}).\n")

    scores: Counter = Counter()
    for nom in candidats:
        status, body = oai._get("odds/multi", key, {
            "eventIds": ",".join(str(e) for e in evenements),
            "bookmakers": nom,
        })
        total = 0
        if status == 200 and isinstance(body, list):
            # Un événement SANS cote ne compte pas : c'est exactement le piège
            # de 1xbet, qui « répond » sur des matchs qu'il ne price pas.
            total = sum(1 for ev in body if (ev or {}).get("bookmakers"))
        scores[nom] = total
        pct = 100 * total / len(evenements)
        print(f"  {nom:<20} {total:>3}/{len(evenements)} matchs cotés  ({pct:.0f} %)")

    if not any(scores.values()):
        sys.exit("\nAucun candidat n'a coté un seul match — ne rien poser.")
    gagnant, n = scores.most_common(1)[0]
    print(f"\nRecommandé : {gagnant} ({n} matchs cotés).")
    print(f"Pour le poser :  python scripts/odds_api_io_books.py set {gagnant}")


def cmd_set(key: str, book: str, oui: bool) -> None:
    poses = oai.selected_bookmakers(key, force=True)
    if book in poses:
        sys.exit(f"{book} est déjà posé.")
    if book.lower() in FAMILLE_1XBET and not oui:
        sys.exit(f"{book} est de la famille 1xbet : mêmes lignes, aucune "
                 f"diversification. --oui pour passer outre.")
    if len(poses) >= 2:
        sys.exit(f"les deux slots sont pris ({', '.join(poses)}). "
                 f"`clear` d'abord si tu veux changer.")

    cible = poses + [book]
    print(f"Compte odds-api.io : {', '.join(poses) or '(vide)'} → {', '.join(cible)}")
    if not oui:
        if input("Confirmer cette modification du compte fournisseur ? [oui/non] ").strip().lower() != "oui":
            sys.exit("annulé.")
    r = requests.put(f"{oai.BASE_URL}/bookmakers/selected",
                     params={"apiKey": key},
                     json={"bookmakers": cible}, timeout=oai.TIMEOUT)
    if r.status_code >= 300:
        sys.exit(f"refus du fournisseur : HTTP {r.status_code} {(r.text or '')[:200]}")
    oai.reset_cache()
    print(f"Posé. Slots : {', '.join(oai.selected_bookmakers(key, force=True))}")
    print("Aucun déploiement nécessaire : selected_bookmakers() relit le compte "
          "à chaque run. Vérifier au prochain scan la ligne "
          "« odds-api.io[soccer]: N matchs … | books=… ».")


def cmd_clear(key: str, oui: bool) -> None:
    if not oui:
        if input("Vider la sélection de bookmakers du compte ? [oui/non] ").strip().lower() != "oui":
            sys.exit("annulé.")
    r = requests.put(f"{oai.BASE_URL}/bookmakers/selected/clear",
                     params={"apiKey": key}, timeout=oai.TIMEOUT)
    if r.status_code >= 300:
        sys.exit(f"refus du fournisseur : HTTP {r.status_code} {(r.text or '')[:200]}")
    oai.reset_cache()
    print("Sélection vidée.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("list", "suggest", "set", "clear"))
    ap.add_argument("book", nargs="*",
                    help="`set` : le bookmaker à poser. `suggest` : les candidats à mesurer.")
    ap.add_argument("--oui", action="store_true", help="ne pas demander confirmation")
    ap.add_argument("--marge", action="store_true",
                    help="entamer sciemment la marge de sûreté du budget "
                         "journalier (geste d'opérateur, quelques requêtes)")
    a = ap.parse_args()

    key = _key()
    if a.action == "list":
        cmd_list(key)
    elif a.action == "suggest":
        cmd_suggest(key, a.book, a.marge)
    elif a.action == "set":
        if len(a.book) != 1:
            sys.exit("`set` attend UN nom de bookmaker (voir `suggest`).")
        cmd_set(key, a.book[0], a.oui)
    else:
        cmd_clear(key, a.oui)


if __name__ == "__main__":
    main()
