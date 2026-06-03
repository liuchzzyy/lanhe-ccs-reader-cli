# LANHE CCS Reader CLI

Python CLI and library for reading LANHE/LAND `.ccs` battery-test files and exporting vendor-style record tables.

The implementation is verified against the real local samples in this repository:

- `samples/AA.ccs` with vendor export `samples/AA.xlsx`
- `samples/BB.ccs` with vendor export `samples/BB.xlsx`
- `samples/CC.ccs`, parsed as a large no-XLSX smoke sample

## Decoded Data

- CCS data pages: 128-byte pages, with an 8-byte page header and up to six 20-byte measurement records.
- Control pages: work mode, cycle number, process step number, step start test time, and wall-clock timing anchors.
- Per-record values: time delta, voltage, current, capacity increment, and energy increment.
- Export-style columns: cycle, step, process step, step duration, step time, test time, system time, cumulative capacity and energy, specific capacity and energy, power, marks, data file, test name, process name, and channel number.
- Summary tables: per-step summaries, per-cycle summaries, and start/finish log rows.
- Header metadata: test name, process name and description, software version, serial number, active material mass, nominal specific capacity, and channel label.

`dQdV` and `dVdQ` are emitted as finite-difference derivative columns. The vendor XLSX export applies an additional smoothing/windowing rule for these derivative columns, so validation reports their error separately from the strict raw/export-column check.

## Usage

Create the environment and run the sample validation:

```powershell
uv sync --group dev
uv run ccs-reader-validate samples\AA.ccs samples\AA.xlsx
uv run ccs-reader-validate samples\BB.ccs samples\BB.xlsx
```

Export parsed records:

```powershell
uv run ccs-reader samples\AA.ccs --csv samples\AA.parsed.csv --summary-json samples\AA.summary.json
```

Use as a Python library:

```python
from ccs_reader import CCSReader, read_ccs

result = read_ccs("samples/AA.ccs")
records = CCSReader("samples/BB.ccs").to_dataframe()
```

Run the test suite:

```powershell
uv run pytest
```

## Acknowledgement

This project was completed with AI assistance.
