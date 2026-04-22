"""
Download XYZ raster tiles from a templated URL (Navionics-style viewer API).

Tokens:
  Defaults are hardcoded below (DEFAULT_NAVIONICS_*). Optional override:
    NAVIONICS_BEARER   = Authorization: Bearer <jwt>
    NAVIONICS_CONFIG   = value of the `config` query parameter (JWT string)
  Do not commit this file to a public repo with live tokens.

Usage examples (PowerShell):
  python download_tiles.py --bbox -5.2 35.8 10.1 45.2 --zoom-min 10 --zoom-max 16 --out ./tiles_store

  # Default: download every combination (nautical/sonar x seabed x transparent x 3 shallow bands).
  python download_tiles.py --anchor-tile 16/18322/24033 --margin 4 --zoom-min 16 --zoom-max 16 --out ./tiles_store

  # Single set of query params (legacy flat z/x/y under --out):
  python download_tiles.py --variants single --layer 1 --du 2 --sd 29 --sa true --transparent false ...

Notes:
  - Respect Garmin / Navionics terms and rate limits; raise --delay or lower --workers if throttled.
  - JWTs expire; refresh NAVIONICS_* when requests start failing with 401/403.
  - Shallow (du, sd) presets are best-effort; confirm values in browser DevTools if tiles look wrong.
"""

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib.parse import quote

import requests

# Garmin Bearer + Navionics config JWT (expire; update when 401/403). Env vars override if set.
DEFAULT_NAVIONICS_BEARER = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJjNDcyNmIxMC0yNjM3LTQ4ZjQtODY3NC1lZTk5NmI5NjE0MGUiLCJhdWQiOiJtYXBzLmdhcm1pbi5jb20iLCJpc3MiOiJnYXJtaW4uY29tIiwiZXhwIjoxNzc2ODc3MDUwLCJpYXQiOjE3NzY4Njk4NTB9.km5MqLkdmV__u4-bjcRKl8Vy7b8ONa2uaRnDuWWJROBELa-QZMGBR_errpHidqm-SZ0GN7KE8mL3fp12KY0TjGxzvgZxdA25PCBf7bttt1KfwwaVoIpl-Zu_S0GpYM7UykaY3T4l2B5jwsBhwjmi253VXwLpu-Cr4MLH9_woxMgvh2ywMtq4X6Eps1JAjdQIJW-IBiLm5vAPfWiaLbr1hDmXZ3Nx73zFD0jW0WafLe-BIf-CKF0ONI6nKUTCkfoDxybeTAixwtp-tDReInZjyOiqGZF6ItxvQQPSbTcaGD2Gji4V9zYEvqEPOKQZ6uneGiWy6zKvUWhufNuWsimfqw"
)
DEFAULT_NAVIONICS_CONFIG = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJycG4iOiJpbnRlcm5hbF9zZXJ2aWNlIiwiYXByIjoiMDEwLUQyMTEyLTEwIn0.Ua7QtpbvTn16y9WDFnzUSiTCzjQbltqcBFAkiFv1PEY"
)

BBox = Tuple[float, float, float, float]  # west, south, east, north

# Shallow shading: (folder_tag, du, sd). du/sd are Navionics query values; literal 10/33/5.5 may not
# match sd integers — tune from maps.garmin.com Network tab if a preset 404s or looks wrong.
# layer 0 = nautical charts, 1 = SonarChart-style (per Garmin/Navionics viewer).
SHALLOW_SHADING_PRESETS = (
    ("sh_m10", "1", "10"),  # 0–10 m band (metric shallow shading)
    ("sh_ft33", "2", "33"),  # 0–33 ft
    ("sh_fm55", "3", "11"),  # 0–5.5 fathoms (du=3 fathoms; sd heuristic — edit if needed)
)


