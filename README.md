# Riven Sniper · version 0.3.0

Cinq recherches PC, une collecte commune et des alertes Discord avec les détails du Riven et le message `/w` prêt à copier.

| Arme | Achat immédiat, bornes incluses |
| --- | --- |
| Burston | 10–401 pl |
| Dual Toxocyst | 10–201 pl |
| Torid | 10–370 pl |
| Verglas | 10–350 pl |
| Magistar | 10–501 pl |

Aucun filtre de statistiques, rolls, polarité ou présence du vendeur. Un prix de départ d'enchères sans achat immédiat n'est pas un prix de vente retenu. Les annonces fermées, privées, masquées ou explicitement hors PC sont ignorées.

## Installer ou mettre à jour sur Windows

1. Arrêter l'ancienne fenêtre du programme avec **Ctrl+C**.
2. Extraire le nouveau ZIP dans un nouveau dossier.
3. Avec Python **3.11 ou plus récent** installé, double-cliquer sur **`Lancer-Riven-Sniper.cmd`**. Il prépare les dépendances et ouvre le tableau local.
4. Vérifier le repère **03** en haut du tableau et les cinq seuils affichés.

Conserver la fenêtre de commande ouverte pendant la surveillance. Le navigateur peut ensuite être fermé : les alertes Discord continuent tant que le programme et le PC restent actifs.

Les données d'une ancienne installation restent dans son sous-dossier `.riven-sniper/`. Pour garder l'historique, copier ce sous-dossier vers le nouveau dossier après avoir arrêté les deux instances. La base est migrée automatiquement ; l'ancien historique Dual Toxocyst au-dessus de 201 pl est conservé dans la base, mais exclu du radar et de l'export correspondant aux nouveaux filtres.

Commandes manuelles équivalentes :

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m riven_sniper --open
```

## Activer ton salon Discord

Tu peux utiliser ton webhook existant. Son adresse doit rester sur ton PC ; ne pas l'envoyer dans le chat, GitHub ou une capture.

1. Dans le tableau, ouvrir **Alertes Discord**.
2. Coller l'adresse complète du webhook dans le champ prévu.
3. Cocher **Envoyer les nouvelles offres sur Discord** puis **Enregistrer localement**.
4. Cliquer sur **Envoyer un message de test**. Vérifier sa réception dans le salon et le compteur des messages envoyés.
5. Laisser le programme ouvert. Les prochaines détections correspondant aux cinq recherches seront envoyées.

La première collecte après chaque démarrage sert de référence **sans alerte**, et l'activation de Discord ne renvoie pas l'ancien historique. Le test du webhook, lui, est immédiat dès que Discord accepte l'envoi.

Les fiches contiennent l'arme, le nom complet du Riven, le prix, le vendeur et son statut, les rolls, le MR, les attributs disponibles dans le flux, le lien de l'annonce et un bloc de code avec le message d'achat :

```text
/w TznMinos Hi, I am interested in buying your Dual Toxocyst Crita-visitis riven for 201 platinum, listed on Warframe.Market.
```

Les champs manquants sont indiqués comme non fournis ; aucun grade ou score de statistiques n'est inventé. Le bloc de code utilise les fonctions de copie disponibles dans ton client Discord. Le radar local possède aussi un bouton **Copier /w**. Aucun message n'est envoyé au joueur et aucun achat n'est effectué automatiquement.

L'adresse du webhook est conservée dans `.riven-sniper/discord.json`, un fichier local exclu de Git. Elle n'est jamais réaffichée par le tableau, les diagnostics ou les messages d'erreur. Ce fichier contient l'adresse en clair : conserver ce dossier pour soi. Le bouton **Effacer le webhook** supprime l'adresse enregistrée. Un champ vide à l'enregistrement conserve le webhook existant.

Si nécessaire, créer un webhook dans les paramètres du salon Discord → **Intégrations** → **Webhooks**. Le compte doit avoir les droits de gérer les webhooks ; le webhook doit pouvoir publier dans le salon choisi.

## Rapidité et livraison

```text
API publique, une connexion persistante
    → cinq filtres locaux + déduplication
    → journal SQLite + file Discord persistante
    → worker Discord indépendant → salon
