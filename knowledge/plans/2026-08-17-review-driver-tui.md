# review-driver Interactive TUI Implementation Plan

See the session plan for the full specification. This file is the repo-local copy.

Give `review-driver` the same TTY auto-detect as `review-analyzer` and `review-action`: a full-screen Textual dashboard on an interactive terminal, plain phase summaries in CI / pipes, and `--tui` / `--no-tui` to override. Children stay `--no-tui`. Disk artifacts stay identical in both modes.

## Phase 1: Progress sink in `run_driver` (no TUI yet)

- [x] `ProgressReporter` Protocol + `PlainReporter` + `DriverRunMeta`
- [x] `run_driver(..., reporter=None)` defaults to `PlainReporter()`
- [x] `_fail_pass` uses `sink.finish` (no double-print)
- [x] `should_use_tui` + `_force_tui_from_args` landed
- [x] Existing driver tests pass
- [x] `TestShouldUseTui` added

## Phase 2: TUI app, CLI flags, live child logs, README

- [x] `deep_architect/review_driver_tui.py`
- [x] `--tui` / `--no-tui` flags and `main()` branch
- [x] `ChildLogFanout` + OCR stderr streaming via Popen
- [x] `tests/test_review_driver_tui.py`
- [x] README Review Driver section updated
