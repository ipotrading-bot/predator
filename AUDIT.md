# AUDIT — PREDATOR

> **Document de référence.** Il enregistre ce qui a été vérifié, ce qui a été
> corrigé, et surtout **quel test garde quel invariant**. Quand un doute
> revient sur l'équilibre du système, c'est ici qu'on regarde avant de
> rouvrir le code.
>
> Règle de tenue : on n'écrit ici que ce qui a été **mesuré**. Une hypothèse
> non vérifiée est marquée comme telle. Un document d'audit qui affirme sans
> preuve fait exactement le dégât qu'il prétend éviter.

Dernière passe : **2026-08-26** (refonte CI + closing line d'exchange). État à
la clôture : **977 tests, 0 échec**, pyflakes propre, les pages du dashboard
rendent (smoke test local). Le compte de tests a BAISSÉ depuis le 2026-08-22
(1055 → 977) : la vérification par regex du câblage des secrets, paramétrée sur
13 workflows, a été remplacée par des tests sur la fonction qui calcule ce
câblage. Moins de cas, même invariant, gardé plus près de sa source.

---

## 1. La classe de bug qui domine ce dépôt

Trois des cinq défauts sérieux trouvés le 2026-08-22 sont **la même panne** :
une liste tenue à la main qui a divergé de sa source, sans qu'aucune erreur
ne soit levée.

| Liste | A divergé de | Conséquence mesurée |
|---|---|---|
| Clés IA dans les 7 workflows | `PRODUCTION_SAFE` | OVH et SiliconFlow inatteignables : 2 fournisseurs sur 9 morts en production. **Récidive constatée le 2026-08-26** : 18 fournisseurs au registre, 15 câblés — CEREBRAS/CHUTES/SAMBANOVA/ZHIPU absents de tout workflow. Supprimé à la racine : les workflows ne listent plus aucun secret (`scripts/ci_env.py`, pools DÉRIVÉS du registre). |
| `_AI_SECRETS` dans `scripts/ops.py` | `REGISTRY` | `secrets-push` sautait `OVH_AI_API_KEY` en silence |
| Tables sport→emoji dans `index.html` | `api/index.py` | 3 sports actifs affichés « 🎯 rugbyleague » |
| 6 pieds de page + `/api/health` | (aucune source) | 6 numéros de version différents sur 6 onglets |

**Pourquoi c'est coûteux ici en particulier.** `core/ai_router.py` ignore
silencieusement un fournisseur sans clé — et c'est sa propriété *désirable* :
un palier gratuit qui ferme ne doit pas casser un run. La contrepartie, c'est
qu'une capacité peut rester morte des mois sans un log, sans un test rouge.
La suite de tests ne pouvait rien voir : **le code était correct**, c'est le
câblage qui manquait.

**La parade retenue** — ne plus jamais tenir ces listes à la main. Soit elles
sont dérivées de leur source (`_AI_SECRETS`, tables sport injectées dans les
templates), soit un test compare la copie à la source à chaque exécution.

---

## 2. Invariants et leur gardien

C'est le tableau à consulter avant de toucher à quoi que ce soit.

| Invariant | Gardien |
|---|---|
| Tout fournisseur `PRODUCTION_SAFE` atteint les pools `scan`/`closing`/`settlement` | `tests/test_ci_env.py::test_tout_fournisseur_de_production_atteint_le_pool_scan` |
| Chaque bloc de secrets d'un workflow est conforme à son pool | `…::test_chaque_bloc_genere_est_conforme_a_son_pool` |
| Aucun secret nommé hors d'un bloc généré | `…::test_aucun_secret_nommé_hors_dun_bloc_genere` |
| Aucun workflow ne fabrique un dump de secrets (GitHub le refuserait) | `…::test_aucun_workflow_ne_fabrique_un_dump_de_secrets` |
| Aucun `if:` de job n'utilise le contexte `inputs` nu | `…::test_aucun_workflow_nutilise_le_contexte_inputs_nu` |
| La liste des clés IA est DÉRIVÉE du registre, comme celle d'`ops.py` | `…::test_liste_ia_derivee_du_registre_comme_ops_py` |
| `CLOUDFLARE_API_TOKEN` ne va jamais sans `CLOUDFLARE_ACCOUNT_ID` | `…::test_cloudflare_a_son_identifiant_de_compte` |
| Aucun pool ne transmet une clé Groq ou Tavily (supprimées le 2026-09-02) | `…::test_aucun_pool_ne_transmet_groq_ou_tavily` |
| Le step REPRICE ne reçoit aucune clé payante (lisible dans le YAML) | `…::test_le_step_reprice_ne_peut_mecaniquement_rien_depenser` |
| `readonly` ne détient aucun jeton d'écriture | `…::test_readonly_ne_detient_aucun_jeton_decriture` |
| Le pool `settlement` porte les clés de résultats (api-sports, TheSportsDB) | `…::test_le_settlement_porte_les_cles_de_resultats` |
| Le score vient d'api-sports puis de sources structurées — JAMAIS d'un LLM | `tests/test_settlement.py::TestFetchMatchResult::test_no_ai_layer_involved` |
| Périmètre d'émission : un match du Tier 2 n'entre que si un exchange confirme son prix sharp (marché vivant) ET si api-sports/MLB/ESPN peut le régler (ESPN liste le match) ; chaque refus est loggé, ESPN muet = refus | `tests/test_perimetre.py` |
| Le calculateur `/system` affiche des retours CONDITIONNELS (si k bons sur N : pire cas, meilleur cas, point mort, marge composée indicative), jamais des probabilités ni un taux de réussite ; le bloc MATH reste exactement ses cinq fonctions | `tests/test_system_page.py::TestScenariosDerivesDuMoteur` |
| ESPN (ouvert, sans clé) règle AVANT TheSportsDB, avec le même contrat (deux noms, candidat unique, terminé) ; api-sports n'est pas appelé hors de la fenêtre de son plan gratuit | `tests/test_score_sources.py::TestESPN`, `::TestChaineAvecESPN`, `tests/test_settlement.py::…::test_api_sports_saute_hors_de_la_fenetre_du_plan_gratuit` |
| Un score en direct (statut non terminé) ne règle jamais | `tests/test_score_sources.py::TestTheSportsDB::test_un_score_en_direct_ne_regle_pas` |
| La recherche d'équipe TheSportsDB ne décide jamais seule (cas Pastoreo) | `…::test_une_mauvaise_equipe_du_flou_ne_regle_rien` |
| Deux matchs candidats → refus, jamais un WIN/LOSS deviné | `…::test_deux_candidats_font_REFUSER_pas_deviner` |
| Un audit qui ne règle rien alerte et repousse la purge | `…::TestAuditSterile`, `…::TestPurgeNeDetruitPasLechantillon` |
| `expired` n'est plus terminal : chaque audit relance la recherche web | `tests/test_relance_expires.py` |
| La relance passe APRÈS le settlement frais (réserve IA en négatif) | `…::TestLaPlaceDansLaudit::test_la_relance_passe_apres_le_settlement_frais` |
| Un audit à vide relance quand même les expirés | `…::test_un_audit_a_vide_relance_quand_meme` |
| Sans score sûr, la ligne RESTE expirée — jamais un WIN/LOSS deviné | `…::TestElleNeDevinePas` |
| Un match réel = UNE ligne de ledger : le jumeau inter-sources est refusé ou promu, jamais inséré (stock nettoyé par `sql/migrate_v10_10`) | `tests/test_ledger_jumeaux.py` |
| Le Tier 2 tourne à CHAQUE tick — jamais conditionné au succès du Tier 1 (seul REPRICE exempte) ; les gates décisifs (LOWPROB) et la purge loggent ce qu'ils jettent | `tests/test_tier2_toujours.py` |
| Toute table `_archive` créée dans `sql/` est refermée (RLS + REVOKE anon) — liste DÉRIVÉE des CREATE TABLE, jamais tenue à la main | `tests/test_migrations_rls.py` |
| Un signal FANTÔME (`is_shadow`, posé AVANT la persistance, figé au rafraîchissement) n'apparaît ni sur `/`, ni sur `/api/signals`, ni sur `/audit`, ni dans le digest Telegram ; la règle T-2h vaut par signal, borne importée de `learning_layer` | `tests/test_signaux_fantomes.py` |
| Le digest Telegram liste les recommandés encore jouables (🆕 nés depuis `REPORT_WINDOW_H`, aligné sur le cron de `reports.yml` par test, ⏳ rappels), se tait sans pari et moteur vivant ; le scan standard compte ses écartés au lieu de se taire ; aucun libellé de session | `tests/test_rapport_digest.py`, `tests/test_telegram_format.py` |
| Un crédit OddsAPI n'achète jamais un fantôme : le pré-vol compte les matchs JOUABLES (T-2h + marge, borne importée de `learning_layer`) et saute une ligue qui n'en a plus ; chaque fenêtre favorable contient au moins un cron standard | `tests/test_odds_api_preflight.py`, `tests/test_scan_windows.py` |
| api-sports : ≤ 9 requêtes/min, et un compte « suspended » n'est plus interrogé de la journée (compartiment partagé), tous sports et settlement compris | `tests/test_api_sports.py` |
| Telegram annonce CHAQUE pari recommandé en simple, une fois (dédup 24 h par pari) — aucun combiné ne conditionne l'annonce | `tests/test_telegram_format.py::TestEmptyAndSingles`, `tests/test_reprice_mode.py` |
| Le curseur tourne : toutes les lignes sont couvertes, pas seulement les 12 premières | `…::TestLeBudgetEtLeCurseur` |
| Une panne de relance ne fait pas échouer un audit qui a réglé | `…::test_une_panne_de_relance_ne_fait_pas_echouer_laudit` |
| Le pool `scan` porte les relais des sources filtrées par IP | `…::test_le_pool_scan_porte_les_relais_des_sources_filtrees_par_ip` |
| Le préflight refuse une `SUPABASE_SERVICE_KEY` qui n'est pas `service_role` | `…::test_preflight_refuse_une_cle_qui_nest_pas_service_role` |
| `ODDS_API_KEY` n'est PLUS requise (la garde échouait fermé) | `…::test_preflight_odds_api_key_nest_plus_requise` |
| Le Tier 1 est rallumé par `MODE_ENV` (standard seulement), jamais par défaut ni en reprice | `…::test_le_tier_1_est_rallume_par_le_workflow_pas_par_le_module` |
| Rythme mensuel OddsAPI : allocation = pool ÷ jours restants, plafond intra-journée, parts par priorité, ligues les plus peuplées d'abord | `tests/test_scan_windows.py::TestRythme`, `…::test_fetch_odds_sert_les_ligues_les_plus_peuplees_d_abord`, `…::test_build_spend_policy_porte_le_rythme` |
| Un Tier 1 allumé mais vide descend quand même au Tier 2 | `tests/test_oddsapi_obsolete.py::…::test_tier_1_allume_mais_vide_descend_au_tier_2` |
| Il n'y a que DEUX modes de scan (`standard`, `reprice`), exactement les crons de `scan.yml`, les options du dispatch et les budgets `SCAN_TIMEOUTS` ; `REPRICE=1` vient de `MODE_ENV`, jamais du YAML ; le chien de garde rattrape `scan.yml` en `reprice` ; la règle fantôme T-2h vaut par SIGNAL, aucune par mode | `tests/test_ci_env.py::test_il_ny_a_que_deux_modes`, `…::test_le_dispatch_de_scan_yml_noffre_que_les_modes_connus`, `…::test_reprice_vient_de_mode_env_pas_du_yaml`, `tests/test_timeout_par_mode.py`, `tests/test_watchdog_worker.py`, `tests/test_signaux_fantomes.py::TestRaisonDuFantome::test_aucune_raison_par_mode` |
| Chaque cron de `scan.yml` a sa ligne dans `CRON_MODES` | `…::test_la_table_cron_mode_est_exactement_les_crons_de_scan_yml` |
| `closing_line.yml` ne tire jamais plus vite que `CLOSING_LINE_REFRESH_MIN` | `…::test_closing_line_cadence_alignee_sur_refresh_min` |
| `ops.py::_AI_SECRETS` couvre tout le registre | `…::test_secrets_push_couvre_tout_le_registre` |
| `secrets-push` n'emporte **pas** les clés d'opérateur vers les runners | `…::test_secrets_push_nemporte_pas_les_cles_operateur` |
| `.env.example` documente tout credential réellement lu | `…::test_env_example_documente_les_credentials_reellement_lus` |
| Tout fournisseur du registre est documenté dans `.env.example` | `…::test_tout_fournisseur_du_registre_est_documente…` |
| Chaque job GitHub a une borne de durée | `…::test_chaque_job_a_une_borne_de_duree` |
| Les runners déclarent Python une seule fois, dans l'action composite | `…::test_la_version_de_python_des_runners_est_declaree_une_seule_fois` |
| Tout sport actif a emoji + libellé + libellé court + ordre | `tests/test_dashboard_sports.py::test_tout_sport_actif_est_couvert` |
| Les sports retirés gardent leur emoji (lignes historiques) | `…::test_les_sports_retires_gardent_leur_emoji` |
| `index.html` ne redéfinit pas les tables sport en dur | `…::TestPasDeTableDupliquee` |
| Tout sport scanné a un seuil d'edge par défaut | `…::test_les_seuils_appris_couvrent_le_meme_perimetre` |
| `/api/audit/run` échoue **fermé** sans jeton | `tests/test_api_admin_auth.py::test_sans_jeton_configure_la_route_refuse` |
| Le jeton ne se devine pas préfixe par préfixe | `…::test_bon_prefixe_mais_jeton_tronque_refuse` |
| Les refus sont indiscernables entre eux | `…::test_la_reponse_ne_dit_pas_pourquoi` |
| Aucune exception brute ne part dans une réponse HTTP | `…::TestPasDeFuiteDansLesReponses` |
| `/api/health` répond base injoignable et ne publie aucun secret | `…::TestSondeDeSante` |
| Aucun mois antérieur à l'époque (août 2026) ne remonte sur /performance | `tests/test_mission2_dashboard_quota.py::test_une_fenetre_elargie_ne_rouvre_pas_juillet` |
| La fenêtre reste glissante AU-DESSUS de l'époque (pas figée) | `…::test_la_fenetre_reste_glissante_au_dessus_de_lepoque` |
| L'archivage ne touche jamais un signal `active` | `…::test_le_script_darchivage_de_juillet_existe_et_narchive_pas_a_laveugle` |
| Tout sport-type servi a Kelly/seuil/quotas/emoji/labels — y compris `tennis`, invisible au test statique | `tests/test_new_sports_phase3.py::TestInvariantDesQuatreFichiers` |
| Les clés tennis sont dynamiques (jamais statiques dans `SPORT_KEYS`) et injectées dans `fetch_odds` | `tests/test_tennis_discovery.py::TestInjectionDansFetchOdds` |
| Un seul `GET /sports` par scan (la découverte réutilise la sonde) | `…::test_un_seul_get_sports_par_scan` + `tests/test_odds_api_keypool.py` |
| NCAAF a son sport-type dédié, Kelly < NFL, contexte de settlement « NCAA » | `tests/test_new_sports_phase3.py::TestNCAAF` |
| `.python-version` reste sur la version de **Vercel** (3.12), jamais « alignée » sur les workflows | `tests/test_workflow_secrets.py::test_python_version_appartient_a_vercel` |
| `vercel.json` et `.python-version` annoncent la même version | `…::test_vercel_json_annonce_la_meme_version_que_python_version` |
| Les workflows sont d'accord entre eux sur Python 3.11 | `…::test_les_workflows_partagent_une_seule_version_de_python` |
| Vercel ne déploie pas AUSSI depuis Git (sinon le `needs: test` est décoratif) | `…::test_vercel_ne_deploie_pas_aussi_depuis_git` |
| `deploy` est le seul job à porter un `environment:` | `…::test_le_deploiement_est_le_seul_job_a_porter_un_environnement` |
| La closing line d'exchange lit le prix de l'EXCHANGE, jamais le prix d'entrée | `tests/test_closing_line_exchange.py::test_le_prix_capture_est_celui_de_lexchange_pas_le_pinnacle_dentree` |
| Football sans prix de nul : refus, jamais de repli sur le moneyline | `…::test_football_sans_prix_de_nul_est_refuse` |
| Le prix soft d'entrée est EXÉCUTABLE, jamais dévigorisé | `tests/test_prix_executable.py::TestToBinaryRendLePrixExecutable` |
| Le prix exécutable est toujours SOUS le prix dévigorisé | `…::test_le_prix_executable_est_toujours_sous_le_prix_devigorise` |
| Football sans nul : refus, jamais de repli sur le moneyline | `…::test_football_sans_nul_est_refuse_jamais_rabattu_sur_le_moneyline` |
| Le bloc soft retenu appartient à UN book réel (pas de max par issue) | `…::TestLineShoppingSurLePrixFinal` |
| Le line shopping se départage sur le prix FINAL, pas sur la cote nue | `…::test_cest_le_prix_executable_qui_departage_pas_la_cote_nue` |
| Le moteur nomme le prix `executable_odd` ; la colonne reste `xbet_odd` | `…::TestSignalEnMemoire` |
| Un DNB synthétique annonce la répartition de ses DEUX jambes | `…::TestAdviceAnnonceLesDeuxJambes` |
| Le last-look reprixe le CÔTÉ MISÉ au prix exécutable, pas la cote 1X2 brute | `tests/test_last_look_reprice.py` |
| Le point mort équilibre le GAIN NET et la mise perdue, pas le payout brut | `tests/test_stats_utils.py::TestPBreakeven` |
| Sans taxe, le point mort vaut exactement 1/cote | `…::test_sans_taxe_le_point_mort_est_la_probabilite_implicite` |
| Le ROI pondéré Kelly est NET de taxe, et calculé en un seul endroit | `tests/test_taxe_reelle.py::TestFormuleUnique` |
| `learning_layer` dérive son point mort de `constants.TAX_RATE` | `tests/test_learning_layer.py::TestBreakevenUsesOperatorTaxRate` |
| La courbe d'équité du disjoncteur est nette de taxe | `tests/test_taxe_reelle.py::TestDrawdownFiscalise` |
| `tax_engine.DEFAULT_TAX_RATE` est DÉRIVÉ de `constants.TAX_RATE` | `tests/test_taxe_reelle.py::TestUnSeulTaux` |
| L'oracle LLM n'existe plus (supprimé le 2026-09-02 avec Groq/Tavily) | `tests/test_closing_line.py::TestCaptureClosingLines::test_loracle_web_nexiste_plus` |
| L'exchange est confronté à Pinnacle même quand celui-ci existe | `tests/test_contre_expertise_exchange.py::TestLaContreExpertiseARemplaceLeContinue` |
| Deux avis sharp trop divergents REFUSENT le match entier | `…::TestLeRefusPorteSurLeMatchEntier` |
| La divergence sharp se mesure en POINTS de probabilité | `…::TestLaDivergenceSeMesureEnPointsDeProbabilite` |
| Un carnet illisible ne vaut pas un carnet en désaccord | `…::test_un_carnet_illisible_ne_juge_pas` |
| L'exchange entre au consensus sans jamais écraser Pinnacle | `…::test_pinnacle_reste_la_reference_et_nest_jamais_ecrase` |
| L'exchange est exclu du juge de divergence CV, pas du vote | `…::test_lexchange_est_exclu_du_juge_pas_du_vote` |
| Le garde VOLATILE mord toujours entre bookmakers | `…::test_le_garde_volatile_mord_toujours_entre_bookmakers` |
| Aucune ligne de `signals` n'est supprimée pour être réécrite | `tests/test_db.py::TestLeDeleteInsertADisparu` |
| `core.db` n'expose plus de remplacement de ligne (vérifié sur l'AST) | `…::test_aucun_module_ne_reference_encore_le_remplacement_de_ligne` |
| Un échec d'écriture ne détruit plus la ligne | `…::test_un_echec_ne_detruit_plus_rien` |
| La capture de closing line ne supprime jamais de ligne | `tests/test_closing_line.py::test_le_remplacement_de_ligne_nexiste_plus_dans_audit_engine` |
| Un seul signal ACTIF par (match_id, market_key) — garanti par la base | `sql/migrate_v10_7_signals_unique_active.sql` + `tests/test_save_preserving.py::TestB2LaBaseArbitre` |
| `_save` ne LIT plus avant d'écrire quand la clé est connue | `…::test_aucun_select_prealable_quand_la_cle_est_connue` |
| Une ligne réglée entre-temps ne fait pas perdre le signal | `…::test_une_ligne_reglee_entre_temps_libere_la_place` |
| Deux matchs sans identifiant ne s'écrasent pas | `…::test_deux_matchs_sans_identifiant_ne_secrasent_pas` |
| Une violation d'unicité n'est pas prise pour une panne d'écriture | `…::TestB2ReconnaissanceDeLaViolation` |
| Une seule ligne de ledger par signal | `sql/migrate_v10_8_ledger_unique_signal.sql` + `tests/test_db.py::TestLedgerIdempotent` |
| Un rejeu de règlement n'alerte pas et ne duplique pas | `…::test_un_second_enregistrement_ne_duplique_pas_ni_nalerte` |
| Un résultat réel remplace une absence de résultat, jamais l'inverse | `…::test_un_resultat_reel_remplace_une_absence_de_resultat` |
| Deux signaux sur la même affiche coexistent au ledger | `…::test_deux_signaux_distincts_sur_la_meme_affiche_coexistent` |
| La reconnaissance d'une violation d'unicité est DÉRIVÉE, pas recopiée | `tests/test_save_preserving.py::…::test_la_reconnaissance_est_derivee_pas_recopiee` |
| Le disjoncteur filtre WIN/LOSS CÔTÉ SQL (sinon fenêtre réelle = 1/20) | `tests/test_risk_manager.py::TestB4LaFenetreEstReelle` |
| Le filtre part dans la requête, pas après coup | `…::test_le_filtre_est_demande_a_la_base_pas_applique_apres_coup` |
| Un gain sans cote est ÉCARTÉ, jamais valorisé à 2.0 | `…::TestB4UneCoteManquanteNestPasInventee` |
| Une perte sans cote est CONSERVÉE (l'écarter embellirait le portefeuille) | `…::test_une_perte_sans_cote_est_CONSERVEE` |
| Un run stérile sort en ÉCHEC, il n'affiche plus une coche verte | `core/run_contract.py` + `tests/test_contrat_de_fin.py` |
| Zéro signal reste VERT — c'est le résultat attendu depuis A1 | `…::TestZeroNestPasUnEchec` |
| Les trois conditions sont des CONJONCTIONS, jamais des seuils | `…::TestLesTroisContradictions` |
| Le contrat est réellement appelé aux trois sorties | `…::TestLeContratEstReellementCable` |
| Un audit à 0 réglé sur N éligibles sort rouge | `…::TestLauditSterileSortEnEchec` |
| Un scan qui n'a rien persisté sort rouge | `…::TestLeScanQuiNePersisteRienSortEnEchec` |
| /performance affiche le taux de résolution (biais de survie) | `tests/test_mission2_dashboard_quota.py::TestTauxDeResolution` |
| Un PUSH compte comme RÉSOLU, `active`/`closed` ne comptent nulle part | `…::test_un_push_compte_comme_resolu`, `…::test_active_et_closed_nentrent_nulle_part` |
| La formule du taux de résolution n'existe qu'une fois (AST) | `…::test_la_formule_nest_pas_recopiee_ailleurs` |
| Le jeton d'admin ne passe QUE par l'en-tête, jamais en query string | `tests/test_api_admin_auth.py::…::test_le_jeton_en_query_string_est_REFUSE` |
| Une query string ne sert pas de repli sur un en-tête erroné | `…::test_la_query_string_ne_sert_pas_de_repli_sur_en_tete_errone` |
| Le refus par query string est journalisé SANS recopier le jeton | `…::test_le_jeton_nest_jamais_recopie_dans_le_log` |
| `/api/scan` est limitée par IP, AVANT tout accès à la base | `tests/test_api_admin_auth.py::TestScanLimiteDeDebit` |
| L'IP vient de l'en-tête de la plateforme, jamais de `remote_addr` | `…::test_deux_IP_ont_des_compteurs_SEPARES` |
| La fenêtre GLISSE — la limite n'est pas un bannissement | `…::test_la_fenetre_glisse` |
| Le compteur par IP ne grandit pas indéfiniment | `…::test_le_compteur_ne_grandit_pas_indefiniment` |
| Le dashboard ne demande JAMAIS de clé d'écriture (vérifié sur l'AST) | `tests/test_api_admin_auth.py::TestLeDashboardNaPlusDeCleDEcriture` |
| `/api/scan` passe par `demander_scan()`, jamais par une écriture directe | `…::TestScanPasseParLaFonctionPostgres` |
| Une panne de la fonction ne fuit ni son nom ni la policy dans la réponse | `…::test_une_panne_de_la_fonction_ne_fuit_pas_dans_la_reponse` |
| `anon` ne peut plus écrire `meta` (RLS + GRANT retirés) | `sql/migrate_v10_9_scan_request_rpc.sql` §3 |
| Le déploiement Vercel ne porte que les 4 variables réellement lues | README §« Le dashboard n'a plus de clé d'écriture » |
| Aucun script du dashboard ne vient d'une URL flottante | `tests/test_dashboard_cdn.py::TestPlusAucuneURLFlottante` |
| Tout script DISTANT porte `integrity` ET `crossorigin` | `…::TestToutTiersEstVerifie` |
| Tailwind est servi par le dépôt, et son empreinte est vérifiée | `…::TestTailwindEstServiParLeDepot` |
| L'ordre de chargement des scripts reste correct | `…::TestLaPageResteChargeable` |
| Un step de PRÉPARATION ne voit que Supabase (pas `pip install`) | `tests/test_ci_env.py::TestAmorcageSupabaseSeul` |
| Le bloc d'amorçage est DÉRIVÉ du pool, jamais listé | `…::test_lamorcage_est_DERIVE_du_pool_jamais_liste` |
| Le préflight COMPLET tourne là où les clés sont présentes | `…::TestLePreflightCompletTourneOuSontLesCles` |
| Seuls `scan` et `settlement` ont des contrôles au-delà des fondations | `…::test_seuls_scan_et_settlement_ont_des_controles_au_dela_des_fondations` |
| `.claude/settings.json` et `.mcp.json` sont du JSON valide ; scripts de hooks existants et exécutables | `tests/test_claude_config.py::TestLesFichiersDeConfigSontValides`, `…::TestLesScriptsReferencesExistent` |
| Aucune mention de Wiz ni chemin machine (`/workspaces/`) sous `.claude/` | `…::TestAucuneMentionMorteNiCheminMachine` |
| Le serveur MCP Supabase est épinglé (pas de `@latest`) et `--read-only` | `…::TestLeServeurMcpEstEpingle` |
| Règle n°1 mécanique : `toJSON(secrets)` refusé à l'écriture d'un workflow | `…::TestGuardWorkflows` + `.claude/hooks/guard_workflows.sh` |
| Règle n°3 mécanique : nom de modèle IA refusé hors `ai_router.py`/tests | `…::TestGuardAiModels` + `.claude/hooks/guard_ai_models.sh` |
| Règle n°11 mécanique : `TAX_RATE`/`SHADOW_SPORTS` exigent une confirmation | `…::TestGuardOperatorDecisions` + `.claude/hooks/guard_operator_decisions.sh` |
| Règle n°9 mécanique : suppressions Supabase refusées via MCP, écritures confirmées | `…::TestGuardSupabaseWrites` + `.claude/hooks/guard_supabase_writes.sh` |
| Commandes destructrices refusées même enfouies dans une commande composée | `…::TestGuardBash` + `.claude/hooks/guard_bash.sh` |
| L'agent `migration-author` ne peut écrire QUE `sql/migrate_v*.sql` | `…::TestGuardMigrationWrites` + `.claude/hooks/guard_migration_writes.sh` |
| Un `.py` modifié est linté à l'édition ; la suite tourne avant tout arrêt de session | `…::TestLintOnEdit`, `…::TestVerifyBeforeStop` |

