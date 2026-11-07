import asyncio
from pathlib import Path
import sys
import pytest


def test_phone_dialog_switches_to_email_before_clicking_covered_header():
    from site_login_browser import SiteLoginBrowser
    html = '''<meta charset="utf-8"><button>Войти</button>
      <div role="dialog" style="position:fixed;inset:0;background:white">
        <h2>Вход</h2><button onclick="document.querySelector('input').hidden=false">Почта</button>
        <input type="email" hidden><input type="password">
      </div>'''
    async def run():
        async with SiteLoginBrowser('https://example.test/login', result_timeout=2) as driver:
            driver.page.set_default_timeout(750)
            await driver.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html))
            await driver.page.goto(driver.login_url)
            email, password = await driver._open_form()
            assert await email.is_visible() and await password.is_visible()
    asyncio.run(run())


def test_site_login_uses_named_header_button_then_email_tab():
    from site_login_browser import SiteLoginBrowser
    html = '''<meta charset="utf-8">
      <button id="open" onclick="document.querySelector('[role=dialog]').hidden=false">Вход</button>
      <button>Войти</button>
      <div role="dialog" hidden>
        <button onclick="document.querySelector('#email').hidden=false">Почта</button>
        <input id="email" type="email" hidden><input type="password">
      </div>'''
    async def run():
        async with SiteLoginBrowser('https://example.test/login', result_timeout=2) as driver:
            await driver.context.route('**/*', lambda route: route.fulfill(
                content_type='text/html', body=html))
            await driver.page.goto(driver.login_url)
            email, password = await driver._open_form()
            assert await email.is_visible()
            assert await password.is_visible()
    asyncio.run(run())


def test_blocked_opening_click_reports_evidence_without_dom_text():
    from site_account import LoginProblem
    from site_login_browser import SiteLoginBrowser
    html = '''<meta charset="utf-8"><button>Войти</button>
      <div style="position:fixed;inset:0;z-index:10">private-page-text</div>'''
    async def run():
        async with SiteLoginBrowser('https://example.test/login') as driver:
            await driver.context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=html))
            with pytest.raises(LoginProblem) as error:
                await driver.login('owner@example.test', 'private-password', None)
            message = str(error.value)
            assert 'перекрывает другой элемент' in message
            assert 'private' not in message
            assert 'example.test' not in message
            assert error.value.page_screenshot.startswith(b'\x89PNG\r\n\x1a\n')
    asyncio.run(run())


def test_custom_login_dialog_submits_its_own_button():
    from site_login_browser import SiteLoginBrowser
    html = '''<meta charset="utf-8"><button id="background">Войти</button>
      <div style="position:fixed;inset:0;background:white;z-index:5">
       <div><input type="email"><input type="password"></div>
       <button onclick="this.parentNode.remove();document.querySelector('#logout').hidden=false">Войти</button>
      </div><button id="logout" hidden>Выйти</button>'''
    async def run():
        async with SiteLoginBrowser('https://example.test/login', result_timeout=1) as driver:
            driver.page.set_default_timeout(1000)
            await driver.context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=html))
            state = await driver.login('owner@example.test', 'fixture-password', None)
            assert 'cookies' in state
    asyncio.run(run())

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]


def test_login_classification_requires_positive_evidence():
    from site_login_browser import classify_login
    assert classify_login(success=False, code=False, error=False, challenge=False) == 'waiting'
    assert classify_login(success=True, code=False, error=False, challenge=False) == 'success'
    assert classify_login(success=True, code=True, error=False, challenge=False) == 'code'
    assert classify_login(success=False, code=False, error=True, challenge=False) == 'error'
    assert classify_login(success=True, code=False, error=False, challenge=True) == 'challenge'


def test_credential_origin_is_pinned_to_configured_https_site():
    from site_login_browser import trusted_origin
    assert trusted_origin('https://example.test/path', 'https://example.test/login')
    assert not trusted_origin('https://evil.test/login', 'https://example.test/login')
    assert not trusted_origin('http://example.test/login', 'https://example.test/login')
    assert not trusted_origin('https://example.test.evil.test/', 'https://example.test/')


