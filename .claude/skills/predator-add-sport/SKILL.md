---
name: predator-add-sport
description: Procédure d'ajout d'un sport au pipeline PREDATOR — l'invariant des quatre fichiers synchrones, les tests gardiens, et ce qui relève de l'opérateur. Use when asked to add, enable, retire or "shadow" a sport, or when a sport-key appears in one file and not the others.
---

# Ajouter (ou retirer) un sport

## L'invariant des quatre fichiers — la raison d'être de cette skill

Tout sport-type présent dans `core/odds_api.py::SPORT_KEYS` (la vérité de ce
qui est réellement récupéré) DOIT exister dans :

1. `core/constants.py::KELLY_FRACTION` — la fraction de mise ;
2. `core/learning_layer.py::SPORT_DEFAULTS` — le seuil d'edge par défaut ;
3. `run_engine.py::SPORT_QUOTA` / `_SPORT_ORDER` — l'équilibrage du
   portefeuille (`_QUOTA_FAST`/`_QUOTA_DEEP`) ;
4. `api/index.py::_SPORT_EMOJI`/`_SPORT_LABEL`/... — l'affichage (superset
   toléré : un sport d'affichage sans flux est du cruft inoffensif, un sport
   de flux sans affichage sort en « 🎯 inconnu »).

Un sport absent d'un des quatre est scanné sans être appris, ou appris sans
être affiché — TOUJOURS en silence. Gardiens :
`tests/test_new_sports_phase3.py::TestInvariantDesQuatreFichiers` (y compris
`tennis`, invisible au test statique — clés dynamiques) et
`tests/test_dashboard_sports.py::test_tout_sport_actif_est_couvert`.

## Procédure

1. Lire `AUDIT.md` §3.11 (doctrine : le moteur ne parie que le favori court —
   un sport « à grosses cotes » ne changerait pas les cotes pariées) et les
   deux contraintes du prochain ajout : une source de SCORES doit exister
   (sinon les signaux restent `active` pour toujours), et le catalogue
   OddsAPI se sonde gratuitement (`/sports`, 0 crédit).
2. Poser la clé dans les quatre fichiers ci-dessus, plus le contexte de
   settlement si le nom d'équipe est ambigu (modèle : NCAAF,
   `tests/test_new_sports_phase3.py::TestNCAAF`).
3. `python -m pytest tests/test_new_sports_phase3.py tests/test_dashboard_sports.py -q`
   puis la suite entière.
4. NFL et sports à saison : vérifier `SEASON_OPENS` (pas de présaison).

## Ce qui est une décision OPÉRATEUR — jamais la tienne

`SHADOW_SPORTS` (émission fantôme), `RETIRED_SPORTS` et le périmètre sportif
sont la règle dure n°11 : ne pas les modifier sans instruction explicite
dans la session courante. Le hook `guard_operator_decisions.sh` force la
question sur `core/constants.py` ; ne cherche pas à le contourner.
