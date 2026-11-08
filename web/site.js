/* Site login is owner-only. Credentials/OTP stay in form memory for one request only. */
(() => {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const page = document.getElementById('page-account');
  const section = document.getElementById('site-section');
  const disclosure = document.getElementById('site-connection-panel');
  const preview = document.getElementById('site-connect-preview');
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
    if (disclosure) disclosure.hidden = false;
    if (preview) preview.textContent = labels[data.phase] || 'Проверка';
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
    if (!window.canUsePremium?.()) throw new Error('Требуется Premium.');
    const stop = new AbortController();
    const timeout = setTimeout(() => stop.abort(), 8000);
    try {
      const resp = await fetch('/api/site', {
        method, signal: stop.signal, cache: 'no-store', credentials: 'same-origin',
        headers: {...(tg?.initData ? {Authorization: 'tma ' + tg.initData} : {}),
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

  /* Manual Japanese-proxy browser: no VNC port or publicly accessible session.
     Requests always carry Telegram's signed initData; secret fields are cleared. */
  const technicalTools = document.createElement('details');
  technicalTools.id = 'site-technical-tools';
  technicalTools.className = 'minimal-disclosure technical-tools';
  const technicalHeading = document.createElement('summary');
  technicalHeading.textContent = 'Инструменты и диагностика';
  technicalTools.appendChild(technicalHeading);
  section.appendChild(technicalTools);

  const remoteLaunch = document.createElement('button');
  remoteLaunch.type = 'button';
  remoteLaunch.className = 'history-outline';
  remoteLaunch.textContent = 'Открыть удалённый браузер · Япония';
  remoteLaunch.style.cssText = 'width:100%;min-height:40px;margin-top:9px';
  technicalTools.appendChild(remoteLaunch);
  const proxyCheck = document.createElement('button');
  proxyCheck.type = 'button';
  proxyCheck.className = 'history-outline';
  proxyCheck.textContent = 'Проверить японский IP сервера';
  proxyCheck.style.cssText = 'width:100%;min-height:40px;margin-top:7px';
  technicalTools.appendChild(proxyCheck);
  const proxyResult = document.createElement('div');
  proxyResult.setAttribute('role', 'status');
  proxyResult.style.cssText = 'font-size:13px;line-height:1.5;overflow-wrap:anywhere;padding:8px 2px';
  technicalTools.appendChild(proxyResult);
  const phoneIPLink = document.createElement('a');
  phoneIPLink.textContent = 'Проверить IP на POCO для сравнения';
  phoneIPLink.href = 'https://api.ipify.org';
  phoneIPLink.target = '_blank';
  phoneIPLink.rel = 'noopener noreferrer';
  phoneIPLink.style.cssText = 'display:block;font-size:13px;margin:2px 0 12px;color:#92bcff';
  technicalTools.appendChild(phoneIPLink);
  const happCheck = document.createElement('button');
  happCheck.type = 'button';
  happCheck.className = 'history-outline';
  happCheck.textContent = 'Проверить два узла Happ отдельно';
  happCheck.style.cssText = 'width:100%;min-height:40px;margin-top:7px';
  technicalTools.appendChild(happCheck);
  const happResult = document.createElement('div');
  happResult.setAttribute('role', 'status');
  happResult.style.cssText = 'font-size:13px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;padding:8px 2px';
  technicalTools.appendChild(happResult);
  const remoteCSS = document.createElement('style');
  remoteCSS.textContent = [
    '#remote-browser-overlay{position:fixed;inset:0;z-index:130;overflow-y:auto;background:#100e18;color:#fff;padding:9px 9px 30px}',
    '#remote-browser-overlay[hidden]{display:none!important}',
    '.remote-bar{position:sticky;top:0;z-index:4;background:#201a2c;border:1px solid #483958;border-radius:13px;padding:9px;box-shadow:0 5px 20px #0007}',
    '.remote-line{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:5px 0}',
    '.remote-line button{flex:1;min-height:42px;border:1px solid #765c9a;border-radius:10px;background:#3a2b56;color:#fff;font-size:13px}',
    '.remote-line button:disabled{opacity:.55}',
    '.remote-line input{flex:1;min-width:0;width:100%;font:inherit;padding:12px;border:1px solid #765c9a;border-radius:10px;background:#171321;color:#fff}',
    '#remote-browser-image{width:100%;height:auto;display:block;touch-action:manipulation;user-select:none;border:1px solid #514360;border-radius:8px;margin-top:9px;background:#252331;cursor:pointer}',
    '#remote-browser-status{font-size:12px;line-height:1.35;color:#d1c5e3;min-height:18px;margin:6px 2px}',
  ].join('');
  document.head.appendChild(remoteCSS);
  const overlay = document.createElement('div');
  overlay.id = 'remote-browser-overlay';
  overlay.hidden = true;
  overlay.innerHTML = [
    '<div class="remote-bar">',
    '<div class="remote-line"><button id="remote-close" type="button">Закрыть</button><button id="remote-save" type="button">Сохранить вход</button></div>',
    '<div id="remote-browser-status" role="status">Браузер подключается…</div>',
    '<div class="remote-line"><input id="remote-text" type="email" autocomplete="off" spellcheck="false" placeholder="Почта"><button id="remote-send-text" type="button">Ввести почту</button></div>',
    '<div class="remote-line"><input id="remote-secret" type="password" autocomplete="new-password" placeholder="Пароль"><button id="remote-send-secret" type="button">Ввести пароль</button></div>',
    '<div class="remote-line"><input id="remote-code" type="text" inputmode="numeric" autocomplete="off" placeholder="Код из почты (если потребуется)"><button id="remote-send-code" type="button">Ввести код</button></div>',
    '<div class="remote-line"><button id="remote-enter" type="button">Enter</button><button id="remote-tab" type="button">Tab</button><button id="remote-backspace" type="button">⌫</button><button id="remote-up" type="button">↑</button><button id="remote-down" type="button">↓</button></div>',
    '<div class="remote-line"><button id="remote-drag-mode" type="button" aria-pressed="false">CAPTCHA: включить перетаскивание</button></div>',
    '</div>',
    '<img id="remote-browser-image" alt="Экран удалённого браузера. Нажми на поле, затем введи текст выше." width="430" height="932" draggable="false">',
    '<p style="font-size:12px;color:#bcb0cd;margin:10px 3px">Для CAPTCHA прокрути снимок до ползунка, включи «CAPTCHA: перетаскивание», затем проведи пальцем от ползунка до выбранной точки. Движение передаётся серверу во время удержания пальца. В остальном — обычные касания. Время сессии ограничено.</p>',
  ].join('');
  document.body.appendChild(overlay);
  const remoteStatus = overlay.querySelector('#remote-browser-status');
  const remoteImage = overlay.querySelector('#remote-browser-image');
  const remoteText = overlay.querySelector('#remote-text');
  const remoteSecret = overlay.querySelector('#remote-secret');
  const remoteCode = overlay.querySelector('#remote-code');
  const remoteDragButton = overlay.querySelector('#remote-drag-mode');
  let remoteDragMode = false;
  let remoteDragTrace = null;
  let remoteGesture = null;
  let remoteGestureCounter = 0;
  let remoteFrameBusy = false;
  let remoteTimer = null;
  let remoteBusy = false;
  let remoteActive = false;
  function remoteNotice(message) { remoteStatus.textContent = message; }
  function remoteSchedule() {
    clearTimeout(remoteTimer);
    if (remoteActive && !overlay.hidden && !document.hidden && !remoteFrameBusy)
      remoteTimer = setTimeout(remoteFrame, remoteGesture ? 320 : 650);
  }
  async function remoteAPI(method, data, timeoutMs = 12000) {
    if (!tg?.initData || !window.canUsePremium?.())
      throw new Error('Открой Mini App из Telegram с правами владельца.');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch('/api/browser', {
        method, signal: controller.signal, cache: 'no-store', credentials: 'same-origin',
        headers: {Authorization: 'tma ' + tg.initData,
          ...(method === 'POST' ? {'Content-Type': 'application/json'} : {})},
        ...(method === 'POST' ? {body: JSON.stringify(data)} : {}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Сервер не ответил.');
      return result;
    } finally { clearTimeout(timer); }
  }
  proxyCheck.addEventListener('click', async () => {
    if (proxyCheck.disabled) return;
    proxyCheck.disabled = true;
    proxyResult.textContent = 'Проверяем выходной IP через Xray…';
    try {
      const data = await remoteAPI('POST', {action:'check_ip'}, 19000);
      const country = data.country === 'JP' ? 'Япония' : data.country;
      proxyResult.textContent = 'Сервер: ' + data.ip + ' · Страна: ' + country
        + (data.japan_verified
          ? '. Сравни этот адрес с IP на POCO.'
          : '. Внимание: японский IP не подтверждён. Вход не выполняй.');
    } catch (err) {
      proxyResult.textContent = err.name === 'AbortError'
        ? 'Проверка IP не завершилась.'
        : err.message;
    } finally {
      proxyCheck.disabled = false;
    }
  });
  happCheck.addEventListener('click', async () => {
    if (happCheck.disabled) return;
    happCheck.disabled = true;
    happResult.textContent = 'Проверка двух узлов через отдельный Xray…';
    try {
      const data = await remoteAPI('POST', {action:'check_happ'}, 60000);
      const labels = {primary:'Основной Happ', fallback:'Резервный Happ'};
      const statuses = {
        not_configured:'Не настроен', socks_not_ready:'SOCKS не запустился',
        node_or_runtime_error:'Ошибка узла или конфигурации',
        egress_check_failed:'Проверка выхода не прошла',
      };
      const rows = (data.nodes || []).map(item => {
        const label = labels[item.node] || 'Узел';
        return item.status === 'ok'
          ? label + ': ' + item.ip + ' · ' + item.country
          : label + ': ' + (statuses[item.status] || 'Ошибка проверки');
      });
      happResult.textContent = rows.join('\n') + (data.cached ? '\nРезультат из кеша.' : '');
    } catch (err) {
      happResult.textContent = err.name === 'AbortError'
        ? 'Время проверки истекло.' : err.message;
    } finally {
      happCheck.disabled = false;
    }
  });
  async function remoteFrame() {
    if (remoteFrameBusy || !remoteActive || overlay.hidden) return;
    remoteFrameBusy = true;
    try {
      const result = await remoteAPI('GET', undefined, 6000);
      if (!result.active) {
        remoteActive = false;
        remoteNotice('Браузер закрыт. Открой заново.');
      } else {
        if (result.image) remoteImage.src = 'data:image/jpeg;base64,' + result.image;
        if (!remoteGesture)
          remoteNotice('Япония · Время до закрытия: ' + Math.floor(result.seconds_remaining / 60) + ' мин.');
      }
    } catch (err) {
      if (!remoteGesture) remoteNotice(err.message);
    } finally {
      remoteFrameBusy = false;
      remoteSchedule();
    }
  }
  async function remoteAction(data) {
    if (remoteBusy || !remoteActive || remoteGesture) return;
    remoteBusy = true;
    clearTimeout(remoteTimer);
    try {
      const result = await remoteAPI('POST', data, 17000);
      if (result.saved) {
        remoteActive = false;
        remoteImage.removeAttribute('src');
        overlay.hidden = true;
        document.body.style.overflow = '';
        notice.textContent = result.message;
        await load(true);
      } else {
        remoteNotice(result.message || 'Выполнено.');
      }
    } catch (err) { remoteNotice(err.message); }
    finally { remoteBusy = false; remoteSchedule(); }
    if (remoteActive) await remoteFrame();
  }
  remoteLaunch.addEventListener('click', async () => {
    if (remoteBusy) return;
    remoteBusy = true;
    overlay.hidden = false;
    document.body.style.overflow = 'hidden';
    remoteNotice('Подключаем защищённый браузер через Японию…');
    try {
      await remoteAPI('POST', {action:'start'}, 75000);
      remoteActive = true;
    } catch (err) {
      remoteNotice(err.message);
    } finally { remoteBusy = false; }
    if (remoteActive) await remoteFrame();
  });
  overlay.querySelector('#remote-close').addEventListener('click', async () => {
    clearTimeout(remoteTimer);
    remoteActive = false;
    overlay.hidden = true;
    document.body.style.overflow = '';
    remoteText.value = '';
    remoteSecret.value = '';
    remoteCode.value = '';
    remoteImage.removeAttribute('src');
    remoteDragTrace = null;
    remoteGesture = null;
    try { await remoteAPI('POST', {action:'close'}); } catch (_) {}
  });
  function remotePoint(event) {
    const rect = remoteImage.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
    return [Number(x.toFixed(4)), Number(y.toFixed(4))];
  }
  remoteDragButton.addEventListener('click', () => {
    if (remoteGesture) return;
    remoteDragMode = !remoteDragMode;
    remoteDragTrace = null;
    remoteImage.style.touchAction = remoteDragMode ? 'none' : 'manipulation';
    remoteDragButton.setAttribute('aria-pressed', String(remoteDragMode));
    remoteDragButton.textContent = remoteDragMode
      ? 'CAPTCHA: перетаскивание ВКЛ · выключить'
      : 'CAPTCHA: включить перетаскивание';
    remoteNotice(remoteDragMode
      ? 'Двигай пальцем по изображению от бегунка к нужному месту и отпускай.'
      : 'Обычные касания включены.');
  });
  remoteImage.addEventListener('click', event => {
    if (!remoteActive || remoteBusy || !remoteImage.src || remoteDragMode) return;
    const [x, y] = remotePoint(event);
    remoteAction({action:'tap', x, y});
  });
  // Forward actual finger down/move/up events, not a prerecorded gesture.
  // Each move replaces any queued unsent move; network lag cannot accumulate.
  async function remoteGesturePump(gesture) {
    if (gesture.running || remoteGesture !== gesture || !remoteActive) return;
    gesture.running = true;
    try {
      if (!gesture.serverDown) {
        await remoteAPI('POST', {
          action:'pointer', phase:'down', gesture:gesture.token,
          x:gesture.start[0], y:gesture.start[1],
        }, 6500);
        gesture.serverDown = true;
      }
      while (remoteGesture === gesture && remoteActive) {
        if (gesture.end) {
          await remoteAPI('POST', {
            action:'pointer', phase:'up', gesture:gesture.token,
            x:gesture.end[0], y:gesture.end[1],
          }, 6500);
          remoteGesture = null;
          remoteNotice('Ползунок отпущен. Проверь ответ CAPTCHA.');
          break;
        }
        if (!gesture.next) break;
        const position = gesture.next;
        gesture.next = null;
        await remoteAPI('POST', {
          action:'pointer', phase:'move', gesture:gesture.token,
          x:position[0], y:position[1],
        }, 6500);
      }
    } catch (err) {
      if (remoteGesture === gesture) {
        remoteGesture = null;
        remoteNotice('Передача движения прервана: ' + err.message);
      }
      if (gesture.serverDown) {
        const point = gesture.end || gesture.last || gesture.start;
        try {
          await remoteAPI('POST', {
            action:'pointer', phase:'up', gesture:gesture.token,
            x:point[0], y:point[1],
          }, 3500);
        } catch (_) { /* Server watchdog releases held mouse after 3s. */ }
      }
    } finally {
      gesture.running = false;
      if (remoteGesture === gesture && (gesture.end || gesture.next))
        remoteGesturePump(gesture);
      else remoteSchedule();
    }
  }
  function finishRemoteGesture(event, cancelled=false) {
    const gesture = remoteDragTrace;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    event.preventDefault();
    remoteDragTrace = null;
    if (remoteImage.hasPointerCapture(event.pointerId))
      remoteImage.releasePointerCapture(event.pointerId);
    gesture.end = cancelled ? (gesture.last || gesture.start) : remotePoint(event);
    gesture.next = null;
    remoteNotice(cancelled
      ? 'Удержание прервано. Отпускаем ползунок на сервере…'
      : 'Отпускаем ползунок на сервере…');
    remoteGesturePump(gesture);
  }
  remoteImage.addEventListener('pointerdown', event => {
    if (!remoteDragMode || !remoteActive || remoteBusy || remoteGesture || !remoteImage.src) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    remoteImage.setPointerCapture(event.pointerId);
    const position = remotePoint(event);
    const gesture = {
      pointerId:event.pointerId,
      token:(remoteGestureCounter = remoteGestureCounter % 2147483646 + 1),
      start:position, last:position, next:null, end:null,
      running:false, serverDown:false,
    };
    remoteDragTrace = remoteGesture = gesture;
    remoteNotice('Удержание началось. Веди ползунок пальцем.');
    remoteGesturePump(gesture);
  });
  remoteImage.addEventListener('pointermove', event => {
    const gesture = remoteDragTrace;
    if (!gesture || gesture.pointerId !== event.pointerId || gesture.end) return;
    event.preventDefault();
    const position = remotePoint(event);
    if (Math.abs(position[0] - gesture.last[0]) + Math.abs(position[1] - gesture.last[1]) < .004) return;
    gesture.last = gesture.next = position;
    remoteGesturePump(gesture);
  });
  remoteImage.addEventListener('pointerup', event => finishRemoteGesture(event));
  remoteImage.addEventListener('pointercancel', event => finishRemoteGesture(event, true));
  async function remoteWrite(field, operation) {
    const value = field.value;
    field.value = '';
    if (value) await remoteAction({action:operation, text:value});
  }
  overlay.querySelector('#remote-send-text').addEventListener('click', () => remoteWrite(remoteText, 'fill_email'));
  overlay.querySelector('#remote-send-secret').addEventListener('click', () => remoteWrite(remoteSecret, 'fill_password'));
  overlay.querySelector('#remote-send-code').addEventListener('click', () => remoteWrite(remoteCode, 'type'));
  overlay.querySelector('#remote-enter').addEventListener('click', () => remoteAction({action:'key', key:'Enter'}));
  overlay.querySelector('#remote-tab').addEventListener('click', () => remoteAction({action:'key', key:'Tab'}));
  overlay.querySelector('#remote-backspace').addEventListener('click', () => remoteAction({action:'key', key:'Backspace'}));
  overlay.querySelector('#remote-up').addEventListener('click', () => remoteAction({action:'scroll', direction:'up'}));
  overlay.querySelector('#remote-down').addEventListener('click', () => remoteAction({action:'scroll', direction:'down'}));
  overlay.querySelector('#remote-save').addEventListener('click', () => remoteAction({action:'save'}));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(remoteTimer);
    else remoteSchedule();
  });

  window.openSiteTab = () => load();
  window.clearSiteData = () => {
    clearTimeout(timer);
    section.hidden = true;
    if (disclosure) { disclosure.hidden = true; disclosure.open = false; }
    if (preview) preview.textContent = '—';
    visible = false;
    lastLoad = 0;
    state = null;
    password.value = ''; code.value = ''; email.value = '';
    notice.textContent = ''; status.textContent = '';
  };
})();