@pytest.mark.parametrize('success_delay', [0, 700])
def test_real_browser_email_code_retry_and_verified_storage(success_delay):
    from site_login_browser import SiteLoginBrowser

    html = '''<!doctype html><html><head><meta charset="utf-8"></head><body>
      <button onclick="document.querySelector('form').hidden=false">Войти</button>
      <form hidden onsubmit="event.preventDefault(); document.querySelector('#otp').hidden=false; this.hidden=true">
        <input type="email"><input type="password"><button type="submit">Войти</button>
      </form>
      <div id="otp" hidden>
        <input autocomplete="one-time-code">
        <button onclick="if(document.querySelector('#otp input').value==='654321'){
          setTimeout(()=>{document.querySelector('#otp').hidden=true;document.querySelector('#logout').hidden=false;
          localStorage.setItem('session','fixture-token');},SUCCESS_DELAY);
        }else{document.querySelector('#invalid').hidden=false}">Подтвердить</button>
        <div id="invalid" role="alert" hidden>Неверный код</div>
      </div>
      <button id="logout" hidden>Выйти</button>
    </body></html>'''

    html = html.replace('SUCCESS_DELAY', str(success_delay))
    async def run():
        driver = SiteLoginBrowser('https://example.test/login', result_timeout=2,
                                  selectors={'success': '#logout'})
        codes = iter(['123456', '654321'])
        retries = []
        async def code(retry):
            retries.append(retry)
            return next(codes)
        async with driver:
            await driver.context.route('https://example.test/**',
                lambda route: route.fulfill(status=200, content_type='text/html', body=html))
            state = await driver.login('owner@example.test', 'fixture-password', code)
        assert retries == [False, True]
        assert state['origins'][0]['localStorage'] == [{'name': 'session', 'value': 'fixture-token'}]
    asyncio.run(run())


def test_real_browser_does_not_treat_disappearing_form_as_login():
    import pytest
    from site_account import LoginProblem
    from site_login_browser import SiteLoginBrowser
    html = '''<meta charset="utf-8"><form onsubmit="event.preventDefault();this.remove()">
      <input type="email"><input type="password"><button>Войти</button></form>'''
    async def run():
        async with SiteLoginBrowser('https://example.test/login', result_timeout=.1) as driver:
            await driver.context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=html))
            with pytest.raises(LoginProblem, match='подтверждён'):
                await driver.login('owner@example.test', 'password', None)
    asyncio.run(run())


def test_logged_out_profile_button_is_not_login_evidence():
    import pytest
    from site_account import LoginProblem
    from site_login_browser import SiteLoginBrowser
    html = '''<meta charset="utf-8"><button aria-label="Профиль">Профиль</button>
      <form onsubmit="event.preventDefault();this.remove()">
      <input type="email"><input type="password"><button>Войти</button></form>'''
    async def run():
        async with SiteLoginBrowser('https://example.test/login', result_timeout=.8) as driver:
            await driver.context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=html))
            with pytest.raises(LoginProblem, match='подтверждён'):
                await driver.login('owner@example.test', 'password', None)
    asyncio.run(run())



def test_japanese_site_login_never_uses_direct_connection():
    from site_account import LoginProblem
    from site_login_browser import SiteLoginBrowser
    async def run():
        with pytest.raises(LoginProblem, match='прямое подключение запрещено'):
            async with SiteLoginBrowser('https://example.test/login', expected_country='JP'):
                pass
    asyncio.run(run())


@pytest.mark.parametrize('country,accepted', [('JP', True), ('SG', False)])
def test_site_egress_country_checked_over_socks_only(monkeypatch, country, accepted):
    import site_login_browser as site
    requests = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'country': country}
    class Client:
        def __init__(self, **kwargs): requests.append(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            assert url == 'https://api.country.is/'
            return Response()
    monkeypatch.setattr(site.httpx, 'AsyncClient', Client)
    async def run():
        if accepted:
            await site.verify_proxy_country('socks5://127.0.0.1:10808', 'JP')
        else:
            from site_account import LoginProblem
            with pytest.raises(LoginProblem, match='страна: SG'):
                await site.verify_proxy_country('socks5://127.0.0.1:10808', 'JP')
    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0]['proxy'] == 'socks5://127.0.0.1:10808'
    assert requests[0]['trust_env'] is False


