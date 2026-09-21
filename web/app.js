"use strict";

const form = document.querySelector("#route-form");
const message = document.querySelector("#message");
const result = document.querySelector("#result");
const mapInput = form.elements.map_id;
const speedInput = form.elements.speed;
const checkpointTimeInput = form.elements.checkpoint_time;
const eventDataPromises = new Map();
const imageSizePromises = new Map();
let map;
let renderedLayers = [];
let activeMapId;
let selectionVersion = 0;

function clearMap() {
  for (const layer of renderedLayers) {
    layer.remove();
  }
  renderedLayers = [];
}

function updateRangeFromPointer(input, event) {
  const bounds = input.getBoundingClientRect();
  const minimum = Number(input.min);
  const maximum = Number(input.max);
  const step = Number(input.step);
  const thumbRadius = bounds.height / 2;
  const position = Math.min(
    1,
    Math.max(
      0,
      (event.clientX - bounds.left - thumbRadius)
        / (bounds.width - 2 * thumbRadius),
    ),
  );
  const value = minimum + position * (maximum - minimum);
  input.value = String(Math.round(value / step) * step);
  input.dispatchEvent(new Event("input"));
}

function configureRangeInput(input, accessibleValue) {
  let activePointer;
  const updateAccessibleValue = () => {
    input.setAttribute("aria-valuetext", accessibleValue(input.value));
  };
  input.addEventListener("input", updateAccessibleValue);
  input.addEventListener("change", requestRouteUpdate);
  input.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    activePointer = event.pointerId;
    input.setPointerCapture(event.pointerId);
    input.focus();
    updateRangeFromPointer(input, event);
  });
  input.addEventListener("pointermove", (event) => {
    if (event.pointerId === activePointer) {
      updateRangeFromPointer(input, event);
    }
  });
  input.addEventListener("pointerup", (event) => {
    if (event.pointerId !== activePointer) {
      return;
    }
    updateRangeFromPointer(input, event);
    activePointer = undefined;
    input.releasePointerCapture(event.pointerId);
    requestRouteUpdate();
  });
  input.addEventListener("pointercancel", () => {
    activePointer = undefined;
  });
  updateAccessibleValue();
}

function projectLocation(location, geometry, imageSize) {
  const radians = Math.PI / 180;
  const latitude = Number(location.latitude);
  const longitude = Number(location.longitude);
  const centreLatitude = Number(geometry.centre_latitude);
  const centreLongitude = Number(geometry.centre_longitude);
  const mercatorNorthing = (value) => 6378137 * Math.log(
    Math.tan(Math.PI / 4 + value * radians / 2),
  );
  const scaleCorrection = Math.cos(centreLatitude * radians);
  const metresPerPixel = Number(geometry.scale) * 0.0254 / Number(geometry.dpi);
  const east = 6378137 * (longitude - centreLongitude) * radians
    * scaleCorrection / metresPerPixel;
  const north = (mercatorNorthing(latitude) - mercatorNorthing(centreLatitude))
    * scaleCorrection / metresPerPixel;
  const rotation = Number(geometry.rotation);
  const x = imageSize.width / 2
    + Math.cos(rotation) * east
    + Math.sin(rotation) * north;
  const y = imageSize.height / 2
    + Math.sin(rotation) * east
    - Math.cos(rotation) * north;
  return [imageSize.height - y, x];
}

function loadImageSize(source) {
  if (imageSizePromises.has(source)) {
    return imageSizePromises.get(source);
  }
  const promise = new Promise((resolve, reject) => {
    const image = new Image();
    image.addEventListener("load", () => resolve({
      width: image.naturalWidth,
      height: image.naturalHeight,
    }));
    image.addEventListener("error", () => reject(
      new Error("The original OOM map could not be loaded."),
    ));
    image.src = source;
  });
  imageSizePromises.set(source, promise);
  return promise;
}

async function loadEventData(mapId) {
  if (!eventDataPromises.has(mapId)) {
    eventDataPromises.set(mapId, fetch(`./data/${mapId}/routes.json`).then(
      (response) => {
        if (!response.ok) {
          throw new Error(`No route data was found for event ${mapId}.`);
        }
        return response.json();
      },
    ));
  }
  try {
    return await eventDataPromises.get(mapId);
  } catch (error) {
    eventDataPromises.delete(mapId);
    throw error;
  }
}

function fillMapWithImage(imageSize) {
  const viewport = map.getSize();
  const zoom = Math.min(
    Math.log2(viewport.x / imageSize.width),
    Math.log2(viewport.y / imageSize.height),
  );
  map.setMinZoom(zoom);
  map.setView(
    [imageSize.height / 2, imageSize.width / 2],
    zoom,
    {animate: false},
  );
}

