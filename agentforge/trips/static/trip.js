const trip = window.TRIP || {};
const apiBase = String(window.TRIP_API || "").replace(/\/$/, "");

const els = {
  title: document.getElementById("trip-title"),
  lede: document.getElementById("lede"),
  meta: document.getElementById("meta"),
  timeline: document.getElementById("timeline"),
  error: document.getElementById("form-error"),
  boardKicker: document.getElementById("board-kicker"),
  boardTime: document.getElementById("board-time"),
  boardSub: document.getElementById("board-sub"),
  statTime: document.getElementById("stat-time"),
  statDist: document.getElementById("stat-km"),
  statStops: document.getElementById("stat-stops"),
  splitHandle: document.getElementById("split-handle"),
};

const map = L.map("map", { zoomControl: true }).setView([50.11, 8.68], 6);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · directions &copy; <a href="https://openrouteservice.org/">openrouteservice</a> / HeiGIT',
}).addTo(map);

const markers = new Map();
const openDetails = new Set();
let routeLayer = null;
let routeTimer = 0;
let activeId = null;

function pinIcon(color, caption) {
  return L.divIcon({
    className: "pin",
    html: `<span class="pin-dot" style="background:${color}">${escapeHtml(caption)}</span>`,
    iconSize: [42, 42],
    iconAnchor: [21, 21],
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function places() {
  const rows = [];
  if (trip.origin) rows.push(trip.origin);
  for (const stop of trip.stops || []) rows.push(stop);
  if (trip.destination) rows.push(trip.destination);
  return rows;
}

function scheduleByPlace() {
  const mapAt = {};
  for (const event of trip.schedule || []) {
    if (!mapAt[event.place_id]) mapAt[event.place_id] = {};
    mapAt[event.place_id][event.event] = event.at;
  }
  return mapAt;
}

function clock(iso) {
  if (!iso) return "--:--";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "--:--";
  return dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function fmtKm(meters) {
  const km = Number(meters || 0) / 1000;
  return `${km.toFixed(km >= 10 ? 0 : 2)} km`;
}

function fmtDur(seconds) {
  const total = Math.round(Number(seconds || 0) / 60);
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  if (hours <= 0) return `${mins} min`;
  return `${hours} h ${mins} min`;
}

function colorFor(place) {
  if (place.kind === "origin") return "#3f6a4d";
  if (place.kind === "destination") return "#c9a227";
  if (place.kind === "restaurant") return "#c45c26";
  if (place.kind === "lodging") return "#4a5d8a";
  return "#5c584e";
}

function captionFor(place) {
  if (place.kind === "origin") return "From";
  if (place.kind === "destination") return "To";
  const word = String(place.label || "?").split(/[\s,]/)[0];
  return word.slice(0, 3);
}

function isOrigin(place) {
  return place.kind === "origin" || place.id === trip.origin?.id;
}

function isDestination(place) {
  return Boolean(trip.destination) && place.id === trip.destination.id;
}

function canToggle(place) {
  return !isOrigin(place) && !isDestination(place);
}

function markerLatLng(place, visible) {
  const same = visible.filter((row) => row.lat === place.lat && row.lon === place.lon);
  if (same.length < 2) return [place.lat, place.lon];
  const index = same.findIndex((row) => row.id === place.id);
  const angle = (2 * Math.PI * index) / same.length;
  const delta = 0.00014;
  return [place.lat + delta * Math.cos(angle), place.lon + delta * Math.sin(angle)];
}

function styleMarker(id) {
  for (const [pid, marker] of markers) {
    const el = marker.getElement();
    if (el) el.classList.toggle("is-selected", pid === id);
    marker.setZIndexOffset(pid === id ? 1000 : 0);
  }
}

function detailsFor(place) {
  const rows = [];
  if (place.address) rows.push(["Address", place.address]);
  if (place.opens || place.closes) {
    rows.push(["Hours", [place.opens, place.closes].filter(Boolean).join(" – ")]);
  }
  if (place.website) rows.push(["Website", place.website]);
  if (place.notes) rows.push(["Notes", place.notes]);
  return rows;
}

function paintMeta() {
  els.meta.replaceChildren();
  const bits = [trip.profile === "foot-walking" ? "Walking" : "Driving"];
  if (trip.departure) bits.push(clock(trip.departure));
  for (const bit of bits) {
    const span = document.createElement("span");
    span.className = "chip";
    span.textContent = bit;
    els.meta.appendChild(span);
  }
}

function paintTimeline() {
  const times = scheduleByPlace();
  els.timeline.replaceChildren();
  for (const place of places()) {
    const li = document.createElement("li");
    li.dataset.id = place.id;
    if (place.id === activeId) li.classList.add("is-active");
    if (place.enabled === false) li.classList.add("is-off");

    const when = document.createElement("span");
    when.className = "when";
    const iso = isOrigin(place)
      ? (times[place.id]?.depart || trip.departure)
      : times[place.id]?.arrive;
    when.textContent = iso ? clock(iso) : "--:--";

    const copy = document.createElement("div");
    copy.className = "copy";
    const title = document.createElement("strong");
    title.textContent = place.label;
    const sub = document.createElement("small");
    const dwell = place.dwell_min ? `${place.dwell_min} min stop` : place.kind;
    sub.textContent = dwell;
    copy.append(title, sub);

    const extra = detailsFor(place);
    if (extra.length) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "more";
      const expanded = openDetails.has(place.id);
      more.setAttribute("aria-expanded", String(expanded));
      more.textContent = expanded ? "Hide details" : "Details";
      more.addEventListener("click", (event) => {
        event.stopPropagation();
        if (openDetails.has(place.id)) openDetails.delete(place.id);
        else openDetails.add(place.id);
        paintTimeline();
      });
      copy.append(more);
      if (expanded) {
        const details = document.createElement("dl");
        details.className = "details";
        for (const [key, value] of extra) {
          const dt = document.createElement("dt");
          dt.textContent = key;
          const dd = document.createElement("dd");
          if (key === "Website") {
            const link = document.createElement("a");
            link.href = value;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = value.replace(/^https?:\/\//, "").replace(/\/$/, "");
            dd.append(link);
          } else {
            dd.textContent = value;
          }
          details.append(dt, dd);
        }
        copy.append(details);
      }
    }

    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "toggle";
    box.checked = place.enabled !== false;
    box.disabled = !canToggle(place);
    box.setAttribute("aria-label", `Include ${place.label}`);
    box.addEventListener("click", (event) => event.stopPropagation());
    box.addEventListener("change", () => {
      place.enabled = box.checked;
      queueReroute();
    });

    li.append(when, copy, box);
    li.addEventListener("click", () => focusPlace(place.id));
    els.timeline.appendChild(li);
  }
}

function paintBoard() {
  const route = trip.route || {};
  const enabledStops = (trip.stops || []).filter((s) => s.enabled !== false).length;
  els.boardKicker.textContent = trip.profile === "foot-walking" ? "Walking" : "Driving";
  els.boardTime.textContent = fmtDur(route.duration_s);
  const dest = trip.destination;
  const times = scheduleByPlace();
  if (dest && times[dest.id]?.arrive) {
    els.boardSub.textContent = `Arrive ${clock(times[dest.id].arrive)} · ${dest.label}`;
  } else {
    els.boardSub.textContent = trip.title || "";
  }
  els.statTime.textContent = fmtDur(route.duration_s);
  els.statDist.textContent = fmtKm(route.distance_m);
  els.statStops.textContent = String(enabledStops);
}

function paintMap() {
  for (const marker of markers.values()) {
    map.removeLayer(marker);
  }
  markers.clear();
  if (routeLayer) {
    map.removeLayer(routeLayer);
    routeLayer = null;
  }

  const visible = places().filter((p) => p.enabled !== false || isOrigin(p) || isDestination(p));
  for (const place of visible) {
    const marker = L.marker(markerLatLng(place, visible), {
      icon: pinIcon(colorFor(place), captionFor(place)),
      title: place.label,
    }).addTo(map);
    marker.bindPopup(place.label);
    marker.on("click", () => focusPlace(place.id));
    markers.set(place.id, marker);
  }

  const line = (trip.route && trip.route.coordinates) || [];
  if (line.length >= 2) {
    routeLayer = L.polyline(line, { color: "#3f6a4d", weight: 5, opacity: 0.9 }).addTo(map);
  }
  styleMarker(activeId);
  if (activeId && markers.has(activeId)) {
    return;
  }
  if (routeLayer) {
    map.fitBounds(routeLayer.getBounds(), { padding: [28, 28] });
  } else if (visible.length) {
    const bounds = L.latLngBounds(visible.map((p) => [p.lat, p.lon]));
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
  }
}

function focusPlace(id) {
  activeId = id;
  const marker = markers.get(id);
  if (!marker) {
    paintMap();
  }
  const target = markers.get(id);
  if (target) {
    styleMarker(id);
    const walking = trip.profile === "foot-walking";
    map.flyTo(target.getLatLng(), walking ? 17 : 15, { duration: 0.35 });
    target.openPopup();
  }
  paintTimeline();
}

function showError(message) {
  els.error.hidden = !message;
  els.error.textContent = message || "";
}

function queueReroute() {
  paintTimeline();
  clearTimeout(routeTimer);
  routeTimer = setTimeout(reroute, 280);
}

async function reroute() {
  if (!trip.id) return;
  const enabled = (trip.stops || []).filter((s) => s.enabled !== false).map((s) => s.id);
  showError("");
  try {
    const response = await fetch(`${apiBase}/api/trips/${trip.id}/route`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled_stop_ids: enabled }),
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.detail || body.error || "Could not recalculate the route.");
    }
    trip.route = body.route;
    trip.schedule = body.schedule || [];
    trip.stops = body.stops || trip.stops;
    paint();
  } catch (err) {
    showError(err.message);
    paintMap();
    paintBoard();
  }
}

function paint() {
  els.title.textContent = trip.title || "Trip";
  document.title = trip.title || "Trip";
  els.lede.textContent = trip.itinerary_md
    ? String(trip.itinerary_md).split("\n")[0].replace(/^#+\s*/, "")
    : "Timed itinerary. Toggle a stop to redraw the route.";
  paintMeta();
  paintTimeline();
  paintBoard();
  paintMap();
}

function bindSplitHandle() {
  let dragging = false;
  const onMove = (event) => {
    if (!dragging) return;
    const width = Math.max(280, Math.min(event.clientX, window.innerWidth * 0.62));
    document.documentElement.style.setProperty("--panel-width", `${Math.round(width)}px`);
    map.invalidateSize();
  };
  const onUp = () => {
    dragging = false;
    document.body.classList.remove("is-resizing");
    map.invalidateSize();
  };
  els.splitHandle.addEventListener("pointerdown", (event) => {
    if (window.innerWidth <= 800) return;
    event.preventDefault();
    dragging = true;
    document.body.classList.add("is-resizing");
    els.splitHandle.setPointerCapture(event.pointerId);
  });
  els.splitHandle.addEventListener("pointermove", onMove);
  els.splitHandle.addEventListener("pointerup", onUp);
  els.splitHandle.addEventListener("pointercancel", onUp);
}

bindSplitHandle();
paint();
