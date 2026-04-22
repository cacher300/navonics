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
  - Map UI is static `index.html` (loads `manifest.json` via HTTP or file-picker on file://). Copied into `--out` each run.
  - Respect Garmin / Navionics terms and rate limits; raise --delay or lower --workers if throttled.
  - JWTs expire; refresh NAVIONICS_* when requests start failing with 401/403.
  - Shallow (du, sd) presets are best-effort; confirm values in browser DevTools if tiles look wrong.
"""

import argparse
import itertools
import json
import math
import os
import shutil
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
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJjNDcyNmIxMC0yNjM3LTQ4ZjQtODY3NC1lZTk5NmI5NjE0MGUiLCJhdWQiOiJtYXBzLmdhcm1pbi5jb20iLCJpc3MiOiJnYXJtaW4uY29tIiwiZXhwIjoxNzc2ODg5NTEzLCJpYXQiOjE3NzY4ODIzMTN9.E78246bOMb_H3cB0CzaIok7-K6_G3_OGLGR7JkUS2GxuDMPHpPHI4k3kjTAi4z4T3_LZ93HtOXbKNZe1K1z4RYnXX3sPZb01q4qjtTKtjnuMTZZ9rps_MiA-NhWCINwZspjt4aWA1owj7qKlQIrv0mjbFdMQ3HIqtPlFX6dnTQJcxwO7zzTq3vePpVVUcRmnNVVQs9ICuP7SOsRaygJNjtZjKxBv6pH6smtbirhCOGSmloUK7yAutAqjjo2GOIDpD5P7xI8SO1mStlQqa8slFvqw-hWNR7VV3O_sAF22GbcGeIDxz6L65ebuX4D6rzW8Go0D0GTQOWN6uu2oCNcxGg"
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


def count_tile_jobs(bbox: BBox, zoom_min: int, zoom_max: int) -> int:
    """Count (z,x,y) jobs without storing them (saves huge RAM on big areas)."""
    return sum(1 for _ in iter_jobs(bbox, zoom_min, zoom_max))


def iter_download_tasks(bbox: BBox, zoom_min: int, zoom_max: int, variants):
    """Same order as before: each variant, then every tile in bbox."""
    for v in variants:
        for z, x, y in iter_jobs(bbox, zoom_min, zoom_max):
            yield v, z, x, y


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


def copy_tile_index(out_root: Path) -> None:
    """Copy static index.html (tile viewer) next to this script into out_root."""
    src = Path(__file__).resolve().parent / "index.html"
    if src.is_file():
        shutil.copy2(src, out_root / "index.html")
    else:
        print("warning: index.html not found next to download_tiles.py", file=sys.stderr)


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

    n_tiles = count_tile_jobs(bbox, args.zoom_min, args.zoom_max)
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

    total_requests = n_tiles * len(variants)
    print(
        f"Tiles: {n_tiles}  variants: {len(variants)}  total GETs (approx): {total_requests} "
        "(existing files skipped per variant folder)"
    )
    if args.dry_run:
        print("bbox:", bbox)
        for j in itertools.islice(iter_jobs(bbox, args.zoom_min, args.zoom_max), 3):
            print(" sample tile:", j)
        for v in variants[:4]:
            print(" sample variant:", v.get("slug") or "(root)", v.get("label", ""))
        if len(variants) > 4:
            print(" ...")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    write_manifest(args.out, bbox, args.zoom_min, args.zoom_max, variants)
    copy_tile_index(args.out)

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
    # Keep only a small number of futures in memory (was: submit millions at once → huge RAM + slow start).
    max_in_flight = min(total_requests, max(workers * 8, 32))
    task_it = iter(iter_download_tasks(bbox, args.zoom_min, args.zoom_max, variants))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = set()
        done = 0

        def submit_next():
            try:
                v, z, x, y = next(task_it)
            except StopIteration:
                return False
            pending.add(ex.submit(dl.fetch_one, v, z, x, y))
            return True

        while len(pending) < max_in_flight:
            if not submit_next():
                break

        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            done += len(finished)
            for _ in finished:
                submit_next()
            if done % 200 == 0 or done == total_requests:
                print(f"progress {done}/{total_requests}  stats={dl.stats}")

    print("done:", dl.stats)
    print("Edit index.html beside download_tiles.py; each run copies it to --out.")
    print(f"  cd {args.out.resolve()}")
    print("  python -m http.server 8765")
    print("  http://127.0.0.1:8765/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
