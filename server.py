#!/usr/bin/env python3
"""Caelix Dashboard — a small, local-only status page for the Arduino UNO Q.

Serves the dashboard in static/ plus a JSON API:

    GET /api/status              host metrics + Hermes gateway state
    GET /api/usage               Codex CLI token use and rate limits (local logs)
    GET /api/weather?lat=&lon=   current weather (Open-Meteo, cached 10 min)
    GET /api/geocode?q=          city lookup (Open-Meteo geocoding)

Standard library only. No AI/model calls. Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import json
import mimetypes
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta, timezone
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
    "codex_home": "~/.codex",
    "media_dir": "media",
}

USER_AGENT = "CaelixDashboard/1.0 (local Arduino UNO Q dashboard)"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_TTL = 600
GEOCODE_TTL = 86400
CODEX_USAGE_TTL = 60

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ogv", ".ogg", ".mov"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".oga"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
}

CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
WHOLE_DISK = re.compile(r"^(?:[shvx]d[a-z]+|nvme\d+n\d+|mmcblk\d+)$")

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
    """Samples CPU, network, disk and process counters every 2 seconds."""

    def __init__(self, interval: float = 2.0) -> None:
        super().__init__(name="caelix-sampler", daemon=True)
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.cpu_percent = 0.0
        self.core_percent: list[float] = []
        self.rx_rate = 0.0
        self.tx_rate = 0.0
        self.rx_total = 0
        self.tx_total = 0
        self.disk_read_rate = 0.0
        self.disk_write_rate = 0.0
        self.disk_read_total = 0
        self.disk_write_total = 0
        self.process_count = 0
        self.processes: list[dict] = []
        self.cpu_history: deque = deque(maxlen=90)
        self.rx_history: deque = deque(maxlen=90)
        self.tx_history: deque = deque(maxlen=90)
        self.disk_read_history: deque = deque(maxlen=90)
        self.disk_write_history: deque = deque(maxlen=90)

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _cpu_snapshot() -> tuple[int, int, list[tuple[int, int]]]:
        """Overall CPU counters plus one (total, idle) pair per logical core."""
        total = idle = 0
        cores: list[tuple[int, int]] = []
        with open("/proc/stat", encoding="ascii") as fh:
            for line in fh:
                if not line.startswith("cpu"):
                    continue
                parts = line.split()
                name = parts[0]
                values = [int(value) for value in parts[1:]]
                core_idle = values[3] + (values[4] if len(values) > 4 else 0)
                core_total = sum(values)
                if name == "cpu":
                    total, idle = core_total, core_idle
                elif name[3:].isdigit():
                    cores.append((core_total, core_idle))
        return total, idle, cores

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

    @staticmethod
    def _disk_counters() -> tuple[int, int]:
        """Bytes read and written across physical disks (not partitions)."""
        read_bytes = write_bytes = 0
        with open("/proc/diskstats", encoding="ascii") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 10 or not WHOLE_DISK.match(fields[2]):
                    continue
                read_bytes += int(fields[5]) * 512
                write_bytes += int(fields[9]) * 512
        return read_bytes, write_bytes

    @staticmethod
    def _process_counters() -> dict[int, dict]:
        processes: dict[int, dict] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                raw = Path("/proc", entry, "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            end = raw.rfind(")")
            if end < 0:
                continue
            rest = raw[end + 2 :].split()
            if len(rest) < 20:
                continue
            try:
                ticks = int(rest[11]) + int(rest[12])
                start = int(rest[19])
                resident = int(Path("/proc", entry, "statm").read_text().split()[1])
            except (OSError, ValueError, IndexError):
                continue
            comm = raw[:end]
            name = comm[comm.find("(") + 1 :] or entry
            processes[int(entry)] = {
                "name": name,
                "ticks": ticks,
                "start": start,
                "rss": resident * PAGE_SIZE,
            }
        return processes

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu_percent": self.cpu_percent,
                "core_percent": list(self.core_percent),
                "rx_rate": self.rx_rate,
                "tx_rate": self.tx_rate,
                "rx_total": self.rx_total,
                "tx_total": self.tx_total,
                "disk_read_rate": self.disk_read_rate,
                "disk_write_rate": self.disk_write_rate,
                "disk_read_total": self.disk_read_total,
                "disk_write_total": self.disk_write_total,
                "process_count": self.process_count,
                "processes": list(self.processes),
                "cpu_history": list(self.cpu_history),
                "rx_history": list(self.rx_history),
                "tx_history": list(self.tx_history),
                "disk_read_history": list(self.disk_read_history),
                "disk_write_history": list(self.disk_write_history),
            }

    def run(self) -> None:  # noqa: D102 - thread body
        try:
            prev_total, prev_idle, prev_cores = self._cpu_snapshot()
            prev_rx, prev_tx = self._net_counters()
            prev_read, prev_write = self._disk_counters()
            prev_procs = self._process_counters()
        except (OSError, ValueError) as exc:
            print(f"[caelix] could not read /proc counters: {exc}")
            return
        prev_time = time.monotonic()
        while not self._stop.wait(self.interval):
            try:
                total, idle, cores = self._cpu_snapshot()
                rx, tx = self._net_counters()
                disk_read, disk_write = self._disk_counters()
                procs = self._process_counters()
            except (OSError, ValueError) as exc:
                print(f"[caelix] sample failed: {exc}")
                continue
            now = time.monotonic()
            elapsed = max(now - prev_time, 1e-6)
            delta_total = total - prev_total
            delta_idle = idle - prev_idle

            core_percent: list[float] = []
            if len(cores) == len(prev_cores):
                for (core_total, core_idle), (old_total, old_idle) in zip(cores, prev_cores):
                    delta = core_total - old_total
                    busy = delta - (core_idle - old_idle)
                    core_percent.append(round(100.0 * busy / delta, 1) if delta > 0 else 0.0)

            entries = []
            for pid, info in procs.items():
                before = prev_procs.get(pid)
                cpu = 0.0
                if (
                    before
                    and before["start"] == info["start"]
                    and info["ticks"] >= before["ticks"]
                ):
                    cpu = 100.0 * (info["ticks"] - before["ticks"]) / (elapsed * CLOCK_TICKS)
                entries.append(
                    {
                        "pid": pid,
                        "name": info["name"],
                        "cpu_percent": round(cpu, 1),
                        "rss": info["rss"],
                    }
                )
            top = {}
            for item in sorted(entries, key=lambda entry: entry["cpu_percent"], reverse=True)[:8]:
                top[item["pid"]] = item
            for item in sorted(entries, key=lambda entry: entry["rss"], reverse=True)[:8]:
                top[item["pid"]] = item
            top_processes = sorted(
                top.values(), key=lambda entry: entry["cpu_percent"], reverse=True
            )

            with self._lock:
                if delta_total > 0:
                    self.cpu_percent = round(
                        100.0 * (delta_total - delta_idle) / delta_total, 1
                    )
                self.core_percent = core_percent
                self.rx_rate = max(0.0, (rx - prev_rx) / elapsed)
                self.tx_rate = max(0.0, (tx - prev_tx) / elapsed)
                self.disk_read_rate = max(0.0, (disk_read - prev_read) / elapsed)
                self.disk_write_rate = max(0.0, (disk_write - prev_write) / elapsed)
                self.rx_total, self.tx_total = rx, tx
                self.disk_read_total, self.disk_write_total = disk_read, disk_write
                self.process_count = len(procs)
                self.processes = top_processes
                self.cpu_history.append(round(self.cpu_percent, 1))
                self.rx_history.append(round(self.rx_rate, 1))
                self.tx_history.append(round(self.tx_rate, 1))
                self.disk_read_history.append(round(self.disk_read_rate, 1))
                self.disk_write_history.append(round(self.disk_write_rate, 1))
            prev_total, prev_idle, prev_cores = total, idle, cores
            prev_rx, prev_tx = rx, tx
            prev_read, prev_write = disk_read, disk_write
            prev_procs = procs
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
# Codex CLI usage (read from the gateway's local rollout files)
# --------------------------------------------------------------------------

_usage_lock = threading.Lock()


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME") or CONFIG.get("codex_home") or "~/.codex"
    return Path(os.path.expanduser(str(raw)))


def _parse_timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _empty_usage() -> dict:
    return {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0, "requests": 0}


def _add_usage(bucket: dict, usage: dict) -> None:
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    bucket["input"] += input_tokens
    bucket["cached"] += _as_int(usage.get("cached_input_tokens"))
    bucket["output"] += output_tokens
    bucket["reasoning"] += _as_int(usage.get("reasoning_output_tokens"))
    bucket["total"] += _as_int(usage.get("total_tokens")) or input_tokens + output_tokens
    bucket["requests"] += 1


def _rollout_files(sessions: Path, since: date) -> list[Path]:
    """Walk only the YYYY/MM/DD folders inside the requested date range."""
    files: list[Path] = []
    try:
        years = list(sessions.iterdir())
    except OSError:
        return files
    for year_dir in years:
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        try:
            months = list(year_dir.iterdir())
        except OSError:
            continue
        for month_dir in months:
            if not month_dir.is_dir() or not month_dir.name.isdigit():
                continue
            try:
                days = list(month_dir.iterdir())
            except OSError:
                continue
            for day_dir in days:
                if not day_dir.is_dir() or not day_dir.name.isdigit():
                    continue
                try:
                    folder_date = date(int(year_dir.name), int(month_dir.name), int(day_dir.name))
                except ValueError:
                    continue
                if folder_date < since:
                    continue
                try:
                    files.extend(day_dir.glob("rollout-*.jsonl"))
                except OSError:
                    continue
    return files


def _usage_limits(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None

    def window(name: str) -> dict | None:
        block = raw.get(name)
        if not isinstance(block, dict) or block.get("used_percent") is None:
            return None
        try:
            percent = round(float(block["used_percent"]), 1)
        except (TypeError, ValueError):
            return None
        return {
            "used_percent": percent,
            "window_minutes": block.get("window_minutes"),
            "resets_at": block.get("resets_at"),
        }

    primary = window("primary")
    secondary = window("secondary")
    if primary is None and secondary is None:
        return None
    credits = raw.get("credits") if isinstance(raw.get("credits"), dict) else None
    return {
        "plan": raw.get("plan_type"),
        "primary": primary,
        "secondary": secondary,
        "credits": (
            {
                "has_credits": credits.get("has_credits"),
                "unlimited": credits.get("unlimited"),
                "balance": credits.get("balance"),
            }
            if credits
            else None
        ),
    }


def read_codex_usage() -> dict:
    """Aggregate token_count events from Codex CLI's local session rollouts."""
    sessions = _codex_home() / "sessions"
    now = datetime.now().astimezone()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=6)
    month_start = today_start - timedelta(days=29)
    cutoffs = {
        "today": today_start.timestamp(),
        "week": week_start.timestamp(),
        "month": month_start.timestamp(),
    }
    buckets = {name: _empty_usage() for name in cutoffs}
    limits = None
    model = None
    latest_at = None
    files = _rollout_files(sessions, month_start.date())

    for path in files:
        previous_total = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"token_count"' not in line and '"turn_context"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    entry_type = entry.get("type")
                    payload = entry.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if entry_type == "turn_context":
                        if payload.get("model"):
                            model = payload["model"]
                        continue
                    if entry_type != "event_msg" or payload.get("type") != "token_count":
                        continue

                    info = payload.get("info") or {}
                    usage = info.get("last_token_usage") or payload.get("last_token_usage") or {}
                    totals = info.get("total_token_usage") or payload.get("total_token_usage") or {}
                    raw_total = totals.get("total_tokens")
                    observed_total = _as_int(raw_total) if raw_total is not None else None

                    if usage:
                        delta = usage
                    elif previous_total is not None and observed_total is not None:
                        delta = {"total_tokens": max(0, observed_total - previous_total)}
                    else:
                        delta = None
                    if observed_total is not None:
                        previous_total = observed_total

                    if delta:
                        timestamp = _parse_timestamp(entry.get("timestamp"))
                        stamp = timestamp.timestamp() if timestamp else time.time()
                        for name, cutoff in cutoffs.items():
                            if stamp >= cutoff:
                                _add_usage(buckets[name], delta)
                        if latest_at is None or stamp > latest_at:
                            latest_at = stamp

                    raw_limits = payload.get("rate_limits")
                    if isinstance(raw_limits, dict):
                        limits = raw_limits
        except OSError:
            continue

    available = bool(files) and (buckets["month"]["requests"] > 0 or limits is not None)
    result = {
        "available": available,
        "path": str(sessions),
        "sessions": len(files),
        "today": buckets["today"],
        "week": buckets["week"],
        "month": buckets["month"],
        "limits": _usage_limits(limits),
        "latest": {
            "at": (
                datetime.fromtimestamp(latest_at, timezone.utc).isoformat(timespec="seconds")
                if latest_at
                else None
            ),
            "model": model,
        },
    }
    if not available:
        result["reason"] = (
            f"No Codex CLI sessions found under {sessions}"
            if not files
            else "Recent Codex CLI sessions contain no token counts"
        )
    return result