function renderRoute(data, routeData, mapId, imageSize) {
  const mapGeometry = data.map_geometry;
  const route = routeData.visits.map((number) => {
    const [latitude, longitude, points] = data.locations[number];
    return {latitude, longitude, number, points};
  });
  document.querySelector("#points").textContent = routeData.value;
  document.querySelector("#route-distance").textContent = (
    routeData.distance / 1000
  ).toFixed(2);
  const imageSource = `./data/${mapId}/map.webp`;
  const imageBounds = [[0, 0], [imageSize.height, imageSize.width]];

  if (!map || activeMapId !== mapId) {
    if (map) {
      map.remove();
    }
    renderedLayers = [];
    map = L.map("map", {
      crs: L.CRS.Simple,
      minZoom: -2,
      zoomSnap: 0,
      zoomControl: false,
    });
    L.control.zoom({position: "bottomright"}).addTo(map);
    L.imageOverlay(imageSource, imageBounds).addTo(map);
    map.setMaxBounds(L.latLngBounds(imageBounds).pad(0.25));
    map.on("resize", () => fillMapWithImage(imageSize));
    activeMapId = mapId;
  } else {
    clearMap();
  }

  const routeCoordinates = routeData.path.map(
    ([longitude, latitude]) => projectLocation(
      {latitude, longitude},
      mapGeometry,
      imageSize,
    ),
  );
  const routeCasing = L.polyline(routeCoordinates, {
    color: "white",
    opacity: 0.9,
    weight: 9,
  }).addTo(map);
  const routeLayer = L.polyline(routeCoordinates, {
    color: "#ff4500",
    opacity: 0.95,
    weight: 5,
  }).addTo(map);
  renderedLayers.push(routeCasing, routeLayer);

  route.slice(0, -1).forEach((location, index) => {
    const start = index === 0;
    const description = start
      ? "Start and finish"
      : `Visit ${index}, control ${location.number}, ${location.points} points`;
    const icon = L.divIcon({
      className: `control-label${start ? " start" : ""}`,
      html: start
        ? '<svg aria-hidden="true"><use href="#start-finish-symbol"></use></svg>'
        : String(index),
    });
    const marker = L.marker(projectLocation(location, mapGeometry, imageSize), {
      alt: description,
      icon,
      keyboard: true,
      title: description,
    }).addTo(map);
    const popup = document.createElement("div");
    popup.textContent = description;
    marker.bindPopup(popup);
    renderedLayers.push(marker);
  });

  result.hidden = false;
  map.invalidateSize();
  fillMapWithImage(imageSize);
}

function requestRouteUpdate() {
  selectionVersion += 1;
  updateRoute(selectionVersion);
}

async function updateRoute(version) {
  const mapId = mapInput.value;
  const speed = Number(speedInput.value);
  const checkpointTime = Number(checkpointTimeInput.value);

  if (![...mapInput.options].some(({value}) => value === mapId)) {
    message.textContent = "Select a valid map.";
    message.hidden = false;
    result.hidden = true;
    return;
  }

  if (!Number.isInteger(speed) || speed < 5 || speed > 15) {
    message.textContent = "Speed must be an integer from 5 to 15 km/h.";
    message.hidden = false;
    result.hidden = true;
    return;
  }

  if (![0, 5, 10, 15, 20].includes(checkpointTime)) {
    message.textContent = "Checkpoint time must be from 0 to 20 seconds in steps of 5.";
    message.hidden = false;
    result.hidden = true;
    return;
  }

  message.hidden = true;
  try {
    const data = await loadEventData(mapId);
    const routeData = data.routes[String(checkpointTime)]?.[String(speed)];
    if (!routeData) {
      throw new Error(
        `No route was found for ${speed} km/h and ${checkpointTime}-second clues.`,
      );
    }
    const imageSize = await loadImageSize(`./data/${mapId}/map.webp`);
    if (version !== selectionVersion) {
      return;
    }
    renderRoute(data, routeData, mapId, imageSize);
    window.history.replaceState(
      null,
      "",
      `?map_id=${encodeURIComponent(mapId)}&speed=${speed}`
        + `&checkpoint_time=${checkpointTime}`,
    );
  } catch (error) {
    if (version !== selectionVersion) {
      return;
    }
    result.hidden = true;
    message.textContent = error instanceof Error
      ? error.message
      : "The route could not be loaded.";
    message.hidden = false;
  }
}

form.addEventListener("submit", (event) => event.preventDefault());
mapInput.addEventListener("change", requestRouteUpdate);

const query = new URLSearchParams(window.location.search);
if (query.has("map_id")) {
  mapInput.value = query.get("map_id");
}
if (query.has("speed")) {
  speedInput.value = query.get("speed");
} else if (query.has("distance")) {
  speedInput.value = query.get("distance");
}
if (query.has("checkpoint_time")) {
  checkpointTimeInput.value = query.get("checkpoint_time");
}
configureRangeInput(
  speedInput,
  (value) => `${value} kilometres per hour`,
);
configureRangeInput(
  checkpointTimeInput,
  (value) => `${value} seconds per checkpoint`,
);
requestRouteUpdate();
