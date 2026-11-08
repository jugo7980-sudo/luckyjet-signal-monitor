/* Authenticated, virtual-only DEMO controls and Telegram notification settings. */
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const demoPanel = document.getElementById('demo-controls');
  const settingsPanel = document.getElementById('settings-controls');
  const demoStatus = document.getElementById('demo-controls-status');
  const settingsStatus = document.getElementById('settings-controls-status');
  let busy = false;
  let generation = 0;
  let loadedAt = 0;
  let latest = null;
  const all = [...document.querySelectorAll('[data-control]')];
  const highPanel = document.getElementById('high-gap-panel');
  const targetPanel = document.getElementById('target-streak-panel');
  const recoveryPanel = document.getElementById('recovery-streak-panel');
  const targetHelpToggle = document.getElementById('target-streak-help-toggle');
  targetHelpToggle?.addEventListener('click', () => {
    const expanded = targetHelpToggle.getAttribute('aria-expanded') === 'true';
    targetHelpToggle.setAttribute('aria-expanded', String(!expanded));
    document.getElementById('target-streak-help-body').hidden = expanded;
  });
  const recoveryHelpToggle = document.getElementById('recovery-streak-help-toggle');
  recoveryHelpToggle?.addEventListener('click', () => {
    const expanded = recoveryHelpToggle.getAttribute('aria-expanded') === 'true';
    recoveryHelpToggle.setAttribute('aria-expanded', String(!expanded));
    document.getElementById('recovery-streak-help-body').hidden = expanded;
  });
  const highHelpToggle = document.getElementById('high-gap-help-toggle');
  highHelpToggle?.addEventListener('click', () => {
    const expanded = highHelpToggle.getAttribute('aria-expanded') === 'true';
    highHelpToggle.setAttribute('aria-expanded', String(!expanded));
    document.getElementById('high-gap-help-body').hidden = expanded;
  });
  function renderHighGap(high, currency = 'USD', balanceVerified = true) {
    high = high || {};
    const phase = high.phase || 'waiting_reference';
    const enabled = Boolean(high.enabled);
    const state = phase === 'paused_gap' ? 'Остановлено: пропуск данных'
      : phase === 'paused_funds' ? 'Остановлено: недостаточно баланса'
      : !enabled ? 'Выключена'
      : phase === 'waiting_reference' ? 'Ожидает коэффициент выше x' + high.reference
      : phase === 'waiting' ? 'Ожидает ' + high.remaining + ' раундов'
      : 'Делает фиксированные виртуальные ставки каждый раунд';
    const compact = phase === 'paused_gap' ? 'Пауза · разрыв архива'
      : phase === 'paused_funds' ? 'Пауза · баланс'
      : !enabled ? 'Выключена'
      : phase === 'waiting_reference' ? 'Ожидает опорный X'
      : phase === 'waiting' ? 'Ждёт ' + (high.remaining ?? '—') + ' раундов'
      : 'Ставки · ' + (high.steps ?? 0);
    document.getElementById('demo-high-preview').textContent = compact;
    // Detailed strategy state keeps its original precise wording for existing integrations.
    document.getElementById('high-gap-state').textContent = state;
    const filterStatus = high.filter_enabled && phase === 'waiting'
      ? ' · Фильтр >x' + high.filter_threshold + ': ' + (high.filter_count ?? 0) +
        ' / ' + high.filter_limit : '';
    const progress = high.filter_rejected && phase === 'waiting_reference'
      ? 'Сигнал отменён фильтром. Ожидание нового опорного X'
      : high.reference_value
      ? 'Последний >x' + high.reference + ': x' + high.reference_value +
        ' · Раундов после: ' + high.elapsed + ' / ' + high.wait_rounds +
        filterStatus : 'Нет подтверждённого опорного раунда';
    document.getElementById('high-gap-progress').textContent = progress;
    const money = window.formatDemoMoney;
    const output = high.steps ? ('Ставок: ' + high.steps + ' · Сумма: ' +
      money(high.total_stake_cents, currency)) : (high.last_outcome ? 'Последний итог: ' + high.last_outcome : '');
    document.getElementById('high-gap-outcome').textContent =
      (balanceVerified === false ? 'Общий баланс не подтверждён. ' : '') + output;
    document.getElementById('high-gap-state-detail').textContent =
      [state, progress, balanceVerified === false ? 'Баланс не подтверждён.' : '',
        output].filter(Boolean).join('. ');
  }
  window.renderHighGapSnapshot = renderHighGap;
  function renderTargetStreak(target = {}, currency = 'USD', balanceVerified = true) {
    const enabled = Boolean(target.enabled);
    const phase = target.phase || 'waiting';
    const x = target.target || '1.57';
    const summary = !enabled ? 'Выключена'
      : phase === 'paused_gap' ? 'Пауза · разрыв архива'
      : phase === 'paused_funds' ? 'Пауза · недостаточно баланса'
      : phase === 'betting' ? 'Ставки · ' + (target.steps ?? 0)
      : 'Серия <x' + x + ': ' + (target.streak ?? 0) + '/' + (target.trigger ?? 3);
    document.getElementById('demo-target-preview').textContent = summary;
    document.getElementById('target-streak-state').textContent = summary;
    const next = target.next_stake_cents ?? target.stake_cents ?? 0;
    document.getElementById('target-streak-progress').textContent = enabled
      ? 'Автовывод x' + x + ' · Следующая ставка: ' + window.formatDemoMoney(next, currency)
      : 'Автовывод x' + x;
    document.getElementById('target-streak-outcome').textContent = (balanceVerified === false
      ? 'Баланс не подтверждён. ' : '') +
      (target.last_outcome ? 'Последний цикл: ' + target.last_outcome : '');
    document.getElementById('target-streak-state-detail').textContent =
      'Старт после ' + (target.trigger ?? 3) + ' результатов подряд ниже x' + x +
      '. Первая ставка: ' + window.formatDemoMoney(target.stake_cents ?? 0, currency) +
      '. После проигрышей цель — вернуть все потери плюс первоначальную ставку. ' +
      'При x менее 2 первая успешная ставка приносит меньше первоначальной суммы.';
  }
  window.renderTargetStreakSnapshot = renderTargetStreak;
  function renderRecoveryStreak(recovery = {}, currency = 'USD', balanceVerified = true) {
    const enabled = Boolean(recovery.enabled);
    const phase = recovery.phase || 'waiting';
    const x = recovery.target || '1.32';
    const summary = !enabled ? 'Выключена'
      : phase === 'paused_gap' ? 'Пауза · разрыв архива'
      : phase === 'paused_funds' ? 'Пауза · недостаточно баланса'
      : phase === 'betting' ? 'Ставки · ' + (recovery.steps ?? 0)
      : 'Серия <x' + x + ': ' + (recovery.streak ?? 0) + '/' + (recovery.trigger ?? 3);
    document.getElementById('demo-recovery-preview').textContent = summary;
    document.getElementById('recovery-streak-state').textContent = summary;
    const next = recovery.next_stake_cents ?? recovery.stake_cents ?? 0;
    document.getElementById('recovery-streak-progress').textContent = enabled
      ? 'Автовывод x' + x + ' · Следующая ставка: ' + window.formatDemoMoney(next, currency)
      : 'Автовывод x' + x;
    document.getElementById('recovery-streak-outcome').textContent = (balanceVerified === false
      ? 'Баланс не подтверждён. ' : '') +
      (recovery.last_outcome ? 'Последний цикл: ' + recovery.last_outcome : '');
    document.getElementById('recovery-streak-state-detail').textContent =
      'Старт после ' + (recovery.trigger ?? 3) + ' результатов подряд ниже x' + x +
      '. Первая ставка: ' + window.formatDemoMoney(recovery.stake_cents ?? 0, currency) +
      '. После проигрыша снова ждём ' + (recovery.trigger ?? 3) +
      ' новых подряд ниже x' + x + '. Следующая ставка покрывает потери; ' +
      'дополнительной цели прибыли нет.';
  }
  window.renderRecoveryStreakSnapshot = renderRecoveryStreak;
  function message(text, error = false) {
    const target = !document.getElementById('page-demo').hidden ? demoStatus : settingsStatus;
    target.textContent = String(text || '');
    target.classList.toggle('control-error', error);
  }
  const probeArea = document.getElementById('demo-probe-area');
  const probeResult = document.getElementById('demo-probe-result');
  function showProbe(probe) {
    const labels = {
      not_checked: 'Проверка ещё не выполнена.',
      region_blocked: 'Сайт недоступен из региона сервера.',
      verification: 'Сайт запросил проверку браузера.',
      demo_available: 'Демо-кнопка найдена. Баланс не подтверждён.',
      game_loaded: 'Интерфейс игры загружен. Баланс не подтверждён.',
      unconfirmed: 'Деморежим не подтверждён.',
      timeout: 'Нет результата за 3 минуты.',
      error: 'Ошибка чтения страницы игры.'
    };
    if (!probe) { probeResult.textContent = 'Не проверено.'; return; }
    probeResult.textContent = probe.running ? 'Проверка выполняется. Ожидание страницы мониторинга.'
      : (labels[probe.state] || 'Статус неизвестен.');
    if (probe.checked_at) probeResult.textContent += ' · ' + probe.checked_at + ' UTC';
  }
  window.showDemoProbe = showProbe;
  function paint(data) {
    latest = data;
    window.renderDemoBalance?.(data.demo);
    const demo = data.demo, settings = data.settings;
    const quality = document.getElementById('demo-quality');
    quality.textContent = demo.balance_verified === false ? 'Не подтверждён'
      : demo.balance_verified === true ? 'По архиву' : 'Проверка';
    quality.dataset.state = demo.balance_verified === false ? 'warn'
      : demo.balance_verified === true ? 'good' : 'unknown';
    document.getElementById('demo-quality-help').textContent = demo.balance_verified === false
      ? 'Баланс DEMO не подтверждён: источник пропустил раунды. Точная сумма неизвестна.'
      : 'Виртуальный баланс рассчитан по доступным сохранённым раундам. Полнота истории самой игры не доказана.';
    const classicPreview = document.getElementById('demo-classic-preview');
    classicPreview.textContent = demo.balance_verified === false ? 'Баланс не подтверждён'
      : demo.active ? 'Активна · шаг ' + demo.step
      : demo.enabled ? 'Серия ' + demo.low_streak + ' из ' + demo.trigger
      : 'Выключена';
    demoPanel.hidden = false;
    highPanel.hidden = false;
    targetPanel.hidden = false;
    recoveryPanel.hidden = false;
    settingsPanel.hidden = false;
    probeArea.hidden = !data.admin;
    if (data.probe) showProbe(data.probe);
    const toggle = document.getElementById('demo-toggle');
    if (demo.balance_verified === false) {
      demoStatus.textContent = 'DEMO-баланс не подтверждён: источник пропустил раунды. Точная сумма неизвестна.';
      demoStatus.classList.add('control-error');
      demoStatus.dataset.archiveWarning = '1';
    } else if (demoStatus.dataset.archiveWarning) {
      delete demoStatus.dataset.archiveWarning;
      demoStatus.textContent = '';
      demoStatus.classList.remove('control-error');
    }
    toggle.textContent = demo.enabled ? 'Выключить DEMO' : 'Включить DEMO';
    document.getElementById('demo-trigger-select').value = String(demo.trigger);
    document.getElementById('demo-stake-input').value = (demo.base_stake_cents / 100).toFixed(demo.currency === 'KRW' ? 0 : 2);
    document.getElementById('demo-stake-label').textContent = 'Стартовая ставка, ' + (demo.currency || 'USD');
    document.getElementById('demo-low-streak').textContent =
      demo.active ? 'Цикл активен · шаг ' + demo.step :
        'Текущая серия <2x: ' + demo.low_streak + ' из ' + demo.trigger;
    const high = data.high_gap || {};
    renderHighGap(high, demo.currency, demo.balance_verified);
    const target = data.target_streak || {};
    renderTargetStreak(target, demo.currency, demo.balance_verified);
    document.getElementById('target-streak-toggle').textContent =
      target.enabled ? 'Выключить стратегию №3' : 'Включить стратегию №3';
    for (const [id, val] of Object.entries({
      'target-streak-x': target.target || '1.57',
      'target-streak-trigger': target.trigger ?? 3,
      'target-streak-stake': ((target.stake_cents ?? demo.base_stake_cents) / 100)
        .toFixed(demo.currency === 'KRW' ? 0 : 2),
    })) {
      const field = document.getElementById(id);
      if (document.activeElement !== field) field.value = String(val);
    }
    document.getElementById('target-streak-stake-label').textContent =
      'Первая ставка, ' + (demo.currency || 'USD');
    const recovery = data.recovery_streak || {};
    renderRecoveryStreak(recovery, demo.currency, demo.balance_verified);
    document.getElementById('recovery-streak-toggle').textContent =
      recovery.enabled ? 'Выключить стратегию №4' : 'Включить стратегию №4';
    for (const [id, val] of Object.entries({
      'recovery-streak-x': recovery.target || '1.32',
      'recovery-streak-trigger': recovery.trigger ?? 3,
      'recovery-streak-stake': ((recovery.stake_cents ?? demo.base_stake_cents) / 100)
        .toFixed(demo.currency === 'KRW' ? 0 : 2),
    })) {
      const field = document.getElementById(id);
      if (document.activeElement !== field) field.value = String(val);
    }
    document.getElementById('recovery-streak-stake-label').textContent =
      'Первая ставка, ' + (demo.currency || 'USD');
    document.getElementById('high-gap-toggle').textContent = high.enabled ?
      'Выключить стратегию №2' : 'Включить стратегию №2';
    const fields = {
      'high-gap-reference': high.reference || '100',
      'high-gap-wait': high.wait_rounds ?? 100,
      'high-gap-filter-threshold': high.filter_threshold || '10',
      'high-gap-filter-limit': high.filter_limit ?? '3',
      'high-gap-target': high.target || '100',
      'high-gap-stake': ((high.stake_cents ?? demo.base_stake_cents) / 100)
        .toFixed(demo.currency === 'KRW' ? 0 : 2),
    };
    for (const [id, val] of Object.entries(fields)) {
      const field = document.getElementById(id);
      if (document.activeElement !== field) field.value = String(val);
    }
    const filterCheckbox = document.getElementById('high-gap-filter-enabled');
    if (document.activeElement !== filterCheckbox) {
      filterCheckbox.checked = high.filter_enabled === true;
    }
    const highStakeLabel = document.getElementById('high-gap-stake-label');
    // Keep the help button: do not rebuild the label on every control refresh.
    const stakeText = highStakeLabel.firstChild;
    const caption = 'Ставка, ' + (demo.currency || 'USD') + ' ';
    if (stakeText && stakeText.nodeType === 3) stakeText.textContent = caption;
    else highStakeLabel.prepend(document.createTextNode(caption));
    document.getElementById('settings-notifications').textContent = settings.notifications ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('low-streak-notice-preview').textContent = settings.notifications ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-sound').textContent = settings.sound ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-threshold').value = String(settings.threshold);
    document.getElementById('settings-history-size').value = String(settings.history_size);
    document.getElementById('settings-delete-delay').value = String(settings.delete_delay);
    const highNotices = data.high_gap_notifications || {};
    const highEvents = highNotices.events || {};
    const highEnabled = highNotices.enabled !== false;
    document.getElementById('high-gap-notice-preview').textContent = highEnabled ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-high-enabled').textContent = highEnabled ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-high-sound').textContent = highNotices.sound !== false ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-high-delete').value = String(highNotices.delete_delay ?? 10);
    for (const [name, fallback] of Object.entries({reference:false, threshold:true, betting:true, goal:true})) {
      document.getElementById('settings-high-' + name).textContent =
        (highEvents[name] ?? fallback) ? 'ВКЛ' : 'ВЫКЛ';
    }
    const targetPrefs = data.target_streak_notifications || {};
    const targetEvents = targetPrefs.events || {};
    const targetEnabled = targetPrefs.enabled !== false;
    document.getElementById('target-streak-notice-preview').textContent = targetEnabled ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-target-enabled').textContent = targetEnabled ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-target-sound').textContent = targetPrefs.sound !== false ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-target-delete').value = String(targetPrefs.delete_delay ?? 10);
    for (const [name, fallback] of Object.entries({armed:true,betting:true,loss:false,goal:true,stopped:true})) {
      document.getElementById('settings-target-' + name).textContent =
        (targetEvents[name] ?? fallback) ? 'ВКЛ' : 'ВЫКЛ';
    }
    const recoveryPrefs = data.recovery_streak_notifications || {};
    const recoveryEvents = recoveryPrefs.events || {};
    const recoveryEnabled = recoveryPrefs.enabled !== false;
    document.getElementById('recovery-streak-notice-preview').textContent = recoveryEnabled ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-recovery-enabled').textContent = recoveryEnabled ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-recovery-sound').textContent = recoveryPrefs.sound !== false ? 'ВКЛ' : 'ВЫКЛ';
    document.getElementById('settings-recovery-delete').value = String(recoveryPrefs.delete_delay ?? 10);
    for (const [name, fallback] of Object.entries({armed:true,betting:true,loss:false,goal:true,stopped:true})) {
      document.getElementById('settings-recovery-' + name).textContent =
        (recoveryEvents[name] ?? fallback) ? 'ВКЛ' : 'ВЫКЛ';
    }
    for (const node of all) node.disabled = busy || (
      demo.active && (node.id === 'demo-trigger-select' || node.id === 'demo-stake-input' ||
        node.id === 'demo-stake-save' || (node.id === 'demo-toggle' && demo.enabled))
    ) || (high.active && ['high-gap-reference', 'high-gap-wait',
      'high-gap-filter-enabled', 'high-gap-filter-threshold', 'high-gap-filter-limit',
      'high-gap-target', 'high-gap-stake', 'high-gap-save'].includes(node.id)) ||
      (target.active && ['target-streak-x', 'target-streak-trigger',
        'target-streak-stake', 'target-streak-save'].includes(node.id)) ||
      (recovery.active && ['recovery-streak-x', 'recovery-streak-trigger',
        'recovery-streak-stake', 'recovery-streak-save'].includes(node.id));
  }
  async function request(method = 'GET', payload) {
    if (!window.canUsePremium?.()) throw new Error('Требуется Premium.');
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 8000);
    try {
      const result = await fetch('/api/controls', {
        method,
        headers: {...(tg?.initData ? {Authorization: 'tma ' + tg.initData} : {}),
          ...(method === 'POST' ? {'Content-Type': 'application/json'} : {})},
        ...(method === 'POST' ? {body: JSON.stringify(payload)} : {}),
        credentials: 'same-origin', cache: 'no-store', signal: abort.signal,
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
    const currentGeneration = generation;
    busy = true;
    message('Загрузка настроек…');
    if (latest) paint(latest);
    try {
      const data = await request();
      if (generation !== currentGeneration || !window.canUsePremium?.()) return;
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
      if (generation === currentGeneration && latest && window.canUsePremium?.()) paint(latest);
    }
  }
  async function action(name, value, confirm = false) {
    if (busy) return;
    const currentGeneration = generation;
    busy = true;
    window.demoViewEpoch++;
    message('Сохранение…');
    if (latest) paint(latest);
    try {
      const data = await request('POST', {action: name, ...(value === undefined ? {} : {value}), ...(confirm ? {confirm: true} : {})});
      if (generation !== currentGeneration || !window.canUsePremium?.()) return;
      window.demoViewEpoch++;
      loadedAt = Date.now();
      paint(data);
      message(data.message || 'Сохранено.');
      if (name === 'demo_reset') {
        resetModal.hidden = true;
        document.getElementById('demo-reset').focus();
        if (window.resetHistoryCache) window.resetHistoryCache();
      }
      if (typeof loadOverview === 'function') loadOverview();
    } catch (error) {
      message(error.name === 'AbortError' ? 'Сервер не ответил.' : error.message, true);
    } finally {
      busy = false;
      if (generation === currentGeneration && latest && window.canUsePremium?.()) paint(latest);
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
  document.getElementById('demo-probe-start').addEventListener('click', () => action('demo_probe_start'));
  document.getElementById('high-gap-toggle').addEventListener('click', () => action('high_gap_toggle'));
  document.getElementById('high-gap-save').addEventListener('click', () => {
    const reference = document.getElementById('high-gap-reference').value.trim();
    const target = document.getElementById('high-gap-target').value.trim();
    const waitText = document.getElementById('high-gap-wait').value.trim();
    const stake = document.getElementById('high-gap-stake').value.trim();
    const filter_enabled = document.getElementById('high-gap-filter-enabled').checked;
    const filter_threshold = document.getElementById('high-gap-filter-threshold').value.trim();
    const filter_limit = document.getElementById('high-gap-filter-limit').value.trim();
    if (!/^\d{1,6}$/.test(waitText) || !reference || !target || !stake ||
        !filter_threshold || !/^[0-9]+$/.test(filter_limit) || !/[1-9]/.test(filter_limit)) {
      message('Проверь коэффициенты, число раундов и фильтр входа.', true); return;
    }
    action('high_gap_config', {reference, wait_rounds: Number(waitText), target, stake,
      filter_enabled, filter_threshold, filter_limit});
  });
  document.getElementById('target-streak-toggle').addEventListener('click', () =>
    action('target_streak_toggle'));
  document.getElementById('target-streak-save').addEventListener('click', () => {
    const target = document.getElementById('target-streak-x').value.trim();
    const trigger = Number(document.getElementById('target-streak-trigger').value);
    const stake = document.getElementById('target-streak-stake').value.trim();
    if (!target || !stake || !Number.isInteger(trigger) || trigger < 2 || trigger > 10) {
      message('Проверь целевой X, серию и первую ставку.', true); return;
    }
    action('target_streak_config', {target, trigger, stake});
  });
  document.getElementById('recovery-streak-toggle').addEventListener('click', () =>
    action('recovery_streak_toggle'));
  document.getElementById('recovery-streak-save').addEventListener('click', () => {
    const target = document.getElementById('recovery-streak-x').value.trim();
    const trigger = Number(document.getElementById('recovery-streak-trigger').value);
    const stake = document.getElementById('recovery-streak-stake').value.trim();
    if (!target || !stake || !Number.isInteger(trigger) || trigger < 2 || trigger > 10) {
      message('Проверь целевой X, серию и первую ставку.', true); return;
    }
    action('recovery_streak_config', {target, trigger, stake});
  });
  const resetModal = document.getElementById('demo-reset-modal');
  const resetForm = document.getElementById('demo-reset-form');
  const resetCurrency = document.getElementById('demo-reset-currency');
  const resetBalance = document.getElementById('demo-reset-balance');
  const resetStake = document.getElementById('demo-reset-stake');
  const resetPreview = document.getElementById('demo-reset-preview');
  let resetRevision = null;
  function resetAmount(raw, currency) {
    const text = raw.trim().replace(',', '.');
    const pattern = currency === 'KRW' ? /^[0-9]+$/ : /^[0-9]+(?:\.[0-9]{1,2})?$/;
    if (!pattern.test(text)) return null;
    const amount = Math.round(Number(text) * 100);
    return Number.isSafeInteger(amount) && amount >= (currency === 'USD' ? 1 : 100) && amount <= 100000000000000 ? amount : null;
  }
  function previewReset() {
    const currency = resetCurrency.value;
    const balance = resetAmount(resetBalance.value, currency), stake = resetAmount(resetStake.value, currency);
    const valid = balance !== null && stake !== null && stake <= balance;
    document.getElementById('demo-reset-submit').disabled = busy || !valid;
    resetPreview.textContent = valid
      ? 'Баланс: ' + window.formatDemoMoney(balance, currency) + ' · Ставка: ' + window.formatDemoMoney(stake, currency) + ' · ' + currency
      : 'Введи баланс и ставку. Ставка не выше баланса. Воны — целые, рубли и доллары — до двух знаков после запятой.';
  }
  document.getElementById('demo-reset').addEventListener('click', () => {
    if (!latest || busy) return;
    resetCurrency.value = latest.demo.currency || 'USD';
    resetBalance.value = ''; resetStake.value = '';
    resetRevision = latest.demo.reset_revision || 'legacy';
    resetModal.hidden = false; previewReset(); resetCurrency.focus();
  });
  document.getElementById('demo-reset-cancel').addEventListener('click', () => {
    if (busy) return;
    resetModal.hidden = true; document.getElementById('demo-reset').focus();
  });
  resetForm.addEventListener('input', previewReset);
  resetForm.addEventListener('change', previewReset);
  resetForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || resetAmount(resetBalance.value, resetCurrency.value) === null || resetAmount(resetStake.value, resetCurrency.value) === null) return;
    document.getElementById('demo-reset-submit').disabled = true;
    await action('demo_reset', {currency: resetCurrency.value, balance: resetBalance.value.trim(), stake: resetStake.value.trim(), revision: resetRevision}, true);
    previewReset();
  });
  resetModal.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !busy) document.getElementById('demo-reset-cancel').click();
    if (event.key === 'Tab') {
      const nodes = [...resetForm.querySelectorAll('select,input,button')].filter(n => !n.disabled);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  document.getElementById('settings-high-enabled').addEventListener('click', () => action('high_gap_notice_toggle'));
  document.getElementById('settings-high-sound').addEventListener('click', () => action('high_gap_notice_sound'));
  selectAction('settings-high-delete', 'high_gap_notice_delete_delay');
  for (const event of ['reference','threshold','betting','goal']) {
    document.getElementById('settings-high-' + event).addEventListener(
      'click', () => action('high_gap_notice_event', event));
  }
  document.getElementById('settings-high-test').addEventListener('click', () => action('high_gap_notice_test'));
  document.getElementById('settings-target-enabled').addEventListener('click', () => action('target_notice_toggle'));
  document.getElementById('settings-target-sound').addEventListener('click', () => action('target_notice_sound'));
  selectAction('settings-target-delete', 'target_notice_delete_delay');
  for (const event of ['armed','betting','loss','goal','stopped']) {
    document.getElementById('settings-target-' + event).addEventListener(
      'click', () => action('target_notice_event', event));
  }
  document.getElementById('settings-target-test').addEventListener('click', () => action('target_notice_test'));
  document.getElementById('settings-recovery-enabled').addEventListener('click', () => action('recovery_notice_toggle'));
  document.getElementById('settings-recovery-sound').addEventListener('click', () => action('recovery_notice_sound'));
  selectAction('settings-recovery-delete', 'recovery_notice_delete_delay');
  for (const event of ['armed','betting','loss','goal','stopped']) {
    document.getElementById('settings-recovery-' + event).addEventListener(
      'click', () => action('recovery_notice_event', event));
  }
  document.getElementById('settings-recovery-test').addEventListener('click', () => action('recovery_notice_test'));
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
    generation++;
    latest = null; loadedAt = 0;
    resetModal.hidden = true;
    demoPanel.hidden = true; highPanel.hidden = true; targetPanel.hidden = true; recoveryPanel.hidden = true; settingsPanel.hidden = true;
    probeArea.hidden = true;
    demoStatus.textContent = 'Нет доступа';
    settingsStatus.textContent = 'Нет доступа';
  };
})();
