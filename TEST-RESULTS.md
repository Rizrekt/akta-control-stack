# Test Results — AkTa Control Stack (public package copy)

All suites below were **re-run inside this package copy** on
**21 November 2026**, Python **3.12.3**, after copying and sanitization.
Nothing was fixed silently: results are reported exactly as observed.

| Component | Command | Result |
|---|---|---|
| Spend Governor | `python3 test_governor.py` | **34 ran — OK (34/34)** |
| Skill Integrity | `python3 test_integrity.py` | **8 ran — OK (8/8)** |
| Process Watchdog | `python3 test_watchdog.py` | **19 ran — OK (19/19)** |
| Cost Ledger | `python3 -m unittest test_ledger` | **23 ran — OK (23/23)** |
| Log Redact | `python3 -m unittest test_redact` | **20 ran — OK (20/20)** |
| **Total** | | **104/104 passing** |

These counts match the numbers recorded in our internal design document
for the originals on the same day.

## System Doctor (no test suite — execution check only)

`python3 doctor.py` in this copy compiles and runs end-to-end (exit code 1).
Its checks are deployment-specific: in this generalized copy the
"secondary wallet session" check reports FAIL because the example session
directory it points to does not exist on the machine — the check itself
works (it invoked the wallet CLI and read its status). The reference live
result from our real deployment on 21 November 2026 is **8 OK, 1 WARN,
0 FAIL** (WARN: Spend Governor state not yet created by any production
runner).

All copied `.py` files also pass `python3 -m py_compile`.

## Sanitization applied to this public copy

The originals are production tools that contained deployment-specific
identifiers. Before copying, the following — and only the following —
were changed. **No logic was altered.**

1. A real wallet address used in `spend-governor`'s `__main__` demo and as
   a `log-redact` test fixture was replaced with example addresses
   (`0x0000…0001`, `0x0000…dEaD`).
2. Personal wallet-session labels in `system-doctor` ("primary"/"secondary"
   replace named sessions; the secondary session directory was generalized
   to `~/.baw-secondary`), and local proxy names were generalized to
   "Local proxy A/B". Internal launcher script names were replaced with
   generic wording.
3. `process-watchdog` test fixtures used internal absolute paths and
   internal process names; they were replaced with `/srv/example/worker-*.py`
   examples, consistently across fixtures, patterns, and assertions. Its
   `config.example.json` was rewritten as a generic example, and its
   internal README replaced with the concise English one in this package.
4. `cost-ledger` tests and README used internal project/lane names as
   example strings; they were replaced with `demo-project`, `search-api`,
   and `llm-router`.
5. `system-doctor`'s internal README was replaced with the concise English
   one in this package.
6. Source-package attribution was kept (SUPERAGENT V10 and the airdrop-agent
   study), but personal descriptors of the source authors and all paths
   into our internal workspace/research folders were removed.
7. Excluded from the copy entirely: `__pycache__`, our generated
   `skills-manifest.json`, and any state/database/log files. A final sweep
   of the package for wallet addresses, home paths, session names, and
   credentials returned zero hits.

Test secrets inside `log-redact` tests are fake values created for the
tests, as marked in that file.
