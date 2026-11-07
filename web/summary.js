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
    root.replaceChildren();
    if (!items.length) {
      const empty = document.createElement('p');
      empty.className = 'history-note';
      empty.textContent = 'В выбранном периоде совпадений нет.';
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
    setText('summary-preview-count', curr.count);
    setText('summary-preview-since', countText(data.rounds_since_since));
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
    setText('ind-previous-count', prev.count);
    setText('ind-compare', quality.complete_comparison
      ? (curr.count - prev.count > 0 ? '+' : '') + (curr.count - prev.count) : '—');
    renderMatches(data.matches || [], data.match_rows_limited);

    status.className = 'connection ' + (quality.complete_window ? 'ind-summary-good' : 'ind-summary-warning');
    if (quality.window_has_discontinuities || quality.comparison_has_discontinuities) {
      status.textContent = 'В архиве обнаружены разрывы. Подсчёт охватывает только сохранённые раунды; неопределённые интервалы скрыты.';
    } else if (!quality.complete_window) {
      status.textContent = 'Архив содержит ' + data.rounds_available + ' из запрошенных ' + data.rounds_requested + ' раундов. Данные неполные.';
    } else if (!quality.complete_comparison) {
      status.textContent = 'Текущие ' + data.rounds_requested + ' раундов обработаны. Для сравнения двух периодов недостаточно архива.';
    } else {
      status.textContent = 'Сохранённые раунды проанализированы; полнота истории самого игрового сайта не подтверждена.';
    }
    if (quality.duplicate_entries) status.textContent += ' Повторные записи: ' + quality.duplicate_entries + '.';
    if (quality.unverified_source_timing) status.textContent += ' Время отдельных раундов не подтверждено источником.';
  }

  async function load() {
    if (!accordion.open || document.visibilityState === 'hidden') return;
    if (busy) return;
    const rounds = numeric('ind-rounds', true, 1, 50000);
    const target = numeric('ind-target', false, 1, 100000);
    const since = numeric('ind-since', false, 1, 100000);
    if (rounds === null || target === null || since === null) {
      status.textContent = 'Укажи целое число раундов от 1 до 50000 и коэффициенты X от 1 до 100000.';
      status.className = 'connection ind-summary-warning';
      return;
    }
    safeStore();
    setText('summary-heading', 'Сводка архива · ' + rounds + ' раундов');
    busy = true;
    button.disabled = true;
    status.textContent = 'Проверяем фактический архив…';
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
    } catch (error) {
      status.className = 'connection ind-summary-warning';
      status.textContent = error.name === 'AbortError'
        ? 'Время запроса истекло. Выбери меньше раундов и повтори.'
        : (error.message || 'Не удалось прочитать архив.');
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
  button.addEventListener('click', load);
  fields.forEach(id => byId(id).addEventListener('change', safeStore));
  accordion.addEventListener('toggle', () => {
    if (accordion.open) {
      load();
      if (refreshTimer !== null) clearInterval(refreshTimer);
      refreshTimer = setInterval(() => {
        const requested = Number(byId('ind-rounds').value);
        if (!busy && document.body.dataset.access === 'premium') load();
      }, 20000);
    } else if (refreshTimer !== null) {
      clearInterval(refreshTimer);
      refreshTimer = null;
      if (controller) controller.abort();
    }
  });
})();
