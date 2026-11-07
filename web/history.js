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
  const archiveTabs = [...document.querySelectorAll('[data-archive-view]')];
  const isArchive = view => String(view).startsWith('archive_');
  const viewFor = view => isArchive(args.view) ? 'archive_' + view : view;
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
  let currency = 'USD';
  const money = cents => cents != null && Number.isFinite(Number(cents))
    ? window.formatDemoMoney(cents, currency) : 'Нет данных';
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
  function pollCoverage(check) {
    const c = check?.poll_coverage;
    if (!c || c.status === 'not_recorded') {
      note('Журнал успешных опросов за эту дату отсутствует. Проверка перерывов до включения контроля недоступна.');
      return;
    }
    info('Минуты успешных опросов', c.observed_minutes + ' из ' + c.expected_minutes,
      'Подтверждается хотя бы одним сборщиком');
    info('Основной / резервный', c.primary_minutes + ' / ' + c.backup_minutes + ' мин');
    info('Минуты без подтверждённых опросов', c.missing_minutes ?? 'Нет данных');
    if (c.longest_missing_minutes) {
      info('Самый длинный перерыв', c.longest_missing_minutes + ' мин');
      for (const interval of c.missing_intervals || [])
        note('Без подтверждения опроса: ' + interval.start + '–' + interval.end +
          ' (' + interval.minutes + ' мин).');
    }
    if (c.status === 'full_poll_coverage') {
      note('Успешные обращения к источнику зарегистрированы в каждую минуту суток. ' +
        'Это не доказывает, что источник отдал все раунды.');
    } else if (c.status === 'poll_gaps') {
      note('Есть минуты без подтверждённого получения истории. Это возможные пропуски; ' +
        'число отсутствующих раундов неизвестно.');
    } else {
      note('Период контроля неполный: день ещё продолжается или мониторинг включили посреди суток.');
    }
  }
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
    const archive = isArchive(args.view);
    const view = archive ? args.view.slice(8) : args.view;
    const active = ['days', 'day'].includes(view) ? 'days'
      : ['high_days', 'high_day', 'high_item'].includes(view) ? 'high_days'
      : ['cycles', 'cycle', 'demo_stats', 'high_cycles', 'high_cycle'].includes(view) ? 'cycles' : 'max_steps';
    document.getElementById('history-live-tabs').hidden = archive;
    document.getElementById('history-archive-tabs').hidden = !archive;
    document.getElementById('history-archive-exit').hidden = !archive;
    document.getElementById('history-reset-panel').hidden = archive;
    document.getElementById('history-archive-launch').hidden = archive || args.view !== 'days';
    for (const button of tabs)
      button.setAttribute('aria-selected', String(button.dataset.historyView === active));
    for (const button of archiveTabs)
      button.setAttribute('aria-selected', String(button.dataset.archiveView === 'archive_' + active));
  }
  function setPager(data) {
    const page = Number(data.page || 0);
    const total = Number(data.total || 0);
    const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    const visible = ['days', 'high_days', 'high_day', 'cycles', 'high_cycles', 'max_steps'].includes(isArchive(data.view) ? data.view.slice(8) : data.view);
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
      navigate(view === 'archive' ? 'archive' : view === 'days' ? viewFor('day') : view === 'max_steps' ? viewFor('max_step_day') : view === 'cycles' ? viewFor('cycles') : viewFor('high_day'), {day: input.value}, true);
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
    const archive = isArchive(data.view);
    const view = archive ? data.view.slice(8) : data.view;
    switch (view) {
      case 'archive': {
        heading('Фактический архив', 'Полученные от источника записи · Владивосток, UTC+10');
        datePicker('archive');
        info('Выбранная дата', formatDay(data.day));
        if (data.aggregate) {
          const a = data.aggregate;
          info('Раундов по архиву', a.total);
          info('<2x', a.lt2);
          for (const t of LIMITS) info('>' + t + 'x', a[String(t)] ?? 0);
          info('Максимальная серия <2x', data.max_low_streak ?? 'Нет данных');
          note('Подсчёт по времени получения записей, а не по неподтверждённому времени игры.');
        } else if (data.truncated) note('Слишком много событий для полного подсчёта за один запрос. Записи ниже доступны постранично.');
        if (data.integrity) {
          const e = data.integrity;
          info('Записано раундов', e.archived_rounds ?? 'Нет данных');
          info('Разрывы в архиве', e.gaps ?? 'Нет данных');
          pollCoverage(e);
          note(e.status === 'verified' ? 'Сохранённые записи согласуются по времени и счётчикам; полнота истории самой игры не доказана.' :
            'Полнота данных источника не гарантирована. ' + ((e.reasons || []).join('; ') || 'Данные ещё не проверены.') + '.');
        }
        if (!data.items.length) note('За выбранную дату нет записей в архиве.');
        for (const record of data.items) {
          const received = Number(record.observed_at);
          const time = Number.isFinite(received) && received > 0 ? formatTime(received / 1000) : 'Время неизвестно';
          const recorded = record.type === 'round';
          const title = recorded ? String(record.coefficient) + 'x' :
            record.type === 'gap' ? 'Разрыв истории' : 'Начало сбора';
          const details = time + ' · ' + (recorded ? 'Раунд ' + record.round_id : 'Служебная запись');
          item(title, details, null, record.source_time ? 'Время игры: ' + record.source_time : 'Время получения');
        }
        if (data.next_cursor) {
          const more = el('button', 'history-outline', 'Показать более ранние записи');
          more.type = 'button';
          more.style.width = '100%';
          more.style.marginTop = '14px';
          more.addEventListener('click', () => navigate('archive', {day: data.day, cursor: data.next_cursor}, true));
          root.appendChild(more);
        }
        break;
      }
      case 'days':
        heading('История Кэф', 'По дням · Владивосток, UTC+10');
        datePicker('days');
        if (!data.days.length) note('Нет сохранённых дней.');
        for (const row of data.days) {
          const day = typeof row === 'string' ? row : row.day;
          item(formatDay(day), archive ? (row.has_records ? 'Неполные данные' : 'Нет записей · неполные данные')
            : 'Коэффициенты и количество раундов',
            () => navigate(viewFor('day'), {day}, true));
        }
        break;
      case 'day': {
        const s = data.summary || {};
        heading(formatDay(data.day), 'История Кэф · Владивосток, UTC+10');
        if (!archive && data.view_reset) note('Данные после сброса.');
        if (data.truncated || !data.summary) { note('Неполные данные: превышен предел проверки.'); break; }
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
          if (check.status !== 'verified' || check.poll_coverage?.status !== 'full_poll_coverage')
            note('Неполные данные · полнота истории не подтверждена.');
        } else if (s.partial || s.time_uncertain) {
          note('Статистика может быть неполной.');
        }
        break;
      }
      case 'high_days':
        heading('Интервалы >100x', 'По дням · Владивосток, UTC+10');
        if (!archive && data.last_high)
          info('Последний >100x', String(data.last_high.value) + 'x', formatTime(data.last_high.stamp));
        if (!archive && data.last_high)
          info('Раундов после >100x', data.gap ? 'Неполные данные' : data.since_high);
        datePicker('high_days');
        if (!data.days.length) note('Нет сохранённых дней.');
        for (const row of data.days) {
          const day = typeof row === 'string' ? row : row.day;
          item(formatDay(day), archive ?
            (row.count ? row.count + ' событий · интервалы могут быть неполными' : 'Нет подтверждённых интервалов') :
            'Посмотреть интервалы', () => navigate(viewFor('high_day'), {day}, true));
        }
        break;
      case 'high_day':
        heading(formatDay(data.day), 'Интервалы >100x · новые сначала');
        if (!data.items.length) note('За этот день нет сохранённых интервалов.');
        if (data.truncated) note('Неполные данные: превышен предел проверки.');
        for (const entry of data.items) {
          const count = entry.rounds == null ? 'Данные неполные' : entry.rounds + ' раундов';
          item(String(entry.value) + 'x', count + ' · ' + formatTime(entry.stamp),
            () => navigate(viewFor('high_item'), archive ? {day: data.day, id: entry.id} : {id: entry.id}, true));
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
      case 'high_cycles':
      case 'cycles':
        heading(view === 'high_cycles' ? 'Стратегия №2 · история' : 'История DEMO',
          data.total + ' циклов · новые сначала');
        if (archive) datePicker('cycles');
        if (view !== 'high_cycles')
          item('Статистика DEMO', 'Результаты по сохранённым сессиям',
            () => navigate(viewFor('demo_stats'), archive && data.day ? {day: data.day} : {}, true));
        if (!data.items.length) note('Завершённых циклов пока нет.');
        for (const c of data.items) {
          currency = c.currency || 'USD';
          const result = c.outcome === 'incomplete_data' || c.net_cents == null
            ? 'Нет полных данных' : signedMoney(c.net_cents);
          const category = c.strategy === 'high_gap' ? 'После >x' + (c.reference_threshold || 100) : 'Серия <2x';
          item(c.steps + ' шаг. · ' + formatTime(c.ended_at || c.started_at),
            archive ? category + ' · ' + c.currency : 'Цикл ' + (c.index + 1),
            () => navigate(view === 'high_cycles' ? 'high_cycle' : viewFor('cycle'),
              archive ? {id: c.id} : {index: c.index}, true), result);
        }
        break;
      case 'high_cycle':
      case 'cycle': {
        const c = data.item;
        currency = c.currency || 'USD';
        const reasons = {recovered: 'Цель достигнута', insufficient_balance: 'Недостаточно баланса',
          incomplete_data: 'Неполные данные', stopped_by_user: 'Остановлена пользователем',
          reference_changed: 'Новый опорный коэффициент'};
        heading(archive ? 'Цикл DEMO' : view === 'high_cycle' ? 'Стратегия №2 · цикл' :
          'Цикл ' + (c.index + 1), 'Время Владивостока, UTC+10');
        if (c.strategy === 'high_gap') {
          info('Стратегия', 'После >x' + (c.reference_threshold || '100'));
          info('Период ожидания', (c.wait_rounds ?? '—') + ' раундов');
          info('Автовывод', 'x' + (c.cashout_multiplier || '100'));
          if (c.reference_coefficient) info('Опорный коэффициент', 'x' + c.reference_coefficient);
        }
        info('Валюта', currency);
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
        if (archive) {
          heading('Статистика DEMO', data.total + ' сохранённых циклов');
          if (data.truncated) { note('Неполные данные: слишком много записей.'); break; }
          const strategies = data.strategies || {classic: data.currencies || {}};
          for (const [strategy, groups] of Object.entries(strategies)) {
            heading(strategy === 'high_gap' ? 'Стратегия №2' : 'Стратегия №1');
            for (const [code, group] of Object.entries(groups)) {
              currency = code;
              info('Валюта', code);
              info('Всего сессий', group.total);
              info('Успешных', group.recovered);
              info('Проигрышных', group.lost);
              info('Неполных', group.incomplete);
              info('Остановленных', group.stopped);
              if (group.reference_changed) info('Новый опорный коэффициент', group.reference_changed);
              info('По истории', signedMoney(group.history_net_cents));
              info('Среднее шагов', group.average_steps);
            }
          }
          if (!data.total) note('Нет сохранённых циклов.');
          break;
        }
        const s = data.stats;
        currency = s.currency || 'USD';
        heading('Статистика DEMO', 'По последним ' + s.total + ' из ' + s.maximum + ' циклов');
        if (s.view_reset) note('Счётчики циклов — после сброса вкладки; баланс DEMO не менялся.');
        info('Валюта', currency);
        info('Всего сессий', s.total);
        info('Успешных', s.recovered);
        info('Проигрышных', s.lost);
        info('Неполных', s.incomplete);
        info('Остановленных', s.stopped);
        info('С последнего сброса DEMO', signedMoney(s.since_reset_cents));
        info('По истории', signedMoney(s.history_net_cents));
        info('Среднее шагов', s.average_steps);
        break;
      }
      case 'max_steps':
        heading('Максимальный шаг', 'По дням · Владивосток, UTC+10');
        if (!archive && data.view_reset) note('Данные после сброса.');
        datePicker('max_steps');
        if (!data.days.length) note('Нет сохранённых дней.');
        for (const d of data.days) item(formatDay(d.day),
          archive ? 'Сохранённые раунды · полнота не подтверждена' : 'Показать максимум',
          () => navigate(viewFor('max_step_day'), {day: d.day}, true),
          d.steps == null ? 'Нет данных' : d.steps + ' шаг.');
        break;
      case 'max_step_day':
        heading(formatDay(data.day), 'Максимальный шаг · Владивосток, UTC+10');
        if (data.truncated) note('Недостаточно данных для точного пересчёта.');
        else if (data.steps == null) note('Нет сохранённых данных за этот день.');
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
  document.getElementById('history-archive').addEventListener('click', () => {
    stack = []; current = null; navigate('archive_days');
  });
  document.getElementById('history-exit-archive').addEventListener('click', () => {
    stack = []; current = null; navigate('days');
  });
  for (const button of archiveTabs) button.addEventListener('click', () => {
    stack = []; current = null; navigate(button.dataset.archiveView);
  });
  document.getElementById('history-reset-scope').addEventListener('click', () => {
    const scope = scopeByView(args.view);
    if (scope) resetHistory(scope);
  });
  document.getElementById('history-reset-all').addEventListener('click', () => resetHistory('all'));

  refresh.addEventListener('click', () => navigate(args.view, {...args}));
  document.getElementById('history-prev').addEventListener('click', () =>
    navigate(args.view, {...args, page: Math.max(0, Number(args.page || 0) - 1)}));
  document.getElementById('history-next').addEventListener('click', () =>
    navigate(args.view, {...args, page: Number(args.page || 0) + 1}));

  const scopeByView = view => {
    if (view === 'archive' || isArchive(view)) return null;
    if (['days', 'day'].includes(view)) return 'days';
    if (['high_days', 'high_day', 'high_item'].includes(view)) return 'high_days';
    if (['cycles', 'cycle', 'high_cycles', 'high_cycle', 'demo_stats'].includes(view)) return 'cycles';
    return 'max_steps';
  };
  async function resetHistory(scope) {
    if (!['days', 'high_days', 'cycles', 'max_steps', 'all'].includes(scope)) return;
    if (!window.canUsePremium?.()) { status.textContent = 'Требуется Premium.'; return; }
    const labels = {days: 'Кэфы', high_days: '>100x', cycles: 'Циклы', max_steps: 'Шаги'};
    const question = scope === 'all'
      ? 'Сбросить отображение всех четырёх вкладок? Полный фактический архив сохранится.'
      : 'Сбросить отображение вкладки «' + labels[scope] + '»? Полный архив сохранится.';
    const confirmed = await new Promise(resolve => {
      if (tg && typeof tg.showConfirm === 'function') tg.showConfirm(question, resolve);
      else resolve(window.confirm(question));
    });
    if (!confirmed) return;
    const currentScope = scopeByView(args.view);
    const scopeButton = document.getElementById('history-reset-scope');
    const allButton = document.getElementById('history-reset-all');
    scopeButton.disabled = true; allButton.disabled = true;
    status.textContent = 'Сохранение сброса…';
    try {
      const response = await fetch('/api/controls', {
        method: 'POST', headers: {'Content-Type': 'application/json',
          ...(tg?.initData ? {Authorization: 'tma ' + tg.initData} : {})},
        credentials: 'same-origin', cache: 'no-store',
        body: JSON.stringify({action: 'history_reset', value: scope, confirm: true}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Не удалось сохранить сброс.');
      stack = []; current = null; lastLoad = 0;
      navigate(scope === 'all' ? 'days' : scope, {}, false);
    } catch (error) {
      status.textContent = String(error.message);
    } finally {
      scopeButton.disabled = false; allButton.disabled = false;
    }
  }
  window.openHistoryTab = () => {
    if (!current || Date.now() - lastLoad > 30000) navigate(args.view, {...args});
  };
  window.openHighGapHistory = () => {
    stack = []; current = null;
    navigate('high_cycles');
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