L'**invariant des sport-keys** (4 fichiers : `core/odds_api.py`,
`core/learning_layer.py`, `api/index.py`) est décrit
dans le skill `predator-pipeline` ; son maillon d'affichage est tenu par
`tests/test_dashboard_sports.py`. Vérifié propre sur les 4 fichiers le
2026-08-22.

---

## 3. Corrections du 2026-08-22 (avec la preuve)

### 3.1 `/api/audit/run` était ouverte à tout Internet — `28afaa4`

`POST /api/audit/run` déclenchait `audit.yml` : 45 min de runner, le
settlement, et la consommation de la réserve IA gardée en négatif exprès.
**Aucune authentification, aucun cooldown, aucune limite de débit**, sur une
URL Vercel publique. Aucune interface du dépôt ne l'appelle — elle n'était
connue que de la table du README. Une boucle `curl` anonyme épuisait le quota.

Le coût est documenté : incident du 10→20 août 2026, dix jours sans signal.

Corrigé par un jeton `DASHBOARD_ADMIN_TOKEN` en **échec fermé**. C'est le
point qui compte : la forme du bug d'origine était « pas de PAT → 503 », donc
« PAT présent → ouvert à tous ». Comparaison par `hmac.compare_digest`, refus
indiscernables.

> ⚠️ **Changement de comportement assumé** : la route ne répond plus tant que
> le jeton n'est pas posé dans les variables d'environnement Vercel.

