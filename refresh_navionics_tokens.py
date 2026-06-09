"""
Refresh Garmin/Navionics tile tokens with Selenium.

The script opens maps.garmin.com, waits for the marine map to load, captures the
tile request JWTs from Chrome performance logs, and writes them to a local JSON
cache consumed by download_tiles.py.
"""

import argparse
import base64
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


DEFAULT_MAP_URL = (
    "https://maps.garmin.com/en-CA/marine?"
    "maps=another-brand&overlay=false&key=dpxsg3zv4m7n&heatmap=false"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def iso_from_epoch(value):
    if not isinstance(value, int):
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def jwt_role(token: str) -> str:
    payload = decode_jwt(token)
    if payload.get("aud") == "maps.garmin.com" and payload.get("iss") == "garmin.com":
        return "bearer"
    if payload.get("rpn") == "internal_service" and payload.get("apr"):
        return "config"
    return ""


def best_chrome_binary() -> str:
    candidates = (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return ""


def collect_text_from_logs(driver) -> tuple[list[str], list[dict]]:
    text_chunks = []
    tile_requests = []
    for entry in driver.get_log("performance"):
        try:
            msg = json.loads(entry["message"])["message"]
        except Exception:
            continue
        text_chunks.append(json.dumps(msg, separators=(",", ":")))
        if msg.get("method") != "Network.requestWillBeSent":
            continue
        request = msg.get("params", {}).get("request", {})
        url = request.get("url", "")
        if "navionics.com/viewer/api/v1/tile/" not in url:
            continue
        tile_requests.append(
            {
                "url": url,
                "authorization": request.get("headers", {}).get("Authorization")
                or request.get("headers", {}).get("authorization"),
            }
        )
    return text_chunks, tile_requests


def refresh_tokens(args) -> dict:
    chrome_binary = args.chrome_binary or best_chrome_binary()
    options = Options()
    if chrome_binary:
        options.binary_location = chrome_binary
    options.set_capability("goog:loggingPrefs", {"performance": "ALL", "browser": "ALL"})
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=390,844")
    options.add_argument(f"--user-agent={args.user_agent}")
    if args.headless:
        options.add_argument("--headless=new")
    options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_setting_values.geolocation": 2,
            "profile.default_content_setting_values.notifications": 2,
        },
    )

    driver = webdriver.Chrome(options=options)
    try:
        try:
            driver.execute_cdp_cmd("Network.enable", {})
        except Exception:
            pass
        driver.get(args.url)
        time.sleep(args.wait)

        text_chunks, tile_requests = collect_text_from_logs(driver)
        try:
            storage = driver.execute_script(
                """
                const out = {local: {}, session: {}, cookies: document.cookie};
                for (let i = 0; i < localStorage.length; i++) {
                  const k = localStorage.key(i);
                  out.local[k] = localStorage.getItem(k);
                }
                for (let i = 0; i < sessionStorage.length; i++) {
                  const k = sessionStorage.key(i);
                  out.session[k] = sessionStorage.getItem(k);
                }
                return out;
                """
            )
            text_chunks.append(json.dumps(storage, separators=(",", ":")))
        except Exception:
            pass
    finally:
        driver.quit()

    bearer = ""
    config = ""
    for tile_request in tile_requests:
        auth = tile_request.get("authorization") or ""
        if auth.startswith("Bearer "):
            bearer = auth.removeprefix("Bearer ").strip()
        parsed = parse_qs(urlparse(tile_request["url"]).query)
        if parsed.get("config"):
            config = parsed["config"][0]
        if bearer and config:
            break

    for chunk in text_chunks:
        for token in JWT_RE.findall(chunk):
            role = jwt_role(token)
            if role == "bearer" and not bearer:
                bearer = token
            elif role == "config" and not config:
                config = token

    if not bearer or not config:
        raise RuntimeError("Could not find both bearer and config JWTs in Selenium network logs.")

    bearer_payload = decode_jwt(bearer)
    tile_url = tile_requests[0]["url"] if tile_requests else None
    result = {
        "bearer": bearer,
        "config": config,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "bearer_iat": bearer_payload.get("iat"),
        "bearer_iat_iso": iso_from_epoch(bearer_payload.get("iat")),
        "bearer_exp": bearer_payload.get("exp"),
        "bearer_exp_iso": iso_from_epoch(bearer_payload.get("exp")),
        "source_url": args.url,
        "tile_url": tile_url,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Garmin/Navionics tile JWTs via Selenium.")
    parser.add_argument("--url", default=DEFAULT_MAP_URL)
    parser.add_argument("--out", type=Path, default=Path(".navionics_tokens.json"))
    parser.add_argument("--wait", type=float, default=8.0, help="Seconds to wait after opening the map.")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--chrome-binary", default="")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--print-env", action="store_true", help="Print PowerShell env var commands.")
    args = parser.parse_args()

    try:
        tokens = refresh_tokens(args)
    except Exception as exc:
        print(f"token refresh failed: {exc}", file=sys.stderr)
        return 1

    args.out.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    if tokens.get("bearer_exp_iso"):
        print(f"bearer expires: {tokens['bearer_exp_iso']}")
    if args.print_env:
        print(f"$env:NAVIONICS_BEARER='{tokens['bearer']}'")
        print(f"$env:NAVIONICS_CONFIG='{tokens['config']}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
