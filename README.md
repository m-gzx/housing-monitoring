# Moniteur immobilier (chalets & condos)

Surveille automatiquement deux recherches en parallèle, à partir du même
point de départ (2450 Boul Laurier, Québec, QC G1V 2L1) — chalets/terrains
à vendre bord de l'eau (150 min de route ou moins) et condos 3-5 chambres
(15 min ou moins) — et génère un rapport HTML (carte interactive avec
points cliquables + fiches, un bouton pour basculer entre les deux
recherches) avec un historique roulant des 5 derniers jours, navigable
avec des flèches précédent/suivant. Pas de notion d'annonce "déjà vue" :
une annonce reste visible tant qu'elle est encore trouvée par la
recherche, peu importe si elle a déjà figuré dans un rapport précédent.

Les deux recherches sont définies dans `SEARCHES` (en haut de
`monitor.py`) — ajuster leurs critères (origine, rayon, temps de route,
chambres) directement là. Les valeurs de filtre "condos" pour Centris
(`CENTRIS_PROPERTY_TYPES["condos"]`) et uBee
(`UBEE_INSCRIPTION_TYPES["condos"]`) sont des estimations **non
confirmées** par capture réseau (contrairement à celles de "chalets") —
voir la section 2 ci-dessous si une recherche condos revient
systématiquement à 0 résultat dans les logs.

## 1. Installation (à faire une fois, dans Claude Code)

```bash
cd housing-monitoring
pip install -r requirements.txt
playwright install chromium
```

La deuxième commande télécharge un navigateur Chromium headless
(~150-300 Mo, une seule fois) — utilisé pour obtenir une session Centris
valide face à sa protection Cloudflare (voir section 2).

Aucun secret/compte courriel requis. Pour ajuster le point de départ
(commun aux deux recherches), le rayon de recherche, le temps de route max
ou les chambres, modifier directement `SEARCHES` en haut de `monitor.py`
(ou les constantes individuelles `ORIGIN_ADDRESS`, `MAX_DRIVE_HOURS`,
`SEARCH_RADIUS_KM`, `CONDO_SEARCH_RADIUS_KM`, `CONDO_MAX_DRIVE_HOURS`,
`CONDO_MIN_BEDROOMS`, `CONDO_MAX_BEDROOMS` qu'il référence). `SEARCH_RADIUS_KM`
doit rester assez large pour couvrir `MAX_DRIVE_HOURS` à vol d'oiseau (voir
le commentaire à côté de sa définition) si tu changes ce dernier.

## 2. Centris : requête validée — comment faire pareil pour un autre site

Centris et uBee n'ont pas d'API publique documentée, mais `monitor.py`
utilise maintenant des requêtes internes **confirmées** par inspection
réseau pour les deux (voir sections suivantes). Si tu veux ajouter
DuProprio (toujours non résolu, voir plus bas), ou re-valider Centris/uBee
après un changement de leur site, voici la marche à suivre avec les outils
de développement du navigateur (F12) :

### Marche à suivre générale (Chrome, Edge ou Firefox)

1. Ouvrir le site (centris.ca ou duproprio.com) dans le navigateur.
2. Ouvrir les outils de développement : touche **F12**, ou clic droit
   n'importe où sur la page → **Inspecter** / **Examiner l'élément**.
3. Cliquer sur l'onglet **Réseau** (*Network*) dans le panneau qui s'ouvre.
4. Cocher **Conserver le journal** / **Preserve log** (sinon la liste des
   requêtes se vide à chaque nouvelle page).
5. Dans la barre de filtre du panneau Réseau, filtrer par **Fetch/XHR**
   pour ne garder que les appels d'API (ça retire les images, CSS, etc.
   et rend la liste beaucoup plus courte).
6. Faire la recherche sur le site pendant que le panneau est ouvert :
   "chalet à vendre" + filtre "bord de l'eau", secteur voulu.
7. Dans la liste qui apparaît, chercher une requête dont le nom contient
   un mot comme `search`, `inscriptions`, `properties`, `listings` ou
   `graphql` — c'est généralement celle qui retourne les résultats.
8. Cliquer sur cette requête, puis :
   - onglet **Charge utile** / **Payload** (ou **Request**) : montre ce
     qui a été envoyé (les filtres — région, prix, type de propriété).
   - onglet **Réponse** / **Response** : montre le JSON retourné avec les
     annonces (adresse, prix, coordonnées, id, URL).
