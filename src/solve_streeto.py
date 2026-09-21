"""Solve a Street-O orienteering route from a distance matrix."""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from pyvrp import Model, ProblemData, Solution, solve
from pyvrp.stop import MaxRuntime

ROUTE_DURATION_SECONDS = 60 * 60
DEFAULT_SEARCH_TIME_LIMIT_SECONDS = 0.5
MILLISECONDS_PER_SECOND = 1_000
MILLISECONDS_PER_METRE_AT_ONE_KPH = 3_600


@dataclass(frozen=True, slots=True)
class RouteSolution:
    """A solved route and its search statistics."""

    visits: tuple[int, ...]
    distance: int
    value: int
    elapsed_seconds: float
    iterations: int


class RouteSolver:
    """Reusable PyVRP problem structure for a single Street-O event."""

    def __init__(
        self,
        distances: list[list[int]],
        values: list[int],
        start_index: int,
    ) -> None:
        if len(values) != len(distances):
            raise ValueError("values and distance matrix must have the same size")
        if not 0 <= start_index < len(distances):
            raise ValueError("start index is outside the distance matrix")

        self._distances = distances
        self._values = values
        self._start = start_index
        self._client_nodes = [
            node for node in range(len(distances)) if node != start_index
        ]
        self._client_index = {
            node: client_index for client_index, node in enumerate(self._client_nodes)
        }

        model = Model()
        model_locations = [
            model.add_location(float(node), 0.0, name=str(node))
            for node in range(len(distances))
        ]
        depot = model.add_depot(location=model_locations[start_index])
        model.add_vehicle_type(
            1,
            start_depot=depot,
            end_depot=depot,
            unit_distance_cost=1,
        )
        # One point must always be worth more than any possible simple route.
        prize_multiplier = sum(max(row, default=0) for row in distances) + 1
        for node in self._client_nodes:
            model.add_client(
                location=model_locations[node],
                prize=values[node] * prize_multiplier,
                required=False,
                name=str(node),
            )
        for from_node, from_location in enumerate(model_locations):
            for to_node, to_location in enumerate(model_locations):
                model.add_edge(
                    from_location,
                    to_location,
                    distance=distances[from_node][to_node],
                    duration=(
                        distances[from_node][to_node]
                        * MILLISECONDS_PER_METRE_AT_ONE_KPH
                    ),
                )
        self._base_data = model.data()

    def _problem_data(
        self, speed_kph: int, checkpoint_time_seconds: int
    ) -> ProblemData:
        """Create a cheap speed-specific view over the shared problem data."""
        duration_matrix = np.array(
            self._base_data.duration_matrix(0), dtype=np.int64, copy=True
        )
        checkpoint_cost = checkpoint_time_seconds * MILLISECONDS_PER_SECOND * speed_kph
        if checkpoint_cost:
            duration_matrix[self._client_nodes, :] += checkpoint_cost
            duration_matrix[self._client_nodes, self._client_nodes] = 0
        vehicle_type = self._base_data.vehicle_type(0).replace(
            shift_duration=(
                ROUTE_DURATION_SECONDS * MILLISECONDS_PER_SECOND * speed_kph
            ),
        )
        return self._base_data.replace(
            vehicle_types=[vehicle_type],
            duration_matrices=[duration_matrix],
        )

    def _initial_solution(
        self, data: ProblemData, route: Sequence[int] | None
    ) -> Solution | None:
        if not route or route[0] != self._start or route[-1] != self._start:
            return None

        clients = [self._client_index[node] for node in route[1:-1]]
        return Solution(data, [clients]) if clients else None

    def solve(
        self,
        speed_kph: int,
        checkpoint_time_seconds: int = 0,
        initial_route: Sequence[int] | None = None,
        search_time_limit_seconds: float = DEFAULT_SEARCH_TIME_LIMIT_SECONDS,
    ) -> RouteSolution:
        """Find a high-value closed route for one speed and clue time."""
        if speed_kph <= 0:
            raise ValueError("speed must be positive")
        if checkpoint_time_seconds < 0:
            raise ValueError("checkpoint time cannot be negative")
        if search_time_limit_seconds <= 0:
            raise ValueError("search time limit must be positive")

        data = self._problem_data(speed_kph, checkpoint_time_seconds)
        result = solve(
            data,
            stop=MaxRuntime(search_time_limit_seconds),
            seed=0,
            collect_stats=False,
            display=False,
            initial_solution=self._initial_solution(data, initial_route),
        )
        if not result.is_feasible():
            raise RuntimeError("the solver could not find a feasible route")

        solved_routes = result.best.routes()
        visited_nodes = (
            [
                self._client_nodes[activity.idx]
                for activity in solved_routes[0]
                if activity.is_client()
            ]
            if solved_routes
            else []
        )
        route = (self._start, *visited_nodes, self._start)
        route_distance = sum(
            self._distances[from_node][to_node]
            for from_node, to_node in zip(route, route[1:], strict=False)
        )
        return RouteSolution(
            visits=route,
            distance=route_distance,
            value=sum(self._values[node] for node in visited_nodes),
            elapsed_seconds=result.runtime,
            iterations=result.num_iterations,
        )


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


def main(
    matrix_path: Path,
    start_node: str,
    speed_kph: int,
    checkpoint_time_seconds: int,
    output_path: Path | None,
) -> None:
    output_path = output_path or matrix_path.with_name(
        f"{matrix_path.stem}_solution.json"
    )
    try:
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
        solution = RouteSolver(distances, values, matching_starts[0]).solve(
            speed_kph,
            checkpoint_time_seconds,
        )
        output = {
            "source": str(matrix_path),
            "start_node": start_node,
            "speed_kph": speed_kph,
            "checkpoint_time_seconds": checkpoint_time_seconds,
            "route_duration_seconds": ROUTE_DURATION_SECONDS,
            "distance": solution.distance,
            "value": solution.value,
            "route": [locations[node] for node in solution.visits],
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "Saved route worth {} points over {} m to {}",
            solution.value,
            solution.distance,
            output_path,
        )
        logger.info(
            "Solved in {:.2f} s after {} iterations",
            solution.elapsed_seconds,
            solution.iterations,
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
        description="Maximize collected Street-O points within a time budget."
    )
    parser.add_argument("matrix_path", type=Path, help="distance matrix JSON file")
    parser.add_argument(
        "--start-node",
        default="start_finish",
        help="starting vertex number (default: start_finish)",
    )
    parser.add_argument(
        "--speed",
        required=True,
        type=int,
        help="running speed in kilometres per hour",
    )
    parser.add_argument(
        "--checkpoint-time",
        default=0,
        type=int,
        choices=(0, 5, 10, 15, 20),
        help="seconds spent solving the clue at each checkpoint",
    )
    parser.add_argument("-o", "--output-path", type=Path, help="JSON destination")
    args = parser.parse_args()
    main(
        args.matrix_path,
        args.start_node,
        args.speed,
        args.checkpoint_time,
        args.output_path,
    )
