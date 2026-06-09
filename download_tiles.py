"""
Download XYZ raster tiles from a templated URL (Navionics-style viewer API).

Tokens:
  Use --refresh-tokens to populate .navionics_tokens.json automatically, or set:
    NAVIONICS_BEARER   = Authorization: Bearer <jwt>
    NAVIONICS_CONFIG   = value of the `config` query parameter (JWT string)
  Do not commit live tokens; .navionics_tokens.json is intentionally ignored.

Usage examples (PowerShell):
  python download_tiles.py --bbox -5.2 35.8 10.1 45.2 --zoom-min 10 --zoom-max 16 --out ./tiles_store

  # Default: download sonar with 10 ft shallow shading.
  python download_tiles.py --refresh-tokens --anchor-tile 16/18322/24033 --margin 4 --zoom-min 16 --zoom-max 16 --out ./tiles_store

  # Single set of query params (legacy flat z/x/y under --out):
  python download_tiles.py --variants single --layer 1 --du 2 --sd 29 --sa true --transparent false ...

Notes:
  - Map UI is static `index.html` (loads `manifest.json` via HTTP or file-picker on file://). Copied into `--out` each run.
  - Respect Garmin / Navionics terms and rate limits; raise --delay or lower --workers if throttled.
  - JWTs expire; automatic refresh retries 401s and refreshes before JWT expiry.
  - Reruns resume automatically: existing tile image files are skipped.
  - Shallow (du, sd) presets are best-effort; confirm values in browser DevTools if tiles look wrong.
"""

import argparse
import base64
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import random
import shutil
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterator, Optional, Tuple
from urllib.parse import quote

import requests

# Garmin bearer expires quickly. Prefer --refresh-tokens, .navionics_tokens.json, or NAVIONICS_BEARER.
DEFAULT_NAVIONICS_BEARER = ""
DEFAULT_NAVIONICS_CONFIG = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJycG4iOiJpbnRlcm5hbF9zZXJ2aWNlIiwiYXByIjoiMDEwLUQyMTEyLTEwIn0.Ua7QtpbvTn16y9WDFnzUSiTCzjQbltqcBFAkiFv1PEY"
)
DEFAULT_TOKEN_CACHE = Path(".navionics_tokens.json")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

BBox = Tuple[float, float, float, float]  # west, south, east, north


def decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def iso_from_epoch(value: int) -> str:
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


# Default: sonar only, feet only, 10 ft shallow shading.
SHALLOW_SHADING_PRESETS = (
    ("sh_ft10", "2", "10"),  # 0-10 ft
)


def build_variant_matrix():
    """Default combination: sonar with 10 ft shallow shading."""
    out = []
    for sh_tag, du, sd in SHALLOW_SHADING_PRESETS:
        layer = "1"
        slug = f"L{layer}_du{du}_sd{sd}_sa1_t0_{sh_tag}"
        out.append(
            {
                "slug": slug,
                "layer": layer,
                "du": du,
                "sd": sd,
                "sa": "true",
                "transparent": "false",
                "ugc": "false",
                "label": f"sonar | shallow={sd} ft",
            }
        )
    return tuple(out)


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    """Web Mercator XYZ tile indices (OSM / Google style, y grows toward south)."""
    n = 2**zoom
    lon = min(180.0 - 1e-12, max(-180.0, lon))
    lat = min(85.05112878, max(-85.05112878, lat))
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


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
    for z in range(zoom_max, zoom_min - 1, -1):
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


