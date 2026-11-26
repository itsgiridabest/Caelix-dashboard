#!/usr/bin/env python3
"""Caelix Dashboard — a small, local-only status page for the Arduino UNO Q.

Serves the dashboard in static/ plus a JSON API:

    GET /api/status              host metrics + Hermes gateway state
    GET /api/weather?lat=&lon=   current weather (Open-Meteo, cached 10 min)
    GET /api/geocode?q=          city lookup (Open-Meteo geocoding)

Standard library only. No AI/model calls. Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8080,
    "hermes_unit": "hermes",
    "hermes_process_match": "hermes",
}

USER_AGENT = "CaelixDashboard/1.0 (local Arduino UNO Q dashboard)"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_TTL = 600
GEOCODE_TTL = 86400

CONFIG = dict(DEFAULTS)
SAMPLER: "Sampler | None" = None


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config() -> dict:
    """config.json is optional; environment variables win over the file."""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            print(f"[caelix] ignoring invalid config.json: {exc}")
    cfg["host"] = os.environ.get("CAELIX_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("CAELIX_PORT", cfg["port"]))
    return cfg


# --------------------------------------------------------------------------
# Host metrics
# --------------------------------------------------------------------------

class Sampler(threading.Thread):
    """Samples CPU and network counters in the background every 2 seconds."""

    def __init__(self, interval: float = 2.0) -> None:
        super().__init__(name="caelix-sampler", daemon=True)
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.cpu_percent = 0.0
        self.rx_rate = 0.0
        self.tx_rate = 0.0
        self.rx_total = 0
        self.tx_total = 0
        self.cpu_history: deque = deque(maxlen=90)
        self.rx_history: deque = deque(maxlen=90)
        self.tx_history: deque = deque(maxlen=90)

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _cpu_counters() -> tuple[int, int]:
        with open("/proc/stat", encoding="ascii") as fh:
            fields = fh.readline().split()[1:]
        values = [int(value) for value in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    @staticmethod
    def _net_counters() -> tuple[int, int]:
        rx = tx = 0
        with open("/proc/net/dev", encoding="ascii") as fh:
            next(fh)
            next(fh)
            for line in fh:
                name, _, rest = line.partition(":")
                if name.strip() == "lo":
                    continue
                fields = rest.split()
                rx += int(fields[0])
                tx += int(fields[8])
        return rx, tx

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu_percent": self.cpu_percent,
                "rx_rate": self.rx_rate,
                "tx_rate": self.tx_rate,
                "rx_total": self.rx_total,
                "tx_total": self.tx_total,
                "cpu_history": list(self.cpu_history),
                "rx_history": list(self.rx_history),
                "tx_history": list(self.tx_history),
            }

    def run(self) -> None:  # noqa: D102 - thread body
        try:
            prev_total, prev_idle = self._cpu_counters()
            prev_rx, prev_tx = self._net_counters()
        except (OSError, ValueError) as exc:
            print(f"[caelix] could not read /proc counters: {exc}")
            return
        prev_time = time.monotonic()
        while not self._stop.wait(self.interval):
            try:
                total, idle = self._cpu_counters()
                rx, tx = self._net_counters()
            except (OSError, ValueError) as exc:
                print(f"[caelix] sample failed: {exc}")
                continue
            now = time.monotonic()
            elapsed = max(now - prev_time, 1e-6)
            delta_total = total - prev_total
            delta_idle = idle - prev_idle
            with self._lock:
                if delta_total > 0:
                    self.cpu_percent = round(
                        100.0 * (delta_total - delta_idle) / delta_total, 1
                    )
                self.rx_rate = max(0.0, (rx - prev_rx) / elapsed)
                self.tx_rate = max(0.0, (tx - prev_tx) / elapsed)
                self.rx_total, self.tx_total = rx, tx
                self.cpu_history.append(round(self.cpu_percent, 1))
                self.rx_history.append(round(self.rx_rate, 1))
                self.tx_history.append(round(self.tx_rate, 1))
            prev_total, prev_idle = total, idle
            prev_rx, prev_tx = rx, tx
            prev_time = now


def read_memory() -> dict:
    info: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return {
        "total": total,
        "used": used,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


def read_disk(path: str = "/") -> dict:
    stats = os.statvfs(path)
    total = stats.f_frsize * stats.f_blocks
    free = stats.f_frsize * stats.f_bavail
    used = total - free
    return {
        "total": total,
        "used": used,
        "free": free,
        "percent": round(100.0 * used / total, 1) if total else 0.0,
    }


def read_uptime() -> float:
    with open("/proc/uptime", encoding="ascii") as fh:
        return float(fh.readline().split()[0])


def read_temperature() -> dict | None:
    """First plausible thermal zone reading, or None on boards without one."""
    try:
        zones = sorted(Path("/sys/class/thermal").glob("thermal_zone*"))
    except OSError:
        return None
    for zone in zones:
        try:
            celsius = int((zone / "temp").read_text().strip()) / 1000.0
        except (OSError, ValueError):
            continue
        if not -20.0 <= celsius <= 150.0:
            continue
        try:
            label = (zone / "type").read_text().strip() or zone.name
        except OSError:
            label = zone.name
        return {"celsius": round(celsius, 1), "label": label, "zone": zone.name}
    return None


def read_system() -> dict:
    board = None
    for candidate in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            board = Path(candidate).read_text(errors="replace").strip("\x00\n ") or None
        except OSError:
            continue
        if board:
            break

    distro = None
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass

    uname = os.uname()
    return {
        "board": board,
        "os": distro,
        "kernel": uname.release,
        "machine": uname.machine,
        "python": platform.python_version(),
    }


# --------------------------------------------------------------------------
# Hermes gateway probe
# --------------------------------------------------------------------------

_hermes_cache: dict = {"at": 0.0, "value": None}
_hermes_lock = threading.Lock()

# Shells and text tools often mention "hermes" without being the gateway.
_WRAPPER_NAMES = {
    "bash", "sh", "dash", "zsh", "fish", "ksh", "csh", "tcsh",
    "sudo", "su", "grep", "pgrep", "systemctl", "git", "vi", "vim", "nano",
}


def check_hermes(unit: str, match: str) -> dict:
    now = time.monotonic()
    with _hermes_lock:
        cached = _hermes_cache["value"]
        if cached is not None and now - _hermes_cache["at"] < 5:
            return cached
    result = _probe_hermes(unit, match)
    with _hermes_lock:
        _hermes_cache["at"] = now
        _hermes_cache["value"] = result
    return result


def _probe_hermes(unit: str, match: str) -> dict:
    if shutil.which("systemctl"):
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=3,
            )
            state = (proc.stdout or "").strip()
            if proc.returncode == 0:
                return {"running": True, "detail": f"systemd unit {unit} {state}"}
            if state in ("inactive", "failed", "deactivating"):
                return {"running": False, "detail": f"systemd unit {unit} {state}"}
        except (OSError, subprocess.SubprocessError):
            pass

    own_pid = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == own_pid:
            continue
        try:
            raw = Path("/proc", entry, "cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if match and _command_matches(cmdline, match):
            return {"running": True, "detail": f"process {entry}: {cmdline[:80]}"}
    return {"running": False, "detail": "no matching service or process found"}


def _command_matches(cmdline: str, match: str) -> bool:
    """Match an executable name or path argument, not prose in a shell line."""
    tokens = [token for token in cmdline.split() if token and not token.startswith("-")]
    if not tokens:
        return False
    if os.path.basename(tokens[0]) in _WRAPPER_NAMES:
        return False
    if match in os.path.basename(tokens[0]):
        return True
    return any("/" in token and match in token for token in tokens[1:])


# --------------------------------------------------------------------------
# Weather (Open-Meteo, free, no API key)
# --------------------------------------------------------------------------

_cache: dict = {}
_cache_lock = threading.Lock()


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def cached(key: tuple, ttl: int, producer):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = producer()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def fetch_weather(lat: float, lon: float) -> dict:
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": ",".join(
            [
                "temperature_2m",
                "relative_humidity_2m",
                "apparent_temperature",
                "is_day",
                "precipitation",
                "weather_code",
                "wind_speed_10m",
                "wind_direction_10m",
            ]
        ),
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }
    data = http_json(f"{WEATHER_URL}?{urllib.parse.urlencode(params)}")
    current = data.get("current") or {}
    units = data.get("current_units") or {}
    return {
        "temperature": current.get("temperature_2m"),
        "apparent": current.get("apparent_temperature"),
        "humidity": current.get("relative_humidity_2m"),
        "precipitation": current.get("precipitation"),
        "wind_speed": current.get("wind_speed_10m"),
        "wind_direction": current.get("wind_direction_10m"),
        "code": current.get("weather_code"),
        "is_day": bool(current.get("is_day", 1)),
        "time": current.get("time"),
        "temperature_unit": units.get("temperature_2m", "°C"),
        "wind_unit": units.get("wind_speed_10m", "km/h"),
    }


def fetch_geocode(query: str) -> dict:
    params = {"name": query, "count": 5, "language": "en", "format": "json"}
    data = http_json(f"{GEOCODE_URL}?{urllib.parse.urlencode(params)}")
    results = []
    for item in data.get("results") or []:
        parts: list[str] = []
        for part in (item.get("name"), item.get("admin1"), item.get("country")):
            if part and part not in parts:
                parts.append(part)
        results.append(
            {
                "name": item.get("name"),
                "label": ", ".join(parts) or query,
                "country": item.get("country"),
                "latitude": item.get("latitude"),
                "longitude": item.get("longitude"),
            }
        )
    return {"results": results}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def build_status() -> dict:
    sampler = SAMPLER.snapshot() if SAMPLER else {}
    return {
        "ok": True,
        "hostname": socket.gethostname(),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uptime_seconds": int(read_uptime()),
        "load": [round(value, 2) for value in os.getloadavg()],
        "cpu": {
            "percent": sampler.get("cpu_percent", 0.0),
            "cores": os.cpu_count() or 1,
            "history": sampler.get("cpu_history", []),
        },
        "memory": read_memory(),
        "disk": read_disk(),
        "network": {
            "rx_rate": round(sampler.get("rx_rate", 0.0), 1),
            "tx_rate": round(sampler.get("tx_rate", 0.0), 1),
            "rx_total": sampler.get("rx_total", 0),
            "tx_total": sampler.get("tx_total", 0),
            "rx_history": sampler.get("rx_history", []),
            "tx_history": sampler.get("tx_history", []),
        },
        "temperature": read_temperature(),
        "system": read_system(),
        "hermes": check_hermes(
            CONFIG.get("hermes_unit", "hermes"),
            CONFIG.get("hermes_process_match", "hermes"),
        ),
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "CaelixDashboard/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):  # keep the journal readable
        print(f"[caelix] {self.address_string()} {fmt % args}")

    def do_GET(self):  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                return self.send_json(build_status())
            if parsed.path == "/api/weather":
                return self.handle_weather(query)
            if parsed.path == "/api/geocode":
                return self.handle_geocode(query)
            return self.send_json({"error": "not found"}, status=404)
        except urllib.error.HTTPError as exc:
            self.send_json({"error": f"weather service returned HTTP {exc.code}"}, status=502)
        except urllib.error.URLError as exc:
            self.send_json({"error": f"weather service unreachable: {exc.reason}"}, status=502)
        except Exception as exc:  # keep one bad request from killing the server
            print(f"[caelix] api error: {exc!r}")
            self.send_json({"error": "internal error"}, status=500)

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_weather(self, query: dict) -> None:
        try:
            lat = float(query.get("lat", [""])[0])
            lon = float(query.get("lon", [""])[0])
        except (TypeError, ValueError):
            return self.send_json({"error": "lat and lon are required"}, status=400)
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return self.send_json({"error": "lat or lon out of range"}, status=400)
        key = ("weather", round(lat, 2), round(lon, 2))
        self.send_json(cached(key, WEATHER_TTL, lambda: fetch_weather(lat, lon)))

    def handle_geocode(self, query: dict) -> None:
        place = (query.get("q", [""])[0] or "").strip()
        if not place:
            return self.send_json({"error": "q is required"}, status=400)
        if len(place) > 100:
            return self.send_json({"error": "q is too long"}, status=400)
        key = ("geocode", place.casefold())
        self.send_json(cached(key, GEOCODE_TTL, lambda: fetch_geocode(place)))


def main() -> None:
    global SAMPLER, CONFIG
    CONFIG = load_config()
    SAMPLER = Sampler()
    SAMPLER.start()
    try:
        httpd = ThreadingHTTPServer((CONFIG["host"], CONFIG["port"]), DashboardHandler)
    except OSError as exc:
        print(f"[caelix] could not bind {CONFIG['host']}:{CONFIG['port']}: {exc}")
        raise SystemExit(1)
    print(
        f"[caelix] serving dashboard on http://{CONFIG['host']}:{CONFIG['port']} "
        "(local only, no AI calls)"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[caelix] shutting down")
    finally:
        SAMPLER.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
