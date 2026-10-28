"""Shared Telegram history panel with a permanent menu entry."""

import main as base


class MenuHistoryPanel(base.TelegramHistoryPanel):
    """Keep the original 5x4 history grid and add one menu row below it."""

    def _reply_markup(self):
        markup = super()._reply_markup()
        rows = [list(row) for row in markup.get("inline_keyboard", [])]
        rows.append([{"text": "☰ Меню", "callback_data": "menu_open"}])
        return {"inline_keyboard": rows}
