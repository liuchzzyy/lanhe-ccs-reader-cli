# LANHE CCS Reader CLI

Python CLI and library for reading LANHE/LAND `.ccs` battery-test files and exporting measurement records and summaries. The parser was checked against the supplied LANHE `CCSData.exe` using static decompilation and against real vendor XLSX exports; **the EXE is never executed** by this project.

## Install

Requires Python 3.11 or newer. Install [uv](https://docs.astral.sh/uv/) and, from this repository, run:

```sh
uv sync
uv run ccs-reader --help
```

For development and validation, use `uv sync --group dev`.

## Usage

Parse a file and print a JSON summary:

```sh
uv run ccs-reader samples/AA.ccs
```

Write a UTF-8-with-BOM record CSV and JSON summary (text cells that could be interpreted as spreadsheet formulas are prefixed with an apostrophe in CSV only; the library data is unchanged):

```sh
uv run ccs-reader samples/AA.ccs --csv samples/AA.parsed.csv --summary-json samples/AA.summary.json
```

Use `--timezone Asia/Shanghai` to render timestamps in an explicit IANA timezone. Without it, timestamps use the machine's local timezone.

Use from Python:

```python
from ccs_reader import CCSReader, read_ccs

result = read_ccs("samples/AA.ccs")
records = CCSReader("samples/BB.ccs").to_dataframe()
print(result.metadata.to_json_dict())
print(result.cycles)
```

## Format and outputs

- The supported file variant has the `0x95279528` magic and 128-byte pages; other headers/variants fail explicitly rather than being silently misread.
- Data pages have an 8-byte header and up to six 20-byte measurement records. Control pages supply work mode, cycle/process-step indices and timing anchors.
- Records include voltage (V), current (µA), capacity (µAh), energy (µWh), power (µW), step/test duration, timestamps and marks. REST records do not accumulate capacity or energy.
- Outputs include measurement records, step and cycle summaries, and log events decoded from control pages. `metadata.finish_time` is set only when the final log event is `Finish`; incomplete tests leave it unset. Cycle average voltage is weighted by elapsed measurement time, not computed as energy divided by capacity.
- `dQdV` and `dVdQ` are unsmoothed finite-difference estimates. The vendor XLSX applies an additional smoothing/windowing rule; these two columns are reported separately and do not decide strict validation success.

The included AA and BB `.ccs` samples have corresponding vendor `.xlsx` exports. CC is an additional large-file parsing sample. Some vendor output fields are approximations or placeholders; do not treat the reconstructed tables as original source code or a byte-for-byte reproduction of an XLSX export.

## Verify

```sh
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ccs-reader-validate samples/AA.ccs samples/AA.xlsx
uv run ccs-reader-validate samples/BB.ccs samples/BB.xlsx
```

The validation command compares record identities and key raw numeric columns with vendor exports. It prints a JSON report and returns a nonzero exit code on strict mismatches; derivative errors are informational.

## Acknowledgement

Developed with AI assistance and validated against the supplied test artifacts.
