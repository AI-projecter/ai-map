const $ = (id) => document.getElementById(id);
let token = localStorage.getItem("wam_token");
let authMode = "login";
let map, streetLayer, satelliteLayer, marker, activePoint = null;
let currentNarration = "";

function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body) headers["Content-Type"] = "application/json";
  return fetch(path, { ...options, headers }).then(async response => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "Request failed");
    return body;
  });
}
function showAuth() { $("auth-screen").classList.remove("hidden"); $("app").classList.add("hidden"); }
function showApp(email) {
  $("auth-screen").classList.add("hidden"); $("app").classList.remove("hidden");
  $("user-email").textContent = email || "";
  initMap(); loadHistory();
  setTimeout(() => map && map.invalidateSize(), 100);
}
document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
  authMode = button.dataset.mode;
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b === button));
  $("auth-submit").textContent = authMode === "login" ? "Войти" : "Создать аккаунт";
  $("password").autocomplete = authMode === "login" ? "current-password" : "new-password";
  $("auth-error").textContent = "";
}));
$("auth-form").addEventListener("submit", async e => {
  e.preventDefault(); $("auth-error").textContent = ""; $("auth-submit").disabled = true;
  try {
    const data = await api(`/api/${authMode}`, { method: "POST", body: JSON.stringify({
      email: $("email").value.trim(), password: $("password").value
    })});
    token = data.token; localStorage.setItem("wam_token", token); showApp(data.email);
  } catch (error) { $("auth-error").textContent = error.message; }
  finally { $("auth-submit").disabled = false; }
});
$("logout").addEventListener("click", () => {
  token = null; localStorage.removeItem("wam_token");
  if (window.speechSynthesis) speechSynthesis.cancel();
  showAuth();
});

function initMap() {
  if (map) { map.invalidateSize(); return; }
  map = L.map("map", { zoomControl: true }).setView([20, 0], 2);
  streetLayer = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
  });
  satelliteLayer = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    maxZoom: 19, attribution: "Tiles &copy; Esri"
  });
  const savedLayer = localStorage.getItem("wam_map_layer") || "street";
  setLayer(savedLayer);
  map.on("click", e => explore(e.latlng.lat, e.latlng.lng));
}
function setLayer(name) {
  if (!map) return;
  map.removeLayer(streetLayer); map.removeLayer(satelliteLayer);
  const satellite = name === "satellite";
  (satellite ? satelliteLayer : streetLayer).addTo(map);
  $("street-layer").classList.toggle("selected", !satellite);
  $("sat-layer").classList.toggle("selected", satellite);
  localStorage.setItem("wam_map_layer", satellite ? "satellite" : "street");
}
$("street-layer").addEventListener("click", () => setLayer("street"));
$("sat-layer").addEventListener("click", () => setLayer("satellite"));

