from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
from pathlib import Path

import pytest


ENTRY = os.environ.get("YEL005_ENTRY", "/app/reconstruct.py")
GRAVITY = 9.80665
LENGTHS = [
    1.34,
    1.24,
    1.15,
    1.06,
    0.98,
    0.90,
    0.82,
    0.74,
]
DAMPINGS = [0.020 + 0.006 * index for index in range(8)]
BETAS = [110.0 + 30.0 * index for index in range(8)]
COUPLINGS = [40.0 + 8.0 * index for index in range(4)]
OFFSETS = [-0.045, -0.015, 0.020, 0.050]
SEGMENTS = [
    ("S01", 2.8, 0.08),
    ("S02", 3.3, 0.16),
    ("S03", 4.2, 0.42),
    ("S04", 4.8, 0.62),
]
QUOTE = """题面：「上限本身允许，超界值拒绝。」"""


def _solve_amplitude(omega: float, beta: float, damping: float, drive: float, force: float) -> float:
    def residual(amplitude: float) -> float:
        stiffness = omega * omega + beta * amplitude * amplitude - drive * drive
        damping_term = 2.0 * damping * drive
        return amplitude * amplitude * (
            stiffness * stiffness + damping_term * damping_term
        ) - force * force

    low = 0.0
    high = max(0.01, force / max(0.5, abs(omega * omega - drive * drive)))
    while residual(high) < 0.0:
        high *= 1.7
    for _ in range(58):
        middle = 0.5 * (low + high)
        if residual(middle) >= 0.0:
            high = middle
        else:
            low = middle
    return 0.5 * (low + high)


def _node_u(index: int, drive: float, force: float) -> complex:
    omega = math.sqrt(GRAVITY / LENGTHS[index])
    amplitude = _solve_amplitude(
        omega, BETAS[index], DAMPINGS[index], drive, force
    )
    real = omega * omega + BETAS[index] * amplitude * amplitude - drive * drive
    imag = 2.0 * DAMPINGS[index] * drive
    return complex(
        amplitude * real / math.hypot(real, imag),
        -amplitude * imag / math.hypot(real, imag),
    )


def _groups(terminal_count: int) -> list[tuple[int, int]]:
    return [(index, index + terminal_count) for index in range(terminal_count)]


def _pair_u(
    terminal_index: int,
    terminal_count: int,
    drive: float,
    force: float,
) -> complex:
    first, second = _groups(terminal_count)[terminal_index]
    left = _node_u(first, drive, force)
    right = _node_u(second, drive, force)
    combined = left + right + COUPLINGS[terminal_index] * left * right
    offset = OFFSETS[terminal_index]
    return combined * complex(math.cos(drive * offset), -math.sin(drive * offset))


def _payload(
    case_id: str = "multi-segment-01",
    terminal_count: int = 4,
    segment_count: int = 3,
    sample_count: int = 80,
    duration_s: float = 6.0,
) -> dict:
    node_count = 2 * terminal_count
    times = [
        round(duration_s * index / (sample_count - 1), 10)
        for index in range(sample_count)
    ]
    excitations = [
        {
            "segment_id": segment_id,
            "drive_frequency_rad_s": frequency,
            "drive_force_m": force,
        }
        for segment_id, frequency, force in SEGMENTS[:segment_count]
    ]
    terminals = []
    for terminal_index in range(terminal_count):
        traces = []
        for segment_id, frequency, force in SEGMENTS[:segment_count]:
            amplitude = _pair_u(
                terminal_index, terminal_count, frequency, force
            )
            samples = []
            for time_s in times:
                response = (
                    amplitude
                    * complex(
                        math.cos(frequency * time_s),
                        math.sin(frequency * time_s),
                    )
                ).real
                samples.append(
                    {"time_s": time_s, "response_m": round(response, 10)}
                )
            traces.append({"segment_id": segment_id, "samples": samples})
        terminals.append(
            {"terminal_id": f"T{terminal_index + 1:02d}", "traces": traces}
        )
    return {
        "schema_version": 1,
        "case_id": case_id,
        "pendulums": [
            {"node_id": f"P{index + 1:02d}", "length_m": LENGTHS[index]}
            for index in range(node_count)
        ],
        "terminals": terminals,
        "excitations": excitations,
        "limits": {
            "max_nodes": 8,
            "max_terminals": 4,
            "max_segments": 3,
            "max_samples": 80,
        },
    }


