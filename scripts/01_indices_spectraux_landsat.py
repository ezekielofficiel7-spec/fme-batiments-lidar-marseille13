# -*- coding: utf-8 -*-
"""
=====================================================================
 Projet FME & Indices spectraux - Master 2 GMS (Aix-Marseille Univ.)
 Script Python : calcul des indices spectraux Landsat 8/9
 Zone : 13e arrondissement de Marseille (13013)
=====================================================================

OBJECTIF
    Calculer, a partir des produits Landsat 8/9 Collection 2 Level-2
    (reflectance de surface + temperature de surface), 8 indicateurs
    pour 4 dates imposees, puis les exporter en GeoTIFF (EPSG:2154)
    pour integration dans le workflow FME (statistiques par batiment).

DATES (scenes retenues, Landsat 8, trace WRS-2 195/030)
    2013_07_23 : LC08_195030_20130723  (nuages 0.6 %)
    2013_12_30 : LC08_195030_20131230  (nuages 0.3 %)
    2025_11_29 : LC08_195030_20251129  (nuages 0.4 %)
    Ete 2026   : LC08_195030_20260727  (nuages 0.3 %)
                 -> coeur de l'ete, meme capteur et meme trace que
                    les 3 autres dates (geometrie d'acquisition homogene)

INDICES ET FORMULES (bandes OLI : B2 bleu, B3 vert, B4 rouge,
                     B5 PIR, B6 SWIR1, B7 SWIR2 ; TIRS : B10)
    LST  = ST_B10 (K) - 273.15                       [deg C]
    NDVI = (B5 - B4) / (B5 + B4)
    NDBI = (B6 - B5) / (B6 + B5)
    NDMI = (B5 - B6) / (B5 + B6)
    NDWI = (B3 - B5) / (B3 + B5)                     (McFeeters, 1996)
    BSI  = ((B6 + B4) - (B5 + B2)) / ((B6 + B4) + (B5 + B2))
    EMIS : methode des seuils NDVI (Sobrino et al., 2004)
           Pv = ((NDVI - 0.2) / (0.5 - 0.2))^2
           eps = 0.004 * Pv + 0.986       si 0.2 <= NDVI <= 0.5
           eps = 0.973 (sol/bati) si NDVI < 0.2 ; 0.99 si NDVI > 0.5 ;
           eps = 0.991 (eau) si NDVI < 0
    ALB  = (0.356 B2 + 0.130 B4 + 0.373 B5 + 0.085 B6 + 0.072 B7
            - 0.0018) / 1.016                          (Liang, 2001)

PRETRAITEMENTS
    - Facteurs d'echelle C2 L2 :
        reflectance = DN * 0.0000275 - 0.2
        temperature = DN * 0.00341802 + 149.0   (Kelvin)
    - Masque QA_PIXEL : nuages dilates (bit 1), nuages (bit 3),
      ombres (bit 4)
    - Masque des reflectances aberrantes : pixels conserves si toutes
      les bandes SR sont dans ]0, 1] (eau, ombres profondes exclues ;
      evite les valeurs hors bornes du BSI et l'albedo negatif)

SORTIES (Google Drive / FME_Landsat)
    Indices_<date>.tif : GeoTIFF float32, 8 bandes nommees
        1 LST, 2 NDVI, 3 NDBI, 4 NDMI, 5 NDWI, 6 BSI, 7 EMIS, 8 ALB
    EPSG:2154 (Lambert 93), 30 m, nodata = -9999
    Controle_indices.csv : min / max / moyenne / % vides par indice

EXECUTION
    Google Colab, cellules a executer dans l'ordre.
    Prerequis : compte Google Earth Engine et projet Cloud enregistre.
"""


# %% 1. INSTALLATION, CONNEXIONS ET PARAMETRES
# (Colab) installation des bibliotheques non presentes par defaut
# !pip -q install geemap rasterio
import os, glob, time
import numpy as np
import pandas as pd
import rasterio
import ee
from google.colab import drive

drive.mount('/content/drive')          # acces au Google Drive
ee.Authenticate()                      # autorisation Earth Engine (fenetre Google)

PROJECT_ID = "votre-projet-gee"        # a remplacer par votre projet Cloud Earth Engine
ee.Initialize(project=PROJECT_ID)

DRIVE_FOLDER = "FME_Landsat"                            # dossier d'export sur le Drive
DOSSIER = f"/content/drive/MyDrive/{DRIVE_FOLDER}"      # meme dossier vu depuis Colab
NODATA = -9999
NOMS_BANDES = ["LST", "NDVI", "NDBI", "NDMI", "NDWI", "BSI", "EMIS", "ALB"]

