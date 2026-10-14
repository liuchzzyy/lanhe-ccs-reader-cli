"""Fail closed on unimplemented CCS formats; preserve 64-bit control times."""

import struct
from pathlib import Path

import pytest

from ccs_reader import read_ccs
from ccs_reader.parser import CCSParser

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "AA.ccs"


@pytest.mark.parametrize(
    ("header", "magic", "message"),
    [
        (0x02000000, 0x11223344, "magic"),
        (0x02000000, 0x95279529, "version"),
        (0x01000000, 0x95279528, "header"),
    ],
)
def test_unsupported_ccs_file_is_not_silently_parsed(tmp_path: Path, header: int, magic: int, message: str) -> None:
    data = bytearray(SAMPLE.read_bytes())
    struct.pack_into("<II", data, 0, header, magic)
    target = tmp_path / "mutated.ccs"
    target.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        read_ccs(target)


def test_short_file_is_rejected_before_page_scanning(tmp_path: Path) -> None:
    target = tmp_path / "short.ccs"
    target.write_bytes(SAMPLE.read_bytes()[:0x8EF])
    with pytest.raises(ValueError, match="short"):
        read_ccs(target)


def test_control_page_times_are_64_bit() -> None:
    data = bytearray(256)
    log_start_ms = 1_700_000_000_000
    first_counter = 1_600_000_000_000
    struct.pack_into("<I", data, 0, 2)
    struct.pack_into("<Q", data, 24, 2**32 + 100)
    struct.pack_into("<Q", data, 56, first_counter)
    struct.pack_into("<I", data, 128, 2)
    struct.pack_into("<Q", data, 128 + 24, 2**32 + 200)
    struct.pack_into("<Q", data, 128 + 56, first_counter + 2**32 + 250)
    steps = CCSParser("unused.ccs")._parse_control_steps(bytes(data), log_start_ms)
    assert steps[0].test_start_ms == 2**32 + 100
    assert steps[128].test_start_ms == 2**32 + 200
    assert steps[128].wall_start_ms == log_start_ms + 2**32 + 250