### 3.2 Deux fournisseurs IA inatteignables + trois listes divergentes — `37f4de0`

Voir §1. En corrigeant, le test neuf a immédiatement trouvé un défaut que la
lecture n'avait pas vu : **`.python-version` annonçait 3.12** contre 3.11
partout ailleurs (interpréteur local 3.11.15, `CLAUDE.md`, `vercel.json`, les
14 workflows). Vercel lit ce fichier pour choisir son runtime : le dashboard
pouvait être servi par un interpréteur sur lequel rien n'est testé.

> ⚠️ **Cette conclusion était FAUSSE, et elle a cassé la production.** Voir
> §3.8. `.python-version` appartient à Vercel : son image de build n'embarque
> pas 3.11. Le 3.12 n'était pas une dérive, c'était la contrainte de la
> plateforme.

`.env.example` — dont tout le propos est « copiez-moi en `.env` » — omettait
10 credentials réellement lus : les 5 variables Betfair et tout le bloc de
`scripts/ops.py`, alors que `CLAUDE.md` présente `ops.py` comme *la* façon de
piloter Supabase et Vercel. La commande documentée était inutilisable après
une copie propre.

### 3.3 Dashboard : nav amputée, version qui ment, tables dupliquées — `ec2cacf`

- **`/ledger` et `/audit` n'étaient atteignables sur mobile par aucun lien** —
  `.nav-pages` est masquée sous 640 px et la barre du bas ne portait que 4
  entrées sur 6. Deux pages entières injoignables au doigt.

  > **Suite, le même jour (`0866820`, décision opérateur)** : le menu a été
  > volontairement ramené à quatre entrées — *Accueil · Sys · Wiz · Perf*,
  > devenues trois après la suppression de Wiz le 2026-08-26 —
  > et `/ledger` et `/audit` en ont été **retirés**. Les deux routes
  > fonctionnent toujours (200 en production) mais ne sont plus atteignables
  > par aucun lien, ni mobile ni desktop : il faut saisir l'URL.
  > Ce n'est plus le bug corrigé ci-dessus (une nav amputée par accident,
  > incohérente entre desktop et mobile) mais un choix de produit assumé et
  > uniforme sur les 6 pages. Consigné ici pour que personne ne le
  > « re-corrige » en croyant retrouver le défaut d'origine. Les six entrées
  ont d'abord été alignées sur les deux menus.

  > **Suite, le même jour — décision opérateur.** `/ledger` et `/audit` ont
  > ensuite été **volontairement masqués des DEUX menus**, qui portent
  > désormais trois entrées dans l'ordre **Accueil · Sys · Perf**
  > (Wiz retiré le 2026-08-26 avec sa page et son moteur).
  > Les deux pages restent servies et rendent normalement : elles ne sont
  > simplement plus liées, et s'atteignent par URL directe. Ce n'est donc
  > plus un défaut à « réparer » en les remettant — la skill
  > `predator-dashboard-check` porte la même consigne, pour qu'un futur
  > contrôle de parité ne les réintroduise pas de bonne foi.
