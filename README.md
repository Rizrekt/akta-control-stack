# AkTa Control Stack

**The control layer for autonomous agents that do real work.**

Built by [AkTa Agent Works](https://OURDOMAIN) — an AI agent operations
studio run by one human founder and two persistent AI agent operators.

> **Status: working private alpha.** This stack runs our own agent operations
> every day. It is published as a source-available toolkit so reviewers and
> other agent teams can inspect and run it; it is not yet a packaged,
> self-serve public release. See [Roadmap](#roadmap).

## The problem

AI agents can already search, call tools, and act. What operators still
cannot do is *trust* them with recurring work:

- an agent can spend money or quota faster than anyone notices;
- its skills and prompts can change silently between sessions;
- it can claim "done" when nothing actually happened;
- a hung process looks alive to naive process checks;
- logs leak secrets the moment they are shared for debugging;
- on-chain actions are irreversible, yet often sent without simulation,
  screening, approval, or final-state verification.

Control Stack is the set of small, boring, tested tools we built to close
those gaps. Other agents optimize for acting. **We optimize for acting only
when the evidence, the limits, and the final state can be verified.**

## Components

| Component | What it does | Tests |
|---|---|---|
| [`spend-governor/`](spend-governor/) | Fail-closed spend authorization: per-transaction, daily, and session caps; incomplete state means *deny*. Includes a manual `HALT` kill file. | 34 |
| [`skill-integrity/`](skill-integrity/) | SHA-256 manifest over an entire skill shelf; verification reports every CHANGED / MISSING / NEW file, so a silently edited agent skill is detected. | 8 |
| [`cost-ledger/`](cost-ledger/) | Append-only cost book for API and on-chain spend, with daily thresholds and alerts — the audit trail the governor's decisions are checked against. | 23 |
| [`process-watchdog/`](process-watchdog/) | Liveness by CPU-time progress, not `pgrep` self-matches; bounded restarts with a per-hour cap and an operator-cleared HALT state. | 19 |
| [`log-redact/`](log-redact/) | Two-layer log redaction: private keys, bot tokens, session strings, credentialed proxy URLs, mnemonics, and literal sensitive env values — while keeping operational addresses auditable. | 20 |
| [`system-doctor/`](system-doctor/) | One read-only command that reports the health of the whole deployment (proxies, wallet sessions, disk, manifest, governor state, clock). | — |

**Total: 104 tests, all passing** (re-run in this package; see
[TEST-RESULTS.md](TEST-RESULTS.md)).

Two further pieces of the stack live in our skill library rather than as
standalone tools, and are described here for completeness:

- **Transaction Pipeline Gates** — every on-chain action follows
  build → simulate → screen → authorize → confirm → broadcast → record, with
  receipt and final-state verification before anything is called a success.
- **Confirmation Code Protocol** — one-time confirmation codes bound to the
  SHA-256 hash of the exact transaction payload; change one byte and the
  code is void.

## The stack in production (our own operations, 21 Nov 2026)

- **48 active agent skills**, **263 files** under the SHA-256 integrity
  manifest — verification clean: **263 OK, 0 changed, 0 missing, 0 new**.
- **System Doctor** live run: **8 OK, 1 WARN, 0 FAIL**. The WARN is
  disclosed, not hidden: the Spend Governor is installed and tested but has
  not yet been wired into a production runner.
- The stack supports daily multi-project Web3 agent operations: agent
  competitions, project and airdrop research, on-chain operations,
  testnet market-making research, prediction-market evidence work, and
  bug-bounty research.

## Quick start

Each component is a dependency-free Python 3 folder (standard library only).
Run its tests from inside the folder:

```bash
cd spend-governor  && python3 test_governor.py
cd skill-integrity && python3 test_integrity.py
cd cost-ledger     && python3 -m unittest test_ledger -v
cd process-watchdog && python3 test_watchdog.py
cd log-redact      && python3 -m unittest test_redact -v
cd system-doctor   && python3 doctor.py        # read-only
```

## Honest limits

- No external users yet: the only operator team using this stack today is
  our own.
- The Spend Governor is a tested tool, not yet a production-wired control
  (see the Doctor WARN above).
- System Doctor's check list is specific to our deployment by design;
  adapt the checks, keep the read-only contract.
- Several components were adapted from third-party agent-tooling research
  packages; each folder's README records its provenance and what we changed.

## Roadmap

- **Months 1–2:** public repository with reproducible demo (this package),
  product website, data-source integration with per-call provenance and
  cost recording, product metrics instrumentation.
- **Months 3–4:** installable packaging of Skill Integrity, Spend Governor,
  Cost Ledger, and Log Redact; operation templates for project triage,
  competition intelligence, and airdrop radar; governor wired into the
  research runner.
- **Months 5–6:** public beta with installation docs, example skills, and
  reproducible integrity reports; a simple dashboard for data budget,
  running workflows, gate rejections, and verification results.
- **Months 7–12:** limited Operations Desk pilots for Web3 teams; publish
  case studies from our own operations; choose the commercial shape
  (developer product, managed service, or bundle) from measured demand.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 AkTa Agent Works.
