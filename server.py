#!/usr/bin/env python3
"""
Field Survey — a standalone plant inventory for one piece of land.

Run it:      python server.py
Then open:   the URL it prints. Scan the QR with your phone to take it outside.

Why it serves itself over HTTPS instead of just being a file you double-click:
browsers only hand out GPS coordinates to a "secure context". A file:// page is
not one. localhost is. A LAN address like 192.168.1.40 is not — unless it's
served over TLS. So we mint a self-signed certificate on first run. Your phone
will complain about it exactly once; accept it and you have GPS in the field.
"""

from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from math import asinh, atan, degrees, floor, pi, radians, sinh, tan
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PHOTOS = DATA / "photos"
TILES = DATA / "tiles"
CERTS = DATA / "certs"
DB_PATH = DATA / "survey.db"
for d in (DATA, PHOTOS, TILES, CERTS):
    d.mkdir(parents=True, exist_ok=True)

UA = "FieldSurvey/1.0 (personal land survey; single user)"

# ---------------------------------------------------------------- controlled vocab
GROWTH_FORMS = ["tree", "shrub", "vine", "forb", "grass", "fern", "fungus", "other"]
STATUSES = ["cultivated", "wild", "invasive", "unknown"]
CONFIDENCE = ["certain", "probable", "tentative"]

# Colours are chosen so that none of them occurs in a summer aerial photograph
# of a Piedmont farm. A green pin disappears into a tree canopy.
STATUS_HEX = {
    "cultivated": "3FC8E4",   # cyan   — you planted it
    "wild": "E8E6DE",         # bone   — it planted itself
    "invasive": "FF3B6B",     # magenta— it needs to go
    "unknown": "A578FF",      # violet — it needs a key and a hand lens
}

