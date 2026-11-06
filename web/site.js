/* Site login is owner-only. Credentials/OTP stay in form memory for one request only. */
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const page = document.getElementById('page-account');
  const section = document.getElementById('site-section');
  const status = document.getElementById('site-status-text');
  const notice = document.getElementById('site-message');
  const login = document.getElementById('site-login-form');
  const codeForm = document.getElementById('site-code-form');
  const email = document.getElementById('site-email');
  const password = document.getElementById('site-password');
  const code = document.getElementById('site-code');
  const cancel = document.getElementById('site-cancel');
  const forget = document.getElementById('site-forget');
  const refresh = document.getElementById('site-refresh');
  let timer = null;
  let busy = false;
  let lastLoad = 0;
  let visible = false;
  let state = null;
  const labels = {
    disconnected: 'Аккаунт не подключён.',
    saved: 'Сессия сохранена. Её действительность проверяет сайт.',
    connected: 'Вход подтверждён. Сессия сохранена.',
    connecting: 'Выполняется вход на сайт…',
    code: 'Сайт запросил код из почты.',
    retry_code: 'Код не принят. Введи новый код из почты.',
    error: 'Последний вход не выполнен.',
  };
  function schedule() {
    clearTimeout(timer);
    if (state && ['connecting', 'code', 'retry_code'].includes(state.phase) && !page.hidden) {
      timer = setTimeout(() => load(true), 2500);
    }
  }
  function paint(data) {
    state = data;
    visible = true;
    section.hidden = false;
    status.textContent = labels[data.phase] || 'Состояние неизвестно.';
    if (data.error) status.textContent += ' ' + data.error;
    login.hidden = ['connecting', 'code', 'retry_code'].includes(data.phase);
    codeForm.hidden = !['code', 'retry_code'].includes(data.phase);
    cancel.hidden = !['connecting', 'code', 'retry_code'].includes(data.phase);
    forget.hidden = !data.saved_session;
    if (busy) {
      login.querySelectorAll('input,button').forEach(node => node.disabled = true);
      codeForm.querySelectorAll('input,button').forEach(node => node.disabled = true);
    } else {
      login.querySelectorAll('input,button').forEach(node => node.disabled = false);
      codeForm.querySelectorAll('input,button').forEach(node => node.disabled = false);
    }
    schedule();
  }
  async function api(method = 'GET', payload) {
    if (!tg || !tg.initData) throw new Error('Открой приложение через Telegram.');
    const stop = new AbortController();
    const timeout = setTimeout(() => stop.abort(), 8000);
    try {
      const resp = await fetch('/api/site', {
        method, signal: stop.signal, cache: 'no-store', credentials: 'omit',
        headers: {Authorization: 'tma ' + tg.initData,
          ...(method === 'POST' ? {'Content-Type': 'application/json'} : {})},
        ...(method === 'POST' ? {body: JSON.stringify(payload)} : {}),
      });
      const result = await resp.json();
      if (!resp.ok) {
        const err = new Error(result.error || 'Не удалось выполнить запрос.');
        err.auth = resp.status === 401 || resp.status === 403;
        throw err;
      }
      return result;
    } finally { clearTimeout(timeout); }
  }
  async function load(force = false) {
    if (busy || (!force && visible && Date.now() - lastLoad < 10000)) return;
    busy = true;
    try {
      const data = await api();
      lastLoad = Date.now();
      paint(data);
    } catch (err) {
      if (err.auth) {
        window.clearSiteData();
      } else if (visible) notice.textContent = err.name === 'AbortError' ? 'Сервер не ответил.' : err.message;
    } finally {
      busy = false;
      if (state) paint(state);
    }
  }
  async function submit(payload) {
    if (busy) return;
    busy = true;
    notice.textContent = 'Отправка…';
    clearTimeout(timer);
    try {
      const data = await api('POST', payload);
      lastLoad = Date.now();
      notice.textContent = data.message || '';
      paint(data);
    } catch (err) {
      notice.textContent = err.name === 'AbortError' ? 'Сервер не ответил.' : err.message;
      if (err.auth) window.clearSiteData();
    } finally {
      busy = false;
      if (state) paint(state);
    }
  }
  login.addEventListener('submit', event => {
    event.preventDefault();
    const submittedEmail = email.value.trim();
    const submittedPassword = password.value;
    password.value = '';
    email.value = '';
    submit({action: 'login', email: submittedEmail, password: submittedPassword});
  });
  codeForm.addEventListener('submit', event => {
    event.preventDefault();
    const submittedCode = code.value.replace(/[\s-]/g, '');
    code.value = '';
    submit({action: 'code', code: submittedCode});
  });
  cancel.addEventListener('click', () => submit({action: 'cancel'}));
  forget.addEventListener('click', () => {
    const question = 'Удалить сохранённую сессию сайта с сервера? Другие входы на сайте не будут отозваны.';
    const action = yes => { if (yes) submit({action: 'forget', confirm: true}); };
    if (tg && typeof tg.showConfirm === 'function') tg.showConfirm(question, action);
    else action(window.confirm(question));
  });
  refresh.addEventListener('click', () => load(true));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(timer);
    else if (!page.hidden) load(true);
  });
  window.openSiteTab = () => load();
  window.clearSiteData = () => {
    clearTimeout(timer);
    section.hidden = true;
    visible = false;
    lastLoad = 0;
    state = null;
    password.value = ''; code.value = ''; email.value = '';
    notice.textContent = ''; status.textContent = '';
  };
})();
