"""Solve a Street-O orienteering route from a distance matrix."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from loguru import logger
from ortools.constraint_solver import pywrapcp, routing_enums_pb2


def load_problem(matrix_path: Path) -> tuple[list[list[int]], list[dict[str, Any]]]:
    """Load and validate the distance matrix and vertex values."""
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    distances = data["distances"]
    locations = data["locations"]
    if not isinstance(distances, list) or not isinstance(locations, list):
        raise ValueError("invalid distance matrix structure")
    if not locations or len(distances) != len(locations):
        raise ValueError("distance matrix and locations must have the same size")
    if any(not isinstance(location, dict) for location in locations):
        raise ValueError("every location must be an object")
    if any(
        not isinstance(row, list) or len(row) != len(locations) for row in distances
    ):
        raise ValueError("distance matrix must be square")

    integer_distances = [
        [math.ceil(float(distance)) for distance in row] for row in distances
    ]
    if any(distance < 0 for row in integer_distances for distance in row):
        raise ValueError("distances cannot be negative")
    for location in locations:
        if int(location["points"]) < 0:
            raise ValueError("vertex values cannot be negative")
        if "number" not in location:
            raise ValueError("every location must have a number")
    return integer_distances, locations


def solve_route(
    distances: list[list[int]],
    values: list[int],
    start_index: int,
    maximum_path_length: int,
) -> tuple[list[int], int, int]:
    """Find a high-value closed route within the distance budget."""
    manager = pywrapcp.RoutingIndexManager(len(distances), 1, start_index)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return distances[from_node][to_node]

    transit_index = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_index)
    routing.AddDimension(
        transit_index,
        0,
        maximum_path_length,
        True,
        "Distance",
    )

    value_multiplier = maximum_path_length + 1
    for node, value in enumerate(values):
        if node != start_index:
            routing.AddDisjunction(
                [manager.NodeToIndex(node)], value * value_multiplier
            )

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search.time_limit.seconds = 30

    solution = routing.SolveWithParameters(search)
    if solution is None:
        raise RuntimeError("the solver could not find a route")

    route = []
    route_distance = 0
    index = routing.Start(0)
    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        route.append(node)
        next_index = solution.Value(routing.NextVar(index))
        route_distance += distances[node][manager.IndexToNode(next_index)]
        index = next_index
    route.append(manager.IndexToNode(index))
    route_value = sum(values[node] for node in set(route))
    return route, route_distance, route_value


def main(
    matrix_path: Path,
    start_node: str,
    maximum_path_length: int,
    output_path: Path | None,
) -> None:
    output_path = output_path or matrix_path.with_name(
        f"{matrix_path.stem}_solution.json"
    )
    try:
        if maximum_path_length <= 0:
            raise ValueError("maximum path length must be positive")
        distances, locations = load_problem(matrix_path)
        matching_starts = [
            index
            for index, location in enumerate(locations)
            if str(location["number"]) == start_node
        ]
        if len(matching_starts) != 1:
            raise ValueError(
                f"start node {start_node!r} does not uniquely identify a vertex"
            )

        values = [int(location["points"]) for location in locations]
        route, route_distance, route_value = solve_route(
            distances,
            values,
            matching_starts[0],
            maximum_path_length,
        )
        output = {
            "source": str(matrix_path),
            "start_node": start_node,
            "maximum_path_length": maximum_path_length,
            "distance": route_distance,
            "value": route_value,
            "route": [locations[node] for node in route],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "Saved route worth {} points over {} m to {}",
            route_value,
            route_distance,
            output_path,
        )
    except (
        KeyError,
        TypeError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logger.error("{}", error)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Maximize collected Street-O points within a distance budget."
    )
    parser.add_argument("matrix_path", type=Path, help="distance matrix JSON file")
    parser.add_argument(
        "--start-node",
        default="start_finish",
        help="starting vertex number (default: start_finish)",
    )
    parser.add_argument(
        "--maximum-path-length",
        required=True,
        type=int,
        help="maximum closed-route distance in metres",
    )
    parser.add_argument("-o", "--output-path", type=Path, help="JSON destination")
    args = parser.parse_args()
    main(
        args.matrix_path,
        args.start_node,
        args.maximum_path_length,
        args.output_path,
    )
