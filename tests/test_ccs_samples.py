from __future__ import annotations

from pathlib import Path

import pytest

from ccs_reader import CCSReader, read_ccs
from ccs_reader.validate import validate_against_xlsx

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

AA_CCS = SAMPLES / "AA.ccs"
AA_XLSX = SAMPLES / "AA.xlsx"
BB_CCS = SAMPLES / "BB.ccs"
BB_XLSX = SAMPLES / "BB.xlsx"
CC_CCS = SAMPLES / "CC.ccs"


@pytest.mark.parametrize(
    ("ccs_path", "expected"),
    [
        (
            AA_CCS,
            {
                "records": 85756,
                "data_start_offset": 0xA80,
                "page_count": 14296,
                "test_name": "LC-01C-Zn-1 M Zn +0.2M Mn -alpha MnO2-801010-5.9mg-1st discharge20#-06-2022",
                "process_name": "LC-C@10",
                "serial_number": "M340A320302052",
                "channel_number": "DefaultGroup_00_4",
                "first_systime": "2023-03-16 01:10:55.203",
                "last_systime": "2023-03-16 07:54:21.971",
                "cycles": [1],
                "step_in_process": ["1-1", "1-2"],
            },
        ),
        (
            BB_CCS,
            {
                "records": 59625,
                "data_start_offset": 0xB80,
                "page_count": 9951,
                "test_name": "LC-01C-Zn-1MZnAC2+02MMn-EMD-8.9mg",
                "process_name": "Zn-MnO2-0.1C",
                "serial_number": "M340A321302060",
                "channel_number": "DefaultGroup_01_5",
                "first_systime": "2025-10-11 12:53:57.092",
                "last_systime": "2025-10-12 05:27:54.109",
                "cycles": [1, 2, 3],
                "step_in_process": ["1-1", "1-2", "1-3"],
            },
        ),
    ],
)
def test_ccs_sample_metadata_and_timing(ccs_path: Path, expected: dict[str, object]) -> None:
    result = read_ccs(ccs_path)

    assert len(result.records) == expected["records"]
    assert result.metadata.data_start_offset == expected["data_start_offset"]
    assert result.metadata.page_count == expected["page_count"]
    assert result.metadata.test_name == expected["test_name"]
    assert result.metadata.process_name == expected["process_name"]
    assert result.metadata.software_version == "4.3.3.04161"
    assert result.metadata.serial_number == expected["serial_number"]
    assert result.metadata.channel_number == expected["channel_number"]
    assert result.records.iloc[0]["SysTime"] == expected["first_systime"]
    assert result.records.iloc[-1]["SysTime"] == expected["last_systime"]
    assert sorted(result.records["Cycle"].drop_duplicates().to_list()) == expected["cycles"]
    assert sorted(result.records["StepInProcess"].drop_duplicates().to_list()) == expected["step_in_process"]


@pytest.mark.parametrize(("ccs_path", "xlsx_path"), [(AA_CCS, AA_XLSX), (BB_CCS, BB_XLSX)])
def test_ccs_samples_match_vendor_xlsx_strict_columns(ccs_path: Path, xlsx_path: Path) -> None:
    report = validate_against_xlsx(ccs_path, xlsx_path)

    assert report["ok"], report
    assert all(item["mismatches"] == 0 for item in report["identity"].values())
    assert all(item["first_failure"] is None for item in report["columns"].values())


def test_reader_wrapper_returns_records() -> None:
    result = CCSReader(AA_CCS).read()

    assert len(result.records) == 85756
    assert result.records.iloc[0]["Record"] == 1


def test_large_ccs_sample_parses_without_vendor_xlsx() -> None:
    result = read_ccs(CC_CCS)

    assert len(result.records) == 116448
    assert result.metadata.file_name == "CC.ccs"
    assert result.metadata.data_start_offset == 0xB00
    assert result.metadata.page_count == 19413
    assert result.metadata.test_name == "LC-01C-Zn-1M Zn-alpha MnO2-80992-6.2mg-1st discharge-1st charge"
    assert result.metadata.process_name == "LC-MnO2-C@10"
    assert result.metadata.serial_number == "M340A320302052"
    assert result.metadata.channel_number == "DefaultGroup_00_7"
    assert result.records.iloc[0]["SysTime"] == "2023-04-28 15:56:43.937"
    assert result.records.iloc[-1]["SysTime"] == "2023-04-29 00:22:28.851"
    assert sorted(result.records["Cycle"].drop_duplicates().to_list()) == [1]
    assert sorted(result.records["StepInProcess"].drop_duplicates().to_list()) == ["1-1", "1-2", "1-3"]
    assert sorted(set(result.records["WorkMode"])) == ["C_CRATE", "D_CRATE", "REST"]
    assert len(result.steps) == 3
    assert len(result.cycles) == 1
    assert len(result.logs) == 2
