"""Pobiera z OSM sieć pieszą i budynki centrum Rzeszowa, zapisuje do cache/rzeszow.json.
Współrzędne lokalne w metrach (x na wschód, y na północ) względem CENTER."""
import json
import math
import pathlib

CENTER = (50.0374, 22.0047)  # Rynek, Rzeszów
DIST = 800  # promień obszaru [m]
CACHE = pathlib.Path(__file__).parent / "cache" / "rzeszow.json"
K = 111320.0
KX = K * math.cos(math.radians(CENTER[0]))


def to_xy(lat, lon):
    return (lon - CENTER[1]) * KX, (lat - CENTER[0]) * K


def to_latlon(x, y):
    return CENTER[0] + y / K, CENTER[1] + x / KX


def _area_centroid(xy):
    a = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]):
        f = x1 * y2 - x2 * y1
        a += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    a /= 2
    if abs(a) < 1e-6:
        return 0.0, xy[0][0], xy[0][1]
    return abs(a), cx / (6 * a), cy / (6 * a)


def default_bbox():
    """Domyślny obszar ewakuacji: kwadrat 2*DIST wokół Rynku, jako (south, west, north, east)."""
    return (CENTER[0] - DIST / K, CENTER[1] - DIST / KX, CENTER[0] + DIST / K, CENTER[1] + DIST / KX)


def area_key(bbox):
    return None if bbox is None else "area_" + "_".join(f"{v:.4f}" for v in bbox)


def bbox_xy(bbox):
    """(south, west, north, east) -> (x0, x1, y0, y1) w metrach lokalnych."""
    x0, y0 = to_xy(bbox[0], bbox[1])
    x1, y1 = to_xy(bbox[2], bbox[3])
    return x0, x1, y0, y1


def _cache(bbox, suffix=""):
    return CACHE if bbox is None and not suffix else CACHE.parent / f"{area_key(bbox) or 'rzeszow'}{suffix}.json"


def build(bbox=None):
    """Sieć piesza i budynki OSM; bbox=None -> domyślny obszar (centrum Rzeszowa)."""
    try:
        import truststore
        truststore.inject_into_ssl()  # certyfikaty z magazynu systemowego
    except ImportError:
        pass
    import osmnx as ox

    if bbox is None:
        G = ox.graph_from_point(CENTER, dist=DIST, network_type="walk", simplify=False)
    else:
        s_, w_, n_, e_ = bbox
        G = ox.graph_from_bbox((w_, s_, e_, n_), network_type="walk", simplify=False)
    G = ox.convert.to_undirected(G)
    ids = {n: i for i, n in enumerate(G.nodes)}
    nodes = [[round(v, 1) for v in to_xy(d["y"], d["x"])] for _, d in G.nodes(data=True)]
    edges = sorted({(min(ids[u], ids[v]), max(ids[u], ids[v]), round(d["length"], 1))
                    for u, v, d in G.edges(data=True) if u != v})

    gdf = ox.features_from_point(CENTER, tags={"building": True}, dist=DIST) if bbox is None else \
        ox.features_from_bbox((bbox[1], bbox[0], bbox[3], bbox[2]), tags={"building": True})
    buildings = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom.geom_type == "MultiPolygon":
            geom = max(geom.geoms, key=lambda g: g.area)
        if geom.geom_type != "Polygon":
            continue
        ll = [(lat, lon) for lon, lat in geom.exterior.coords[:-1]]
        area, cx, cy = _area_centroid([to_xy(*p) for p in ll])

        def tag(k):
            v = row.get(k)
            return v if isinstance(v, str) else None

        buildings.append({
            "id": f"{idx[0][0]}{idx[1]}",
            "name": tag("name"),
            "type": tag("building"),
            "levels": tag("building:levels"),
            "area": round(area),
            "cx": round(cx, 1), "cy": round(cy, 1),
            "poly": [[round(a, 6), round(b, 6)] for a, b in ll],
        })

    data = {"center": CENTER, "dist": DIST, "nodes": nodes, "edges": edges, "buildings": buildings,
            "bbox": list(bbox or default_bbox())}
    CACHE.parent.mkdir(exist_ok=True)
    _cache(bbox).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    return data


def load(bbox=None):
    f = _cache(bbox)
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        d.setdefault("bbox", list(bbox or default_bbox()))
        return d
    return build(bbox)


# ---------------- BDOT10k (GUGiK) ----------------
BDOT_URL = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/18/1863_GML.zip"  # powiat m. Rzeszów
BDOT_ZIP = CACHE.parent / "1863_GML.zip"
BDOT_CACHE = CACHE.parent / "rzeszow_bdot.json"

# funkcja szczegółowa BDOT10k -> typ zgodny z wagami POI (search.py)
BDOT_TYPE = {
    "dworzec kolejowy": "train_station", "dworzec autobusowy": "transportation",
    "szkoła podstawowa": "school", "szkoła ponadpodstawowa": "school", "szkoła wyższa": "university",
    "przedszkole": "kindergarten", "żłobek": "kindergarten", "internat lub bursa szkolna": "school",
    "centrum handlowe": "retail", "dom towarowy lub handlowy": "retail", "hipermarket lub supermarket": "supermarket",
    "obiekt handlowo-usługowy": "commercial", "restauracja": "commercial", "szpital": "hospital",
    "placówka ochrony zdrowia": "hospital", "kościół": "church", "kaplica": "church", "urząd miasta": "civic",
    "urząd marszałkowski": "civic", "urząd wojewódzki": "civic", "sąd": "civic", "prokuratura": "civic",
    "inny urząd administracji publicznej": "public", "dom kultury": "public", "biblioteka": "public",
    "muzeum": "public", "teatr": "public", "kino": "public", "hotel": "hotel", "siedziba firmy lub firm": "office",
    "bank": "office", "hala sportowa": "sports_hall", "dom studencki": "school",
    "budynek jednorodzinny": "house", "budynek wielorodzinny": "apartments", "garaż": "garage",
    "budynek gospodarczy": "shed",
}
BDOT_GEN = {"budynki biurowe": "office", "budynki handlowo-usługowe": "commercial",
            "budynki oświaty, nauki i kultury oraz budynki sportowe": "school",
            "budynki szpitali i inne budynki opieki zdrowotnej": "hospital", "budynki mieszkalne": "residential"}
