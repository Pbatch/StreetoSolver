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


def main(map_id: str, output_directory: Path | None) -> None:
    """Build static route data for every supported distance."""
    output_directory = output_directory or Path("web/data")
    map_directory = output_directory / map_id
    matrix_path = Path(f"data/oom_{map_id}_points_distance_matrix.json")
    points_path = Path(f"data/oom_{map_id}_points.json")
    map_path = Path(f"data/oom_{map_id}_map.jpg")

    try:
        load_dotenv()
        api_key = os.environ.get("OPENROUTESERVICE_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTESERVICE_API_KEY is not set")

        points_data = json.loads(points_path.read_text(encoding="utf-8"))
        map_geometry = points_data["map_geometry"]
        distances, locations = load_problem(matrix_path)
        start_index = next(
            index
            for index, location in enumerate(locations)
            if location["number"] == "start_finish"
        )
        map_directory.mkdir(parents=True, exist_ok=True)

        for distance_km in range(5, 16):
            output_path = map_directory / f"{distance_km}.json"
            if output_path.exists():
                cached_text = output_path.read_text(encoding="utf-8")
                cached_result = json.loads(cached_text)
                cached_result["map_geometry"] = map_geometry
                compact_result = (
                    json.dumps(
                        cached_result,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                if cached_text != compact_result:
                    output_path.write_text(
                        compact_result,
                        encoding="utf-8",
                    )
                    logger.info("Compacted cached route at {}", output_path)
                else:
                    logger.info("Using cached route at {}", output_path)
                continue

            route, route_distance, route_value = solve_route(
                distances,
                [int(location["points"]) for location in locations],
                start_index,
                distance_km * 1000,
            )
            solution: dict[str, Any] = {
                "start_node": "start_finish",
                "maximum_path_length": distance_km * 1000,
                "distance": route_distance,
                "value": route_value,
                "route": [locations[node] for node in route],
            }
            output_path.write_text(
                json.dumps(
                    {
                        "solution": solution,
                        "route_geojson": fetch_route(solution["route"], api_key),
                        "map_geometry": map_geometry,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            logger.info(
                "Saved {} km route worth {} points to {}",
                distance_km,
                route_value,
                output_path,
            )

        shutil.copy2(map_path, map_directory / "map.jpg")
        manifest_path = output_directory / "maps.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {"maps": []}
        )
        manifest["maps"] = [
            event for event in manifest["maps"] if event["id"] != map_id
        ]
        manifest["maps"].append(
            {
                "id": map_id,
                "title": points_data["title"],
                "distances": list(range(5, 16)),
            }
        )
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info("Static site data is ready in {}", output_directory)
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
