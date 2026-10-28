# Telegram history + streak design

- History is one permanent Telegram message. It always contains the latest 20 completed coefficients as 5 columns × 4 rows and is updated only by editing the same message.
- A restart/deploy must reuse the saved history message ID. A new history message may be created only if no saved message exists or Telegram reports that the saved message no longer exists.
- Streak monitoring is independent from history. A streak begins when 4 consecutive completed rounds are below 2.00x, continues without a maximum length, and ends on the first coefficient >= 2.00x.
- The streak is one separate temporary message rendered as one full-width inline button with minimal text. Active: `🔵 N подряд < 2x`. Finished: `🟪 N подряд → X.XXx`.
- The same streak message is edited while active. After completion it remains with the final coefficient for 60 seconds and is then deleted.
- History must never be deleted when a streak ends.
