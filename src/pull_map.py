"""Pull scored control coordinates from a saved OOM v4 map."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from loguru import logger


def load_map(map_id: str) -> dict[str, Any]:
    """Load saved map metadata from OOM v4."""
    if not re.fullmatch(r"[0-9a-f]{13}", map_id):
        raise ValueError(
            "map ID must contain exactly 13 lowercase hexadecimal characters"
        )

    request = Request(
        "https://oomap.dna-software.co.uk/php/load.php",
        data=urlencode({"shortcode": map_id}).encode(),
        headers={"User-Agent": "StreetoSolver/0.1"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310
        result = json.load(response)

    if not result.get("success"):
        message = result.get("message", "unknown OOM error")
        raise RuntimeError(f"could not load map {map_id}: {message}")

    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"OOM returned invalid data for map {map_id}")
    return data


def scored_points(map_data: dict[str, Any]) -> list[dict[str, int | float]]:
    """Extract scored controls, excluding starts, finishes, and crossings."""
    controls = map_data.get("controls")
    if not isinstance(controls, list):
        raise RuntimeError("OOM map data does not contain a control list")

    points = [
        {
            "number": int(control["number"]),
            "latitude": float(control["wgs84lat"]),
            "longitude": float(control["wgs84lon"]),
            "points": int(control["score"]),
        }
        for control in controls
        if isinstance(control, dict) and control.get("type") == "c_regular"
    ]
    return sorted(points, key=lambda point: point["number"])


def start_finish(map_data: dict[str, Any]) -> dict[str, int | float]:
    """Extract the shared start/finish location."""
    controls = map_data.get("controls")
    if not isinstance(controls, list):
        raise RuntimeError("OOM map data does not contain a control list")

    locations = [
        {
            "latitude": float(control["wgs84lat"]),
            "longitude": float(control["wgs84lon"]),
            "points": 0,
        }
        for control in controls
        if isinstance(control, dict) and control.get("type") == "c_startfinish"
    ]
    if len(locations) != 1:
        raise RuntimeError("OOM map must contain exactly one shared start/finish")
    return locations[0]


def web_mercator(latitude: Any, longitude: Any) -> tuple[int, int]:
    """Convert WGS84 coordinates to rounded EPSG:3857 northing and easting."""
    latitude_radians = math.radians(float(latitude))
    easting = 6_378_137 * math.radians(float(longitude))
    northing = 6_378_137 * math.log(math.tan(math.pi / 4 + latitude_radians / 2))
    return round(northing), round(easting)


def render_url(map_id: str, map_data: dict[str, Any]) -> str:
    """Construct OOM v4's saved-map JPG render URL."""
    controls = map_data["controls"]

    def positions(control_type: str, include_angle: bool = False) -> str:
        values = []
        for control in controls:
            if control["type"] != control_type:
                continue
            northing, easting = web_mercator(control["wgs84lat"], control["wgs84lon"])
            if include_angle:
                values.extend([str(control["angle"]), str(northing), str(easting)])
            else:
                values.extend([str(northing), str(easting)])
        return ",".join(values)

    regular_controls = []
    for control in controls:
        if control["type"] != "c_regular":
            continue
        northing, easting = web_mercator(control["wgs84lat"], control["wgs84lon"])
        regular_controls.extend(
            [
                str(control["number"]),
                str(control["angle"]),
                str(northing),
                str(easting),
            ]
        )

    northing, easting = web_mercator(map_data["centre_lat"], map_data["centre_lon"])
    paper_long = int(map_data["papersize"][1:4]) / 1000
    paper_short = int(map_data["papersize"][6:9]) / 1000
    paper = (
        (paper_short, paper_long)
        if map_data["paperorientation"] == "portrait"
        else (paper_long, paper_short)
    )
    parameters = [
        f"style={map_data['style']}",
        f"paper={paper[0]:.3f},{paper[1]:.3f}",
        f"scale={str(map_data['scale']).removeprefix('s')}",
        f"centre={northing},{easting}",
        f"title={quote(str(map_data['title']), safe='')}",
        f"eventdate={map_data['eventdate']}",
        f"club={map_data['club']}",
        f"id={map_id}",
        f"start={positions('c_startfinish')}",
        f"finish={positions('c_finish')}",
        f"crosses={positions('c_cross')}",
        f"cps={positions('c_crossingpoint', include_angle=True)}",
        f"controls={','.join(regular_controls)}",
        f"rotation={float(map_data['rotation']):.4f}",
        "grid=yes",
        "rail=yes",
        "walls=yes",
        "trees=yes",
        "hedges=yes",
        "drives=no",
        "fences=yes",
        "sidewalks=no",
        "schools=no",
        "power=yes",
        "privroads=yes",
        "buildings=yes",
        "benches=yes",
        "halo=no",
        f"linear={'yes' if str(map_data['linear']) == '1' else 'no'}",
        "dpi=150",
        "purple=A626FF",
    ]
    return "https://oomap.dna-software.co.uk/render/jpg/?" + "|".join(parameters)


def pull_map_image(map_id: str, map_data: dict[str, Any], output_path: Path) -> None:
    """Download OOM's JPG render."""
    request = Request(
        render_url(map_id, map_data),
        headers={"User-Agent": "StreetoSolver/0.1"},
    )
    with urlopen(request, timeout=180) as response:  # noqa: S310
        image_data = response.read()
    if not image_data.startswith(b"\xff\xd8"):
        raise RuntimeError("OOM did not return a valid JPG image")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_data)


def main(map_id: str, output_path: Path | None, map_output_path: Path | None) -> None:
    output_path = output_path or Path(f"data/oom_{map_id}_points.json")
    map_output_path = map_output_path or Path(f"data/oom_{map_id}_map.jpg")
    try:
        map_data = load_map(map_id)
        output = {
            "map_id": map_id,
            "title": map_data.get("title"),
            "map_geometry": {
                "centre_latitude": float(map_data["centre_lat"]),
                "centre_longitude": float(map_data["centre_lon"]),
                "scale": int(str(map_data["scale"]).removeprefix("s")),
                "rotation": float(map_data["rotation"]),
                "dpi": 150,
            },
            "start_finish": start_finish(map_data),
            "points": scored_points(map_data),
        }
        rendered = json.dumps(output, indent=2, ensure_ascii=False) + "\n"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        pull_map_image(map_id, map_data, map_output_path)
        logger.info(
            "Saved {} scored controls to {} and map to {}",
            len(output["points"]),
            output_path,
            map_output_path,
        )
    except (
        HTTPError,
        URLError,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logger.error("{}", error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pull scored control coordinates from an OOM v4 map."
    )
    parser.add_argument(
        "-m",
        "--map-id",
        default="6a9939a6dd86d",
        help="OOM v4 map ID",
    )
    parser.add_argument(
        "-o",
        "--output-path",
        type=Path,
        help="JSON destination (default: data/oom_<map-id>_points.json)",
    )
    parser.add_argument(
        "--map-output-path",
        type=Path,
        help="JPG destination (default: data/oom_<map-id>_map.jpg)",
    )
    args = parser.parse_args()
    main(args.map_id, args.output_path, args.map_output_path)
