import asyncio
from pathlib import Path
import sys
import pytest

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
