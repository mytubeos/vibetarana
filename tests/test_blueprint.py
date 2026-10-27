"""R2–R4 boundaries, scheduling, web identity and deployment ownership regressions."""
import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pyrogram.enums import ChatType, ChatMemberStatus

from bot.core import preferences, service, scheduler, discovery
from bot.core.queue import QueueManager, Track, queues
from bot.core.session import sessions, operation, get_session
from bot.core.lease import WorkerLease
from bot.core.metrics import Metrics
from bot.platforms.playlist import canonical_playlist
from bot.web.auth import validate_init_data
from bot.web import server
from config import settings


def track(name='song', user=1, link='https://www.youtube.com/watch?v=abcdefghijk'):
    return Track(name, '3:00', link, None, user, f'User {user}')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    queues._chats.clear()
    sessions.clear()
    preferences._cache.clear()
    preferences._locks.clear()
    server._rates.clear()
    server._seen.clear()
    monkeypatch.setattr(preferences.db, 'get_chat_settings', AsyncMock(return_value={}))
    yield
    queues._chats.clear()
    sessions.clear()
    preferences._cache.clear()
    preferences._locks.clear()


def signed(user=2, timestamp=None, **extras):
    data = {'auth_date': str(int(time.time()) if timestamp is None else timestamp), 'user': json.dumps({'id': user, 'first_name': 'Listener'}), **extras}
    secret = hmac.new(b'WebAppData', settings.bot_token.encode(), hashlib.sha256).digest()
    data['hash'] = hmac.new(secret, '\n'.join(f'{k}={v}' for k,v in sorted(data.items())).encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_auth_valid_and_signed_extra_fields():
    assert validate_init_data(signed(signature='telegram-extra-signature'), settings.bot_token)['id'] == 2


@pytest.mark.parametrize('raw', ['garbage', '', 'hash=bad', signed(timestamp=1), signed(timestamp=int(time.time())+500), signed()+'&user=x'])
def test_auth_rejects_forged_expired_future_duplicate(raw):
    with pytest.raises(ValueError):
        validate_init_data(raw, settings.bot_token)


def test_auth_wrong_bot_key():
    with pytest.raises(ValueError):
        validate_init_data(signed(), 'other-bot-token')


def test_fair_queue_preserves_current_and_fifo():
    q = QueueManager()
    items = [track('current',1), track('a1',1), track('a2',1), track('b1',2), track('b2',2), track('c1',3)]
    q.get(1).queue = items[:]
    q.fair(1)
    assert [t.title for t in q.get(1).queue] == ['current','b1','c1','a1','b2','a2']
    assert q.get(1).current is items[0]
    q.fair(1)
    assert [t.title for t in q.get(1).queue] == ['current','b1','c1','a1','b2','a2']


def test_fair_enqueue_and_priority_override(monkeypatch):
    monkeypatch.setattr(preferences, 'get', AsyncMock(return_value={'fair_queue': True}))
    monkeypatch.setattr(service.calls, '_get_healthy_assistant', lambda _: object())
    monkeypatch.setattr(service.calls, 'persist_queue', AsyncMock())
    monkeypatch.setattr(service.calls, 'schedule_prefetch', lambda _: None)
    queues.get(1).queue = [track('current',1), track('a',1)]
    async def run():
        await service.enqueue(1,[track('b',2)])
        assert [t.title for t in queues.get(1).queue] == ['current','b','a']
        await service.enqueue(1,[track('priority',1)], next_up=True)
        assert queues.get(1).queue[1].title == 'priority'
    asyncio.run(run())


def test_schedule_timezone_and_window():
    now = datetime(2026,10,27,10,tzinfo=timezone.utc)
    assert scheduler.parse_time('2026-10-27T18:00', 'Asia/Kolkata', now=now) == datetime(2026,10,27,12,30,tzinfo=timezone.utc)
    for value, zone in [('2026-10-27T10:00','UTC'),('2027-01-01T10:00','UTC'),('2026-11-01T01:30','America/New_York'),('2026-10-27T18:00','Bad/Zone'),('2026-10-27T18:00+05:30','Asia/Kolkata')]:
        with pytest.raises(ValueError):
            scheduler.parse_time(value,zone,now=now)


def test_schedule_rejects_spring_gap():
    with pytest.raises(ValueError):
        scheduler.parse_time('2026-03-08T02:30', 'America/New_York', now=datetime(2026,3,1,tzinfo=timezone.utc))


def test_schedule_revoked_admin_never_plays(monkeypatch):
    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(scheduler.db,'db',SimpleNamespace(scheduled_sessions=collection))
    monkeypatch.setattr(scheduler.bot,'get_chat',AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(scheduler,'user_is_admin_or_owner',AsyncMock(return_value=False))
    enqueue = AsyncMock()
    monkeypatch.setattr(service,'enqueue',enqueue)
    asyncio.run(scheduler.execute({'_id':'job','chat_id':1,'user_id':2,'due':datetime.now(timezone.utc),'tracks':[]}))
    enqueue.assert_not_awaited()
    assert collection.update_one.call_args.args[1]['$set']['status'] == 'permission_revoked'


def test_schedule_missed_job_does_not_replay(monkeypatch):
    collection = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr(scheduler.db,'db',SimpleNamespace(scheduled_sessions=collection))
    enqueue=AsyncMock(); monkeypatch.setattr(service,'enqueue',enqueue)
    asyncio.run(scheduler.execute({'_id':'job','due':datetime.now(timezone.utc)-timedelta(minutes=10)}))
    enqueue.assert_not_awaited()
    assert collection.update_one.call_args.args[1]['$set']['status'] == 'missed'


def test_schedule_cancel_scopes_chat_and_cancels_task(monkeypatch):
    collection=SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
    monkeypatch.setattr(scheduler.db,'db',SimpleNamespace(scheduled_sessions=collection))
    async def run():
        task=asyncio.create_task(asyncio.sleep(100))
        scheduler._running['job']=task
        assert await scheduler.cancel(7,'job')
        assert task.cancelled()
        scheduler._running.clear()
    asyncio.run(run())
    assert collection.update_one.call_args.args[0]['chat_id'] == 7


@pytest.mark.parametrize('url', ['https://youtube.com.evil.test/playlist?list=PL1234567890','https://localhost/playlist?list=PL1234567890','http://youtube.com/playlist?list=PL1234567890','https://youtube.com:8443/playlist?list=PL1234567890','https://user@youtube.com/playlist?list=PL1234567890'])
def test_playlist_url_trust_boundary(url):
    with pytest.raises(ValueError): canonical_playlist(url)


def test_playlist_canonicalizes_not_arbitrary_query():
    assert canonical_playlist('https://www.youtube.com/playlist?list=PL1234567890&url=http://localhost') == 'https://www.youtube.com/playlist?list=PL1234567890'


def test_metrics_no_fabricated_samples_and_bounds():
    m=Metrics(); assert m.snapshot()['success_rate'] is None
    m.record('failed',4)
    for i in range(1100): m.record('success',i)
    s=m.snapshot()
    assert s['latency_samples']==1000
    assert s['successful_start_seconds_p50']==599
    assert s['successful_start_seconds_p95']==1049
    assert s['success_rate']==1100/1101


def test_lease_conflict_cleans_owned_keys(monkeypatch):
    from pymongo.errors import DuplicateKeyError
    from bot.core import lease as module
    coll=SimpleNamespace(update_one=AsyncMock(side_effect=[SimpleNamespace(matched_count=1,upserted_id=None),DuplicateKeyError('busy')]), delete_one=AsyncMock())
    monkeypatch.setattr(module.db,'db',SimpleNamespace(worker_leases=coll))
    worker=WorkerLease()
    with pytest.raises(RuntimeError,match='Another worker'): asyncio.run(worker.acquire())
    assert not worker.keys
    assert coll.delete_one.await_count==1
    assert coll.delete_one.call_args.args[0]['owner']==worker.owner


def test_lease_expiration_fails_closed():
    worker=WorkerLease();worker.enforced=True;worker.keys=['x'];worker.deadline=time.monotonic()-1
    with pytest.raises(RuntimeError): worker.check()
    assert worker.lost.is_set()


def test_stale_web_skip_is_rechecked_under_lock(monkeypatch):
    from bot.plugins.controls import action
    advance=AsyncMock();monkeypatch.setattr(service.calls,'_advance_and_play_unlocked',advance)
    queues.get(1).playback_id=5
    async def run():
        session=get_session(1)
        async with session.lock:
            task=asyncio.create_task(action(1,'skip',expected_playback_id=5))
            await asyncio.sleep(0)
            queues.get(1).playback_id=6
        with pytest.raises(ValueError,match='Track changed'): await task
    asyncio.run(run())
    advance.assert_not_awaited()


def test_mood_without_key_is_honest_search(monkeypatch):
    monkeypatch.setattr(settings,'openai_api_key','')
    result, ai=asyncio.run(discovery.mood_query('rainy evening','Hindi'))
    assert 'Hindi' in result and not ai


def test_disabled_recognition_and_lyrics(monkeypatch):
    monkeypatch.setattr(settings,'audd_api_token','')
    monkeypatch.setattr(settings,'lyrics_enabled',False)
    with pytest.raises(ValueError):asyncio.run(discovery.recognize(None))
    with pytest.raises(ValueError):asyncio.run(discovery.lyrics_link('song'))


def setup_web(monkeypatch, membership=True, controller=False):
    monkeypatch.setattr(server.bot,'get_chat',AsyncMock(return_value=SimpleNamespace(id=-123,type=ChatType.SUPERGROUP,title='Test group')))
    monkeypatch.setattr(server,'member',AsyncMock(return_value=membership))
    monkeypatch.setattr(server,'can_control',AsyncMock(return_value=controller))
    monkeypatch.setattr(server,'can_request',AsyncMock(return_value=True))
    monkeypatch.setattr(server.db,'list_playlists',AsyncMock(return_value=['favourites']))
    app=web.Application(client_max_size=16384);server.attach(app)
    return app

async def request(app, payload, raw=None):
    async with TestClient(TestServer(app)) as client:
        response=await client.post('/api',json=payload,headers={'X-Telegram-Init-Data':raw or signed()})
        return response.status,await response.json()


def test_web_member_read_no_track_credentials(monkeypatch):
    app=setup_web(monkeypatch);queues.get(-123).queue=[track()]
    status,data=asyncio.run(request(app,{'chat_id':-123,'action':'state'}))
    assert status==200 and data['queue'][0]['title']=='song'
    assert 'link' not in data['queue'][0]
    assert not data['can_control']


def test_web_outsider_cannot_read_queue(monkeypatch):
    app=setup_web(monkeypatch,membership=False)
    status,_=asyncio.run(request(app,{'chat_id':-123,'action':'state'}))
    assert status==403


def test_web_member_cannot_skip(monkeypatch):
    app=setup_web(monkeypatch)
    status,_=asyncio.run(request(app,{'chat_id':-123,'action':'skip'}))
    assert status==403


def test_web_forged_auth_rejected_before_telegram(monkeypatch):
    app=setup_web(monkeypatch)
    status,_=asyncio.run(request(app,{'chat_id':-123},'hash=fake'))
    assert status==401
    server.bot.get_chat.assert_not_awaited()


def test_web_operator_metrics_owner_only(monkeypatch):
    app=setup_web(monkeypatch)
    status,_=asyncio.run(request(app,{'action':'ops'}))
    assert status==403
    app=setup_web(monkeypatch)
    status,data=asyncio.run(request(app,{'action':'ops'},signed(settings.owner_id)))
    assert status==200 and 'play_attempts' in data


def test_web_stale_revision_never_enqueues(monkeypatch):
    app=setup_web(monkeypatch)
    enqueue=AsyncMock();monkeypatch.setattr(service,'enqueue',enqueue)
    status,_=asyncio.run(request(app,{'action':'play','chat_id':-123,'revision':-1,'request_id':'a'*16,'query':'test'}))
    assert status==409
    enqueue.assert_not_awaited()


def test_web_rate_limit_is_bounded():
    for _ in range(60): server.rate_limit(2)
    with pytest.raises(web.HTTPTooManyRequests):server.rate_limit(2)


def test_recommendation_avoids_history(monkeypatch):
    from bot.core import recommendations
    state=queues.get(1);state.last_track=track();state.history=[track()]
    lookup=AsyncMock(return_value=track())
    monkeypatch.setattr(recommendations,'VideosSearch',lambda *a,**k:SimpleNamespace(next=AsyncMock(return_value={'result':[{'id':'abcdefghijk','title':'old','duration':'3:00'},{'id':'new12345678','title':'new','duration':'2:00'}]})))
    result=asyncio.run(recommendations.choose(1,state,lookup))
    assert result.title=='new'


def test_vote_unique_members_and_next_track_reset(monkeypatch):
    from bot.plugins import community
    state=queues.get(1);state.queue=[track()]
    monkeypatch.setattr(community,'member',AsyncMock(return_value=True))
    monkeypatch.setattr(community.preferences,'get',AsyncMock(return_value={'vote_skip':2}))
    advance=AsyncMock();monkeypatch.setattr(community.calls,'_advance_and_play_unlocked',advance)
    monkeypatch.setattr(community.calls,'persist_queue',AsyncMock());monkeypatch.setattr(community.player,'refresh',AsyncMock())
    message=SimpleNamespace(chat=SimpleNamespace(id=1),from_user=SimpleNamespace(id=2),reply_text=AsyncMock())
    async def run():
        await community.voteskip(None,message);await community.voteskip(None,message)
        assert state.skip_voters=={2};advance.assert_not_awaited()
        message.from_user.id=3;await community.voteskip(None,message)
        advance.assert_awaited_once();assert not state.skip_voters
    asyncio.run(run())


def test_discovery_buttons_use_existing_callback_protocol(monkeypatch):
    from bot.plugins import discovery as plugin
    from bot.plugins.play import _choices
    monkeypatch.setattr(plugin,'VideosSearch',lambda *a,**kw:SimpleNamespace(next=AsyncMock(return_value={'result':[{'id':'abcdefghijk','title':'Found song','duration':'3:00'}]})))
    message=SimpleNamespace(chat=SimpleNamespace(id=12),from_user=SimpleNamespace(id=2,first_name='Listener'),reply_text=AsyncMock())
    asyncio.run(plugin.choices(message,'query','Suggestions',7))
    button=message.reply_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
    prefix,token,index=button.callback_data.split(':')
    assert prefix=='pick' and index=='0'
    assert _choices[token][1:4]==(12,2,7)
    _choices.clear()


def test_deployment_failure_restores_previous_container(monkeypatch,tmp_path):
    from scripts import deploy
    env=tmp_path/'settings.env';env.write_text('PORT=8080\n')
    commands=[]
    def docker(*args,**kwargs):
        commands.append(args)
        data=[{'Config':{'Labels':{'vibetarana.managed':'true'}}}]
        return SimpleNamespace(returncode=0,stdout=json.dumps(data))
    monkeypatch.setattr(deploy,'docker',docker)
    with pytest.raises(RuntimeError): deploy.deploy('image:sha',str(env),'vibetarana',8080,timeout=0)
    assert ('rm','-f','vibetarana') in commands
    assert commands[-1]==('start','vibetarana')
    assert commands[-2][0]=='rename' and commands[-2][2]=='vibetarana'


def test_deployment_success_keeps_previous_stopped(monkeypatch,tmp_path):
    from scripts import deploy
    env=tmp_path/'settings.env';env.write_text('PORT=8080\n')
    commands=[]
    def docker(*args,**kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'Config':{'Labels':{'vibetarana.managed':'true'}}}]))
    monkeypatch.setattr(deploy,'docker',docker)
    monkeypatch.setattr(deploy,'ready',lambda _:True)
    monkeypatch.setattr(deploy.time,'sleep',lambda _:None)
    backup=deploy.deploy('image:sha',str(env),'vibetarana',8080)
    assert backup.startswith('vibetarana-previous-')
    assert not any(args[0]=='rm' for args in commands)


