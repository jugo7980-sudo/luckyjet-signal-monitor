# Telegram site account Implementation Plan

> Implement inline with superpowers:executing-plans, under test-driven-development.

**Goal:** Owner signs into the site via Telegram, submits email challenges, and reuses the resulting browser session.

**Architecture:** SiteAccount owns the temporary dialogue and bounded browser task. SiteLoginBrowser handles only login DOM operations. SessionStore atomically saves browser storage; direct_only reloads it on generation changes. TelegramPasswordGate routes private owner messages before normal command/demo handling.

**Tech Stack:** Existing Python, asyncio, Playwright, Telegram HTTP client, pytest.

**Spec:** docs/superpowers/specs/2026-11-05-site-account.md

## Global Constraints
- Admin Telegram ID and private chat required on every action and input.
- No credential persistence or logging; session file excluded from Git/build context.
- Existing demo/history/state remain unchanged; no bets.
- Do not publish or restart Render until local change is reviewable.

## Review Focus
- Forged callbacks/group messages never receive or alter authentication state.
- Cancellation, timeout and restart never reuse pending passwords or codes.
- Login false positives never produce connected status.
- Browser lock released on cancellation/error; old requests cannot save after logout.
- Saved state is reused by monitoring, and logout invalidates its active context.

### Task 1: Account service and browser driver
**Files:** site_account.py, site_login_browser.py, tests/test_site_account.py, tests/test_site_login_browser.py.
**Interfaces:** SiteAccount.handle_message(message), handle_callback(callback), menu(), close(); busy/generation, storage_state(), browser lock; async login(email, password, request_code) driver.
- [ ] Write tests for owner restriction, password/OTP workflow, expiry/cancel, storage reload and failure.
- [ ] Run tests and confirm feature is absent.
- [ ] Implement bounded conversation and storage without credentials on disk; browser selectors, success verification and OTP retries.
- [ ] Run service tests and real Playwright local-fixture tests.

### Task 2: Runtime integration and delivery
**Files:** app_v1.py, luckyjet_password_gate.py, src/luckyjet_signal/direct_only.py, tests/test_site_account_integration.py, docs/site-account.md, .gitignore, .dockerignore.
**Interfaces:** Gate.site_account optional; monitor.site_account optional. direct_only yields browser lock while account busy and recreates browser on generation change with storage_state.
- [ ] Test gate routing, owner-only menu and monitor context restore/reload.
- [ ] Implement wiring and cleanup; add operational instructions and persistent volume requirement.
- [ ] Run full pytest suite, compileall, git diff --check and independent review.
- [ ] Package reviewable changes with truthful live-site limitations.
