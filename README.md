# Riven Sniper

Prototype personnel de surveillance **Dual Toxocyst · PC · achat immédiat entre 10 et 2 000 pl inclus**. Aucune condition sur les stats, les rolls, la polarité ou le statut en ligne du vendeur. Le prix de départ d'une enchère ne compte pas comme un prix d'achat immédiat.

Il affiche les annonces, émet un son et/ou une notification de navigateur après activation, conserve les détections dans SQLite et exporte un CSV. Le bouton **Aleca reçue** note manuellement l'arrivée de l'alerte concurrente pour la même annonce et le même événement de prix. Aucun compte warframe.market ni secret n'est nécessaire ; aucune interaction avec le jeu, aucun achat et aucun message automatique.

## Lancer sur ton PC Windows

Installer Python **3.11 ou plus récent**, ouvrir un terminal dans le dossier du projet, puis :

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m riven_sniper --open
```

Le navigateur s'ouvre sur le tableau local. Activer le son et, si souhaité, autoriser les notifications dans le tableau. Garder le terminal et l'onglet ouverts ; arrêt avec **Ctrl+C**. Le navigateur ou Windows peuvent retarder les notifications en arrière-plan ou en mode concentration : vérifier ces réglages pendant la comparaison.

Pour essayer sans accès à warframe.market :

```powershell
.\.venv\Scripts\python.exe -m riven_sniper --demo --open
```

La simulation ajoute une annonce toutes les six secondes et utilise une base séparée. Ses délais et ses vendeurs sont fictifs.

Linux/macOS/cloud :

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m riven_sniper --demo
```

Sur une machine avec navigateur local, ajouter `--open`. Dans le cloud, les requêtes locales servent à la validation ; aucun service n'est exposé publiquement. Le code est dans le checkout existant `/workspace/test`, sans worktree supplémentaire.

## Comparer à AlecaFrame

1. Créer dans AlecaFrame une alerte **Dual Toxocyst, PC, 10–2 000 pl**, sans filtre de stats/rolls, et vérifier que la même définition du prix d'achat immédiat est utilisée. Le prototype inclut les vendeurs hors ligne ; aligner aussi ce critère.
2. Démarrer le prototype avant les annonces à mesurer. La première réponse initialise une référence **sans notification**, même après un redémarrage. Les annonces apparues pendant l'arrêt ne sont donc pas mesurées comme de nouvelles alertes au redémarrage.
3. Quand Aleca signale une annonce déjà détectée ici, vérifier son lien/identifiant et son prix, puis cliquer immédiatement **Aleca reçue** sur la ligne correspondante. Si Aleca arrive avant le radar, utiliser le formulaire de mémorisation avec le lien de l'annonce et son prix : l'heure est conservée pour l'associer à la future détection exacte, ce qui permet un delta négatif. Ces heures en attente sont conservées uniquement pendant la session ; une annonce jamais détectée reste non appariée. Une nouvelle annonce et une modification de prix sont deux événements distincts. Les anciennes lignes sont un historique, pas une garantie de disponibilité actuelle.
4. Répéter sur plusieurs annonces communes aux deux outils et exporter le CSV. Un delta positif signifie que la détection du prototype précède ton clic après Aleca. Le clic est manuel : ce delta **inclut ton temps de réaction**. La médiane ne porte que sur les événements appariés, pas sur les annonces manquées par un outil. Il n'y a aucune lecture automatique des notifications Aleca.

`detected_at` est l'heure de réception/traitement de la réponse API par le script, pas l'heure d'affichage de sa notification. Le tableau est actualisé toutes les 500 ms ; le navigateur peut ajouter son propre délai. `api_age_seconds` est `detected_at - created` fourni par le site : cette différence peut inclure cache, ancienneté et décalage d'horloge. Elle ne prouve pas le délai de publication réel. Les horodatages CSV sont en UTC ; le tableau utilise l'heure locale du navigateur.

## Architecture et vitesse possible

```text
API HTTPS publique → une connexion persistante / un poller
                  → filtre local arme + achat immédiat
                  → déduplication persistante / événements de prix
                  → tableau local → son / notification
                  → journal SQLite + CSV + comparaison manuelle
```

Le prototype consulte `GET https://api.warframe.market/v1/auctions`, en-tête `Platform: pc`, sans authentification. Ce flux global récent est utilisé par des clients publics ; son ordre, sa taille maximale, sa fréquence d'actualisation et sa compatibilité actuelle **restent à vérifier sur l'API réelle**. Les annonces fermées, privées, masquées, explicitement hors PC ou sans achat immédiat sont ignorées. L'en-tête PC n'atteste pas à lui seul l'éligibilité cross-save/2FA de chaque vendeur.

