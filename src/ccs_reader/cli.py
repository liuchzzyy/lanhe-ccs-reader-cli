"""Command-line interface for LANHE CCS parsing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .parser import read_ccs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse a LANHE/LAND .ccs file.")
    parser.add_argument("ccs_file", type=Path, help="Path to the .ccs file.")
    parser.add_argument("--csv", type=Path, help="Write parsed measurement records to CSV.")
    parser.add_argument(
        "--summary-json",
        type=Path,
        help="Write metadata, step, cycle, and first/last record summary JSON.",
    )
    parser.add_argument("--timezone", help="IANA timezone for Unix millisecond timestamps, for example Asia/Shanghai.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = read_ccs(args.ccs_file, timezone=args.timezone)

    if args.csv:
        result.records.to_csv(args.csv, index=False, encoding="utf-8-sig")
    if args.summary_json:
        result.write_summary_json(args.summary_json)

    print(json.dumps(result.summary_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
