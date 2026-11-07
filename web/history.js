/* Read-only history views for Telegram Mini App. All data comes from the user's authenticated API. */
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const root = document.getElementById('history-content');
  const status = document.getElementById('history-status');
  const pager = document.getElementById('history-pager');
  const back = document.getElementById('history-back');
  const refresh = document.getElementById('history-refresh');
  const tabs = [...document.querySelectorAll('[data-history-view]')];
  const PAGE_SIZE = 10;
  const LIMITS = [2, 5, 10, 25, 50, 100, 200, 300, 500, 800, 1000, 1500, 2000, 3000, 5000];
  let current = null;
  let args = {view: 'days', page: 0};
  let stack = [];
  let requestNo = 0;
  let lastLoad = 0;

  const el = (tag, className = '', text = '') => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = String(text);
    return node;
  };
  const money = cents => Number.isFinite(Number(cents))
    ? new Intl.NumberFormat('ru-RU', {style: 'currency', currency: 'USD'}).format(Number(cents) / 100) : 'Нет данных';
  const signedMoney = cents => cents == null ? 'Нет данных' : (Number(cents) > 0 ? '+' : '') + money(cents);
  const formatDay = day => /^\d{4}-\d{2}-\d{2}$/.test(String(day))
    ? String(day).slice(8, 10) + '.' + String(day).slice(5, 7) + '.' + String(day).slice(0, 4) : 'Дата неизвестна';
  const formatTime = timestamp => {
    if (!timestamp) return 'Время не сохранено';
    const raw = typeof timestamp === 'number' ? timestamp * 1000 : Date.parse(timestamp);
    if (!Number.isFinite(raw)) return 'Время не сохранено';
    return new Date(raw).toLocaleString('ru-RU', {timeZone: 'Asia/Vladivostok', day: '2-digit',
      month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'});
  };
  function info(label, value, hint) {
    const row = el('div', 'history-metric');
    const left = el('span', '', label);
    if (hint) left.appendChild(el('small', '', hint));
    row.append(left, el('strong', '', value));
    root.appendChild(row);
  }
  function heading(value, detail = '') {
    root.appendChild(el('h2', 'history-heading', value));
    if (detail) root.appendChild(el('p', 'history-subtitle', detail));
  }
  function note(value) { root.appendChild(el('p', 'history-note', value)); }
  function item(title, subtitle, action, extra) {
    const button = el('button', 'history-item');
    button.type = 'button';
    const label = el('span', 'history-item-label');
    label.appendChild(el('strong', '', title));
    if (subtitle) label.appendChild(el('small', '', subtitle));
    button.appendChild(label);
    if (extra) button.appendChild(el('span', 'history-item-extra', extra));
    if (action) {
      button.appendChild(el('span', 'history-chevron', '›'));
      button.addEventListener('click', action);
    } else button.disabled = true;
    root.appendChild(button);
  }
  function setTabs() {
    const active = ['days', 'day'].includes(args.view) ? 'days'
      : ['high_days', 'high_day', 'high_item'].includes(args.view) ? 'high_days'
      : ['cycles', 'cycle', 'demo_stats'].includes(args.view) ? 'cycles' : 'max_steps';
    for (const button of tabs) button.setAttribute('aria-selected', String(button.dataset.historyView === active));
  }
  function setPager(data) {
    const page = Number(data.page || 0);
    const total = Number(data.total || 0);
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const visible = ['days', 'high_days', 'high_day', 'cycles', 'max_steps'].includes(data.view);
    pager.hidden = !visible;
    if (!visible) return;
    document.getElementById('history-page-number').textContent = (page + 1) + ' / ' + pages;
    document.getElementById('history-prev').disabled = page <= 0;
    document.getElementById('history-next').disabled = page + 1 >= pages;
  }
  function datePicker(view) {
    const wrap = el('div', 'history-date-row');
    const input = el('input');
    input.type = 'date';
    input.id = 'history-date';
    input.max = new Intl.DateTimeFormat('sv-SE', {timeZone: 'Asia/Vladivostok', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date());
    const button = el('button', 'history-date-button', 'Показать');
    button.type = 'button';
    button.addEventListener('click', () => {
      if (!input.value || input.value > input.max) { status.textContent = 'Выбери корректную дату.'; return; }
      navigate(view === 'days' ? 'day' : view === 'max_steps' ? 'max_step_day' : 'high_day', {day: input.value}, true);
    });
    wrap.append(input, button);
    root.appendChild(wrap);
  }
  function render(data) {
    root.replaceChildren();
    current = data;
    setTabs();
    back.hidden = stack.length === 0;
    setPager(data);
    switch (data.view) {
      case 'days':
        heading('История Кэф', 'Статистика по дням · Владивосток, UTC+10');
        datePicker('days');
        if (!data.days.length) note('Пока нет сохранённых дней.');
        for (const day of data.days) item(formatDay(day), 'Коэффициенты и количество раундов',
          () => navigate('day', {day}, true));
        break;
      case 'day': {
        const s = data.summary || {};
        heading(formatDay(data.day), 'История Кэф · Владивосток, UTC+10');
        info('Всего раундов', s.total ?? 0);
        info('<2x', s.lt2_tracking ? (s.lt2 ?? 0) : 'Нет данных',
          s.lt2_partial ? 'С обновления' : '');
        for (const t of LIMITS) {
          const recent = t === 2 || t === 5;
          const enabled = !recent || Boolean(s.new_threshold_tracking);
          info('>' + t + 'x', enabled ? (s[String(t)] ?? 0) : 'Нет данных',
            recent && s.new_threshold_partial ? 'С обновления' : '');
        }
        const check = data.integrity;
        if (check) {
          info('Раундов в независимом архиве', check.archived_rounds ?? 0);
          if (check.status === 'verified') {
            note('Сверка успешна: число раундов совпадает, разрывов истории не обнаружено.');
          } else if (check.status === 'sequence_checked') {
            note('Число раундов совпадает с архивом, разрывов не обнаружено. ' +
              'Время — по первому получению: источник не сообщает точное окончание раунда.');
          } else {
            note('Полнота и точность статистики не подтверждены. ' +
              ((check.reasons || []).join('; ') || 'Недостаточно данных для проверки.') + '.');
          }
        } else if (s.partial || s.time_uncertain) {
          note('Статистика может быть неполной.');
        }
        break;
      }
      case 'high_days':
        heading('Интервалы >100x', 'Число раундов между коэффициентами выше 100x');
        if (data.last_high) info('Последний >100x', String(data.last_high.value) + 'x',
          formatTime(data.last_high.stamp));
        info('Раундов после >100x', data.gap ? 'Неполные данные' : data.since_high);
        datePicker('high_days');
        if (!data.days.length) note('Сохранённых интервалов пока нет.');
        for (const day of data.days) item(formatDay(day), 'Посмотреть интервалы',
          () => navigate('high_day', {day}, true));
        break;
      case 'high_day':
        heading(formatDay(data.day), 'Интервалы >100x · новые сначала');
        if (!data.items.length) note('За этот день нет сохранённых интервалов.');
        for (const entry of data.items) {
          const count = entry.rounds == null ? 'Данные неполные' : entry.rounds + ' раундов';
          item(String(entry.value) + 'x', count + ' · ' + formatTime(entry.stamp),
            () => navigate('high_item', {id: entry.id}, true));
        }
        break;
      case 'high_item': {
        const e = data.item;
        heading(String(e.value) + 'x', formatTime(e.stamp));
        info('Интервал', e.rounds == null ? 'Нет полных данных' : e.rounds + ' раундов');
        if (e.previous) info('Предыдущий >100x', String(e.previous.value) + 'x',
          formatTime(e.previous.stamp));
        if (e.uncertain) note('Время события приблизительное.');
        break;
      }
      case 'cycles':
        heading('История DEMO', data.total + ' циклов · новые сначала');
        item('Статистика DEMO', 'Результаты по последним сессиям',
          () => navigate('demo_stats', {}, true));
        if (!data.items.length) note('Завершённых циклов пока нет.');
        for (const c of data.items) {
          const result = c.outcome === 'incomplete_data' || c.net_cents == null
            ? 'Нет полных данных' : signedMoney(c.net_cents);
          item(c.steps + ' шаг. · ' + formatTime(c.ended_at || c.started_at),
            'Цикл ' + (c.index + 1), () => navigate('cycle', {index: c.index}, true), result);
        }
        break;
      case 'cycle': {
        const c = data.item;
        const reasons = {recovered: 'Цель 2x достигнута', insufficient_balance: 'Недостаточно баланса',
          incomplete_data: 'Неполные данные', stopped_by_user: 'Остановлена пользователем'};
        heading('Цикл ' + (c.index + 1), 'Время Владивостока, UTC+10');
        info('Начало', formatTime(c.started_at));
        info('Завершение', formatTime(c.ended_at));
        info('Шагов', c.steps);
        info('Результат', reasons[c.outcome] || 'Неизвестно');
        (c.rounds || []).forEach((r, i) => {
          const amount = r.payout_cents ? Number(r.payout_cents) - Number(r.stake_cents)
            : -Number(r.stake_cents);
          info('Шаг ' + (i + 1) + ' · ' + r.coefficient + 'x', signedMoney(amount));
        });
        info('Всего поставлено', money(c.total_stake_cents));
        info('Выплата', c.payout_cents == null ? 'Нет данных' : money(c.payout_cents));
        info('Чистый итог', signedMoney(c.net_cents));
        info('Баланс до', money(c.balance_before_cents));
        info('Баланс после', money(c.balance_after_cents));
        break;
      }
      case 'demo_stats': {
        const s = data.stats;
        heading('Статистика DEMO', 'По последним ' + s.total + ' из ' + s.maximum + ' циклов');
        info('Всего сессий', s.total);
        info('Успешных', s.recovered);
        info('Проигрышных', s.lost);
        info('Неполных', s.incomplete);
        info('Остановленных', s.stopped);
        info('С последнего сброса', signedMoney(s.since_reset_cents));
        info('По истории', signedMoney(s.history_net_cents));
        info('Среднее шагов', s.average_steps);
        break;
      }
      case 'max_steps':
        heading('Максимальный шаг', 'По дням · Владивосток, UTC+10');
        datePicker('max_steps');
        if (!data.days.length) note('Данных пока нет.');
        for (const d of data.days) item(formatDay(d.day), 'Показать максимум', 
          () => navigate('max_step_day', {day: d.day}, true), d.steps + ' шаг.');
        break;
      case 'max_step_day':
        heading(formatDay(data.day), 'Максимальный шаг · Владивосток, UTC+10');
        if (data.steps == null) note('Нет сохранённых данных за этот день.');
        else info('Максимальный шаг', data.steps);
        break;
    }
  }
  async function navigate(view, extra = {}, push = false) {
    if (push && current) stack.push({data: current, args: {...args}});
    args = {view, page: 0, ...extra};
    back.hidden = stack.length === 0;
    root.replaceChildren();
    pager.hidden = true;
    setTabs();
    const seq = ++requestNo;
    status.textContent = 'Загрузка истории…';
    if (!window.canUsePremium?.()) { status.textContent = 'Требуется Premium.'; return; }
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 8000);
    try {
      const url = '/api/history?' + new URLSearchParams(args);
      const response = await fetch(url, {
        headers: {...(tg?.initData ? {Authorization: 'tma ' + tg.initData} : {})}, credentials: 'same-origin',
        cache: 'no-store', signal: abort.signal,
      });
      const result = await response.json();
      if (seq !== requestNo) return;
      if (!response.ok) throw new Error(result.error || 'Ошибка загрузки истории.');
      lastLoad = Date.now();
      status.textContent = '';
      render(result);
    } catch (error) {
      if (seq !== requestNo) return;
      current = null;
      status.textContent = error.name === 'AbortError'
        ? 'Сервер не ответил. Нажми «Обновить».' : String(error.message);
    } finally {
      clearTimeout(timeout);
    }
  }
  function goBack() {
    if (!stack.length) return false;
    ++requestNo;
    const previous = stack.pop();
    args = previous.args;
    status.textContent = '';
    render(previous.data);
    return true;
  }
  for (const button of tabs) button.addEventListener('click', () => {
    stack = []; current = null; navigate(button.dataset.historyView);
  });
  back.addEventListener('click', goBack);
  refresh.addEventListener('click', () => navigate(args.view, {...args}));
  document.getElementById('history-prev').addEventListener('click', () =>
    navigate(args.view, {...args, page: Math.max(0, Number(args.page || 0) - 1)}));
  document.getElementById('history-next').addEventListener('click', () =>
    navigate(args.view, {...args, page: Number(args.page || 0) + 1}));
  window.openHistoryTab = () => {
    if (!current || Date.now() - lastLoad > 30000) navigate(args.view, {...args});
  };
  window.historyBackInApp = goBack;
  window.resetHistoryCache = () => {
    ++requestNo; current = null; stack = []; lastLoad = 0;
    root.replaceChildren(); pager.hidden = true; back.hidden = true;
    status.textContent = '';
  };
  window.clearHistoryData = () => {
    window.resetHistoryCache();
    status.textContent = 'Нет доступа';
  };
})();
