---
name: predator-incident
description: Gabarit et procédure d'une entrée INCIDENTS.md après un correctif dans PREDATOR. Use right after fixing a bug worth remembering, or when the user says "consigne ça" / "ajoute l'incident".
---

# Consigner un incident

INCIDENTS.md est la MÉMOIRE des pannes : « ces textes sont chers, chacun a
été payé par une panne ». Une entrée s'écrit PENDANT que les détails sont
frais — run ids, horodatages, chiffres mesurés.

## Procédure

Délègue la rédaction au sub-agent **`incident-scribe`** (Edit limité à
INCIDENTS.md). Il applique le gabarit ci-dessous ; la session principale
garde deux responsabilités :

1. Si une **règle dure** naît du correctif : ajouter la ligne dans
   CLAUDE.md (une ligne, renvoi `— *Titre de section*` vers la nouvelle
   entrée — `tests/test_documentation.py` vérifie que le renvoi pointe une
   section réelle).
2. Si un **invariant testable** naît : la ligne invariant → gardien dans
   `AUDIT.md` §2.

## Gabarit d'une entrée (le format des entrées existantes)

```markdown
### <Le mécanisme qui était faux, pas « bug fixé »> (AAAA-MM-JJ)

<Symptôme observable — ce que l'opérateur voyait.>
<Cause mécanique — l'organe précis, fichier/fonction nommés.>
MESURÉ le AAAA-MM-JJ : <les chiffres/run ids qui l'ont prouvé.>
<Ce qui a été fait — et ce qui n'a PAS été fait, avec la raison.>
⚠️ <Piège à ne pas défaire.> / ⛔ <Interdit qui en découle.>
Gardien : `tests/test_xxx.py::TestYyy`.
```

Placer l'entrée dans la SECTION thématique du sommaire (moteur / sources /
règlement / IA / CI / dashboard / transverse), jamais en vrac à la fin.
Règle de tenue héritée d'AUDIT.md : on n'écrit que ce qui a été MESURÉ ;
une hypothèse non vérifiée est marquée comme telle.
