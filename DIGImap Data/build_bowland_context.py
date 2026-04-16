import json
import math
import os
import sqlite3
import struct


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOWLAND_DIR = os.path.join(ROOT, "data", "DIGImap Data", "Bowland")
BOUNDARY_PATH = os.path.join(BOWLAND_DIR, "VS code Bowland Forest.json")
OUT_DIR = os.path.join(ROOT, "data", "processed")
OUT_PATH = os.path.join(OUT_DIR, "bowland_context.json")

TERRAIN_ASC = [
    os.path.join(BOWLAND_DIR, "Download_2962652", "terrain-5-dtm_6363872", "sd", "SD65SW.asc"),
    os.path.join(BOWLAND_DIR, "Download_2962652", "terrain-5-dtm_6363872", "sd", "SD65SE.asc"),
]

MASTERMAP_TOPO = os.path.join(
    BOWLAND_DIR,
    "Download_2962652",
    "mastermap-topo_6363866",
    "mastermap-topo_6363866.gpkg",
)

VML = [
    os.path.join(BOWLAND_DIR, "Download_2962652", "vml_6363875", "sd", "vml-sd65sw.gpkg"),
    os.path.join(BOWLAND_DIR, "Download_2962652", "vml_6363875", "sd", "vml-sd65se.gpkg"),
]


def read_boundary():
    with open(BOUNDARY_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    coords = data["features"][0]["geometry"]["coordinates"][0]
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return {
        "type": "Polygon",
        "coordinates": coords,
        "bounds": {
            "minX": min(xs),
            "minY": min(ys),
            "maxX": max(xs),
            "maxY": max(ys),
        },
    }


def padded_bounds(bounds, pad):
    return {
        "minX": bounds["minX"] - pad,
        "minY": bounds["minY"] - pad,
        "maxX": bounds["maxX"] + pad,
        "maxY": bounds["maxY"] + pad,
    }


def bbox_intersects(a, b):
    return not (a["maxX"] < b["minX"] or a["minX"] > b["maxX"] or a["maxY"] < b["minY"] or a["minY"] > b["maxY"])


def geom_bounds(geom):
    pts = []

    def walk(value):
        if not isinstance(value, list):
            return
        if len(value) >= 2 and isinstance(value[0], (int, float)) and isinstance(value[1], (int, float)):
            pts.append(value)
            return
        for child in value:
            walk(child)

    walk(geom["coordinates"])
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"minX": min(xs), "minY": min(ys), "maxX": max(xs), "maxY": max(ys)}


def gpkg_wkb(blob):
    if blob is None:
        return None
    if blob[:2] == b"GP":
        flags = blob[3]
        envelope_code = (flags >> 1) & 7
        envelope_lengths = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
        offset = 8 + envelope_lengths.get(envelope_code, 0)
        blob = blob[offset:]
    return parse_wkb(blob, 0)[0]


def parse_wkb(data, offset):
    endian = "<" if data[offset] == 1 else ">"
    offset += 1
    gtype = struct.unpack(endian + "I", data[offset : offset + 4])[0]
    offset += 4
    gtype = gtype % 1000

    if gtype == 1:
        x, y = struct.unpack(endian + "dd", data[offset : offset + 16])
        return {"type": "Point", "coordinates": [round(x, 2), round(y, 2)]}, offset + 16

    if gtype == 2:
        n = struct.unpack(endian + "I", data[offset : offset + 4])[0]
        offset += 4
        coords = []
        for _ in range(n):
            x, y = struct.unpack(endian + "dd", data[offset : offset + 16])
            offset += 16
            coords.append([round(x, 2), round(y, 2)])
        return {"type": "LineString", "coordinates": coords}, offset

    if gtype == 3:
        n = struct.unpack(endian + "I", data[offset : offset + 4])[0]
        offset += 4
        rings = []
        for _ in range(n):
            m = struct.unpack(endian + "I", data[offset : offset + 4])[0]
            offset += 4
            ring = []
            for _ in range(m):
                x, y = struct.unpack(endian + "dd", data[offset : offset + 16])
                offset += 16
                ring.append([round(x, 2), round(y, 2)])
            rings.append(ring)
        return {"type": "Polygon", "coordinates": rings}, offset

    if gtype in (4, 5, 6, 7):
        n = struct.unpack(endian + "I", data[offset : offset + 4])[0]
        offset += 4
        geoms = []
        for _ in range(n):
            child, offset = parse_wkb(data, offset)
            geoms.append(child)
        names = {4: "MultiPoint", 5: "MultiLineString", 6: "MultiPolygon", 7: "GeometryCollection"}
        if gtype == 5:
            return {"type": names[gtype], "coordinates": [g["coordinates"] for g in geoms]}, offset
        if gtype == 6:
            return {"type": names[gtype], "coordinates": [g["coordinates"] for g in geoms]}, offset
        return {"type": names[gtype], "geometries": geoms}, offset

    return None, offset