def test_site_login_chromium_receives_proxy_and_keeps_context(monkeypatch):
    import site_login_browser as site
    calls = []
    class Page:
        def set_default_timeout(self, n): pass
    class Context:
        async def new_page(self): return Page()
    class Browser:
        async def new_context(self, **kwargs):
            calls.append(('context', kwargs))
            return Context()
        async def close(self): calls.append(('closed', True))
    class Chromium:
        async def launch(self, **kwargs):
            calls.append(('launch', kwargs))
            return Browser()
    class PW:
        chromium = Chromium()
    class Manager:
        async def __aenter__(self): return PW()
        async def __aexit__(self, *args): pass
    async def verifier(proxy, country):
        calls.append(('verification', (proxy, country)))
    monkeypatch.setattr(site, 'verify_proxy_country', verifier)
    async def run():
        async with site.SiteLoginBrowser(
            'https://example.test/login', proxy_url='socks5://127.0.0.1:10808',
            expected_country='JP', playwright_factory=lambda: Manager()
        ):
            pass
    asyncio.run(run())
    assert calls[0] == ('verification', ('socks5://127.0.0.1:10808', 'JP'))
    assert calls[1] == ('launch', {'headless': True, 'proxy': {'server': 'socks5://127.0.0.1:10808'}})
    assert calls[2][0] == 'context'
    assert calls[3] == ('closed', True)


def test_timeout_with_open_login_form_reports_unconfirmed_submission(caplog):
    from site_account import LoginProblem
    from site_login_browser import SiteLoginBrowser
    html = """<form onsubmit="event.preventDefault()"><input type="email">
        <input type="password"><button>Войти</button></form>"""
    async def run():
        async with SiteLoginBrowser('https://example.test/login', result_timeout=.15) as driver:
            await driver.context.route('**/*', lambda route: route.fulfill(
                status=200, content_type='text/html', body=html))
            with pytest.raises(LoginProblem, match='Форма входа осталась открыта'):
                await driver.login('private@example.test', 'secret789', None)
    asyncio.run(run())
    assert 'secret789' not in caplog.text
    assert 'private@example.test' not in caplog.text


def test_login_opener_recovers_from_spa_click_timeout_when_dialog_already_open():
    from playwright.async_api import TimeoutError as BrowserTimeout
    from site_login_browser import SiteLoginBrowser
    class FakeButton:
        async def click(self):
            raise BrowserTimeout('Timeout 5000ms exceeded')
    driver = SiteLoginBrowser('https://example.test/login')
    async def field(name):
        return object() if name == 'password' else None
    driver._field = field
    async def run():
        await driver._click(FakeButton(), 'открытия входа')
    asyncio.run(run())


def test_login_opener_dom_fallback_requires_visible_uncovered_enabled(monkeypatch):
    from playwright.async_api import TimeoutError as BrowserTimeout
    from site_account import LoginProblem
    from site_login_browser import SiteLoginBrowser
    class Page:
        url = 'https://example.test/login'
        async def screenshot(self, **kwargs): return b'PNG'
    class FakeButton:
        def __init__(self, covered):
            self.covered = covered
            self.dispatched = False
        async def click(self):
            raise BrowserTimeout('Timeout 5000ms exceeded')
        async def evaluate(self, script, **kwargs):
            return {'visible': True, 'uncovered': not self.covered, 'enabled': True}
        async def dispatch_event(self, event, **kwargs):
            assert event == 'click'
            self.dispatched = True
    driver = SiteLoginBrowser('https://example.test/login')
    driver.page = Page()
    async def field(name): return None
    async def challenge(): return False
    driver._field = field
    driver._challenge = challenge
    async def run():
        allowed = FakeButton(covered=False)
        await driver._click(allowed, 'открытия входа')
        assert allowed.dispatched
        blocked = FakeButton(covered=True)
        with pytest.raises(LoginProblem, match='перекрывает другой элемент'):
            await driver._click(blocked, 'открытия входа')
        assert not blocked.dispatched
    asyncio.run(run())