def test_deployment_refuses_unmanaged_container(monkeypatch,tmp_path):
    from scripts import deploy
    env=tmp_path/'settings.env';env.write_text('PORT=8080\n')
    commands=[]
    def docker(*args,**kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'Config':{'Labels':{}}}]))
    monkeypatch.setattr(deploy,'docker',docker)
    with pytest.raises(ValueError,match='not managed'):deploy.deploy('image:sha',str(env),'vibetarana',8080)
    assert all(args[0] in ('image','container') for args in commands)


def test_provider_budget_counts_failures_and_caps_calls(monkeypatch):
    monkeypatch.setattr(settings,'provider_requests_per_hour',2)
    discovery._usage.clear()
    discovery.budget('ai');discovery.budget('ai')
    with pytest.raises(ValueError,match='hourly limit'):discovery.budget('ai')
    discovery.budget('lyrics')
    discovery._usage.clear()


def test_pool_shutdown_leaves_native_calls_before_client(monkeypatch):
    from bot.core.assistants import AssistantPool
    order=[]
    async def leave(chat):order.append(('leave',chat))
    async def stop():order.append(('stop',None))
    pool=AssistantPool();pool.assistants=[SimpleNamespace(chats={9},index=0,call_py=SimpleNamespace(leave_call=leave),client=SimpleNamespace(stop=stop))]
    pool._chat_assignment={9:0}
    asyncio.run(pool.stop())
    assert order==[('leave',9),('stop',None)]
    assert not pool.assistants and not pool._chat_assignment
