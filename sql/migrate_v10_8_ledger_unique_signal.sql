-- ============================================================================
-- migrate_v10_8_ledger_unique_signal.sql — PHASE B3 (2026-08-27)
--
-- UNE SEULE LIGNE DE LEDGER PAR SIGNAL, garantie par la BASE.
--
-- POURQUOI
-- --------
-- `core.db.log_to_ledger` fait un INSERT sec. Rien n'empêche d'écrire deux
-- fois le même résultat : un audit relancé, un règlement rejoué après un
-- timeout, deux workflows qui se croisent. Le doublon ne lève aucune erreur et
-- ne se voit nulle part — il gonfle simplement le `n` de
-- `core.learning_layer`, qui croit alors avoir mesuré deux paris là où il n'y
-- en avait qu'un. Un échantillon dupliqué resserre les intervalles de Wilson
-- SANS apporter d'information : c'est la façon la plus discrète de se
-- convaincre qu'on a prouvé quelque chose.
--
-- ⚠️ LA CLÉ EST `signal_id`, ET SÛREMENT PAS (match, market_type, selection)
-- ---------------------------------------------------------------------------
-- La demande d'origine visait `(match_id, market_key, selection_name)` — ce
-- sont les colonnes de `signals`, pas celles de cette table-ci, qui n'a ni
-- `match_id`, ni `market_key`, ni `selection_name`. Leurs équivalents sont
-- `match` (le libellé « A vs B »), `market_type` et `selection`.
--
-- Or ce triplet porte DÉJÀ des doublons parfaitement légitimes. Mesuré en base
-- le 2026-08-27, sur 315 lignes :
--     ('Uros Medic vs Daniel Rodriguez', 'h2h', 'Uros Medic')  → 02/08 et 04/08
--     ('Cavalier FC vs Salcedo FC',      'h2h', 'Cavalier FC') → 21/08 et 22/08
-- Deux paris DISTINCTS, à deux dates différentes, sur la même affiche. Une
-- contrainte sur ce triplet aurait refusé la seconde ligne — c'est-à-dire
-- DÉTRUIT un résultat réglé, précisément l'échantillon que tout le projet
-- cherche à constituer. Et le problème est structurel, pas anecdotique : deux
-- équipes se rencontrent deux fois par saison.
--
-- `signal_id` n'a aucun de ces défauts. Mesuré le même jour : renseigné sur
-- 315 lignes sur 315, ZÉRO doublon. Un signal = un pari = une ligne. La clé
-- est d'autant plus solide depuis B1, qui a supprimé le DELETE+INSERT donnant
-- un `id` neuf au signal à chaque règlement.
--
-- L'index est PARTIEL (`WHERE signal_id IS NOT NULL`) : une ligne sans
-- identifiant de signal n'est pas contrainte. Il n'y en a aucune aujourd'hui ;
-- la clause évite qu'un futur appelant sans `id` ne fasse échouer l'insertion
-- au lieu de simplement ne pas être protégé.
--
-- À APPLIQUER À LA MAIN dans le SQL Editor Supabase. Idempotent.
-- ============================================================================

-- ── 1. Vérifier d'abord ────────────────────────────────────────────────────
-- Si cette requête rend des lignes, l'étape 3 échouera. Les REGARDER avant de
-- dédoublonner : un doublon peut signaler un règlement rejoué, donc un bug en
-- amont que supprimer les lignes masquerait.
SELECT signal_id, count(*) AS n,
       array_agg(id ORDER BY created_at)      AS ids,
       array_agg(outcome ORDER BY created_at) AS outcomes
FROM public.ai_learning_ledger
WHERE signal_id IS NOT NULL
GROUP BY signal_id
HAVING count(*) > 1;

-- ── 2. Dédoublonnage — À N'EXÉCUTER QUE SI L'ÉTAPE 1 A RENDU DES LIGNES ────
-- On garde la ligne DÉCISIVE quand il y en a une (WIN/LOSS/PUSH), sinon la
-- plus ancienne. Garder aveuglément la plus récente pourrait remplacer un
-- WIN réel par un 'expired' écrit ensuite par un autre chemin.
--
-- DELETE FROM public.ai_learning_ledger l
-- USING (
--   SELECT id,
--          row_number() OVER (
--            PARTITION BY signal_id
--            ORDER BY (outcome IN ('WIN','LOSS','PUSH')) DESC, created_at
--          ) AS rang
--   FROM public.ai_learning_ledger
--   WHERE signal_id IS NOT NULL
-- ) d
-- WHERE l.id = d.id AND d.rang > 1;

-- ── 3. La contrainte ───────────────────────────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS ledger_signal_id_uniq
  ON public.ai_learning_ledger (signal_id)
  WHERE signal_id IS NOT NULL;

-- ── 4. Vérification après application ──────────────────────────────────────
--   SELECT indexname FROM pg_indexes
--   WHERE tablename = 'ai_learning_ledger' AND indexname = 'ledger_signal_id_uniq';
--
-- Puis, après le prochain audit, le compte de lignes doit progresser
-- normalement — l'idempotence ne doit RIEN empêcher d'écrire :
--   SELECT count(*), count(DISTINCT signal_id) FROM public.ai_learning_ledger;
--
-- ── Retour arrière ─────────────────────────────────────────────────────────
--   DROP INDEX IF EXISTS public.ledger_signal_id_uniq;
-- Le code fonctionne sans l'index : `log_to_ledger` retombe sur l'INSERT sec,
-- sans collision à intercepter. Il perd la garantie, pas la capacité d'écrire.
