#!/usr/bin/env python3
"""
Moniteur immobilier à deux recherches, partant du même point de départ
(ORIGIN_ADDRESS) : chalets/terrains à vendre bord de l'eau (150 min de
route ou moins) et condos 3-5 chambres près du centre-ville (15 min ou
moins).

Flux :
  1. Géocode ORIGIN_ADDRESS une seule fois via Nominatim (partagé par les
     deux recherches).
  2. Pour chacune des deux recherches (voir SEARCHES) : interroge Centris
     et uBee pour les annonces correspondant à ses critères (type de
     propriété, chambres, bord de l'eau...), puis filtre par temps de
     route réel (via OSRM), pas juste à vol d'oiseau.
  3. Ajoute le jour courant à l'historique roulant (history.json, les
     HISTORY_DAYS derniers jours — pas de notion d'annonce "déjà vue", une
     annonce reste visible tant qu'elle est encore trouvée par la
     recherche), un groupe d'annonces par recherche.
  4. Génère un rapport HTML autonome (carte interactive avec points
     cliquables + fiches, paginé par jour, avec un bouton pour basculer
     entre les deux recherches) et l'ouvre dans le navigateur par défaut.

Nécessite : pip install -r requirements.txt puis, une seule fois,
"playwright install chromium" (utilisé pour obtenir une session Centris
valide face à Cloudflare — voir search_centris_listings()).
Aucune configuration/secret requis — ajuster les constantes ci-dessous
au besoin (adresse de départ, rayons, temps de route max, chambres).
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
# Point de départ partagé par les deux recherches (chalets et condos).
ORIGIN_ADDRESS = "2450 Boul Laurier, Québec, QC G1V 2L1"

# Coordonnées de secours pour ORIGIN_ADDRESS, utilisées seulement si
# Nominatim échoue à la géocoder (voir geocode_address). Place
# Sainte-Foy, 2450 Boulevard Laurier — coordonnées confirmées par le
# propriétaire (Apple Maps), pas une estimation.
ORIGIN_FALLBACK_COORDS = (46.773389, -71.27876)

HISTORY_DAYS = 5  # nombre de jours conservés dans l'historique roulant du rapport

MAX_DRIVE_HOURS = 2.5  # 150 minutes — chalets et terrains
# Rayon à vol d'oiseau pré-filtré avant OSRM (voir run_search) : doit rester
# assez large pour ne jamais exclure une annonce à MAX_DRIVE_HOURS ou moins
# (la route est toujours plus longue que la ligne droite). 250 km couvre
# large pour 150 min même en roulant à 100 km/h en ligne droite tout du long.
SEARCH_RADIUS_KM = 250

CONDO_MAX_DRIVE_HOURS = 0.25  # 15 minutes — condos
CONDO_SEARCH_RADIUS_KM = 8  # zone urbaine, pas besoin d'un grand rayon
CONDO_MIN_BEDROOMS = 3
# CONDO_MAX_BEDROOMS n'est actuellement PAS envoyé à uBee (voir
# search_ubee_listings) : un champ maxBedrooms devinée a provoqué un 400
# de leur API en production le 2026-09-20. Utilisée pour l'instant
# seulement dans le texte de critères affiché dans le rapport.
CONDO_MAX_BEDROOMS = 5
CONDO_MAX_PRICE = 750_000

# Les deux recherches du rapport, toutes deux à partir d'ORIGIN_ADDRESS
# (géocodée une seule fois dans main()). Voir search_centris_listings() /
# search_ubee_listings() pour comment category sélectionne les filtres.
# "max_price" est optionnel (None = pas de plafond) ; voir run_search().
SEARCHES = {
    "chalets": {
        "label": "🏡 Chalets bord de l'eau",
        "criteria": f"{MAX_DRIVE_HOURS * 60:.0f} min de route max de {ORIGIN_ADDRESS}",
        "search_radius_km": SEARCH_RADIUS_KM,
        "max_drive_hours": MAX_DRIVE_HOURS,
        "max_price": None,
        "min_bedrooms": None,
        "map_zoom": 9,
    },
    "condos": {
        "label": "🏢 Condos centre-ville",
        "criteria": (
            f"{CONDO_MAX_DRIVE_HOURS * 60:.0f} min de route max de {ORIGIN_ADDRESS}, "
            f"{CONDO_MIN_BEDROOMS}-{CONDO_MAX_BEDROOMS} chambres, "
            + f"{CONDO_MAX_PRICE:,}".replace(",", " ")
            + " $ max"
        ),
        "search_radius_km": CONDO_SEARCH_RADIUS_KM,
        "max_drive_hours": CONDO_MAX_DRIVE_HOURS,
        "max_price": CONDO_MAX_PRICE,
        "min_bedrooms": CONDO_MIN_BEDROOMS,
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
    "condos": ["SellCondo"],  # confirmé par capture réseau (Centris, 2026-09-20)
}
UBEE_INSCRIPTION_TYPES = {
    "chalets": ["Terrain", "Unifamiliale"],
    "condos": ["Condo"],  # confirmé par capture réseau (uBee, 2026-09-20)
}

BASE_DIR = Path(__file__).parent
HISTORY_FILE = BASE_DIR / "history.json"
REPORT_FILE = BASE_DIR / "report.html"


def geocode_address(address: str) -> tuple[float, float]:
    """
    Géocode une adresse complète (numéro + rue + ville, pas un code postal
    isolé) via Nominatim, en recherche libre restreinte au Canada par le
    paramètre structuré `countrycodes=ca` plutôt qu'en ajoutant "Canada" en
    texte libre dans la requête.

    Ce dernier point compte : une tentative précédente de géocodage (pour
    un code postal seul, voir l'historique de cette fonction dans git log)
    a démontré en production le 2026-09-10 qu'ajouter ", Canada" en texte
    peut faire correspondre un lieu-dit non pertinent nommé littéralement
    "Canada" (à Pike County, Kentucky, États-Unis !) plutôt que le pays —
    countrycodes=ca évite complètement cette ambiguïté.

    Une adresse complète (numéro civique + rue + ville) est un cas d'usage
    standard pour Nominatim, contrairement à un code postal canadien isolé
    (dont la recherche a échoué de quatre façons différentes en production
    avant qu'on abandonne cette approche — voir git log) : donc pas de
    repli structuré/FSA ici, juste une recherche libre directe. En cas
    d'échec quand même, retombe sur ORIGIN_FALLBACK_COORDS plutôt que de
    faire planter tout le pipeline.
    """
    url = "https://nominatim.openstreetmap.org/search"
    headers = {"User-Agent": "housing-monitoring-personnel/1.0"}

    try:
        resp = requests.get(
            url,
            params={"q": address, "format": "json", "countrycodes": "ca"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            print(f"[Géocodage] \"{address}\" -> {data[0].get('display_name')}")
            return float(data[0]["lat"]), float(data[0]["lon"])
    except requests.RequestException as exc:
        print(f"[Géocodage] échec de la requête pour \"{address}\" : {exc}")

    print(
        f"[Géocodage] échec pour \"{address}\" — "
        f"utilisation des coordonnées de secours {ORIGIN_FALLBACK_COORDS}."
    )
    return ORIGIN_FALLBACK_COORDS


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
      - chambres : texte du <div class="cac"> (chambres à coucher), confirmé
        par capture réseau le 2026-09-22 — absent pour un terrain/lot sans
        bâtiment, d'où le repli sur None plutôt qu'un KeyError.
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
        cac_div = card.select_one(".cac")
        bedrooms = None
        if cac_div is not None:
            cac_text = cac_div.get_text(strip=True)
            if cac_text.isdigit():
                bedrooms = int(cac_text)

        listings.append({
            "id": sku_meta["content"],
            "url": "https://www.centris.ca" + link["href"],
            "price": int(price_meta["content"]) if price_meta and price_meta.get("content") else None,
            "address": ", ".join(address_lines) or "Voir l'annonce",
            "lat": float(score_span["data-lat"]),
            "lon": float(score_span["data-lng"]),
            "bedrooms": bedrooms,
        })
    return listings


def search_centris_listings(
    origin: tuple[float, float], radius_km: float, category: str, max_price: int | None = None
) -> list[dict]:
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

    `max_price` (optionnel) remplace le plafond par défaut du champ
    SalePrice (déjà confirmé par capture réseau, seule sa valeur change) —
    permet de filtrer côté serveur sans deviner un nouveau fieldId.

    `category` sélectionne les filtres via CENTRIS_PROPERTY_TYPES — pour
    "condos", confirmé par capture réseau le 2026-09-20 sur une vraie
    recherche Condo sur centris.ca : PropertyType="SellCondo" avec
    valueConditionId="IsResidentialForSale" (différent de "IsResidential"
    utilisé pour les chalets), et pas de champ LandArea dans la requête
    (logique : LandArea concerne ResidentialLot, pas les condos).

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
    property_type_condition = "IsResidential" if category == "chalets" else "IsResidentialForSale"
    fields_values = [
        {"fieldId": "PropertyType", "value": v, "fieldConditionId": "", "valueConditionId": property_type_condition}
        for v in CENTRIS_PROPERTY_TYPES[category]
    ]
    if category == "chalets":
        fields_values += [
            {"fieldId": "NearbyWater", "value": "Waterfront", "fieldConditionId": "IsResidential", "valueConditionId": ""},
            {"fieldId": "Resort", "value": "Resort", "fieldConditionId": "IsResort", "valueConditionId": ""},
            {"fieldId": "LandArea", "value": "SquareFeet", "fieldConditionId": "IsLandArea", "valueConditionId": ""},
        ]
    fields_values += [
        {"fieldId": "Category", "value": "Residential", "fieldConditionId": "", "valueConditionId": ""},
        {"fieldId": "SellingType", "value": "Sale", "fieldConditionId": "", "valueConditionId": ""},
        {"fieldId": "LivingArea", "value": "SquareFeet", "fieldConditionId": "IsResidentialNotLot", "valueConditionId": ""},
        {"fieldId": "SalePrice", "value": 0, "fieldConditionId": "ForSale", "valueConditionId": ""},
        {"fieldId": "SalePrice", "value": max_price or 999999999999, "fieldConditionId": "ForSale", "valueConditionId": ""},
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
    """
    Transforme les entrées brutes de SearchProperties en fiches structurées.

    "bedrooms" vient de "nbBedrooms", confirmé par capture réseau (Response,
    pas Payload) le 2026-09-22.
    """
    listings = []
    for r in results:
        listings.append({
            "id": r["id"],
            "url": f"https://ubee.com/a-vendre/{r['citySlug']}/{r['slugFr']}",
            "price": r.get("askPrice"),
            "address": f"{r['address']}, {r['city']}",
            "lat": r["latitude"],
            "lon": r["longitude"],
            "bedrooms": r.get("nbBedrooms"),
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
    "Unifamiliale"]. La valeur pour "condos" ("Copropriete") avait d'abord
    été devinée, ce qui causait un 400 Bad Request en production (voir
    ci-dessous) ; corrigée en "Condo", confirmée par capture réseau le
    2026-09-20 sur une vraie recherche Condo sur ubee.com.

    uBee **rejette** les requêtes avec une valeur non reconnue (`400 Bad
    Request`) au lieu de simplement retourner 0 résultat — confirmé en
    production le 2026-09-20 (`RuntimeError: uBee a refusé la requête
    SearchProperties (400)`), causé par l'ancienne valeur devinée
    `inscriptionTypes: ["Copropriete"]`. Ce n'est donc pas plus sûr à
    deviner qu'un fieldId Centris (voir search_centris_listings()) —
    run_search() encaisse ce genre d'échec proprement (voir sa docstring)
    plutôt que de planter tout le script.

    Pour "condos", seul `minBedrooms` est fixé à CONDO_MIN_BEDROOMS — c'est
    un champ confirmé (déjà présent, à 0, dans la requête chalets
    d'origine). Un `maxBedrooms` avait été ajouté par supposition mais
    retiré après l'incident ci-dessus : mieux vaut confirmer d'abord que
    `inscriptionTypes` fonctionne avant de deviner un deuxième champ.

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
                    "bedrooms": l.get("bedrooms"),
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
                    price_attr = price if price is not None else ""
                    bedrooms = l.get("bedrooms")
                    bedrooms_attr = bedrooms if bedrooms is not None else ""
                    bedrooms_badge = f'<span class="bedrooms">🛏 {bedrooms}</span>' if bedrooms is not None else ""
                    cards += f"""
        <a class="card" href="{l['url']}" target="_blank" rel="noopener" data-price="{price_attr}" data-bedrooms="{bedrooms_attr}">
          <div class="card-body">
            <div class="card-address">{address}</div>
            <div class="card-meta">
              <span class="price">{price_display}</span>
              {bedrooms_badge}
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
  .card-meta {{ display: flex; justify-content: space-between; gap: 8px; font-size: 0.9rem; color: #44403c; }}
  .price {{ font-weight: 600; color: #15803d; }}
  .bedrooms {{ color: #78716c; }}
  .empty {{ color: #78716c; font-style: italic; }}
  footer {{ margin-top: 24px; font-size: 0.8rem; color: #78716c; }}
  .filters {{ display: flex; flex-direction: column; gap: 16px; margin-bottom: 20px; }}
  .range-filter-label {{ font-size: 0.9rem; color: #44403c; margin-bottom: 8px; }}
  .range-filter-slider {{ position: relative; height: 24px; }}
  .range-filter-track {{ position: absolute; top: 10px; left: 0; right: 0; height: 4px; background: #d6d3d1; border-radius: 999px; }}
  .range-filter-fill {{ position: absolute; top: 10px; height: 4px; background: #1c1917; border-radius: 999px; }}
  .range-filter-slider input[type="range"] {{
    position: absolute; top: 6px; left: 0; width: 100%; height: 12px; margin: 0;
    -webkit-appearance: none; appearance: none; background: transparent; pointer-events: none;
  }}
  .range-filter-slider input[type="range"]::-webkit-slider-runnable-track {{ height: 4px; background: transparent; }}
  .range-filter-slider input[type="range"]::-moz-range-track {{ height: 4px; background: transparent; border: none; }}
  .range-filter-slider input[type="range"]::-webkit-slider-thumb {{
    -webkit-appearance: none; pointer-events: auto; width: 16px; height: 16px; border-radius: 50%;
    background: #1c1917; border: 2px solid white; box-shadow: 0 0 0 1px #1c1917; cursor: pointer; margin-top: -6px;
  }}
  .range-filter-slider input[type="range"]::-moz-range-thumb {{
    pointer-events: auto; width: 16px; height: 16px; border-radius: 50%;
    background: #1c1917; border: 2px solid white; box-shadow: 0 0 0 1px #1c1917; cursor: pointer;
  }}
  .card.filtered-out {{ display: none; }}
</style>
</head>
<body>
  <div class="wrap">
    <h1>🏠 Chalets &amp; condos</h1>
    <p class="subtitle" id="subtitle">{initial_subtitle}</p>
    <div class="toggle">{toggle_buttons}</div>
    <div class="filters">
      <div>
        <div class="range-filter-label" id="priceLabel"></div>
        <div class="range-filter-slider">
          <div class="range-filter-track"></div>
          <div class="range-filter-fill" id="priceFill"></div>
          <input type="range" id="priceMin">
          <input type="range" id="priceMax">
        </div>
      </div>
      <div>
        <div class="range-filter-label" id="bedroomsLabel"></div>
        <div class="range-filter-slider">
          <div class="range-filter-track"></div>
          <div class="range-filter-fill" id="bedroomsFill"></div>
          <input type="range" id="bedroomsMin">
          <input type="range" id="bedroomsMax">
        </div>
      </div>
    </div>
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
    let previousCategory = null; // null force l'init des filtres au premier render()

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

    // --- Filtres à curseur double (prix, chambres) ---
    // Bornes calculées une fois par recherche à partir de toutes les
    // annonces connues (tous les jours confondus), pour que l'échelle des
    // curseurs ne bouge pas en naviguant entre les jours — recalculée
    // seulement au changement de recherche (chalets/condos ont des
    // échelles de prix et de chambres très différentes). Une annonce dont
    // la valeur filtrée est inconnue (scraping incomplet, ex. bedrooms=null
    // pour toutes les annonces uBee) n'est jamais exclue à tort — même
    // convention que le filtre CONDO_MAX_PRICE/CONDO_MIN_BEDROOMS côté
    // serveur (run_search).
    function makeRangeFilter(minInput, maxInput, fillEl, getValue) {{
      const bounds = {{}};
      for (const cat of categories) {{
        const values = DAYS[cat].flat().map(getValue).filter(v => v != null);
        bounds[cat] = values.length ? [Math.min(...values), Math.max(...values)] : [0, 0];
      }}
      const state = {{ range: [0, 0] }};
      state.reset = () => {{
        const [lo, hi] = bounds[currentCategory];
        minInput.min = maxInput.min = lo;
        minInput.max = maxInput.max = hi;
        minInput.value = lo;
        maxInput.value = hi;
        state.range = [lo, hi];
      }};
      state.updateUI = () => {{
        let lo = Number(minInput.value), hi = Number(maxInput.value);
        if (lo > hi) {{ [lo, hi] = [hi, lo]; }} // les deux curseurs ne se croisent jamais
        state.range = [lo, hi];
        const [boundLo, boundHi] = bounds[currentCategory];
        const span = (boundHi - boundLo) || 1;
        fillEl.style.left = `${{((lo - boundLo) / span) * 100}}%`;
        fillEl.style.right = `${{100 - ((hi - boundLo) / span) * 100}}%`;
        return {{ lo, hi, isFullRange: lo === boundLo && hi === boundHi }};
      }};
      state.matches = (value) => {{
        const [lo, hi] = state.range;
        return value == null || (value >= lo && value <= hi);
      }};
      return state;
    }}

    const priceMinInput = document.getElementById('priceMin');
    const priceMaxInput = document.getElementById('priceMax');
    const priceLabel = document.getElementById('priceLabel');
    const priceFilter = makeRangeFilter(priceMinInput, priceMaxInput, document.getElementById('priceFill'), l => l.price);

    const bedroomsMinInput = document.getElementById('bedroomsMin');
    const bedroomsMaxInput = document.getElementById('bedroomsMax');
    const bedroomsLabel = document.getElementById('bedroomsLabel');
    const bedroomsFilter = makeRangeFilter(bedroomsMinInput, bedroomsMaxInput, document.getElementById('bedroomsFill'), l => l.bedrooms);

    function fmtPrice(n) {{ return Math.round(n).toLocaleString('fr-CA') + ' $'; }}

    function renderMarkers() {{
      if (!markersLayer) return;
      markersLayer.clearLayers();
      for (const l of DAYS[currentCategory][currentDay]) {{
        if (!priceFilter.matches(l.price) || !bedroomsFilter.matches(l.bedrooms)) continue;
        L.circleMarker([l.lat, l.lon], {{radius: 7, color: '#dc2626', fillColor: '#dc2626', fillOpacity: 0.85}})
          .addTo(markersLayer)
          .bindPopup(popupContent(l));
      }}
    }}

    function applyFilters() {{
      const price = priceFilter.updateUI();
      const bedrooms = bedroomsFilter.updateUI();
      const cards = document.querySelectorAll('.page:not([hidden]) .card');
      let visible = 0;
      cards.forEach(card => {{
        const p = card.dataset.price ? Number(card.dataset.price) : null;
        const b = card.dataset.bedrooms ? Number(card.dataset.bedrooms) : null;
        const shown = priceFilter.matches(p) && bedroomsFilter.matches(b);
        card.classList.toggle('filtered-out', !shown);
        if (shown) visible++;
      }});
      const total = DAYS[currentCategory][currentDay].length;
      const priceText = price.isFullRange ? 'Tous les prix' : `${{fmtPrice(price.lo)}} – ${{fmtPrice(price.hi)}}`;
      priceLabel.textContent = `💰 ${{priceText}} · ${{visible}}/${{total}} annonce(s)`;
      const bedroomsText = bedrooms.isFullRange
        ? 'Toutes les chambres'
        : (bedrooms.lo === bedrooms.hi ? `${{bedrooms.lo}} chambres` : `${{bedrooms.lo}}-${{bedrooms.hi}} chambres`);
      bedroomsLabel.textContent = `🛏 ${{bedroomsText}}`;
      renderMarkers();
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
      if (currentCategory !== previousCategory) {{
        // Recherche différente : réinitialise les curseurs à leur pleine
        // échelle plutôt que de garder un intervalle qui n'a plus de sens
        // (ex. un 250k-750k choisi pour les condos, appliqué tel quel aux
        // chalets).
        priceFilter.reset();
        bedroomsFilter.reset();
        previousCategory = currentCategory;
      }}
      applyFilters();
    }}
    prevBtn.addEventListener('click', () => {{ if (currentDay < DAY_LABELS.length - 1) {{ currentDay++; render(); }} }});
    nextBtn.addEventListener('click', () => {{ if (currentDay > 0) {{ currentDay--; render(); }} }});
    toggleBtns.forEach(b => b.addEventListener('click', () => {{ currentCategory = b.dataset.category; render(); }}));
    priceMinInput.addEventListener('input', applyFilters);
    priceMaxInput.addEventListener('input', applyFilters);
    bedroomsMinInput.addEventListener('input', applyFilters);
    bedroomsMaxInput.addEventListener('input', applyFilters);
    render();
  </script>
</body>
</html>"""

    REPORT_FILE.write_text(html, encoding="utf-8")
    return REPORT_FILE


def run_search(name: str, origin: tuple[float, float], config: dict) -> list[dict]:
    """
    Exécute une recherche complète (Centris + uBee + filtre temps de route)
    pour une entrée de SEARCHES à partir d'une origine déjà résolue
    (partagée par toutes les recherches, géocodée une seule fois dans
    main()), et retourne les candidats.

    Centris et uBee sont appelés dans des try/except séparés : une requête
    refusée par l'un des deux (ex. valeur de filtre non reconnue) ne doit
    interrompre ni l'autre site, ni les autres recherches de SEARCHES. Un
    run du 2026-09-20 a montré que c'était nécessaire — uBee a répondu
    400 Bad Request pour la recherche "condos" (valeur de filtre
    inscriptionTypes/Copropriete devinée à tort, corrigée depuis en
    "Condo" — voir UBEE_INSCRIPTION_TYPES), ce qui a fait planter tout le
    script avant même la génération du rapport, alors que "chalets" avait
    déjà réussi. Le try/except reste utile pour toute future valeur de
    filtre à deviner.
    """
    radius_km = config["search_radius_km"]
    max_drive_hours = config["max_drive_hours"]
    max_price = config.get("max_price")
    min_bedrooms = config.get("min_bedrooms")

    raw_listings: list[dict] = []
    try:
        raw_listings += search_centris_listings(origin, radius_km, category=name, max_price=max_price)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"[{name}] échec de la recherche Centris, ignorée pour ce run : {exc}")
    try:
        raw_listings += search_ubee_listings(origin, radius_km, category=name)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"[{name}] échec de la recherche uBee, ignorée pour ce run : {exc}")
    print(f"[{name}] {len(raw_listings)} annonce(s) brute(s) trouvée(s) (Centris + uBee).")

    if max_price is not None:
        # uBee n'a pas de filtre de prix côté serveur (voir search_ubee_listings) —
        # on filtre donc ici, après coup, pour couvrir les deux sources de façon
        # symétrique. Une annonce sans prix connu (scraping incomplet) est gardée
        # plutôt qu'exclue à tort.
        before = len(raw_listings)
        raw_listings = [l for l in raw_listings if l.get("price") is None or l["price"] <= max_price]
        print(f"[{name}] {len(raw_listings)} annonce(s) à {max_price:,} $ ou moins (sur {before}).".replace(",", " "))

    if min_bedrooms is not None:
        # Centris "condos" n'a aucun filtre de chambres côté serveur (voir
        # search_centris_listings) — c'est ce qui laissait passer des
        # annonces à 2 chambres malgré CONDO_MIN_BEDROOMS. On filtre donc
        # ici sur le nombre de chambres scrapé (Centris : confirmé par
        # capture réseau le 2026-09-22, voir parse_centris_listing_cards ;
        # uBee : "nbBedrooms", confirmé le même jour, voir
        # parse_ubee_listings — filtre redondant avec son minBedrooms
        # serveur, déjà confirmé, mais ne coûte rien). Une annonce sans
        # chambre connue est gardée plutôt qu'exclue à tort, même
        # convention que pour un prix inconnu.
        before = len(raw_listings)
        raw_listings = [l for l in raw_listings if l.get("bedrooms") is None or l["bedrooms"] >= min_bedrooms]
        print(f"[{name}] {len(raw_listings)} annonce(s) à {min_bedrooms}+ chambres (sur {before}).")
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

    return candidates


def main() -> None:
    origin = geocode_address(ORIGIN_ADDRESS)
    print(f"Origine ({ORIGIN_ADDRESS}) géocodée à {origin}.")

    origins: dict[str, tuple[float, float]] = {}
    categories: dict[str, list[dict]] = {}
    for name, config in SEARCHES.items():
        candidates = run_search(name, origin, config)
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
