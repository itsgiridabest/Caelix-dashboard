"use strict";

const REFRESH_MS = 20000;
const LOCATION_KEY = "caelix.location";

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

const $ = (id) => document.getElementById(id);

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

function compass(degrees) {
  if (!Number.isFinite(degrees)) return "";
  const points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  return points[Math.round(degrees / 22.5) % 16];
}

function setBar(element, percent) {
  const value = Math.min(100, Math.max(0, Number(percent) || 0));
  element.style.width = `${value}%`;
  element.dataset.level = value >= 90 ? "high" : value >= 70 ? "warn" : "ok";
}

function formatPercent(value) {
  return Number.isFinite(Number(value)) ? Math.round(Number(value)).toString() : "—";
}

function setUpdated(text) {
  $("updated").textContent = text;
}

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

/* Status ------------------------------------------------------------- */

function renderStatus(data) {
  $("hostname").textContent = data.hostname || "—";

  const cpu = data.cpu || {};
  $("cpu-value").textContent = formatPercent(cpu.percent);
  const load = (data.load || []).map((value) => Number(value).toFixed(2)).join(" ");
  $("cpu-sub").textContent = `${cpu.cores || "—"} cores${load ? ` · load ${load}` : ""}`;
  setBar($("cpu-bar"), cpu.percent);

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

  const hermes = data.hermes || {};
  const pill = $("hermes-pill");
  pill.textContent = hermes.running ? "running" : "not running";
  pill.className = `pill ${hermes.running ? "pill--ok" : "pill--bad"}`;
  $("hermes-detail").textContent = hermes.detail || "—";
}

async function loadStatus() {
  try {
    const data = await fetchJson("/api/status");
    renderStatus(data);
    setUpdated(`updated ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    setUpdated(`offline — ${error.message}`);
  }
}

/* Weather ------------------------------------------------------------ */

function readLocation() {
  try {
    const raw = localStorage.getItem(LOCATION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (typeof parsed.lat === "number" && typeof parsed.lon === "number") return parsed;
  } catch {
    /* fall through */
  }
  return null;
}

function saveLocation(location) {
  localStorage.setItem(LOCATION_KEY, JSON.stringify(location));
}

function clearLocation() {
  localStorage.removeItem(LOCATION_KEY);
}

function setHint(text) {
  $("weather-hint").textContent = text;
}

function renderWeather(weather) {
  const [description, icon] = WEATHER_CODES[weather.code] || ["—", "🌡️"];
  const night = !weather.is_day;
  const temperatureUnit = weather.temperature_unit || "°C";

  $("weather-temp").textContent = fmtNumber(weather.temperature);
  $("weather-unit").textContent = temperatureUnit;
  $("weather-desc").textContent = description;
  $("weather-icon").textContent = night ? (icon === "☀️" ? "🌙" : icon) : icon;
  $("weather-feels").textContent =
    weather.apparent === null || weather.apparent === undefined
      ? "—"
      : `${fmtNumber(weather.apparent)}${temperatureUnit}`;
  $("weather-humidity").textContent =
    weather.humidity === null || weather.humidity === undefined
      ? "—"
      : `${Math.round(weather.humidity)}%`;
  $("weather-wind").textContent =
    weather.wind_speed === null || weather.wind_speed === undefined
      ? "—"
      : `${Math.round(weather.wind_speed)} ${weather.wind_unit || "km/h"} ${compass(weather.wind_direction)}`.trim();
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

/* Boot --------------------------------------------------------------- */

$("weather-form").addEventListener("submit", handleLocationSubmit);
$("weather-change").addEventListener("click", handleLocationChange);

loadStatus();
loadWeather();

setInterval(() => {
  loadStatus();
  loadWeather();
}, REFRESH_MS);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    loadStatus();
    loadWeather();
  }
});