9. Le plus simple pour tout capturer d'un coup : clic droit sur la requête
   → **Copier** → **Copier en tant que cURL** (*Copy as cURL*). Ça inclut
   l'URL exacte, les en-têtes et le corps de la requête.
10. Coller ce cURL (en retirant les cookies/tokens de session personnels
    si présents) dans une conversation Claude Code pour ajuster
    `search_centris_listings()` avec les bons noms de champs.

### Spécifique à Centris — ✅ validé le 2026-09-10

- L'endpoint utilisé est `POST https://www.centris.ca/Property/GetInscriptions`
  (vue "Galerie"), paginé par `page`/`pageSize` (20 par page), avec la
  structure de filtres `FieldsValues` déjà intégrée dans `monitor.py`.
- Un premier essai avec `GetMarkers` (`/api/property/map/GetMarkers`,
  vue "Carte") s'est avéré être un cul-de-sac : cet endpoint ne retourne
  que des **clusters** de propriétés (position + nombre regroupé), pas
  d'annonces individuelles — gardé en historique dans le code/commits
  mais plus utilisé.
- La réponse de `GetInscriptions` contient le HTML pré-rendu des fiches
  (`d.Result.html`) plutôt que des champs JSON — `monitor.py` le parse
  avec BeautifulSoup (`parse_centris_listing_cards()`) pour en extraire
  id, prix, adresse, url et coordonnées de chaque annonce.
- Centris est protégé par Cloudflare — `monitor.py` utilise Playwright
  pour établir une session valide avant d'appeler cet endpoint (voir
  section 1, `playwright install chromium`).
- Si Centris change son HTML ou son endpoint dans le futur, refaire la
  capture avec la marche à suivre générale ci-dessus (viser la vue
  **Galerie**, pas **Carte**) et ajuster `search_centris_listings()`
  / `parse_centris_listing_cards()` en conséquence.

### Spécifique à DuProprio

- DuProprio utilise souvent une API de type GraphQL (une seule requête
  **POST**, avec un champ `query` contenant la requête GraphQL et un champ
  `variables` avec les filtres) — repérable dans le panneau Réseau par une
  requête vers un chemin contenant `graphql`.
- Il faudrait une fonction distincte, par exemple
  `search_duproprio_waterfront_cottages()`, suivant le même principe que
  celle de Centris.

### uBee (ubee.com/carte/a-vendre) — ✅ validé le 2026-09-10

- L'endpoint utilisé est `POST https://api.ubee.ca/api/anonymous/Search/SearchProperties`,
  paginé par `?pageIndex=N` (0-indexé) en paramètre d'URL.
- Contrairement à Centris, uBee n'a **aucune protection Cloudflare/cookie** —
  `search_ubee_listings()` utilise donc un simple `requests.post()`,
  pas besoin de Playwright.
- Le filtre bord de l'eau : `complimentaryFilters.hasWaterAccess: true`. uBee
  n'a pas de catégorie "Chalet" séparée dans son interface — les chalets y
  sont classés sous "Unifamiliale" ou "Terrain", d'où
  `inscriptionTypes: ["Terrain", "Unifamiliale"]`.
- La réponse est du JSON propre (pas de HTML à parser comme pour Centris) :
  `results[].id/address/city/askPrice/latitude/longitude/citySlug/slugFr`.
- URL de fiche : `https://ubee.com/a-vendre/{citySlug}/{slugFr}` (confirmée
  sur un exemple réel).
- Si uBee change son endpoint ou son format dans le futur, refaire la
  capture avec la marche à suivre générale ci-dessus et ajuster
  `search_ubee_listings()` / `parse_ubee_listings()` en
  conséquence.

**Alternative plus simple si les API s'avèrent trop instables** :
utiliser un flux RSS de recherche sauvegardée (Centris et DuProprio en
offrent parfois) et adapter `monitor.py` pour parser du RSS au lieu de
l'API — je peux réécrire cette partie si tu préfères cette avenue.

## 3. Tester manuellement

```bash
python monitor.py
```

Chaque exécution ouvre un rapport avec toutes les annonces trouvées ce
jour-là, plus les jours précédents déjà dans `history.json` (jusqu'à
`HISTORY_DAYS`, 5 par défaut) — naviguer entre les jours avec les flèches
précédent/suivant en haut du rapport.

