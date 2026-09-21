"""Precompute every route used by the static Street-O web app."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv
from loguru import logger

from src.render_solution import fetch_route
from src.solve_streeto import (
    DEFAULT_SEARCH_TIME_LIMIT_SECONDS,
    RouteSolution,
    RouteSolver,
    load_problem,
)

SPEEDS_KPH = range(5, 16)
CHECKPOINT_TIMES_SECONDS = (0, 5, 10, 15, 20)
SOLVER_NAME = "pyvrp"
ROUTE_REQUEST_INTERVAL_SECONDS = 1.6


def solve_speed_series(
    distances: list[list[int]],
    values: list[int],
    start_index: int,
    checkpoint_time: int,
    cached_routes: dict[int, list[int]],
    search_time_limit_seconds: float,
) -> tuple[int, list[tuple[int, RouteSolution]]]:
    """Solve one clue-time series, warm-starting each speed in sequence."""
    solver = RouteSolver(distances, values, start_index)
    previous_route: Sequence[int] | None = None
    solved_routes = []
    for speed_kph in SPEEDS_KPH:
        if speed_kph in cached_routes:
            previous_route = cached_routes[speed_kph]
            continue
        solution = solver.solve(
            speed_kph,
            checkpoint_time,
            initial_route=previous_route,
            search_time_limit_seconds=search_time_limit_seconds,
        )
        previous_route = solution.visits
        solved_routes.append((speed_kph, solution))
    return checkpoint_time, solved_routes


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

    output: dict[str, Any] = {
        "map_geometry": None,
        "locations": {},
        "routes": {"0": {}},
    }
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
        output["routes"]["0"][legacy_path.stem] = compact_route(
            solution, legacy["route_geojson"]
        )

    write_compact_json(output_path, output)
    for legacy_path in legacy_paths:
        legacy_path.unlink()
    logger.info("Combined {} routes into {}", len(legacy_paths), output_path)
    return True


def main(
    map_id: str,
    output_directory: Path | None,
    search_time_limit_seconds: float = DEFAULT_SEARCH_TIME_LIMIT_SECONDS,
) -> None:
    """Build static route data for every supported speed and checkpoint time."""
    output_directory = output_directory or Path("web/data")
    map_directory = output_directory / map_id
    output_path = map_directory / "routes.json"
    matrix_path = Path(f"data/oom_{map_id}_points_distance_matrix.json")
    points_path = Path(f"data/oom_{map_id}_points.json")

    try:
        map_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            Path(f"data/oom_{map_id}_map.webp"),
            map_directory / "map.webp",
        )
        migrate_legacy_routes(map_directory, output_path)

        output: dict[str, Any] | None = (
            json.loads(output_path.read_text(encoding="utf-8"))
            if output_path.exists()
            else None
        )
        if (
            output is not None
            and output["routes"]
            and all(
                isinstance(route, dict) and "distance" in route
                for route in output["routes"].values()
            )
        ):
            output["routes"] = {"0": output["routes"]}
            write_compact_json(output_path, output)
            logger.info("Migrated cached routes to the speed/checkpoint-time format")

        if output is not None and output.get("solver") != SOLVER_NAME:
            output["routes"] = {}
            output["solver"] = SOLVER_NAME
            write_compact_json(output_path, output)
            logger.info("Discarded routes generated by the previous solver")

        if output is not None and all(
            str(speed_kph) in output["routes"].get(str(checkpoint_time), {})
            for checkpoint_time in CHECKPOINT_TIMES_SECONDS
            for speed_kph in SPEEDS_KPH
        ):
            logger.info("Using cached routes at {}", output_path)
            return

        load_dotenv()
        if search_time_limit_seconds <= 0:
            raise ValueError("search time limit must be positive")
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
                "solver": SOLVER_NAME,
            }

        number_to_index = {
            str(location["number"]): index for index, location in enumerate(locations)
        }
        values = [int(location["points"]) for location in locations]
        cached_series: dict[int, dict[int, list[int]]] = {}
        for checkpoint_time in CHECKPOINT_TIMES_SECONDS:
            routes_for_time = output["routes"].setdefault(str(checkpoint_time), {})
            cached_routes: dict[int, list[int]] = {}
            for speed_kph in SPEEDS_KPH:
                cached_route = routes_for_time.get(str(speed_kph))
                if cached_route is not None:
                    logger.info(
                        "Using cached {} km/h route with {} s checkpoints",
                        speed_kph,
                        checkpoint_time,
                    )
                    cached_routes[speed_kph] = [
                        number_to_index[number] for number in cached_route["visits"]
                    ]
            cached_series[checkpoint_time] = cached_routes

        with ProcessPoolExecutor(max_workers=len(CHECKPOINT_TIMES_SECONDS)) as executor:
            futures = [
                executor.submit(
                    solve_speed_series,
                    distances,
                    values,
                    start_index,
                    checkpoint_time,
                    cached_series[checkpoint_time],
                    search_time_limit_seconds,
                )
                for checkpoint_time in CHECKPOINT_TIMES_SECONDS
                if len(cached_series[checkpoint_time]) < len(SPEEDS_KPH)
            ]
            for future in as_completed(futures):
                checkpoint_time, solved_routes = future.result()
                routes_for_time = output["routes"][str(checkpoint_time)]
                for speed_kph, solved in solved_routes:
                    route_data = {
                        "distance": solved.distance,
                        "value": solved.value,
                        "route": [locations[node] for node in solved.visits],
                    }
                    route_geojson = fetch_route(route_data["route"], api_key)
                    time.sleep(ROUTE_REQUEST_INTERVAL_SECONDS)
                    routes_for_time[str(speed_kph)] = compact_route(
                        route_data, route_geojson
                    )
                    write_compact_json(output_path, output)
                    logger.info(
                        "Saved {} km/h route with {} s checkpoints worth "
                        "{} points to {}",
                        speed_kph,
                        checkpoint_time,
                        solved.value,
                        output_path,
                    )
                    logger.info(
                        "Solved in {:.2f} s after {} iterations",
                        solved.elapsed_seconds,
                        solved.iterations,
                    )

        logger.info("Static site data is ready in {}", map_directory)
    except (
        HTTPError,
        HTTPException,
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
    parser.add_argument(
        "--search-time-limit",
        default=DEFAULT_SEARCH_TIME_LIMIT_SECONDS,
        type=float,
        help="solver time per route in seconds (default: 0.5)",
    )
    args = parser.parse_args()
    main(
        args.map_id,
        args.output_directory,
        args.search_time_limit,
    )
