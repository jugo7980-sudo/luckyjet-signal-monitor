# Telegram History + Streak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one permanent 20-button history message and one temporary editable streak button message.

**Architecture:** `TelegramHistoryPanel` owns the permanent history message and persists/reuses its message ID. `TelegramStreakPanel` owns a separate temporary inline-keyboard message and never mutates or replaces history. Telegram requests continue through the shared HTTP client.

**Tech Stack:** Python, asyncio, httpx, Telegram Bot API, pytest, Render.

**Spec:** `docs/superpowers/specs/2026-10-29-telegram-history-streak-design.md`

## Global Constraints
- History: exactly one message, latest 20 coefficients, 5 × 4 buttons.
- Streak threshold: 4 consecutive rounds `< 2.00x`; ends at first `>= 2.00x`.
- Finished streak message deletion delay: 60 seconds.
- No change to coefficient collection/source logic.

## Review Focus
- Restart reuses history message ID instead of sending a duplicate.
- Exactly 20 history buttons remain after new rounds.
- Streak does not appear at 1–3 qualifying rounds.
- Streak can continue beyond 20 rounds without truncation.
- Ending a streak never deletes or recreates history.

---

### Task 1: Permanent history message
**Files:** Modify `main.py`; Test `tests/test_browser_runtime.py`
- [ ] Write failing tests for initial 20-button 5×4 layout and persisted history `message_id` reuse with no `sendMessage`.
- [ ] Run tests and verify the new persistence test fails.
- [ ] Implement minimal history message-ID load/save and reuse; recreate only on Telegram "message not found"-style failure.
- [ ] Run targeted tests and verify pass.

### Task 2: Streak as one large button
**Files:** Modify `main.py`; Test `tests/test_browser_runtime.py`
- [ ] Write failing tests: no message before 4, `sendMessage` with one full-width button at 4, same message edited at 5+, final button `N подряд → X.XXx`, scheduled delete after completion.
- [ ] Run tests and verify failures.
- [ ] Implement minimal inline-keyboard streak renderer and lifecycle.
- [ ] Run targeted tests and verify pass.

### Task 3: Regression + deployment
**Files:** existing tests only
- [ ] Run full project test suite and report any unrelated failures by name.
- [ ] Deploy to Render.
- [ ] Verify service is live and inspect runtime logs for history updates and absence of duplicate history creation after startup.
