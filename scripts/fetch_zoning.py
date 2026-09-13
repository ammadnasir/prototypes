#!/usr/bin/env python3
"""
Snapshot Toronto's residential-capable zoning into one dissolved polygon.

Runs in GitHub Actions, not in the browser. Writes data/zoning-residential.geojson,
which the map intersects against its reach shape. Doing the dissolve here means the
page downloads one clean multipolygon instead of thousands of parcels.

Source: City of Toronto, Zoning By-law 569-2013 (Zoning Area layer).
"""

import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from shapely.geometry import shape, mapping
from shapely.ops import unary_union

URL = "https://gis.toronto.ca/arcgis/rest/services/cot_geospatial11/FeatureServer/3/query"

# Everything reachable from 215 Fort York Blvd in 60 minutes, generously bounded.
BBOX = (-79.58, 43.58, -79.26, 43.80)

# Zone categories that permit dwellings.
HOME = {"R", "RD", "RS", "RT", "RM", "RA", "CR", "CRE"}
# Every category, used to work out which field holds the zone code.
ALL = HOME | {"CL", "C", "EL", "EH", "EO", "E", "IH", "IPU", "IE", "I",
              "ON", "OR", "OG", "OM", "OC", "O", "UT"}

PAGE = 1000
SIMPLIFY_DEG = 0.00008     # roughly 8 m, enough to shed vertices without moving edges
OUT = "data/zoning-residential.geojson"


def get(params, tries=4):
    url = URL + "?" + urlencode(params)
    for attempt in range(tries):
        try:
            with urlopen(url, timeout=120) as r:
                return json.loads(r.read().decode())
        except Exception as exc:                       # noqa: BLE001
            if attempt == tries - 1:
                raise
            print(f"  retry {attempt + 1} after {exc}")
            time.sleep(3 * (attempt + 1))


def find_zone_field():
    """The layer's field naming isn't documented, so infer it from a sample."""
    sample = get({
        "where": "1=1",
        "geometry": ",".join(map(str, BBOX)),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326", "outSR": "4326",
        "outFields": "*", "returnGeometry": "false",
        "resultRecordCount": "50", "f": "json",
    })
    hits = {}
    for feat in sample.get("features", []):
        for key, val in (feat.get("attributes") or {}).items():
            if isinstance(val, str) and val.strip().upper() in ALL:
                hits[key] = hits.get(key, 0) + 1
    if not hits:
        raise SystemExit("Could not find a zone-code field. Fields seen: "
                         + ", ".join(f["name"] for f in sample.get("fields", [])))
    field = max(hits, key=hits.get)
    print(f"zone field: {field}")
    return field


def esri_to_shapely(geom):
    """Esri rings: clockwise is an outer ring, counter-clockwise is a hole."""
    from shapely.geometry import Polygon
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
    return polys


def fetch_all(field):
    where = f"{field} IN (" + ",".join(f"'{z}'" for z in sorted(HOME)) + ")"
    geoms, offset = [], 0
    while True:
        page = get({
            "where": where,
            "geometry": ",".join(map(str, BBOX)),
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": field,
            "returnGeometry": "true",
            "maxAllowableOffset": "0.00005",
            "geometryPrecision": "6",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE),
            "f": "json",
        })
        feats = page.get("features", [])
        for feat in feats:
            geoms.extend(esri_to_shapely(feat.get("geometry") or {}))
        print(f"  {offset + len(feats)} features")
        if not page.get("exceededTransferLimit") and len(feats) < PAGE:
            break
        offset += len(feats)
        if not feats:
            break
    return geoms


def main():
    field = find_zone_field()
    geoms = [g for g in fetch_all(field) if g.is_valid or g.buffer(0).is_valid]
    if not geoms:
        raise SystemExit("No residential zoning returned — refusing to overwrite good data.")

    print(f"dissolving {len(geoms)} parcels")
    merged = unary_union([g if g.is_valid else g.buffer(0) for g in geoms])
    merged = merged.simplify(SIMPLIFY_DEG, preserve_topology=True)

    os.makedirs("data", exist_ok=True)
    out = {
        "type": "Feature",
        "properties": {
            "source": "City of Toronto, Zoning By-law 569-2013",
            "categories": sorted(HOME),
            "note": "Areas outside By-law 569-2013 are absent, not unzoned.",
            "generated": time.strftime("%Y-%m-%d"),
        },
        "geometry": mapping(merged),
    }
    with open(OUT, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    size = os.path.getsize(OUT) / 1e6
    print(f"wrote {OUT} ({size:.1f} MB)")
    if size > 40:
        print("warning: large for a static asset, consider raising SIMPLIFY_DEG", file=sys.stderr)


if __name__ == "__main__":
    main()
