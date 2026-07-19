# Quickstart — your first survey session

Fifteen minutes from `pip install` to standing in front of a tree with your
phone. Do the first half at a desk; the second half is outside.

---

## 1 · Install and launch

You need Python 3.10+ and a phone on the same wifi as this machine.

```bash
cd LookoutLodge_PlantApp
python3 -m venv venv && source venv/bin/activate   # optional but tidy
pip install -r requirements.txt
python server.py
```

The banner prints two URLs and a QR code:

```
This machine   https://192.168.1.73:8443
Your phone     https://192.168.1.73:8443   (same wifi)
```

Leave this terminal running — it *is* the app. Everything lives in `data/`
beside it: the SQLite database, photos, cached map tiles, and the TLS
certificate it mints for itself on first run.

> **Why HTTPS with a scary certificate?** Browsers only give GPS to secure
> pages. `https://` on your LAN qualifies; the self-signed certificate is what
> makes that possible. Your phone will warn once — accept it and move on. If
> the laptop later joins a different network and gets a new address, a fresh
> certificate is minted and you accept one more time.

For desktop-only testing there's `python server.py --http` — plain HTTP,
localhost only, no phone, no GPS.

## 2 · Find your land (desktop first)

Open the URL on this machine. You'll be looking at satellite imagery with a
crosshair — the **reticle** — fixed at the centre. The first rule of the app:
**you move the map under the reticle, not a pin over the map.** One-handed in
a field, your fingertip covers the exact square metre you're trying to mark.
The reticle never does.

1. Pan and zoom until your property fills the screen. There's no search box —
   navigate like you would a paper map. If you're lost, switch to
   OpenStreetMap for a moment (◨ button, top right) to get road names, then
   switch back to Esri.
2. Press **⌂** once — it jumps to the saved home view. Frame your property the
   way you like, then press **⌂ again** while you're there: that frame is now
   home. Every session starts here.

## 3 · Draw the property boundary

Sidebar → **Property boundary**. Aim the reticle at each corner of your
parcel and press **⊕ Drop corner**; tap **Close shape** when you're back
near the start.

Watch the acreage readout as you go. You know what your parcel is supposed
to be — if the number is wildly off, something is wrong (usually a corner
dropped in the wrong place). The boundary draws as a dashed line from now on
and rides along in every export.

## 4 · Take it outside

Scan the QR code with your phone. Accept the certificate warning:

- **iPhone / Safari** — "This Connection Is Not Private" → **Show Details** →
  **visit this website**.
- **Android / Chrome** — **Advanced** → **Proceed to 192.168.x.x (unsafe)**.

That warning is about encryption between your phone and your laptop, on your
own wifi. Nothing here touches the internet except map-tile downloads.

Now walk to a plant you can name.

## 5 · Pin your first plant

1. Stand next to it. Tap **◎**. The phone asks for location permission —
   allow it. The reticle locks cyan and follows your GPS; the accuracy
   (±N m) shows at the top. Under open sky expect ±3–5 m; under canopy it
   can be worse — the number is honest, believe it.
2. If the fix is sloppy, or the plant is somewhere you can't stand: pan the
   map by hand instead. You can usually *see* the individual tree crown or
   shrub in the imagery — put the reticle on it. (Panning by hand switches
   GPS-follow off; that's intentional.)
3. Tap **⊕ Mark here**.
4. Fill in what you know. A common name alone is fine — `Blackgum` is a
   record; taxonomy can come later. Set **status** (this drives the pin
   colour) and be honest with **determination confidence** — `tentative` is
   a valid answer, and you can filter for it later when you come back with a
   field guide.
5. **Add photo** opens the camera. Get the whole plant and a close-up of a
   leaf — future-you doing identification will thank you.
6. **Save record.** The pin drops where the reticle was. GPS-placed records
   store the fix accuracy; hand-placed ones are marked `manual`. Both are
   kept forever, because in two years you'll want to know which pins to
   trust to the metre.

Logged one blackgum and standing in front of another? Tap the first one's
pin → **Log another** (or ⧉ in the list). Same species, notes cleared, new
position — aim and save. This is how a fencerow of twelve gets logged in
three minutes.

## 6 · Before you walk beyond the wifi

Tiles are cached to disk automatically the moment you look at them, but the
back of the property you haven't looked at yet. While still on wifi:

1. Frame an area you'll survey. ◨ → **Cache this view for offline use**.
2. Repeat for two or three framings that cover your route.

If it refuses because the view needs too many tiles, zoom in and do it in
pieces. Out of range, the app keeps working against the cache; anything
uncached draws as blank — and saves still need the laptop reachable on wifi,
so true off-grid surveying means carrying the laptop (a hotspot from a
second phone also works).

## 7 · Use what you built

- **Chips** under the search box filter by status. Show only magenta and
  you're holding the invasive-removal worklist. Only violet: everything
  still waiting on an identification.
- **Species list** tab: every taxon you've recorded, with counts and
  first/last-seen dates. This is "what actually grows here" — the question
  the whole app exists to answer.
- **Export** → GeoJSON (QGIS, geopandas), KML (Google Earth Pro — try the
  historical-imagery slider), or CSV. The database itself is
  `data/survey.db`, plain SQLite; the [README](README.md#your-data) has
  pandas snippets.
- **Back up** by copying `data/` somewhere. That directory is the entire
  application state.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "No GPS here — the page must be served over HTTPS" | You opened the `http://` variant or `--http` mode. Use the `https://` URL from the banner. |
| Certificate warning came back | The laptop's LAN address changed (new network or DHCP lease); a fresh cert was minted for it. Accept it once, carry on. |
| Map is blank patches in the field | Those tiles were never viewed or cached on wifi. Cache the views in advance (step 6). |
| "Needs a common or scientific name" | Records must have at least one name. `unknown shrub` is a perfectly good common name for now. |
| Save failed out in the field | The phone lost the laptop's wifi. Saves fail loudly rather than queue silently — walk back into range and save again. |
| Lost the URL | It's printed in the terminal where `server.py` runs, QR included. |
