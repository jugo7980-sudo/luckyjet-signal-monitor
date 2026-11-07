"""Shared history navigation and high-coefficient history screens."""
import json
from datetime import datetime
from coefficient_stats import TZ


def button(text, action):
    return {'text': text, 'callback_data': action}


def footer(back):
    return [[button('⬅ Назад', back)],
            [button('🏠 Главное меню', 'menu_main'), button('✖ Закрыть', 'menu_close')]]


def pager(page, pages, action):
    row=[]
    if page: row.append(button('◀', action(page-1)))
    if page+1<pages: row.append(button('▶', action(page+1)))
    return [row] if row else []


def stamp(value):
    return datetime.fromtimestamp(float(value),TZ).strftime('%d.%m.%Y %H:%M')


async def high_screen(stats, action):
    r=stats.redis
    state=await r.hgetall('luckyjet:coef:high:state')
    last=json.loads(state['last_high']) if state.get('last_high') else None
    counter=(f"После >100x: {state.get('since',0)} раундов" if last else 'Ожидается первое >100x')
    if state.get('gap')=='1': counter='После >100x: неполные данные'
    header='Интервалы >100x\n'+counter
    if last: header+=f"\nПоследнее: {stamp(last['stamp'])} · {last['value']}x"
    parts=action.split(':')
    if parts[0]=='hi_case':
        raw=await r.hget('luckyjet:coef:high:entries',parts[1])
        if not raw: return 'Запись недоступна.',{'inline_keyboard':footer('hi_days:0')}
        entry=json.loads(raw); previous=json.loads(entry['previous']) if entry.get('previous') else None
        count=entry['rounds'] if entry['rounds'] is not None else 'нет полных данных'
        text=f"{stamp(entry['stamp'])} · {entry['value']}x\nИнтервал: " + (f"{count} раундов" if entry['rounds'] is not None else 'нет полных данных')
        if previous: text+=f"\nПредыдущее: {stamp(previous['stamp'])} · {previous['value']}x"
        if entry.get('uncertain'): text+='\nВремя приблизительное'
        return text,{'inline_keyboard':footer(f"hi_day:{entry['day']}:{parts[2]}")}
    if parts[0]=='hi_days':
        page=int(parts[1]); total=await r.zcard('luckyjet:coef:high:days'); pages=max(1,(total+9)//10); page=min(page,pages-1)
        days=await r.zrevrange('luckyjet:coef:high:days',page*10,page*10+9)
        rows=[[button(datetime.strptime(day,'%Y-%m-%d').strftime('%d.%m.%Y'),f'hi_day:{day}:0')] for day in days]
        text=f'{header}\n{page+1}/{pages} · Владивосток, UTC+10'
        if not rows:text+='\nЗаписей пока нет.'
        rows+=pager(page,pages,lambda p:f'hi_days:{p}')
        rows+=[[button('📅 Дата','hi_date')]]+footer('coef_days:0')
        return text,{'inline_keyboard':rows}
    day=parts[1]; page=int(parts[2]); total=await r.llen(f'luckyjet:coef:high:{day}'); pages=max(1,(total+9)//10);page=min(page,pages-1)
    ids=await r.lrange(f'luckyjet:coef:high:{day}',page*10,page*10+9)
    values=await r.hmget('luckyjet:coef:high:entries',ids) if ids else []
    rows=[]
    for key,raw in zip(ids,values):
        if not raw:continue
        item=json.loads(raw); count=item['rounds'] if item['rounds'] is not None else '?'
        when=datetime.fromtimestamp(item['stamp'],TZ).strftime('%H:%M')
        rows.append([button(f"{count} раундов · {when} · {item['value']}x",f'hi_case:{key}:{page}')])
    label=datetime.strptime(day,'%Y-%m-%d').strftime('%d.%m.%Y')
    text=f'{header}\n{label} · {page+1}/{pages} · Владивосток, UTC+10'
    if not rows:text+='\nНет сохранённых записей.'
    rows+=pager(page,pages,lambda p:f'hi_day:{day}:{p}')
    rows+=[[button('📅 Дата','hi_date')]]+footer('hi_days:0')
    return text,{'inline_keyboard':rows}
