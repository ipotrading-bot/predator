"""Gardiens de la page /performance (templates/performance.html) — revue
opérateur du 2026-09-09 sur téléphone.

- Les tableaux ne défilent plus de côté : predator.css pose
  `table { min-width: 760px }` pour le ledger et la page en héritait, ce qui
  rejetait Unités et G–P hors de l'écran (« PARIS 14–12 » lu sans sa ligne).
- Réussite en première colonne, réussite décroissante pour PAR SPORT,
  résultat en première colonne de l'historique.
- Les fantômes (golden hour) restent HORS des chiffres et hors de l'écran :
  seulement en infobulle `title=`.
- Règle n°7 toujours rendue : Wilson et point mort sur chaque taux.
"""
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
PERF = (RACINE / "templates" / "performance.html").read_text(encoding="utf-8")


def _sans_commentaires_jinja(src: str) -> str:
    return re.sub(r"\{#.*?#\}", "", src, flags=re.S)


def test_les_tableaux_ne_heritent_plus_de_la_largeur_minimale_du_ledger():
    assert re.search(r"\.perf-table\s*\{[^}]*min-width:\s*0", PERF), (
        "sans `min-width: 0`, la règle globale `table { min-width: 760px }` "
        "de predator.css fait défiler /performance de côté sur téléphone")
    assert "min-width: 560px" not in PERF and 'class="perf-table wide"' not in PERF


def test_reussite_puis_resultat_en_premiere_colonne():
    m = re.search(r"\{% macro th_row\(titre\) %\}(.*?)\{% endmacro %\}", PERF, re.S)
    assert m, "la macro d'en-tête commune a disparu"
    ths = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", m.group(1))
    assert ths[0] == "Réussite" and ths[1] == "{{ titre }}", ths
    assert "G&ndash;P" in ths[-1], "gagnés–perdus doit être libellé G–P, pas « Paris »"
    assert "global_s.by_sport|sort(attribute='win_rate', reverse=true)" in PERF
    hist = PERF[PERF.index("&#9632; HISTORIQUE"):]
    premier_th = re.search(r"<th(?:\s[^>]*)?>(.*?)</th>", hist).group(1)
    assert premier_th == "Résultat", premier_th


def test_les_fantomes_sont_hors_ecran_mais_toujours_separes():
    corps = _sans_commentaires_jinja(PERF)
    # Plus de tuile ni de pied de carte « fantômes » : chaque mention restante
    # est dans un attribut title="…".
    for m in re.finditer(r"fantômes", corps):
        avant = corps[:m.start()]
        assert avant.rfind('title="') > avant.rfind('">'), (
            "une mention des fantômes est rendue à l'écran : " + corps[m.start()-60:m.start()+20])
    # …et la séparation recommandés / fantômes reste rendue (gardien historique).
    assert "global_s.phantoms" in PERF and "m.phantoms" in PERF


def test_chaque_taux_porte_wilson_et_point_mort():
    m = re.search(r"\{% macro cell_rate\(x\) %\}(.*?)\{% endmacro %\}", PERF, re.S)
    assert m and "win_rate_lo" in m.group(1) and "p_breakeven" in m.group(1)
    assert PERF.count("cell_rate(") >= 3      # définition + sport + macro tableau
