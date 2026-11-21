# Process Watchdog

Liveness detection and bounded restarts for long-running agent processes.

## Why

`pgrep -f <pattern>` lies: the checking shell's own command line contains the
pattern, so a dead process can look alive. This watchdog decides liveness the
honest way — a process is alive only if it exists in the `ps` table **and its
CPU time keeps advancing** between ticks. A process that is present but whose
CPU time is frozen is `stalled`, and stalled counts as dead.

## What it does

- Reads a `ps -eo pid=,time=,args=` table and matches configured bracket-style
  regex patterns (the bracket style keeps the watchdog from matching itself).
- Tracks CPU-time progress per process across ticks.
- Restarts dead/stalled processes with an explicit argv command — never a
  shell string — subject to a **restart cap per hour** (sliding window).
- When the cap is hit, the process is **HALTED**: no more restarts until an
  operator clears the halt (`--clear-halt`). State persists across watchdog
  restarts (`state.json`), and every tick writes a `status.json` snapshot.
- `restart_command: null` means **monitor-only**: death is reported, the
  process is never restarted.

## Usage

```bash
cp config.example.json config.json   # then edit the process list
python3 watchdog.py                  # loop, 60s interval
python3 watchdog.py --once           # single tick
python3 watchdog.py --clear-halt all # operator reset
```

## Tests

```bash
python3 test_watchdog.py   # 19 tests
```

All process tables in the tests are fixture strings, not live processes.

## Provenance

Adapted from the watchdog tool in the SUPERAGENT V10 agent-tooling package;
the CPU-time liveness rule and the halt-on-cap behaviour are our own
hardening, added after a real incident in which a dead proxy kept reporting
as alive.
