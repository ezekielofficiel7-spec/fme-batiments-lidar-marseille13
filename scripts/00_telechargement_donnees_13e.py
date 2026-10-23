# -*- coding: utf-8 -*-
"""
Téléchargement des données du projet FME (hors BD TOPO) à partir de la limite du 13e arr. de Marseille

  1. LiDAR HD IGN : dalles MNS + MNT (GeoTIFF 1 km, 50 cm)
  2. Landsat 8/9 Collection 2 Level-2 : 4 dates, bandes recadrées sur le 13e
  3. OpenStreetMap : occupation / usage du sol (landuse, amenity, leisure, natural)

À exécuter dans Google Colab (une cellule par bloc "# %%").
Sorties : /content/drive/MyDrive/FME/data/{lidar,landsat,osm}
"""

# %% 0. Installation + Drive + paramètres
# !pip -q install geopandas osmnx pystac-client planetary-computer rasterio tqdm

import os, time, requests
import geopandas as gpd
import pandas as pd
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import transform_bounds
from tqdm import tqdm

from google.colab import drive
drive.mount('/content/drive')

LIMITE = "/content/drive/MyDrive/FME/Arrondissement 13.gpkg"
OUT    = "/content/drive/MyDrive/FME/data"
for sub in ["lidar/mns", "lidar/mnt", "landsat", "osm"]:
    os.makedirs(f"{OUT}/{sub}", exist_ok=True)

DATES = {
    "2013_07_23":  "2013-07-23/2013-07-23",
    "2013_12_30":  "2013-12-30/2013-12-30",
    "2026_summer": "2026-06-21/2026-09-22",   # fenêtre : on garde la scène la moins nuageuse
    "2025_11_29":  "2025-11-29/2025-11-29",
}

# %% 1. Limite du 13e (Lambert 93) + emprise
gdf_lim = gpd.read_file(LIMITE).to_crs(2154)
limite = gdf_lim.geometry.union_all() if hasattr(gdf_lim.geometry, "union_all") else gdf_lim.unary_union
limite_buf = limite.buffer(100)                         # marge de sécurité (m)
xmin, ymin, xmax, ymax = limite_buf.bounds
print("Emprise L93 :", [round(v) for v in (xmin, ymin, xmax, ymax)])

# %% 2. Fonctions utilitaires
def fix_crs(g):
    """GeoJSON du WFS : CRS parfois absent -> détection lon/lat vs Lambert 93."""
    if g.crs is None:
        g = g.set_crs(2154 if abs(g.total_bounds[0]) > 1000 else 4326)
    return g.to_crs(2154)

