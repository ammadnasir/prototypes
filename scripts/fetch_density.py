#!/usr/bin/env python3
"""
Snapshot 2021 census population density by dissemination area.

Writes data/density-da.geojson: one polygon per DA with people per square km,
plus the quantile breaks the map uses for its colour scale. A dissemination area
holds 400-700 people, fine enough to separate a tower block from the street behind it.

Sources, both free and public:
  boundaries  Statistics Canada 2021 Census Dissemination Area boundary file
  counts      Statistics Canada table 98-10-0015 (population, land area, density)
"""

import csv
import io
import json
import os
import time
import zipfile
from urllib.request import urlopen

BOUNDARY_CANDIDATES = [
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lda_000b21a_e.zip",
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lda_000a21a_e.zip",
    "https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/boundary-limites/files-fichiers/lda_000b21f_e.zip",
]
COUNTS_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/98100015-eng.zip"

BBOX = (-79.58, 43.58, -79.26, 43.80)     # same window as the zoning snapshot
OUT = "data/density-da.geojson"
SIMPLIFY_DEG = 0.00008


def fetch(url, tries=3):
    for attempt in range(tries):
        try:
            print(f"  downloading {url.rsplit('/', 1)[-1]}")
            with urlopen(url, timeout=600) as r:
                return r.read()
        except Exception as exc:                       # noqa: BLE001
            print(f"    attempt {attempt + 1} failed: {exc}")
            time.sleep(4 * (attempt + 1))
    return None


def load_counts():
    """DAUID -> people per square km, from the census table."""
    raw = fetch(COUNTS_URL)
    if not raw:
        raise SystemExit("Could not download the census counts table.")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in zf.namelist()
                if n.lower().endswith(".csv") and "metadata" not in n.lower())
    print(f"  counts file: {name}")
    text = zf.read(name).decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    print(f"  columns: {header[:12]}")

    def find(*words):
        for i, h in enumerate(header):
            low = h.lower()
            if all(w in low for w in words):
                return i
        return None

    i_guid = find("dguid")
    i_den = find("density")
    i_pop = find("population", "2021")
    i_area = find("land area")
    print(f"  using columns dguid={i_guid} density={i_den} pop={i_pop} area={i_area}")
    if i_guid is None or (i_den is None and (i_pop is None or i_area is None)):
        raise SystemExit(f"Unexpected column layout: {header}")

    out, sample = {}, None
    for row in reader:
        if len(row) <= i_guid:
            continue
        guid = row[i_guid].strip()
        if "S0512" not in guid:            # dissemination area records only
            continue
        dauid = guid[-8:]
        try:
            if i_den is not None and row[i_den].strip():
                out[dauid] = float(row[i_den].replace(",", ""))
            else:
                pop = float(row[i_pop].replace(",", ""))
                area = float(row[i_area].replace(",", ""))
                out[dauid] = pop / area if area else 0.0
        except (ValueError, IndexError):
            continue
        if sample is None:
            sample = (dauid, out[dauid])
    print(f"  {len(out)} dissemination areas with counts, sample {sample}")
    return out


def load_boundaries():
    """DAUID -> polygon rings in lon/lat, clipped to our window."""
    import shapefile                       # pyshp
    from pyproj import Transformer
    from shapely.geometry import shape as shp_shape, box
    from shapely.ops import transform as shp_transform

    raw = None
    for url in BOUNDARY_CANDIDATES:
        raw = fetch(url, tries=2)
        if raw:
            break
    if not raw:
        raise SystemExit("Could not download any dissemination area boundary file.")

    zf = zipfile.ZipFile(io.BytesIO(raw))
    stem = next(n[:-4] for n in zf.namelist() if n.lower().endswith(".shp"))
    print(f"  shapefile: {stem}")
    reader = shapefile.Reader(shp=io.BytesIO(zf.read(stem + ".shp")),
                              dbf=io.BytesIO(zf.read(stem + ".dbf")))
    fields = [f[0] for f in reader.fields[1:]]
    i_da = fields.index("DAUID") if "DAUID" in fields else 0
    print(f"  fields: {fields[:8]}")

    to_wgs = Transformer.from_crs("EPSG:3347", "EPSG:4326", always_xy=True).transform
    window = box(*BBOX)
    out = {}
    for rec in reader.iterShapeRecords():
        geom = shp_shape(rec.shape.__geo_interface__)
        geom = shp_transform(to_wgs, geom)
        if not geom.intersects(window):
            continue
        geom = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
        if not geom.is_empty:
            out[str(rec.record[i_da])] = geom
    print(f"  {len(out)} areas inside the window")
    return out


def main():
    counts = load_counts()
    shapes = load_boundaries()

    from shapely.geometry import mapping
    feats = []
    for dauid, geom in shapes.items():
        d = counts.get(dauid)
        if d is None:
            continue
        feats.append({"type": "Feature",
                      "properties": {"d": round(d)},
                      "geometry": mapping(geom)})
    print(f"  {len(feats)} areas matched to counts")
    if len(feats) < 100:
        raise SystemExit("Too few matched areas - refusing to overwrite good data.")

    vals = sorted(f["properties"]["d"] for f in feats if f["properties"]["d"] > 0)
    breaks = [vals[int(len(vals) * q)] for q in (0.2, 0.4, 0.6, 0.8)]

    os.makedirs("data", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "properties": {
                       "source": "Statistics Canada, 2021 Census (dissemination areas)",
                       "units": "people per square kilometre",
                       "breaks": breaks,
                       "generated": time.strftime("%Y-%m-%d")},
                   "features": feats}, fh, separators=(",", ":"))
    mb = os.path.getsize(OUT) / 1e6
    print(f"wrote {OUT} ({mb:.2f} MB), {len(feats)} areas, breaks {breaks}")

    meta = json.load(open("data/meta.json")) if os.path.exists("data/meta.json") else {}
    meta["density"] = {"areas": len(feats), "breaks": breaks, "megabytes": round(mb, 2),
                       "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    json.dump(meta, open("data/meta.json", "w"), indent=2)


if __name__ == "__main__":
    main()