def build_variant_matrix():
    """All combinations: 2 layers x 2 sa x 2 transparent x len(SHALLOW_SHADING_PRESETS)."""
    out = []
    for sh_tag, du, sd in SHALLOW_SHADING_PRESETS:
        for layer in ("0", "1"):
            for sa in ("true", "false"):
                for transparent in ("true", "false"):
                    slug = (
                        f"L{layer}_du{du}_sd{sd}_sa{1 if sa == 'true' else 0}"
                        f"_t{1 if transparent == 'true' else 0}_{sh_tag}"
                    )
                    layer_name = "nautical" if layer == "0" else "sonar"
                    out.append(
                        {
                            "slug": slug,
                            "layer": layer,
                            "du": du,
                            "sd": sd,
                            "sa": sa,
                            "transparent": transparent,
                            "ugc": "false",
                            "label": (
                                f"{layer_name} | seabed={sa} | transparent={transparent} | "
                                f"shallow={sh_tag} (du={du} sd={sd})"
                            ),
                        }
                    )
    return tuple(out)


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    """Web Mercator XYZ tile indices (OSM / Google style, y grows toward south)."""
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def bbox_to_xy_range(bbox: BBox, zoom: int) -> Tuple[int, int, int, int]:
    west, south, east, north = bbox
    corners = (
        lonlat_to_tile(west, south, zoom),
        lonlat_to_tile(west, north, zoom),
        lonlat_to_tile(east, south, zoom),
        lonlat_to_tile(east, north, zoom),
    )
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def tile_y_to_latitude(z: int, y_edge: float) -> float:
    """Latitude of a horizontal WebMercator tile boundary (y is a row index, float allowed)."""
    n = 2**z
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y_edge / n))))


def parse_anchor_tile(s: str) -> Tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)/(\d+)/(\d+)", s.strip())
    if not m:
        raise argparse.ArgumentTypeError("anchor tile must look like 16/18322/24033")
    z, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return z, x, y


def anchor_margin_bbox(anchor: str, margin: int) -> BBox:
    z, x0, y0 = parse_anchor_tile(anchor)
    n = 2**z
    xmin, xmax = x0 - margin, x0 + margin
    ymin, ymax = y0 - margin, y0 + margin
    west = xmin / n * 360.0 - 180.0
    east = (xmax + 1) / n * 360.0 - 180.0
    north = tile_y_to_latitude(z, ymin)
    south = tile_y_to_latitude(z, ymax + 1)
    return west, south, east, north


def iter_jobs(
    bbox: BBox,
    zoom_min: int,
    zoom_max: int,
) -> Iterator[Tuple[int, int, int]]:
    for z in range(zoom_min, zoom_max + 1):
        xmin, ymin, xmax, ymax = bbox_to_xy_range(bbox, z)
        for x in range(xmin, xmax + 1):
            for y in range(ymin, ymax + 1):
                yield z, x, y


def extension_from_content_type(ct):
    # type: (Optional[str]) -> str
    if not ct:
        return ".bin"
    ct = ct.split(";")[0].strip().lower()
    if ct == "image/png":
        return ".png"
    if ct in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if ct == "image/webp":
        return ".webp"
    return ".bin"


def build_url(
    template: str,
    z: int,
    x: int,
    y: int,
    config: str,
    transparent: str,
    ugc: str,
    layer: str,
    du: str,
    sd: str,
    sa: str,
) -> str:
    # config is already a JWT; encode for query safety
    q = (
        f"config={quote(config, safe='')}"
        f"&transparent={transparent}"
        f"&ugc={ugc}"
        f"&layer={layer}"
        f"&du={du}"
        f"&sd={sd}"
        f"&sa={sa}"
    )
    return template.format(z=z, x=x, y=y) + "?" + q


