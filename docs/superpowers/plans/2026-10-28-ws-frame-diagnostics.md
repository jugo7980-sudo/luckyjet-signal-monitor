# WebSocket Frame Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture a small, redacted structural sample of incoming Lucky Jet WebSocket frames so the real `a` event payload can be identified without changing signal logic.

**Architecture:** Add a focused `ws_diagnostics` module that summarizes Socket.IO-like payloads while redacting sensitive fields, then wraps the existing `handle_ws_frame` delegate. `main.py` installs the wrapper once at startup; the monitor remains read-only and the original handler still processes every frame.

**Tech Stack:** Python 3, asyncio, logging, pytest, Playwright, Render.

**Spec:** Current 2026-10-28 debugging brief from Render evidence; no separate repository spec file.

## Global Constraints

- Do not place bets or add any betting action.
- Do not change streak/signal decision logic.
- Never log tokens, authorization, cookies, session/customer identifiers, emails, or passwords.
- Log at most 8 WebSocket frame samples per process.
- Every frame must still reach the original `handle_ws_frame` delegate.

## Review Focus

- Sensitive keys nested inside dicts must be redacted.
- Non-JSON WebSocket frames must not crash diagnostics.
- Large strings/lists must be bounded in logs.
- Diagnostic sampling must stop after the configured limit.
- Delegated frame processing must continue even after the sample limit is reached.

---

### Task 1: Safe WebSocket payload diagnostics

**Files:**
- Create: `src/luckyjet_signal/ws_diagnostics.py`
- Create: `tests/test_ws_diagnostics.py`
- Modify: `main.py`

**Interfaces:**
- Produces: `summarize_ws_payload(payload) -> str`
- Produces: `wrap_ws_handler(delegate, *, max_samples=8)` returning an async-compatible handler.
- Consumes: existing `luckyjet_signal.monitor.handle_ws_frame` and `luckyjet_signal.monitor.run_monitor`.

- [ ] **Step 1: Write failing tests** for redaction/shape preservation and sample-limit/delegate behavior.
- [ ] **Step 2: Run tests and verify RED** because `luckyjet_signal.ws_diagnostics` does not exist.
- [ ] **Step 3: Implement the minimal diagnostics module** with bounded recursive redaction and a sample-limited wrapper.
- [ ] **Step 4: Run tests and verify GREEN** with both diagnostics tests passing.
- [ ] **Step 5: Modify `main.py`** to wrap `monitor.handle_ws_frame` once before starting `monitor.run_monitor`.
- [ ] **Step 6: Run syntax/tests** and confirm diagnostics do not alter delegate calls.
- [ ] **Step 7: Commit changes** on `debug/ws-frame-diagnostics` and open a pull request to `main`.
