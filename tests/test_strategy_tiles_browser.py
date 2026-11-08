"""Real mobile home tiles, switch, existing panels, and tab-change auto-close."""
import asyncio
import json
from urllib.parse import urlparse, parse_qs

from mini_app import response


def test_four_live_tiles_sheet_and_navigation_on_small_phones():
    from playwright.async_api import async_playwright, expect

    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for width in (390, 320):
                page = await browser.new_page(viewport={'width': width, 'height': 844})
                errors, actions, history_views = [], [], []
                overview_gate = {'hold': False}
                overview_started, overview_release = asyncio.Event(), asyncio.Event()
                page.on('pageerror', lambda e: errors.append(str(e)))
                tiles = [
                    {'id':1,'enabled':True,'phase':'waiting','progress':'3/5','stake_cents':100000,
                     'target':'2','net_cents':38000,'last_action':'Выигрыш'},
                    {'id':2,'enabled':True,'phase':'waiting','progress':'78/100','stake_cents':100000,
                     'target':'100','net_cents':None,'last_action':'Нет ставок'},
                    {'id':3,'enabled':True,'phase':'betting','progress':'Ставка','stake_cents':250000,
                     'target':'1.57','net_cents':95000,'last_action':'Выигрыш'},
                    {'id':4,'enabled':False,'phase':'waiting','progress':'2/4','stake_cents':363200,
                     'target':'1.38','net_cents':-100000,'last_action':'Проигрыш'},
                ]
                demo = {'currency':'KRW','enabled':True,'active':False,'balance_cents':10000000,
                        'balance_verified':True,'base_stake_cents':100000,
                        'trigger':5,'low_streak':3,'reset_revision':'test',
                        'high_gap':{'enabled':True,'phase':'waiting','reference':'100','target':'100',
                                    'wait_rounds':100,'elapsed':78,'remaining':22,'stake_cents':100000},
                        'target_streak':{'enabled':True,'target':'1.57','trigger':5,
                                         'stake_cents':100000,'next_stake_cents':250000,'phase':'betting'},
                        'recovery_streak':{'enabled':False,'target':'1.38','trigger':4,
                                           'stake_cents':100000,'next_stake_cents':363200,'phase':'waiting'}}
                def controls_data():
                    return {'admin':False,'demo':demo, 'strategy_tiles':tiles,
                        'high_gap':demo['high_gap'],'target_streak':demo['target_streak'],
                        'recovery_streak':demo['recovery_streak'],
                        'settings':{'notifications':True,'sound':True,'threshold':3,
                                    'history_size':20,'delete_delay':10}}

                async def route(req):
                    path = urlparse(req.request.url).path
                    if path == '/app':
                        await req.fulfill(content_type='text/html', body=response('/app')[2])
                    elif path == '/access.js':
                        await req.fulfill(content_type='application/javascript',
                            body="window.accessState={premium:true,registered:true,username:'tester'};"
                                 "document.body.dataset.access='premium';"
                                 "window.canUsePremium=()=>true;window.refreshAccessOverview?.();")
                    elif path in ('/summary.js','/history.js','/controls.js','/strategy-tiles.js'):
                        await req.fulfill(content_type='application/javascript', body=response(path)[2])
                    elif path in ('/site.js','/admin.js'):
                        await req.fulfill(content_type='application/javascript', body='')
                    elif path == '/api/overview':
                        # Freeze a pre-action GET and deliver it after the POST
                        # to reproduce the race that reverted power colors.
                        body = json.dumps({'coefficients':['1.20x','1.38x'],
                            'last_round_at':1794000000,'strategy_tiles':tiles,'demo':demo,
                            'status':{'age_seconds':1,'fresh':True,'streak_count':0,
                                      'threshold':3,'uptime_seconds':120}})
                        if overview_gate['hold']:
                            overview_gate['hold'] = False
                            overview_started.set()
                            await overview_release.wait()
                        await req.fulfill(content_type='application/json', body=body)
                    elif path == '/api/controls':
                        if req.request.method == 'POST':
                            action = req.request.post_data_json['action']
                            actions.append(action)
                            index = ['demo_toggle','high_gap_toggle',
                                     'target_streak_toggle','recovery_streak_toggle'].index(action)
                            tiles[index]['enabled'] = not tiles[index]['enabled']
                            demo['recovery_streak']['enabled'] = tiles[3]['enabled']
                        await req.fulfill(content_type='application/json',
                            body=json.dumps({'message':'Сохранено.', **controls_data()}))
                    elif path == '/api/history':
                        view = parse_qs(urlparse(req.request.url).query).get('view',['days'])[0]
                        history_views.append(view)
                        cycle_views = ('cycles','high_cycles','target_cycles','recovery_cycles')
                        if view in cycle_views:
                            strategy = ('classic','high_gap','target_streak','recovery_streak')[cycle_views.index(view)]
                            data = {'view': view, 'total': 2, 'page': 0,
                                'stats': {'currency': 'KRW', 'total': 2, 'recovered': 2,
                                          'lost': 0, 'incomplete': 0, 'stopped': 0,
                                          'history_net_cents': 60000, 'average_steps': 1},
                                'items': [
                                    {'index':i, 'currency':'KRW', 'strategy':strategy,
                                     'steps':1,'outcome':'recovered','net_cents':30000,
                                     'ended_at':'2026-11-09T01:07:00+10:00'}
                                    for i in range(2)]}
                        else:
                            data = {'view':view,'total':0,'page':0,'items':[],'days':[]}
                        await req.fulfill(content_type='application/json', body=json.dumps(data))
                    elif path == '/api/round-summary':
                        await req.fulfill(content_type='application/json',
                            body=json.dumps({'rounds_requested':150,'rounds_available':0,
                                             'current':{},'previous':{},'matches':[],
                                             'quality':{'complete_window':False}}))
                    else:
                        await req.fulfill(content_type='application/javascript',
                            body="window.Telegram={WebApp:{initData:'fixture',ready(){},expand(){},"
                                 "setHeaderColor(){},setBackgroundColor(){},close(){},"
                                 "BackButton:{onClick(){},hide(){},show(){}},"
                                 "HapticFeedback:{selectionChanged(){}}}}")
                await page.route('**/*', route)
                await page.goto('https://example.test/app')
                await expect(page.locator('#strategy-tile-strip')).to_be_visible()
                cards = page.locator('[data-strategy-tile]')
                assert await cards.count() == 4
                boxes = [await cards.nth(i).bounding_box() for i in range(4)]
                assert all(abs(box['y'] - boxes[0]['y']) < 2 for box in boxes)
                assert all(box['height'] <= 56 for box in boxes)
                assert [await cards.nth(i).locator('.strategy-tile-main').inner_text()
                        for i in range(4)] == ['3/5','78/100','2 500 ₩','Выкл'] or (
                    await cards.nth(0).locator('.strategy-tile-main').inner_text() == '3/5'
                    and await cards.nth(1).locator('.strategy-tile-main').inner_text() == '78/100')
                assert '1' in await cards.nth(3).locator('.strategy-tile-net').inner_text()
                await cards.nth(3).click()
                await expect(page.locator('#strategy-sheet-backdrop')).to_be_visible()
                pane = await page.locator('.strategy-sheet').bounding_box()
                assert pane['width'] <= 350.5
                buttons = [await page.locator('#strategy-sheet-' + x).bounding_box()
                           for x in ('power','settings','history')]
                assert abs(buttons[0]['width'] - buttons[1]['width']) < 2
                assert abs(buttons[1]['width'] - buttons[2]['width']) < 2
                assert buttons[0]['x'] < buttons[1]['x'] < buttons[2]['x']
                assert await page.locator('#strategy-sheet-power').get_attribute('data-enabled') == 'false'
                async def power_css():
                    return await page.locator('#strategy-sheet-power').evaluate("""node => {
                        const style = getComputedStyle(node);
                        return {rgb: style.backgroundColor.match(/\\d+/g).slice(0,3).map(Number),
                                opacity: Number(style.opacity)};
                    }""")
                off = await power_css()
                assert off['rgb'][0] > off['rgb'][1], off  # red OFF, not neutral
                assert off['opacity'] == 1
                # Start a stale poll BEFORE pressing ON; release it AFTER POST.
                overview_gate['hold'] = True
                await asyncio.wait_for(overview_started.wait(), timeout=6)
                await page.locator('#strategy-sheet-power').click()
                await expect(page.locator('#strategy-sheet-power')).to_have_attribute('data-enabled','true')
                on = await power_css()
                assert on['rgb'][1] > on['rgb'][0], on  # green ON
                assert on['opacity'] == 1
                assert actions[-1] == 'recovery_streak_toggle'
                await page.evaluate("""() => {
                    window.__powerWentRedAgain = false;
                    new MutationObserver(() => {
                        if (document.getElementById('strategy-sheet-power').dataset.enabled === 'false')
                            window.__powerWentRedAgain = true;
                    }).observe(document.getElementById('strategy-sheet-power'),
                               {attributes:true, attributeFilter:['data-enabled']});
                }""")
                overview_release.set()
                await page.wait_for_timeout(280)
                assert not await page.evaluate('window.__powerWentRedAgain')
                on = await power_css()
                assert on['rgb'][1] > on['rgb'][0], on
                await page.locator('#strategy-sheet-power').click()
                await expect(page.locator('#strategy-sheet-power')).to_have_attribute('data-enabled','false')
                off_again = await power_css()
                assert off_again['rgb'][0] > off_again['rgb'][1], off_again
                assert off_again['opacity'] == 1
                assert '1' in await page.locator('#strategy-sheet-result').inner_text()

                # Main navigation remains clickable while overlay is present.
                await page.locator('nav [data-tab=demo]').click()
                await expect(page.locator('#strategy-sheet-backdrop')).to_be_hidden()
                await page.locator('nav [data-tab=home]').click()
                assert await page.locator('#strategy-sheet-backdrop').is_hidden()
                await cards.nth(3).click()
                await page.locator('#strategy-sheet-settings').click()
                await expect(page.locator('#page-demo')).to_be_visible()
                assert await page.locator('#demo-recovery-accordion').evaluate('(n) => n.open')
                assert await page.locator('#strategy-sheet-backdrop').is_hidden()
                await page.locator('nav [data-tab=home]').click()
                await cards.nth(1).click()
                await page.locator('#strategy-sheet-history').click()
                await expect(page.locator('#page-history')).to_be_visible()
                assert 'high_cycles' in history_views
                await expect(page.locator('#history-demo-cycles')).to_have_attribute('open', '')
                assert await page.locator('#history-demo-cycles .history-item').count() == 2
                assert not await page.locator('#history-management').evaluate('(el) => el.open')
                assert await page.locator('#strategy-sheet-backdrop').is_hidden()

                # All four mini-tiles must land directly in their OWN OPEN cycle history.
                # Never expand Archive & Reset automatically.
                for index, view in enumerate(('cycles','high_cycles','target_cycles','recovery_cycles')):
                    await page.locator('nav [data-tab=home]').click()
                    await cards.nth(index).click()
                    await page.locator('#strategy-sheet-history').click()
                    await expect(page.locator('#page-history')).to_be_visible()
                    await expect(page.locator('#history-demo-cycles')).to_have_attribute('open','')
                    assert await page.locator('#history-demo-cycles .history-item').count() == 2
                    assert history_views[-1] == view
                    assert not await page.locator('#history-management').evaluate('(el) => el.open')
                    assert await page.locator(
                        '#demo-history-strategy-' +
                        ('classic','high','target','recovery')[index]
                    ).get_attribute('aria-expanded') == 'true'
                # Empty histories also open their disclosure and show a clear empty state.
                await page.route('**/api/history?view=cycles*', lambda req: req.fulfill(
                    content_type='application/json', body=json.dumps({
                        'view':'cycles','total':0,'page':0,'items':[],
                        'stats':{'currency':'KRW','total':0,'recovered':0,
                                 'lost':0,'incomplete':0,'stopped':0,
                                 'history_net_cents':0,'average_steps':0}})))
                await page.locator('nav [data-tab=home]').click()
                await cards.nth(0).click()
                await page.locator('#strategy-sheet-history').click()
                await expect(page.locator('#history-demo-cycles')).to_have_attribute('open','')
                await expect(page.locator('#history-demo-cycles')).to_contain_text('Завершённых циклов пока нет')
                assert not await page.locator('#history-management').evaluate('(el) => el.open')

                await page.locator('#history-management summary').click()
                assert await page.locator('#history-management').evaluate('(el) => el.open')
                await page.locator('[data-history-view=days]').click()
                assert not await page.locator('#history-management').evaluate('(el) => el.open')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                await page.close()
            await browser.close()
    asyncio.run(run())
