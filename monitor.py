#!/usr/bin/env python3
"""
Moniteur immobilier à deux recherches : chalets à vendre bord de l'eau (à
2h de route ou moins de G3A2P8) et condos 3-5 chambres près du centre-ville
de Québec.

Flux, pour chacune des deux recherches (voir SEARCHES) :
  1. Détermine le point de départ (géocodé via Nominatim pour les chalets,
     coordonnées fixes pour les condos — voir SEARCHES).
  2. Interroge Centris et uBee pour les annonces correspondant aux critères
     de cette recherche (type de propriété, chambres, bord de l'eau...).
  3. Filtre les résultats par temps de route réel (via OSRM), pas juste
     à vol d'oiseau.
  4. Ajoute le jour courant à l'historique roulant (history.json, les
     HISTORY_DAYS derniers jours — pas de notion d'annonce "déjà vue", une
     annonce reste visible tant qu'elle est encore trouvée par la
     recherche), un groupe d'annonces par recherche.
  5. Génère un rapport HTML autonome (carte interactive avec points
     cliquables + fiches, paginé par jour, avec un bouton pour basculer
     entre les deux recherches) et l'ouvre dans le navigateur par défaut.

Nécessite : pip install -r requirements.txt puis, une seule fois,
"playwright install chromium" (utilisé pour obtenir une session Centris
valide face à Cloudflare — voir search_centris_listings()).
Aucune configuration/secret requis — ajuster les constantes ci-dessous
au besoin (codes postaux, rayons, temps de route max, chambres).
"""

import json
import math
import os
import time
import webbrowser
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- CONFIGURATION ---
ORIGIN_POSTAL_CODE = "G3A2P8"
MAX_DRIVE_HOURS = 2.0
SEARCH_RADIUS_KM = 180  # rayon large à vol d'oiseau, filtré ensuite par temps de route réel
HISTORY_DAYS = 5  # nombre de jours conservés dans l'historique roulant du rapport

# Coordonnées de secours pour ORIGIN_POSTAL_CODE, utilisées seulement si
# Nominatim échoue à le géocoder (voir geocode_postal_code : quatre
# stratégies différentes testées en production le 2026-09-10 n'ont retourné
# aucune donnée de code postal pour le Canada sur l'instance publique).
# Saint-Augustin-de-Desmaures, QC (confirmé par le propriétaire) — à mettre
# à jour si ORIGIN_POSTAL_CODE change.
FALLBACK_ORIGIN_COORDS = (46.75588, -71.37319)

# Recherche condos centre-ville de Québec : origine codée en dur (Grande
# Allée / colline Parlementaire, à ajuster si ce n'est pas le bon centre)
# plutôt que géocodée — un nom de lieu se géocoderait probablement bien via
# Nominatim (contrairement aux codes postaux, voir geocode_postal_code),
# mais une coordonnée fixe évite tout appel réseau superflu pour un point
# qui ne change jamais.
CONDO_ORIGIN_COORDS = (46.8092, -71.2145)
CONDO_SEARCH_RADIUS_KM = 8
CONDO_MAX_DRIVE_HOURS = 0.25  # 15 minutes — zone urbaine, pas besoin d'un grand rayon
CONDO_MIN_BEDROOMS = 3
CONDO_MAX_BEDROOMS = 5

# Les deux recherches du rapport. "chalets" garde le comportement d'origine
# (géocodage de ORIGIN_POSTAL_CODE) ; "condos" utilise une coordonnée fixe
# (origin_postal_code=None). Voir search_centris_listings() /
# search_ubee_listings() pour comment category sélectionne les filtres.
SEARCHES = {
    "chalets": {
        "label": "🏡 Chalets bord de l'eau",
        "criteria": f"{MAX_DRIVE_HOURS:.0f}h de route max de {ORIGIN_POSTAL_CODE}",
        "origin_postal_code": ORIGIN_POSTAL_CODE,
        "origin_coords": None,
        "search_radius_km": SEARCH_RADIUS_KM,
        "max_drive_hours": MAX_DRIVE_HOURS,
        "map_zoom": 9,
    },
    "condos": {
        "label": "🏢 Condos centre-ville",
        "criteria": f"{CONDO_SEARCH_RADIUS_KM} km du centre-ville de Québec, {CONDO_MIN_BEDROOMS}-{CONDO_MAX_BEDROOMS} chambres",
        "origin_postal_code": None,
        "origin_coords": CONDO_ORIGIN_COORDS,
        "search_radius_km": CONDO_SEARCH_RADIUS_KM,
        "max_drive_hours": CONDO_MAX_DRIVE_HOURS,
        "map_zoom": 13,
    },
}