class TileContentCache:
    def __init__(self, root: Path, dedupe_existing: bool) -> None:
        self.root = root
        self.dedupe_existing = dedupe_existing
        self.lock = threading.Lock()
        self.by_hash = {}
        self.stats = {"indexed": 0, "deduped_existing": 0, "hardlink_failed": 0}

    def digest_bytes(self, content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def digest_file(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def image_files(self):
        if not self.root.is_dir():
            return
        for p in self.root.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                yield p

    def same_file(self, a: Path, b: Path) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return False

    def hardlink_replace(self, src: Path, dst: Path) -> bool:
        if self.same_file(src, dst):
            return True
        tmp = dst.with_name(f"{dst.name}.link_tmp")
        tmp.unlink(missing_ok=True)
        try:
            os.link(src, tmp)
            tmp.replace(dst)
            return True
        except OSError:
            tmp.unlink(missing_ok=True)
            return False

    def scan_existing(self) -> None:
        for path in self.image_files() or ():
            try:
                digest = self.digest_file(path)
            except OSError:
                continue
            with self.lock:
                canonical = self.by_hash.get(digest)
                if canonical is None or not canonical.is_file():
                    self.by_hash[digest] = path
                    self.stats["indexed"] += 1
                    continue
            if self.dedupe_existing:
                if self.hardlink_replace(canonical, path):
                    with self.lock:
                        self.stats["deduped_existing"] += 1
                else:
                    with self.lock:
                        self.stats["hardlink_failed"] += 1

    def put(self, content: bytes, ext: str, final: Path, tmp_path: Path) -> str:
        digest = self.digest_bytes(content)
        with self.lock:
            canonical = self.by_hash.get(digest)
            if canonical is not None and not canonical.is_file():
                canonical = None
                self.by_hash.pop(digest, None)
            if canonical is not None:
                if self.hardlink_replace(canonical, final):
                    return "cached"
                tmp_path.write_bytes(content)
                tmp_path.replace(final)
                return "copy"

            tmp_path.write_bytes(content)
            tmp_path.replace(final)
            self.by_hash[digest] = final
            self.stats["indexed"] += 1
            return "stored"


class DedupeReporter:
    def __init__(self, content_cache: TileContentCache, report_path: Path, interval_s: float) -> None:
        self.content_cache = content_cache
        self.report_path = report_path
        self.interval_s = max(0.0, interval_s)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self) -> None:
        if self.interval_s <= 0:
            self.write_report("disabled")
            return
        self.thread = threading.Thread(target=self.run, name="dedupe-reporter", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1.0, min(self.interval_s, 10.0)))
        self.write_report("final")

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            self.content_cache.scan_existing()
            self.write_report("running")

    def image_storage_stats(self) -> dict:
        files = 0
        apparent_bytes = 0
        actual_bytes = 0
        seen_inodes = set()
        for path in self.content_cache.image_files() or ():
            try:
                st = path.stat()
            except OSError:
                continue
            files += 1
            apparent_bytes += st.st_size
            key = (getattr(st, "st_dev", 0), getattr(st, "st_ino", str(path.resolve())))
            if key not in seen_inodes:
                seen_inodes.add(key)
                actual_bytes += st.st_size
        return {
            "files": files,
            "unique_storage_files": len(seen_inodes),
            "apparent_bytes": apparent_bytes,
            "actual_bytes": actual_bytes,
            "saved_bytes": max(0, apparent_bytes - actual_bytes),
        }

    def fmt_bytes(self, n: int) -> str:
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        val = float(n)
        for unit in units:
            if val < 1024 or unit == units[-1]:
                return f"{val:.2f} {unit}"
            val /= 1024
        return f"{n} B"

    def write_report(self, status: str) -> None:
        storage = self.image_storage_stats()
        with self.content_cache.lock:
            cache_stats = dict(self.content_cache.stats)
            unique_hashes = len(self.content_cache.by_hash)
        lines = [
            f"status: {status}",
            f"updated_at: {dt.datetime.now(dt.timezone.utc).isoformat()}",
            f"tile_files: {storage['files']}",
            f"unique_storage_files: {storage['unique_storage_files']}",
            f"unique_hashes_indexed: {unique_hashes}",
            f"apparent_size: {self.fmt_bytes(storage['apparent_bytes'])} ({storage['apparent_bytes']} bytes)",
            f"actual_size_after_hardlinks: {self.fmt_bytes(storage['actual_bytes'])} ({storage['actual_bytes']} bytes)",
            f"estimated_storage_saved: {self.fmt_bytes(storage['saved_bytes'])} ({storage['saved_bytes']} bytes)",
            f"dedupe_existing_replacements: {cache_stats.get('deduped_existing', 0)}",
            f"hardlink_failed: {cache_stats.get('hardlink_failed', 0)}",
        ]
        tmp = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.report_path)


