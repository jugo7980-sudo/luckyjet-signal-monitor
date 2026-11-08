/* Archive-only summary: no DEMO controls, strategy data or live-tile limits. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const accordion = byId('summary-accordion');
  if (!accordion) return;
  const button = byId('ind-summary-apply');
  const status = byId('ind-summary-status');
  const fields = ['ind-rounds', 'ind-target', 'ind-since'];
  const storageKey = 'luckyjet:archive-summary-settings:v1';
  let controller = null;
  let busy = false;
  let refreshTimer = null;
  let lastLoadedAt = 0;
  let lastMatchesKey = null;
  let renderedSettingsKey = null;

  function safeStore() {
    try {
      localStorage.setItem(storageKey, JSON.stringify(fields.map(id => byId(id).value)));
    } catch (_error) { /* Browsers may block localStorage. */ }
  }
  try {
    const old = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (Array.isArray(old) && old.length === fields.length) {
      old.forEach((value, index) => {
        if (typeof value === 'string' && value.length <= 12) byId(fields[index]).value = value;
      });
    }
  } catch (_error) { /* Use safe defaults. */ }

  function setText(id, value) {
    const node = byId(id);
    if (node) node.textContent = value == null ? '—' : String(value);
  }
  function numeric(id, integer, lower, upper) {
    const raw = byId(id).value.trim().replace(',', '.');
    const value = Number(raw);
    if (!raw || !Number.isFinite(value) || (integer && !Number.isInteger(value))
        || value < lower || value > upper) return null;
    return value;
  }
  const countText = n => n == null ? '—' : String(n);
  const coefText = n => n == null ? '—' : Number(n).toFixed(2) + 'x';

  function moment(value) {
    if (!value) return 'Время неизвестно';
    const stamp = Number(value);
    if (!Number.isFinite(stamp) || stamp <= 0) return 'Время неизвестно';
    try {
      return new Intl.DateTimeFormat('ru-RU', {
        timeZone: 'Asia/Vladivostok', day: '2-digit', month: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit'
      }).format(new Date(stamp)) + ' · зафиксировано, Владивосток';
    } catch (_error) { return 'Время неизвестно'; }
  }

  function renderMatches(items, limited) {
    const root = byId('ind-summary-matches');
    const matchesKey = JSON.stringify([items, limited]);
    if (lastMatchesKey === matchesKey) return;
    lastMatchesKey = matchesKey;
    root.replaceChildren();
    if (!items.length) {
      setText('ind-summary-match-total', '0');
      const empty = document.createElement('p');
      empty.className = 'history-note';
      empty.textContent = 'За выбранное количество раундов искомый коэффициент не встретился.';
      root.appendChild(empty);
      return;
    }
    for (const record of items) {
      const row = document.createElement('div');
      row.className = 'ind-summary-match';
      const left = document.createElement('div');
      const header = document.createElement('b');
      header.textContent = coefText(record.coefficient);
      const time = document.createElement('small');
      time.textContent = moment(record.observed_at);
      left.append(header, time);
      const right = document.createElement('strong');
      right.textContent = countText(record.rounds_ago) + ' раундов назад';
      const interval = document.createElement('small');
      interval.textContent = record.interval_to_previous == null
        ? 'Интервал: нет подтверждения'
        : 'До предыдущего: ' + record.interval_to_previous + ' раундов';
      right.appendChild(interval);
      row.append(left, right);
      root.appendChild(row);
    }
    if (limited) {
      const note = document.createElement('p');
      note.className = 'history-note';
      note.textContent = 'Показаны последние 50 совпадений. Общее число указано выше.';
      root.appendChild(note);
    }
  }

  function render(data) {
    const quality = data.quality || {};
    const curr = data.current || {}, prev = data.previous || {};
    setText('summary-heading', 'Сводка архива · ' + data.rounds_requested + ' раундов');
    setText('ind-summary-context', data.rounds_available + ' сохранённых раундов · ≥ ' + data.target + 'x · отсчёт ≥ ' + data.since_threshold + 'x');
    setText('ind-summary-match-total', String(curr.count ?? 0));
    setText('summary-preview-count', curr.count);
    setText('summary-preview-since', countText(data.rounds_since_since));
    byId('summary-preview-placeholder').hidden = true;
    byId('summary-preview-results').hidden = false;
    setText('ind-found', curr.count);
    setText('ind-since-count', countText(data.rounds_since_since));
    setText('ind-target-count', countText(data.rounds_since_target));
    setText('ind-longest', curr.longest_wait);
    setText('ind-average-interval', countText(curr.average_interval));
    setText('ind-min-interval', countText(curr.minimum_interval));
    setText('ind-max-interval', countText(curr.maximum_interval));
    setText('ind-average', coefText(curr.average));
    setText('ind-maximum', coefText(curr.maximum));
    setText('ind-below-two', curr.below_two);
    setText('ind-previous-count', quality.complete_comparison ? prev.count : null);
    setText('ind-compare', quality.complete_comparison
      ? (curr.count - prev.count > 0 ? '+' : '') + (curr.count - prev.count) : '—');
    renderMatches(data.matches || [], data.match_rows_limited);
    for (const id of ['ind-average-interval', 'ind-min-interval', 'ind-max-interval']) {
      const metric = byId(id);
      const missing = metric.textContent === '—';
      metric.closest('.ind-stat')?.setAttribute('data-missing', String(missing));
      const hint = byId(id + '-hint');
      if (hint) hint.textContent = missing
        ? (curr.count ?? 0) < 2
          ? 'Нужно хотя бы 2 совпадения без разрыва архива.'
          : 'Промежуток между X не подтверждён из-за отсутствующих данных.'
        : id === 'ind-average-interval' ? 'Среднее число раундов между найденными X.'
        : id === 'ind-min-interval' ? 'Самый короткий подтверждённый промежуток между X.'
        : 'Самый длинный подтверждённый промежуток между X.';
    }
    const previousHint = byId('ind-previous-count-hint');
    if (previousHint) previousHint.textContent = quality.complete_comparison
      ? 'Число искомых X в предшествующих ' + data.rounds_requested + ' раундах.'
      : 'Для сравнения недостаточно данных предыдущего периода.';
    const differenceHint = byId('ind-compare-hint');
    if (differenceHint) differenceHint.textContent = quality.complete_comparison
      ? 'Сколько искомых X стало больше (+) или меньше (−).'
      : 'Показатель появится, когда будут доступны два полных периода.';
    const sinceHint = byId('ind-since-count-hint');
    if (sinceHint && data.rounds_since_since == null) sinceHint.textContent =
      'Последний выбранный X не подтверждён в доступной истории.';
    const targetHint = byId('ind-target-count-hint');
    if (targetHint && data.rounds_since_target == null) targetHint.textContent =
      'Последний искомый X не подтверждён в доступной истории.';
    for (const card of document.querySelectorAll('.ind-stat')) {
      const value = card.querySelector('strong');
      if (value) card.setAttribute('data-missing', String(value.textContent === '—'));
    }

    // Keep the visible quality strip one line high; the full audit text lives in ?.
    status.className = 'connection ' + (quality.complete_window && !quality.window_has_discontinuities ? 'ind-summary-good' : 'ind-summary-warning');
    if (quality.window_has_discontinuities || quality.comparison_has_discontinuities) {
      status.textContent = 'Архив: есть разрывы';
    } else if (!quality.complete_window) {
      status.textContent = 'Архив: ' + data.rounds_available + ' из ' + data.rounds_requested + ' раундов';
    } else {
      status.textContent = 'Архив: сохранённые раунды';
    }
    const notices = [];
    if (quality.window_has_discontinuities || quality.comparison_has_discontinuities) {
      notices.push('В архиве обнаружены разрывы. Подсчёт охватывает только сохранённые раунды; неопределённые интервалы скрыты.');
    } else if (!quality.complete_window) {
      notices.push('Архив содержит ' + data.rounds_available + ' из запрошенных ' + data.rounds_requested + ' раундов. Данные неполные.');
    } else if (!quality.complete_comparison) {
      notices.push('Текущие ' + data.rounds_requested + ' раундов обработаны. Для сравнения двух периодов недостаточно архива.');
    } else {
      notices.push('Сохранённые раунды проанализированы.');
    }
    notices.push('Полнота истории самого игрового сайта не подтверждена.');
    if (quality.history_search_limited) notices.push('Поиск последнего X ограничен глубиной архива.');
    if (quality.duplicate_entries) notices.push('Повторные записи: ' + quality.duplicate_entries + '.');
    if (quality.unverified_source_timing) notices.push('Время отдельных раундов не подтверждено источником.');
    setText('ind-summary-status-detail', notices.join(' '));
  }

  async function load(options = {}) {
    const quiet = Boolean(options.quiet);
    if (!accordion.open || document.visibilityState === 'hidden') return;
    if (busy) return;
    const rounds = numeric('ind-rounds', true, 1, 50000);
    const target = numeric('ind-target', false, 1, 100000);
    const since = numeric('ind-since', false, 1, 100000);
    if (rounds === null || target === null || since === null) {
      if (!quiet) {
        status.textContent = 'Проверь введённые параметры';
        setText('ind-summary-status-detail', 'Укажи целое число раундов от 1 до 50000 и коэффициенты X от 1 до 100000.');
        status.className = 'connection ind-summary-warning';
      }
      return;
    }
    safeStore();
    busy = true;
    lastLoadedAt = Date.now();
    button.disabled = true;
    if (!quiet) {
      status.textContent = 'Архив: обновление…';
      setText('ind-summary-status-detail', 'Проверяем фактический архив. Уже показанные результаты сохраняются до ответа сервера.');
    }
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const query = new URLSearchParams({rounds: String(rounds), target: String(target), since: String(since)});
      const tg = window.Telegram?.WebApp;
      const headers = tg?.initData ? {Authorization: 'tma ' + tg.initData} : {};
      const response = await fetch('/api/round-summary?' + query.toString(), {
        method: 'GET', credentials: 'same-origin', headers, cache: 'no-store',
        signal: controller.signal
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Архив недоступен.');
      render(data);
      renderedSettingsKey = [rounds, target, since].join(':');
    } catch (error) {
      status.className = 'connection ind-summary-warning';
      status.textContent = 'Архив: ошибка обновления';
      setText('ind-summary-status-detail', error.name === 'AbortError'
        ? 'Время запроса истекло. Выбери меньше раундов и повтори.'
        : (error.message || 'Не удалось прочитать архив.'));
    } finally {
      clearTimeout(timeout);
      busy = false;
      button.disabled = false;
    }
  }

  for (const suffix of ['target', 'since']) {
    const preset = byId('ind-' + suffix + '-preset');
    preset.addEventListener('change', () => {
      if (preset.value) byId('ind-' + suffix).value = preset.value;
    });
  }
  function bindDisclosure(buttonId, contentId) {
    const control = byId(buttonId), content = byId(contentId);
    if (!control || !content) return;
    control.addEventListener('click', () => {
      const expanded = control.getAttribute('aria-expanded') === 'true';
      control.setAttribute('aria-expanded', String(!expanded));
      content.hidden = expanded;
    });
  }
  bindDisclosure('summary-stats-toggle', 'summary-extra-stats');
  bindDisclosure('summary-help-toggle', 'summary-help-body');
  const popup = byId('summary-metric-popup');
  const popupTitle = byId('summary-metric-title');
  const popupText = byId('summary-metric-text');
  let popupReturnFocus = null;
  function closeMetricPopup() {
    if (popup.hidden) return;
    popup.hidden = true;
    popupReturnFocus?.focus();
  }
  // One delegated handler also supports metrics and notes inserted after a History refresh.
  document.addEventListener('click', event => {
    const control = event.target.closest?.('[data-summary-help-for], [data-help-text]');
    if (!control) return;
    event.preventDefault();
    event.stopPropagation();
    const source = control.hasAttribute('data-summary-help-for')
      ? byId(control.getAttribute('data-summary-help-for')) : null;
    const detail = source?.textContent?.trim() || control.getAttribute('data-help-text') || '';
    popupTitle.textContent = control.getAttribute('aria-label') || 'Справка';
    popupText.textContent = detail || 'Пояснение пока недоступно.';
    popupReturnFocus = control;
    popup.hidden = false;
    byId('summary-metric-close').focus();
  });
  byId('summary-metric-close').addEventListener('click', closeMetricPopup);
  popup.addEventListener('click', event => { if (event.target === popup) closeMetricPopup(); });
  function closeOverviewHelp() {
    const expanded = byId('summary-help-toggle').getAttribute('aria-expanded') === 'true';
    if (expanded) byId('summary-help-toggle').click();
  }
  byId('summary-help-close').addEventListener('click', closeOverviewHelp);
  byId('summary-help-body').addEventListener('click', event => { if (event.target === event.currentTarget) closeOverviewHelp(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closeMetricPopup(); closeOverviewHelp(); }
  });
  button.addEventListener('click', load);
  fields.forEach(id => byId(id).addEventListener('change', safeStore));
  accordion.addEventListener('toggle', () => {
    if (accordion.open) {
      load();
      if (refreshTimer !== null) clearInterval(refreshTimer);
      refreshTimer = setInterval(() => {
        const requested = Number(byId('ind-rounds').value);
        const refreshEvery = requested > 5000 ? 60000 : requested > 1000 ? 20000 : 5000;
        if (!busy && document.body.dataset.access === 'premium' && Date.now() - lastLoadedAt >= refreshEvery &&
            renderedSettingsKey === [Number(byId('ind-rounds').value), Number(byId('ind-target').value), Number(byId('ind-since').value)].join(':')) load({quiet:true});
      }, 5000);
    } else if (refreshTimer !== null) {
      clearInterval(refreshTimer);
      refreshTimer = null;
      if (controller) controller.abort();
    }
  });
})();