La [FAQ officielle actuelle](https://www.warframe.com/en/crossprogression), consultée pour ce prototype, précise que Cross Platform Play doit être activé des deux côtés ; pour échanger avec un compte PC, le joueur sur une autre plateforme doit avoir un compte Cross Platform Save. TennoGuard 2FA est requis pour les comptes PC/cross-save. Le Platinum ne s'échange pas entre Nintendo Switch et les autres plateformes. Ces conditions se vérifient avec le vendeur ; le script ne les certifie pas.

Une recherche `/auctions/search` triée par prix risque de masquer les nouvelles annonces hors de la première page. Aucun tri `created_desc` non documenté n'est utilisé. L'instantané global évite ce tri mais peut lui aussi omettre des annonces s'il est limité en taille, peu actualisé ou si beaucoup d'annonces arrivent entre deux réponses. **La couverture exhaustive n'est pas garantie.** Les en-têtes `Date`, `Age` et `Cache-Control`, lorsqu'ils existent, sont conservés dans l'état du tableau pour examiner le cache.

Cadence initiale : **une requête par seconde**, sans requêtes concurrentes ni requêtes supplémentaires par annonce. La connexion HTTPS est réutilisée, la vérification TLS reste activée et les accès proxy sont respectés. Les erreurs transitoires déclenchent une pause croissante ; `429` et `Retry-After` sont respectés. Un refus d'accès ou un format incompatible arrête la collecte en affichant le problème. Ne pas lancer plusieurs instances : les quotas du site peuvent être partagés avec d'autres outils utilisant la même connexion.

La limite historique de **3 requêtes/s** est citée par des clients communautaires. La documentation officielle actuelle n'a pas pu être consultée dans ce cloud : **1 requête/s est un réglage prudent, pas une autorisation actuelle vérifiée ni une promesse de vitesse**. Vérifier les règles actuelles de warframe.market avant un usage prolongé. Aucun flux WebSocket public officiellement autorisé pour les nouvelles annonces Riven n'a été confirmé ; le prototype n'utilise pas de canal privé, de contournement de cache ou de rotation d'adresses.

**Faisabilité :** battre une collecte qui interroge moins souvent est plausible une fois l'annonce visible dans l'API. En revanche, si l'API ne rafraîchit son flux que périodiquement, interroger chaque seconde ne rendra pas les annonces visibles plus tôt. Un autre client public emploie un cycle de 120 secondes ; cela justifie de mesurer le flux, mais ne prouve pas un cache serveur de 120 secondes. Aucun gain sur les 45–120 secondes d'AlecaFrame n'est encore établi. Un abonnement public officiel aux événements Riven serait préférable s'il devient documenté et accessible.

Pour les cinq armes plus tard, conserver un budget réseau commun. Le flux global peut être filtré pour plusieurs armes sans multiplier les requêtes. Les seuils envisagés, à confirmer avant extension, sont Burston 10–401, Dual Toxocyst 10–201, Torid 10–370, Verglas 10–350 et Magistar 10–501 pl ; **seul le test Dual Toxocyst 10–2 000 est activé ici**.

## Options et données

```sh
python -m riven_sniper --help
python -m riven_sniper --min-price 10 --max-price 200 --interval 2 --open
```

`--interval` accepte une valeur ≥ 1 seconde. `--data-dir` choisit le dossier des bases, par défaut `.riven-sniper/` (ignoré par Git). `--port` choisit le port local, par défaut 8765. Les historiques réel et simulé sont séparés. Les changements de prix et les entrées dans la fourchette génèrent une nouvelle alerte ; un identifiant/prix inchangé ne sonne pas de nouveau. La première réponse de chaque session reste silencieuse.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Les tests couvrent le filtrage, les événements et la persistance, la comparaison, les erreurs de l'API simulée et le serveur HTTP local. La démonstration permet d'exercer le parcours complet sans API externe. Ils ne prouvent ni l'accessibilité, ni la fraîcheur, ni les performances du flux réel.

Validation réalisée : **43 tests réussis**, interface exercée dans Chromium, et parcours complet du vrai serveur local en simulation : initialisation silencieuse, détection d'une nouvelle offre, appariement Aleca avant détection avec delta négatif, puis export CSV. Le serveur réel affiche le refus d'accès et arrête sa collecte ; il ne simule pas une surveillance réussie.

Dans ce cloud, les premières requêtes vers warframe.market ont été refusées par le proxy (`CONNECT 403`). Le client final reçoit maintenant une **page de protection du site en HTTP 403**, tout comme la documentation API : le service réel et sa vitesse ne sont pas validés. Aucun contournement n'est effectué. L'accès à l'API doit être accepté par le service ; ajouter un domaine réseau ne résout pas à lui seul cette protection. Le script peut être essayé sur ton PC, sous réserve des règles actuelles et de l'accès autorisé au service ; en cas de refus persistant, s'adresser à warframe.market plutôt que contourner sa protection.

Les domaines `api.warframe.market`, `warframe.market` et `www.warframe.com`, ainsi que les instructions d'installation/démarrage, sont enregistrés dans le brouillon d'environnement pour revue. Un brouillon enregistré ne publie pas l'environnement et ne prouve pas une mise à jour de la machine. Revoir/enregistrer ces réglages puis publier depuis les paramètres pour conserver l'environnement préparé. Les processus devront être relancés dans les futures tâches. Aucun résultat de vitesse réelle ne doit être annoncé avant un essai réel réussi.

## Sources et limites de vérification

- Documentation officielle à vérifier : [API warframe.market](https://warframe.market/api_docs), [documentation v2 référencée par des clients](https://42bytes.notion.site/WFM-Api-v2-Documentation-5d987e4aa2f74b55a80db1a09932459d).
- [Ancien schéma communautaire des enchères](https://github.com/RNG21/wfm-api-docs/blob/master/api-doc.yml) : champs de prix, recherche et en-tête de plateforme. Source ancienne, pas un contrat actuel.
- [Client pywmapi](https://github.com/leonardodalinky/pywmapi#readme) : cite la limite historique et la documentation officielle.
- [Client public utilisant l'instantané récent](https://github.com/IgorGorskiy/Warframe-Riven-Scanner/blob/main/auction_worker.py) : endpoint global et stratégie de synchronisation. Source secondaire, pas une garantie de service.
- [FAQ officielle cross-save](https://www.warframe.com/en/crossprogression) : règles d'éligibilité des comptes et échanges vérifiées sur cette page ; le prototype ne certifie pas l'éligibilité de chaque compte.