TILE_SOURCES = {
    # USGS is public-domain US federal orthoimagery (NAIP-derived). Cache freely.
    "usgs": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
    # Esri's World Imagery is higher resolution in most places but is not public
    # domain. Fine for personal viewing; keep offline caching modest.
    "esri": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    # Roads/parcel context. OSM's tile policy forbids bulk download — no prefetch.
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
# Measured over Pittsboro, not guessed. USGS's cached basemap stops dead at z16
# (~2 m/px) — fine for orientation, useless for pinning a shrub. Esri serves to
# z20 (~12 cm/px), which is the only thing here with the resolution to do this job.
NATIVE_MAX = {"usgs": 16, "esri": 20, "osm": 19}
NO_PREFETCH = {"osm"}
PREFETCH_TILE_CAP = 2500


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
    id              TEXT PRIMARY KEY,
    common_name     TEXT NOT NULL DEFAULT '',
    scientific_name TEXT NOT NULL DEFAULT '',
    family          TEXT NOT NULL DEFAULT '',
    growth_form     TEXT NOT NULL DEFAULT 'other',
    status          TEXT NOT NULL DEFAULT 'unknown',
    det_confidence  TEXT NOT NULL DEFAULT 'probable',
    abundance       TEXT NOT NULL DEFAULT '',
    phenology       TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    date_observed   TEXT NOT NULL,
    lat             REAL NOT NULL,
    lng             REAL NOT NULL,
    fix_source      TEXT NOT NULL DEFAULT 'manual',
    gps_accuracy_m  REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plants_status ON plants(status);
CREATE INDEX IF NOT EXISTS ix_plants_sci    ON plants(scientific_name);

CREATE TABLE IF NOT EXISTS photos (
    id         TEXT PRIMARY KEY,
    plant_id   TEXT NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    filename   TEXT NOT NULL,
    thumb      TEXT NOT NULL DEFAULT '',
    caption    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_photos_plant ON photos(plant_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    return c


def init_db() -> None:
    with closing(db()) as c:
        c.executescript(SCHEMA)
        c.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_setting(key: str, default=None):
    with closing(db()) as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def put_setting(key: str, value) -> None:
    with closing(db()) as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        c.commit()


# ---------------------------------------------------------------- models
class Plant(BaseModel):
    id: Optional[str] = None
    common_name: str = ""
    scientific_name: str = ""
    family: str = ""
    growth_form: str = "other"
    status: str = "unknown"
    det_confidence: str = "probable"
    abundance: str = ""
    phenology: str = ""
    notes: str = ""
    date_observed: str = Field(default_factory=lambda: date.today().isoformat())
    lat: float
    lng: float
    fix_source: str = "manual"
    gps_accuracy_m: Optional[float] = None


def row_to_plant(r: sqlite3.Row, photos: list[dict]) -> dict:
    d = dict(r)
    d["photos"] = photos
    return d


def fetch_plants(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM plants ORDER BY date_observed DESC, created_at DESC").fetchall()
    pmap: dict[str, list[dict]] = {}
    for p in conn.execute("SELECT * FROM photos ORDER BY created_at").fetchall():
        pmap.setdefault(p["plant_id"], []).append(dict(p))
    return [row_to_plant(r, pmap.get(r["id"], [])) for r in rows]


# ---------------------------------------------------------------- app
app = FastAPI(title="Field Survey", docs_url="/api/docs", redoc_url=None)


@app.get("/", response_class=HTMLResponse)
def index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/config")
def config():
    return {
        "growth_forms": GROWTH_FORMS,
        "statuses": STATUSES,
        "confidence": CONFIDENCE,
        "status_hex": {k: "#" + v for k, v in STATUS_HEX.items()},
        "tile_sources": list(TILE_SOURCES),
        "native_max": NATIVE_MAX,
        "no_prefetch": sorted(NO_PREFETCH),
        "home": get_setting("home", {"lat": 35.7199, "lng": -79.1775, "zoom": 16}),
        "basemap": get_setting("basemap", "esri"),
        "boundary": get_setting("boundary", None),
    }


@app.put("/api/config/{key}")
async def set_config(key: str, request: Request):
    if key not in ("home", "basemap", "boundary"):
        raise HTTPException(400, "unknown setting")
    put_setting(key, await request.json())
    return {"ok": True}


# ------------------------------------------------------- plants CRUD
@app.get("/api/plants")
def list_plants():
    with closing(db()) as c:
        return fetch_plants(c)


@app.post("/api/plants")
def upsert_plant(p: Plant):
    if not p.common_name.strip() and not p.scientific_name.strip():
        raise HTTPException(422, "needs a common or scientific name")
    if p.growth_form not in GROWTH_FORMS:
        raise HTTPException(422, f"growth_form must be one of {GROWTH_FORMS}")
    if p.status not in STATUSES:
        raise HTTPException(422, f"status must be one of {STATUSES}")
    if p.det_confidence not in CONFIDENCE:
        raise HTTPException(422, f"det_confidence must be one of {CONFIDENCE}")

    pid = p.id or f"p{uuid.uuid4().hex[:12]}"
    t = now()
    with closing(db()) as c:
        c.execute(
            """INSERT INTO plants (id, common_name, scientific_name, family, growth_form,
                    status, det_confidence, abundance, phenology, notes, date_observed,
                    lat, lng, fix_source, gps_accuracy_m, created_at, updated_at)
               VALUES (:id,:common_name,:scientific_name,:family,:growth_form,
                    :status,:det_confidence,:abundance,:phenology,:notes,:date_observed,
                    :lat,:lng,:fix_source,:gps_accuracy_m,:t,:t)
               ON CONFLICT(id) DO UPDATE SET
                    common_name=excluded.common_name,
                    scientific_name=excluded.scientific_name,
                    family=excluded.family,
                    growth_form=excluded.growth_form,
                    status=excluded.status,
                    det_confidence=excluded.det_confidence,
                    abundance=excluded.abundance,
                    phenology=excluded.phenology,
                    notes=excluded.notes,
                    date_observed=excluded.date_observed,
                    lat=excluded.lat, lng=excluded.lng,
                    fix_source=excluded.fix_source,
                    gps_accuracy_m=excluded.gps_accuracy_m,
                    updated_at=excluded.updated_at""",
            {**p.model_dump(), "id": pid, "t": t},
        )
        c.commit()
        row = c.execute("SELECT * FROM plants WHERE id=?", (pid,)).fetchone()
        ph = [dict(x) for x in c.execute("SELECT * FROM photos WHERE plant_id=?", (pid,))]
    return row_to_plant(row, ph)


@app.delete("/api/plants/{pid}")
def delete_plant(pid: str):
    with closing(db()) as c:
        for ph in c.execute("SELECT filename, thumb FROM photos WHERE plant_id=?", (pid,)):
            for f in (ph["filename"], ph["thumb"]):
                if f:
                    (PHOTOS / f).unlink(missing_ok=True)
        n = c.execute("DELETE FROM plants WHERE id=?", (pid,)).rowcount
        c.commit()
    if not n:
        raise HTTPException(404, "no such record")
    return {"ok": True}


@app.get("/api/taxa")
def taxa():
    """Species rollup. The flat table stores one row per physical plant; this is
    the 'what actually grows here' view, which is the question that started all
    of this."""
    with closing(db()) as c:
        rows = c.execute(
            """SELECT COALESCE(NULLIF(scientific_name,''), common_name) AS taxon,
                      MAX(common_name)  AS common_name,
                      MAX(family)       AS family,
                      MAX(growth_form)  AS growth_form,
                      MAX(status)       AS status,
                      COUNT(*)          AS n,
                      MIN(date_observed) AS first_seen,
                      MAX(date_observed) AS last_seen
               FROM plants GROUP BY LOWER(taxon) ORDER BY n DESC, taxon"""
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------- photos
@app.post("/api/plants/{pid}/photos")
async def add_photo(pid: str, file: UploadFile = File(...)):
    with closing(db()) as c:
        if not c.execute("SELECT 1 FROM plants WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "no such record")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")

    phid = f"ph{uuid.uuid4().hex[:12]}"
    fn, thumb = f"{phid}.jpg", f"{phid}_t.jpg"
    try:
        from PIL import Image, ImageOps

        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        full = im.copy()
        full.thumbnail((2000, 2000))
        full.save(PHOTOS / fn, "JPEG", quality=88, optimize=True)
        im.thumbnail((480, 480))
        im.save(PHOTOS / thumb, "JPEG", quality=80, optimize=True)
    except Exception:
        # Pillow missing or the file is something exotic — keep the bytes anyway.
        fn = f"{phid}{Path(file.filename or '.jpg').suffix or '.jpg'}"
        (PHOTOS / fn).write_bytes(raw)
        thumb = fn

    with closing(db()) as c:
        c.execute(
            "INSERT INTO photos (id, plant_id, filename, thumb, created_at) VALUES (?,?,?,?,?)",
            (phid, pid, fn, thumb, now()),
        )
        c.commit()
    return {"id": phid, "filename": fn, "thumb": thumb}


@app.delete("/api/photos/{phid}")
def delete_photo(phid: str):
    with closing(db()) as c:
        r = c.execute("SELECT filename, thumb FROM photos WHERE id=?", (phid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such photo")
        for f in (r["filename"], r["thumb"]):
            if f:
                (PHOTOS / f).unlink(missing_ok=True)
        c.execute("DELETE FROM photos WHERE id=?", (phid,))
        c.commit()
    return {"ok": True}


# ------------------------------------------------------- exports
def _geojson() -> dict:
    with closing(db()) as c:
        plants = fetch_plants(c)
    feats = []
    for p in plants:
        props = {k: v for k, v in p.items() if k not in ("lat", "lng", "photos")}
        props["photo_count"] = len(p["photos"])
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lng"], p["lat"]]},
                "properties": props,
            }
        )
    fc = {"type": "FeatureCollection", "features": feats}
    b = get_setting("boundary")
    if b:
        fc["features"].append(
            {"type": "Feature", "geometry": b, "properties": {"name": "Property boundary"}}
        )
    return fc


@app.get("/export/geojson")
def export_geojson():
    return Response(
        json.dumps(_geojson(), indent=2),
        media_type="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="survey-{date.today()}.geojson"'},
    )


@app.get("/export/csv")
def export_csv():
    cols = [
        "id", "common_name", "scientific_name", "family", "growth_form", "status",
        "det_confidence", "abundance", "phenology", "date_observed", "lat", "lng",
        "fix_source", "gps_accuracy_m", "photo_count", "notes",
    ]
    with closing(db()) as c:
        plants = fetch_plants(c)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for p in plants:
        w.writerow({**p, "photo_count": len(p["photos"])})
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="survey-{date.today()}.csv"'},
    )


def _kml_color(hex_rgb: str) -> str:
    """KML wants aabbggrr. Yes, the byte order is backwards. It always has been."""
    r, g, b = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    return f"ff{b}{g}{r}".lower()


def _xml(s: str) -> str:
    return (
        str(s or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@app.get("/export/kml")
def export_kml():
    """Opens in Google Earth Pro / Earth Web, colour-coded, foldered by status,
    with the whole record in the balloon and in ExtendedData."""
    with closing(db()) as c:
        plants = fetch_plants(c)

    styles = "".join(
        f"""
    <Style id="s_{s}">
      <IconStyle>
        <color>{_kml_color(STATUS_HEX[s])}</color>
        <scale>1.1</scale>
        <Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>
      </IconStyle>
      <LabelStyle><scale>0.8</scale></LabelStyle>
    </Style>"""
        for s in STATUSES
    )

    folders = []
    for s in STATUSES:
        members = [p for p in plants if p["status"] == s]
        if not members:
            continue
        marks = []
        for p in members:
            name = p["common_name"] or p["scientific_name"]
            desc = f"""<![CDATA[
<div style="font-family:sans-serif;max-width:280px">
  <div style="font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:#888">
    {_xml(s)} &middot; {_xml(p['growth_form'])}</div>
  <h3 style="margin:4px 0">{_xml(p['common_name'] or '(unnamed)')}</h3>
  <div style="font-style:italic;color:#555">{_xml(p['scientific_name'])}</div>
  <hr>
  <b>Family</b> {_xml(p['family'] or '—')}<br>
  <b>Determination</b> {_xml(p['det_confidence'])}<br>
  <b>Abundance</b> {_xml(p['abundance'] or '—')}<br>
  <b>Phenology</b> {_xml(p['phenology'] or '—')}<br>
  <b>Observed</b> {_xml(p['date_observed'])}<br>
  <b>Fix</b> {_xml(p['fix_source'])}{f" ±{round(p['gps_accuracy_m'])} m" if p['gps_accuracy_m'] else ""}<br>
  <p>{_xml(p['notes'])}</p>
</div>]]>"""
            ext = "".join(
                f'<Data name="{k}"><value>{_xml(p[k])}</value></Data>'
                for k in ("id", "family", "growth_form", "det_confidence", "abundance",
                          "phenology", "date_observed", "fix_source")
            )
            marks.append(
                f"""
      <Placemark>
        <name>{_xml(name)}</name>
        <styleUrl>#s_{s}</styleUrl>
        <description>{desc}</description>
        <ExtendedData>{ext}</ExtendedData>
        <Point><coordinates>{p['lng']:.7f},{p['lat']:.7f},0</coordinates></Point>
      </Placemark>"""
            )
        folders.append(
            f"""
    <Folder>
      <name>{s.title()} ({len(members)})</name>
      <open>1</open>{''.join(marks)}
    </Folder>"""
        )

    boundary = ""
    b = get_setting("boundary")
    if b and b.get("type") == "Polygon":
        ring = " ".join(f"{x:.7f},{y:.7f},0" for x, y in b["coordinates"][0])
        boundary = f"""
    <Placemark>
      <name>Property boundary</name>
      <Style>
        <LineStyle><color>ffdee6e8</color><width>2</width></LineStyle>
        <PolyStyle><fill>0</fill></PolyStyle>
      </Style>
      <Polygon><outerBoundaryIs><LinearRing>
        <coordinates>{ring}</coordinates>
      </LinearRing></outerBoundaryIs></Polygon>
    </Placemark>"""

    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Plant survey — {date.today()}</name>
    <description>{len(plants)} records</description>{styles}{boundary}{''.join(folders)}
  </Document>
</kml>
"""
    return Response(
        kml,
        media_type="application/vnd.google-earth.kml+xml",
        headers={"Content-Disposition": f'attachment; filename="survey-{date.today()}.kml"'},
    )


@app.post("/api/import")
async def import_geojson(request: Request):
    fc = await request.json()
    if fc.get("type") != "FeatureCollection":
        raise HTTPException(400, "expected a GeoJSON FeatureCollection")
    n = skipped = 0
    for f in fc.get("features", []):
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        pr = f.get("properties") or {}
        coords = g.get("coordinates") or []
        if len(coords) < 2:
            skipped += 1
            continue
        lng, lat = coords[:2]
        try:
            upsert_plant(
                Plant(
                    id=pr.get("id"),
                    common_name=pr.get("common_name", ""),
                    scientific_name=pr.get("scientific_name", ""),
                    family=pr.get("family", ""),
                    growth_form=pr.get("growth_form", "other"),
                    status=pr.get("status", "unknown"),
                    det_confidence=pr.get("det_confidence", "probable"),
                    abundance=pr.get("abundance", "") or "",
                    phenology=pr.get("phenology", "") or "",
                    notes=pr.get("notes", "") or "",
                    date_observed=pr.get("date_observed") or date.today().isoformat(),
                    lat=lat, lng=lng,
                    fix_source=pr.get("fix_source", "manual"),
                    gps_accuracy_m=pr.get("gps_accuracy_m"),
                )
            )
            n += 1
        except HTTPException:
            skipped += 1
            continue
    return {"imported": n, "skipped": skipped}


# ------------------------------------------------------- tiles (cache + offline)
def tile_path(src: str, z: int, x: int, y: int) -> Path:
    return TILES / src / str(z) / str(x) / f"{y}.jpg"


def fetch_tile(src: str, z: int, x: int, y: int) -> Optional[bytes]:
    url = TILE_SOURCES[src].format(z=z, x=x, y=y)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    except Exception:
        return None


@app.get("/tiles/{src}/{z}/{x}/{y}")
def tile(src: str, z: int, x: int, y: int):
    if src not in TILE_SOURCES:
        raise HTTPException(404, "unknown tile source")
    p = tile_path(src, z, x, y)
    if p.exists():
        return FileResponse(p, media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=604800", "X-Tile": "disk"})
    body = fetch_tile(src, z, x, y)
    if body is None:
        # Offline and not cached. Leaflet draws nothing; that's the honest answer.
        raise HTTPException(504, "tile unavailable offline")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return Response(body, media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=604800", "X-Tile": "net"})


def deg2tile(lat: float, lng: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    xt = int((lng + 180.0) / 360.0 * n)
    lat_r = radians(lat)
    yt = int((1.0 - asinh(tan(lat_r)) / pi) / 2.0 * n)
    return xt, yt


class Prefetch(BaseModel):
    src: str
    north: float
    south: float
    east: float
    west: float
    zmin: int = 15
    zmax: int = 20


@app.post("/api/prefetch")
def prefetch(q: Prefetch):
    """Pull every tile covering the current view down to disk, so the app still
    draws imagery at the back of the property where there's no signal."""
    if q.src not in TILE_SOURCES:
        raise HTTPException(404, "unknown tile source")
    if q.src in NO_PREFETCH:
        raise HTTPException(
            400, f"{q.src} forbids bulk tile download in its usage policy — use usgs or esri"
        )

    # Asking a source for zoom levels it does not publish just harvests 404s.
    zmax = min(q.zmax, NATIVE_MAX[q.src])
    zmin = min(q.zmin, zmax)

    jobs = []
    for z in range(max(0, zmin), zmax + 1):
        x0, y0 = deg2tile(q.north, q.west, z)
        x1, y1 = deg2tile(q.south, q.east, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                if not tile_path(q.src, z, x, y).exists():
                    jobs.append((z, x, y))

    if len(jobs) > PREFETCH_TILE_CAP:
        raise HTTPException(
            400,
            f"that view needs {len(jobs):,} tiles (cap {PREFETCH_TILE_CAP:,}). "
            f"Zoom in, or lower the max zoom.",
        )

    def work(j) -> int:
        z, x, y = j
        b = fetch_tile(q.src, z, x, y)
        time.sleep(0.02)  # be a polite client
        if not b:
            return 0
        p = tile_path(q.src, z, x, y)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
        return 1

    with ThreadPoolExecutor(max_workers=6) as ex:
        got = sum(ex.map(work, jobs))

    return {"requested": len(jobs), "cached": got, "failed": len(jobs) - got,
            "zmax_used": zmax, "native_max": NATIVE_MAX[q.src]}


@app.get("/api/status")
def status():
    files = list(TILES.rglob("*.jpg"))
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) n FROM plants").fetchone()["n"]
        ph = c.execute("SELECT COUNT(*) n FROM photos").fetchone()["n"]
    return {
        "records": n,
        "photos": ph,
        "tiles_cached": len(files),
        "tile_cache_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1),
        "db": str(DB_PATH),
    }


app.mount("/photos", StaticFiles(directory=PHOTOS), name="photos")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


# ---------------------------------------------------------------- TLS + launch
def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(ip: str) -> tuple[Path, Path]:
    crt, key = CERTS / "survey.crt", CERTS / "survey.key"
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if crt.exists() and key.exists():
        # Reuse the cert only if it covers the address we're serving on now.
        # Take the laptop to a different network and the DHCP lease changes;
        # a cert minted for the old IP just makes phones fail confusingly.
        try:
            san = (
                x509.load_pem_x509_certificate(crt.read_bytes())
                .extensions.get_extension_for_class(x509.SubjectAlternativeName)
                .value
            )
            if ipaddress.ip_address(ip) in san.get_values_for_type(x509.IPAddress):
                return crt, key
        except Exception:
            pass  # unreadable/malformed — fall through and mint a fresh one

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Field Survey")])
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address(ip)),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(k, hashes.SHA256())
    )
    key.write_bytes(
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return crt, key


def main() -> None:
    ap = argparse.ArgumentParser(description="Field Survey — plant inventory for one piece of land")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--http", action="store_true",
                    help="plain HTTP on localhost only (no phone GPS)")
    args = ap.parse_args()

    init_db()
    import uvicorn

    ip = lan_ip()
    if args.http:
        url = f"http://localhost:{args.port}"
        ssl = {}
    else:
        crt, key = ensure_cert(ip)
        url = f"https://{ip}:{args.port}"
        ssl = {"ssl_certfile": str(crt), "ssl_keyfile": str(key)}

    bar = "─" * 54
    print(f"\n{bar}\n  FIELD SURVEY\n{bar}")
    print(f"  This machine   {url}")
    if not args.http:
        print(f"  Your phone     {url}   (same wifi)")
        print("\n  The phone will warn about the certificate. Accept it once —")
        print("  that warning is the price of GPS on a local network.")
        try:
            import qrcode

            q = qrcode.QRCode(border=1)
            q.add_data(url)
            q.print_ascii(invert=True)
        except ImportError:
            pass
    print(f"  API docs       {url}/api/docs")
    print(f"  Database       {DB_PATH}")
    print(f"{bar}\n")

    # --http means what the help text says: localhost only. Only the TLS
    # variant, which phones can actually use for GPS, listens on the LAN.
    host = "127.0.0.1" if args.http else "0.0.0.0"
    uvicorn.run(app, host=host, port=args.port, log_level="warning", **ssl)


if __name__ == "__main__":
    main()
