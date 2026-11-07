# DEMO currencies implementation plan

Approved specification: reset form chooses RUB/KRW/USD, manual balance and stake each reset; currency on all money views and records, Premium and DEMO balance in header. Minimum RUB 1, KRW 1, USD .01; KRW integer, others two decimals. Insufficient funds stops DEMO retaining stake. Reset clears financial history only; archive and source maxima remain. Legacy accounts USD, starting balance 10000.

Architecture: keep integer hundredths in existing persisted fields (including KRW, constrained to multiples of 100). Add currency and initial balance, format by currency; preserve legacy serialization. Reuse existing per-user mutation lock and transactional Redis persistence.

- [ ] Ledger: failing tests for currencies, validation, reset, restart, insufficient funds, archive checkpoint preservation; implement currency helpers and ledger fields.
- [ ] APIs: reset requires validated manual configuration, commit replacement only after persistence, revision to deduplicate reset submissions; expose currency in overview/access/history and baseline statistics. Test invalid submissions and isolation.
- [ ] UI and Telegram: reset dialog, currency-aware formatting and notifications, Premium badge and zero-safe live balance. Keep Telegram reset buttons pointing to same configuration form. Mobile browser tests for submit, cancel, header and history.
- [ ] Verification: targeted then full Python suite and browser tests; independent whole-diff review; publish to GitHub and verify Railway deployment/logs without restarting collectors.

Review focus: no reset on invalid/cancelled request; duplicate submit and delayed pre-reset response; source catch-up cannot spend new funds on archived rounds before reset; no mixing USD with other money; persistence errors preserve old account.
