#!/usr/bin/env python3
"""Recover a multi-segment Duffing pendulum array from sensor traces."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


GRAVITY = 9.80665
TIME_EPSILON = 1e-9
RESIDUAL_LIMIT = 2e-4
COMPLEX = complex


class ContractError(ValueError):
    """The packet violates the frozen public contract."""


class DuplicateKeyError(ContractError):
    """A JSON object contains a repeated key."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def load_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_bytes().decode("utf-8")
        value = json.loads(text, object_pairs_hook=_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("input") from exc
    if not isinstance(value, dict):
        raise ContractError("object")
    return value


def expect_keys(value: dict[str, Any], keys: set[str]) -> None:
    if set(value) != keys:
        raise ContractError("keys")


def number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < low or parsed > high:
        raise ContractError("range")
    return parsed


def integer(value: Any, expected: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("integer")
    if expected is not None and value != expected:
        raise ContractError("integer")
    return value


def text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError("text")
    return value


def parse_samples(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, list) or not 40 <= len(raw) <= 80:
        raise ContractError("samples")
    times: list[float] = []
    values: list[float] = []
    for sample in raw:
        if not isinstance(sample, dict):
            raise ContractError("sample")
        expect_keys(sample, {"time_s", "response_m"})
        times.append(number(sample["time_s"], 0.0, 6.0))
        values.append(number(sample["response_m"], -100.0, 100.0))
    if times[0] != 0.0:
        raise ContractError("time_origin")
    if any(times[index] <= times[index - 1] for index in range(1, len(times))):
        raise ContractError("time_order")
    spacing = times[1] - times[0]
    if spacing <= 0.0:
        raise ContractError("spacing")
    for index in range(2, len(times)):
        if abs(times[index] - times[index - 1] - spacing) > TIME_EPSILON:
            raise ContractError("spacing")
    return {"times": times, "values": values, "spacing": spacing}


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    expect_keys(
        payload,
        {
            "schema_version",
            "case_id",
            "pendulums",
            "terminals",
            "excitations",
            "limits",
        },
    )
    integer(payload["schema_version"], 1)
    case_id = text(payload["case_id"])

    limits = payload["limits"]
    if not isinstance(limits, dict):
        raise ContractError("limits")
    expect_keys(
        limits,
        {"max_nodes", "max_terminals", "max_segments", "max_samples"},
    )
    integer(limits["max_nodes"], 8)
    integer(limits["max_terminals"], 4)
    integer(limits["max_segments"], 3)
    integer(limits["max_samples"], 80)

    raw_nodes = payload["pendulums"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) != 8:
        raise ContractError("pendulums")
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise ContractError("pendulum")
        expect_keys(item, {"node_id", "length_m"})
        node_id = text(item["node_id"])
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in node_id):
            raise ContractError("node_id")
        if node_id in node_ids:
            raise ContractError("node_id")
        node_ids.add(node_id)
        length = number(item["length_m"], 0.35, 1.40)
        nodes.append(
            {
                "node_id": node_id,
                "length_m": length,
                "omega": math.sqrt(GRAVITY / length),
            }
        )
    nodes.sort(key=lambda item: item["node_id"])

    raw_excitations = payload["excitations"]
    if not isinstance(raw_excitations, list) or len(raw_excitations) != 3:
        raise ContractError("excitations")
    excitations: list[dict[str, Any]] = []
    segment_ids: set[str] = set()
    for item in raw_excitations:
        if not isinstance(item, dict):
            raise ContractError("excitation")
        expect_keys(
            item,
            {"segment_id", "drive_frequency_rad_s", "drive_force_m"},
        )
        segment_id = text(item["segment_id"])
        if segment_id in segment_ids:
            raise ContractError("segment_id")
        segment_ids.add(segment_id)
        excitations.append(
            {
                "segment_id": segment_id,
                "omega": number(item["drive_frequency_rad_s"], 2.0, 6.5),
                "force": number(item["drive_force_m"], 0.05, 1.20),
            }
        )
    excitations.sort(key=lambda item: item["segment_id"])
    segment_index = {
        item["segment_id"]: index for index, item in enumerate(excitations)
    }

    raw_terminals = payload["terminals"]
    if not isinstance(raw_terminals, list) or len(raw_terminals) != 4:
        raise ContractError("terminals")
    if len(nodes) != 2 * len(raw_terminals):
        raise ContractError("topology")
    terminals: list[dict[str, Any]] = []
    terminal_ids: set[str] = set()
    common_axes: dict[str, list[float]] = {}
    common_counts: dict[str, int] = {}
    for item in raw_terminals:
        if not isinstance(item, dict):
            raise ContractError("terminal")
        expect_keys(item, {"terminal_id", "traces"})
        terminal_id = text(item["terminal_id"])
        if terminal_id in terminal_ids:
            raise ContractError("terminal_id")
        terminal_ids.add(terminal_id)
        raw_traces = item["traces"]
        if not isinstance(raw_traces, list) or len(raw_traces) != len(excitations):
            raise ContractError("traces")
        traces_by_segment: dict[str, dict[str, Any]] = {}
        for trace in raw_traces:
            if not isinstance(trace, dict):
                raise ContractError("trace")
            expect_keys(trace, {"segment_id", "samples"})
            segment_id = text(trace["segment_id"])
            if segment_id not in segment_index or segment_id in traces_by_segment:
                raise ContractError("segment_id")
            parsed = parse_samples(trace["samples"])
            if segment_id not in common_axes:
                common_axes[segment_id] = parsed["times"]
                common_counts[segment_id] = len(parsed["times"])
            else:
                if common_counts[segment_id] != len(parsed["times"]):
                    raise ContractError("axis")
                if any(
                    abs(left - right) > TIME_EPSILON
                    for left, right in zip(common_axes[segment_id], parsed["times"])
                ):
                    raise ContractError("axis")
            traces_by_segment[segment_id] = parsed
        if set(traces_by_segment) != segment_ids:
            raise ContractError("segment_set")
        terminals.append(
            {
                "terminal_id": terminal_id,
                "traces": [
                    traces_by_segment[item["segment_id"]] for item in excitations
                ],
            }
        )
    terminals.sort(key=lambda item: item["terminal_id"])
    return {
        "case_id": case_id,
        "nodes": nodes,
        "excitations": excitations,
        "terminals": terminals,
    }


def solve_amplitude(
    omega: float,
    beta: float,
    damping: float,
    drive_omega: float,
    force: float,
) -> float:
    def residual(amplitude: float) -> float:
        stiffness = omega * omega + beta * amplitude * amplitude - drive_omega * drive_omega
        damping_term = 2.0 * damping * drive_omega
        return amplitude * amplitude * (
            stiffness * stiffness + damping_term * damping_term
        ) - force * force

    low = 0.0
    high = max(0.01, force / max(0.5, abs(omega * omega - drive_omega * drive_omega)))
    for _ in range(40):
        if residual(high) >= 0.0:
            break
        high *= 1.7
    else:
        raise ContractError("amplitude")
    for _ in range(58):
        middle = 0.5 * (low + high)
        if residual(middle) >= 0.0:
            high = middle
        else:
            low = middle
    return 0.5 * (low + high)


def node_response(
    node: dict[str, Any],
    excitation: dict[str, Any],
    damping: float,
    beta: float,
) -> complex:
    amplitude = solve_amplitude(
        node["omega"], beta, damping, excitation["omega"], excitation["force"]
    )
    real = node["omega"] ** 2 + beta * amplitude * amplitude - excitation["omega"] ** 2
    imag = 2.0 * damping * excitation["omega"]
    return complex(amplitude * real / math.hypot(real, imag), -amplitude * imag / math.hypot(real, imag))


def pair_response(
    first: dict[str, Any],
    second: dict[str, Any],
    excitation: dict[str, Any],
    damping_first: float,
    damping_second: float,
    beta_first: float,
    beta_second: float,
    coupling: float,
    offset: float,
) -> complex:
    left = node_response(first, excitation, damping_first, beta_first)
    right = node_response(second, excitation, damping_second, beta_second)
    combined = left + right + coupling * left * right
    return combined * complex(
        math.cos(excitation["omega"] * offset),
        -math.sin(excitation["omega"] * offset),
    )


def solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    size = len(rhs)
    augmented = [
        [float(value) for value in matrix[row]] + [float(rhs[row])]
        for row in range(size)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-14:
            raise ContractError("singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][size] for row in range(size)]


def extract_complex(samples: dict[str, Any], drive_omega: float) -> complex:
    columns: list[list[float]] = [[], [], [], []]
    target: list[float] = []
    for time_s, value in zip(samples["times"], samples["values"]):
        columns[0].append(math.cos(drive_omega * time_s))
        columns[1].append(math.sin(drive_omega * time_s))
        columns[2].append(math.cos(2.0 * drive_omega * time_s))
        columns[3].append(math.sin(2.0 * drive_omega * time_s))
        target.append(value)
    normal = [[0.0 for _ in range(4)] for _ in range(4)]
    rhs = [0.0 for _ in range(4)]
    for row in range(4):
        rhs[row] = sum(a * b for a, b in zip(columns[row], target))
        for column in range(row, 4):
            value = sum(a * b for a, b in zip(columns[row], columns[column]))
            normal[row][column] = value
            normal[column][row] = value
    coefficients = solve_linear(normal, rhs)
    return complex(coefficients[0], -coefficients[1])


def observed_responses(parsed: dict[str, Any]) -> list[list[complex]]:
    observed: list[list[complex]] = []
    for terminal in parsed["terminals"]:
        row = []
        for excitation, samples in zip(parsed["excitations"], terminal["traces"]):
            row.append(extract_complex(samples, excitation["omega"]))
        observed.append(row)
    return observed


def complex_residual(predicted: complex, observed: complex) -> tuple[float, float]:
    difference = predicted - observed
    return difference.real, difference.imag


def low_force_model(
    first: dict[str, Any],
    second: dict[str, Any],
    excitation: dict[str, Any],
    damping_first: float,
    damping_second: float,
    offset: float,
) -> complex:
    return pair_response(
        first,
        second,
        excitation,
        damping_first,
        damping_second,
        0.0,
        0.0,
        0.0,
        offset,
    )


def make_residual(
    prediction: Callable[[list[float], int], complex],
    observed: list[complex],
    indices: list[int],
) -> Callable[[list[float]], list[float]]:
    def residual(parameters: list[float]) -> list[float]:
        values: list[float] = []
        for segment_index in indices:
            real, imag = complex_residual(
                prediction(parameters, segment_index),
                observed[segment_index],
            )
            scale = max(0.02, abs(observed[segment_index]))
            values.extend([real / scale, imag / scale])
        return values

    return residual


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def lm_fit(
    residual: Callable[[list[float]], list[float]],
    starts: list[list[float]],
    bounds: list[tuple[float, float]],
    steps: list[float],
    iterations: int = 14,
) -> tuple[float, list[float]]:
    best_cost = math.inf
    best_parameters: list[float] | None = None
    for raw_start in starts:
        parameters = [
            clamp(value, bounds[index][0], bounds[index][1])
            for index, value in enumerate(raw_start)
        ]
        values = residual(parameters)
        cost = sum(value * value for value in values)
        damping = 1e-2
        for _ in range(iterations):
            jacobian: list[list[float]] = []
            for parameter_index, step in enumerate(steps):
                upper = list(parameters)
                lower = list(parameters)
                upper[parameter_index] += step
                lower[parameter_index] -= step
                upper_residual = residual(upper)
                lower_residual = residual(lower)
                jacobian.append(
                    [
                        (high - low) / (2.0 * step)
                        for high, low in zip(upper_residual, lower_residual)
                    ]
                )
            size = len(parameters)
            normal = [[0.0 for _ in range(size)] for _ in range(size)]
            gradient = [0.0 for _ in range(size)]
            for row in range(size):
                gradient[row] = sum(
                    jacobian[row][index] * values[index]
                    for index in range(len(values))
                )
                for column in range(size):
                    normal[row][column] = sum(
                        jacobian[row][index] * jacobian[column][index]
                        for index in range(len(values))
                    )
            accepted = False
            for _ in range(8):
                system = [
                    [
                        normal[row][column]
                        + (damping * max(1.0, normal[row][row]) if row == column else 0.0)
                        for column in range(size)
                    ]
                    for row in range(size)
                ]
                try:
                    change = solve_linear(system, [-value for value in gradient])
                except ContractError:
                    damping *= 10.0
                    continue
                candidate = [
                    clamp(
                        parameters[index] + change[index],
                        bounds[index][0],
                        bounds[index][1],
                    )
                    for index in range(size)
                ]
                candidate_values = residual(candidate)
                candidate_cost = sum(value * value for value in candidate_values)
                if candidate_cost < cost:
                    parameters = candidate
                    values = candidate_values
                    cost = candidate_cost
                    damping = max(1e-8, damping * 0.25)
                    accepted = True
                    break
                damping *= 8.0
            if not accepted:
                break
        if cost < best_cost:
            best_cost = cost
            best_parameters = parameters
    if best_parameters is None:
        raise ContractError("fit")
    return best_cost, best_parameters


def low_pair_fit(
    first: dict[str, Any],
    second: dict[str, Any],
    parsed: dict[str, Any],
    observed: list[complex],
    indices: list[int],
) -> tuple[float, list[float]]:
    def prediction(parameters: list[float], segment_index: int) -> complex:
        return low_force_model(
            first,
            second,
            parsed["excitations"][segment_index],
            parameters[0],
            parameters[1],
            parameters[2],
        )

    residual = make_residual(prediction, observed, indices)
    starts = [
        [first_damping, second_damping, 0.0]
        for first_damping in (0.03, 0.08, 0.15)
        for second_damping in (0.03, 0.08, 0.15)
    ]
    return lm_fit(
        residual,
        starts,
        [(0.005, 0.30), (0.005, 0.30), (-0.08, 0.08)],
        [1e-4, 1e-4, 1e-5],
        iterations=12,
    )


def full_pair_fit(
    first: dict[str, Any],
    second: dict[str, Any],
    parsed: dict[str, Any],
    observed: list[complex],
    low_parameters: list[float],
    iterations: int = 18,
) -> tuple[float, list[float]]:
    all_indices = list(range(len(parsed["excitations"])))

    def prediction(parameters: list[float], segment_index: int) -> complex:
        return pair_response(
            first,
            second,
            parsed["excitations"][segment_index],
            parameters[0],
            parameters[1],
            parameters[2],
            parameters[3],
            parameters[4],
            parameters[5],
        )

    residual = make_residual(prediction, observed, all_indices)
    starts = [
        [0.03, 0.12, 100.0, 250.0, 80.0, 0.0],
        [0.12, 0.03, 250.0, 100.0, 80.0, 0.0],
        [0.07, 0.07, 180.0, 280.0, 0.0, 0.0],
        [0.03, 0.03, 100.0, 180.0, 60.0, 0.0],
        [0.15, 0.15, 300.0, 250.0, 100.0, 0.0],
        [0.06, 0.09, 200.0, 280.0, 70.0, 0.03],
        [0.09, 0.06, 280.0, 200.0, 70.0, -0.03],
        [0.05, 0.05, 150.0, 250.0, 100.0, 0.05],
        [0.07, 0.09, 360.0, 450.0, 80.0, 0.02],
        [0.10, 0.06, 450.0, 350.0, 90.0, -0.02],
        [0.05, 0.12, 250.0, 420.0, 60.0, 0.05],
        [0.04, 0.08, 140.0, 340.0, 50.0, -0.02],
        [0.08, 0.04, 340.0, 140.0, 50.0, 0.02],
        [0.06, 0.10, 250.0, 380.0, 60.0, 0.03],
        [0.10, 0.06, 380.0, 250.0, 60.0, -0.03],
        [
            low_parameters[0],
            low_parameters[1],
            150.0,
            220.0,
            0.0,
            low_parameters[2],
        ],
    ]
    return lm_fit(
        residual,
        starts,
        [
            (0.005, 0.30),
            (0.005, 0.30),
            (10.0, 500.0),
            (10.0, 500.0),
            (-200.0, 200.0),
            (-0.08, 0.08),
        ],
        [8e-5, 8e-5, 0.04, 0.04, 0.02, 8e-6],
        iterations=iterations,
    )


def assignment_for_terminal(
    terminal_index: int,
    parsed: dict[str, Any],
    observed: list[list[complex]],
    low_indices: list[int],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    terminals = parsed["terminals"]
    nodes = parsed["nodes"]
    candidates = []
    for first_index in range(len(nodes)):
        for second_index in range(first_index + 1, len(nodes)):
            low_cost, low_parameters = low_pair_fit(
                nodes[first_index],
                nodes[second_index],
                parsed,
                observed[terminal_index],
                low_indices,
            )
            cost, parameters = full_pair_fit(
                nodes[first_index],
                nodes[second_index],
                parsed,
                observed[terminal_index],
                low_parameters,
                iterations=10,
            )
            candidates.append(
                {
                    "first": first_index,
                    "second": second_index,
                    "cost": cost,
                    "parameters": parameters,
                    "low_cost": low_cost,
                }
            )
    candidates.sort(key=lambda item: item["cost"])
    shortlist = candidates[:4]
    if len(shortlist) < 4:
        raise ContractError("candidates")
    return terminals[terminal_index], shortlist


def global_mapping(
    parsed: dict[str, Any],
    observed: list[list[complex]],
    low_indices: list[int],
) -> list[dict[str, Any]]:
    terminal_candidates = [
        assignment_for_terminal(index, parsed, observed, low_indices)
        for index in range(len(parsed["terminals"]))
    ]
    node_count = len(parsed["nodes"])
    full_mask = (1 << node_count) - 1
    states: dict[int, tuple[float, list[dict[str, Any]]]] = {0: (0.0, [])}
    for _, candidates in terminal_candidates:
        next_states: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        for mask, (cost, selected) in states.items():
            for candidate in candidates:
                bit = (1 << candidate["first"]) | (1 << candidate["second"])
                if mask & bit:
                    continue
                next_mask = mask | bit
                new_cost = cost + candidate["cost"]
                previous = next_states.get(next_mask)
                if previous is None or new_cost < previous[0]:
                    next_states[next_mask] = (new_cost, selected + [candidate])
        states = next_states
        if not states:
            raise ContractError("assignment")
    if full_mask not in states:
        raise ContractError("assignment")
    best_cost, selected = states[full_mask]
    alternatives = 0
    alternative_cost = math.inf
    for mask, (cost, _) in states.items():
        if mask == full_mask:
            continue
        alternatives += 1
        alternative_cost = min(alternative_cost, cost)
    if alternatives and alternative_cost < best_cost * 1.01:
        raise ContractError("ambiguous")
    return selected


def fit_mapping(
    parsed: dict[str, Any],
    observed: list[list[complex]],
) -> list[dict[str, Any]]:
    force_order = sorted(
        range(len(parsed["excitations"])),
        key=lambda index: parsed["excitations"][index]["force"],
    )
    low_indices = force_order[:3]
    selected = global_mapping(parsed, observed, low_indices)
    fitted = []
    for terminal_index, candidate in enumerate(selected):
        first = parsed["nodes"][candidate["first"]]
        second = parsed["nodes"][candidate["second"]]
        cost, parameters = full_pair_fit(
            first,
            second,
            parsed,
            observed[terminal_index],
            candidate["parameters"],
            iterations=32,
        )
        fitted.append(
            {
                "terminal": parsed["terminals"][terminal_index],
                "first": candidate["first"],
                "second": candidate["second"],
                "cost": cost,
                "parameters": parameters,
            }
        )
    return fitted


def max_response_residual(
    parsed: dict[str, Any], fitted: list[dict[str, Any]]
) -> float:
    maximum = 0.0
    nodes = parsed["nodes"]
    for item in fitted:
        first = nodes[item["first"]]
        second = nodes[item["second"]]
        parameters = item["parameters"]
        for segment_index, excitation in enumerate(parsed["excitations"]):
            predicted = pair_response(
                first,
                second,
                excitation,
                parameters[0],
                parameters[1],
                parameters[2],
                parameters[3],
                parameters[4],
                parameters[5],
            )
            samples = item["terminal"]["traces"][segment_index]
            for time_s, observed in zip(samples["times"], samples["values"]):
                modeled = (
                    predicted
                    * complex(
                        math.cos(excitation["omega"] * time_s),
                        math.sin(excitation["omega"] * time_s),
                    )
                ).real
                maximum = max(maximum, abs(modeled - observed))
    return maximum


def solve(parsed: dict[str, Any]) -> dict[str, Any]:
    observed = observed_responses(parsed)
    fitted = fit_mapping(parsed, observed)
    residual = max_response_residual(parsed, fitted)
    if residual > RESIDUAL_LIMIT:
        raise ContractError("residual")

    nodes = parsed["nodes"]
    damping = [0.0] * len(nodes)
    beta = [0.0] * len(nodes)
    terminal_map = []
    couplings = []
    for item in fitted:
        first_index = item["first"]
        second_index = item["second"]
        parameters = item["parameters"]
        damping[first_index] = parameters[0]
        damping[second_index] = parameters[1]
        beta[first_index] = parameters[2]
        beta[second_index] = parameters[3]
        first_id = nodes[first_index]["node_id"]
        second_id = nodes[second_index]["node_id"]
        terminal_map.append(
            {
                "terminal_id": item["terminal"]["terminal_id"],
                "node_ids": [first_id, second_id],
                "time_offset_s": round(parameters[5], 9),
                "correlation": round(1.0 / (1.0 + item["cost"]), 9),
            }
        )
        couplings.append(
            {
                "node_ids": [first_id, second_id],
                "coefficient": round(parameters[4], 9),
            }
        )
    terminal_map.sort(key=lambda item: item["terminal_id"])
    couplings.sort(key=lambda item: tuple(item["node_ids"]))
    return {
        "schema_version": 1,
        "case_id": parsed["case_id"],
        "terminal_map": terminal_map,
        "pendulums": [
            {
                "node_id": node["node_id"],
                "frequency_rad_s": round(node["omega"], 12),
                "cubic_stiffness": round(beta[index], 9),
                "damping_s_inv": round(damping[index], 9),
            }
            for index, node in enumerate(nodes)
        ],
        "couplings": couplings,
        "fit": {
            "max_abs_response_residual_m": round(residual, 10),
            "segment_count": len(parsed["excitations"]),
        },
    }


def compact_bytes(value: dict[str, Any]) -> bytes:
    text_value = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (text_value + "\n").encode("utf-8")


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def planned_ledger(path: Path, line: bytes) -> bytes:
    existing = b""
    if path.exists():
        if not path.is_file():
            raise ContractError("ledger")
        existing = path.read_bytes()
    if line in existing.splitlines(keepends=True):
        return existing
    if existing and not existing.endswith(b"\n"):
        raise ContractError("ledger")
    return existing + line


def execute(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    ledger_path = Path(args.ledger)
    if output_path.exists() and not output_path.is_file():
        raise ContractError("output_type")
    if ledger_path.exists():
        if not ledger_path.is_file():
            raise ContractError("ledger_type")
        existing_ledger = ledger_path.read_bytes()
        if existing_ledger and not existing_ledger.endswith(b"\n"):
            raise ContractError("ledger_format")
    input_bytes = input_path.read_bytes()
    parsed = validate(load_payload(input_path))
    result = solve(parsed)
    result_bytes = compact_bytes(result)
    ledger_record = compact_bytes(
        {
            "schema_version": 1,
            "event": "completed",
            "case_id": parsed["case_id"],
            "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        }
    )
    planned = planned_ledger(ledger_path, ledger_record)
    atomic_write(output_path, result_bytes)
    if not ledger_path.exists() or ledger_path.read_bytes() != planned:
        atomic_write(ledger_path, planned, mode=0o600)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ledger", required=True)
    return parser.parse_args(argv)


def main() -> int:
    try:
        execute(parse_args(sys.argv[1:]))
    except Exception:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