# Emprise d'etude : rectangle englobant le 13e arrondissement (WGS84, avec marge)
AOI = ee.Geometry.Rectangle([5.35, 43.30, 5.50, 43.42])
print("Initialisation OK")


# %% 2. RECHERCHE DES SCENES DISPONIBLES (justification du choix des dates)
collection = (ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
              .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
              .filterBounds(AOI))

def lister_scenes(debut, fin):
    """Affiche les scenes d'une periode, triees par couverture nuageuse."""
    imgs = collection.filterDate(debut, fin).sort("CLOUD_COVER")
    ids = imgs.aggregate_array("system:index").getInfo()
    nuages = imgs.aggregate_array("CLOUD_COVER").getInfo()
    if not ids:
        print("   aucune scene")
    for i, c in zip(ids, nuages):
        print(f"   {i} | nuages : {c:.1f} %")

PERIODES = {
    "2013_07_23": ("2013-07-22", "2013-07-24"),
    "2013_12_30": ("2013-12-29", "2013-12-31"),
    "2025_11_29": ("2025-11-28", "2025-11-30"),
    "Ete 2026":   ("2026-06-21", "2026-09-23"),
}
for nom, (d, f) in PERIODES.items():
    print("---", nom)
    lister_scenes(d, f)


# %% 3. SCENES RETENUES
# Landsat 8, trace 195/030 pour les 4 dates (meme capteur, meme geometrie).
# Ete 2026 : 27/07/2026, scene quasi sans nuage au coeur de l'ete.
IMAGES = {
    "2013_07_23": ee.Image("LANDSAT/LC08/C02/T1_L2/LC08_195030_20130723"),
    "2013_12_30": ee.Image("LANDSAT/LC08/C02/T1_L2/LC08_195030_20131230"),
    "2025_11_29": ee.Image("LANDSAT/LC08/C02/T1_L2/LC08_195030_20251129"),
    "2026_07_27": ee.Image("LANDSAT/LC08/C02/T1_L2/LC08_195030_20260727"),
}


# %% 4. PRETRAITEMENTS ET CALCUL DES INDICES

def appliquer_echelle(img):
    """Facteurs d'echelle Collection 2 Level-2 + masque des reflectances aberrantes."""
    sr = img.select("SR_B.").multiply(0.0000275).add(-0.2)        # reflectance 0-1
    st = img.select("ST_B10").multiply(0.00341802).add(149.0)     # temperature (K)
    # Pixel valide si toutes les reflectances sont dans ]0, 1]
    valide = (sr.reduce(ee.Reducer.min()).gt(0)
              .And(sr.reduce(ee.Reducer.max()).lte(1)))
    return (img.addBands(sr, None, True)
               .addBands(st, None, True)
               .updateMask(valide))


def masque_nuages(img):
    """Masque QA_PIXEL : nuages dilates (bit 1), nuages (bit 3), ombres (bit 4)."""
    qa = img.select("QA_PIXEL")
    clair = (qa.bitwiseAnd(1 << 1).eq(0)
             .And(qa.bitwiseAnd(1 << 3).eq(0))
             .And(qa.bitwiseAnd(1 << 4).eq(0)))
    return img.updateMask(clair)


def calculer_indices(img):
    """Retourne une image 8 bandes : LST, NDVI, NDBI, NDMI, NDWI, BSI, EMIS, ALB."""
    img = appliquer_echelle(masque_nuages(img))
    B2, B4 = img.select("SR_B2"), img.select("SR_B4")
    B5, B6, B7 = img.select("SR_B5"), img.select("SR_B6"), img.select("SR_B7")

    # Indices normalises : (a - b) / (a + b)
    ndvi = img.normalizedDifference(["SR_B5", "SR_B4"]).rename("NDVI")   # vegetation
    ndbi = img.normalizedDifference(["SR_B6", "SR_B5"]).rename("NDBI")   # bati
    ndmi = img.normalizedDifference(["SR_B5", "SR_B6"]).rename("NDMI")   # humidite
    ndwi = img.normalizedDifference(["SR_B3", "SR_B5"]).rename("NDWI")   # eau (McFeeters)

    # BSI = ((SWIR1 + R) - (PIR + B)) / ((SWIR1 + R) + (PIR + B))
    bsi = (B6.add(B4).subtract(B5.add(B2))
           .divide(B6.add(B4).add(B5.add(B2)))).rename("BSI")

    # Emissivite - methode des seuils NDVI (Sobrino et al., 2004)
    pv = ndvi.subtract(0.2).divide(0.5 - 0.2).pow(2)          # proportion de vegetation
    emis = (ee.Image(0.004).multiply(pv).add(0.986)           # zone de melange
            .where(ndvi.lt(0.2), 0.973)                       # sol nu / bati
            .where(ndvi.gt(0.5), 0.99)                        # vegetation dense
            .where(ndvi.lt(0), 0.991)                         # eau
            .rename("EMIS"))

    # Albedo de Liang (2001), coefficients adaptes a OLI
    alb = (B2.multiply(0.356).add(B4.multiply(0.130)).add(B5.multiply(0.373))
           .add(B6.multiply(0.085)).add(B7.multiply(0.072))
           .subtract(0.0018).divide(1.016)).rename("ALB")

    # LST : produit Surface Temperature L2 (corrige atmosphere + emissivite), en deg C
    lst = img.select("ST_B10").subtract(273.15).rename("LST")

    return ee.Image.cat([lst, ndvi, ndbi, ndmi, ndwi, bsi, emis, alb]).toFloat()