- Six pieds de page, six versions (`v8.5`, `v8.6`, `v8.8`, `v9.4`, `v10.0`,
  `v1.0`) + `« 8.8 »` en dur dans `/api/health`. Désormais `DASHBOARD_VERSION`,
  une seule définition, injectée par un `context_processor`.
- `/api/signals` ne filtrait pas les matchs commencés alors que les trois
  autres consommateurs le faisaient. **Mesuré en base le 2026-08-22 : 37
  signaux actifs, dont 23 déjà commencés — 62 % de lignes non jouables.**
  Règle extraite dans `_is_playable()`, partagée ; `?all=1` pour le
  diagnostic.
- `Cache-Control` : `no-store` gardé sur les **pages** (un signal périmé
  affiché comme actif est un faux pari), 10 min sur `/static` qui était
  re-téléchargé à chaque navigation.

### 3.4 Une cote ronde était lue comme absente — `328c0ec`

`core/oracle.py` exigeait `\d+\.\d+`, point décimal obligatoire. Or un modèle
sérialise très souvent une cote ronde sans décimale (`"draw": 3`,
`"price": 2`). Le motif ne matchait pas, la fonction rendait `(None, None)`,
et **le prix sharp était perdu en silence** sur le chemin du settlement —
pour la seule raison que la cote tombait juste.