def codex_usage() -> dict:
    with _usage_lock:
        return cached(("codex-usage",), CODEX_USAGE_TTL, read_codex_usage)


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
        "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
        "daily": "sunrise,sunset",
        "forecast_days": "2",
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }
    data = http_json(f"{WEATHER_URL}?{urllib.parse.urlencode(params)}")
    current = data.get("current") or {}
    units = data.get("current_units") or {}

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    now_hour = str(current.get("time") or "")[:13]
    forecast = []
    for index, stamp in enumerate(times):
        if not isinstance(stamp, str) or stamp[:13] < now_hour:
            continue
        values = hourly.get("temperature_2m") or []
        codes = hourly.get("weather_code") or []
        rain = hourly.get("precipitation_probability") or []
        daylight = hourly.get("is_day") or []
        if index >= len(values):
            break
        forecast.append(
            {
                "time": stamp,
                "temperature": values[index],
                "code": codes[index] if index < len(codes) else None,
                "precipitation_probability": rain[index] if index < len(rain) else None,
                "is_day": bool(daylight[index]) if index < len(daylight) else True,
            }
        )
        if len(forecast) >= 6:
            break

    daily = data.get("daily") or {}
    sunrise = (daily.get("sunrise") or [None])[0]
    sunset = (daily.get("sunset") or [None])[0]

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
        "hourly": forecast,
        "sunrise": sunrise,
        "sunset": sunset,
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
# Local media library
# --------------------------------------------------------------------------