def simplify_line(coords, step=3):
    if len(coords) <= 8:
        return coords
    kept = coords[::step]
    if kept[-1] != coords[-1]:
        kept.append(coords[-1])
    return kept


def simplify_geom(geom):
    if geom["type"] == "LineString":
        return {"type": "LineString", "coordinates": simplify_line(geom["coordinates"])}
    if geom["type"] == "Polygon":
        return {"type": "Polygon", "coordinates": [simplify_line(ring, 3) for ring in geom["coordinates"]]}
    if geom["type"] == "MultiLineString":
        return {"type": "MultiLineString", "coordinates": [simplify_line(line, 3) for line in geom["coordinates"]]}
    if geom["type"] == "MultiPolygon":
        return {
            "type": "MultiPolygon",
            "coordinates": [[simplify_line(ring, 3) for ring in poly] for poly in geom["coordinates"]],
        }
    return geom


def classify_mastermap_area(group, term):
    text = f"{group or ''} {term or ''}".lower()
    if "building" in text:
        return "building"
    if "water" in text:
        return "water"
    if "road" in text or "track" in text or "path" in text:
        return "track"
    if "tree" in text or "wood" in text or "scrub" in text:
        return "forest"
    if "grass" in text or "heath" in text or "agricultural" in text:
        return "grass"
    if "cliff" in text or "boulder" in text:
        return "rock"
    return "surface"


def classify_vml_area(desc):
    text = (desc or "").lower()
    if "building" in text:
        return "building"
    if "water" in text:
        return "water"
    if "woodland" in text or "shrub" in text:
        return "forest"
    if "grass" in text or "heath" in text or "marsh" in text:
        return "grass"
    if "rock" in text or "boulder" in text or "shingle" in text:
        return "rock"
    return "surface"


def add_feature(collection, geom, props, clip_bounds, limit=None):
    if geom is None:
        return
    bounds = geom_bounds(geom)
    if not bounds or not bbox_intersects(bounds, clip_bounds):
        return
    if limit is not None and len(collection) >= limit:
        return
    collection.append({"geometry": simplify_geom(geom), "properties": props})


def read_mastermap(clip_bounds):
    layers = {"areas": [], "lines": []}
    if not os.path.exists(MASTERMAP_TOPO):
        return layers
    con = sqlite3.connect(MASTERMAP_TOPO)
    for geom_blob, group, term in con.execute("select geom, descriptivegroup, descriptiveterm from Topographicarea"):
        geom = gpkg_wkb(geom_blob)
        kind = classify_mastermap_area(group, term)
        add_feature(layers["areas"], geom, {"kind": kind, "source": "MasterMap", "label": term or group or kind}, clip_bounds)

    for geom_blob, group, term in con.execute("select geom, descriptivegroup, descriptiveterm from Topographicline"):
        geom = gpkg_wkb(geom_blob)
        text = f"{group or ''} {term or ''}".lower()
        if "water" in text:
            kind = "stream"
        elif "path" in text:
            kind = "path"
        elif "road" in text or "track" in text:
            kind = "road"
        elif "cliff" in text:
            kind = "landform"
        else:
            kind = "boundary"
        add_feature(layers["lines"], geom, {"kind": kind, "source": "MasterMap", "label": term or group or kind}, clip_bounds)
    con.close()
    return layers