def download(url, dest, retries=3):
    """Téléchargement en flux, saute les fichiers déjà présents."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    for i in range(retries):
        try:
            with requests.get(url, stream=True, timeout=180) as r:
                r.raise_for_status()
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            os.replace(tmp, dest)
            return dest
        except Exception as e:
            print(f"   ! essai {i+1}/{retries} échoué ({e})")
            time.sleep(3 * (i + 1))
    return None

# %% 3. LiDAR HD : MNS + MNT
WFS_LIDAR = "https://data.geopf.fr/private/wfs/wfs"

def wfs_lidar_tiles(layer):
    r = requests.get(WFS_LIDAR, params=dict(
        apikey="interface_catalogue",   # cle publique du catalogue IGN (pas un secret)
        SERVICE="WFS", VERSION="2.0.0", REQUEST="GetFeature",
        TYPENAMES=layer, OUTPUTFORMAT="application/json",
        BBOX=f"{xmin},{ymin},{xmax},{ymax},EPSG:2154", COUNT=5000), timeout=180)
    try:
        feats = r.json().get("features", [])
    except ValueError:
        raise RuntimeError(f"Réponse non JSON (HTTP {r.status_code}) :\n{r.text[:800]}")
    g = gpd.GeoDataFrame.from_features(feats)
    if g.empty:
        return g
    g = fix_crs(g)
    return g[g.intersects(limite_buf)].reset_index(drop=True)

for prod, layer in [("mns", "IGNF_LIDAR-HD_TA:mns-dalle"), ("mnt", "IGNF_LIDAR-HD_TA:mnt-dalle")]:
    tiles = wfs_lidar_tiles(layer)
    print(f"{prod.upper()} : {len(tiles)} dalles")
    if tiles.empty:
        print("   -> aucune dalle : vérifier https://macarte.ign.fr/carte/mThSup/diffusionMNxLiDARHD")
        continue
    url_col  = next((c for c in tiles.columns if c.lower() == "url"), None)
    name_col = next((c for c in tiles.columns if c.lower() == "name"), None)
    if url_col is None:
        print("   Colonnes disponibles :", list(tiles.columns)); continue
    for _, t in tqdm(tiles.iterrows(), total=len(tiles), desc=prod):
        fname = (t[name_col] if name_col else os.path.basename(t[url_col])).split("?")[0]
        if not fname.lower().endswith((".tif", ".tiff")):
            fname += ".tif"
        download(t[url_col], f"{OUT}/lidar/{prod}/{fname}")
    tiles.to_file(f"{OUT}/lidar/{prod}_index.gpkg", driver="GPKG")

# %% 4. Landsat 8/9 Collection 2 Level-2 (Planetary Computer, recadré sur le 13e)
import pystac_client, planetary_computer

cat = pystac_client.Client.open("https://planetarycomputer.microsoft.com/api/stac/v1",
                                modifier=planetary_computer.sign_inplace)
geom_wgs84 = gpd.GeoSeries([limite], crs=2154).to_crs(4326).iloc[0].__geo_interface__
BANDS = {"blue": "SR_B2", "green": "SR_B3", "red": "SR_B4", "nir08": "SR_B5",
         "swir16": "SR_B6", "swir22": "SR_B7", "lwir11": "ST_B10", "qa_pixel": "QA_PIXEL"}

def clip_to_disk(href, dest):
    """Lecture fenêtrée du COG distant -> GeoTIFF local (valeurs brutes DN, UTM 31N d'origine)."""
    with rasterio.open(href) as src:
        b = transform_bounds(2154, src.crs, xmin, ymin, xmax, ymax)
        win = from_bounds(*b, transform=src.transform).round_offsets().round_lengths()
        data = src.read(1, window=win)
        prof = src.profile.copy()
        prof.update(height=data.shape[0], width=data.shape[1],
                    transform=src.window_transform(win), compress="deflate", tiled=False)
        with rasterio.open(dest, "w", **prof) as dst:
            dst.write(data, 1)

resume = []
for label, dt in DATES.items():
    items = list(cat.search(collections=["landsat-c2-l2"], intersects=geom_wgs84, datetime=dt,
                            query={"platform": {"in": ["landsat-8", "landsat-9"]}}).items())
    if not items:
        print(f"[{label}] aucune scène Landsat 8/9 -> élargir la fenêtre de dates")
        resume.append((label, None, None)); continue
    items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100))
    it = items[0]
    print(f"[{label}] {it.id} | {it.properties['platform']} | nuages {it.properties.get('eo:cloud_cover'):.1f} %")
    d = f"{OUT}/landsat/{label}"; os.makedirs(d, exist_ok=True)
    for asset, name in BANDS.items():
        dest = f"{d}/{it.id}_{name}.tif"
        if not os.path.exists(dest):
            clip_to_disk(it.assets[asset].href, dest)
    resume.append((label, it.id, it.properties.get("eo:cloud_cover")))

print(pd.DataFrame(resume, columns=["date", "scene", "nuages_%"]))

# %% 5. OpenStreetMap : occupation / usage du sol (les bâtiments OSM sont déjà dans ton fichier)
import osmnx as ox

poly_wgs84 = gpd.GeoSeries([limite], crs=2154).to_crs(4326).iloc[0]
tags = {"landuse": True, "amenity": True, "leisure": True, "natural": True}
osm = ox.features_from_polygon(poly_wgs84, tags=tags).reset_index()
keep = ["element", "id", "landuse", "amenity", "leisure", "natural", "name", "geometry"]
osm = osm[[c for c in keep if c in osm.columns]].to_crs(2154)
osm = osm[osm.geometry.type.isin(["Polygon", "MultiPolygon"])]
osm.to_file(f"{OUT}/osm/osm_occupation_sol_13e.gpkg", layer="occupation_sol", driver="GPKG")
print("OSM occupation du sol :", len(osm))

print("\nTerminé. Données dans :", OUT)