function setBusy(busy) {
  $("empty-state").classList.toggle("hidden", busy || !!activePoint);
  $("loading").classList.toggle("hidden", !busy);
  $("result").classList.toggle("hidden", busy || !activePoint);
}
function weatherMarkup(weather) {
  if (!weather || weather.unavailable) return `<div class="muted small-text">Погода недоступна. ${weather?.unavailable || ""}</div>`;
  const c = weather.current || {};
  const icon = c.icon ? `https://openweathermap.org/img/wn/${c.icon}@2x.png` : "";
  return `<div class="weather-main"><div><div class="temp">${c.temp == null ? "—" : Math.round(c.temp) + "°C"}</div><div class="weather-desc">${escapeHtml(c.description || "Нет данных")}</div></div>${icon ? `<img class="weather-icon" src="${icon}" alt="">` : ""}</div>
  <div class="weather-meta"><span>◉ Ощущается ${c.feels_like == null ? "—" : Math.round(c.feels_like) + "°"}C</span><span>💧 ${c.humidity ?? "—"}%</span><span>↗ ${c.wind ?? "—"} м/с</span></div>`;
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function renderPoint(data) {
  activePoint = data; const analysis = data.analysis || {};
  $("empty-state").classList.add("hidden"); $("loading").classList.add("hidden"); $("result").classList.remove("hidden");
  $("place-name").textContent = data.place?.name || analysis.title || "Выбранная местность";
  $("coordinates").textContent = `${Number(data.lat).toFixed(5)}, ${Number(data.lon).toFixed(5)}`;
  $("weather").innerHTML = weatherMarkup(data.weather);
  $("description").textContent = analysis.description || "Описание отсутствует.";
  currentNarration = analysis.narration || analysis.description || "";
  $("image-wrap").classList.toggle("hidden", !data.image);
  $("image-note").classList.toggle("hidden", !!data.image);
  if (data.image) $("location-image").src = data.image;
  const forecast = data.weather?.forecast || [];
  $("forecast").innerHTML = forecast.length ? forecast.map(item => {
    const date = new Date(item.time.replace(" ", "T") + "Z");
    const label = date.toLocaleString("ru-RU", { weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
    const icon = item.icon ? `https://openweathermap.org/img/wn/${item.icon}.png` : "";
    return `<div class="forecast-item"><span>${label}</span>${icon ? `<img src="${icon}" alt="">` : ""}<b>${item.temp == null ? "—" : Math.round(item.temp) + "°"}</b><span>${escapeHtml(item.description || "")}</span></div>`;
  }).join("") : `<span class="muted small-text">Прогноз недоступен</span>`;
  if (marker) marker.remove();
  marker = L.marker([data.lat, data.lon]).addTo(map).bindPopup(escapeHtml($("place-name").textContent)).openPopup();
  renderHistoryItem(data);
}
async function explore(lat, lon) {
  if (!token) return showAuth();
  if (marker) marker.remove();
  marker = L.marker([lat, lon]).addTo(map);
  activePoint = null; $("empty-state").classList.add("hidden"); $("result").classList.add("hidden"); $("loading").classList.remove("hidden");
  try {
    const data = await api("/api/points/analyze", { method: "POST", body: JSON.stringify({ lat, lon }) });
    renderPoint(data); await loadHistory();
  } catch (error) {
    $("loading").classList.add("hidden"); $("empty-state").classList.remove("hidden");
    $("empty-state").querySelector("h3").textContent = "Не удалось исследовать точку";
    $("empty-state").querySelector("p").textContent = error.message;
  }
}
function renderHistoryItem(data) {
  const el = document.createElement("button"); el.className = "history-item";
  const title = data.place?.name || data.analysis?.title || "Точка на карте";
  el.innerHTML = `${escapeHtml(title)}<small>${Number(data.lat).toFixed(3)}, ${Number(data.lon).toFixed(3)}</small>`;
  el.addEventListener("click", () => {
    map.setView([data.lat, data.lon], Math.max(map.getZoom(), 8));
    renderPoint(data);
  });
  const history = $("history");
  const key = `${Number(data.lat).toFixed(5)}:${Number(data.lon).toFixed(5)}`;
  const existing = [...history.querySelectorAll(".history-item")].find(node => node.dataset.key === key);
  el.dataset.key = key;
  if (existing) existing.replaceWith(el); else history.prepend(el);
}
async function loadHistory() {
  try {
    const items = await api("/api/history");
    $("history").innerHTML = "";
    if (!items.length) { $("history").innerHTML = '<p class="muted small-text">Сохранённых точек пока нет</p>'; return; }
    items.forEach(item => {
      const el = document.createElement("button"); el.className = "history-item";
      el.dataset.key = `${Number(item.lat).toFixed(5)}:${Number(item.lon).toFixed(5)}`;
      el.innerHTML = `${escapeHtml(item.place?.name || item.analysis?.title || "Точка")}<small>${Number(item.lat).toFixed(3)}, ${Number(item.lon).toFixed(3)}</small>`;
      el.addEventListener("click", () => { map.setView([item.lat, item.lon], Math.max(map.getZoom(), 8)); renderPoint(item); });
      $("history").appendChild(el);
    });
  } catch (error) { $("history").innerHTML = `<p class="muted small-text">${escapeHtml(error.message)}</p>`; }
}
$("refresh-history").addEventListener("click", loadHistory);
$("clear-history").addEventListener("click", async () => {
  if (!confirm("Удалить всю историю выбранных точек?")) return;
  try { await api("/api/history", { method: "DELETE" }); $("history").innerHTML = '<p class="muted small-text">История очищена</p>'; }
  catch (e) { alert(e.message); }
});
$("speak").addEventListener("click", () => {
  if (!currentNarration || !("speechSynthesis" in window)) { alert("Озвучка не поддерживается этим браузером."); return; }
  speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(currentNarration);
  utterance.lang = "ru-RU"; utterance.rate = 0.95;
  const voices = speechSynthesis.getVoices();
  const russian = voices.find(v => v.lang.toLowerCase().startsWith("ru"));
  if (russian) utterance.voice = russian;
  speechSynthesis.speak(utterance);
});
(async function boot() {
  if (!token) return showAuth();
  try { const user = await api("/api/me"); showApp(user.email); }
  catch { token = null; localStorage.removeItem("wam_token"); showAuth(); }
})();
  
