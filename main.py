from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Tuple, Dict, Any
import requests
from shapely.geometry import Polygon
from shapely.validation import make_valid
import xml.etree.ElementTree as ET

app = FastAPI(title="Cadastral overlap API", version="1.0.0")

WFS_URL = "https://services.cuzk.cz/wfs/inspire-cp-wfs.asp"

class OverlapRequest(BaseModel):
    coords: List[List[float]] = Field(..., description="Polygon vertices as [[x,y],...], EPSG:5514")
    include_touches: bool = Field(False, description="Include parcels that only touch boundary (overlap area = 0)")
    limit: int = Field(500, ge=1, le=5000, description="Max parcels to fetch in bbox (safety)")

def normalize_coords(coords: List[List[float]]) -> List[Tuple[float, float]]:
    pts = [(float(x), float(y)) for x, y in coords]
    # force negative
    pts = [(-abs(x), -abs(y)) for x, y in pts]
    # if abs(Y) > abs(X) is NOT true for most points, swap
    good = sum(1 for x, y in pts if abs(y) > abs(x))
    if good < max(1, len(pts) // 2):
        pts = [(y, x) for x, y in pts]
    # close
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    # drop consecutive duplicates
    cleaned = [pts[0]]
    for p in pts[1:]:
        if p != cleaned[-1]:
            cleaned.append(p)
    if len(cleaned) < 4:
        raise ValueError("Polygon has too few points after normalization.")
    return cleaned

def polygon_bbox(pts: List[Tuple[float, float]]):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))

def wfs_get_parcels_by_bbox(minx: float, miny: float, maxx: float, maxy: float, limit: int) -> str:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "cp:CadastralParcel",
        "srsName": "http://www.opengis.net/def/crs/EPSG/0/5514",
        "bbox": f"{minx},{miny},{maxx},{maxy},http://www.opengis.net/def/crs/EPSG/0/5514",
        "count": str(limit),
        "outputFormat": "text/xml; subtype=gml/3.2.1",
    }
    r = requests.get(WFS_URL, params=params, timeout=60)
    r.raise_for_status()
    return r.text

def parse_gml_polygon_to_shapely(polygon_elem: ET.Element) -> Polygon:
    ns = {"gml": "http://www.opengis.net/gml/3.2"}
    exterior = polygon_elem.find(".//gml:exterior//gml:posList", ns)
    if exterior is None or exterior.text is None:
        raise ValueError("No exterior posList found")
    coords = [float(v) for v in exterior.text.split()]
    pts = list(zip(coords[0::2], coords[1::2]))
    poly = Polygon(pts)

    holes = []
    for interior in polygon_elem.findall(".//gml:interior", ns):
        pos = interior.find(".//gml:posList", ns)
        if pos is not None and pos.text:
            c = [float(v) for v in pos.text.split()]
            ring = list(zip(c[0::2], c[1::2]))
            if len(ring) >= 4:
                holes.append(ring)

    if holes:
        poly = Polygon(poly.exterior.coords, holes)
    return poly

def read_wfs_members(xml_text: str) -> List[Dict[str, Any]]:
    root = ET.fromstring(xml_text)
    ns = {
        "wfs": "http://www.opengis.net/wfs/2.0",
        "gml": "http://www.opengis.net/gml/3.2",
        "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
        "xlink": "http://www.w3.org/1999/xlink",
    }
    out = []
    for member in root.findall("wfs:member", ns):
        parcel = member.find("cp:CadastralParcel", ns)
        if parcel is None:
            continue

        label = parcel.findtext("cp:label", default="", namespaces=ns)
        ncr = parcel.findtext("cp:nationalCadastralReference", default="", namespaces=ns)
        area_txt = parcel.findtext("cp:areaValue", default="", namespaces=ns)

        zoning = parcel.find("cp:zoning", ns)
        ku_name = zoning.get("{http://www.w3.org/1999/xlink}title") if zoning is not None else None
        ku_href = zoning.get("{http://www.w3.org/1999/xlink}href") if zoning is not None else None

        geom_poly = parcel.find(".//cp:geometry//gml:Polygon", ns)
        if geom_poly is None:
            continue

        try:
            shp = make_valid(parse_gml_polygon_to_shapely(geom_poly))
        except Exception:
            continue

        try:
            area_val = float(area_txt) if area_txt else None
        except Exception:
            area_val = None

        out.append(
            {
                "label": label,
                "nationalCadastralReference": ncr,
                "areaValue": area_val,
                "ku": {"name": ku_name, "href": ku_href},
                "geometry": shp,
            }
        )
    return out

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/overlap")
def overlap(req: OverlapRequest):
    try:
        pts = normalize_coords(req.coords)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    user_poly = make_valid(Polygon(pts))
    if user_poly.is_empty:
        raise HTTPException(status_code=400, detail="Polygon is empty/invalid.")

    minx, miny, maxx, maxy = polygon_bbox(list(user_poly.exterior.coords))

    xml_text = wfs_get_parcels_by_bbox(minx, miny, maxx, maxy, req.limit)
    parcels = read_wfs_members(xml_text)

    results = []
    ku_set = {}
    overlap_total = 0.0

    for p in parcels:
        g = p["geometry"]
        if g.is_empty:
            continue

        inter = user_poly.intersection(g)
        inter_area = float(inter.area) if not inter.is_empty else 0.0

        if inter_area > 0 or (req.include_touches and user_poly.intersects(g)):
            parcel_area = p["areaValue"] if p["areaValue"] is not None else float(g.area)
            pct = (inter_area / parcel_area * 100.0) if parcel_area > 0 else None

            ku_name = p["ku"]["name"]
            ku_href = p["ku"]["href"]
            if ku_name and ku_href:
                ku_set[(ku_name, ku_href)] = True

            results.append(
                {
                    "label": p["label"],
                    "nationalCadastralReference": p["nationalCadastralReference"],
                    "areaParcel": round(parcel_area, 1),
                    "overlap": round(inter_area, 1),
                    "pct": round(pct, 2) if pct is not None else None,
                    "kuName": ku_name,
                    "kuHref": ku_href,
                }
            )
            overlap_total += inter_area

    results.sort(key=lambda x: x["overlap"], reverse=True)
    ku_list = [{"name": k[0], "href": k[1]} for k in ku_set.keys()]

    return {
        "ku": ku_list,
        "parcels": results,
        "overlapTotal": round(overlap_total, 1),
        "polygonArea": round(float(user_poly.area), 1),
        "bbox": {"minX": minx, "minY": miny, "maxX": maxx, "maxY": maxy},
    }
