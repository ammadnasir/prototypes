#!/usr/bin/env python3
"""
Snapshot Toronto zoning into two dissolved polygons:

  data/zoning-residential.geojson  land where dwellings are permitted
  data/zoning-nodata.geojson       land inside Toronto that By-law 569-2013
                                   doesn't cover, so nothing can be said about it

The second file exists because absence of zoning is not the same as a prohibition.
215 Fort York Blvd sits in one of these holes, governed by a former by-law.

Runs in GitHub Actions. Source: City of Toronto, Zoning Area and City Ward layers.
"""

import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from shapely.geometry import mapping, box, Polygon
from shapely.ops import unary_union

BASE = "https://gis.toronto.ca/arcgis/rest/services/cot_geospatial11/FeatureServer"
ZONING, WARDS = f"{BASE}/3/query", f"{BASE}/0/query"

# Everything reachable from 215 Fort York Blvd in 60 minutes, generously bounded.
BBOX = (-79.58, 43.58, -79.26, 43.80)

# Split by built form, because "where can I buy a house" and "where can I buy a
# condo" are different questions with very different answers in Toronto.
FORMS = {
    "houses":     {"R", "RD", "RS", "RT"},                  # detached, semi, town
    "apartments": {"RM", "RA"},                             # multiplex and apartment
    "mixed":      {"CR", "CRE"},                            # dwellings above commercial
    "parks":      {"O", "ON", "OR", "OG", "OM", "OC"},      # open space and parkland
}
HOME = {"R", "RD", "RS", "RT", "RM", "RA", "CR", "CRE"}
OTHER = {"CL", "C", "EL", "EH", "EO", "E", "IH", "IPU", "IE", "I",
         "ON", "OR", "OG", "OM", "OC", "O", "UT"}   # everything the map leaves blank
ALL = HOME | OTHER

PAGE = 1000
SIMPLIFY_DEG = 0.00008        # about 8 m
MIN_HOLE_DEG2 = 2e-7          # drop slivers, roughly 2,000 m2


def get(url, params, tries=4):
    full = url + "?" + urlencode(params)
    for attempt in range(tries):
        try:
            with urlopen(full, timeout=180) as r:
                return json.loads(r.read().decode())
        except Exception as exc:                       # noqa: BLE001
            if attempt == tries - 1:
                raise
            print(f"  retry {attempt + 1} after {exc}")
            time.sleep(3 * (attempt + 1))


def base_params(**extra):
    p = {
        "geometry": ",".join(map(str, BBOX)),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326", "outSR": "4326",
        "geometryPrecision": "6",
        "f": "json",
    }
    p.update(extra)
    return p


def find_zone_field():
    """The layer's field naming isn't documented, so infer it from a sample."""
    sample = get(ZONING, base_params(where="1=1", outFields="*",
                                     returnGeometry="false", resultRecordCount="50"))
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


def fetch_polygons(url, where, label, out_field="OBJECTID"):
    geoms, offset = [], 0
    while True:
        page = get(url, base_params(where=where,
                                    outFields=out_field,
                                    returnGeometry="true",
                                    maxAllowableOffset="0.00005",
                                    resultOffset=str(offset),
                                    resultRecordCount=str(PAGE)))
        feats = page.get("features", [])
        for feat in feats:
            geoms.extend(esri_to_shapely(feat.get("geometry") or {}))
        offset += len(feats)
        print(f"  {label}: {offset} features")
        if not feats or (not page.get("exceededTransferLimit") and len(feats) < PAGE):
            break
    return geoms


def dissolve(geoms):
    clean = [g if g.is_valid else g.buffer(0) for g in geoms]
    return unary_union([g for g in clean if not g.is_empty])


def write(path, geom, props):
    with open(path, "w") as fh:
        json.dump({"type": "Feature", "properties": props, "geometry": mapping(geom)},
                  fh, separators=(",", ":"))
    mb = os.path.getsize(path) / 1e6
    print(f"wrote {path} ({mb:.2f} MB)")
    return round(mb, 2)


def main():
    field = find_zone_field()

    def in_list(codes):
        return f"{field} IN (" + ",".join(f"'{z}'" for z in sorted(codes)) + ")"

    by_form, homes_raw = {}, []
    for name, codes in FORMS.items():
        raw = fetch_polygons(ZONING, in_list(codes), name, field)
        by_form[name] = raw
        if name != "parks":
            homes_raw += raw
    if not homes_raw:
        raise SystemExit("No residential zoning returned - refusing to overwrite good data.")
    other_raw = fetch_polygons(ZONING, in_list(OTHER), "other zones", field)
    wards_raw = fetch_polygons(WARDS, "1=1", "wards")

    print("dissolving")
    homes = dissolve(homes_raw).simplify(SIMPLIFY_DEG, preserve_topology=True)
    covered = dissolve(homes_raw + other_raw)
    city = dissolve(wards_raw) if wards_raw else box(*BBOX)

    # Toronto land inside our window that the by-law says nothing about
    window = box(*BBOX).intersection(city)
    nodata = window.difference(covered).simplify(SIMPLIFY_DEG, preserve_topology=True)
    if nodata.geom_type == "MultiPolygon":
        parts = [p for p in nodata.geoms if p.area > MIN_HOLE_DEG2]
        if parts:
            nodata = unary_union(parts)

    os.makedirs("data", exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    # one file, one feature per built form
    forms_fc = {"type": "FeatureCollection", "properties": {
        "source": "City of Toronto, Zoning By-law 569-2013", "generated": stamp},
        "features": []}
    for name, raw in by_form.items():
        if not raw:
            continue
        geom = dissolve(raw).simplify(SIMPLIFY_DEG, preserve_topology=True)
        forms_fc["features"].append({
            "type": "Feature",
            "properties": {"form": name, "categories": sorted(FORMS[name]),
                           "parcels": len(raw)},
            "geometry": mapping(geom)})
    with open("data/zoning-forms.geojson", "w") as fh:
        json.dump(forms_fc, fh, separators=(",", ":"))
    mb_f = round(os.path.getsize("data/zoning-forms.geojson") / 1e6, 2)
    print(f"wrote data/zoning-forms.geojson ({mb_f} MB)")

    # kept so an older deploy of the page keeps working
    mb_h = write("data/zoning-residential.geojson", homes, {
        "source": "City of Toronto, Zoning By-law 569-2013",
        "meaning": "Dwellings are permitted here.",
        "categories": sorted(HOME), "generated": stamp})
    mb_n = write("data/zoning-nodata.geojson", nodata, {
        "source": "City of Toronto, Zoning By-law 569-2013",
        "meaning": "Not covered by By-law 569-2013. Former by-laws apply; "
                   "this is unknown, not prohibited.",
        "generated": stamp})

    with open("data/meta.json", "w") as fh:
        json.dump({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "zone_field": field,
            "parcels_residential": len(homes_raw),
            "parcels_by_form": {k: len(v) for k, v in by_form.items()},
            "parcels_other": len(other_raw),
            "ward_polygons": len(wards_raw),
            "megabytes": {"forms": mb_f, "residential": mb_h, "nodata": mb_n},
            "bbox": list(BBOX),
            "categories_residential": sorted(HOME),
        }, fh, indent=2)
    print("wrote data/meta.json")

    if mb_f + mb_h + mb_n > 40:
        print("warning: large for static assets, consider raising SIMPLIFY_DEG", file=sys.stderr)


if __name__ == "__main__":
    main()