# Valeurs de filtre par recherche. Celles de "chalets" sont confirmées par
# capture réseau (voir README). Celles de "condos" sont ma meilleure
# estimation, PAS confirmées par capture — search_centris_listings()/
# search_ubee_listings() impriment les décomptes à chaque étape pour que ce
# soit visible dans les logs si ces valeurs ne matchent rien côté serveur.
CENTRIS_PROPERTY_TYPES = {
    "chalets": ["Chalet", "ResidentialLot"],
    "condos": ["Condominium"],  # non confirmé
}
UBEE_INSCRIPTION_TYPES = {
    "chalets": ["Terrain", "Unifamiliale"],
    "condos": ["Copropriete"],  # non confirmé
}

BASE_DIR = Path(__file__).parent
HISTORY_FILE = BASE_DIR / "history.json"
REPORT_FILE = BASE_DIR / "report.html"


def geocode_postal_code(postal_code: str) -> tuple[float, float]:
    """
    Convertit un code postal canadien en (lat, lon) via Nominatim.

    Nominatim n'indexe presque jamais les codes postaux canadiens complets
    à 6 caractères comme entités propres (Canada Post ne publie pas de
    limites précises par LDU), mais indexe généralement les secteurs de tri
    (FSA, les 3 premiers caractères) comme polygones dans OSM.

    Historique des essais (tous en production, le 2026-09-10) :
    1. Recherche structurée `postalcode=` avec le code complet seul :
       échoue (`ValueError: Impossible de géocoder G3A2P8`) — attendu, OSM
       n'a pas de polygone au niveau LDU.
    2. Recherche libre `q="{code}, Canada"` : ne lève plus d'erreur, mais
       résout vers un mauvais endroit — il existe un lieu-dit nommé
       littéralement "Canada" à Pike County, Kentucky (États-Unis), et
       Nominatim fait correspondre CE lieu plutôt que d'interpréter
       "Canada" comme le pays (`"G3A 2P8, Canada" -> "Canada, Pike County,
       Kentucky, 41519, United States"`), donnant une origine à ~1900 km au
       sud sans la moindre erreur.
    3. Recherche libre `q="{code}"` + `countrycodes=ca` (sans le mot
       "Canada" en texte) : retourne 0 résultat pour le code complet ET
       pour le FSA seul — sans "Canada" comme ancre textuelle, le
       tokenizer de Nominatim ne trouve aucune correspondance du tout pour
       un code postal nu, même restreint géographiquement.
    4. Recherche **structurée** dédiée aux codes postaux (`postalcode=` +
       `country=`) : retourne aussi 0 résultat, pour le code complet ET le
       FSA seul. L'instance publique de Nominatim semble simplement n'avoir
       aucune donnée de code postal indexée pour le Canada, quelle que soit
       la méthode de recherche.

    Après ces quatre échecs, on renonce à géocoder le code postal lui-même
    et on retombe sur FALLBACK_ORIGIN_COORDS (coordonnées approximatives
    codées en dur) plutôt que de faire planter tout le pipeline — un script
    personnel à origine fixe n'a pas besoin de dépendre d'un géocodage fiable
    à chaque exécution.
    """
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "housing-monitoring-personnel/1.0"}

    for code in (f"{postal_code[:3]} {postal_code[3:]}", postal_code[:3]):
        resp = requests.get(
            url,
            params={"postalcode": code, "country": "Canada", "format": "json"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            print(f"[Géocodage] postalcode=\"{code}\" -> {data[0].get('display_name')}")
            return float(data[0]["lat"]), float(data[0]["lon"])
        time.sleep(1)  # respecter la politique d'usage de Nominatim entre deux essais

    print(
        f"[Géocodage] échec de toutes les stratégies pour {postal_code} — "
        f"utilisation des coordonnées de secours {FALLBACK_ORIGIN_COORDS}."
    )
    return FALLBACK_ORIGIN_COORDS


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance à vol d'oiseau (km) entre deux points (lat, lon)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def driving_time_minutes(origin: tuple[float, float], dest: tuple[float, float]) -> float:
    """
    Temps de route via OSRM (serveur public de démonstration).
    Usage personnel léger uniquement — ne pas appeler en boucle serrée.

    En cas d'échec (timeout, erreur HTTP, réponse "code" != "Ok"), affiche
    la raison sur stderr plutôt que de l'avaler silencieusement — un run du
    2026-09-10 sur GitHub Actions a trouvé zéro candidat sur ~2000 annonces
    sans que rien dans les logs n'explique pourquoi, ce qui a rendu le
    diagnostic impossible après coup.
    """
    url = (
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
    )
    try:
        resp = requests.get(url, params={"overview": "false"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok":
            print(f"[OSRM] réponse non-Ok pour {dest} : {data.get('code')} — {data.get('message', '')}")
            return float("inf")
        return data["routes"][0]["duration"] / 60
    except requests.RequestException as exc:
        print(f"[OSRM] échec de requête pour {dest} : {exc}")
        return float("inf")


def parse_centris_listing_cards(html: str) -> list[dict]:
    """
    Parse le HTML pré-rendu retourné par GetInscriptions (vue "Thumbnail")
    en une liste de fiches structurées.

    Confirmé le 2026-09-10 sur un échantillon réel : chaque annonce est un
    bloc `.property-thumbnail-item` contenant, entre autres :
      - id/MLS : <meta itemprop="sku" content="...">
      - url : href du <a class="property-thumbnail-summary-link">
      - prix (nombre brut, sans formatage) : <meta itemprop="price" content="...">
      - adresse : les <div> à l'intérieur de <div class="address">
        (rue puis ville, la rue est parfois absente pour un terrain)
      - lat/lon : attributs data-lat/data-lng du <span class="ll-match-score">
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for card in soup.select(".property-thumbnail-item"):
        link = card.select_one("a.property-thumbnail-summary-link")
        sku_meta = card.select_one('meta[itemprop="sku"]')
        price_meta = card.select_one('meta[itemprop="price"]')
        score_span = card.select_one(".ll-match-score")
        if link is None or sku_meta is None or score_span is None:
            continue

        address_div = card.select_one(".address")
        address_lines = [d.get_text(strip=True) for d in address_div.find_all("div")] if address_div else []

        listings.append({
            "id": sku_meta["content"],
            "url": "https://www.centris.ca" + link["href"],
            "price": int(price_meta["content"]) if price_meta and price_meta.get("content") else None,
            "address": ", ".join(address_lines) or "Voir l'annonce",
            "lat": float(score_span["data-lat"]),
            "lon": float(score_span["data-lng"]),
        })
    return listings


def search_centris_listings(origin: tuple[float, float], radius_km: float, category: str) -> list[dict]:
    """
    Interroge l'endpoint interne de recherche Centris (GetInscriptions),
    capturé et validé par inspection réseau (F12) le 2026-09-10 sur une
    recherche Chalet + Terrain + Bord de l'eau + Villégiature.

    Centris est protégé par Cloudflare : l'appel exige des cookies de
    session (dont `cf_clearance`) obtenus en résolvant un défi JavaScript.
    On utilise donc Playwright pour ouvrir une vraie page Centris une
    première fois (ce qui établit ces cookies dans le contexte du
    navigateur), puis on référence les requêtes POST à travers ce même
    contexte — elles envoient automatiquement les bons cookies.

    `category` sélectionne les filtres via CENTRIS_PROPERTY_TYPES — pour
    "condos", la valeur "Condominium" n'est PAS confirmée par capture
    réseau (contrairement au reste de cette fonction) ; si le run trouve
    0 annonce pour cette recherche, c'est le premier endroit à vérifier
    (capturer une vraie recherche Condo sur centris.ca avec la marche à
    suivre du README).

    Pas de filtre "nombre de chambres" ici, contrairement à uBee
    (CONDO_MIN_BEDROOMS/CONDO_MAX_BEDROOMS) : le fieldId Centris pour ça
    n'est pas connu, et deviner sa forme (structure de range vs valeur
    unique) risquerait de faire échouer toute la requête plutôt que de
    simplement retourner 0 résultat — donc pour l'instant, les condos
    Centris ne sont PAS filtrées par nombre de chambres (à ajouter une
    fois le bon fieldId confirmé par capture).

    Historique :
    - Un premier endpoint tenté (GetMarkers, /api/property/map/GetMarkers)
      ne retourne que des clusters de positions pour dessiner la carte, pas
      des annonces individuelles.
    - GetInscriptions (/Property/GetInscriptions), lui, retourne (dans
      `d.Result.html`) le HTML pré-rendu des fiches de la vue "Galerie",
      paginées par 20 (`pageSize`/`page`) plutôt que par zone géographique
      — la requête capturée n'a pas de rayon/bounding box, juste
      `"region": "Quebec"` (aucune ville précisée dans la barre de
      recherche du site lors de la capture). `d.Result.count` donne le
      nombre total d'annonces correspondant aux filtres. On parcourt donc
      toutes les pages pour "Quebec" et c'est le filtre de temps de route
      (voir main()) qui réduit ensuite aux propriétés dans les critères de
      la recherche — radius_km n'est donc pas utilisé ici pour l'instant
      (paramètre conservé pour usage futur si une restriction géographique
      côté Centris est ajoutée).
    """
    from playwright.sync_api import sync_playwright

    url = "https://www.centris.ca/Property/GetInscriptions"
    fields_values = [
        {"fieldId": "PropertyType", "value": v, "fieldConditionId": "", "valueConditionId": "IsResidential"}
        for v in CENTRIS_PROPERTY_TYPES[category]
    ]
    if category == "chalets":
        fields_values += [
            {"fieldId": "NearbyWater", "value": "Waterfront", "fieldConditionId": "IsResidential", "valueConditionId": ""},
            {"fieldId": "Resort", "value": "Resort", "fieldConditionId": "IsResort", "valueConditionId": ""},
        ]
    fields_values += [
        {"fieldId": "Category", "value": "Residential", "fieldConditionId": "", "valueConditionId": ""},
        {"fieldId": "SellingType", "value": "Sale", "fieldConditionId": "", "valueConditionId": ""},
        {"fieldId": "LivingArea", "value": "SquareFeet", "fieldConditionId": "IsResidentialNotLot", "valueConditionId": ""},
        {"fieldId": "LandArea", "value": "SquareFeet", "fieldConditionId": "IsLandArea", "valueConditionId": ""},
        {"fieldId": "SalePrice", "value": 0, "fieldConditionId": "ForSale", "valueConditionId": ""},
        {"fieldId": "SalePrice", "value": 999999999999, "fieldConditionId": "ForSale", "valueConditionId": ""},
    ]
    query = {
        "SearchName": "",
        "UseGeographyShapes": 0,
        "Filters": [],
        "FieldsValues": fields_values,
        "BrokerCode": None,
        "OfficeKey": None,
    }

    headers = {
        "content-type": "application/json; charset=UTF-8",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
        "referer": "https://www.centris.ca/fr/propriete~a-vendre",
    }

    page_size = 20
    max_pages = 60  # garde-fou (60 * 20 = 1200 annonces max)
    listings: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                )
            )
            # Établit la session (cookies Cloudflare inclus) avant les appels.
            browser_page = context.new_page()
            browser_page.goto("https://www.centris.ca/fr", wait_until="networkidle", timeout=30000)

            total_count = None
            for page_number in range(1, max_pages + 1):
                payload = {
                    "mode": "Result",
                    "searchView": "Thumbnail",
                    "sortSeed": 1,
                    "sort": "None",
                    "pageSize": page_size,
                    "page": page_number,
                    "query": query,
                    "region": "Quebec",
                }
                resp = context.request.post(url, data=json.dumps(payload), headers=headers, timeout=15000)
                if not resp.ok:
                    raise RuntimeError(
                        f"Centris a refusé la requête GetInscriptions ({resp.status}) — "
                        "cookies de session ou défi Cloudflare probablement invalides."
                    )
                result = resp.json()["d"]["Result"]
                total_count = result["count"]
                page_listings = parse_centris_listing_cards(result["html"])
                if not page_listings:
                    break
                listings.extend(page_listings)
                if len(listings) >= total_count:
                    break
                time.sleep(1)  # ménager Centris entre les pages
        finally:
            browser.close()

    return listings


def parse_ubee_listings(results: list[dict]) -> list[dict]:
    """Transforme les entrées brutes de SearchProperties en fiches structurées."""
    listings = []
    for r in results:
        listings.append({
            "id": r["id"],
            "url": f"https://ubee.com/a-vendre/{r['citySlug']}/{r['slugFr']}",
            "price": r.get("askPrice"),
            "address": f"{r['address']}, {r['city']}",
            "lat": r["latitude"],
            "lon": r["longitude"],
        })
    return listings


def search_ubee_listings(origin: tuple[float, float], radius_km: float, category: str) -> list[dict]:
    """
    Interroge l'endpoint interne de recherche uBee (SearchProperties),
    capturé et validé par inspection réseau (F12) le 2026-09-10 avec le
    filtre "bord de l'eau" actif sur ubee.com/carte/a-vendre.

    Contrairement à Centris, uBee n'a aucune protection Cloudflare/cookie —
    un simple requests.post() suffit, pas besoin de Playwright.

    uBee n'a pas de catégorie "Chalet" distincte dans son interface : les
    chalets y sont classés sous "Unifamiliale" (résidence uni-familiale) ou
    "Terrain", d'où UBEE_INSCRIPTION_TYPES["chalets"] = ["Terrain",
    "Unifamiliale"]. La valeur pour "condos" ("Copropriete") n'est PAS
    confirmée par capture réseau — si le run trouve 0 annonce pour cette
    recherche, c'est le premier endroit à vérifier.

    Pour "condos", `minBedrooms`/`maxBedrooms` sont fixés à
    CONDO_MIN_BEDROOMS/CONDO_MAX_BEDROOMS — `minBedrooms` est un champ
    confirmé (déjà présent, à 0, dans la requête chalets d'origine), mais
    `maxBedrooms` est une supposition non confirmée (uBee ignore
    probablement un champ JSON qu'il ne reconnaît pas plutôt que de
    rejeter toute la requête, donc le risque d'échec total est faible même
    si le nom est faux — contrairement au fieldId Centris, voir
    search_centris_listings()).

    Pagination par ?pageIndex=N (0-indexé) en paramètre d'URL ; le corps de
    la requête reste identique à chaque page (mapBoundaries fixe = bounding
    box couvrant tout le Québec habité, comme radius_km n'est pas utilisé
    par cet endpoint — le filtre de temps de route réel, voir main(),
    réduit ensuite aux annonces dans les critères de la recherche).

    URL de fiche : https://ubee.com/a-vendre/{citySlug}/{slugFr}, confirmée
    le 2026-09-10 sur un exemple réel (ex. .../a-vendre/chertsey/terrain-...).
    """
    url = "https://api.ubee.ca/api/anonymous/Search/SearchProperties"
    headers = {
        "accept": "text/json",
        "content-type": "application/*+json",
        "origin": "https://ubee.com",
        "referer": "https://ubee.com/",
    }
    map_boundaries = {
        "type": "Polygon",
        "coordinates": [[
            [-79.02226422505078, 41.32405887412813],
            [-62.97773577494998, 41.32405887412813],
            [-62.97773577494998, 52.397461900458296],
            [-79.02226422505078, 52.397461900458296],
            [-79.02226422505078, 41.32405887412813],
        ]],
    }
    payload = {
        "minBathrooms": 0,
        "minBedrooms": 0,
        "toBuild": False,
        "onlineSinceInDays": 0,
        "sortBy": "DateDescending",
        "mapBoundaries": json.dumps(map_boundaries),
        "listingType": "Seller",
        "complimentaryFilters": {
            "hasCitySewerSystem": False,
            "hasCityWaterSupply": False,
            "hasSwimmingPool": False,
            "hasWaterAccess": category == "chalets",
            "isAccessibleReducedMobility": False,
        },
        "isResidential": True,
        "minLandSurfaceInMeters": None,
        "maxLandSurfaceInMeters": None,
        "minLivingSurfaceInMeters": None,
        "maxLivingSurfaceInMeters": None,
        "hasGarage": False,
        "inscriptionTypes": UBEE_INSCRIPTION_TYPES[category],
    }
    if category == "condos":
        payload["minBedrooms"] = CONDO_MIN_BEDROOMS
        payload["maxBedrooms"] = CONDO_MAX_BEDROOMS  # non confirmé, voir docstring

    max_pages = 100  # garde-fou
    listings: list[dict] = []
    page_index = 0
    while page_index < max_pages:
        resp = requests.post(url, params={"pageIndex": page_index}, json=payload, headers=headers, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"uBee a refusé la requête SearchProperties ({resp.status_code})")
        data = resp.json()
        page_results = data.get("results", [])
        if not page_results:
            break
        listings.extend(parse_ubee_listings(page_results))
        total_count = data.get("totalCount", 0)
        if len(listings) >= total_count:
            break
        page_index += 1
        time.sleep(1)  # ménager uBee entre les pages

    return listings


def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False))


FRENCH_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FRENCH_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def format_date_fr(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return f"{FRENCH_WEEKDAYS[d.weekday()]} {d.day} {FRENCH_MONTHS[d.month - 1]} {d.year}"


def build_html_report(origins: dict[str, tuple[float, float]], history: list[dict], generated_at: str) -> Path:
    """
    Construit un rapport HTML autonome avec un historique roulant des
    HISTORY_DAYS derniers jours, paginé (une page par jour, navigable avec
    des flèches précédent/suivant côté client) — et un bouton pour basculer
    entre les recherches définies dans SEARCHES (chalets/condos).

    Pas de notion d'annonce "déjà vue" : chaque jour montre toutes les
    annonces trouvées ce jour-là dans les critères de chaque recherche,
    qu'elles aient déjà figuré dans un rapport précédent ou non — voir
    history.json pour le stockage (chaque jour y a un groupe d'annonces
    par recherche, sous "categories").

    La carte est une carte interactive Leaflet/OpenStreetMap (chargée par
    CDN) plutôt qu'une image statique : les points sont cliquables et
    ouvrent un popup (adresse, prix, temps de route, lien vers l'annonce).
    Une seule instance de carte est partagée entre les jours ET les
    recherches — son centre/zoom et ses marqueurs sont remplacés en JS au
    changement de jour ou de recherche plutôt que de générer une image par
    combinaison jour/recherche.
    """
    # Page la plus récente en premier (history est trié du plus vieux au plus récent).
    days = list(reversed(history))
    today_iso = date.today().isoformat()
    category_names = list(SEARCHES.keys())
    first_category = category_names[0]

    blocks_html = ""
    day_labels = []
    days_json_data = {name: [] for name in category_names}

    for i, day in enumerate(days):
        label = format_date_fr(day["date"])
        if day["date"] == today_iso:
            label = f"Aujourd'hui — {label}"
        day_labels.append(label)

        day_categories = day.get("categories", {})
        for name in category_names:
            listings = day_categories.get(name, [])
            days_json_data[name].append([
                {
                    "lat": l["lat"],
                    "lon": l["lon"],
                    "address": l.get("address", "Voir l'annonce"),
                    "price": l.get("price"),
                    "drive_minutes": l["drive_minutes"],
                    "url": l["url"],
                }
                for l in listings
            ])

            if listings:
                cards = ""
                for l in listings:
                    address = l.get("address", "Voir l'annonce")
                    price = l.get("price")
                    price_display = f"{price:,}".replace(",", " ") + " $" if price else "Prix non précisé"
                    cards += f"""
        <a class="card" href="{l['url']}" target="_blank" rel="noopener">
          <div class="card-body">
            <div class="card-address">{address}</div>
            <div class="card-meta">
              <span class="price">{price_display}</span>
              <span class="drive">🚗 {l['drive_minutes']:.0f} min</span>
            </div>
          </div>
        </a>"""
                body_html = f'<div class="grid">{cards}</div>'
            else:
                body_html = '<p class="empty">Aucune annonce trouvée ce jour-là.</p>'

            visible = i == 0 and name == first_category
            blocks_html += f"""
    <section class="page" data-day="{i}" data-category="{name}" {"" if visible else "hidden"}>
      {body_html}
    </section>"""

    days_json = json.dumps(days_json_data, ensure_ascii=False).replace("</", "<\\/")
    day_labels_json = json.dumps(day_labels, ensure_ascii=False).replace("</", "<\\/")
    searches_json = json.dumps(
        {
            name: {
                "label": cfg["label"],
                "criteria": cfg["criteria"],
                "origin": list(origins[name]),
                "zoom": cfg["map_zoom"],
            }
            for name, cfg in SEARCHES.items()
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")
    toggle_buttons = "".join(
        f'<button class="toggle-btn{" active" if name == first_category else ""}" '
        f'data-category="{name}">{cfg["label"]}</button>'
        for name, cfg in SEARCHES.items()
    )
    initial_subtitle = f"{SEARCHES[first_category]['criteria']} — généré le {generated_at}"

    html = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Chalets &amp; condos</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 24px 16px; background: #f5f5f4; color: #1c1917;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }}
  .wrap {{ max-width: 900px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .subtitle {{ color: #57534e; margin: 0 0 16px; font-size: 0.9rem; }}
  .toggle {{ display: flex; gap: 8px; margin-bottom: 16px; }}
  .toggle-btn {{
    font: inherit; font-size: 0.9rem; padding: 8px 14px; border-radius: 999px;
    border: 1px solid #d6d3d1; background: white; color: #57534e; cursor: pointer;
  }}
  .toggle-btn.active {{ background: #1c1917; color: white; border-color: #1c1917; }}
  .nav {{
    display: flex; align-items: center; gap: 12px; margin-bottom: 20px;
  }}
  .nav button {{
    font: inherit; font-size: 1.1rem; width: 36px; height: 36px; border-radius: 999px;
    border: 1px solid #d6d3d1; background: white; cursor: pointer;
  }}
  .nav button:disabled {{ opacity: 0.35; cursor: default; }}
  .nav .day-label {{ font-weight: 600; }}
  #map {{
    width: 100%; height: 380px; border-radius: 12px; border: 1px solid #d6d3d1;
    margin-bottom: 24px; background: #e7e5e4;
  }}
  .leaflet-popup-content {{ font-family: inherit; font-size: 0.9rem; }}
  .leaflet-popup-content a {{ color: #2563eb; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 12px; }}
  .card {{
    display: block; background: white; border: 1px solid #e7e5e4; border-radius: 10px;
    padding: 14px 16px; text-decoration: none; color: inherit; transition: box-shadow .15s, transform .15s;
  }}
  .card:hover {{ box-shadow: 0 4px 14px rgba(0,0,0,.08); transform: translateY(-1px); }}
  .card-address {{ font-weight: 600; margin-bottom: 8px; }}
  .card-meta {{ display: flex; justify-content: space-between; font-size: 0.9rem; color: #44403c; }}
  .price {{ font-weight: 600; color: #15803d; }}
  .empty {{ color: #78716c; font-style: italic; }}
  footer {{ margin-top: 24px; font-size: 0.8rem; color: #78716c; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>🏠 Chalets &amp; condos</h1>
    <p class="subtitle" id="subtitle">{initial_subtitle}</p>
    <div class="toggle">{toggle_buttons}</div>
    <div class="nav">
      <button id="prevBtn" aria-label="Jour précédent">‹</button>
      <span class="day-label" id="dayLabel"></span>
      <button id="nextBtn" aria-label="Jour suivant">›</button>
    </div>
    <div id="map"></div>
    {blocks_html}
    <footer>housing-monitoring · historique roulant des {HISTORY_DAYS} derniers jours</footer>
  </div>
  <script>
    const SEARCHES = {searches_json};  // {{chalets: {{label, criteria, origin:[lat,lon], zoom}}, condos: {{...}}}}
    const DAYS = {days_json};          // {{chalets: [[listing,...], ...jours], condos: [[...], ...]}}
    const DAY_LABELS = {day_labels_json}; // libellé de date par jour (indépendant de la recherche)
    const categories = Object.keys(SEARCHES);

    const pages = Array.from(document.querySelectorAll('.page'));
    const dayLabelEl = document.getElementById('dayLabel');
    const subtitleEl = document.getElementById('subtitle');
    const prevBtn = document.getElementById('prevBtn');
    const nextBtn = document.getElementById('nextBtn');
    const toggleBtns = Array.from(document.querySelectorAll('.toggle-btn'));

    let currentDay = 0; // 0 = le plus récent
    let currentCategory = categories[0];

    // Si Leaflet n'a pas pu se charger (CDN indisponible, bloqueur de
    // contenu, etc.), afficher un message plutôt qu'un rectangle vide et
    // silencieux — les fiches en dessous restent consultables sans la carte.
    let map = null;
    let markersLayer = null;
    let originMarker = null;
    if (typeof L === 'undefined') {{
      document.getElementById('map').textContent =
        "Carte indisponible (connexion internet requise pour charger Leaflet/OpenStreetMap).";
    }} else {{
      const initial = SEARCHES[currentCategory];
      map = L.map('map').setView(initial.origin, initial.zoom);
      L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        maxZoom: 18,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }}).addTo(map);
      originMarker = L.circleMarker(initial.origin, {{radius: 8, color: '#2563eb', fillColor: '#2563eb', fillOpacity: 1}})
        .addTo(map)
        .bindTooltip('Point de départ');
      markersLayer = L.layerGroup().addTo(map);
    }}

    function popupContent(l) {{
      const priceText = l.price ? l.price.toLocaleString('fr-CA') + ' $' : 'Prix non précisé';
      const div = document.createElement('div');
      const addr = document.createElement('div');
      addr.style.fontWeight = '600';
      addr.textContent = l.address;
      div.appendChild(addr);
      const meta = document.createElement('div');
      meta.textContent = `${{priceText}} · 🚗 ${{Math.round(l.drive_minutes)}} min`;
      div.appendChild(meta);
      const link = document.createElement('a');
      link.href = l.url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = "Voir l'annonce ↗";
      div.appendChild(link);
      return div;
    }}

    function renderMarkers() {{
      if (!markersLayer) return;
      markersLayer.clearLayers();
      for (const l of DAYS[currentCategory][currentDay]) {{
        L.circleMarker([l.lat, l.lon], {{radius: 7, color: '#dc2626', fillColor: '#dc2626', fillOpacity: 0.85}})
          .addTo(markersLayer)
          .bindPopup(popupContent(l));
      }}
    }}

    function render() {{
      pages.forEach(p => {{
        p.hidden = !(Number(p.dataset.day) === currentDay && p.dataset.category === currentCategory);
      }});
      const count = DAYS[currentCategory][currentDay].length;
      dayLabelEl.textContent = `${{DAY_LABELS[currentDay]}} — ${{count}} annonce(s) (${{currentDay + 1}}/${{DAY_LABELS.length}})`;
      prevBtn.disabled = currentDay >= DAY_LABELS.length - 1; // précédent = jour plus vieux
      nextBtn.disabled = currentDay <= 0; // suivant = jour plus récent
      toggleBtns.forEach(b => b.classList.toggle('active', b.dataset.category === currentCategory));
      subtitleEl.textContent = `${{SEARCHES[currentCategory].criteria}} — généré le {generated_at}`;
      if (map) {{
        const cfg = SEARCHES[currentCategory];
        map.setView(cfg.origin, cfg.zoom);
        originMarker.setLatLng(cfg.origin);
      }}
      renderMarkers();
    }}
    prevBtn.addEventListener('click', () => {{ if (currentDay < DAY_LABELS.length - 1) {{ currentDay++; render(); }} }});
    nextBtn.addEventListener('click', () => {{ if (currentDay > 0) {{ currentDay--; render(); }} }});
    toggleBtns.forEach(b => b.addEventListener('click', () => {{ currentCategory = b.dataset.category; render(); }}));
    render();
  </script>
</body>
</html>"""

    REPORT_FILE.write_text(html, encoding="utf-8")
    return REPORT_FILE


def run_search(name: str, config: dict) -> tuple[tuple[float, float], list[dict]]:
    """
    Exécute une recherche complète (Centris + uBee + filtre temps de route)
    pour une entrée de SEARCHES, et retourne (origine résolue, candidats).
    """
    if config["origin_postal_code"]:
        origin = geocode_postal_code(config["origin_postal_code"])
    else:
        origin = config["origin_coords"]
    print(f"[{name}] Origine : {origin}.")

    radius_km = config["search_radius_km"]
    max_drive_hours = config["max_drive_hours"]

    raw_listings = search_centris_listings(origin, radius_km, category=name)
    raw_listings += search_ubee_listings(origin, radius_km, category=name)
    print(f"[{name}] {len(raw_listings)} annonce(s) brute(s) trouvée(s) (Centris + uBee).")
    for l in raw_listings[:5]:
        d = haversine_km(origin, (l["lat"], l["lon"]))
        print(f"[{name}]   échantillon : {l.get('address')} — ({l['lat']}, {l['lon']}) — {d:.0f} km à vol d'oiseau")
    if raw_listings:
        distances = [haversine_km(origin, (l["lat"], l["lon"])) for l in raw_listings]
        print(f"[{name}] Distance à vol d'oiseau min={min(distances):.0f} km, max={max(distances):.0f} km.")

    # Pré-filtre à vol d'oiseau avant d'appeler OSRM : la route est presque
    # toujours plus longue que la ligne droite, donc ce filtre ne peut pas
    # exclure de vrai candidat, mais il évite des centaines d'appels OSRM
    # inutiles — important vu qu'OSRM est un serveur public de démo, sensible
    # au rate-limiting sur de gros volumes séquentiels.
    nearby = [l for l in raw_listings if haversine_km(origin, (l["lat"], l["lon"])) <= radius_km]
    print(f"[{name}] {len(nearby)} annonce(s) dans le rayon de {radius_km} km à vol d'oiseau.")

    candidates = []
    for listing in nearby:
        dest = (listing["lat"], listing["lon"])
        minutes = driving_time_minutes(origin, dest)
        time.sleep(1)  # ménager le serveur OSRM public
        if minutes <= max_drive_hours * 60:
            listing["drive_minutes"] = minutes
            candidates.append(listing)
    print(f"[{name}] {len(candidates)} annonce(s) à {max_drive_hours * 60:.0f} min de route ou moins.")

    return origin, candidates


def main() -> None:
    origins: dict[str, tuple[float, float]] = {}
    categories: dict[str, list[dict]] = {}
    for name, config in SEARCHES.items():
        origin, candidates = run_search(name, config)
        origins[name] = origin
        categories[name] = candidates

    today_iso = date.today().isoformat()
    # "categories" in day exclut les entrées de l'ancien format (avant
    # l'ajout des recherches multiples), qui n'ont plus la bonne forme.
    history = [
        day for day in load_history() if day["date"] != today_iso and "categories" in day
    ]  # remplace un run précédent du jour
    history.append({"date": today_iso, "categories": categories})
    history = history[-HISTORY_DAYS:]
    save_history(history)

    report_path = build_html_report(origins, history, f"{datetime.now():%Y-%m-%d %H:%M}")
    if not os.environ.get("CI"):
        # Pas de navigateur à ouvrir sur un runner CI (ex. GitHub Actions) —
        # le rapport y est plutôt publié via GitHub Pages (voir workflow).
        webbrowser.open(f"file://{report_path.resolve()}")
    total = sum(len(c) for c in categories.values())
    print(f"Rapport généré avec {len(history)} jour(s) d'historique, {total} annonce(s) aujourd'hui : {report_path}")


if __name__ == "__main__":
    main()