def _invoke(
    directory: Path,
    payload: dict,
    entry: str = ENTRY,
    existing_output: bytes | None = None,
    existing_ledger: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    input_path = directory / "request.json"
    output_path = directory / "response.json"
    ledger_path = directory / "events.jsonl"
    directory.mkdir(parents=True, exist_ok=True)
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    if existing_output is not None:
        output_path.write_bytes(existing_output)
    if existing_ledger is not None:
        ledger_path.write_bytes(existing_ledger)
    completed = subprocess.run(
        [
            "python3",
            entry,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--ledger",
            str(ledger_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=os.environ.copy(),
    )
    return completed, output_path, ledger_path, input_path


def _raw_invoke(
    directory: Path, raw: bytes, entry: str = ENTRY
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    input_path = directory / "raw.json"
    output_path = directory / "raw-out.json"
    ledger_path = directory / "raw-events.jsonl"
    directory.mkdir(parents=True, exist_ok=True)
    input_path.write_bytes(raw)
    completed = subprocess.run(
        [
            "python3",
            entry,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--ledger",
            str(ledger_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=os.environ.copy(),
    )
    return completed, output_path, ledger_path


@pytest.fixture(scope="session")
def solved_case(tmp_path_factory: pytest.TempPathFactory) -> dict:
    directory = tmp_path_factory.mktemp("solved-case")
    payload = _payload()
    completed, output_path, ledger_path, input_path = _invoke(directory, payload)
    result = json.loads(output_path.read_text(encoding="utf-8"))
    raw = output_path.read_bytes()
    return {
        "directory": directory,
        "payload": payload,
        "completed": completed,
        "output_path": output_path,
        "ledger_path": ledger_path,
        "input_path": input_path,
        "result": result,
        "raw": raw,
    }


def test_recovers_full_nonlinear_case(solved_case: dict):
    """题面：「接线关系、时基偏移和全部物理参数都没有直接给出。」"""
    payload = solved_case["payload"]
    result = solved_case["result"]
    assert solved_case["completed"].returncode == 0
    assert len(payload["pendulums"]) == 8
    assert len(payload["terminals"]) == 4
    assert len(payload["excitations"]) == 3
    assert all(
        len(trace["samples"]) == 80
        for terminal in payload["terminals"]
        for trace in terminal["traces"]
    )
    assert max(
        sample["time_s"]
        for terminal in payload["terminals"]
        for trace in terminal["traces"]
        for sample in trace["samples"]
    ) == pytest.approx(6.0)
    assert len(result["terminal_map"]) == 4
    assert len(result["pendulums"]) == 8
    assert len(result["couplings"]) == 4
    assert result["fit"]["segment_count"] == 3
    assert result["fit"]["max_abs_response_residual_m"] <= 2e-4
    mapping = {
        item["terminal_id"]: item["node_ids"]
        for item in result["terminal_map"]
    }
    assert mapping["T01"] == ["P01", "P05"]
    assert mapping["T04"] == ["P04", "P08"]
    assert result["pendulums"][0]["cubic_stiffness"] > 50.0


def test_recovers_terminal_mapping(solved_case: dict):
    """题面：「每个端子恰好连接两根不同摆杆」。"""
    actual = {
        item["terminal_id"]: item["node_ids"]
        for item in solved_case["result"]["terminal_map"]
    }
    groups = _groups(4)
    expected = {
        f"T{index + 1:02d}": [
            f"P{groups[index][0] + 1:02d}",
            f"P{groups[index][1] + 1:02d}",
        ]
        for index in range(4)
    }
    assert solved_case["completed"].returncode == 0
    assert actual == expected


def test_variant_identifiers_cannot_replay_fixed_fixture(tmp_path: Path):
    """两个不同输入场景必须改变输出绑定，静态回放原夹具不能得分。"""
    payload = _payload("variant-identifiers")
    node_ids = {
        item["node_id"]: f"N{20 - index:02d}"
        for index, item in enumerate(payload["pendulums"])
    }
    for item in payload["pendulums"]:
        item["node_id"] = node_ids[item["node_id"]]
    terminal_ids = {
        item["terminal_id"]: f"X{index + 11:02d}"
        for index, item in enumerate(payload["terminals"])
    }
    for item in payload["terminals"]:
        item["terminal_id"] = terminal_ids[item["terminal_id"]]

    completed, output_path, _, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    actual = {
        item["terminal_id"]: item["node_ids"]
        for item in result["terminal_map"]
    }
    expected = {}
    for index, terminal in enumerate(payload["terminals"]):
        first, second = _groups(4)[index]
        expected[terminal["terminal_id"]] = sorted(
            [node_ids[f"P{first + 1:02d}"], node_ids[f"P{second + 1:02d}"]]
        )
    assert actual == expected


def test_recovers_sensor_time_offsets(solved_case: dict):
    """题面：「传感器的时基字段 `time_offset_s` 仍取 `-0.08` 至 `0.08` 区间，摆动相位反演后与真值的允许偏差为 `3e-4 s`。」"""
    actual = {
        item["terminal_id"]: item["time_offset_s"]
        for item in solved_case["result"]["terminal_map"]
    }
    assert solved_case["completed"].returncode == 0
    assert abs(actual["T01"] - OFFSETS[0]) <= 3e-4
    assert abs(actual["T02"] - OFFSETS[1]) <= 3e-4
    assert abs(actual["T04"] - OFFSETS[3]) <= 3e-4


def test_recovers_cubic_stiffness(solved_case: dict):
    """题面：「cubic stiffness 允许偏差 `0.08`。」"""
    actual = {
        item["node_id"]: item["cubic_stiffness"]
        for item in solved_case["result"]["pendulums"]
    }
    assert solved_case["completed"].returncode == 0
    assert abs(actual["P01"] - BETAS[0]) <= 0.08
    assert abs(actual["P05"] - BETAS[4]) <= 0.08
    assert abs(actual["P08"] - BETAS[7]) <= 0.08


def test_recovers_node_damping_values(solved_case: dict):
    """题面：「阻尼允许偏差 `8e-4 s^-1`。」"""
    actual = {
        item["node_id"]: item["damping_s_inv"]
        for item in solved_case["result"]["pendulums"]
    }
    assert solved_case["completed"].returncode == 0
    assert abs(actual["P01"] - DAMPINGS[0]) <= 8e-4
    assert abs(actual["P06"] - DAMPINGS[5]) <= 8e-4
    assert abs(actual["P08"] - DAMPINGS[7]) <= 8e-4


def test_recovers_coupling_coefficients(solved_case: dict):
    """题面：「耦合系数允许偏差 `0.35`。」"""
    actual = {
        tuple(item["node_ids"]): item["coefficient"]
        for item in solved_case["result"]["couplings"]
    }
    assert solved_case["completed"].returncode == 0
    assert abs(actual[("P01", "P05")] - COUPLINGS[0]) <= 0.35
    assert abs(actual[("P02", "P06")] - COUPLINGS[1]) <= 0.35
    assert abs(actual[("P04", "P08")] - COUPLINGS[3]) <= 0.35


def test_emits_exact_result_schema(solved_case: dict):
    """题面：「成功结果写入 `--output`。」"""
    result = solved_case["result"]
    assert solved_case["completed"].returncode == 0
    assert set(result) == {
        "schema_version",
        "case_id",
        "terminal_map",
        "pendulums",
        "couplings",
        "fit",
    }
    assert set(result["fit"]) == {
        "max_abs_response_residual_m",
        "segment_count",
    }
    assert set(result["pendulums"][0]) == {
        "node_id",
        "frequency_rad_s",
        "cubic_stiffness",
        "damping_s_inv",
    }


def test_ledger_has_exact_keys_and_digests(solved_case: dict):
    """题面：「每条成功记录一行，字段固定为：」"""
    record = json.loads(
        solved_case["ledger_path"].read_text(encoding="utf-8")
    )
    assert solved_case["completed"].returncode == 0
    assert set(record) == {
        "case_id",
        "event",
        "input_sha256",
        "result_sha256",
        "schema_version",
    }
    assert record["result_sha256"] == hashlib.sha256(solved_case["raw"]).hexdigest()


def test_repeating_same_input_does_not_append(solved_case: dict):
    """题面：「同一输入重复执行时不得新增或改写账本行。」"""
    before = solved_case["ledger_path"].read_bytes()
    completed, _, _, _ = _invoke(
        solved_case["directory"], solved_case["payload"]
    )
    assert completed.returncode == 0
    assert solved_case["ledger_path"].read_bytes() == before


def test_new_case_appends_ledger_record(solved_case: dict):
    """题面：「新的成功结果必须追加一行。」"""
    ledger_path = solved_case["ledger_path"]
    first_count = len(ledger_path.read_text(encoding="utf-8").splitlines())
    second, _, _, _ = _invoke(
        solved_case["directory"], _payload("case-b")
    )
    second_count = len(ledger_path.read_text(encoding="utf-8").splitlines())
    assert solved_case["completed"].returncode == 0
    assert second.returncode == 0
    assert first_count == 1
    assert second_count == 2


def test_rejects_sample_count_above_eighty(tmp_path: Path):
    """题面：「每段轨迹有 40 至 80 个样本。」"""
    completed, output_path, ledger_path, _ = _invoke(
        tmp_path,
        _payload(sample_count=81),
    )
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_sample_count_below_forty(tmp_path: Path):
    """题面：「每段轨迹有 40 至 80 个样本。」"""
    completed, output_path, ledger_path, _ = _invoke(
        tmp_path,
        _payload(sample_count=39),
    )
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_time_window_above_six(tmp_path: Path):
    """题面：「采样间隔恒定，末点不高于 `6.0 s`。」"""
    completed, output_path, ledger_path, _ = _invoke(
        tmp_path,
        _payload(duration_s=6.05),
    )
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_segment_count_not_three(tmp_path: Path):
    """题面：「`excitations` 保存 3 段已知激励。」"""
    completed, output_path, ledger_path, _ = _invoke(
        tmp_path,
        _payload(segment_count=4),
    )
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_topology_not_four_terminals_eight_nodes(tmp_path: Path):
    """题面：「每个端子恰好连接两根不同摆杆，每根摆杆恰好属于一个端子。」"""
    short, short_output, short_ledger, _ = _invoke(
        tmp_path,
        _payload(terminal_count=3),
    )
    long_payload = copy.deepcopy(_payload())
    extra_terminal = copy.deepcopy(long_payload["terminals"][0])
    extra_terminal["terminal_id"] = "T05"
    long_payload["terminals"].append(extra_terminal)
    long, long_output, long_ledger, _ = _invoke(
        tmp_path / "case_02",
        long_payload,
    )
    assert short.returncode == 3
    assert long.returncode == 3
    assert not short_output.exists()
    assert not long_output.exists()
    assert not short_ledger.exists()
    assert not long_ledger.exists()


def test_rejects_top_level_extra_key(tmp_path: Path):
    """题面：「试验包的根字段固定为：」"""
    payload = copy.deepcopy(_payload())
    payload["comment"] = "extra"
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_nested_sample_extra_key(tmp_path: Path):
    """题面：「采样对象只允许：」"""
    payload = copy.deepcopy(_payload())
    payload["terminals"][0]["traces"][0]["samples"][0]["unit"] = "m"
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_boolean_length_is_rejected(tmp_path: Path):
    """题面：「`length_m` 的取值范围是 `0.35` 至 `1.40`。」"""
    payload = copy.deepcopy(_payload())
    payload["pendulums"][0]["length_m"] = True
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_duplicate_json_key_is_rejected(tmp_path: Path):
    """题面：「摆阵包读不进来、出现重复键、字段集合对不上或值类型不对时，程序以状态码 `3` 退出。」"""
    raw = b'{"schema_version":1,"schema_version":1,"case_id":"x"}'
    completed, output_path, ledger_path = _raw_invoke(tmp_path, raw)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_nonzero_time_origin(tmp_path: Path):
    """题面：「`time_s` 的首点为 `0.0`」。"""
    payload = copy.deepcopy(_payload())
    for trace in payload["terminals"][0]["traces"]:
        for sample in trace["samples"]:
            sample["time_s"] += 0.01
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_nonuniform_time_axis(tmp_path: Path):
    """题面：「同一段号在所有端子中的时间轴必须一致。」"""
    payload = copy.deepcopy(_payload())
    payload["terminals"][0]["traces"][1]["samples"][8]["time_s"] += 0.001
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_duplicate_node_identifier(tmp_path: Path):
    """题面：「重复摆杆编号会终止整批求解。」"""
    payload = copy.deepcopy(_payload())
    payload["pendulums"][1]["node_id"] = payload["pendulums"][0]["node_id"]
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_wrong_schema_version(tmp_path: Path):
    """题面：「`schema_version` 必须是整数 `1`。」"""
    payload = copy.deepcopy(_payload())
    payload["schema_version"] = 2
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_invalid_input_preserves_previous_bytes(tmp_path: Path):
    """题面：「失败不得创建或改写结果文件。」"""
    payload = copy.deepcopy(_payload())
    payload["terminals"].pop()
    completed, output_path, ledger_path, _ = _invoke(
        tmp_path,
        payload,
        existing_output=b"old-result\n",
        existing_ledger=b"old-ledger\n",
    )
    assert completed.returncode == 3
    assert output_path.read_bytes() == b"old-result\n"
    assert ledger_path.read_bytes() == b"old-ledger\n"


def test_install_failure_preserves_directory_and_ledger(tmp_path: Path):
    """题面：「发布摆阵复原单时，先在目标目录暂存草稿，再一次性换到正式文件名；换名失败则旧复原单与旧流水字节不动。」"""
    payload = _payload()
    input_path = tmp_path / "request.json"
    output_path = tmp_path / "response.json"
    ledger_path = tmp_path / "events.jsonl"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    output_path.mkdir()
    completed = subprocess.run(
        [
            "python3",
            ENTRY,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--ledger",
            str(ledger_path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=os.environ.copy(),
    )
    assert completed.returncode == 3
    assert output_path.is_dir()
    assert not ledger_path.exists()


def test_malformed_ledger_preserves_output(tmp_path: Path):
    """题面：「失败不得改动账本。」"""
    completed, output_path, ledger_path, _ = _invoke(
        tmp_path,
        _payload(),
        existing_output=b"prior\n",
        existing_ledger=b"{broken",
    )
    assert completed.returncode == 3
    assert output_path.read_bytes() == b"prior\n"
    assert ledger_path.read_bytes() == b"{broken"


def test_result_uses_compact_sorted_json(solved_case: dict):
    """题面：「摆阵复原单的字节外观固定：JSON 不带插入空格，键名按升序落盘，文件收口只有一个换行。」"""
    raw = solved_case["raw"]
    assert solved_case["completed"].returncode == 0
    assert b", " not in raw
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")
    assert list(json.loads(raw)) == sorted(json.loads(raw))


def test_success_path_is_silent(solved_case: dict):
    """题面：「正常扫频结束后，终端的标准输出和标准错误都保持空。」"""
    assert solved_case["completed"].returncode == 0
    assert solved_case["completed"].stdout == ""
    assert solved_case["completed"].stderr == ""


def test_rejects_missing_segment_trace(tmp_path: Path):
    """题面：「每个端子必须恰好包含一段对应每个 `segment_id` 的轨迹。」"""
    payload = copy.deepcopy(_payload())
    payload["terminals"][0]["traces"].pop()
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()


def test_rejects_mismatched_trace_segment_id(tmp_path: Path):
    """题面：「同一输入内的段号集合必须与全部端子轨迹逐段一致。」"""
    payload = copy.deepcopy(_payload())
    payload["terminals"][1]["traces"][0]["segment_id"] = "S99"
    completed, output_path, ledger_path, _ = _invoke(tmp_path, payload)
    assert completed.returncode == 3
    assert not output_path.exists()
    assert not ledger_path.exists()