def media_root() -> Path:
    raw = os.environ.get("CAELIX_MEDIA_DIR") or CONFIG.get("media_dir") or "media"
    path = Path(os.path.expanduser(str(raw)))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def media_file(name: str) -> Path | None:
    """Resolve one file inside the media folder, refusing anything outside it."""
    if not name or "/" in name or "\\" in name:
        return None
    root = media_root()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix.lower() not in MEDIA_EXTENSIONS or not candidate.is_file():
        return None
    return candidate


def list_media() -> dict:
    root = media_root()
    try:
        entries = list(root.iterdir())
    except OSError:
        return {"available": False, "path": str(root), "items": []}
    items = []
    for entry in entries:
        try:
            if not entry.is_file() or entry.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            stats = entry.stat()
        except OSError:
            continue
        suffix = entry.suffix.lower()
        items.append(
            {
                "name": entry.name,
                "url": "/media/" + urllib.parse.quote(entry.name),
                "size": stats.st_size,
                "kind": "video" if suffix in VIDEO_EXTENSIONS else "audio",
                "modified": datetime.fromtimestamp(
                    stats.st_mtime, timezone.utc
                ).isoformat(timespec="seconds"),
            }
        )
    items.sort(key=lambda item: item["name"].casefold())
    return {"available": True, "path": str(root), "items": items}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def build_status() -> dict:
    sampler = SAMPLER.snapshot() if SAMPLER else {}
    disk = read_disk()
    disk.update(
        {
            "read_rate": round(sampler.get("disk_read_rate", 0.0), 1),
            "write_rate": round(sampler.get("disk_write_rate", 0.0), 1),
            "read_total": sampler.get("disk_read_total", 0),
            "write_total": sampler.get("disk_write_total", 0),
            "read_history": sampler.get("disk_read_history", []),
            "write_history": sampler.get("disk_write_history", []),
        }
    )
    return {
        "ok": True,
        "hostname": socket.gethostname(),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uptime_seconds": int(read_uptime()),
        "load": [round(value, 2) for value in os.getloadavg()],
        "cpu": {
            "percent": sampler.get("cpu_percent", 0.0),
            "cores": os.cpu_count() or 1,
            "core_percent": sampler.get("core_percent", []),
            "history": sampler.get("cpu_history", []),
        },
        "memory": read_memory(),
        "disk": disk,
        "network": {
            "rx_rate": round(sampler.get("rx_rate", 0.0), 1),
            "tx_rate": round(sampler.get("tx_rate", 0.0), 1),
            "rx_total": sampler.get("rx_total", 0),
            "tx_total": sampler.get("tx_total", 0),
            "rx_history": sampler.get("rx_history", []),
            "tx_history": sampler.get("tx_history", []),
        },
        "processes": {
            "count": sampler.get("process_count", 0),
            "top": sampler.get("processes", []),
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
        if parsed.path.startswith("/media/"):
            return self.handle_media(parsed.path[len("/media/") :])
        if not parsed.path.startswith("/api/"):
            return super().do_GET()
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                return self.send_json(build_status())
            if parsed.path == "/api/usage":
                return self.send_json(codex_usage())
            if parsed.path == "/api/media":
                return self.send_json(list_media())
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

    def handle_media(self, raw_name: str) -> None:
        path = media_file(urllib.parse.unquote(raw_name))
        if path is None:
            return self.send_error(404, "Media not found")
        try:
            size = path.stat().st_size
        except OSError:
            return self.send_error(404, "Media not found")
        if size <= 0:
            return self.send_error(404, "Media not found")

        content_type = MEDIA_TYPES.get(path.suffix.lower()) or (
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        start, end = 0, size - 1
        status = 200
        range_header = (self.headers.get("Range") or "").strip()
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            first, second = match.groups()
            if first:
                start = int(first)
                end = int(second) if second else size - 1
            elif second:
                start = max(0, size - int(second))
                end = size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        remaining = length
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

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
