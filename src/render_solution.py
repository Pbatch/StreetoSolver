"""Render a solved Street-O route as an interactive HTML map."""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from loguru import logger


def load_solution(solution_path: Path) -> dict[str, Any]:
    """Load and validate a solution JSON file."""
    solution = json.loads(solution_path.read_text(encoding="utf-8"))
    route = solution.get("route")
    if not isinstance(route, list) or len(route) < 2:
        raise ValueError("solution must contain a route with at least two locations")
    for location in route:
        if not isinstance(location, dict):
            raise ValueError("every route location must be an object")
        float(location["latitude"])
        float(location["longitude"])
        int(location["points"])
        if "number" not in location:
            raise ValueError("every route location must have a number")
    return solution


def fetch_route(route: list[dict[str, Any]], api_key: str) -> dict[str, Any]:
    """Fetch pedestrian route geometry through the solution vertices."""
    request = Request(
        "https://api.heigit.org/openrouteservice/v2/directions/foot-walking/geojson",
        data=json.dumps(
            {
                "coordinates": [
                    [location["longitude"], location["latitude"]] for location in route
                ],
                "instructions": False,
            }
        ).encode(),
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "User-Agent": "StreetoSolver/0.1",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        geojson = json.load(response)
    if geojson.get("type") != "FeatureCollection" or not geojson.get("features"):
        raise RuntimeError("routing service returned invalid GeoJSON")
    return geojson


def render_html(solution: dict[str, Any], geojson: dict[str, Any]) -> str:
    """Create a self-contained map page backed by Leaflet tiles."""
    page_data = json.dumps(
        {"solution": solution, "geojson": geojson}, ensure_ascii=False
    ).replace("</", "<\\/")
    title = html.escape(f"Street-O solution: {solution.get('value', 0)} points")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{ height: 100%; margin: 0; }}
    .summary {{
      background: white;
      border-radius: 4px;
      box-shadow: 0 1px 5px #777;
      font: 14px/1.4 sans-serif;
      padding: 8px 12px;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const data = {page_data};
    const map = L.map("map");
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">' +
        'OpenStreetMap</a> contributors',
      maxZoom: 19,
    }}).addTo(map);

    const routeLayer = L.geoJSON(data.geojson, {{
      style: {{color: "#a626ff", opacity: 0.9, weight: 5}},
    }}).addTo(map);
    map.fitBounds(routeLayer.getBounds(), {{padding: [24, 24]}});

    data.solution.route.slice(0, -1).forEach((location, index) => {{
      const isStart = index === 0;
      const marker = L.circleMarker([location.latitude, location.longitude], {{
        color: isStart ? "#d7191c" : "#5b168d",
        fillColor: isStart ? "#ffeb3b" : "#a626ff",
        fillOpacity: 1,
        radius: isStart ? 9 : 7,
        weight: 3,
      }}).addTo(map);
      marker.bindTooltip(document.createTextNode(String(location.number)), {{
        permanent: true,
        direction: "top",
      }});
      const details = document.createElement("div");
      details.textContent = isStart
        ? "Start / finish"
        : `Visit ${{index}}: control ${{location.number}} ` +
          `(${{location.points}} points)`;
      marker.bindPopup(details);
    }});

    const summary = L.control({{position: "topleft"}});
    summary.onAdd = () => {{
      const element = L.DomUtil.create("div", "summary");
      element.textContent = `${{data.solution.value}} points · ` +
        `${{data.solution.distance}} m`;
      return element;
    }};
    summary.addTo(map);
  </script>
</body>
</html>
"""


def main(solution_path: Path, output_path: Path | None, api_key: str | None) -> None:
    output_path = output_path or solution_path.with_suffix(".html")
    try:
        if not api_key:
            raise ValueError("OPENROUTESERVICE_API_KEY is not set")
        solution = load_solution(solution_path)
        geojson = fetch_route(solution["route"], api_key)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            render_html(solution, geojson),
            encoding="utf-8",
        )
        logger.info("Saved rendered solution to {}", output_path)
    except (
        HTTPError,
        URLError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logger.error("{}", error)


if __name__ == "__main__":
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Render a Street-O solution as an interactive HTML map."
    )
    parser.add_argument("solution_path", type=Path, help="solution JSON file")
    parser.add_argument("-o", "--output-path", type=Path, help="HTML destination")
    args = parser.parse_args()
    main(
        args.solution_path, args.output_path, os.environ.get("OPENROUTESERVICE_API_KEY")
    )
