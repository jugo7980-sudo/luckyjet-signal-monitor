/* Admin-only access screen. Never render user IDs until a verified 200 response. */
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const panel = document.getElementById('admin-section');
  const list = document.getElementById('admin-users-list');
  const status = document.getElementById('admin-status');
  const counter = document.getElementById('admin-counter');
  const pager = document.getElementById('admin-pager');
  const previous = document.getElementById('admin-prev');
  const next = document.getElementById('admin-next');
  const position = document.getElementById('admin-position');
  let page = 0;
  let busy = false;
  let lastLoad = 0;
  let visible = false;

  const element = (tag, text = '', className = '') => {
    const e = document.createElement(tag);
    e.textContent = String(text);
    if (className) e.className = className;
    return e;
  };
  async function api(method = 'GET', payload = null, pageNumber = page) {
    if (!window.canUsePremium?.()) throw new Error('Требуется Premium.');
    const stop = new AbortController();
    const timeout = setTimeout(() => stop.abort(), 8000);
    try {
      const response = await fetch('/api/admin?page=' + encodeURIComponent(pageNumber), {
        method, credentials: 'same-origin', cache: 'no-store', signal: stop.signal,
        headers: {
          ...(tg?.initData ? {Authorization: 'tma ' + tg.initData} : {}),
          ...(method === 'POST' ? {'Content-Type': 'application/json'} : {}),
        },
        ...(method === 'POST' ? {body: JSON.stringify(payload)} : {}),
      });
      const data = await response.json();
      if (response.status === 403 || response.status === 401) {
        const error = new Error(response.status === 403 ? 'Нет прав администратора.' : 'Авторизация истекла.');
        error.status = response.status;
        throw error;
      }
      if (!response.ok) throw new Error(data.error || 'Не удалось обновить список.');
      return data;
    } finally {
      clearTimeout(timeout);
    }
  }
  function promptConfirm(target, action) {
    const words = {
      block: 'Заблокировать доступ',
      revoke: 'Отозвать доступ',
      unblock: 'Разблокировать',
    };
    const warning = action === 'unblock'
      ? 'После разблокировки человеку потребуется снова ввести пароль.'
      : 'Сохранённые DEMO-циклы и статистика пользователя будут удалены.';
    const question = words[action] + ' для ID ' + target + '? ' + warning;
    const confirmed = async yes => {
      if (!yes || busy) return;
      busy = true;
      status.textContent = 'Применяю изменение…';
      try {
        const response = await api('POST', {
          action, target, confirm: true, confirm_id: String(target),
        });
        status.textContent = response.message || 'Изменено.';
        busy = false;
        await load(page, true, true);
      } catch (error) {
        busy = false;
        status.textContent = error.message;
      }
    };
    if (tg && typeof tg.showConfirm === 'function') tg.showConfirm(question, confirmed);
    else confirmed(window.confirm(question));
  }
  function render(data) {
    panel.hidden = false;
    visible = true;
    list.replaceChildren();
    counter.textContent = 'С доступом: ' + data.authorized_count + ' · Заблокировано: ' + data.blocked_count;
    const pageCount = Math.max(1, Math.ceil(data.total / data.page_size));
    page = data.page;
    position.textContent = (page + 1) + ' / ' + pageCount;
    pager.hidden = pageCount <= 1;
    previous.disabled = page === 0;
    next.disabled = page + 1 >= pageCount;
    if (!data.users.length) {
      list.appendChild(element('p', 'Пользователей нет.', 'history-note'));
    }
    for (const u of data.users) {
      const card = element('div', '', 'admin-user');
      const head = element('div', '', 'admin-user-title');
      head.append(element('strong', 'ID ' + u.id));
      head.append(element('span', u.status === 'blocked' ? 'Заблокирован' : 'Есть доступ',
        u.status === 'blocked' ? 'admin-state-blocked' : 'admin-state-active'));
      card.appendChild(head);
      const row = element('div', '', 'admin-actions');
      const options = u.status === 'blocked'
        ? [['Разблокировать', 'unblock']]
        : [['Отозвать', 'revoke'], ['Заблокировать', 'block']];
      for (const [label, name] of options) {
        const button = element('button', label, name === 'block' ? 'admin-danger' : '');
        button.type = 'button';
        button.addEventListener('click', () => promptConfirm(u.id, name));
        row.appendChild(button);
      }
      card.appendChild(row);
      list.appendChild(card);
    }
  }
  async function load(targetPage = page, force = false, keepMessage = false) {
    if (busy || (!force && visible && Date.now() - lastLoad < 15000)) return;
    busy = true;
    if (!keepMessage) status.textContent = 'Загрузка пользователей…';
    try {
      const data = await api('GET', null, targetPage);
      render(data);
      lastLoad = Date.now();
      if (!keepMessage) status.textContent = '';
    } catch (error) {
      if (error.status === 403) {
        panel.hidden = true;
        visible = false;
        list.replaceChildren();
        status.textContent = '';
      } else if (error.status === 401) {
        panel.hidden = true;
        visible = false;
        list.replaceChildren();
      } else {
        status.textContent = error.name === 'AbortError' ? 'Сервер не ответил.' : error.message;
      }
    } finally {
      busy = false;
    }
  }
  previous.addEventListener('click', () => { if (!busy && page > 0) load(page - 1, true); });
  next.addEventListener('click', () => { if (!busy && !next.disabled) load(page + 1, true); });
  document.getElementById('admin-refresh').addEventListener('click', () => load(page, true));
  window.openAdminTab = () => load(page);
  window.clearAdminData = () => {
    panel.hidden = true; visible = false; page = 0; lastLoad = 0;
    list.replaceChildren(); status.textContent = '';
  };
})();