### 3.5 MMA/boxe cherchaient des « compositions » — `f0b620c`

Depuis la refonte du périmètre, MMA et boxe sont sur flux OddsAPI réel, donc
émis, donc analysés par Wiz. `_SPORT_QUERY_A` n'avait pas d'entrée pour eux :
requête générique « team news lineup injuries ». Dans un sport de combat,
« composition » et « absences » n'existent pas — ce qui déplace la cote c'est
la pesée ratée, le remplaçant de dernière minute, le changement de catégorie.

### 3.6 README : un produit qui n'existe pas — `README.md`

Vérification poste par poste contre le code. **Sept éléments annoncés
n'existent nulle part dans le dépôt** : console de log « style Matrix »,
courbes Chart.js, intégration QuantStats, export PDF, ticker de news, ratios
Sortino/Calmar, monitoring BetterStack.

Le plus grave était chiffré : le README annonçait **« Kelly 25 % »** avec la
formule `Mise = Bankroll × (Edge / Odds) × 0.25`. Le code applique 0.08–0.15
selon le sport (`KELLY_FRACTION`). **Faux dans un rapport de 2 à 3.** Sur un
système de mise, un chiffre de documentation faux ne fait pas perdre du
temps : il fait perdre de l'argent.

Également retirés : « Max Drawdown 15 % (hard stop) » et « Stop Loss dynamique
selon volatilité » (aucune constante, aucun code), et le tableau de valeurs
cibles (Win Rate > 65 %, Sharpe > 2.0, Sortino > 2.5, Profit Factor > 2.0)
dont **rien n'était calculé** — `sharpe` n'apparaît que dans un commentaire.

### 3.7 Wiz : le correctif des sources ramenait des faits, le plafond les jetait

> ⚠️ **SOUS-SYSTÈME SUPPRIMÉ le 2026-08-26.** La page /wiz et son moteur
> (`run_wiz.py`, `core/wiz_*`, `wiz.yml`) n'existent plus — décision
> opérateur. Les fichiers et tests cités ci-dessous ont été supprimés avec
> eux ; cette section est conservée comme RÉCIT (la leçon sur les sources
> qui « répondent » sans porter de fait reste valable ailleurs), pas comme
> carte d'un code existant.


Trouvé en **mesurant les sources en réseau réel**, pas en lisant le code.

`core/wiz_sources.py::gather()` fusionne Google News et Bing News, déduplique,
puis tronque à `MAX_TOTAL = 12`. Or `FREE_SOURCES` commence par Google News —
qui couvre presque tous les matchs mais dont **tous** les items sont des
titres nus : sa `<description>` RSS recopie le titre, et `_echoes_title()` en
vide donc le contenu. `keep()` empilant dans l'ordre des sources, la
troncature finale **gardait en priorité ce qui ne porte aucun fait**.