## 4. Automatiser avec GitHub Actions (recommandé)

Le dépôt inclut `.github/workflows/monitor.yml` : un workflow qui tourne
tous les jours (cron `0 12 * * *`, ~7-8h heure de l'Est), exécute
`monitor.py` sur un runner GitHub, committe l'`history.json` mis à jour
dans le dépôt (l'historique roulant des `HISTORY_DAYS` derniers jours) et
publie `report.html` sur **GitHub Pages** — à chaque run, le site affiche
le rapport paginé le plus à jour.

Étapes pour l'activer (une seule fois) :

1. Pousser ce dépôt sur GitHub (déjà fait si tu lis ceci depuis GitHub).
2. Dans le dépôt GitHub : **Settings → Pages → Build and deployment →
   Source**, choisir **GitHub Actions**.
3. Le workflow tourne automatiquement chaque jour, ou manuellement via
   l'onglet **Actions → Housing monitor → Run workflow**.
4. L'URL du site (visible dans Settings → Pages une fois le premier
   déploiement fait, ou dans le résumé du run sous "Déploie sur GitHub
   Pages") affiche le rapport, avec des flèches précédent/suivant pour
   naviguer entre les derniers jours.

Pas besoin de garder un ordinateur allumé ni d'installer quoi que ce soit
localement pour cette option.

## 4bis. Automatiser avec cron (Mac/Linux, en local)

```bash
crontab -e
```

Ajouter une ligne pour vérifier tous les jours à 8h :

```
0 8 * * * cd /chemin/vers/housing-monitoring && /usr/bin/python3 monitor.py >> monitor.log 2>&1
```

Sur Windows : utiliser le Planificateur de tâches avec une action qui
lance `python monitor.py` dans le dossier du projet.

Note : si le cron tourne pendant que l'ordinateur est verrouillé ou
éteint, `webbrowser.open()` peut échouer silencieusement à ouvrir un
onglet — le rapport reste quand même disponible dans `report.html` et
peut être ouvert manuellement au retour.

## 5. Ce que fait le script à chaque exécution

1. Géocode `ORIGIN_ADDRESS` (2450 Boul Laurier, Québec, QC G1V 2L1) une
   seule fois — c'est le point de départ des deux recherches.

Pour chacune des deux recherches définies dans `SEARCHES` (chalets, condos) :

2. Cherche les annonces correspondant aux critères de cette recherche
   (type de propriété, chambres, bord de l'eau...) dans un rayon large
   autour de ce point.
3. Filtre par **temps de route réel** via OSRM, pas juste à vol d'oiseau
   (≤ 150 min pour les chalets/terrains, ≤ 15 min pour les condos).

Puis, une fois les deux recherches terminées :

4. Ajoute le jour courant à `history.json` (un groupe d'annonces par
   recherche ; remplace l'entrée du jour si le script est relancé le même
   jour) et ne garde que les `HISTORY_DAYS` jours les plus récents.
5. Construit un rapport HTML autonome (`report.html`, une carte
   interactive Leaflet/OpenStreetMap partagée entre les jours ET les
   recherches — cliquer sur un point ouvre un popup avec
   adresse/prix/temps de route et un lien vers l'annonce — plus des
   fiches cliquables par jour, paginé avec des flèches précédent/suivant,
   et un bouton pour basculer entre chalets et condos) et l'ouvre dans le
   navigateur par défaut.

## Notes

- La carte du rapport charge Leaflet et les tuiles OpenStreetMap depuis
  un CDN à l'ouverture de la page — une connexion internet est donc
  nécessaire pour la voir (mais pas pour consulter les fiches en dessous).
- Le service de temps de route utilisé (OSRM, serveur public de démo)
  est gratuit mais partagé — éviter les vérifications trop fréquentes
  (1x/jour est raisonnable).
- Pour ajouter DuProprio en plus de Centris et uBee, il faudrait une
  fonction de recherche additionnelle, similaire à
  `search_centris_listings()` (voir section 2 ci-dessus).
- `report.html` est régénéré à chaque exécution — il n'est pas versionné
  dans Git (voir `.gitignore`). `history.json`, lui, l'est : c'est ce qui
  permet à `HISTORY_DAYS` jours de survivre d'une exécution à l'autre sur
  un runner GitHub Actions éphémère.
