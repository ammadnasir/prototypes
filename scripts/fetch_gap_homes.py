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

MIN_GAP_AREA = 4e-6      # only chase gaps bigger than roughly 4 hectares
MAX_GAPS = 40
SIMPLIFY_DEG = 0.000015  # about 1.5 m, keeps building corners honest


def overpass(south, west, north, east, tries=3):
    q = f"""[out:json][timeout:180];
(way["building"~"^(house|detached|semidetached_house|terrace|houses|bungalow|apartments|residential|dormitory|condominium)$"]({south},{west},{north},{east}););
out geom;"""
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


def main():
    if not os.path.exists(GAP_FILE):
        raise SystemExit(f"{GAP_FILE} is missing - run the zoning job first.")
    gap = shape(json.load(open(GAP_FILE))["geometry"])
    parts = list(gap.geoms) if hasattr(gap, "geoms") else [gap]
    big = sorted([p for p in parts if p.area > MIN_GAP_AREA],
                 key=lambda p: -p.area)[:MAX_GAPS]
    print(f"{len(parts)} gaps, {len(big)} big enough to search")
    if not big:
        raise SystemExit("No sizeable gaps found.")

    hunted = unary_union(big)
    feats, seen = [], set()
    for i, part in enumerate(big, 1):
        w, s, e, n = part.bounds
        print(f"  gap {i}/{len(big)} bbox {round(w,3)},{round(s,3)},{round(e,3)},{round(n,3)}")
        data = overpass(s, w, n, e)
        for el in data.get("elements", []):
            if el.get("id") in seen or "geometry" not in el:
                continue
            kind = (el.get("tags") or {}).get("building", "")
            if kind not in WANTED:
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
            except Exception:                          # noqa: BLE001
                continue
            seen.add(el["id"])
            feats.append({"type": "Feature",
                          "properties": {"form": "houses" if kind in HOUSE else "apartments"},
                          "geometry": mapping(poly.simplify(SIMPLIFY_DEG, preserve_topology=True))})
        time.sleep(3)      # be polite to a shared public service

    print(f"  {len(feats)} residential buildings inside the gaps")
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
    meta["gap_homes"] = {"buildings": len(feats), "megabytes": round(mb, 2),
                         "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    json.dump(meta, open("data/meta.json", "w"), indent=2)


if __name__ == "__main__":
    main()
