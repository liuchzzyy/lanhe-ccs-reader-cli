"""Reverse-engineered parser for LANHE/LAND CCS binary files."""

from __future__ import annotations

import json
import math
import struct
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

PAGE_SIZE = 128
PAGE_HEADER_SIZE = 8
RECORD_SIZE = 20
RECORDS_PER_PAGE = 6
DATA_PAGE_KIND = 0x03
CONTROL_PAGE_KIND = 0x02

UNIX_MS_MIN = 946_684_800_000
UNIX_MS_MAX = 4_102_444_800_000


@dataclass(frozen=True)
class ModeInfo:
    """Known export naming for a CCS page state."""

    name: str
    mark1: str
    mark2: int


MODE_BY_CODE: dict[int, ModeInfo] = {
    0x00: ModeInfo("FINISH", "F", 0),
    0x01: ModeInfo("REST", "R", 1),
    0x24: ModeInfo("C_CRATE", "C", 36),
    0x44: ModeInfo("D_CRATE", "D", 68),
}


@dataclass(frozen=True)
class ControlStep:
    """Decoded control-page metadata for a measurement step."""

    mode: ModeInfo
    cycle: int
    process_step: int
    test_start_ms: int
    wall_start_ms: int | None


@dataclass
class CCSMetadata:
    """Header and parse metadata for a CCS file."""

    source_path: str
    file_name: str
    group_name: str
    channel_index: int | None
    channel_number: str
    test_name: str
    process_name: str
    process_description: str
    software_version: str
    serial_number: str
    nominal_specific_capacity_mAh_g: float | None
    active_material_mass_g: float | None
    header_start_time: datetime | None
    log_start_time: datetime | None
    finish_time: datetime | None
    data_start_offset: int
    page_count: int

    @property
    def active_material_text(self) -> str:
        """Return an official-export-like active material description."""
        parts: list[str] = []
        if self.nominal_specific_capacity_mAh_g is not None:
            parts.append(f"Nominal specific capacity: {self.nominal_specific_capacity_mAh_g:g} mAh/g")
        if self.active_material_mass_g is not None:
            parts.append(f"Active material: {self.active_material_mass_g * 1000:g} mg")
        return " ".join(parts)

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable metadata dictionary."""
        data = asdict(self)
        for key in ("header_start_time", "log_start_time", "finish_time"):
            value = data[key]
            data[key] = None if value is None else value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return data


@dataclass
class CCSParseResult:
    """Parsed CCS data and export-style tables."""

    metadata: CCSMetadata
    records: pd.DataFrame
    steps: pd.DataFrame
    cycles: pd.DataFrame
    logs: pd.DataFrame

    def summary_dict(self) -> dict[str, Any]:
        """Return a compact JSON-serializable summary."""
        return {
            "metadata": self.metadata.to_json_dict(),
            "record_count": int(len(self.records)),
            "step_count": int(len(self.steps)),
            "cycle_count": int(len(self.cycles)),
            "records": {
                "first": _row_to_json(self.records.iloc[0]) if not self.records.empty else None,
                "last": _row_to_json(self.records.iloc[-1]) if not self.records.empty else None,
            },
            "steps": [_row_to_json(row) for _, row in self.steps.iterrows()],
            "cycles": [_row_to_json(row) for _, row in self.cycles.iterrows()],
        }

    def write_summary_json(self, path: str | Path) -> None:
        """Write a parse summary JSON file."""
        Path(path).write_text(json.dumps(self.summary_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


class CCSParser:
    """Parser for LANHE/LAND CCS files."""

    def __init__(self, path: str | Path, *, timezone: str | None = None) -> None:
        self.path = Path(path)
        self.timezone = timezone

    def parse(self) -> CCSParseResult:
        """Parse the CCS file into records, summaries, and metadata."""
        data = self.path.read_bytes()
        data_start = self._detect_data_start(data)
        page_count = (len(data) - data_start) // PAGE_SIZE

        header_start_ms = _read_unix_ms(data, 0xA8)
        log_start_ms = _read_unix_ms(data, 0xA38)
        if log_start_ms is None:
            log_start_ms = header_start_ms
        if log_start_ms is None:
            raise ValueError(f"Could not find a valid log start timestamp in {self.path}")

        metadata = self._parse_metadata(data, data_start, page_count, header_start_ms, log_start_ms)
        control_steps = self._parse_control_steps(data, log_start_ms)
        records = self._parse_records(data, data_start, log_start_ms, control_steps)
        if records.empty:
            raise ValueError(f"No measurement records found in {self.path}")

        finish_ms = log_start_ms + int(records["TestTime_ms"].iloc[-1])
        metadata.finish_time = _datetime_from_unix_ms(finish_ms, self.timezone)
        records = self._add_derived_record_columns(records, metadata, control_steps)
        steps = self._build_steps(records, metadata)
        cycles = self._build_cycles(steps, metadata)
        logs = self._build_logs(metadata)
        return CCSParseResult(metadata=metadata, records=records, steps=steps, cycles=cycles, logs=logs)

    def _detect_data_start(self, data: bytes) -> int:
        """Find the page-aligned measurement suffix."""
        page_count = len(data) // PAGE_SIZE
        suffix_valid = [False] * (page_count + 1)
        suffix_data_pages = [0] * (page_count + 1)
        suffix_valid[page_count] = len(data) % PAGE_SIZE == 0

        for page_index in range(page_count - 1, -1, -1):
            offset = page_index * PAGE_SIZE
            header = _page_header(data, offset)
            if header is None:
                continue
            kind = header[0] & 0xFF
            valid = (header[0] >> 8) & 0xFF
            is_data = kind == DATA_PAGE_KIND and 1 <= valid <= RECORDS_PER_PAGE
            is_control = kind == CONTROL_PAGE_KIND
            if (is_data or is_control) and suffix_valid[page_index + 1]:
                suffix_valid[page_index] = True
                suffix_data_pages[page_index] = suffix_data_pages[page_index + 1] + int(is_data)

        max_scan_pages = min(page_count, math.ceil(min(len(data), 1024 * 1024) / PAGE_SIZE))
        best: tuple[int, int] | None = None
        for page_index in range(max_scan_pages):
            if not suffix_valid[page_index] or suffix_data_pages[page_index] == 0:
                continue
            offset = page_index * PAGE_SIZE
            header = _page_header(data, offset)
            if header is None:
                continue
            kind = header[0] & 0xFF
            valid = (header[0] >> 8) & 0xFF
            if kind != DATA_PAGE_KIND or not 1 <= valid <= RECORDS_PER_PAGE:
                continue
            candidate = (suffix_data_pages[page_index], offset)
            if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1]):
                best = candidate

        if best is None:
            raise ValueError(f"Could not detect CCS data pages in {self.path}")
        return best[1]

    def _parse_metadata(
        self,
        data: bytes,
        data_start: int,
        page_count: int,
        header_start_ms: int | None,
        log_start_ms: int,
    ) -> CCSMetadata:
        group_name = _read_c_string(data, 0x10, 64) or "DefaultGroup"
        group_ordinal = _read_group_ordinal(data)
        channel_index = _read_channel_index(data)
        channel_number = f"{group_name}_{group_ordinal:02}_{channel_index}" if channel_index is not None else group_name

        nominal_capacity = _read_float(data, 0x324)
        if nominal_capacity is not None and not 0 < nominal_capacity < 100_000:
            nominal_capacity = None

        active_mass_g = _read_float(data, 0x328)
        if active_mass_g is not None and not 0 < active_mass_g < 100:
            active_mass_g = None

        return CCSMetadata(
            source_path=str(self.path),
            file_name=self.path.name,
            group_name=group_name,
            channel_index=channel_index,
            channel_number=channel_number,
            test_name=_read_c_string(data, 0xB0, 320),
            process_name=_read_c_string(data, 0x734, 64),
            process_description=_read_c_string(data, 0x774, 128),
            software_version=_read_c_string(data, 0x2D0, 32),
            serial_number=_read_c_string(data, 0x360, 32),
            nominal_specific_capacity_mAh_g=nominal_capacity,
            active_material_mass_g=active_mass_g,
            header_start_time=_datetime_from_unix_ms(header_start_ms, self.timezone),
            log_start_time=_datetime_from_unix_ms(log_start_ms, self.timezone),
            finish_time=None,
            data_start_offset=data_start,
            page_count=page_count,
        )

    def _parse_control_steps(self, data: bytes, log_start_ms: int) -> dict[int, ControlStep]:
        """Map data-page state words to their decoded control-page metadata."""
        raw_steps: list[tuple[int, ModeInfo, int, int, int, int | None]] = []
        for offset in range(0, len(data) - PAGE_HEADER_SIZE, PAGE_SIZE):
            header = _page_header(data, offset)
            if header is None or (header[0] & 0xFF) != CONTROL_PAGE_KIND:
                continue
            if offset + 64 > len(data):
                continue
            mode_code = struct.unpack_from("<I", data, offset + 8)[0]
            cycle_index = struct.unpack_from("<I", data, offset + 12)[0]
            process_step_index = struct.unpack_from("<I", data, offset + 16)[0]
            test_start_ms = struct.unpack_from("<I", data, offset + 24)[0]
            wall_counter = struct.unpack_from("<I", data, offset + 56)[0]
            raw_steps.append(
                (
                    offset,
                    _mode_for_code(mode_code),
                    int(cycle_index) + 1,
                    int(process_step_index) + 1,
                    int(test_start_ms),
                    int(wall_counter),
                )
            )

        if not raw_steps:
            return {}

        first_counter = next((item[5] for item in raw_steps if item[5] is not None), None)
        steps: dict[int, ControlStep] = {}
        for offset, mode, cycle, process_step, test_start_ms, wall_counter in raw_steps:
            wall_start_ms: int | None = None
            if first_counter is not None and wall_counter is not None:
                delta = (wall_counter - first_counter) & 0xFFFFFFFF
                if delta > 0x7FFFFFFF:
                    delta -= 0x100000000
                wall_start_ms = log_start_ms + delta
            steps[offset] = ControlStep(
                mode=mode,
                cycle=cycle,
                process_step=process_step,
                test_start_ms=test_start_ms,
                wall_start_ms=wall_start_ms,
            )
        return steps

    def _parse_records(
        self,
        data: bytes,
        data_start: int,
        log_start_ms: int,
        control_steps: dict[int, ControlStep],
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        step = 0
        previous_state: int | None = None
        step_time_ms = 0
        step_capacity_uah = 0.0
        step_energy_uwh = 0.0
        step_info = ControlStep(_mode_for_code(0), 1, 1, 0, log_start_ms)

        for page_offset in range(data_start, len(data), PAGE_SIZE):
            header0, state = struct.unpack_from("<II", data, page_offset)
            kind = header0 & 0xFF
            valid = (header0 >> 8) & 0xFF
            if kind != DATA_PAGE_KIND or not 1 <= valid <= RECORDS_PER_PAGE:
                continue

            if state != previous_state:
                step += 1
                previous_state = state
                step_time_ms = 0
                step_capacity_uah = 0.0
                step_energy_uwh = 0.0
                fallback_mode = _mode_for_code(state)
                step_info = control_steps.get(
                    state,
                    ControlStep(
                        mode=fallback_mode,
                        cycle=1,
                        process_step=step,
                        test_start_ms=0,
                        wall_start_ms=log_start_ms,
                    ),
                )

            for index_in_page in range(valid):
                record_offset = page_offset + PAGE_HEADER_SIZE + index_in_page * RECORD_SIZE
                delta_ms, voltage_v, current_a, capacity_ah, energy_wh = struct.unpack_from(
                    "<Iffff",
                    data,
                    record_offset,
                )

                step_time_ms += int(delta_ms)
                test_time_ms = step_info.test_start_ms + step_time_ms
                step_capacity_uah += abs(float(capacity_ah)) * 1_000_000.0
                step_energy_uwh += abs(float(energy_wh)) * 1_000_000.0

                current_uA = _f32(float(current_a) * 1_000_000.0)
                capacity_uAh = _f32(step_capacity_uah)
                energy_uWh = _f32(step_energy_uwh)
                wall_start_ms = step_info.wall_start_ms if step_info.wall_start_ms is not None else log_start_ms
                sys_time = _datetime_from_unix_ms(wall_start_ms + step_time_ms, self.timezone)

                rows.append(
                    {
                        "Cycle": step_info.cycle,
                        "Step": step,
                        "Record": len(rows) + 1,
                        "WorkMode": step_info.mode.name,
                        "StateWord": state,
                        "StepInProcess": f"1-{step_info.process_step}",
                        "StepStartTestTime_ms": step_info.test_start_ms,
                        "StepWallStart_ms": wall_start_ms,
                        "StepTime_ms": step_time_ms,
                        "TestTime_ms": test_time_ms,
                        "Voltage/V": float(voltage_v),
                        "Current/uA": current_uA,
                        "Capacity/uAh": capacity_uAh,
                        "Energy/uWh": energy_uWh,
                        "Power/uW": _f32(float(voltage_v) * float(current_a) * 1_000_000.0),
                        "Temperature/C": "0",
                        "Humidity/%": "0",
                        "SysTime": _format_datetime(sys_time),
                        "PageOffset": page_offset,
                        "RecordOffset": record_offset,
                        "PageValidRecords": valid,
                    }
                )

        return pd.DataFrame(rows)

    def _add_derived_record_columns(
        self,
        records: pd.DataFrame,
        metadata: CCSMetadata,
        control_steps: dict[int, ControlStep],
    ) -> pd.DataFrame:
        records = records.copy()
        step_starts = records.groupby("Step", sort=False)["StepStartTestTime_ms"].first()
        step_max_times = records.groupby("Step", sort=False)["StepTime_ms"].max()
        step_durations: dict[int, int] = {}
        ordered_steps = list(step_starts.index)
        for index, step in enumerate(ordered_steps):
            duration_ms = int(step_max_times.loc[step])
            if index + 1 < len(ordered_steps):
                next_step = ordered_steps[index + 1]
                next_start = int(step_starts.loc[next_step])
                current_start = int(step_starts.loc[step])
                if next_start > current_start:
                    duration_ms = next_start - current_start
            step_durations[int(step)] = duration_ms
        records["StepDuration_ms"] = records["Step"].map(step_durations).astype("int64")
        records["StepDuration"] = records["StepDuration_ms"].map(format_duration_ms)
        records["StepTime"] = records["StepTime_ms"].map(format_duration_ms)
        records["TestTime"] = records["TestTime_ms"].map(format_duration_ms)

        if metadata.active_material_mass_g:
            divisor = metadata.active_material_mass_g * 1000.0
            records["SpeCap/mAh/g"] = records["Capacity/uAh"].map(lambda value: _f32(float(value) / divisor))
            records["SpeEnergy/mWh/g"] = records["Energy/uWh"].map(lambda value: _f32(float(value) / divisor))
        else:
            records["SpeCap/mAh/g"] = 0.0
            records["SpeEnergy/mWh/g"] = 0.0

        records["dQdV/uAh/V"] = 0.0
        records["dVdQ/V/uAh"] = 0.0
        for _, group in records.groupby("Step", sort=False):
            idx = list(group.index)
            voltages = group["Voltage/V"].to_list()
            capacities = group["Capacity/uAh"].to_list()
            for pos, row_idx in enumerate(idx[:-1]):
                dq = float(capacities[pos + 1]) - float(capacities[pos])
                dv = float(voltages[pos + 1]) - float(voltages[pos])
                if dq != 0:
                    records.at[row_idx, "dVdQ/V/uAh"] = _f32(dv / dq)
                if dv != 0:
                    records.at[row_idx, "dQdV/uAh/V"] = _f32(dq / dv)

        records["Mark1"] = records["StateWord"].map(
            lambda state: control_steps.get(
                int(state),
                ControlStep(_mode_for_code(int(state)), 1, 1, 0, None),
            ).mode.mark1
        )
        records["Mark2"] = "0"
        for _, group in records.groupby("Step", sort=False):
            idx = list(group.index)
            if not idx:
                continue
            state_word = int(records.at[idx[0], "StateWord"])
            mode = control_steps.get(state_word, ControlStep(_mode_for_code(state_word), 1, 1, 0, None)).mode
            normal = mode.mark2
            for row_idx in idx[1:]:
                records.at[row_idx, "Mark2"] = str(normal)
            records.at[idx[-1], "Mark2"] = str(normal + 128)
            final_capacity = float(records.at[idx[-1], "Capacity/uAh"])
            if final_capacity > 0:
                half_capacity = final_capacity / 2.0
                half_rows = group[group["Capacity/uAh"].astype(float) >= half_capacity]
                if not half_rows.empty:
                    half_idx = half_rows.index[0]
                    if half_idx != idx[-1]:
                        records.at[half_idx, "Mark2"] = "257" if mode.name.startswith("D") else "256"

        records["BatteryCode"] = ""
        records["DataFile"] = _normalized_path(self.path)
        records["TestName"] = metadata.test_name
        records["ProcessName"] = metadata.process_name
        records["Thicknessmm"] = ""
        records["ThicknessPressureg"] = ""
        records["ThicknessTempC"] = ""
        records["ChannelNumber"] = metadata.channel_number

        return records[
            [
                "Cycle",
                "Step",
                "Record",
                "WorkMode",
                "StepInProcess",
                "StepDuration",
                "StepTime",
                "TestTime",
                "Voltage/V",
                "Current/uA",
                "Capacity/uAh",
                "SpeCap/mAh/g",
                "Energy/uWh",
                "SpeEnergy/mWh/g",
                "Power/uW",
                "dQdV/uAh/V",
                "dVdQ/V/uAh",
                "Temperature/C",
                "Humidity/%",
                "SysTime",
                "Mark1",
                "Mark2",
                "BatteryCode",
                "DataFile",
                "TestName",
                "ProcessName",
                "Thicknessmm",
                "ThicknessPressureg",
                "ThicknessTempC",
                "ChannelNumber",
                "StepTime_ms",
                "TestTime_ms",
                "StepDuration_ms",
                "StepStartTestTime_ms",
                "StepWallStart_ms",
                "StateWord",
                "PageOffset",
                "RecordOffset",
            ]
        ]

    def _build_steps(self, records: pd.DataFrame, metadata: CCSMetadata) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        total_capacity_by_mode: dict[str, float] = {}
        total_energy_by_mode: dict[str, float] = {}

        for step, group in records.groupby("Step", sort=False):
            first = group.iloc[0]
            last = group.iloc[-1]
            mode = str(first["WorkMode"])
            capacity = float(last["Capacity/uAh"])
            energy = float(last["Energy/uWh"])
            total_capacity_by_mode[mode] = total_capacity_by_mode.get(mode, 0.0) + capacity
            total_energy_by_mode[mode] = total_energy_by_mode.get(mode, 0.0) + energy
            mid_voltage = _mid_voltage(group)

            if metadata.active_material_mass_g:
                divisor = metadata.active_material_mass_g * 1000.0
                spe_cap = _f32(capacity / divisor)
                spe_energy = _f32(energy / divisor)
                spe_cap_total = _f32(total_capacity_by_mode[mode] / divisor)
                spe_energy_total = _f32(total_energy_by_mode[mode] / divisor)
            else:
                spe_cap = 0.0
                spe_energy = 0.0
                spe_cap_total = 0.0
                spe_energy_total = 0.0

            rows.append(
                {
                    "Cycle": int(first["Cycle"]),
                    "Step": int(step),
                    "WorkMode": mode,
                    "StepDuration": str(last["StepDuration"]),
                    "StepInProcess": str(first["StepInProcess"]),
                    "Capacity/uAh": _f32(capacity),
                    "Capacity(Total)/uAh": _f32(total_capacity_by_mode[mode]),
                    "SpeCap/mAh/g": spe_cap,
                    "SpeCap(Total)/mAh/g": spe_cap_total,
                    "Energy/uWh": _f32(energy),
                    "Energy(Total)/uWh": _f32(total_energy_by_mode[mode]),
                    "SpeEnergy/mWh/g": spe_energy,
                    "SpeEnergy(Total)/mWh/g": spe_energy_total,
                    "DCIR/KOhm": 0,
                    "StartVolt/V": float(first["Voltage/V"]),
                    "EndVolt/V": float(last["Voltage/V"]),
                    "MidVoltD/V": mid_voltage,
                    "StartTemperature/C": 0,
                    "EndTemperature/C": 0,
                    "DataFile": _normalized_path(self.path),
                    "ChannelNumber": metadata.channel_number,
                    "Capacitance": 0,
                }
            )

        return pd.DataFrame(rows)

    def _build_cycles(self, steps: pd.DataFrame, metadata: CCSMetadata) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for cycle, cycle_steps in steps.groupby("Cycle", sort=False):
            cap_c = _sum_mode(cycle_steps, charge=True, column="Capacity/uAh")
            cap_d = _sum_mode(cycle_steps, charge=False, column="Capacity/uAh")
            energy_c = _sum_mode(cycle_steps, charge=True, column="Energy/uWh")
            energy_d = _sum_mode(cycle_steps, charge=False, column="Energy/uWh")

            spe_cap_c = _specific(cap_c, metadata.active_material_mass_g)
            spe_cap_d = _specific(cap_d, metadata.active_material_mass_g)
            spe_energy_c = _specific(energy_c, metadata.active_material_mass_g)
            spe_energy_d = _specific(energy_d, metadata.active_material_mass_g)
            discharge_steps = cycle_steps[cycle_steps["WorkMode"].astype(str).str.startswith("D")]

            if not discharge_steps.empty:
                discharge_duration = _sum_durations(discharge_steps["StepDuration"].to_list())
                mid_volt_d = float(discharge_steps.iloc[-1]["MidVoltD/V"])
                end_volt_d = float(discharge_steps.iloc[-1]["EndVolt/V"])
            else:
                discharge_duration = "0 00:00:00.000"
                mid_volt_d = 0.0
                end_volt_d = 0.0

            retention_d = 0.0
            if metadata.nominal_specific_capacity_mAh_g and spe_cap_d:
                retention_d = _f32(spe_cap_d / metadata.nominal_specific_capacity_mAh_g * 100.0)

            rows.append(
                {
                    "Cycle": int(cycle),
                    "CapC/uAh": _f32(cap_c),
                    "CapD/uAh": _f32(cap_d),
                    "SpeCapC/mAh/g": _f32(spe_cap_c),
                    "SpeCapD/mAh/g": _f32(spe_cap_d),
                    "CoulombEfficiency/%": _f32((cap_d / cap_c) * 100.0) if cap_c else 0,
                    "EnergyC/uWh": _f32(energy_c),
                    "EnergyD/uWh": _f32(energy_d),
                    "SpeEnergyC/mWh/g": _f32(spe_energy_c),
                    "SpeEnergyD/mWh/g": _f32(spe_energy_d),
                    "EnergyEfficiency/%": _f32((energy_d / energy_c) * 100.0) if energy_c else 0,
                    "CC-Cap/uAh": _f32(cap_c),
                    "CC-Per/%": 100 if cap_c else 0,
                    "DC-Cap/uAh": _f32(cap_d),
                    "DC-Per/%": 100 if cap_d else 0,
                    "PlatCapD/uAh": 0,
                    "PlatSpeCapD/mAh/g": 0,
                    "PlatPerD/%": 0,
                    "PlatTimeD": "0 00:00:00.000",
                    "CapacitanceC/uF": 0,
                    "CapacitanceD/uF": 0,
                    "DCIR/KOhm": 0,
                    "MidVoltC/V": 0,
                    "EndVoltC/V": 0,
                    "MidVoltD/V": mid_volt_d,
                    "EndVoltD/V": end_volt_d,
                    "RetentionC/%": 0,
                    "RetentionD/%": retention_d,
                    "DurationC": "0 00:00:00.000",
                    "DurationD": discharge_duration,
                    "DataFile": _normalized_path(self.path),
                    "ChannelNumber": metadata.channel_number,
                    "AvgVoltC/V": _f32(energy_c / cap_c) if cap_c else 0,
                    "AvgVoltD/V": energy_d / cap_d if cap_d else 0,
                }
            )

        return pd.DataFrame(rows)

    def _build_logs(self, metadata: CCSMetadata) -> pd.DataFrame:
        rows = []
        if metadata.log_start_time is not None:
            rows.append(
                {
                    "Test name": metadata.test_name,
                    "Dev SN": metadata.serial_number,
                    "Chl Num": metadata.channel_index,
                    "Log Num": 1,
                    "Cycle ID": 1,
                    "SysTime": _format_datetime(metadata.log_start_time),
                    "Log Type": "Test start",
                    "Log Details": "",
                }
            )
        if metadata.finish_time is not None:
            rows.append(
                {
                    "Test name": metadata.test_name,
                    "Dev SN": metadata.serial_number,
                    "Chl Num": metadata.channel_index,
                    "Log Num": 2,
                    "Cycle ID": 1,
                    "SysTime": _format_datetime(metadata.finish_time),
                    "Log Type": "Finish",
                    "Log Details": "",
                }
            )
        return pd.DataFrame(rows)


class CCSReader:
    """Small reader wrapper around :class:`CCSParser`."""

    def __init__(self, path: str | Path, *, timezone: str | None = None) -> None:
        self.path = Path(path)
        self.timezone = timezone

    def read(self) -> CCSParseResult:
        """Read the configured CCS file."""
        return read_ccs(self.path, timezone=self.timezone)

    def to_dataframe(self) -> pd.DataFrame:
        """Return parsed measurement records as a DataFrame."""
        return self.read().records


def read_ccs(path: str | Path, *, timezone: str | None = None) -> CCSParseResult:
    """Parse a LANHE/LAND CCS file."""
    return CCSParser(path, timezone=timezone).parse()


def format_duration_ms(milliseconds: int | float) -> str:
    """Format milliseconds as the vendor export duration string."""
    total_ms = int(round(float(milliseconds)))
    days, remainder = divmod(total_ms, 86_400_000)
    hours, remainder = divmod(remainder, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, ms = divmod(remainder, 1_000)
    return f"{days} {hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def _read_c_string(data: bytes, offset: int, length: int) -> str:
    if offset >= len(data):
        return ""
    raw = data[offset : min(len(data), offset + length)]
    raw = raw.split(b"\x00", 1)[0]
    return raw.decode("gb18030", errors="replace").strip()


def _read_float(data: bytes, offset: int) -> float | None:
    if offset + 4 > len(data):
        return None
    return struct.unpack_from("<f", data, offset)[0]


def _read_unix_ms(data: bytes, offset: int) -> int | None:
    if offset + 8 > len(data):
        return None
    value = struct.unpack_from("<Q", data, offset)[0]
    if UNIX_MS_MIN <= value <= UNIX_MS_MAX:
        return int(value)
    return None


def _datetime_from_unix_ms(value: int | None, timezone: str | None = None) -> datetime | None:
    if value is None:
        return None
    if timezone:
        return datetime.fromtimestamp(value / 1000.0, tz=ZoneInfo(timezone)).replace(tzinfo=None)
    return datetime.fromtimestamp(value / 1000.0)


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _page_header(data: bytes, offset: int) -> tuple[int, int] | None:
    if offset + PAGE_HEADER_SIZE > len(data):
        return None
    return struct.unpack_from("<II", data, offset)


def _read_group_ordinal(data: bytes) -> int:
    if len(data) >= 0x54:
        candidate = struct.unpack_from("<I", data, 0x50)[0]
        if 0 <= candidate < 100:
            return int(candidate)
    return 0


def _read_channel_index(data: bytes) -> int | None:
    if len(data) >= 0x09:
        candidate = data[0x08] + 1
        if 1 <= candidate <= 255:
            return int(candidate)
    if len(data) >= 0x378:
        candidate = data[0x376]
        if 1 <= candidate <= 255:
            return int(candidate)
    return None


def _mode_for_code(code: int) -> ModeInfo:
    if code in MODE_BY_CODE:
        return MODE_BY_CODE[code]
    if 1 <= code <= 255:
        return ModeInfo(f"UNKNOWN_0x{code:02X}", "?", int(code))
    return ModeInfo(f"UNKNOWN_0x{code:04X}", "?", 0)


def _mode_for_state(state: int) -> ModeInfo:
    if state in MODE_BY_CODE:
        return MODE_BY_CODE[state]
    if 1 <= state <= 255:
        return ModeInfo(f"UNKNOWN_0x{state:02X}", "?", int(state))
    return ModeInfo(f"UNKNOWN_0x{state:04X}", "?", 0)


def _f32(value: float) -> float:
    if not math.isfinite(value):
        return value
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _normalized_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def _row_to_json(row: pd.Series) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in row.to_dict().items():
        if pd.isna(value):
            data[key] = None
        elif hasattr(value, "item"):
            data[key] = value.item()
        else:
            data[key] = value
    return data


def _specific(value: float, mass_g: float | None) -> float:
    if not mass_g:
        return 0.0
    return float(value) / (mass_g * 1000.0)


def _mid_voltage(group: pd.DataFrame) -> float:
    last = group.iloc[-1]
    capacity = float(last["Capacity/uAh"])
    if capacity > 0:
        target = capacity / 2.0
        candidates = group[group["Capacity/uAh"].astype(float) >= target]
        if not candidates.empty:
            return float(candidates.iloc[0]["Voltage/V"])
    duration = int(last["StepTime_ms"])
    target_time = duration / 2.0
    candidates = group[group["StepTime_ms"].astype(float) >= target_time]
    if not candidates.empty:
        return float(candidates.iloc[0]["Voltage/V"])
    return float(last["Voltage/V"])


def _sum_mode(steps: pd.DataFrame, *, charge: bool, column: str) -> float:
    if steps.empty:
        return 0.0
    prefix = "C" if charge else "D"
    selected = steps[steps["WorkMode"].astype(str).str.startswith(prefix)]
    if selected.empty:
        return 0.0
    return float(selected[column].astype(float).sum())


def _sum_durations(values: list[str]) -> str:
    total = sum(_parse_duration_ms(value) for value in values)
    return format_duration_ms(total)


def _parse_duration_ms(value: str) -> int:
    days_text, time_text = str(value).split(" ", 1)
    hours_text, minutes_text, seconds_text = time_text.split(":")
    seconds, milliseconds = seconds_text.split(".")
    return (
        int(days_text) * 86_400_000
        + int(hours_text) * 3_600_000
        + int(minutes_text) * 60_000
        + int(seconds) * 1_000
        + int(milliseconds)
    )