Mesuré sur Espanyol–Real Madrid, deux requêtes, avant correction :

| | items rendus | dont porteurs de faits |
|---|---|---|
| avant | 12 | **5 (42 %)** |
| après | 12 | **10 (83 %)** |

Les 7 titres nus occupaient 58 % du prompt pour n'y annoncer que leur propre
titre, et la troncature faisait tomber **tous** les extraits de la seconde
requête. Un modèle à qui l'on sert majoritairement des titres répond
`INDISPONIBLE` — et c'est la bonne réponse de sa part : on ne lui avait rien
donné à lire.

Correction : tri **stable** « les faits d'abord » avant la troncature. Les
titres nus ne sont pas supprimés (un titre « X forfait » informe), ils passent
après. Gardé par `tests/test_wiz_sources.py::TestLesFaitsDabord`.

> Ceci n'annule pas §5.1 : la cause racine de l'`INDISPONIBLE` en production
> peut être ailleurs (Mistral, quota). Ce défaut-ci est **mesuré et corrigé** ;
> il reste à voir ce que le run de 16:15 UTC produit.

### 3.8 L'erreur de cet audit : « aligner » `.python-version` a cassé le déploiement

À consigner, parce que la leçon vaut plus que l'incident.

Le test neuf `test_une_seule_version_de_python` a signalé que
`.python-version` annonçait **3.12** quand `CLAUDE.md`, `vercel.json`,
l'interpréteur local et les 14 workflows disaient **3.11**. Un fichier contre
cinq : conclusion tirée, « dérive », aligné sur 3.11.

Le déploiement suivant a échoué :

```
Failed to run "uv sync --active --no-dev --link-mode hardlink --locked --no-editable"
error: No interpreter found for Python 3.11 in managed installations or search path
```

`.python-version` est **le seul fichier du dépôt que Vercel lit** pour choisir
son interpréteur, et son image de build n'embarque pas 3.11. Le 3.12 n'était
pas une dérive : c'était la contrainte de la plateforme de déploiement.
Conséquence concrète : la production est restée sur le commit précédent —
donc **sans le correctif de sécurité de `/api/audit/run`** — jusqu'à la
réparation.

**Ce que ça enseigne, au-delà du cas.** Une valeur isolée n'est pas une valeur
fausse. Avant d'aligner un fichier sur la majorité, il faut savoir **qui le
lit**. Ici la majorité (les runners GitHub) et le minoritaire (Vercel) ont
deux lecteurs distincts et deux contraintes distinctes ; les « accorder »
revenait à casser l'un pour faire plaisir à l'autre.

Et surtout : **un test qui encode la mauvaise règle est pire qu'aucun test**.
Il donne l'autorité d'une suite verte à une erreur. Le test a été remplacé par
trois tests qui disent la règle réelle :

- `test_python_version_appartient_a_vercel` — ce fichier reste à 3.12, avec
  le message d'erreur de Vercel cité dans le code ;
- `test_vercel_json_annonce_la_meme_version_que_python_version` — deux
  fichiers de config Vercel qui se contredisent, c'est la prochaine personne
  qui corrige le mauvais des deux ;
- `test_les_workflows_partagent_une_seule_version_de_python` — la règle
  réellement utile : les runners doivent rester d'accord **entre eux**.

État final assumé et testé :

| Lecteur | Version | Fichier |
|---|---|---|
| Runners GitHub + dev local | 3.11 | `.github/actions/setup` — l'UNIQUE `setup-python` du dépôt depuis la refonte du 2026-08-26 ; les 6 workflows passent par elle |
| Build Vercel | 3.12 | `.python-version`, `vercel.json` |

Le code doit rester compatible avec les deux.

### 3.8 Le dépôt vit sur DEUX interpréteurs — et un test avait encodé le contraire

**Régression introduite par cet audit même, et la leçon la plus utile qu'il
ait produite.**

Le test neuf `test_une_seule_version_de_python` avait trouvé que
`.python-version` annonçait 3.12 contre 3.11 partout ailleurs. L'alignement
paraissait évident — il a été fait dans le mauvais sens. L'image de build
Vercel **n'embarque pas Python 3.11** :

```
Warning: Python version "3.11" detected in .python-version is not installed
         and will be ignored.
Using python version: 3.12
error: No interpreter found for Python 3.11 in managed installations
```

Le déploiement a échoué et la production est restée bloquée sur le commit
précédent — **donc sans le correctif de sécurité de §3.1**. Une correction de
sécurité non déployée ne protège rien.

La règle réelle, désormais écrite dans le test :

| Fichier | Interpréteur | Qui l'impose |
|---|---|---|
| `.python-version`, `vercel.json` | **3.12** | Vercel — son image de build n'a pas 3.11 |
| les 14 workflows, le dev local | **3.11** | choix du projet |

Ce n'est pas une incohérence à réparer, c'est une contrainte subie.

> **La leçon** : un test qui encode la mauvaise règle ne se contente pas
> d'être inutile — il donne l'autorité d'une suite verte à une erreur. Ici
> 1012 tests au vert accompagnaient un déploiement cassé, parce qu'aucun
> d'eux ne déployait quoi que ce soit. Après toute modification de
> `vercel.json`, `.python-version`, `requirements.txt` ou `api/index.py` :
> **vérifier le déploiement, pas seulement la suite.**
> ```bash
> python scripts/ops.py vercel deployments | head -3   # READY, pas ERROR
> curl -s https://predator-two.vercel.app/api/health
> ```

### 3.9 Époque zéro : le système commence en août 2026

Décision opérateur du 2026-08-22 : « predator n'était pas au point et avait
des bugs en juillet, on recommence tout en août ». Les lignes de juillet ne
mesurent donc pas le système actuel — les garder dans les agrégats revenait à
juger la version d'aujourd'hui sur les erreurs d'une version corrigée depuis.

**Ce qui a été fait, en deux temps qui se complètent.**

*En base* — `sql/migrate_v10_5_archive_pre_august.sql` a déplacé 206 lignes
vers `ai_learning_ledger_archive` : 194 de juillet, plus 12 de sports retirés
encore présents en août. Sept lignes de `signals` (esports, tabletennis,
toutes `settled`/`closed`) sont parties vers `signals_archive`. Il reste 126
lignes vivantes, août uniquement, quatre sports.

