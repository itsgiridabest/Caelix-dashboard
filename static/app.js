"use strict";

const STORAGE_SETTINGS = "caelix.settings.v1";
const STORAGE_LOCATION = "caelix.location";
const REFRESH_OPTIONS = [10, 20, 30, 60];
const THEMES = ["dark", "light", "cream", "ink"];
const USAGE_INTERVAL = 60000;

const WIDGETS = [
  { id: "weather", name: "Weather" },
  { id: "cpu", name: "CPU" },
  { id: "memory", name: "Memory" },
  { id: "disk", name: "Disk" },
  { id: "network", name: "Network" },
  { id: "temperature", name: "Temperature" },
  { id: "hermes", name: "Hermes gateway" },
  { id: "usage", name: "Hermes usage" },
  { id: "system", name: "System" },
];

const DEFAULT_SETTINGS = {
  theme: "dark",
  units: "c",
  interval: 20,
  widgets: WIDGETS.map(({ id }) => ({ id, visible: true })),
};

const WEATHER_CODES = {
  0: ["Clear sky", "☀️"],
  1: ["Mainly clear", "🌤️"],
  2: ["Partly cloudy", "⛅"],
  3: ["Overcast", "☁️"],
  45: ["Fog", "🌫️"],
  48: ["Depositing rime fog", "🌫️"],
  51: ["Light drizzle", "🌦️"],
  53: ["Drizzle", "🌦️"],
  55: ["Dense drizzle", "🌧️"],
  56: ["Freezing drizzle", "🌧️"],
  57: ["Dense freezing drizzle", "🌧️"],
  61: ["Light rain", "🌦️"],
  63: ["Rain", "🌧️"],
  65: ["Heavy rain", "🌧️"],
  66: ["Freezing rain", "🌧️"],
  67: ["Heavy freezing rain", "🌧️"],
  71: ["Light snow", "🌨️"],
  73: ["Snow", "🌨️"],
  75: ["Heavy snow", "❄️"],
  77: ["Snow grains", "🌨️"],
  80: ["Light showers", "🌦️"],
  81: ["Showers", "🌧️"],
  82: ["Violent showers", "⛈️"],
  85: ["Snow showers", "🌨️"],
  86: ["Heavy snow showers", "❄️"],
  95: ["Thunderstorm", "⛈️"],
  96: ["Thunderstorm with hail", "⛈️"],
  99: ["Thunderstorm with heavy hail", "⛈️"],
};

const state = {
  settings: null,
  timer: null,
  lastStatus: null,
  lastWeather: null,
};

const $ = (id) => document.getElementById(id);

/* Formatting --------------------------------------------------------- */

function fmtBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const digits = index === 0 || value >= 100 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[index]}`;
}

function fmtRate(bytesPerSecond) {
  return `${fmtBytes(bytesPerSecond)}/s`;
}

function fmtUptime(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function fmtNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Math.round(Number(value)).toString();
}

function formatPercent(value) {
  return Number.isFinite(Number(value)) ? Math.round(Number(value)).toString() : "—";
}

function compass(degrees) {
  if (!Number.isFinite(degrees)) return "";
  const points = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
  ];
  return points[Math.round(degrees / 22.5) % 16];
}

function setBar(element, percent) {
  const value = Math.min(100, Math.max(0, Number(percent) || 0));
  element.style.width = `${value}%`;
  element.dataset.level = value >= 90 ? "high" : value >= 70 ? "warn" : "ok";
}

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim() || "#35d0ba";
}

function tempUnitLabel() {
  return state.settings.units === "f" ? "°F" : "°C";
}

function windUnitLabel() {
  return state.settings.units === "f" ? "mph" : "km/h";
}

function convertTemp(celsius) {
  if (celsius === null || celsius === undefined) return null;
  const value = Number(celsius);
  return state.settings.units === "f" ? (value * 9) / 5 + 32 : value;
}

function convertWind(kmh) {
  if (kmh === null || kmh === undefined) return null;
  const value = Number(kmh);
  return state.settings.units === "f" ? value * 0.621371 : value;
}

/* Charts -------------------------------------------------------------- */

function drawSparkline(canvas, series) {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return;

  const dpr = window.devicePixelRatio || 1;
  const width = Math.round(rect.width);
  const height = Math.round(rect.height);
  if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
  }

  const context = canvas.getContext("2d");
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);

  const count = Math.max(0, ...series.map((line) => line.values.length));
  if (count < 2) return;

  const max = Math.max(1, ...series.flatMap((line) => line.values)) * 1.15;
  const step = width / (count - 1);

  for (const line of series) {
    if (line.values.length < 2) continue;
    const offset = count - line.values.length;

    context.beginPath();
    line.values.forEach((value, index) => {
      const x = (offset + index) * step;
      const y = height - 1 - (Math.min(Math.max(value, 0), max) / max) * (height - 2);
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.strokeStyle = line.color;
    context.lineWidth = 1.6;
    context.lineJoin = "round";
    context.stroke();

    context.lineTo(width, height);
    context.lineTo(offset * step, height);
    context.closePath();
    context.globalAlpha = 0.14;
    context.fillStyle = line.color;
    context.fill();
    context.globalAlpha = 1;
  }
}

/* Settings ------------------------------------------------------------ */

function loadSettings() {
  const settings = structuredClone(DEFAULT_SETTINGS);
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_SETTINGS) || "null");
  } catch {
    stored = null;
  }
  if (!stored || typeof stored !== "object") return settings;

  if (THEMES.includes(stored.theme)) settings.theme = stored.theme;
  if (stored.units === "c" || stored.units === "f") settings.units = stored.units;
  if (REFRESH_OPTIONS.includes(Number(stored.interval))) {
    settings.interval = Number(stored.interval);
  }
  if (Array.isArray(stored.widgets)) {
    const known = new Set(WIDGETS.map((widget) => widget.id));
    const seen = new Set();
    const widgets = [];
    for (const entry of stored.widgets) {
      const id = entry && entry.id;
      if (!known.has(id) || seen.has(id)) continue;
      seen.add(id);
      widgets.push({ id, visible: entry.visible !== false });
    }
    for (const widget of WIDGETS) {
      if (!seen.has(widget.id)) widgets.push({ id: widget.id, visible: true });
    }
    settings.widgets = widgets;
  }
  return settings;
}

function saveSettings() {
  localStorage.setItem(STORAGE_SETTINGS, JSON.stringify(state.settings));
}

function applyTheme() {
  document.body.dataset.theme = state.settings.theme;
}

function applyWidgets() {
  const grid = $("grid");
  for (const widget of state.settings.widgets) {
    const card = document.querySelector(`[data-widget="${widget.id}"]`);
    if (!card) continue;
    card.hidden = !widget.visible;
    grid.appendChild(card);
  }
}

function scheduleRefresh() {
  if (state.timer) clearInterval(state.timer);
  const seconds = state.settings.interval;
  state.timer = setInterval(() => {
    loadStatus();
    loadWeather();
  }, seconds * 1000);
  $("refresh-note").textContent = `refreshes every ${seconds} s`;
}

function commitSettings() {
  saveSettings();
  applyTheme();
  applyWidgets();
  renderSettings();
  scheduleRefresh();
  if (state.lastStatus) renderStatus(state.lastStatus);
  if (state.lastWeather) renderWeather(state.lastWeather);
}

function stepButton(text, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "step-button";
  button.textContent = text;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", onClick);
  return button;
}

function moveWidget(index, delta) {
  const widgets = state.settings.widgets;
  const target = index + delta;
  if (target < 0 || target >= widgets.length) return;
  const [moved] = widgets.splice(index, 1);
  widgets.splice(target, 0, moved);
  commitSettings();
}

function renderSettings() {
  const list = $("widget-list");
  list.replaceChildren();

  state.settings.widgets.forEach((widget, index) => {
    const meta = WIDGETS.find((entry) => entry.id === widget.id);
    if (!meta) return;

    const row = document.createElement("li");
    row.className = "widget-row";

    const label = document.createElement("label");
    label.className = "widget-row__label";

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = widget.visible;
    checkbox.addEventListener("change", () => {
      widget.visible = checkbox.checked;
      commitSettings();
    });

    const name = document.createElement("span");
    name.textContent = meta.name;
    label.append(checkbox, name);

    const controls = document.createElement("div");
    controls.className = "widget-row__controls";

    const up = stepButton("↑", `Move ${meta.name} up`, () => moveWidget(index, -1));
    up.disabled = index === 0;
    const down = stepButton("↓", `Move ${meta.name} down`, () => moveWidget(index, 1));
    down.disabled = index === state.settings.widgets.length - 1;

    controls.append(up, down);
    row.append(label, controls);
    list.append(row);
  });
}

function openSettings() {
  $("setting-theme").value = state.settings.theme;
  $("setting-units").value = state.settings.units;
  $("setting-interval").value = String(state.settings.interval);
  renderSettings();
  $("settings").showModal();
}

/* Data ---------------------------------------------------------------- */

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function renderStatus(data) {
  state.lastStatus = data;
  $("hostname").textContent = data.hostname || "—";

  const cpu = data.cpu || {};
  $("cpu-value").textContent = formatPercent(cpu.percent);
  const load = (data.load || []).map((value) => Number(value).toFixed(2)).join(" ");
  $("cpu-sub").textContent = `${cpu.cores || "—"} cores${load ? ` · load ${load}` : ""}`;
  setBar($("cpu-bar"), cpu.percent);
  drawSparkline($("cpu-spark"), [{ values: cpu.history || [], color: cssVar("--accent") }]);

  const memory = data.memory || {};
  $("memory-value").textContent = formatPercent(memory.percent);
  $("memory-sub").textContent = `${fmtBytes(memory.used)} of ${fmtBytes(memory.total)}`;
  setBar($("memory-bar"), memory.percent);

  const disk = data.disk || {};
  $("disk-value").textContent = formatPercent(disk.percent);
  $("disk-sub").textContent = `${fmtBytes(disk.free)} free of ${fmtBytes(disk.total)}`;
  setBar($("disk-bar"), disk.percent);

  const network = data.network || {};
  $("net-down").textContent = fmtRate(network.rx_rate);
  $("net-up").textContent = fmtRate(network.tx_rate);
  $("net-down-total").textContent = `${fmtBytes(network.rx_total)} total`;
  $("net-up-total").textContent = `${fmtBytes(network.tx_total)} total`;
  $("uptime").textContent = `up ${fmtUptime(data.uptime_seconds)}`;
  drawSparkline($("net-spark"), [
    { values: network.rx_history || [], color: cssVar("--accent") },
    { values: network.tx_history || [], color: cssVar("--info") },
  ]);

  const temperature = data.temperature;
  if (temperature) {
    $("temp-value").textContent = fmtNumber(convertTemp(temperature.celsius));
    $("temp-unit").textContent = tempUnitLabel();
    $("temp-label").textContent = temperature.label || "sensor";
    setBar($("temp-bar"), temperature.celsius);
  } else {
    $("temp-value").textContent = "—";
    $("temp-unit").textContent = "";
    $("temp-label").textContent = "no sensor";
    setBar($("temp-bar"), 0);
  }

  const hermes = data.hermes || {};
  const pill = $("hermes-pill");
  pill.textContent = hermes.running ? "running" : "not running";
  pill.className = `pill ${hermes.running ? "pill--ok" : "pill--bad"}`;
  $("hermes-detail").textContent = hermes.detail || "—";

  const system = data.system || {};
  $("system-board").textContent = system.board || "—";
  $("system-os").textContent = system.os || "—";
  $("system-kernel").textContent = system.kernel || "—";
  $("system-machine").textContent = system.machine || "—";
}

async function loadStatus() {
  try {
    const data = await fetchJson("/api/status");
    renderStatus(data);
    $("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    $("updated").textContent = `offline — ${error.message}`;
  }
}

/* Codex usage --------------------------------------------------------- */

function fmtTokens(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "—";
  if (number >= 1e9) return `${(number / 1e9).toFixed(1).replace(/\.0$/, "")}B`;
  if (number >= 1e6) return `${(number / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
  if (number >= 1e3) return `${(number / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  return Math.round(number).toLocaleString();
}

function resetLabel(epochSeconds) {
  const reset = Number(epochSeconds);
  if (!Number.isFinite(reset)) return "";
  const minutes = Math.round((reset * 1000 - Date.now()) / 60000);
  if (minutes <= 0) return "reset time passed";
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return `in ${days}d ${hours}h`;
  if (hours > 0) return `in ${hours}h ${mins}m`;
  return `in ${mins}m`;
}

function limitTitle(minutes) {
  const value = Number(minutes);
  if (!Number.isFinite(value) || value <= 0) return "Usage limit";
  if (value === 300) return "5-hour limit";
  if (value === 1440) return "Daily limit";
  if (value === 10080) return "Weekly limit";
  if (value % 1440 === 0) return `${value / 1440}-day limit`;
  if (value >= 60) return `${Math.round(value / 60)}-hour limit`;
  return `${value}-minute limit`;
}

function usageLimitRow(limit) {
  const wrap = document.createElement("div");
  wrap.className = "usage-limit";

  const head = document.createElement("div");
  head.className = "usage-limit__head";
  const label = document.createElement("span");
  label.textContent = limitTitle(limit.window_minutes);
  const value = document.createElement("strong");
  value.textContent = `${Math.round(limit.used_percent)}% used`;
  head.append(label, value);

  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("div");
  fill.className = "bar__fill";
  setBar(fill, limit.used_percent);
  bar.append(fill);
  wrap.append(head, bar);

  if (limit.resets_at) {
    const reset = document.createElement("p");
    reset.className = "muted small";
    const when = new Date(limit.resets_at * 1000);
    const stamp = when.toLocaleString([], {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
    reset.textContent = `resets ${stamp} · ${resetLabel(limit.resets_at)}`;
    wrap.append(reset);
  }
  return wrap;
}

function renderUsage(data) {
  const body = $("usage-body");
  const empty = $("usage-empty");
  const limitsBox = $("usage-limits");
  limitsBox.replaceChildren();

  if (!data || !data.available) {
    body.hidden = true;
    empty.hidden = false;
    empty.textContent = (data && data.reason) || "No Codex CLI usage found yet.";
    $("usage-plan").textContent = "codex cli";
    return;
  }

  body.hidden = false;
  empty.hidden = true;

  const plan = data.limits && data.limits.plan;
  $("usage-plan").textContent = plan ? `codex cli · ${plan}` : "codex cli";
  $("usage-today").textContent = fmtTokens(data.today && data.today.total);
  $("usage-week").textContent = fmtTokens(data.week && data.week.total);
  $("usage-month").textContent = fmtTokens(data.month && data.month.total);

  const limits = [];
  if (data.limits) {
    if (data.limits.primary) limits.push(data.limits.primary);
    if (data.limits.secondary) limits.push(data.limits.secondary);
  }
  for (const limit of limits) limitsBox.append(usageLimitRow(limit));

  if (!limits.length) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent = "No rate-limit snapshot in recent sessions.";
    limitsBox.append(note);
  }
}

async function loadUsage() {
  try {
    renderUsage(await fetchJson("/api/usage"));
  } catch (error) {
    renderUsage({ available: false, reason: `Usage unavailable — ${error.message}` });
  }
}

/* Weather ------------------------------------------------------------- */

function readLocation() {
  try {
    const raw = localStorage.getItem(STORAGE_LOCATION);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed.lat === "number" && typeof parsed.lon === "number") return parsed;
  } catch {
    /* fall through */
  }
  return null;
}

function saveLocation(location) {
  localStorage.setItem(STORAGE_LOCATION, JSON.stringify(location));
}

function clearLocation() {
  localStorage.removeItem(STORAGE_LOCATION);
}

function setHint(text) {
  $("weather-hint").textContent = text;
}

function renderWeather(weather) {
  state.lastWeather = weather;
  const [description, icon] = WEATHER_CODES[weather.code] || ["—", "🌡️"];
  const night = !weather.is_day;
  const shownIcon = night && (icon === "☀️" || icon === "🌤️") ? "🌙" : icon;

  $("weather-temp").textContent = fmtNumber(convertTemp(weather.temperature));
  $("weather-unit").textContent = tempUnitLabel();
  $("weather-desc").textContent = description;
  $("weather-icon").textContent = shownIcon;
  $("weather-feels").textContent =
    weather.apparent === null || weather.apparent === undefined
      ? "—"
      : `${fmtNumber(convertTemp(weather.apparent))}${tempUnitLabel()}`;
  $("weather-humidity").textContent =
    weather.humidity === null || weather.humidity === undefined
      ? "—"
      : `${Math.round(weather.humidity)}%`;
  $("weather-wind").textContent =
    weather.wind_speed === null || weather.wind_speed === undefined
      ? "—"
      : `${Math.round(convertWind(weather.wind_speed))} ${windUnitLabel()} ${compass(
          weather.wind_direction
        )}`.trim();
}

async function loadWeather() {
  const location = readLocation();
  const empty = $("weather-empty");
  const box = $("weather-data");
  const change = $("weather-change");

  if (!location) {
    empty.hidden = false;
    box.hidden = true;
    change.hidden = true;
    return;
  }

  empty.hidden = true;
  box.hidden = false;
  change.hidden = false;
  $("weather-place").textContent = location.label || "Saved location";

  try {
    const weather = await fetchJson(
      `/api/weather?lat=${encodeURIComponent(location.lat)}&lon=${encodeURIComponent(location.lon)}`
    );
    renderWeather(weather);
  } catch (error) {
    $("weather-desc").textContent = `Weather unavailable (${error.message})`;
  }
}

async function handleLocationSubmit(event) {
  event.preventDefault();
  const query = $("weather-query").value.trim();
  if (!query) return;

  const submit = event.target.querySelector("button[type=submit]");
  submit.disabled = true;
  setHint("Looking up…");
  try {
    const data = await fetchJson(`/api/geocode?q=${encodeURIComponent(query)}`);
    const place = (data.results || [])[0];
    if (!place) {
      setHint("No match. Try a more specific name, e.g. “Berlin, Germany”.");
      return;
    }
    saveLocation({ label: place.label, lat: place.latitude, lon: place.longitude });
    setHint("");
    $("weather-query").value = "";
    await loadWeather();
  } catch (error) {
    setHint(`Lookup failed: ${error.message}`);
  } finally {
    submit.disabled = false;
  }
}

function handleLocationChange() {
  clearLocation();
  setHint("");
  $("weather-query").value = "";
  loadWeather();
  $("weather-query").focus();
}

/* Boot ---------------------------------------------------------------- */

state.settings = loadSettings();
applyTheme();
applyWidgets();
renderSettings();
scheduleRefresh();

$("settings-open").addEventListener("click", openSettings);

$("setting-theme").addEventListener("change", (event) => {
  state.settings.theme = event.target.value;
  commitSettings();
});

$("setting-units").addEventListener("change", (event) => {
  state.settings.units = event.target.value;
  commitSettings();
});

$("setting-interval").addEventListener("change", (event) => {
  state.settings.interval = Number(event.target.value);
  commitSettings();
});

$("settings-reset").addEventListener("click", () => {
  state.settings = structuredClone(DEFAULT_SETTINGS);
  $("setting-theme").value = state.settings.theme;
  $("setting-units").value = state.settings.units;
  $("setting-interval").value = String(state.settings.interval);
  commitSettings();
});

$("weather-form").addEventListener("submit", handleLocationSubmit);
$("weather-change").addEventListener("click", handleLocationChange);

let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.lastStatus) renderStatus(state.lastStatus);
  }, 150);
});

loadStatus();
loadWeather();
loadUsage();
setInterval(loadUsage, USAGE_INTERVAL);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    loadStatus();
    loadWeather();
    loadUsage();
  }
});
