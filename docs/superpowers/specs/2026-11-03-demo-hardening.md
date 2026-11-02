# Demo Hardening Spec

## Safety
- Keep `main` unchanged until branch verification is complete.
- Preserve backup branch `backup/working-before-grill-2026-11-03` at commit `43f71831eea726463a15e07e076fcd6b5dfae379`.
- Demo only: never place real wagers.

## Demo session rules
- Default demo balance: $10,000.
- Default and minimum starting stake: $10.
- User may manually raise starting stake between sessions; it persists.
- Active-session settings cannot be changed until the session ends or is explicitly reset.
- Trigger threshold for demo is user-selected from existing demo options.
- Trigger rounds only arm a session; the first wager is the following completed round.
- Loss doubles the next stake x2.
- Any coefficient >=2x wins the step with payout calculated at exactly 2x while storing the real coefficient.
- If the next doubled stake cannot be funded, the current session ends as a loss with reason `insufficient_balance`; the unaffordable step is not counted. Starting stake is reset to $10 and remains $10 until manually changed.
- After an insufficient-balance loss, a new session waits for a fresh trigger.
- If balance is below $10, demo pauses until balance is manually raised/reset.
- Demo emits no routine start/progress/end notifications. Exception: insufficient balance or unrecoverable data gap generates a warning message that lives up to 10 minutes and has a Close button.

## Integrity and recovery
- Never process the same completed round twice.
- On restart or temporary outage, restore active demo state and process every recoverable missed completed round in chronological order.
- If missed rounds cannot be reconstructed without guessing, do not fabricate a financial result. Finish the active demo session as `incomplete_data`, preserve the last confirmed balance, keep the starting stake unchanged, and wait for a fresh trigger.
- A user-requested reset during an active session stores it as `stopped_by_user`, excluded from win/loss statistics.
- Disabling demo while a session is active must not create a data gap; block the action until the session ends or is reset.

## History
- Keep exactly the newest 100 completed/terminated demo sessions, 10 per page.
- Button format for normal sessions: `<steps> шаг. · DD.MM HH:MM · +/-$amount` using Thailand UTC+7 and session end time.
- Incomplete session button uses `⚠️ данные` instead of a made-up amount.
- Details show start time, end time, duration, each real step, starting/ending balance, total stake, net result when known, and termination reason.
- Newest first. Back from a session returns to its exact page.
- Refresh does not change balance/session/history and stays on the current page where possible.
- When a new session finishes, page 1 can refresh to include it; users browsing later pages must not be forced back to page 1.
- Reset demo does not delete history. History clearing, if exposed, is a separate confirmed action.

## Statistics
- Count: total sessions, successful, losses, incomplete, stopped by user, total known net result, average steps over financially complete sessions.
- Incomplete and user-stopped sessions are neither wins nor losses and are excluded from aggregate financial result.
- Insufficient-balance sessions count as losses.
- Statistics derive from stored history so there is a single source of truth.

## UI and timers
- History remains open; ordinary menu/transient windows close after 60 seconds of inactivity and each user click resets that 60-second timer.
- Insufficient-balance / incomplete-data warnings last up to 10 minutes and can be closed manually.
- Existing permanent history panel remains independent from temporary menu panels.
- Balance button text keeps the refresh icon before the money icon.
- Regular streak notifications obey only their own notification threshold setting; demo does not create them.

## Performance
- Normal source polling target interval: 0.1 seconds between scheduled poll starts when the previous poll is not still running.
- Never run overlapping polls for the same source refresh.
- On source errors/rate limits use adaptive backoff: 0.1 -> 0.25 -> 0.5 -> 1 -> 2 seconds, then return to 0.1 immediately after a successful source response.
- Telegram edits happen only when rendered content changed. Preserve existing coalescing, caching, Redis batching, direct-endpoint concurrency, and per-user ordering.
- A button-triggered refresh performs one source refresh immediately; it does not create a session or mutate data when no new round exists.

## Verification
- Every production behavior change gets a regression test first.
- Full `pytest -q` must pass before merging to main.
- Verify the branch against this spec line by line before deployment.