# budynki, które z racji konstrukcji/funkcji są kandydatami na miejsce schronienia (heurystyka)
SHELTER_FN = {"parking wielopoziomowy", "centrum handlowe", "dom towarowy lub handlowy", "hipermarket lub supermarket",
              "szkoła podstawowa", "szkoła ponadpodstawowa", "szkoła wyższa", "szpital", "hala sportowa",
              "urząd miasta", "urząd marszałkowski", "urząd wojewódzki", "inny urząd administracji publicznej",
              "dom kultury", "kościół", "dworzec kolejowy", "dworzec autobusowy", "koszary", "policja", "straż pożarna"}


def build_bdot(bbox=None):
    """Budynki z BDOT10k (warstwa OT_BUBD_A, układ EPSG:2180) w obszarze symulacji (tylko powiat m. Rzeszów)."""
    import urllib.request
    import xml.etree.ElementTree as ET
    import zipfile

    from pyproj import Transformer

    if not BDOT_ZIP.exists():
        import ssl
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with urllib.request.urlopen(BDOT_URL, timeout=300, context=ctx) as r:
            BDOT_ZIP.write_bytes(r.read())
    tr = Transformer.from_crs(2180, 4326, always_xy=True)  # posList: easting northing
    lim = DIST * 1.15
    X0, X1, Y0, Y1 = bbox_xy(bbox) if bbox else (-lim, lim, -lim, lim)
    z = zipfile.ZipFile(BDOT_ZIP)
    name = next(n for n in z.namelist() if n.endswith("OT_BUBD_A.xml"))
    out = []
    for _, el in ET.iterparse(z.open(name)):
        if not el.tag.endswith("}OT_BUBD_A"):
            continue
        pos = el.findtext(".//{*}exterior//{*}posList")
        if pos and el.findtext("{*}kategoriaIstnienia") == "eksploatowany":
            v = list(map(float, pos.split()))
            lon, lat = tr.transform(v[0::2], v[1::2])
            ll = list(zip(lat, lon))[:-1]
            area, cx, cy = _area_centroid([to_xy(*p) for p in ll])
            if X0 <= cx <= X1 and Y0 <= cy <= Y1 and area > 5:
                fs = el.findtext("{*}funkcjaSzczegolowaBudynku") or el.findtext("{*}przewazajacaFunkcjaBudynku")
                fo = el.findtext("{*}funkcjaOgolnaBudynku")
                lv = el.findtext("{*}liczbaKondygnacji")
                lvn = int(lv) if (lv or "").isdigit() else 1
                cand = area >= 300 and (fs in SHELTER_FN or (fs == "budynek wielorodzinny" and lvn >= 4))
                out.append({
                    "id": el.findtext("{*}lokalnyId"), "name": el.findtext("{*}nazwa"),
                    "type": BDOT_TYPE.get(fs) or BDOT_GEN.get(fo, "yes"), "func": fs or fo, "levels": lv,
                    "area": round(area), "cx": round(cx, 1), "cy": round(cy, 1), "cand": cand, "src": "bdot",
                    "poly": [[round(a, 6), round(b, 6)] for a, b in ll],
                })
        el.clear()
    (BDOT_CACHE if bbox is None else _cache(bbox, "_bdot")).write_text(
        json.dumps(out, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return out


def load_bdot(bbox=None):
    f = BDOT_CACHE if bbox is None else _cache(bbox, "_bdot")
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return build_bdot(bbox)


# ---------------- Punkty schronienia (KG PSP, dane.gov.pl, CC BY 4.0) ----------------
PSP_URL = "https://gdziesieukryc.pl/PS_XML/punkty_schronienia.csv"
PSP_CSV = CACHE.parent / "punkty_schronienia.csv"


def load_psp(bbox=None):
    """Oficjalne punkty schronienia PSP w obszarze symulacji (domyślnie: Rzeszów, centrum)."""
    import csv
    if not PSP_CSV.exists():
        import ssl
        import urllib.request
        import truststore
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with urllib.request.urlopen(PSP_URL, timeout=300, context=ctx) as r:
            PSP_CSV.write_bytes(r.read())
    lim = DIST * 1.05
    out = []
    with open(PSP_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if bbox is None and r["Gmina"] != "Rzeszów":
                continue
            try:
                lat, lon = float(r["Szerokosc geograficzna"]), float(r["Dlugosc geograficzna"])
            except ValueError:
                continue
            if bbox is not None and not (bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]):
                continue
            x, y = to_xy(lat, lon)
            if bbox is not None or (abs(x) < lim and abs(y) < lim):
                out.append({"id": r["Identyfikator publiczny"], "addr": r["Adres"].replace(", Rzeszów", ""),
                            "access": r["Dostepnosc"], "lat": lat, "lon": lon, "x": round(x, 1), "y": round(y, 1)})
    return out


if __name__ == "__main__":
    d = build()
    print(f"węzły: {len(d['nodes'])}, krawędzie: {len(d['edges'])}, budynki: {len(d['buildings'])}")
