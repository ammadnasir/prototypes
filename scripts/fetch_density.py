#!/usr/bin/env python3
"""
Snapshot 2021 census population density by dissemination area.

Writes data/density-da.geojson: one polygon per DA with people per square km,
plus the quantile breaks the map uses for its colour scale.

A dissemination area holds 400-700 people, so it's fine-grained enough to show
the difference between a tower block and the street behind it.

Source: Statistics Canada 2021 Census, Dissemination Area boundary file and
table 98-10-0015 (population, dwellings, land area), served as a feature layer.
"""

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import urlopen

URL = ("https://services2.arcgis.com/11XBiaBYA9Ep0yNJ/ArcGIS/rest/services/"
       "Census_2021_Dissemination_Areas/FeatureServer/0/query")

# Same window as the zoning snapshot.
BBOX = (-79.58, 43.58, -79.26, 43.80)
PAGE = 1000
OUT = "data/density-da.geojson"


def get(params, tries=4):
    url = URL + "?" + urlencode(params)
    for attempt in range(tries):
        try:
            with urlopen(url, timeout=180) as r:
                return json.loads(r.read().decode())
        except Exception as exc:                       # noqa: BLE001
            if attempt == tries - 1:
                raise
            print(f"  retry {attempt + 1} after {exc}")
            time.sleep(3 * (attempt + 1))


def round_coords(node, places=5):
    if isinstance(node, list):
        if node and isinstance(node[0], (int, float)):
            return [round(c, places) for c in node]
        return [round_coords(n, places) for n in node]
    return node


def density_of(props):
    """Prefer the published density; fall back to population over land area."""
    for key in ("DAPOPDEN", "DA_POP_DENSITY", "POP_DENSITY"):
        v = props.get(key)
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    pop = props.get("DAPOP2021") or props.get("DAPOP") or 0
    area = props.get("DAAREA") or 0
    return float(pop) / float(area) if area else 0.0


def probe():
    """Confirm the layer is there and say what it calls things."""
    try:
        with urlopen(URL.replace("/query", "?f=json"), timeout=60) as r:
            meta = json.loads(r.read().decode())
        print("layer:", meta.get("name"), "| max records:", meta.get("maxRecordCount"))
        print("fields:", ", ".join(f["name"] for f in meta.get("fields", []))[:300])
    except Exception as exc:                           # noqa: BLE001
        print("probe failed:", exc)


# Toronto is one census subdivision, so an attribute filter beats a spatial one.
ENVELOPE = json.dumps({"xmin": BBOX[0], "ymin": BBOX[1], "xmax": BBOX[2], "ymax": BBOX[3],
                       "spatialReference": {"wkid": 4326}})
VARIANTS = [
    {"where": "CSDUID='3520005'"},
    {"where": "CSDNAME='Toronto'"},
    {"where": "CDNAME='Toronto'"},
    {"where": "1=1", "geometry": ENVELOPE, "geometryType": "esriGeometryEnvelope",
     "spatialRel": "esriSpatialRelIntersects", "inSR": "4326"},
]


def query(variant, offset, count_only=False):
    p = {
        "where": "1=1",
        "outSR": "4326",
        "outFields": "*",
        "returnGeometry": "false" if count_only else "true",
        "f": "json" if count_only else "geojson",
    }
    if count_only:
        p["returnCountOnly"] = "true"
    else:
        p["resultOffset"] = str(offset)
        p["resultRecordCount"] = str(PAGE)
        p["geometryPrecision"] = "5"
    p.update(variant)
    return get(p)


def rings_to_geojson(geom):
    """Esri rings to a GeoJSON polygon, clockwise ring is the outer one."""
    from shapely.geometry import Polygon, mapping
    from shapely.ops import unary_union
    outers, holes = [], []
    for ring in geom.get("rings", []):
        if len(ring) < 4:
            continue
        area = sum((ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1])
                   for i in range(len(ring) - 1)) / 2.0
        (holes if area > 0 else outers).append(ring)
    polys = []
    for outer in outers:
        shell = Polygon(outer)
        inner = [h for h in holes if shell.contains(Polygon(h).representative_point())]
        polys.append(Polygon(outer, inner))
    return mapping(unary_union(polys)) if polys else None


def main():
    probe()

    variant = None
    for v in VARIANTS:
        label = v.get("where", "")[:40]
        c = query(v, 0, count_only=True)
        if c.get("error"):
            print(f"  [{label}] count error: {str(c['error'])[:140]}")
            continue
        print(f"  [{label}] count = {c.get('count')}")
        if not c.get("count"):
            continue
        first = query(v, 0)
        n = len(first.get("features", []))
        print(f"  [{label}] first page = {n} features")
        if n:
            variant = v
            break
    if variant is None:
        raise SystemExit("No query variant returned features.")

    feats, offset = [], 0
    while True:
        page = query(variant, offset)
        got = page.get("features", [])
        for f in got:
            geom = f.get("geometry")
            props = f.get("properties") or f.get("attributes") or {}
            if geom and "rings" in geom:
                geom = rings_to_geojson(geom)
            if not geom:
                continue
            d = density_of(props)
            geom["coordinates"] = round_coords(geom["coordinates"])
            feats.append({"type": "Feature",
                          "properties": {"d": round(d)},
                          "geometry": geom})
        offset += len(got)
        print(f"  {offset} dissemination areas")
        if not got or len(got) < PAGE:
            break

    if len(feats) < 100:
        raise SystemExit(f"Only {len(feats)} areas returned - refusing to overwrite good data.")

    # quantile breaks, so the colour scale reflects Toronto rather than a guess
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

    meta = {}
    if os.path.exists("data/meta.json"):
        meta = json.load(open("data/meta.json"))
    meta["density"] = {"areas": len(feats), "breaks": breaks,
                       "megabytes": round(mb, 2),
                       "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    with open("data/meta.json", "w") as fh:
        json.dump(meta, fh, indent=2)


if __name__ == "__main__":
    main()
