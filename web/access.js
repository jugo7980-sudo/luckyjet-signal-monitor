/* One identity for Telegram Mini App and browser, Premium unlocks private APIs. */
(() => {
  'use strict';
  const app = window.Telegram && window.Telegram.WebApp;
  const modal = document.getElementById('access-modal');
  const dialog = document.getElementById('access-dialog');
  const login = document.getElementById('top-login');
  const premium = document.getElementById('top-premium');
  const headline = document.getElementById('access-headline');
  const explanation = document.getElementById('access-explanation');
  const nativeRegister = document.getElementById('access-register');
  const nativeLogin = document.getElementById('access-telegram-login');
  const accountLogout = document.getElementById('account-logout');
  const accountLogoutConfirm = document.getElementById('account-logout-confirm');
  const accountLogoutButton = document.getElementById('account-logout-confirm-button');
  const accountLogoutCancel = document.getElementById('account-logout-cancel');
  const accountLogoutStatus = document.getElementById('account-logout-status');
  const widget = document.getElementById('telegram-login-widget');
  const keyForm = document.getElementById('access-premium-form');
  const keyInput = document.getElementById('access-key');
  const message = document.getElementById('access-message');
  const logout = document.getElementById('access-logout');
  const close = document.getElementById('access-close');
  const defaultAccess = {verified: false, registered: false, premium: false, username: null};
  window.accessState = {...defaultAccess};
  let busy = false;
  let widgetLoaded = false;
  let previousFocus = null;

  function toggleLogoutConfirm(show) {
    if (!accountLogoutConfirm) return;
    accountLogoutConfirm.hidden = !show;
    accountLogout?.setAttribute('aria-expanded', String(show));
    if (!show && accountLogoutStatus) accountLogoutStatus.textContent = '';
  }

  const authHeaders = () => app && app.initData ? {Authorization: 'tma ' + app.initData} : {};
  async function api(method = 'GET', payload) {
    const timeout = new AbortController();
    const stop = setTimeout(() => timeout.abort(), 8000);
    try {
      const resp = await fetch('/api/access', {
        method, signal: timeout.signal, cache: 'no-store', credentials: 'same-origin',
        headers: {...authHeaders(), ...(method === 'POST' ? {'Content-Type': 'application/json'} : {})},
        ...(method === 'POST' ? {body: JSON.stringify(payload)} : {}),
      });
      const result = await resp.json();
      if (!resp.ok) throw new Error(result.error || 'Ошибка авторизации.');
      return result;
    } finally { clearTimeout(stop); }
  }
  function render(data) {
    const before = Boolean(window.accessState.premium);
    window.accessState = data;
    document.body.dataset.access = data.premium ? 'premium' : 'guest';
    const name = data.registered
      ? (data.username ? '@' + data.username : data.first_name || 'Telegram')
      : 'Войти';
    login.textContent = name;
    login.title = data.registered ? 'Аккаунт Telegram' : 'Войти через Telegram';
    const accountName = document.getElementById('account-username');
    const accountStatus = document.getElementById('account-premium-status');
    if (accountName) accountName.textContent = data.registered ? name : 'Гость';
    if (accountStatus) {
      accountStatus.textContent = data.premium ? 'Premium активен' : 'Premium неактивен';
      accountStatus.dataset.state = data.premium ? 'good' : 'warn';
    }
    premium.hidden = !data.registered || data.premium;
    nativeLogin.hidden = !(Boolean(app && app.initData) && data.signed_out === true);
    document.querySelectorAll('.access-open').forEach(button => {
      button.textContent = data.signed_out ? 'Войти через Telegram' : 'Войти / Получить доступ';
    });
    if (data.signed_out) toggleLogoutConfirm(false);
    document.getElementById('header-premium').hidden = !data.premium;
    if (data.premium) window.renderDemoBalance?.({balance_cents:data.balance_cents, currency:data.currency, balance_verified:data.balance_verified});
    else document.getElementById('header-balance').textContent = '—';
    nativeRegister.hidden = !(data.verified && !data.registered);
    keyForm.hidden = !(data.registered && !data.premium);
    logout.hidden = !data.verified || Boolean(app && app.initData);
    widget.hidden = Boolean(data.verified || (app && app.initData));
    if (data.premium) {
      headline.textContent = 'Premium активирован';
      explanation.textContent = 'Полный доступ к сводке, истории, DEMO и настройкам открыт.';
    } else if (data.signed_out) {
      headline.textContent = 'Войти через Telegram';
      explanation.textContent = 'Вы вышли из LuckyJet. Для восстановления доступа нажми кнопку входа. Premium и данные сохранены.';
    } else if (data.registered) {
      headline.textContent = 'Активировать Premium';
      explanation.textContent = 'Введи Premium-ключ или пароль доступа к Telegram-боту.';
    } else {
      headline.textContent = 'Войти через Telegram';
      explanation.textContent = 'Зарегистрируйся через Telegram, затем активируй ключ Premium.';
    }
    if (before !== Boolean(data.premium)) {
      if (!data.premium) {
        if (window.clearHistoryData) window.clearHistoryData();
        if (window.clearControlsData) window.clearControlsData();
        if (window.clearAdminData) window.clearAdminData();
        if (window.clearSiteData) window.clearSiteData();
      } else {
        const active = document.querySelector('nav button[aria-current="page"]')?.dataset.tab;
        if (active === 'demo' || active === 'account') window.openControlsTab?.();
        if (active === 'history') window.openHistoryTab?.();
        if (active === 'account') { window.openAdminTab?.(); window.openSiteTab?.(); }
      }
      window.refreshAccessOverview?.();
    }
  }
  async function refresh() {
    try { render(await api()); }
    catch (err) { message.textContent = err.message; }
  }
  async function submit(action) {
    if (busy) return;
    busy = true;
    message.textContent = 'Проверка…';
    nativeRegister.disabled = true;
    nativeLogin.disabled = true;
    if (accountLogoutButton) accountLogoutButton.disabled = true;
    keyForm.querySelector('button').disabled = true;
    try {
      const result = await api('POST', action);
      render(result);
      message.textContent = action.action === 'premium'
        ? 'Premium активирован.' : action.action === 'register'
          ? 'Telegram подтверждён. Для полного доступа активируй Premium.'
          : action.action === 'logout' ? 'Выход из LuckyJet выполнен.'
          : 'Вход подтверждён.';
      if (action.action === 'logout') toggleLogoutConfirm(false);
      if (result.premium) hideModal();
    } catch (err) {
      const errorText = err.name === 'AbortError' ? 'Сервер не ответил.' : err.message;
      message.textContent = errorText;
      if (action.action === 'logout' && accountLogoutStatus) accountLogoutStatus.textContent = errorText;
    } finally {
      busy = false;
      nativeRegister.disabled = false;
      nativeLogin.disabled = false;
      if (accountLogoutButton) accountLogoutButton.disabled = false;
      keyForm.querySelector('button').disabled = false;
    }
  }
  window.onTelegramAuth = user => {
    if (!user || typeof user !== 'object') return;
    submit({action: 'telegram_login', user});
  };
  function loadWidget() {
    if (widgetLoaded || (app && app.initData)) return;
    widgetLoaded = true;
    const script = document.createElement('script');
    script.async = true;
    script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.dataset.telegramLogin = 'jugo_luckyjet_signal_bot';
    script.dataset.size = 'large';
    script.dataset.radius = '14';
    script.dataset.onauth = 'onTelegramAuth(user)';
    script.dataset.requestAccess = 'read';
    widget.appendChild(script);
  }
  function showModal() {
    previousFocus = document.activeElement;
    modal.hidden = false;
    message.textContent = '';
    loadWidget();
    close.focus();
  }
  function hideModal() {
    modal.hidden = true;
    keyInput.value = '';
    if (previousFocus && previousFocus.focus) previousFocus.focus();
  }
  login.addEventListener('click', showModal);
  premium.addEventListener('click', showModal);
  document.querySelectorAll('.access-open').forEach(button => button.addEventListener('click', showModal));
  close.addEventListener('click', hideModal);
  modal.addEventListener('click', e => { if (e.target === modal) hideModal(); });
  modal.addEventListener('keydown', e => {
    if (e.key === 'Escape') hideModal();
    if (e.key !== 'Tab') return;
    const focusables = [...dialog.querySelectorAll('button:not(:disabled):not([hidden]),input:not([hidden]),a')].filter(el => el.getClientRects().length);
    if (!focusables.length) return;
    if (e.shiftKey && document.activeElement === focusables[0]) {
      e.preventDefault(); focusables[focusables.length - 1].focus();
    } else if (!e.shiftKey && document.activeElement === focusables[focusables.length - 1]) {
      e.preventDefault(); focusables[0].focus();
    }
  });
  nativeRegister.addEventListener('click', () => submit({action: 'register'}));
  nativeLogin.addEventListener('click', () => submit({action: 'login'}));
  accountLogout?.addEventListener('click', () => toggleLogoutConfirm(true));
  accountLogoutCancel?.addEventListener('click', () => toggleLogoutConfirm(false));
  accountLogoutButton?.addEventListener('click', () => submit({action: 'logout'}));
  keyForm.addEventListener('submit', e => {
    e.preventDefault();
    const key = keyInput.value.trim();
    keyInput.value = '';
    submit({action: 'premium', key});
  });
  logout.addEventListener('click', () => submit({action: 'logout'}));
  window.canUsePremium = () => Boolean(window.accessState?.premium);
  window.openAccessModal = showModal;
  window.refreshAccessState = refresh;
  refresh();
})();