def read_vml(clip_bounds):
    layers = {"areas": [], "lines": []}
    for path in VML:
        if not os.path.exists(path):
            continue
        con = sqlite3.connect(path)
        prefix = os.path.basename(path).replace(".gpkg", "")
        area_tables = [
            (f"{prefix}_Landform_Area", "landform"),
            (f"{prefix}_Water_Area", "water"),
            (f"{prefix}_Building", "building"),
        ]
        line_tables = [
            (f"{prefix}_Road_Centreline", "road"),
            (f"{prefix}_Water_Line", "stream"),
            (f"{prefix}_Height_Contours", "contour"),
        ]
        for table, fallback in area_tables:
            for geom_blob, desc in con.execute(f"select geom, FeatDesc from '{table}'"):
                geom = gpkg_wkb(geom_blob)
                kind = classify_vml_area(desc) if fallback == "landform" else fallback
                add_feature(layers["areas"], geom, {"kind": kind, "source": "VectorMap Local", "label": desc or kind}, clip_bounds, 450)
        for table, kind in line_tables:
            extra = ", roadName" if kind == "road" else ""
            for row in con.execute(f"select geom, FeatDesc{extra} from '{table}'"):
                geom = gpkg_wkb(row[0])
                label = row[2] or row[1] if kind == "road" and len(row) > 2 else row[1]
                add_feature(layers["lines"], geom, {"kind": kind, "source": "VectorMap Local", "label": label or kind}, clip_bounds, 650)
        con.close()
    return layers


class AscTile:
    def __init__(self, path):
        self.path = path
        with open(path, "r", encoding="utf-8") as f:
            self.ncols = int(f.readline().split()[1])
            self.nrows = int(f.readline().split()[1])
            self.xll = float(f.readline().split()[1])
            self.yll = float(f.readline().split()[1])
            self.cell = float(f.readline().split()[1])
            self.values = [[float(v) for v in line.split()] for line in f if line.strip()]
        self.bounds = {
            "minX": self.xll,
            "minY": self.yll,
            "maxX": self.xll + self.ncols * self.cell,
            "maxY": self.yll + self.nrows * self.cell,
        }

    def sample(self, x, y):
        if x < self.bounds["minX"] or x >= self.bounds["maxX"] or y < self.bounds["minY"] or y >= self.bounds["maxY"]:
            return None
        col = int((x - self.xll) / self.cell)
        row = self.nrows - 1 - int((y - self.yll) / self.cell)
        if row < 0 or row >= self.nrows or col < 0 or col >= self.ncols:
            return None
        return self.values[row][col]


def build_terrain(clip_bounds):
    tiles = [AscTile(path) for path in TERRAIN_ASC if os.path.exists(path)]
    spacing = 25
    min_x = math.floor(clip_bounds["minX"] / spacing) * spacing
    max_x = math.ceil(clip_bounds["maxX"] / spacing) * spacing
    min_y = math.floor(clip_bounds["minY"] / spacing) * spacing
    max_y = math.ceil(clip_bounds["maxY"] / spacing) * spacing
    rows = []
    z_values = []
    y = max_y
    while y >= min_y:
        row = []
        x = min_x
        while x <= max_x:
            z = None
            for tile in tiles:
                z = tile.sample(x, y)
                if z is not None:
                    break
            if z is not None:
                z = round(z, 2)
                z_values.append(z)
            row.append(z)
            x += spacing
        rows.append(row)
        y -= spacing
    return {
        "spacing": spacing,
        "origin": {"x": min_x, "y": max_y},
        "cols": len(rows[0]) if rows else 0,
        "rows": len(rows),
        "zMin": min(z_values) if z_values else None,
        "zMax": max(z_values) if z_values else None,
        "values": rows,
        "source": "OS Terrain 5 DTM ASCII grid, 5 m source sampled to 25 m",
    }


def merge_layers(*items):
    areas = []
    lines = []
    for item in items:
        areas.extend(item["areas"])
        lines.extend(item["lines"])
    return {"areas": areas, "lines": lines}


def main():
    boundary = read_boundary()
    context_bounds = padded_bounds(boundary["bounds"], 350)
    terrain = build_terrain(context_bounds)
    mastermap = read_mastermap(context_bounds)
    vml = read_vml(context_bounds)
    vectors = merge_layers(vml, mastermap)

    data = {
        "crs": "OSGB 1936 / British National Grid EPSG:27700",
        "boundary": boundary,
        "bounds": context_bounds,
        "terrain": terrain,
        "vectors": vectors,
        "inventory": {
            "areas": len(vectors["areas"]),
            "lines": len(vectors["lines"]),
            "sources": [
                "VS code Bowland Forest.json boundary",
                "OS Terrain 5 DTM ASCII grid",
                "OS MasterMap Topography GeoPackage",
                "OS VectorMap Local GeoPackage",
            ],
        },
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    print(json.dumps(data["inventory"], indent=2))
    print(f"terrain {terrain['cols']} cols x {terrain['rows']} rows, z {terrain['zMin']}m to {terrain['zMax']}m")
    print(OUT_PATH)


if __name__ == "__main__":
    main()
