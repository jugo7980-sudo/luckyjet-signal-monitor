# Demo Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make demo sessions lossless/restart-safe, retain 100 sessions, enforce the approved $10 reset rules, keep Telegram UI consistent, and reduce source polling latency without introducing overlapping work.

**Architecture:** Keep `DemoLedger` as the single demo accounting state machine, preserve Redis split persistence, and make source integrity explicit before any demo mutation. Extend existing Telegram rendering and timers surgically rather than introducing a new framework. Polling changes stay inside the direct-only runtime and existing fast poller.

**Tech Stack:** Python 3.12, asyncio, httpx, Redis, Telegram Bot API, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-03-demo-hardening.md`

## Global Constraints
- Demo only; never place real wagers.
- `main` remains unchanged until full branch verification is green.
- Backup `backup/working-before-grill-2026-11-03` remains untouched.
- Minimum/default demo stake is $10; default balance is $10,000.
- Store at most 100 demo sessions, 10 per page.
- Normal poll target 0.1s; no overlapping refreshes; adaptive backoff 0.1/0.25/0.5/1/2s.
- Preserve existing Telegram coalescing/caching and Redis batching.

## Review Focus
- Multiple missed rounds with and without the old round ID still present.
- Two consecutive equal coefficients without stable round IDs.
- Restart during an active demo session.
- 100th history entry and callback length/range boundaries.
- Source failure/backoff recovery without duplicate or overlapping polling.

---

### Task 1: Source integrity and missed-round recovery

**Files:**
- Modify: `src/luckyjet_signal/monitor.py`
- Modify: `luckyjet_source_guard.py`
- Test: `tests/test_source_guard.py`
- Test: `tests/test_app_v1.py`

**Interfaces:**
- Consumes: source history newest-first, `State.last_round_id`, `State.previous`.
- Produces: ordered list of only confirmed new completed rounds; explicit gap result when continuity cannot be proven.

- [ ] Add failing tests for: recover all missed rounds in chronological order; do not process only newest when continuity is unknown; equal coefficients with distinct IDs are distinct rounds; no-ID ambiguity never invents a round.
- [ ] Run targeted tests and confirm failures are for the missing integrity behavior.
- [ ] Implement the smallest continuity/gap logic in existing history processing/source guard.
- [ ] Run targeted tests, then full `pytest -q`.
- [ ] Commit.

### Task 2: Demo ledger rules and 100-session persistence

**Files:**
- Modify: `src/luckyjet_signal/demo_ledger.py`
- Modify: `demo_persistence.py` only if needed for bounded restore.
- Test: `tests/test_demo_ledger.py`
- Test: `tests/test_demo_persistence.py`

**Interfaces:**
- Consumes: one confirmed completed round at a time.
- Produces: demo event dictionaries and bounded history entries with outcomes `recovered`, `insufficient_balance`, `incomplete_data`, `stopped_by_user`.

- [ ] Add failing tests for default $10, 100-session retention, insufficient-balance closes before unaffordable step and resets base stake to $10, incomplete/stopped outcomes, reset preserving history, active setting locks.
- [ ] Verify RED.
- [ ] Implement minimal ledger changes and bounded restore.
- [ ] Verify targeted + full suite.
- [ ] Commit.

### Task 3: Telegram demo UI, warnings, history, and statistics

**Files:**
- Modify: `luckyjet_password_gate.py`
- Modify: `luckyjet_reliable_gate.py` only for warning persistence if restart recovery requires it.
- Test: `tests/test_cycle_list.py`
- Test: `tests/test_cycle_button_round_count.py`
- Test: `tests/test_menu_timeout.py`
- Add/modify focused demo UI tests as needed.

**Interfaces:**
- Consumes: demo events/outcomes from Task 2.
- Produces: 100-session navigation, status-aware detail text, 10-minute closable warnings, demo statistics, stable navigation.

- [ ] Add failing tests for callback indexes 0-99, 10 pages, incomplete labels, exact page return, reset/history behavior, active demo toggle/settings lock, 10-minute closable warnings, duration/details, statistics exclusions, refresh icon before money icon.
- [ ] Verify RED.
- [ ] Implement only required UI/timer/event handling changes.
- [ ] Verify targeted + full suite.
- [ ] Commit.

### Task 4: Low-latency polling with bounded backoff

**Files:**
- Modify: `src/luckyjet_signal/settings.py`
- Modify: `src/luckyjet_signal/direct_only.py`
- Modify: `luckyjet_fast_poll.py` only where 0.25s preferred-source hedge conflicts with 0.1s target.
- Test: `tests/test_direct_only_mode.py`
- Add/modify polling tests.

**Interfaces:**
- Consumes: existing `HistoryRefresh` single-flight refresh and direct poller.
- Produces: non-overlapping poll starts targeting 0.1s in healthy state and adaptive backoff 0.25/0.5/1/2s on source failures.

- [ ] Add deterministic failing tests using mocked monotonic/sleep for cadence, no overlap, backoff, and immediate recovery to 0.1s.
- [ ] Verify RED.
- [ ] Implement monotonic-deadline scheduling and bounded backoff without parallel poll loops.
- [ ] Verify targeted + full suite.
- [ ] Commit.

### Task 5: Branch-wide verification and cleanup

**Files:**
- Remove temporary branch-only CI workflow before merge if no longer needed.
- No production refactors.

- [ ] Run full `pytest -q` on final branch and inspect complete result.
- [ ] Compare branch against `main`; verify every changed file maps to the spec.
- [ ] Re-read spec line-by-line and record any unimplemented item as a blocker.
- [ ] Verify `main` and backup branch still point to the original working commit until merge.
- [ ] Only after all checks pass, merge/publish through the existing safe deployment path.
