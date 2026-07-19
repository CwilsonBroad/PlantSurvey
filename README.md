# Field Survey

A plant inventory for one piece of land. Satellite imagery, a pin for every plant
you identify, and a database you own.

---

## Run it

```bash
pip install -r requirements.txt
python server.py
```

It prints two URLs and a QR code. Open the first on this machine; scan the QR
with your phone to take it outside.

First time? **[QUICKSTART.md](QUICKSTART.md)** walks the whole first session —
install, boundary, first pin, offline caching — in fifteen minutes.

```bash
python server.py --http    # localhost only, no TLS — desktop testing, no GPS
python server.py --port 9000
```

## Why it serves itself over HTTPS

Browsers only hand out GPS coordinates to a **secure context**. `https://` counts.
`localhost` counts. A `file://` page does not, and neither does a plain-HTTP LAN
address like `http://192.168.1.40:8443` — which is exactly the address your phone
needs in the field.

So the app mints a self-signed certificate on first run (`data/certs/`). Your
phone will warn you about it once. Accept it. That warning is the entire price of
having GPS work on your own local network. (If the laptop later gets a different
LAN address, a fresh certificate is minted for it automatically — you'll accept
one more warning, once per address, not per session.)

---

## Using it

**The reticle.** The crosshair in the centre of the screen is fixed. You move the
*map* under it, not a pin over it. This is deliberate: one-handed in a field, your
fingertip covers the exact square metre you're trying to mark. The reticle never
does. It works the way a Garmin works.

**Mark here** logs whatever is under the reticle. Tap **◎** to have GPS drive the
reticle instead, and the record is saved with `fix_source: gps` and the accuracy
in metres. Hand-placed records are saved as `manual`. The distinction is kept
because it matters later, when a pin is 8 m from where you remember the tree.

**Pins carry two axes of information at once.** The letter is growth form
(T/S/V/H/G/F/M). The colour is status:

| | | |
|---|---|---|
| ● cyan | `cultivated` | you planted it |
| ● bone | `wild` | it planted itself |
| ● magenta | `invasive` | it needs to go |
| ● violet | `unknown` | it needs a key and a hand lens |

None of those four hues occurs in a summer aerial photograph of a Piedmont farm.
That's the point — a green pin disappears into a tree canopy.

Filter to magenta and you have a removal worklist. Filter to violet and you have a
list of everything still waiting on a determination.

**Property boundary.** Draw it corner by corner; the app reports the enclosed area
in acres, computed on the sphere. You know roughly what your parcel is, so that
number is a free check on the whole coordinate pipeline. If it reads 40 acres,
something upstream is wrong.

---

## Imagery — measured, not assumed

Tested directly against these coordinates:

| Source | Max zoom served | Ground resolution | Verdict |
|---|---|---|---|
| **Esri World Imagery** | z20–21 | **~12 cm/px** | the only source sharp enough to pin an individual shrub |
| USGS / NAIP | z16 (404s above) | ~2 m/px | orientation only — you cannot see a shrub |
| OpenStreetMap | z19 | — | roads and parcel context |

Esri is therefore the default. USGS is public domain and pleasant to have, but its
cached basemap stops dead at z16 over Chatham County, and at 2 m per pixel a
blueberry bush is a smudge. If you ever need public-domain imagery at NAIP's true
resolution, it lives behind an ArcGIS *ImageServer* (`exportImage`) rather than an
XYZ tile endpoint — a different integration, not a config change.

## Offline

Tiles cache to disk the moment you look at them. **Cache this view for offline
use** pulls the current view down in advance, so the back of the property still
draws imagery with no signal. Prefetch clamps to whatever zoom the source actually
publishes, so you don't harvest 404s. OSM's usage policy forbids bulk tile
download, so the app refuses to do it.

The UI itself carries no CDN dependencies — Leaflet and the fonts are vendored
into `static/vendor/` — so the app boots and runs with no internet at all. The
only thing the client ever asks the network for is `/tiles`, and those cache.

---

## Your data

Three exports, all under **Export**:

- **GeoJSON** — geopandas, QGIS, Google Earth
- **KML** — colour-coded, foldered by status. Open in **Google Earth Pro** for the
  historical-imagery slider: scrub back fifteen years and watch the orchard get
  planted.
- **CSV** — one row per plant, decimal degrees, WGS 84

But the real answer is that the database is a plain SQLite file at
`data/survey.db`, and you already know what to do with one of those:

```python
import sqlite3, pandas as pd

con = sqlite3.connect("data/survey.db")

df = pd.read_sql("SELECT * FROM plants", con)

# what's actually out there
df.groupby(["status", "growth_form"]).size().unstack(fill_value=0)

# the removal worklist, nearest-first from the house
house = (35.7199, -79.1775)
inv = df[df.status == "invasive"].copy()
inv["m"] = ((inv.lat - house[0])**2 + (inv.lng - house[1])**2)**.5 * 111_320
inv.sort_values("m")[["common_name", "abundance", "m"]]
```

There is no lock-in. There is no account. Nothing leaves the machine except tile
requests.

---

## Schema note

`plants` is one row per **physical plant or patch** — an occurrence, not a taxon.
Twelve blackgums are twelve rows. The species list ("what actually grows here")
is a `GROUP BY` over that table, served at `/api/taxa`.

That's a deliberate denormalisation. A proper `taxa` ⇄ `occurrences` split is more
correct, and if you outgrow this it's a clean migration — but field entry has to be
fast or you stop doing it, and one flat insert is fast.

Interactive API docs: `/api/docs`.

---

## Not built, on purpose

- **No offline write queue.** If the server is unreachable, saves fail loudly
  rather than silently pretending. Add a service worker if you want to survey with
  the laptop shut.
- **No plant ID.** Determination is yours. The app records how confident you were
  (`certain` / `probable` / `tentative`) and never guesses on your behalf.
- **No photo-EXIF GPS.** Photos attach to the record's position, not their own.

## Attribution

Imagery © Esri, Maxar, Earthstar Geographics · USGS National Map · OpenStreetMap
contributors. Personal, single-user use — check each provider's terms before you
do anything larger.