```

Ajouter ces cinq armes **n'ajoute aucune requête warframe.market** : les détails sont repris dans la même réponse. Une attente réseau côté Discord ne conserve pas le verrou du collecteur et ne bloque donc pas la collecte. Une file de messages peut néanmoins retarder les alertes Discord en cas de rafale ou de limitation.

Cadence de collecte cible : une requête par seconde, séquentielle. Une réponse de 2,8 secondes implique un intervalle réel d'au moins 2,8 secondes. Les erreurs transitoires entraînent des pauses progressives ; `Retry-After` est respecté. La confiance HTTPS conserve les certificats système et ajoute `certifi`, avec vérification du certificat et du nom d'hôte.

Discord est limité à au plus deux envois par seconde par ce programme ; ses indications de quota et ses `429` sont respectés. Les messages en attente survivent au redémarrage avec le même webhook. Désactiver Discord suspend les envois et n'ajoute pas de nouvelles alertes dans la file. Changer de webhook annule les messages en attente de l'ancien salon. Une alerte en attente depuis plus de dix minutes expire pour éviter un rattrapage d'offres anciennes. Le tableau affiche les envois, les attentes, les erreurs et les expirations.

Les identifiants d'événements empêchent les doublons ordinaires. Si un envoi a été accepté par Discord mais que sa réponse réseau est perdue, une nouvelle tentative peut néanmoins créer un doublon. Les changements de prix ou retours dans la fourchette constituent de nouveaux événements.

## Comparer à AlecaFrame

Aligner les cinq armes, les prix immédiats et les critères de présence du vendeur. Le bouton **Aleca reçue** note manuellement l'arrivée de l'alerte correspondant au même identifiant/prix. Si Aleca arrive en premier, le formulaire de mémorisation conserve cette heure jusqu'à la future détection correspondante. Ces heures en attente ne survivent pas à l'arrêt.

Un delta positif est l'heure de ton clic après Aleca moins l'heure de détection du serveur : il inclut ton temps de réaction. Le tableau se rafraîchit toutes les 500 ms. La livraison Discord et les notifications Windows ajoutent leur propre délai. Une médiane calculée sur les annonces appariées ne mesure pas les annonces manquées par un outil.

Le CSV utilise UTC ; le tableau utilise l'heure locale du navigateur. `api_age_seconds` compare la date de création déclarée par le site à l'heure de réception, ce qui ne mesure pas directement sa latence de publication. Les anciennes lignes restent un historique, pas une garantie que l'offre est encore disponible.

## Diagnostic et simulation

Si aucune réponse API n'a réussi, le radar n'est pas encore opérationnel. Le compteur sépare tentatives, réussites et échecs. Les erreurs et pauses sont visibles et imprimées dans le terminal. Après des échecs répétés, les pauses peuvent atteindre 120 secondes.

Double-cliquer sur **`Diagnostic-Riven-Sniper.cmd`** pour une seule tentative HTTPS et un rapport sans identifiants, enregistré dans `.riven-sniper/diagnostic.json`. Le bouton **Diagnostic** du tableau exporte aussi les statuts du collecteur et de Discord sans webhook ni historique des vendeurs. Il n'envoie rien vers Discord.

```powershell
.\.venv\Scripts\python.exe -m riven_sniper --diagnose
.\.venv\Scripts\python.exe -m riven_sniper --demo --open
```

La démonstration parcourt les cinq armes avec une annonce synthétique toutes les six secondes. Ses bases et réglages Discord sont séparés dans `demo.sqlite3` et `discord-demo.json`. Il faut donc y configurer le webhook séparément si souhaité, et ses alertes portent **[SIMULATION]**. Aucun résultat de vitesse réelle ne peut être tiré de ce mode.

## Développement

Linux/macOS/cloud, depuis le checkout existant `/workspace/test` sans worktree supplémentaire :

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m riven_sniper --demo
```

Pour une seule recherche avec seuils explicites :

```sh
python -m riven_sniper --weapon dual_toxocyst --min-price 10 --max-price 2000 --interval 1 --open
```

`--interval` doit être ≥ 1 seconde. `--port` change le port local, par défaut 8765. `--data-dir` choisit le dossier de données. Le serveur écoute uniquement sur le PC local. Ne pas lancer plusieurs instances pour multiplier la cadence. Les processus doivent être relancés après restauration d'un environnement cloud ; les requêtes locales y servent à la validation, sans lien public de prévisualisation.

Validation : **72 tests** couvrent les limites des cinq recherches, la migration de l'ancien historique, les données et messages d'achat, le diagnostic, la persistance de la file, la déduplication, les quotas/reprises Discord, les erreurs sans fuite du webhook et l'indépendance de la collecte pendant un envoi bloqué. Le parcours navigateur est vérifié avec un transport Discord simulé. Aucun vrai webhook utilisateur n'est employé dans les tests ; la réception réelle doit être validée par le bouton de test sur le PC. Les lanceurs Windows n'ont pas été exécutés dans ce cloud Linux.

## Source et limites

Le flux utilisé reste `GET https://api.warframe.market/v1/auctions`, avec `Platform: pc`, sans compte ni secret warframe.market. Le flux global est limité en taille et peut être mis en cache : des annonces peuvent manquer entre deux mises à jour. La couverture exhaustive et une avance systématique sur AlecaFrame ne sont pas garanties. Aucun tri de nouveauté non documenté, contournement de cache ou canal privé n'est utilisé.

L'accès au flux a fonctionné sur le PC de l'utilisateur avec la version 0.2. Le cloud a rencontré une page de protection HTTP 403 ; aucune protection n'est contournée. La cadence maximale actuelle doit être vérifiée dans la documentation du service : une requête/seconde est un réglage prudent, pas une certification actuelle des quotas.

Le marché PC ne certifie pas l'éligibilité de chaque vendeur. La FAQ officielle précise les conditions Cross Platform Play, compte Cross Platform Save pour un joueur non-PC échangeant avec PC, TennoGuard 2FA et les restrictions du Platinum Nintendo Switch.

Références : [documentation API](https://warframe.market/api_docs), [ancien schéma communautaire](https://github.com/RNG21/wfm-api-docs/blob/master/api-doc.yml), [client utilisant le flux global](https://github.com/IgorGorskiy/Warframe-Riven-Scanner/blob/main/auction_worker.py), [FAQ cross-save officielle](https://www.warframe.com/en/crossprogression), [webhooks Discord](https://discord.com/developers/docs/resources/webhook), [quotas Discord](https://discord.com/developers/docs/topics/rate-limits).