class TileDownloader:
    def __init__(
        self,
        session: requests.Session,
        url_builder,
        out_root: Path,
        referer: str,
        origin: str,
        delay_s: float,
        delay_jitter_s: float,
        lock: threading.Lock,
        content_cache: TileContentCache,
        token_manager,
        auth_retries: int,
    ) -> None:
        self.session = session
        self.url_builder = url_builder
        self.out_root = out_root
        self.referer = referer
        self.origin = origin
        self.delay_s = delay_s
        self.delay_jitter_s = max(0.0, delay_jitter_s)
        self.lock = lock
        self.content_cache = content_cache
        self.throttle_lock = threading.Lock()
        self.next_request_at = 0.0
        self.token_manager = token_manager
        self.auth_retries = max(0, auth_retries)
        self.stats = {
            "ok": 0,
            "cached": 0,
            "cache_copy": 0,
            "skip": 0,
            "forbidden": 0,
            "rate_limited": 0,
            "fail": 0,
            "retry": 0,
            "auth_refresh": 0,
        }

    def wait_for_rate_limit_slot(self) -> None:
        delay = max(0.0, self.delay_s) + random.uniform(0.0, self.delay_jitter_s)
        if delay <= 0:
            return
        with self.throttle_lock:
            now = time.monotonic()
            wait_s = max(0.0, self.next_request_at - now)
            self.next_request_at = max(now, self.next_request_at) + delay
        if wait_s > 0:
            time.sleep(wait_s)

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

        path_unknown.unlink(missing_ok=True)

        r = None
        for attempt in range(self.auth_retries + 1):
            self.wait_for_rate_limit_slot()

            if self.token_manager.bearer_expires_soon():
                refreshed, launched_refresh = self.token_manager.refresh(
                    self.session,
                    "Bearer is close to expiry",
                )
                if refreshed:
                    with self.lock:
                        if launched_refresh:
                            self.stats["auth_refresh"] += 1
                else:
                    break

            token_version = self.token_manager.version
            url = self.url_builder(z, x, y, variant)
            r = self.session.get(url, headers=headers, timeout=60)
            if r.status_code == 429:
                with self.lock:
                    self.stats["rate_limited"] += 1
                break
            if r.status_code == 403 and not self.token_manager.bearer_expires_soon():
                break
            if r.status_code != 401 and r.status_code != 403:
                break
            if attempt >= self.auth_retries:
                break
            refreshed, launched_refresh = self.token_manager.refresh_after_auth_failure(self.session, token_version)
            if not refreshed:
                break
            with self.lock:
                self.stats["retry"] += 1
                if launched_refresh:
                    self.stats["auth_refresh"] += 1

        if r is None:
            with self.lock:
                self.stats["fail"] += 1
            return
        if r.status_code == 403:
            with self.lock:
                self.stats["forbidden"] += 1
            tag = slug or "default"
            sys.stderr.write(f"[forbidden] {tag} z={z} x={x} y={y} status=403\n")
            return
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
        cache_result = self.content_cache.put(r.content, ext, final, tmp)
        with self.lock:
            if cache_result == "cached":
                self.stats["cached"] += 1
            elif cache_result == "copy":
                self.stats["cache_copy"] += 1
            else:
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


def load_token_cache(path: Path) -> Tuple[str, str]:
    if not path.is_file():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"warning: could not read token cache {path}: {exc}", file=sys.stderr)
        return "", ""
    return str(data.get("bearer") or "").strip(), str(data.get("config") or "").strip()


def refresh_token_cache(path: Path, headless: bool, wait_s: float) -> None:
    script = Path(__file__).resolve().parent / "refresh_navionics_tokens.py"
    if not script.is_file():
        raise FileNotFoundError(f"missing token refresh script: {script}")
    cmd = [
        sys.executable,
        str(script),
        "--out",
        str(path),
        "--wait",
        str(wait_s),
    ]
    if headless:
        cmd.append("--headless")
    subprocess.run(cmd, check=True)


