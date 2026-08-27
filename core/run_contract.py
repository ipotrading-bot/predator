"""
core/run_contract.py — PHASE B5 — Un run stérile ne sort plus en vert.

LE PROBLÈME QUE CE MODULE EXISTE POUR RENDRE VISIBLE
----------------------------------------------------
Ce dépôt échoue en silence, et il échoue en silence de la même façon depuis le
début : le travail ne se fait pas, rien ne lève, GitHub Actions affiche une
coche verte, et personne ne l'apprend avant des jours. Les cas déjà consignés
dans INCIDENTS.md et AUDIT.md :

  · 2026-07-07 — ~17 h de « 0/N signals persisted », chaque run vert, parce que
    chaque écriture échouait UNE PAR UNE sur une RLS 42501 ;
  · 2026-08-24 → 26 — l'audit rendait « 0 settled | 52 skipped » EN VERT
    pendant deux jours, les deux quotas de recherche ayant lâché ensemble ;
  · 2026-08-26 — cinq workflows sur six n'ont créé AUCUN job, sans log ni
    annotation, à cause d'un dump de secrets refusé par GitHub ;
  · 2026-08-27 (B4) — le disjoncteur de drawdown ne pouvait pas se déclencher :
    sa fenêtre réelle était d'UNE ligne décisive sur vingt.

Aucun de ces incidents n'était un plantage. Tous étaient des runs verts.

CE QUE CE MODULE DÉCIDE, ET CE QU'IL NE DÉCIDE PAS
---------------------------------------------------
Il ne juge PAS la qualité de ce qui a été produit : zéro signal est un
résultat parfaitement légitime — c'est même le résultat attendu depuis A1, et
la phase A6 l'a écrit noir sur blanc. Ce qu'il détecte est plus étroit et plus
grave : une CONTRADICTION INTERNE, un run qui a fait la moitié d'un travail.

  · des sources ont répondu, et pourtant aucun match n'en est sorti ;
  · des signaux ont été émis, et pas un seul n'a été persisté ;
  · des règlements étaient éligibles, et pas un seul n'a abouti.

Chacune de ces trois phrases décrit un pipeline cassé au milieu, jamais un
marché calme. C'est ce qui les rend utilisables comme critère d'échec.

⚠️ CE MODULE NE TOUCHE PAS AUX `except Exception` DU DÉPÔT, et c'est
volontaire : c'est le contrat de SORTIE qui rend leur silence détectable. Une
exception avalée ne se voit pas ; un run qui n'a rien produit alors qu'il
aurait dû, si.
"""
import logging

log = logging.getLogger("PREDATOR.run_contract")


def verdict_de_fin(*,
                   sources_joignables: bool = False,
                   matches_vus: int = 0,
                   signaux_emis: int = 0,
                   signaux_persistes: int = 0,
                   settlement_eligible: int = 0,
                   settlement_regles: int = 0) -> str | None:
    """
    Le motif d'échec du run, ou None s'il est légitimement vert.

    Fonction PURE : elle ne lit rien, n'écrit rien, ne sort pas du processus.
    L'appelant décide quoi faire du motif — c'est ce qui la rend testable sans
    rejouer un scan.

    Les trois conditions sont des CONJONCTIONS, jamais des seuils. « 0 match »
    seul ne prouve rien (un créneau creux existe) ; « 0 match ALORS QUE des
    sources ont répondu » prouve que le pipeline a perdu ce qu'on lui a donné.
    De même « 0 signal » est normal, « 0 PERSISTÉ sur N émis » ne l'est pas.
    """
    if sources_joignables and matches_vus == 0:
        return ("des sources ont répondu mais AUCUN match n'en est sorti — "
                "le pipeline a perdu en route ce qu'on lui a donné")
    if signaux_emis >= 1 and signaux_persistes == 0:
        return (f"{signaux_emis} signal(s) émis, AUCUN persisté — "
                f"ils n'existeront pour aucun règlement ni apprentissage")
    if settlement_eligible >= 1 and settlement_regles == 0:
        return (f"{settlement_eligible} règlement(s) éligible(s), AUCUN abouti — "
                f"l'échantillon ne grandit pas et les signaux vont expirer")
    return None


def terminer(motif: str | None, *, contexte: str = "run") -> None:
    """
    Sort du processus en ÉCHEC si `motif` est renseigné, sinon ne fait rien.

    Le log est en CRITICAL et nomme la contradiction : un opérateur qui ouvre
    un run rouge doit comprendre en une ligne ce qui manque, sans dérouler
    trente pages de logs de scan.
    """
    if not motif:
        return
    log.critical("RUN STÉRILE [%s] — %s. Sortie en ÉCHEC : un run qui n'a pas "
                 "fait son travail ne doit pas afficher une coche verte.",
                 contexte, motif)
    raise SystemExit(1)
