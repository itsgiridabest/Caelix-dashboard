# Caelix Dashboard

A small, private status page for the Arduino UNO Q. It runs on the board's Debian
side and shows, in one screen:

- current weather for a place you choose
- CPU load, with a live two-minute sparkline
- memory use
- disk space
- network download/upload rates and history
- board/CPU temperature where the kernel exposes a sensor
- board, OS and kernel details
- whether the Hermes gateway is running
- token use and rate-limit windows from the gateway's Codex CLI sessions

A settings panel (gear icon) controls the theme (dark, light, cream or ink),
metric/imperial units, the refresh interval, and which widgets are shown and in
what order. It refreshes
every 20 seconds by default, works on phones and desktops, and makes **no AI or
model calls**. Everything is standard-library Python 3 — no packages to install.

## Requirements

- Arduino UNO Q with the preinstalled Debian 13 ("Trixie") arm64 environment
- Python 3.11 or newer (`python3 --version`) — only the standard library is used
- `systemd` for the optional autostart unit (already present on the board)
- Internet access if you want the weather card (Open-Meteo, no API key)

## Quick start

Copy the folder to the board and run it:

```bash
scp -r caelix-dashboard arduino@arduino.local:~/
ssh arduino@arduino.local
cd ~/caelix-dashboard
python3 server.py
```

Then open <http://127.0.0.1:8080> on the board, or use the SSH tunnel described
below to open it from your laptop or phone.

## Configuration

The server reads an optional `config.json` next to `server.py`, and environment
variables override it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `host` / `CAELIX_HOST` | `127.0.0.1` | Interface to bind. Keep loopback unless you know why. |
| `port` / `CAELIX_PORT` | `8080` | TCP port for the dashboard. |
| `hermes_unit` | `hermes` | systemd unit checked with `systemctl is-active`. |
| `hermes_process_match` | `hermes` | Fallback: match against process command lines. |
| `codex_home` / `CODEX_HOME` | `~/.codex` | Codex CLI home; its `sessions/` logs feed the usage card. |

```bash
cp config.example.json config.json   # optional, git-ignored
CAELIX_PORT=9000 python3 server.py   # one-off override
```

## Settings

Open the gear icon in the top bar:

- **Theme** — dark, light, cream (warm paper) or ink (black and cream).
- **Units** — metric (°C, km/h) or imperial (°F, mph), applied to the weather
  card and the temperature widget.
- **Refresh** — 10, 20, 30 or 60 seconds between polls.
- **Widgets** — untick a card to hide it, use the arrows to change its order on
  the grid.

Settings live in the browser's `localStorage` (key `caelix.settings.v1`), so they
are per device and are never written to the board. **Reset** restores the
defaults; your weather location is kept until you press **Change** in the
weather card.

## Weather and location privacy

The weather card starts empty and does nothing until you enter a city. The
typed name goes only to Open-Meteo's free geocoding API; once resolved, the
coordinates are kept in your browser's `localStorage` and sent to this server
only to fetch the forecast (cached for 10 minutes). Nothing about your location
is written to disk on the board, and no other service is contacted.

## Start on boot

The unit is a **user** service, so it needs no root:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/caelix-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now caelix-dashboard
sudo loginctl enable-linger arduino     # keep it running without a login session
```

Check it and follow the log:

```bash
systemctl --user status caelix-dashboard
journalctl --user -u caelix-dashboard -f
```

## Reaching it from another device

The dashboard binds to `127.0.0.1`, so it is not reachable from the network by
default. The safest way to view it from a laptop or phone on the same Wi-Fi is
an SSH tunnel — the same approach the Arduino security guide recommends:

```bash
ssh -L 8080:127.0.0.1:8080 arduino@arduino.local
# then open http://127.0.0.1:8080 in your local browser
```

If you later want the page available to other devices on a private network,
put it behind Tailscale instead of exposing the port publicly: the dashboard has
no authentication, so it must never be forwarded to the open internet.
`CAELIX_HOST=0.0.0.0` exists for that private-network case only.

## HTTP API

| Route | Purpose |
| --- | --- |
| `GET /api/status` | Hostname, uptime, load, CPU (with history), memory, disk, network rates and history, temperature, system details, Hermes state |
| `GET /api/usage` | Codex CLI token totals for today, 7 days and 30 days, plus the latest rate-limit snapshot (cached 60 s) |
| `GET /api/weather?lat=&lon=` | Current conditions from Open-Meteo (cached 10 min) |
| `GET /api/geocode?q=` | City lookup, up to 5 matches |

The page itself is served from `static/` (`/`, `/styles.css`, `/app.js`).

## Layout

```
server.py                       HTTP server, metrics sampler, weather proxy
static/index.html               dashboard markup
static/styles.css               themed responsive styles
static/app.js                   polling, rendering, charts, settings, location setup
config.example.json             optional local settings
systemd/caelix-dashboard.service  user unit for autostart
```

## Notes and limits

- Single-user, local tool: no accounts, no authentication, no database.
- Metrics are read from `/proc` and `statvfs`; CPU and network are sampled every
  2 seconds, the page polls every 20 seconds.
- Weather is the only outbound request, and only after a location is set.
- The usage card reads Codex CLI rollout files under `~/.codex/sessions` (or
  `CODEX_HOME`) locally and reports the last 30 days plus the latest rate-limit
  snapshot. If Hermes runs as another user, point `codex_home` at that user's
  `~/.codex`; nothing is sent off the board.
- Restarting the service clears the in-memory weather/geocode cache and the
  sparkline history (the last ~3 minutes of samples).
- Settings and the chosen weather location are stored in the browser, not on the
  board; clearing site data returns the dashboard to its defaults.
