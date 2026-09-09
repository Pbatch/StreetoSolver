"""Construct a geographic distance matrix from an OOM points file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from loguru import logger


def walking_distance_matrix(
    locations: list[dict[str, Any]], api_key: str
) -> list[list[float]]:
    """Request a walking-distance matrix from OpenRouteService."""
    payload = {
        "locations": [
            [location["longitude"], location["latitude"]] for location in locations
        ],
        "metrics": ["distance"],
        "units": "m",
    }
    request = Request(
        "https://api.heigit.org/openrouteservice/v2/matrix/foot-walking",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
            "User-Agent": "StreetoSolver/0.1",
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:  # noqa: S310
        result = json.load(response)

    distances = result.get("distances")
    if not isinstance(distances, list) or len(distances) != len(locations):
        raise RuntimeError("routing service returned an invalid distance matrix")
    if any(
        not isinstance(row, list) or len(row) != len(locations) for row in distances
    ):
        raise RuntimeError("routing service returned an incomplete distance matrix")
    if any(distance is None for row in distances for distance in row):
        raise RuntimeError("at least one pair has no pedestrian route")
    return [[round(float(distance), 1) for distance in row] for row in distances]


def load_locations(points_path: Path) -> list[dict[str, Any]]:
    """Load the start/finish and scored controls from a points file."""
    data = json.loads(points_path.read_text(encoding="utf-8"))
    start_finish = data["start_finish"]
    points = data["points"]
    if not isinstance(start_finish, dict) or not isinstance(points, list):
        raise ValueError("invalid points file structure")

    locations = [{"number": "start_finish", **start_finish}, *points]
    for location in locations:
        if not isinstance(location, dict):
            raise ValueError("every location must be an object")
        float(location["latitude"])
        float(location["longitude"])
        int(location["points"])
    return locations


def main(points_path: Path, output_path: Path | None, api_key: str | None) -> None:
    output_path = output_path or points_path.with_name(
        f"{points_path.stem}_distance_matrix.json"
    )
    if output_path.exists():
        logger.info("Distance matrix already exists at {}; skipping", output_path)
        return
    try:
        if not api_key:
            raise ValueError("OPENROUTESERVICE_API_KEY is not set")
        locations = load_locations(points_path)
        matrix = walking_distance_matrix(locations, api_key)
        output = {
            "source": str(points_path),
            "units": "meters",
            "costing": "pedestrian",
            "routing_provider": "openrouteservice",
            "locations": locations,
            "distances": matrix,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "Saved a {} x {} distance matrix to {}",
            len(locations),
            len(locations),
            output_path,
        )
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
        description="Construct a distance matrix from an OOM points JSON file."
    )
    parser.add_argument("points_path", type=Path, help="OOM points JSON file")
    parser.add_argument("-o", "--output-path", type=Path, help="JSON destination")
    args = parser.parse_args()
    main(args.points_path, args.output_path, os.environ.get("OPENROUTESERVICE_API_KEY"))
