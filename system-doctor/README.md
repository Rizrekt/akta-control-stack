# System Doctor

One read-only command that reports the health of a whole agent deployment on
one screen, so a failure is diagnosed in seconds instead of being hunted
subsystem by subsystem.

## What it checks (in our deployment)

- **Local proxies** — an HTTP `GET /` against each local proxy port; for the
  proxies we run, `404` on the root is the healthy answer.
- **Wallet CLI sessions** — `<wallet-cli> wallet status --json` for each
  configured session (a pure read; session files are never opened or printed).
- **Disk and /tmp** — free space, plus the real size of `/tmp` (on our VM it
  is a small tmpfs that vanishes on reboot, so work files must not live there).
- **Skill-integrity manifest** — presence and age of the SHA-256 manifest.
- **Spend Governor state** — whether a `HALT` file is present, and whether
  the governor state database exists yet.
- **System clock** — because every project deadline is read from this clock.

## Guarantees

- **Read-only, absolutely.** It never restarts a proxy, never re-pairs a
  session, never writes a file, and never reads credential or session-file
  contents.
- Exit code `0` when there is no FAIL (WARN is allowed), `1` on any FAIL.
- `--json` prints the same results as JSON.

## Usage

```bash
python3 doctor.py
python3 doctor.py --json
```

## Adapting it

The check list is deliberately deployment-specific — that is the point of a
doctor. Keep the read-only contract and the one-screen report; replace the
checks with the subsystems your own agents depend on.

In our live deployment on 21 November 2026 it reported **8 OK, 1 WARN,
0 FAIL**; the WARN was honest: the Spend Governor was installed and tested
but had not yet been wired into a production runner, so its state database
did not exist yet.

## Provenance

The doctor/self-check pattern was adapted from the SUPERAGENT V10
agent-tooling package; the check set is ours.
