"""core/match_reel.py — deux signaux portent-ils sur le MÊME match réel ?

Pur (aucun accès réseau). Vivait dans run_engine.py (`_meme_match_reel`,
2026-09-30) ; déplacé ici le 2026-10-02 parce que le dashboard en a besoin
aussi : une seule définition pour l'émission ET pour l'affichage (règle n°6),
sans faire importer le moteur de scan à une page en lecture seule.

POURQUOI LE TENNIS A SA FENÊTRE ET SA LECTURE DES NOMS (2026-10-02).
« Xinran Sun vs Cristina Bucsa » (odds-api.io, 03/10 03:00 UTC) et « Sun
Xinran vs Cristina Bucsa » (OddsAPI, 03/10 07:30 UTC) sont le même match du
China Open. Le dashboard l'affichait en DEUX cartes et comptait « 5 matchs »
pour 4 ; la garde d'émission ne les voyait pas jumeaux non plus, donc le même
pari venu des deux sources serait sorti deux fois.
  - L'HEURE : un tournoi ne publie pas de coup d'envoi mais un ordre de jeu.
    Une source donne l'ouverture de la session (11:00 à Pékin), l'autre une
    estimation ; Sofascore annonçait 06:30 pour ce match. 30 minutes ne
    veulent rien dire ici. En simple, deux joueurs ne se rencontrent pas deux
    fois dans la demi-journée : la fenêtre du tennis est de 12 heures.
  - LE NOM : une source écrit le nom de famille en premier (ordre chinois),
    l'autre en dernier. `strict_team_match` compare des chaînes : il laisse
    passer « Xinran Sun »/« Sun Xinran » de justesse et refuse « Qinwen
    Zheng »/« Zheng Qinwen ». Pour un JOUEUR, les mêmes mots dans un autre
    ordre sont la même personne.
Les deux assouplissements ne valent que pour le tennis, et les DEUX camps
restent exigés.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from core.paim_engine import strict_team_match

# Écart de coup d'envoi toléré entre deux sources pour un même match : une
# équipe ne joue pas deux matchs en 30 minutes, et un doubleheader est à des
# heures d'écart. Rejoué le 2026-09-30 sur 677 signaux (tests/
# test_jumeaux_inter_sources.py).
JUMEAU_FENETRE_MIN = 30

# Exceptions par sport — voir l'en-tête. Toute entrée se justifie par une
# mesure et un test : élargir une fenêtre fait REFUSER des signaux.
JUMEAU_FENETRE_MIN_PAR_SPORT = {"tennis": 12 * 60}

# Sports dont les « camps » sont des PERSONNES : l'ordre des mots du nom ne
# distingue personne.
_SPORTS_A_JOUEURS = frozenset({"tennis"})

_MOT = re.compile(r"[a-z0-9]+")


def fenetre_jumeau_min(sport: str | None) -> int:
    """Fenêtre de coup d'envoi (minutes) dans laquelle deux libellés
    concordants sont le même match, pour ce sport."""
    return JUMEAU_FENETRE_MIN_PAR_SPORT.get((sport or "").lower(), JUMEAU_FENETRE_MIN)


def sans_accents(texte: str) -> str:
    """« Montréal » → « Montreal » : une source accentue, l'autre non."""
    return "".join(c for c in unicodedata.normalize("NFKD", texte or "")
                   if not unicodedata.combining(c))


def coup_d_envoi(valeur):
    """match_time → datetime UTC, None si illisible (on ne devine pas)."""
    try:
        t = datetime.fromisoformat(str(valeur).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _mots(nom: str) -> frozenset:
    return frozenset(_MOT.findall(sans_accents(nom).lower()))


def _meme_camp(sport: str, a: str, b: str) -> bool:
    """Deux libellés désignent-ils le même camp ? `strict_team_match`, plus —
    pour un JOUEUR seulement — les mêmes mots dans un autre ordre. Un nom d'un
    seul mot ne profite pas de cette voie : il n'a pas d'ordre à inverser."""
    if strict_team_match(a, b):
        return True
    if sport in _SPORTS_A_JOUEURS:
        ma, mb = _mots(a), _mots(b)
        return len(ma) >= 2 and ma == mb
    return False


def meme_match_reel(a: dict, b: dict, par_identifiant: bool = True) -> bool:
    """Deux signaux portent-ils sur le MÊME match réel ? Pur.

    Même match_id → oui (sauf `par_identifiant=False` : le dashboard, qui
    regroupe pour l'œil, ne veut que ce que l'opérateur perçoit — les noms et
    l'heure). Sinon il faut TOUT : même sport, coups d'envoi connus et à
    `fenetre_jumeau_min(sport)` près, les deux camps appariés (dans un sens ou
    dans l'autre : une source peut inverser domicile et extérieur sur terrain
    neutre). Un nom de moins de 3 lettres ou un libellé sans « vs » fait
    refuser : `strict_team_match` rend True sur un nom vide, et un doute ne
    doit jamais supprimer un vrai signal."""
    ida, idb = a.get("match_id"), b.get("match_id")
    if par_identifiant and ida and ida == idb:
        return True
    sport = (a.get("sport") or "").lower()
    if sport != (b.get("sport") or "").lower():
        return False
    ta, tb = coup_d_envoi(a.get("match_time")), coup_d_envoi(b.get("match_time"))
    if ta is None or tb is None:
        return False
    if abs((ta - tb).total_seconds()) > fenetre_jumeau_min(sport) * 60:
        return False
    camps = []
    for sig in (a, b):
        libelle = sans_accents(sig.get("match") or "")
        if " vs " not in libelle:
            return False
        h, v = (p.strip() for p in libelle.split(" vs ", 1))
        if len(h) < 3 or len(v) < 3:
            return False
        camps.append((h, v))
    (ha, va), (hb, vb) = camps
    return ((_meme_camp(sport, ha, hb) and _meme_camp(sport, va, vb))
            or (_meme_camp(sport, ha, vb) and _meme_camp(sport, va, hb)))
