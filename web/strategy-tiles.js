/* Four compact, real-time, virtual DEMO strategy cards and one shared bottom sheet. */
(() => {
  'use strict';
  const strip = document.getElementById('strategy-tile-strip');
  const backdrop = document.getElementById('strategy-sheet-backdrop');
  if (!strip || !backdrop) return;
  const tiles = [...strip.querySelectorAll('[data-strategy-tile]')];
  const sheet = backdrop.querySelector('.strategy-sheet');
  const closeButton = document.getElementById('strategy-sheet-close');
  const powerButton = document.getElementById('strategy-sheet-power');
  const settingsButton = document.getElementById('strategy-sheet-settings');
  const historyButton = document.getElementById('strategy-sheet-history');
  const error = document.getElementById('strategy-sheet-error');
  let latest = [];
  let selected = 0;
  let currency = 'USD';
  let verified = false;
  let fresh = false;
  let busy = false;
  let previousFocus = null;

  const isPaused = tile => ['paused_gap', 'paused_funds'].includes(tile?.phase);
  const trustworthy = () => verified === true && fresh === true;
  const fmt = cents => window.formatDemoMoney?.(cents, currency) || '—';
  const signed = cents => cents == null || !Number.isFinite(Number(cents))
    ? '—' : (cents > 0 ? '+' : '') + fmt(cents);
  const resultColor = cents => cents == null ? 'unknown' : cents > 0 ? 'profit' : cents < 0 ? 'loss' : 'neutral';

  function state(tile) {
    if (!tile) return 'Нет данных';
    if (isPaused(tile)) return tile.phase === 'paused_gap' ? 'Пропуск раундов' : 'Недостаточно средств';
    if (!tile.enabled) return 'Остановлена';
    if (!trustworthy()) return 'Нет свежих данных';
    if (tile.phase === 'betting') return 'Ставка';
    if (tile.id === 2 && tile.phase === 'waiting_reference') return 'Ожидает опорный X';
    if (tile.id === 2) return 'Ожидание ' + (tile.progress || '—');
    return 'Ожидание ' + (tile.progress || '—');
  }

  function render() {
    const activeAccess = window.accessState?.premium === true;
    strip.hidden = !activeAccess || latest.length !== 4;
    tiles.forEach((button, index) => {
      const tile = latest[index];
      if (!tile) return;
      const indicator = button.querySelector('.strategy-tile-led');
      const middle = button.querySelector('.strategy-tile-main');
      const money = button.querySelector('.strategy-tile-net');
      const online = trustworthy() && tile.enabled && !isPaused(tile);
      indicator.dataset.state = online ? 'on' : !tile.enabled ? 'off' : 'unknown';
      middle.textContent = !tile.enabled ? 'Выкл' : !trustworthy() || isPaused(tile)
        ? '—' : tile.phase === 'betting' ? fmt(tile.stake_cents) : tile.progress || '—';
      const net = trustworthy() ? tile.net_cents : null;
      money.textContent = signed(net);
      money.dataset.state = resultColor(net);
      button.title = 'Стратегия №' + tile.id + ' · ' + state(tile) +
        ' · Результат: ' + signed(net);
      button.setAttribute('aria-label', button.title);
    });
    if (selected) renderSheet();
  }

  function renderSheet() {
    const tile = latest[selected - 1];
    if (!tile) return;
    const on = tile.enabled && !isPaused(tile) && trustworthy();
    document.getElementById('strategy-sheet-title').textContent = 'Стратегия №' + selected;
    document.getElementById('strategy-sheet-led').dataset.state = on ? 'on'
      : !tile.enabled ? 'off' : 'unknown';
    document.getElementById('strategy-sheet-state').textContent = state(tile);
    document.getElementById('strategy-sheet-target').textContent = '×' + tile.target;
    document.getElementById('strategy-sheet-action').textContent =
      trustworthy() ? tile.last_action || 'Нет ставок' : 'Нет данных';
    const net = trustworthy() ? tile.net_cents : null;
    const result = document.getElementById('strategy-sheet-result');
    result.textContent = signed(net);
    result.dataset.state = resultColor(net);
    powerButton.dataset.enabled = tile.enabled ? 'true' : 'false';
    powerButton.setAttribute('aria-label', tile.enabled ? 'Выключить стратегию №' + selected
      : 'Включить стратегию №' + selected);
    powerButton.title = powerButton.getAttribute('aria-label');
    powerButton.disabled = busy;
  }

  function closeSheet() {
    if (backdrop.hidden) return;
    backdrop.hidden = true;
    selected = 0;
    error.textContent = '';
    if (previousFocus?.isConnected && !previousFocus.closest('[hidden]')) {
      previousFocus.focus({preventScroll:true});
    }
    previousFocus = null;
  }
  window.closeStrategySheet = closeSheet;
  window.renderStrategyTiles = (items, options = {}) => {
    if (Array.isArray(items)) latest = items;
    if (options.currency) currency = options.currency;
    if (Object.prototype.hasOwnProperty.call(options, 'verified')) verified = options.verified;
    if (Object.prototype.hasOwnProperty.call(options, 'fresh')) fresh = options.fresh === true;
    render();
  };

  tiles.forEach(button => button.addEventListener('click', () => {
    if (strip.hidden || !window.accessState?.premium) return;
    previousFocus = button;
    selected = Number(button.dataset.strategyTile);
    error.textContent = '';
    renderSheet();
    backdrop.hidden = false;
    closeButton.focus({preventScroll:true});
  }));
  closeButton.addEventListener('click', closeSheet);
  backdrop.addEventListener('click', e => { if (e.target === backdrop) closeSheet(); });
  document.addEventListener('keydown', e => {
    if (backdrop.hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); closeSheet(); }
    if (e.key === 'Tab') {
      const buttons = [closeButton, powerButton, settingsButton, historyButton].filter(b => !b.disabled);
      const first = buttons[0], last = buttons[buttons.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  powerButton.addEventListener('click', async () => {
    const tile = latest[selected - 1];
    if (!tile || busy || !window.accessState?.premium) return;
    busy = true;
    powerButton.disabled = true;
    error.textContent = '';
    const action = ['demo_toggle', 'high_gap_toggle', 'target_streak_toggle', 'recovery_streak_toggle'][selected - 1];
    const tg = window.Telegram?.WebApp;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/api/controls', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {
          ...(tg?.initData ? {Authorization: 'tma ' + tg.initData} : {}),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({action}), signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось изменить состояние.');
      window.invalidateControlsSnapshot?.();
      window.renderStrategyTiles(data.strategy_tiles, {
        currency: data.demo?.currency, verified: data.demo?.balance_verified,
      });
      window.refreshAccessOverview?.();
    } catch (e) {
      error.textContent = e.name === 'AbortError' ? 'Сервер не ответил.' :
        String(e.message || 'Ошибка управления.');
    } finally {
      clearTimeout(timeout);
      busy = false;
      if (!backdrop.hidden) renderSheet();
    }
  });

  settingsButton.addEventListener('click', () => {
    if (!selected) return;
    const id = selected;
    closeSheet();
    window.invalidateControlsSnapshot?.();
    window.openAppTab?.('demo');
    const accordion = document.getElementById([
      'demo-classic-accordion', 'demo-high-accordion',
      'demo-target-accordion', 'demo-recovery-accordion',
    ][id - 1]);
    if (accordion) {
      accordion.open = true;
      accordion.scrollIntoView({block:'start', behavior:'instant'});
    }
  });

  historyButton.addEventListener('click', () => {
    if (!selected) return;
    const id = selected;
    closeSheet();
    window.openAppTab?.('history');
    window.openStrategyHistory?.(id);
  });
})();