C'est un **déplacement, pas une destruction**, conformément à la politique
déjà écrite dans `sql/archive_retired_sports.sql` : ces lignes (cotes, edge
d'entrée, CLV, issue réelle) sont la seule trace empirique du comportement
passé, et « juillet était buggé » est une hypothèse qu'on peut vouloir
re-vérifier sur pièces. Un backtest futur qui ignorerait des paris réglés
souffrirait d'un biais de survie. Le bloc RESTAURATION du script donne le
chemin inverse. Une sauvegarde JSON des 206 lignes a été prise avant
exécution.

*Dans le code* — `PERF_START_MONTH` (`core/perf_view.py`, défaut `2026-08`)
empêche tout mois antérieur de remonter sur /performance, **même si des
lignes étaient réinsérées**. La condition est portée deux fois — dans
`shown_months()` et dans `filter_rows()` — parce que relever
`PERF_MONTHS_SHOWN` pour inspecter un historique ne doit pas ramener juillet
en douce dans les agrégats : il faut abaisser la borne explicitement.

Sans cette borne, la fenêtre glissante afficherait une carte « juillet
0 gagné / 0 perdu » — un mois vide qui ne dit pas « aucun pari » mais
« période exclue ». Afficher 0/0 pour une période volontairement écartée
trompe davantage que de ne rien afficher.

### 3.10 /performance : moins de littérature, même rigueur

Demande opérateur : « il y a trop de littérature et d'informations, mets
juste les infos essentielles ». Quatre sections ont quitté la page — seuils
d'edge appris, dernier cycle d'apprentissage, calibration de Brier par
tranche de confiance, découpage par mois. Ce sont des rouages internes, pas
des résultats ; ils restent mesurés et lisibles ailleurs (table
`brier_scores`, `meta.threshold_<sport>`, `scripts/weekly_report.py`).

**Ce qui n'a PAS été simplifié, et pourquoi.** Le code portait une règle
explicite : *ne jamais afficher un taux de réussite sans son intervalle de
Wilson et le seuil de rentabilité après taxe* — parce que « 3 gagnés sur 4 »
fait 75 % et ne prouve rien. Supprimer cette garde aurait été une régression
de sûreté sur un système de mise, pas une simplification.

Elle est donc **traduite au lieu d'être retirée**. Là où la page affichait
« IC 95% [41.2 – 66.8%] · seuil rentable net taxe 57.0% ✗ pas confirmé »,
elle affiche maintenant :

> **À confirmer** — il faut 57 % de réussite pour être rentable, et 85 paris
> ne suffisent pas encore à le prouver.

Même calcul, même prudence, une phrase que l'on lit sans dictionnaire. Le
tableau par sport suit la même logique : les colonnes « IC 95% » et « seuil
rentable » disparaissent, la colonne **Verdict** qu'elles alimentaient reste.

La table d'emojis codée en dur dans le template (deux copies, divergentes de
`api/index.py`) est remplacée par l'injection — même correctif que sur
`index.html`, même raison (§1). 386 → 286 lignes, 13 règles CSS mortes
retirées, quatre appels Supabase de moins par chargement.

### 3.11 Périmètre sports : Predator ne parie jamais au-dessus de 2,20 — par construction

À la question « quels sports à plus grosses cotes ? », la mesure a répondu avant l'opinion.
Sur 254 paris décisifs (ledger + archive), **un seul** au-dessus de 2,20, perdu. Ce n'est pas
l'échantillon : `SHARP_PROB_BY_MARKET` (`core/paim_engine.py:36-41`) n'accepte que les
sélections à ≥ 50–55 % de probabilité sharp (cote juste ≤ 2,0), le football ne produit que
l'AH 0.0 du favori (`core/math_engine.py:200-219`), et le plafond de cote appris
`_ODDS_CEILINGS` est dormant (commit `1af5ff2`). Et la seule tranche rentable est la plus
courte : **< 1,50 → 81 % de réussite, +2,2 u ; 1,50–2,20 → 44–48 %, −33 u.**

Conséquence écrite noir sur blanc parce que la question reviendra : **un sport « à grosses
cotes » ne changerait pas les cotes pariées** — le moteur y chercherait encore le favori, et le
biais favori-outsider joue contre un bot d'edge-contre-le-sharp. Décision opérateur : doctrine
inchangée ; on ajoute pour le volume. Les sports choisis sont ceux où le favori court est la
norme : **NCAAF** (sport-type dédié `college_football`) et **tennis Grand Chelem / Masters**
(clés OddsAPI dynamiques, infra déjà complète). Détail, coûts, non-retenus :
`reports/refonte_scope_2026-08.md` §9.

Deux contraintes à garder en tête pour le prochain ajout : le **settlement est une recherche
web pour tous les sports** (aucune API de scores — un sport aux scores introuvables laisse ses
signaux `active` pour toujours), et le **catalogue OddsAPI** est sondable gratuitement
(`/sports`, 175 clés le 2026-08-22 : ni darts, ni snooker, ni golf en match, ni volley).

---

## 4. Vérifié sain (ne pas re-diagnostiquer)

Mesuré le 2026-08-22, avec la méthode :

- **Invariant des sport-keys** — les 4 fichiers couvrent les 10 sports actifs,
  0 manquant.
- **Routes ↔ liens ↔ assets** — les 13 routes Flask répondent ; tous les
  `href`/`src` locaux des 6 pages résolvent en 200 ; aucun résidu Jinja ni
  trace d'exception dans le HTML rendu. Les 6 pages portent 6 entrées de nav
  avec le bon état actif.
- **Garde des sports retirés** — dernière émission `esports` le 2026-08-05 et
  `tabletennis` le 2026-08-02, toutes deux **antérieures** au retrait du
  2026-08-22. La garde de `_emit` tient.
- **Les 6 fournisseurs IA non câblés** portent tous un `terms_flag`
  (`payment_required`, `non_commercial`, `evaluation`, `quota_zero`) — ils
  sont exclus de `PRODUCTION_SAFE` **exprès**, ce n'est pas un oubli.
- **Les `terms_flag` du registre sont confirmés PAR L'INFÉRENCE RÉELLE**
  (`python scripts/ops.py ai`, 2026-08-22) — pas par lecture de catalogue,
  qui ne prouve rien. Chaque fournisseur marqué échoue bien comme annoncé :

  | Fournisseur | `terms_flag` | Réponse réelle |
  |---|---|---|
  | cerebras, chutes, sambanova | `payment_required` | **HTTP 402** |
  | scaleway | `quota_zero` | **HTTP 429** INSUFFICIENT QUOTA |
  | cohere, upstage | `non_commercial` / `evaluation` | OK — mais exclus de la production **exprès** |
  | gemini, cloudflare, openrouter, ollama_cloud | *(aucun)* | **OK** |

  Le registre dit donc la vérité sur le terrain. Seule nouveauté :
  `ZHIPU_API_KEY` rend **401 « token expired or incorrect »** — la clé est
  périmée. Impact nul (Zhipu est `non_commercial`, donc hors production), à
  renouveler ou à retirer du `.env` au choix de l'opérateur.
  **Tranché le 2026-09-03** (décision opérateur, ré-mesuré par `ops.py ai`) :
  cerebras, chutes, sambanova, scaleway, cloudflare (401 désormais) et zhipu
  sont RETIRÉS du registre — 11 fournisseurs, 8 sans clause restrictive.
  Voir INCIDENTS.md « Six fournisseurs IA morts retirés du registre ».
- **`team_aliases`** existe en base (12 lignes) : la migration `v10_3` est
  bien appliquée, contrairement à ce que `CLAUDE.md` laissait entendre.
- **Aucun TODO/FIXME/HACK** dans le code de production.
- **14 workflows, YAML valide**, tous bornés en durée, tous en Python 3.11
  (Vercel, lui, construit en 3.12 — divergence VOULUE, voir §3.8).
- Les 2 fichiers orphelins de la Phase 1 (`api/static/logo.jpg`,
  `.vercel-build-trigger`) ont bien été supprimés.
- **Vérifié sur la PRODUCTION** (`predator-two.vercel.app`, commit `3934fd5`)
  et non seulement en local : les 6 pages rendent en 200, `/api/health`
  annonce la version unique `10.4` et `db_configured: true`,
  `POST /api/audit/run` sans jeton renvoie bien **401**, et `/api/signals`
  rend **14** signaux jouables contre **37** en `?all=1` — le filtre mesuré
  en base (37 actifs, 23 déjà commencés) se retrouve exactement à l'écran.

---