class TileDownloader:
    def __init__(
        self,
        session: requests.Session,
        url_builder,
        out_root: Path,
        referer: str,
        origin: str,
        delay_s: float,
        lock: threading.Lock,
    ) -> None:
        self.session = session
        self.url_builder = url_builder
        self.out_root = out_root
        self.referer = referer
        self.origin = origin
        self.delay_s = delay_s
        self.lock = lock
        self.stats = {"ok": 0, "skip": 0, "fail": 0}

    def fetch_one(self, variant: dict, z: int, x: int, y: int) -> None:
        url = self.url_builder(z, x, y, variant)
        slug = variant.get("slug") or ""
        tile_root = self.out_root / slug if slug else self.out_root
        ext_dir = tile_root / str(z) / str(x)
        ext_dir.mkdir(parents=True, exist_ok=True)

        # probe extension with HEAD optional — skip: try GET and use content-type
        path_unknown = ext_dir / f"{y}.tmp_dl"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Referer": self.referer,
            "Origin": self.origin,
        }

        final_png = ext_dir / f"{y}.png"
        final_jpg = ext_dir / f"{y}.jpg"
        final_webp = ext_dir / f"{y}.webp"
        for p in (final_png, final_jpg, final_webp):
            if p.exists():
                with self.lock:
                    self.stats["skip"] += 1
                return

        if self.delay_s > 0:
            time.sleep(self.delay_s)

        r = self.session.get(url, headers=headers, timeout=60)
        if r.status_code != 200:
            with self.lock:
                self.stats["fail"] += 1
            tag = slug or "default"
            sys.stderr.write(f"[fail] {tag} z={z} x={x} y={y} status={r.status_code}\n")
            return

        ext = extension_from_content_type(r.headers.get("Content-Type"))
        if ext == ".bin" and r.content[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif ext == ".bin" and r.content[:2] == b"\xff\xd8":
            ext = ".jpg"

        final = ext_dir / f"{y}{ext}"
        tmp = path_unknown
        tmp.write_bytes(r.content)
        if final.exists():
            final.unlink()
        tmp.replace(final)
        with self.lock:
            self.stats["ok"] += 1


def write_manifest(
    out_root: Path,
    bbox: BBox,
    zoom_min: int,
    zoom_max: int,
    variants,
) -> None:
    meta = {
        "bbox": {"west": bbox[0], "south": bbox[1], "east": bbox[2], "north": bbox[3]},
        "zoom_min": zoom_min,
        "zoom_max": zoom_max,
        "xyz_scheme": "web-mercator-osm",
        "variants": [
            {
                "slug": v["slug"],
                "label": v["label"],
                "layer": v["layer"],
                "du": v["du"],
                "sd": v["sd"],
                "sa": v["sa"],
                "transparent": v["transparent"],
            }
            for v in variants
        ],
    }
    (out_root / "manifest.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def write_leaflet_index(
    out_root: Path,
    bbox: BBox,
    zoom_min: int,
    zoom_max: int,
    variants,
) -> None:
    west, south, east, north = bbox
    vjson = json.dumps(
        [
            {
                "slug": v["slug"],
                "label": v["label"],
                "layer": str(v["layer"]),
                "du": str(v["du"]),
                "sd": str(v["sd"]),
                "sa": str(v["sa"]).lower(),
                "transparent": str(v["transparent"]).lower(),
            }
            for v in variants
        ],
        ensure_ascii=True,
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Local tiles</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
    integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
  <style>
    html, body {{ height: 100%; margin: 0; background: #0f1115; }}
    #layout {{ display: flex; height: 100%; }}
    #controls {{
      flex: 0 0 232px; max-width: 92vw;
      background: #1a1d24; color: #e8eaef;
      font: 13px/1.35 system-ui, -apple-system, "Segoe UI", sans-serif;
      padding: 18px 16px 24px; box-shadow: 4px 0 24px rgba(0,0,0,0.45);
      overflow-y: auto; z-index: 2000;
    }}
    .section-title {{
      font-size: 10px; font-weight: 700; letter-spacing: 0.1em; color: #8b93a7;
      margin: 18px 0 10px; text-transform: uppercase;
    }}
    .section-title:first-child {{ margin-top: 0; }}
    .row {{ display: flex; align-items: center; gap: 10px; margin: 8px 0; cursor: pointer; }}
    .row span {{ color: #dce1ec; }}
    .row input {{
      appearance: none; width: 20px; height: 20px; margin: 0; flex-shrink: 0;
      border: 2px solid #5c6370; border-radius: 50%; cursor: pointer;
      background: transparent;
    }}
    .row input:checked {{
      border-color: #fff; background: #fff;
      box-shadow: inset 0 0 0 4px #1a1d24;
    }}
    .row input:focus-visible {{ outline: 2px solid #6b9fff; outline-offset: 2px; }}
    #shallow-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 8px; flex-wrap: wrap; }}
    #shallow-val {{ font-size: 12px; font-weight: 600; color: #fff; letter-spacing: 0.02em; }}
    #shallow-range {{
      width: 100%; margin: 10px 0 4px; height: 6px; border-radius: 3px;
      accent-color: #c8cdd8; cursor: pointer;
    }}
    .range-ends {{
      display: flex; justify-content: space-between; font-size: 11px; color: #6b7280;
      margin-bottom: 8px;
    }}
    #no-tiles {{
      font-size: 11px; color: #f59e0b; margin-top: 8px; line-height: 1.4;
    }}
    #map-wrap {{ flex: 1; position: relative; min-width: 0; }}
    #map {{ position: absolute; inset: 0; }}
  </style>
</head>
<body>
  <div id="layout">
    <aside id="controls">
      <div class="section-title">View</div>
      <label class="row"><input type="radio" name="view" value="map" checked /><span>Map</span></label>
      <label class="row"><input type="radio" name="view" value="heatmap" /><span>Heatmap</span></label>

      <div class="section-title">Chart type</div>
      <label class="row"><input type="radio" name="chart" value="nautical" checked /><span>Nautical charts</span></label>
      <label class="row"><input type="radio" name="chart" value="sonar" /><span>SonarChart™ maps</span></label>

      <div class="section-title">Seabed areas</div>
      <label class="row"><input type="radio" name="seabed" value="show" checked /><span>Show</span></label>
      <label class="row"><input type="radio" name="seabed" value="hide" /><span>Hide</span></label>

      <div class="section-title">Depth units</div>
      <label class="row"><input type="radio" name="depth" value="ft" /><span>Feet (ft)</span></label>
      <label class="row"><input type="radio" name="depth" value="m" /><span>Meters (m)</span></label>
      <label class="row"><input type="radio" name="depth" value="fath" checked /><span>Fathoms (fath)</span></label>

      <div class="section-title" id="shallow-head">
        <span>Shallow shading</span>
        <span id="shallow-val">3 FTH</span>
      </div>
      <input type="range" id="shallow-range" min="0" max="5.5" step="0.1" value="3" />
      <div class="range-ends"><span>0</span><span id="shallow-max-label">5.5</span></div>
      <p id="no-tiles" style="display:none;">No downloaded tiles match this combination. Pick another option or re-run the downloader with those variants.</p>
    </aside>
    <div id="map-wrap"><div id="map"></div></div>
  </div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
    integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const ALL = {vjson};
    const zMin = {zoom_min};
    const zMax = {zoom_max};
    const southWest = L.latLng({south}, {west});
    const northEast = L.latLng({north}, {east});
    const bounds = L.latLngBounds(southWest, northEast);

    const map = L.map('map', {{
      minZoom: zMin,
      maxZoom: zMax,
      maxBounds: bounds.pad(0.05),
      maxBoundsViscosity: 0.85
    }});
    map.fitBounds(bounds, {{ maxZoom: zMax }});

    function tileUrlTemplate(slug) {{
      const prefix = slug ? (slug + '/') : '';
      return prefix + '{{z}}/{{x}}/{{y}}.png';
    }}

    function readState() {{
      const view = document.querySelector('input[name="view"]:checked').value;
      const transparent = view === 'heatmap' ? 'true' : 'false';
      const chart = document.querySelector('input[name="chart"]:checked').value;
      const layer = chart === 'nautical' ? '0' : '1';
      const seabed = document.querySelector('input[name="seabed"]:checked').value;
      const sa = seabed === 'show' ? 'true' : 'false';
      const depth = document.querySelector('input[name="depth"]:checked').value;
      const du = depth === 'ft' ? '2' : (depth === 'm' ? '1' : '3');
      const shallow = parseFloat(document.getElementById('shallow-range').value);
      return {{ transparent, layer, sa, du, shallow, depth }};
    }}

    function filterVariants(st) {{
      return ALL.filter(function (v) {{
        return v.transparent === st.transparent && v.layer === st.layer && v.sa === st.sa && v.du === st.du;
      }});
    }}

    function pickVariant(st) {{
      const f = filterVariants(st);
      if (!f.length) return null;
      const t = st.shallow;
      let best = f[0];
      let bestD = Math.abs(parseFloat(best.sd, 10) - t);
      for (let i = 1; i < f.length; i++) {{
        const d = Math.abs(parseFloat(f[i].sd, 10) - t);
        if (d < bestD) {{ bestD = d; best = f[i]; }}
      }}
      return best;
    }}

    function depthAxis(du) {{
      if (du === '1') return {{ min: 0, max: 10, step: 0.5, suffix: ' M', decimals: 1 }};
      if (du === '2') return {{ min: 0, max: 33, step: 1, suffix: ' FT', decimals: 0 }};
      return {{ min: 0, max: 5.5, step: 0.1, suffix: ' FTH', decimals: 1 }};
    }}

    function syncShallowSliderToData() {{
      const st = readState();
      const f = filterVariants(st);
      const ax = depthAxis(st.du);
      const r = document.getElementById('shallow-range');
      r.min = String(ax.min);
      r.max = String(ax.max);
      r.step = String(ax.step);
      document.getElementById('shallow-max-label').textContent = String(ax.max);
      let v = parseFloat(r.value, 10);
      if (isNaN(v) || v < ax.min || v > ax.max) v = Math.min(ax.max, Math.max(ax.min, (ax.min + ax.max) / 2));
      if (f.length) {{
        const sds = f.map(function (x) {{ return parseFloat(x.sd, 10); }}).sort(function (a,b) {{ return a-b; }});
        const mid = sds[Math.floor(sds.length / 2)];
        if (sds.indexOf(v) < 0) v = mid;
      }}
      r.value = String(v);
      updateShallowLabel();
    }}

    function updateShallowLabel() {{
      const st = readState();
      const ax = depthAxis(st.du);
      const v = parseFloat(document.getElementById('shallow-range').value, 10);
      const dec = ax.decimals;
      const num = dec ? v.toFixed(dec) : String(Math.round(v));
      document.getElementById('shallow-val').textContent = num + ax.suffix.trim();
    }}

    let tileLayer = null;

    function applyLayer() {{
      const st = readState();
      const v = pickVariant(st);
      const warn = document.getElementById('no-tiles');
      if (!v) {{
        warn.style.display = 'block';
        if (tileLayer) {{ map.removeLayer(tileLayer); tileLayer = null; }}
        return;
      }}
      warn.style.display = 'none';
      const url = tileUrlTemplate(v.slug);
      if (tileLayer) map.removeLayer(tileLayer);
      tileLayer = L.tileLayer(url, {{
        minZoom: zMin,
        maxZoom: zMax,
        maxNativeZoom: zMax,
        bounds: bounds,
        tms: false,
        noWrap: true
      }}).addTo(map);
    }}

    function wireControls() {{
      document.querySelectorAll('#controls input[type=radio]').forEach(function (el) {{
        el.addEventListener('change', function () {{
          syncShallowSliderToData();
          applyLayer();
        }});
      }});
      document.getElementById('shallow-range').addEventListener('input', function () {{
        updateShallowLabel();
      }});
      document.getElementById('shallow-range').addEventListener('change', applyLayer);
    }}

    if (ALL.length <= 1) {{
      document.getElementById('controls').style.display = 'none';
      document.getElementById('layout').style.display = 'block';
      document.getElementById('map-wrap').style.height = '100%';
      let tileLayer0 = L.tileLayer(tileUrlTemplate(ALL[0] ? ALL[0].slug : ''), {{
        minZoom: zMin, maxZoom: zMax, maxNativeZoom: zMax, bounds: bounds, tms: false, noWrap: true
      }}).addTo(map);
    }} else {{
      wireControls();
      syncShallowSliderToData();
      applyLayer();
    }}
  </script>
</body>
</html>
"""
    (out_root / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="Download XYZ tiles into z/x/y files.")
    p.add_argument(
        "--base",
        default="https://tile1.navionics.com/viewer/api/v1/tile/{z}/{x}/{y}",
        help="URL template with {{z}}, {{x}}, {{y}} placeholders (curl braces: use single braces in default).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Geographic bounding box in degrees (W S E N).",
    )
    g.add_argument(
        "--anchor-tile",
        type=str,
        help="Reference tile z/x/y at your max zoom, e.g. 16/18322/24033",
    )
    p.add_argument("--margin", type=int, default=16, help="With --anchor-tile, half-size in tiles at anchor zoom.")
    p.add_argument("--zoom-min", type=int, default=10)
    p.add_argument("--zoom-max", type=int, default=16)
    p.add_argument("--out", type=Path, default=Path("tiles_store"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--delay",
        type=float,
        default=0.05,
        help="Seconds to sleep before each GET (default 0). Increase (e.g. 0.05) if the server throttles you.",
    )
    p.add_argument("--referer", default="https://maps.garmin.com/")
    p.add_argument("--origin", default="https://maps.garmin.com")
    p.add_argument(
        "--variants",
        choices=("all", "single"),
        default="all",
        help="all: nautical+sonar x seabed x transparent x shallow presets (see SHALLOW_SHADING_PRESETS). "
        "single: one query set via --layer/--du/--sd/--sa/--transparent.",
    )
    p.add_argument(
        "--transparent",
        default="false",
        help="With --variants single only.",
    )
    p.add_argument("--ugc", default="false", help="With --variants single only (unchanged in 'all' mode).")
    p.add_argument("--layer", default="0", help="With --variants single: 0 nautical, 1 sonar.")
    p.add_argument("--du", default="1", help="With --variants single only.")
    p.add_argument("--sd", default="2", help="With --variants single only.")
    p.add_argument("--sa", default="false", help="With --variants single only: seabed areas true/false.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.anchor_tile:
        bbox = anchor_margin_bbox(args.anchor_tile, args.margin)
    else:
        bbox = tuple(args.bbox)  # type: ignore[assignment]

    if args.zoom_min > args.zoom_max:
        p.error("--zoom-min must be <= --zoom-max")

    bearer = (os.environ.get("NAVIONICS_BEARER") or DEFAULT_NAVIONICS_BEARER).strip()
    config = (os.environ.get("NAVIONICS_CONFIG") or DEFAULT_NAVIONICS_CONFIG).strip()
    if not args.dry_run and (not bearer or not config):
        print("Missing tokens: set NAVIONICS_* env vars or DEFAULT_* in download_tiles.py.", file=sys.stderr)
        return 2

    jobs = list(iter_jobs(bbox, args.zoom_min, args.zoom_max))
    if args.variants == "all":
        variants = build_variant_matrix()
    else:
        tr = str(args.transparent).lower()
        sa = str(args.sa).lower()
        if sa not in ("true", "false"):
            p.error("--sa must be true or false")
        if tr not in ("true", "false"):
            p.error("--transparent must be true or false")
        variants = (
            {
                "slug": "",
                "layer": str(args.layer),
                "du": str(args.du),
                "sd": str(args.sd),
                "sa": sa,
                "transparent": tr,
                "ugc": str(args.ugc).lower(),
                "label": "single variant (flat z/x/y)",
            },
        )

    total_requests = len(jobs) * len(variants)
    print(
        f"Tiles: {len(jobs)}  variants: {len(variants)}  total GETs (approx): {total_requests} "
        "(existing files skipped per variant folder)"
    )
    if args.dry_run:
        print("bbox:", bbox)
        for j in jobs[:3]:
            print(" sample tile:", j)
        for v in variants[:4]:
            print(" sample variant:", v.get("slug") or "(root)", v.get("label", ""))
        if len(variants) > 4:
            print(" ...")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    write_manifest(args.out, bbox, args.zoom_min, args.zoom_max, variants)
    write_leaflet_index(args.out, bbox, args.zoom_min, args.zoom_max, variants)

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {bearer}"

    # Normalize template: allow user to pass {{z}} style
    template = args.base
    template = template.replace("{{z}}", "{z}").replace("{{x}}", "{x}").replace("{{y}}", "{y}")

    def url_builder(z: int, x: int, y: int, v: dict) -> str:
        return build_url(
            template,
            z,
            x,
            y,
            config,
            v["transparent"],
            v["ugc"],
            v["layer"],
            v["du"],
            v["sd"],
            v["sa"],
        )

    lock = threading.Lock()
    dl = TileDownloader(session, url_builder, args.out, args.referer, args.origin, args.delay, lock)

    workers = max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = {
            ex.submit(dl.fetch_one, v, z, x, y) for v in variants for z, x, y in jobs
        }
        total = len(pending)
        done = 0
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            done += len(finished)
            if done % 200 == 0 or done == total:
                print(f"progress {done}/{total}  stats={dl.stats}")

    print("done:", dl.stats)
    print("Open a static server in the output folder and visit index.html, e.g.:")
    print(f"  cd {args.out.resolve()}")
    print("  python -m http.server 8765")
    print("  http://127.0.0.1:8765/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
