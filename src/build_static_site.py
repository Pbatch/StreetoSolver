"""Precompute every route used by the static Street-O web app."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv
from loguru import logger

from src.render_solution import fetch_route
from src.solve_streeto import load_problem, solve_route


def write_compact_json(output_path: Path, data: dict[str, Any]) -> None:
    """Write minified JSON with a trailing newline."""
    output_path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def compact_route(
    solution: dict[str, Any], route_geojson: dict[str, Any]
) -> dict[str, Any]:
    """Keep only the fields required by the static route viewer."""
    coordinates = route_geojson["features"][0]["geometry"]["coordinates"]
    if not isinstance(coordinates, list):
        raise ValueError("route geometry must contain a coordinate list")
    return {
        "distance": int(solution["distance"]),
        "value": int(solution["value"]),
        "visits": [str(location["number"]) for location in solution["route"]],
        "path": coordinates,
    }


def migrate_legacy_routes(map_directory: Path, output_path: Path) -> bool:
    """Combine existing per-distance artifacts without recalculating routes."""
    legacy_paths = sorted(
        (path for path in map_directory.glob("*.json") if path.stem.isdigit()),
        key=lambda path: int(path.stem),
    )
    if not legacy_paths:
        return False

    output: dict[str, Any] = {"map_geometry": None, "locations": {}, "routes": {}}
    for legacy_path in legacy_paths:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        output["map_geometry"] = legacy["map_geometry"]
        solution = legacy["solution"]
        for location in solution["route"]:
            output["locations"][str(location["number"])] = [
                float(location["latitude"]),
                float(location["longitude"]),
                int(location["points"]),
            ]
        output["routes"][legacy_path.stem] = compact_route(
            solution, legacy["route_geojson"]
        )

    write_compact_json(output_path, output)
    for legacy_path in legacy_paths:
        legacy_path.unlink()
    logger.info("Combined {} routes into {}", len(legacy_paths), output_path)
    return True


def main(map_id: str, output_directory: Path | None) -> None:
    """Build one static route-data file for every supported distance."""
    output_directory = output_directory or Path("web/data")
    map_directory = output_directory / map_id
    output_path = map_directory / "routes.json"
    matrix_path = Path(f"data/oom_{map_id}_points_distance_matrix.json")
    points_path = Path(f"data/oom_{map_id}_points.json")
    map_path = Path(f"data/oom_{map_id}_map.jpg")

    try:
        map_directory.mkdir(parents=True, exist_ok=True)
        migrate_legacy_routes(map_directory, output_path)

        output: dict[str, Any] | None = (
            json.loads(output_path.read_text(encoding="utf-8"))
            if output_path.exists()
            else None
        )
        if output is not None and all(
            str(distance_km) in output["routes"] for distance_km in range(5, 16)
        ):
            logger.info("Using cached routes at {}", output_path)
            return

        load_dotenv()
        api_key = os.environ.get("OPENROUTESERVICE_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTESERVICE_API_KEY is not set")

        points_data = json.loads(points_path.read_text(encoding="utf-8"))
        distances, locations = load_problem(matrix_path)
        start_index = next(
            index
            for index, location in enumerate(locations)
            if location["number"] == "start_finish"
        )
        if output is None:
            output = {
                "map_geometry": points_data["map_geometry"],
                "locations": {
                    str(location["number"]): [
                        float(location["latitude"]),
                        float(location["longitude"]),
                        int(location["points"]),
                    ]
                    for location in locations
                },
                "routes": {},
            }

        for distance_km in range(5, 16):
            if str(distance_km) in output["routes"]:
                logger.info("Using cached {} km route", distance_km)
                continue
            route, route_distance, route_value = solve_route(
                distances,
                [int(location["points"]) for location in locations],
                start_index,
                distance_km * 1000,
            )
            solution = {
                "distance": route_distance,
                "value": route_value,
                "route": [locations[node] for node in route],
            }
            output["routes"][str(distance_km)] = compact_route(
                solution, fetch_route(solution["route"], api_key)
            )
            write_compact_json(output_path, output)
            logger.info(
                "Saved {} km route worth {} points to {}",
                distance_km,
                route_value,
                output_path,
            )

        shutil.copy2(map_path, map_directory / "map.jpg")
        logger.info("Static site data is ready in {}", map_directory)
    except (
        HTTPError,
        URLError,
        KeyError,
        StopIteration,
        TypeError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logger.error("{}", error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Precompute the data files used by the static web app."
    )
    parser.add_argument(
        "--map-id",
        default="6a9939a6dd86d",
        help="OOM v4 map ID",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        help="static data destination (default: web/data)",
    )
    args = parser.parse_args()
    main(args.map_id, args.output_directory)