class TokenManager:
    def __init__(
        self,
        bearer: str,
        config: str,
        cache_path: Path,
        refresh_headless: bool,
        refresh_wait_s: float,
        refresh_before_expiry_s: int,
        auto_refresh: bool,
    ) -> None:
        self.bearer = bearer
        self.config = config
        self.cache_path = cache_path
        self.refresh_headless = refresh_headless
        self.refresh_wait_s = refresh_wait_s
        self.refresh_before_expiry_s = refresh_before_expiry_s
        self.auto_refresh = auto_refresh
        self.lock = threading.Lock()
        self.version = 0

    def apply_to_session(self, session: requests.Session) -> None:
        session.headers["Authorization"] = f"Bearer {self.bearer}"

    def bearer_expiry(self) -> int:
        payload = decode_jwt_payload(self.bearer)
        exp = payload.get("exp")
        return exp if isinstance(exp, int) else 0

    def bearer_expires_soon(self) -> bool:
        exp = self.bearer_expiry()
        if not exp:
            return False
        return exp <= int(time.time()) + self.refresh_before_expiry_s

    def refresh(self, session: requests.Session, reason: str) -> Tuple[bool, bool]:
        if not self.auto_refresh:
            return False, False
        with self.lock:
            print(f"{reason}; refreshing Garmin/Navionics token with Selenium...", file=sys.stderr)
            try:
                refresh_token_cache(self.cache_path, self.refresh_headless, self.refresh_wait_s)
                bearer, config = load_token_cache(self.cache_path)
            except subprocess.CalledProcessError as exc:
                print(f"Token refresh failed with exit code {exc.returncode}.", file=sys.stderr)
                return False, False
            except Exception as exc:
                print(f"Token refresh failed: {exc}", file=sys.stderr)
                return False, False
            if not bearer or not config:
                print(f"Token refresh did not produce both bearer and config in {self.cache_path}.", file=sys.stderr)
                return False, False
            self.bearer = bearer
            self.config = config
            self.version += 1
            self.apply_to_session(session)
            exp = self.bearer_expiry()
            if exp:
                print(f"bearer usable until {iso_from_epoch(exp)}", file=sys.stderr)
            return True, True

    def refresh_after_auth_failure(self, session: requests.Session, seen_version: int) -> Tuple[bool, bool]:
        if not self.auto_refresh:
            return False, False
        with self.lock:
            if self.version != seen_version:
                self.apply_to_session(session)
                return True, False
        return self.refresh(session, "Authorization failed")


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
        default=0.01,
        help="Minimum seconds between request starts across all workers.",
    )
    p.add_argument(
        "--delay-jitter",
        type=float,
        default=0.15,
        help="Extra random seconds added to --delay for each request.",
    )
    p.add_argument("--referer", default="https://maps.garmin.com/")
    p.add_argument("--origin", default="https://maps.garmin.com")
    p.add_argument(
        "--variants",
        choices=("all", "single"),
        default="all",
        help="all: nautical+sonar x feet shallow presets (see SHALLOW_SHADING_PRESETS). "
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
    p.add_argument(
        "--token-cache",
        type=Path,
        default=DEFAULT_TOKEN_CACHE,
        help="JSON token cache written by refresh_navionics_tokens.py.",
    )
    p.add_argument(
        "--refresh-tokens",
        action="store_true",
        help="Force a Selenium token refresh before downloading.",
    )
    p.add_argument(
        "--no-auto-refresh-tokens",
        action="store_true",
        help="Disable automatic Selenium refresh on missing tokens, 401s, or near-expiry JWTs.",
    )
    p.add_argument(
        "--refresh-headless",
        action="store_true",
        help="Run the Selenium refresh browser in headless mode.",
    )
    p.add_argument(
        "--refresh-wait",
        type=float,
        default=8.0,
        help="Seconds to wait for maps.garmin.com during --refresh-tokens.",
    )
    p.add_argument(
        "--refresh-before-expiry",
        type=int,
        default=300,
        help="Refresh JWT this many seconds before its exp timestamp.",
    )
    p.add_argument(
        "--auth-retries",
        type=int,
        default=2,
        help="Retries per tile after refreshing tokens for auth failures.",
    )
    p.add_argument(
        "--no-dedupe-existing",
        action="store_true",
        help="Do not scan and hardlink duplicate tile images already present under --out.",
    )
    p.add_argument(
        "--dedupe-report-interval",
        type=float,
        default=300.0,
        help="Seconds between background duplicate scans and storage-savings report updates. Use 0 to disable.",
    )
    p.add_argument(
        "--dedupe-report",
        type=Path,
        default=None,
        help="Path for duplicate-storage report text file (default: OUT/dedupe_report.txt).",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.anchor_tile:
        bbox = anchor_margin_bbox(args.anchor_tile, args.margin)
    else:
        bbox = tuple(args.bbox)  # type: ignore[assignment]

    if args.zoom_min > args.zoom_max:
        p.error("--zoom-min must be <= --zoom-max")

    auto_refresh_tokens = not args.no_auto_refresh_tokens
    if args.refresh_tokens and not args.dry_run:
        try:
            refresh_token_cache(args.token_cache, args.refresh_headless, args.refresh_wait)
        except subprocess.CalledProcessError as exc:
            print(f"Token refresh failed with exit code {exc.returncode}.", file=sys.stderr)
            return exc.returncode or 1
        except Exception as exc:
            print(f"Token refresh failed: {exc}", file=sys.stderr)
            return 1

    cache_bearer, cache_config = load_token_cache(args.token_cache)
    bearer = (os.environ.get("NAVIONICS_BEARER") or cache_bearer or DEFAULT_NAVIONICS_BEARER).strip()
    config = (os.environ.get("NAVIONICS_CONFIG") or cache_config or DEFAULT_NAVIONICS_CONFIG).strip()
    if not args.dry_run and auto_refresh_tokens and (not bearer or not config):
        try:
            refresh_token_cache(args.token_cache, args.refresh_headless, args.refresh_wait)
            cache_bearer, cache_config = load_token_cache(args.token_cache)
            bearer = (os.environ.get("NAVIONICS_BEARER") or cache_bearer or DEFAULT_NAVIONICS_BEARER).strip()
            config = (os.environ.get("NAVIONICS_CONFIG") or cache_config or DEFAULT_NAVIONICS_CONFIG).strip()
        except subprocess.CalledProcessError as exc:
            print(f"Token refresh failed with exit code {exc.returncode}.", file=sys.stderr)
            return exc.returncode or 1
        except Exception as exc:
            print(f"Token refresh failed: {exc}", file=sys.stderr)
            return 1
    if not args.dry_run and (not bearer or not config):
        print(
            "Missing tokens: run refresh_navionics_tokens.py, keep automatic refresh enabled, "
            "or set NAVIONICS_* env vars.",
            file=sys.stderr,
        )
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

    content_cache = TileContentCache(args.out, dedupe_existing=not args.no_dedupe_existing)
    print("Indexing existing tile images for duplicate-content cache...")
    content_cache.scan_existing()
    print(f"content cache: {content_cache.stats}")
    report_path = args.dedupe_report or (args.out / "dedupe_report.txt")
    dedupe_reporter = DedupeReporter(content_cache, report_path, args.dedupe_report_interval)
    dedupe_reporter.write_report("starting")
    dedupe_reporter.start()

    session = requests.Session()
    token_manager = TokenManager(
        bearer=bearer,
        config=config,
        cache_path=args.token_cache,
        refresh_headless=args.refresh_headless,
        refresh_wait_s=args.refresh_wait,
        refresh_before_expiry_s=args.refresh_before_expiry,
        auto_refresh=auto_refresh_tokens,
    )
    token_manager.apply_to_session(session)

    # Normalize template: allow user to pass {{z}} style
    template = args.base
    template = template.replace("{{z}}", "{z}").replace("{{x}}", "{x}").replace("{{y}}", "{y}")

    def url_builder(z: int, x: int, y: int, v: dict) -> str:
        return build_url(
            template,
            z,
            x,
            y,
            token_manager.config,
            v["transparent"],
            v["ugc"],
            v["layer"],
            v["du"],
            v["sd"],
            v["sa"],
        )

    lock = threading.Lock()
    dl = TileDownloader(
        session,
        url_builder,
        args.out,
        args.referer,
        args.origin,
        args.delay,
        args.delay_jitter,
        lock,
        content_cache,
        token_manager,
        args.auth_retries,
    )

    workers = max(1, args.workers)
    # Keep only a small number of futures in memory (was: submit millions at once → huge RAM + slow start).
    max_in_flight = min(total_requests, max(workers * 8, 32))
    print(f"Starting downloads with workers={workers}, max_in_flight={max_in_flight}")
    task_it = iter(iter_download_tasks(bbox, args.zoom_min, args.zoom_max, variants))
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            pending = set()
            done = 0
            submitted = 0
            last_heartbeat = time.monotonic()

            def submit_next():
                nonlocal submitted
                try:
                    v, z, x, y = next(task_it)
                except StopIteration:
                    return False
                pending.add(ex.submit(dl.fetch_one, v, z, x, y))
                submitted += 1
                return True

            while len(pending) < max_in_flight:
                if not submit_next():
                    break

            while pending:
                finished, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
                if not finished:
                    now = time.monotonic()
                    if now - last_heartbeat >= 30:
                        print(
                            f"heartbeat completed={done}/{total_requests} "
                            f"submitted={submitted} pending={len(pending)} stats={dl.stats}"
                        )
                        last_heartbeat = now
                    continue
                done += len(finished)
                for _ in finished:
                    submit_next()
                if done % 200 == 0 or done == total_requests:
                    print(f"progress {done}/{total_requests}  stats={dl.stats}")
    finally:
        dedupe_reporter.stop()

    print("done:", dl.stats)
    print(f"dedupe report: {report_path.resolve()}")
    print("Edit index.html beside download_tiles.py; each run copies it to --out.")
    print(f"  cd {args.out.resolve()}")
    print("  python -m http.server 8765")
    print("  http://127.0.0.1:8765/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
