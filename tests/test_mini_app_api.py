import asyncio
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from mini_app_api import telegram_user,overview
import pytest


def signed(uid=42, token='fixture', when=None, extra=None):
    fields={'auth_date':str(int(time.time()) if when is None else when),'user':json.dumps({'id':uid})}
    fields.update(extra or {})
    check='\n'.join(f'{k}={v}' for k,v in sorted(fields.items()))
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    fields['hash']=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_auth_rejects_tampering_duplicates_expiry_and_other_bot():
    assert telegram_user(signed(),'fixture')==42
    for raw,token in [(signed(),'wrong'),(signed()+'&user=%7B%22id%22%3A1%7D','fixture'),(signed(when=1),'fixture'),(signed(when=int(time.time())+1000),'fixture'),(signed().replace('42','43'),'fixture')]:
        with pytest.raises(ValueError):telegram_user(raw,token)


def test_read_api_isolated_by_verified_user_and_respects_access():
    from luckyjet_reliable_gate import ReliableTelegramPasswordGate
    async def run():
        gate=ReliableTelegramPasswordGate(None,history_provider=lambda:['2.50x'])
        gate.authorized.update({42,43})
        gate._demo_ledger(42).balance_cents=123450
        gate._demo_ledger(43).balance_cents=998870
        status,_,body=await overview('tma '+signed(),gate,'fixture')
        data=json.loads(body)
        assert status=='200 OK' and data['demo']['balance_cents']==123450
        assert data['coefficients']==['2.50x'] and '998870' not in body.decode()
        gate.blocked.add(42)
        assert (await overview('tma '+signed(),gate,'fixture'))[0]=='403 Forbidden'
        assert (await overview('tma '+signed(99),gate,'fixture'))[0]=='403 Forbidden'
        assert (await overview('',gate,'fixture'))[0]=='401 Unauthorized'
        await gate.close()
    asyncio.run(run())
