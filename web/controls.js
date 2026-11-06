/* Authenticated, virtual-only DEMO controls and Telegram notification settings. */
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const demoPanel = document.getElementById('demo-controls');
  const settingsPanel = document.getElementById('settings-controls');
  const demoStatus = document.getElementById('demo-controls-status');
  const settingsStatus = document.getElementById('settings-controls-status');
  let busy = false;
  let loadedAt = 0;
  let latest = null;
  const all = [...document.querySelectorAll('[data-control]')];
  function message(text, error = false) {
    const target = !document.getElementById('page-demo').hidden ? demoStatus : settingsStatus;
    target.textContent = String(text || '');
    target.classList.toggle('control-error', error);
  }
  function paint(data) {
    latest = data;
    const demo = data.demo, settings = data.settings;
    demoPanel.hidden = false;
    settingsPanel.hidden = false;
    const toggle = document.getElementById('demo-toggle');
    toggle.textContent = demo.enabled ? 'Выключить DEMO' : 'Включить DEMO';
    document.getElementById('demo-trigger-select').value = String(demo.trigger);
    document.getElementById('demo-stake-input').value = (demo.base_stake_cents / 100).toFixed(2);
    document.getElementById('demo-low-streak').textContent =
      demo.active ? 'Цикл активен · шаг ' + demo.step :
        'Текущая серия <2x: ' + demo.low_streak + ' из ' + demo.trigger;
    document.getElementById('settings-notifications').textContent = settings.notifications ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-sound').textContent = settings.sound ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-threshold').value = String(settings.threshold);
    document.getElementById('settings-history-size').value = String(settings.history_size);
    document.getElementById('settings-delete-delay').value = String(settings.delete_delay);
    for (const node of all) node.disabled = busy || (
      demo.active && (node.id === 'demo-trigger-select' || node.id === 'demo-stake-input' ||
        node.id === 'demo-stake-save' || (node.id === 'demo-toggle' && demo.enabled))
    );
  }
  async function request(method = 'GET', payload) {
    if (!tg || !tg.initData) throw new Error('Открой приложение через Telegram-бота.');
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 8000);
    try {
      const result = await fetch('/api/controls', {
        method,
        headers: {Authorization: 'tma ' + tg.initData,
          ...(method === 'POST' ? {'Content-Type': 'application/json'} : {})},
        ...(method === 'POST' ? {body: JSON.stringify(payload)} : {}),
        credentials: 'omit', cache: 'no-store', signal: abort.signal,
      });
      const data = await result.json();
      if (!result.ok) throw new Error(data.error || 'Ошибка сохранения.');
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }
  async function load(force = false) {
    if (busy || (!force && latest && Date.now() - loadedAt < 20000)) return;
    busy = true;
    message('Загрузка настроек…');
    if (latest) paint(latest);
    try {
      const data = await request();
      loadedAt = Date.now();
      demoStatus.textContent = '';
      settingsStatus.textContent = '';
      paint(data);
    } catch (error) {
      if (/Нет доступа|Сеанс|через Telegram/.test(error.message)) {
        demoPanel.hidden = true;
        settingsPanel.hidden = true;
        latest = null;
      }
      message(error.name === 'AbortError' ? 'Сервер не ответил.' : error.message, true);
    } finally {
      busy = false;
      if (latest) paint(latest);
    }
  }
  async function action(name, value, confirm = false) {
    if (busy) return;
    busy = true;
    message('Сохранение…');
    if (latest) paint(latest);
    try {
      const data = await request('POST', {action: name, ...(value === undefined ? {} : {value}), ...(confirm ? {confirm: true} : {})});
      loadedAt = Date.now();
      paint(data);
      message(data.message || 'Сохранено.');
      if (name === 'demo_reset' && window.resetHistoryCache) window.resetHistoryCache();
      if (typeof loadOverview === 'function') loadOverview();
    } catch (error) {
      message(error.name === 'AbortError' ? 'Сервер не ответил.' : error.message, true);
    } finally {
      busy = false;
      if (latest) paint(latest);
    }
  }
  function confirmAction(text, name) {
    if (tg && typeof tg.showConfirm === 'function') {
      tg.showConfirm(text, yes => { if (yes) action(name, undefined, true); });
    } else if (window.confirm(text)) {
      action(name, undefined, true);
    }
  }
  const selectAction = (id, name) => {
    document.getElementById(id).addEventListener('change', event => {
      if (!latest) return;
      action(name, Number(event.target.value));
    });
  };
  document.getElementById('demo-toggle').addEventListener('click', () => action('demo_toggle'));
  selectAction('demo-trigger-select', 'demo_trigger');
  document.getElementById('demo-stake-save').addEventListener('click', () =>
    action('demo_stake', document.getElementById('demo-stake-input').value));
  document.getElementById('demo-reset').addEventListener('click', () =>
    confirmAction('Сбросить DEMO? Баланс станет $10,000.00. История циклов, максимальные шаги и статистика DEMO будут удалены.', 'demo_reset'));
  document.getElementById('settings-notifications').addEventListener('click', () => action('notifications_toggle'));
  document.getElementById('settings-sound').addEventListener('click', () => action('sound_toggle'));
  selectAction('settings-threshold', 'notification_threshold');
  selectAction('settings-history-size', 'history_size');
  selectAction('settings-delete-delay', 'delete_delay');
  document.getElementById('settings-reset').addEventListener('click', () =>
    confirmAction('Вернуть настройки по умолчанию? DEMO сохранится.', 'settings_reset'));
  document.getElementById('settings-test-notification').addEventListener('click', () =>
    action('test_notification'));
  window.openControlsTab = () => load();
  window.clearControlsData = () => {
    latest = null; loadedAt = 0;
    demoPanel.hidden = true; settingsPanel.hidden = true;
    demoStatus.textContent = 'Нет доступа';
    settingsStatus.textContent = 'Нет доступа';
  };
})();
