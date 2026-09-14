#!/usr/bin/env python3
"""
Find residential buildings where the zoning map says nothing.

Writes data/homes-gap.geojson: building footprints inside the By-law 569-2013
gaps, tagged houses or apartments. This is what fills Fort York, CityPlace and
the Railway Lands, where the City's zoning layer is silent and the map has been
showing blank land that is in fact full of homes.

Source: OpenStreetMap via Overpass. Buildings are what they are regardless of
which by-law governs the block.
"""

import json
import os
import time
from urllib.parse import urlencode
from urllib.request import urlopen, Request

from shapely.geometry import shape, Polygon, mapping, box
from shapely.ops import unary_union

OVERPASS = "https://overpass-api.de/api/interpreter"
GAP_FILE = "data/zoning-nodata.geojson"
OUT = "data/homes-gap.geojson"

HOUSE = {"house", "detached", "semidetached_house", "terrace", "houses", "bungalow"}
APARTMENT = {"apartments", "residential", "dormitory", "condominium"}
WANTED = HOUSE | APARTMENT

# One query over the downtown window beats forty over scattered gaps: Overpass
# rate-limits per request, so the round trips cost far more than the data.
WINDOW = (43.612, -79.48, 43.695, -79.31)   # south, west, north, east
SIMPLIFY_DEG = 0.000015  # about 1.5 m, keeps building corners honest


def overpass(south, west, north, east, tries=3, kind="buildings"):
    area = f"({south},{west},{north},{east})"
    if kind == "buildings":
        q = f'[out:json][timeout:240];(way["building"]{area};);out geom;'
    else:
        q = ('[out:json][timeout:240];('
             f'way["leisure"~"^(park|garden|nature_reserve|recreation_ground)$"]{area};'
             f'way["landuse"~"^(grass|recreation_ground|village_green)$"]{area};'
             ');out geom;')
    for attempt in range(tries):
        try:
            req = Request(OVERPASS, data=urlencode({"data": q}).encode(),
                          headers={"User-Agent": "reach-map/1.0"})
            with urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode())
        except Exception as exc:                       # noqa: BLE001
            print(f"    attempt {attempt + 1} failed: {exc}")
            time.sleep(15 * (attempt + 1))
    return {"elements": []}


def classify(tags):
    kind = (tags or {}).get("building", "")
    if kind in HOUSE:
        return "houses"
    if kind in APARTMENT:
        lv = storeys(tags)
        if lv is None:
            return "apartments_unknown"        # counted, then resolved below
        return "towers" if lv >= 8 else "multiplex"
    if kind in ("commercial", "retail", "office", "industrial", "warehouse",
                "school", "university", "hospital", "church", "civic",
                "public", "hotel", "parking", "garage", "garages", "roof",
                "shed", "service", "train_station", "stadium"):
        return None                      # clearly not somewhere you live
    levels = storeys(tags)
    if kind in ("yes", "") and levels and levels >= 4:
        return "towers" if levels >= 8 else "multiplex"
    return "other"


def storeys(tags):
    for key in ("building:levels", "levels"):
        try:
            v = (tags or {}).get(key)
            if v:
                return float(str(v).split(";")[0])
        except ValueError:
            pass
    try:
        h = (tags or {}).get("height")
        if h:
            return float(str(h).replace("m", "").strip()) / 3.1
    except ValueError:
        pass
    return None


def main():
    if not os.path.exists(GAP_FILE):
        raise SystemExit(f"{GAP_FILE} is missing - run the zoning job first.")
    gap = shape(json.load(open(GAP_FILE))["geometry"])
    hunted = gap.intersection(box(WINDOW[1], WINDOW[0], WINDOW[3], WINDOW[2]))
    print(f"searching {round(hunted.area * 1e4, 1)} (deg^2 x 1e4) of zoning gap")

    data = overpass(*WINDOW)
    print(f"  {len(data.get('elements', []))} buildings returned")
    green = overpass(*WINDOW, kind="green")
    print(f"  {len(green.get('elements', []))} green spaces returned")

    feats, counts = [], {}
    for el in data.get("elements", []):
        if "geometry" not in el:
            continue
        form = classify(el.get("tags"))
        if form is None:
            continue
        ring = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(ring) < 4:
            continue
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or not poly.representative_point().within(hunted):
                continue
        except Exception:                              # noqa: BLE001
            continue
        if form == "apartments_unknown":
            form = "towers"      # downtown, an untagged apartment block is almost always tall
            counts["towers_assumed"] = counts.get("towers_assumed", 0) + 1
        counts[form] = counts.get(form, 0) + 1
        feats.append({"type": "Feature",
                      "properties": {"form": form},
                      "geometry": mapping(poly.simplify(SIMPLIFY_DEG, preserve_topology=True))})
    # parks and open space in the same gaps
    for el in green.get("elements", []):
        if "geometry" not in el:
            continue
        ring = [(p["lon"], p["lat"]) for p in el["geometry"]]
        if len(ring) < 4:
            continue
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or not poly.representative_point().within(hunted):
                continue
        except Exception:                              # noqa: BLE001
            continue
        counts["parks"] = counts.get("parks", 0) + 1
        feats.append({"type": "Feature",
                      "properties": {"form": "parks"},
                      "geometry": mapping(poly.simplify(SIMPLIFY_DEG, preserve_topology=True))})

    print(f"  inside the gaps: {counts}")

    if not feats:
        raise SystemExit("Found no buildings - refusing to overwrite good data.")

    os.makedirs("data", exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"type": "FeatureCollection",
                   "properties": {"source": "OpenStreetMap contributors",
                                  "meaning": "Residential buildings where no zoning is published",
                                  "generated": time.strftime("%Y-%m-%d")},
                   "features": feats}, fh, separators=(",", ":"))
    mb = os.path.getsize(OUT) / 1e6
    print(f"wrote {OUT} ({mb:.2f} MB)")

    meta = json.load(open("data/meta.json")) if os.path.exists("data/meta.json") else {}
    meta["gap_homes"] = {"buildings": len(feats), "by_form": counts, "megabytes": round(mb, 2),
                         "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    json.dump(meta, open("data/meta.json", "w"), indent=2)


if __name__ == "__main__":
    main()