# %% 5. EXPORT DES 4 GEOTIFF VERS GOOGLE DRIVE
os.makedirs(DOSSIER, exist_ok=True)
for f in glob.glob(f"{DOSSIER}/Indices_*.tif"):     # supprime d'eventuelles anciennes versions
    os.remove(f)

taches = []
for nom, img in IMAGES.items():
    tache = ee.batch.Export.image.toDrive(
        image=calculer_indices(img).clip(AOI).unmask(NODATA),
        description=f"Indices_{nom}",
        folder=DRIVE_FOLDER,
        fileNamePrefix=f"Indices_{nom}",
        region=AOI,
        scale=30,                     # resolution native Landsat
        crs="EPSG:2154",              # Lambert 93, CRS commun du projet
        maxPixels=1e9,
        formatOptions={"noData": NODATA})
    tache.start()
    taches.append(tache)
    print("Export lance :", nom)

# Attente de la fin des exports
while any(t.status()["state"] in ("READY", "RUNNING") for t in taches):
    time.sleep(20)
for t in taches:
    s = t.status()
    print(s["description"], "->", s["state"])


# %% 6. POST-TRAITEMENT : NaN -> NODATA ET NOMMAGE DES BANDES
# Earth Engine peut ecrire certains pixels masques en NaN : on les remplace
# par -9999 (nodata declare) pour que FME les ignore dans les statistiques.

# Attente de la synchronisation du Drive (jusqu'a 5 min)
for _ in range(30):
    fichiers = sorted(glob.glob(f"{DOSSIER}/Indices_*.tif"))
    if len(fichiers) == len(IMAGES):
        break
    time.sleep(10)
print(len(fichiers), "fichiers trouves")

for f in fichiers:
    with rasterio.open(f) as src:
        data = src.read().astype("float32")
        meta = src.meta.copy()
    n_nan = int(np.isnan(data).sum())
    data[np.isnan(data)] = NODATA
    meta.update(nodata=NODATA, dtype="float32")
    tmp = f + ".tmp.tif"
    with rasterio.open(tmp, "w", **meta) as dst:
        dst.write(data)
        dst.descriptions = tuple(NOMS_BANDES)
    os.replace(tmp, f)
    print(f"{os.path.basename(f)} : {n_nan:,} NaN remplaces par {NODATA}")


# %% 7. CONTROLE QUALITE (min / max / moyenne / % de pixels vides)
BORNES = {"LST": (-20, 70), "NDVI": (-1, 1), "NDBI": (-1, 1), "NDMI": (-1, 1),
          "NDWI": (-1, 1), "BSI": (-1, 1), "EMIS": (0.97, 0.995), "ALB": (0, 1)}

lignes = []
for f in sorted(glob.glob(f"{DOSSIER}/Indices_*.tif")):
    date = os.path.basename(f).replace("Indices_", "").replace(".tif", "")
    with rasterio.open(f) as src:
        assert src.crs.to_epsg() == 2154 and src.count == 8 and src.nodata == NODATA
        for i, nom in enumerate(NOMS_BANDES, start=1):
            b = src.read(i, masked=True)
            lo, hi = BORNES[nom]
            lignes.append({
                "date": date, "indice": nom,
                "min": round(float(b.min()), 3), "max": round(float(b.max()), 3),
                "moyenne": round(float(b.mean()), 3),
                "vides_%": round(100 * float(np.ma.getmaskarray(b).mean()), 1),
                "dans_bornes": bool(b.min() >= lo and b.max() <= hi)})

controle = pd.DataFrame(lignes)
controle.to_csv(f"{DOSSIER}/Controle_indices.csv", index=False)
print(controle.to_string(index=False))
print("Tous les indices dans leurs bornes :", controle["dans_bornes"].all())
