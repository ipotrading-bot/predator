"""
tests/test_logging_jobs.py — un job qui ne peut pas dire pourquoi il a échoué.

POURQUOI (2026-09-09)
---------------------
Tous les modules de `core/` loggent sous « PREDATOR.* ». Seul `run_engine.py`
configurait le logger « PREDATOR » — et ni `run_audit.py` ni
`run_closing_line.py` ne l'importent. Dans ces deux jobs, tout `core/` était
donc MUET : niveau effectif WARNING, root sans handler, messages jetés avant
même d'atteindre un formateur.

Ce qui disparaissait n'était pas du bavardage : `CLOSE SKIP |` (match sans
cote d'exchange, sélection non résolue, pas de prix de nul au football) et
`LINEMOVE |` (la ligne pariée a bougé) sont les SEULES lignes qui expliquent
une capture de clôture manquée. Le job rendait « capture done: 0 » sans
pouvoir dire pourquoi — la panne « vert mais vide » que ce dépôt a déjà payée
deux fois (INCIDENTS.md : « Un audit stérile ALERTE », « Closing line :
capture_from_scan est morte avec OddsAPI »).

Chaque test tourne dans un PROCESSUS NEUF : la configuration d'un logger est
un état global de processus, et pytest comme les autres tests en installent.
Vérifier dans le processus courant ne prouverait rien sur le vrai job.
Aucun réseau, aucune credential — seuls des imports.
"""
import subprocess
import sys

WORKDIR = "/workspaces/predator"


def _dans_un_processus_neuf(code: str) -> str:
    """Exécute `code` avec le dépôt sur le PYTHONPATH et rend sa sortie.

    stdout ET stderr : un `logging.StreamHandler()` sans argument écrit sur
    STDERR, alors qu'un `print` va sur stdout. Ne lire que stdout faisait
    passer ces tests pour rouges alors que le correctif marchait.
    """
    res = subprocess.run([sys.executable, "-c", code], cwd=WORKDIR,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"le script a échoué : {res.stderr[-800:]}"
    return res.stdout + res.stderr


class TestLeJobDeClotureParle:
    def test_les_lignes_de_diagnostic_sortent_vraiment(self):
        """Le test qui aurait attrapé la panne : on logge sous le logger
        RÉEL de core/closing_line.py et on exige de le VOIR."""
        sortie = _dans_un_processus_neuf(
            "import core.audit_engine\n"          # ce que fait run_closing_line.py
            "import core.closing_line as cl\n"
            "cl.log.info('CLOSE SKIP | motif de test')\n"
        )
        assert "CLOSE SKIP | motif de test" in sortie, (
            "les CLOSE SKIP / LINEMOVE du job de clôture sont muets : "
            "le logger « PREDATOR » n'a pas de handler dans ce processus")

    def test_le_niveau_laisse_passer_info(self):
        sortie = _dans_un_processus_neuf(
            "import logging, core.audit_engine\n"
            "print(logging.getLogger('PREDATOR').getEffectiveLevel())\n"
        )
        assert int(sortie.strip()) <= 20, "INFO (20) doit passer"

    def test_le_logger_de_laudit_parle_toujours(self):
        """Le correctif ne devait rien retirer à « AUDIT »."""
        sortie = _dans_un_processus_neuf(
            "import core.audit_engine as ae\n"
            "ae.log.info('AUDIT vivant')\n"
        )
        assert "AUDIT vivant" in sortie


class TestPasDeDoubleLigne:
    def test_le_scan_ne_logge_pas_deux_fois(self):
        """`run_engine.py` configure déjà « PREDATOR ». Si `core.audit_engine`
        est importé ensuite dans le même processus, il ne doit PAS ajouter un
        second handler — sinon chaque ligne du scan sortirait en double. C'est
        ce que garde le `if not _predator.handlers`."""
        sortie = _dans_un_processus_neuf(
            "import logging, time\n"
            "_h = logging.StreamHandler()\n"
            "p = logging.getLogger('PREDATOR')\n"
            "p.setLevel(logging.INFO); p.addHandler(_h); p.propagate = False\n"
            "import core.audit_engine\n"          # ne doit rien ajouter
            "print('HANDLERS', len(p.handlers))\n"
            "p.info('une seule fois')\n"
        )
        assert "HANDLERS 1" in sortie, "un second handler a été ajouté"
        assert sortie.count("une seule fois") == 1, "la ligne sort en double"
