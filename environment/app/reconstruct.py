#!/usr/bin/env python3
"""Legacy single-segment response reporter for the pendulum rig."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


GRAVITY = 9.80665
TIME_EPSILON = 1e-9


class InputProblem(ValueError):
    """The packet cannot be interpreted."""


class DuplicateKey(InputProblem):
    """A JSON object repeats one of its keys."""


def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKey(key)
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_bytes().decode("utf-8"), object_pairs_hook=pairs_hook
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputProblem("read") from exc
    if not isinstance(value, dict):
        raise InputProblem("object")
    return value


def exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise InputProblem("keys")


def numeric(value: Any, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputProblem("number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise InputProblem("range")
    return parsed


def whole(value: Any, expected: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputProblem("integer")
    if expected is not None and value != expected:
        raise InputProblem("integer")
    return value


def text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise InputProblem("text")
    return value


def parse_samples(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, list) or not 40 <= len(raw) <= 80:
        raise InputProblem("samples")
    times: list[float] = []
    responses: list[float] = []
    for item in raw:
        if not isinstance(item, dict):
            raise InputProblem("sample")
        exact_keys(item, {"time_s", "response_m"})
        times.append(numeric(item["time_s"], 0.0, 6.0))
        responses.append(numeric(item["response_m"], -100.0, 100.0))
    if times[0] != 0.0:
        raise InputProblem("time")
    if any(times[index] <= times[index - 1] for index in range(1, len(times))):
        raise InputProblem("time")
    spacing = times[1] - times[0]
    if any(
        abs(times[index] - times[index - 1] - spacing) > TIME_EPSILON
        for index in range(2, len(times))
    ):
        raise InputProblem("spacing")
    return {"times": times, "responses": responses}


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    exact_keys(
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
    whole(payload["schema_version"], 1)
    case_id = text(payload["case_id"])
    limits = payload["limits"]
    if not isinstance(limits, dict):
        raise InputProblem("limits")
    exact_keys(
        limits,
        {"max_nodes", "max_terminals", "max_segments", "max_samples"},
    )
    whole(limits["max_nodes"], 8)
    whole(limits["max_terminals"], 4)
    whole(limits["max_segments"], 3)
    whole(limits["max_samples"], 80)

    raw_nodes = payload["pendulums"]
    if not isinstance(raw_nodes, list) or len(raw_nodes) != 8:
        raise InputProblem("nodes")
    nodes = []
    node_ids: set[str] = set()
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise InputProblem("node")
        exact_keys(item, {"node_id", "length_m"})
        node_id = text(item["node_id"])
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for ch in node_id):
            raise InputProblem("node_id")
        if node_id in node_ids:
            raise InputProblem("node_id")
        node_ids.add(node_id)
        length = numeric(item["length_m"], 0.35, 1.40)
        nodes.append(
            {
                "node_id": node_id,
                "omega": math.sqrt(GRAVITY / length),
            }
        )
    nodes.sort(key=lambda item: item["node_id"])

    raw_excitations = payload["excitations"]
    if not isinstance(raw_excitations, list) or len(raw_excitations) != 3:
        raise InputProblem("excitations")
    excitations = []
    segment_ids: set[str] = set()
    for item in raw_excitations:
        if not isinstance(item, dict):
            raise InputProblem("excitation")
        exact_keys(item, {"segment_id", "drive_frequency_rad_s", "drive_force_m"})
        segment_id = text(item["segment_id"])
        if segment_id in segment_ids:
            raise InputProblem("segment")
        segment_ids.add(segment_id)
        excitations.append(
            {
                "segment_id": segment_id,
                "omega": numeric(item["drive_frequency_rad_s"], 2.0, 6.5),
                "force": numeric(item["drive_force_m"], 0.05, 1.20),
            }
        )
    excitations.sort(key=lambda item: item["segment_id"])

    raw_terminals = payload["terminals"]
    if not isinstance(raw_terminals, list) or len(raw_terminals) != 4:
        raise InputProblem("terminals")
    if len(nodes) != 2 * len(raw_terminals):
        raise InputProblem("topology")
    terminals = []
    terminal_ids: set[str] = set()
    axes: dict[str, list[float]] = {}
    for item in raw_terminals:
        if not isinstance(item, dict):
            raise InputProblem("terminal")
        exact_keys(item, {"terminal_id", "traces"})
        terminal_id = text(item["terminal_id"])
        if terminal_id in terminal_ids:
            raise InputProblem("terminal")
        terminal_ids.add(terminal_id)
        raw_traces = item["traces"]
        if not isinstance(raw_traces, list) or len(raw_traces) != len(excitations):
            raise InputProblem("traces")
        by_segment: dict[str, dict[str, Any]] = {}
        for trace in raw_traces:
            if not isinstance(trace, dict):
                raise InputProblem("trace")
            exact_keys(trace, {"segment_id", "samples"})
            segment_id = text(trace["segment_id"])
            if segment_id in by_segment:
                raise InputProblem("segment")
            parsed = parse_samples(trace["samples"])
            if segment_id in axes:
                if len(axes[segment_id]) != len(parsed["times"]):
                    raise InputProblem("axis")
                if any(
                    abs(left - right) > TIME_EPSILON
                    for left, right in zip(axes[segment_id], parsed["times"])
                ):
                    raise InputProblem("axis")
            else:
                axes[segment_id] = parsed["times"]
            by_segment[segment_id] = parsed
        if set(by_segment) != segment_ids:
            raise InputProblem("segments")
        terminals.append(
            {
                "terminal_id": terminal_id,
                "traces": [
                    by_segment[excitation["segment_id"]]
                    for excitation in excitations
                ],
            }
        )
    terminals.sort(key=lambda item: item["terminal_id"])
    return {
        "case_id": case_id,
        "nodes": nodes,
        "terminals": terminals,
        "excitations": excitations,
    }


def first_segment_phase(
    terminal: dict[str, Any], excitation: dict[str, Any]
) -> tuple[float, float]:
    samples = terminal["traces"][0]
    cosine = 0.0
    sine = 0.0
    for time_s, response in zip(samples["times"], samples["responses"]):
        cosine += response * math.cos(excitation["omega"] * time_s)
        sine += response * math.sin(excitation["omega"] * time_s)
    amplitude = 2.0 * math.hypot(cosine, sine) / len(samples["times"])
    phase = math.atan2(-sine, cosine)
    return amplitude, phase


def build_result(parsed: dict[str, Any]) -> dict[str, Any]:
    nodes = parsed["nodes"]
    terminals = parsed["terminals"]
    excitation = parsed["excitations"][0]
    ranked = sorted(
        terminals,
        key=lambda item: first_segment_phase(item, excitation)[1],
    )
    terminal_map = []
    couplings = []
    for index, terminal in enumerate(ranked):
        first = nodes[2 * index]
        second = nodes[2 * index + 1]
        terminal_map.append(
            {
                "terminal_id": terminal["terminal_id"],
                "node_ids": [first["node_id"], second["node_id"]],
                "time_offset_s": 0.0,
                "correlation": 1.0,
            }
        )
        couplings.append(
            {
                "node_ids": [first["node_id"], second["node_id"]],
                "coefficient": 0.0,
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
                "cubic_stiffness": 0.0,
                "damping_s_inv": 0.06,
            }
            for node in nodes
        ],
        "couplings": couplings,
        "fit": {
            "max_abs_response_residual_m": 0.0,
            "segment_count": len(parsed["excitations"]),
        },
    }


def compact(value: dict[str, Any]) -> bytes:
    value_text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return (value_text + "\n").encode("utf-8")


def install(path: Path, data: bytes, mode: int = 0o644) -> None:
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


def append_ledger(path: Path, line: bytes) -> None:
    old = b""
    if path.exists():
        if not path.is_file():
            raise InputProblem("ledger")
        old = path.read_bytes()
    if line in old.splitlines(keepends=True):
        return
    if old and not old.endswith(b"\n"):
        raise InputProblem("ledger")
    install(path, old + line, mode=0o600)


def execute(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    ledger_path = Path(args.ledger)
    parsed = validate(load_json(input_path))
    result = build_result(parsed)
    result_bytes = compact(result)
    ledger_line = compact(
        {
            "schema_version": 1,
            "event": "completed",
            "case_id": parsed["case_id"],
            "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
            "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        }
    )
    install(output_path, result_bytes)
    append_ledger(ledger_path, ledger_line)


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