## 5. Ouvert / non vérifié

Honnêteté du document : ce qui suit n'est **pas** réglé.

### 5.1 Wiz : 100 % d'INDISPONIBLE — troisième cause trouvée, la localisation ne tirait jamais

> ⚠️ **SOUS-SYSTÈME SUPPRIMÉ le 2026-08-26.** La page /wiz et son moteur
> (`run_wiz.py`, `core/wiz_*`, `wiz.yml`) n'existent plus — décision
> opérateur. Les fichiers et tests cités ci-dessous ont été supprimés avec
> eux ; cette section est conservée comme RÉCIT (la leçon sur les sources
> qui « répondent » sans porter de fait reste valable ailleurs), pas comme
> carte d'un code existant.


**Chronologie des trois causes, toutes mesurées le 2026-08-22.**

1. Matin — les sources ne portaient pas de faits (titres nus de Google News, plafond qui
   jetait les extraits de Bing). Corrigé (`9bd8a6b`).
2. Après-midi — la requête sélectionnait les pages de preview (« team news lineup » est
   leur titre SEO), en ET implicite, en anglais partout. Refondue en groupes OR dans la
   langue de la presse locale, avec la locale du flux (`cb4f44f`). Mesuré en local :
   3 % → 23 % de sources porteuses de faits.
3. Soir — **run 18:46 sur `e6ff5ad` : 13 INDISPONIBLE, 0 verdict, mais 6,2 sources en
   moyenne contre 4,8.** R4 n'est pas en cause (le modèle rend lui-même `arguments: []`,
   aucun rejet loggé). En rejouant la chaîne exacte de prod en local : **`signals.league`
   vaut « Serie A » nu, « Liga Profesional Argentina », « Major League Soccer » — sans le
   préfixe pays que `press_lang` lisait.** La localisation, juste sur le papier, n'avait
   JAMAIS tiré en production pour le Brésil et l'Argentine : Cruzeiro–Flamengo partait en
   requête anglaise (2 items de fait) au lieu de portugaise (7). Le libellé de ligue vient
   de QUATRE sources avec quatre conventions — OddsAPI met le pays en suffixe ou dans le nom,
   Matchbook/api-sports donnent des noms nus ou abrégés, seules les sources gratuites
   préfixent. `press_lang` accepte désormais les quatre, testé sur les 54 libellés réels
   de la base (`tests/test_wiz_engine.py::TestLangueDeLaPresse`).

**Ce que le rejeu local a aussi montré**, à garder en tête si le prochain run reste à
100 % : via le routeur IA (pas de clé Mistral en local), un modèle a bien produit un verdict
(VETO, 1 red flag) à partir des mêmes sources — mais en **confondant** le match de
Libertadores déjà joué avec celui de Serie A à venir. Et un modèle à raisonnement a rendu sa
chaîne de pensée au lieu du JSON (non parsable). Donc, si la collecte localisée ne suffit
pas : (a) le prompt ancré devrait rappeler la DATE du match et exiger d'écarter les articles
sur un autre match des mêmes équipes ; (b) `mistral-small-latest` est peut-être trop prudent
pour extraire un fait d'un titre — à comparer avec un modèle plus grand sur le même bloc.

→ Requête à passer après le prochain run (cron `15 */2`) :
```bash
python scripts/ops.py supabase sql "select verdict, count(*), round(avg(sources_count),1) \
  from wiz_analysis where analyzed_at > now() - interval '2 hours' group by 1"
```

Indépendant : Tavily `HTTP 432` (quota de plan) et connecteur `web_search` Mistral épuisé —
pas la cause, mais Wiz est privé de ses deux replis.


### 5.2 Deux clés production-safe ne sont pas encore obtenues

`OVH_AI_API_KEY` et `SILICONFLOW_API_KEY` sont désormais **câblées** dans les
7 workflows, mais aucun secret n'existe côté GitHub ni dans le `.env` local.
Le câblage est inoffensif (clé absente = fournisseur ignoré) et deviendra
actif le jour où l'opérateur ouvre les comptes. Rien à faire si ce n'est
souhaité — c'est de la capacité de repli, pas un manque.

### 5.3 ✅ `DASHBOARD_ADMIN_TOKEN` — posé sur Vercel le 2026-08-22 (19:15 UTC)

Fait : jeton généré (`secrets.token_urlsafe(32)`), posé en variable
d'environnement **production** sur Vercel, redéploiement effectué, copie dans
le `.env` local (gitignoré) pour l'opérateur. Vérifié en prod sur l'alias
public : `POST /api/audit/run` sans jeton → **401**, mauvais jeton → **401**,
`GET` → **405**. Le bon jeton n'a volontairement pas été testé en production :
il déclencherait un vrai `audit.yml` de 45 minutes.

Usage : `curl -X POST https://predator-two.vercel.app/api/audit/run -H "X-Predator-Token: $DASHBOARD_ADMIN_TOKEN"`

### 5.4 Non traité délibérément

- **Découpage des gros fichiers** (`run_engine.py`, `core/ai_router.py` 1086 l.,
  `core/learning_layer.py` 1085 l., `api/index.py` ~1050 l.) — couverts par
  les tests et stables. Découper un moteur qui gagne de l'argent pour un
  critère de taille : risque > gain.
- **`base.html` Jinja** pour les 6 templates — la duplication du `<head>` et
  de la nav est réelle, mais chaque page a des variations de style voulues.
  À faire seulement si on retouche le dashboard pour une autre raison. La nav
  du bas, elle, est désormais identique partout (vérifiée par smoke test).
- **Entrées mortes de `.env.example`** (`NEWS_API_KEY`, `PERPLEXITY_API_KEY`,
  `BETTERSTACK_*`, `PREDATOR_SECRET`…) — conservées **exprès**, chacune avec
  une mention `⚠️ UNUSED` datée. Elles servent de pierre tombale : sans
  elles, quelqu'un les réintroduit de bonne foi.
- **Closing line des totals/spreads Tier 2 : limite STRUCTURELLE** (mesuré
  2026-09-02, 0/14 tarifés sur 7 jours). Un signal d'une ligue hors
  `SPORT_KEYS` n'existe pas dans le payload OddsAPI (`capture_from_scan`)
  et la voie exchange est h2h-only par contrat (il faut la MÊME ligne que
  le pari — `_line_market_close`). Aucune voie de capture n'existe : à
  accepter, pas à « réparer » — sauf à payer une source qui cote ces
  lignes. Le compteur `count_missed_closing_lines` documente désormais
  ses vraies causes (stock, pas flux).

---

## 6. Comment refaire cet audit

```bash
python -m pytest tests/ -q                      # doit rester à 0 échec
python -m pyflakes $(git ls-files '*.py')       # doit rester vide
python scripts/ops.py doctor                    # credentials
python scripts/ops.py status                    # santé pipeline en un écran
python scripts/ops.py ai                        # INFÉRENCE réelle par fournisseur
python scripts/ops.py sources                   # chaque source de cotes
```

Le dashboard ne se vérifie **que** par rendu réel — la suite de tests ne rend
aucun template et n'appelle aucune route Flask. Utiliser la skill
`predator-dashboard-check`.

> `python scripts/ops.py ai` est le seul diagnostic qui tranche pour l'IA :
> un catalogue lisible ne prouve rien (Cerebras/SambaNova/Chutes rendent 200
> sur `/models` et 402 à l'inférence ; Scaleway rend 429 quota-zéro).
