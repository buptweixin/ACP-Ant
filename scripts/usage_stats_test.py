"""Request accounting, stream transparency, persistence and protected panel APIs."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from usage_stats import Ledger, RequestUsage, ResponseObserver, UsageMiddleware, CURRENT
runtime=tempfile.TemporaryDirectory()
os.environ.update(AGY_ROOT=runtime.name,AGY_GATEWAY_KEY='test-key',AGY_PANEL_PASSWORD='test-admin-password')
import acp_ant as core
import acp_panel as panel
import httpx

class UsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=Ledger(self.tmp.name+'/usage.sqlite3');self.addCleanup(self.ledger.close);self.now=time.time()
    def row(self,**kwargs):
        s=RequestUsage(model='model-a',authorized=True,done=True,status=200,attempts=1)
        s.acp(dict(inputTokens=100,outputTokens=20,cachedReadTokens=80,cachedWriteTokens=10,thoughtTokens=5,totalTokens=120))
        r=s.record();r.update(kwargs);return r
    def test_inclusive_agy_counters_and_cost(self):
        self.ledger.set_prices({'model-a':dict(input=2,output=4,cache_read=.5,cache_write=3)})
        self.ledger.write(self.row())
        t=self.ledger.query(self.now-1,self.now+1)['summary']
        self.assertEqual((t['tokens'],t['uncached_tokens'],t['output']),(120,30,20))
        self.assertEqual(t['cache_rate'],.8);self.assertAlmostEqual(t['cost'],.00017)
    def test_real_capture_does_not_double_count_thought(self):
        s=RequestUsage();s.acp(dict(inputTokens=1430,outputTokens=24,thoughtTokens=17,totalTokens=1454))
        self.assertEqual(s.values['input']+s.values['output'],1454)
    def test_exclusive_cache_and_reasoning_reconciled_against_total(self):
        s=RequestUsage();s.acp(dict(inputTokens=20,outputTokens=15,cachedReadTokens=80,thoughtTokens=5,totalTokens=120))
        self.assertEqual((s.values['input'],s.values['output']),(100,20))
        bad=RequestUsage();bad.acp(dict(inputTokens=20,outputTokens=15,cachedReadTokens=80,totalTokens=999))
        self.assertEqual(bad.measured,0);self.assertTrue(bad.raw)
        s.acp(dict(inputTokens=20,outputTokens=15,totalTokens=999));s.done=True;s.status=200
        self.assertEqual(s.record()['usage_state'],'partial')
    def test_unknown_and_partial_not_free(self):
        a=RequestUsage(done=True,status=200,model='unknown');self.ledger.write(a.record())
        r=self.row(usage_state='partial');self.ledger.write(r)
        t=self.ledger.query(self.now-1,self.now+1)['summary']
        self.assertEqual((t['unknown'],t['partial'],t['unpriced']),(1,1,2))
    def test_persistence_filters_pagination_no_duplicates(self):
        a=self.row();self.ledger.write(a);self.ledger.write(a)
        self.ledger.write(self.row(model='model-b',provider='aistudio',outcome='error'))
        reopened=Ledger(self.ledger.path);self.addCleanup(reopened.close)
        self.assertEqual(reopened.query(self.now-1,self.now+1)['summary']['requests'],2)
        self.assertEqual(len(reopened.query(self.now-1,self.now+1,offset=1,limit=1)['rows']),1)
        self.assertEqual(reopened.query(self.now-1,self.now+1,model='model-b',provider='aistudio',outcome='error')['summary']['requests'],1)
        self.assertEqual(os.stat(self.ledger.path).st_mode&0o777,0o600)
    def test_bad_prices_rejected_and_unknown_not_zero_cost(self):
        for prices in [[],{'m':{'input':2}}, {'m':dict(input=float('nan'),output=0,cache_read=0,cache_write=0)}, {'m':dict(input=True,output=0,cache_read=0,cache_write=0)}]:
            with self.assertRaises(ValueError):self.ledger.set_prices(prices)
        r=self.ledger.decorate(self.row(),{});self.assertIsNone(r['cost'])
    def test_range_validation_retention_and_bucket_boundary(self):
        for start,end in [(0,float('inf')),(1,0),(0,91*86400)]:
            with self.assertRaises(ValueError):self.ledger.query(start,end)
        self.ledger.write(self.row(started=self.now-91*86400))
        self.ledger.write(self.row(started=self.now))
        d=self.ledger.query(self.now-1800,self.now+1800)
        self.assertEqual(d['summary']['requests'],1);self.assertEqual(d['series'][0]['at'],self.now-1800)
    def test_stream_snapshot_not_double_counted_and_chunk_splits(self):
        s=RequestUsage(provider='aistudio',path='/v1/messages');o=ResponseObserver(s);o.sse=True
        events=[{'type':'message_start','message':{'usage':{'input_tokens':20,'cache_read_input_tokens':80}}}, {'type':'message_delta','usage':{'output_tokens':3}}, {'type':'message_delta','usage':{'output_tokens':7}}]
        raw=b''.join(('event: x\r\ndata: '+json.dumps(e)+'\r\n\r\n').encode() for e in events)
        for i in range(0,len(raw),3):o.feed(raw[i:i+3])
        o.feed(b'',True)
        self.assertEqual(s.values,dict(input=100,output=7,cache_read=80,cache_write=0,reasoning=0))
    def test_openai_responses_snapshot_and_estimate_ignored(self):
        for path,event in [('/v1/responses',{'response':{'usage':{'input_tokens':100,'output_tokens':20,'input_tokens_details':{'cached_tokens':80},'output_tokens_details':{'reasoning_tokens':5}}}}),('/v1/chat/completions',{'usage':{'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':80}}})]:
            s=RequestUsage(provider='aistudio',path=path);s.event(event);s.event(event)
            self.assertEqual((s.values['input'],s.values['output'],s.values['cache_read']),(100,20,80))
        s=RequestUsage(provider='aistudio',path='/v1/chat/completions');s.event({'usage':{'prompt_tokens':123,'estimated':True}});self.assertEqual(s.measured,0)
        s=RequestUsage(provider='antigravity');s.event({'usage':{'input_tokens':999}});self.assertEqual(s.measured,0)
    def test_oversize_sse_recovers_and_error_detected(self):
        s=RequestUsage(provider='aistudio',path='/v1/chat/completions');o=ResponseObserver(s);o.sse=True
        o.feed(b'data: '+b'x'*(o.LIMIT+1));o.feed(b'\n\ndata: {"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n')
        o.feed(b'event: error\ndata: {"error":{"message":"do not store this secret"}}\n\n',True)
        self.assertEqual(s.values['input'],3);self.assertEqual(s.outcome,'error');self.assertNotIn('secret',json.dumps(s.record()))

class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.ledger=Ledger(self.tmp.name+'/usage.sqlite3');self.addCleanup(self.ledger.close)
    async def request(self, app, disconnect=False):
        sent=[]
        async def recv():return {'type':'http.disconnect' if disconnect else 'http.request','body':b''}
        async def send(msg):sent.append(msg)
        await UsageMiddleware(app,self.ledger)({'type':'http','method':'POST','path':'/v1/messages'},recv,send)
        return sent,self.ledger.query(time.time()-60,time.time()+1)
    async def test_stream_passthrough_one_request_multiple_attempts(self):
        chunks=[b'data: {"type":"message_start"}\n\n',b'data: {"type":"message_stop"}\n\n']
        async def app(scope,recv,send):
            s=CURRENT.get();s.authorized=True;s.attempts=2;s.missing_attempts=1;s.acp({'inputTokens':100,'outputTokens':20,'thoughtTokens':5,'totalTokens':120})
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            for i,c in enumerate(chunks):await send({'type':'http.response.body','body':c,'more_body':i==0})
        sent,d=await self.request(app)
        self.assertEqual([m['body'] for m in sent[1:]],chunks);self.assertEqual(d['summary']['requests'],1)
        self.assertEqual((d['rows'][0]['attempts'],d['rows'][0]['usage_state']),(2,'partial'))
    async def test_disconnect_and_unauthorized(self):
        async def cancel(scope,recv,send):
            CURRENT.get().authorized=True;await recv()
        _,d=await self.request(cancel,True);self.assertEqual(d['rows'][0]['outcome'],'cancelled')
        async def unauthorized(scope,recv,send):
            await send({'type':'http.response.start','status':401,'headers':[]});await send({'type':'http.response.body','body':b'{}'})
        _,d=await self.request(unauthorized);self.assertEqual(d['summary']['requests'],1)
    async def test_stream_error_is_not_http_success(self):
        async def app(scope,recv,send):
            CURRENT.get().authorized=True
            await send({'type':'http.response.start','status':200,'headers':[(b'content-type',b'text/event-stream')]})
            await send({'type':'http.response.body','body':b'data: {"type":"error","error":{"type":"rate_limit_error"}}\n\n'})
        _,d=await self.request(app);self.assertEqual((d['rows'][0]['outcome'],d['rows'][0]['http_status']),('error',200))
    async def test_concurrent_requests_are_isolated(self):
        async def app(scope,recv,send):
            s=CURRENT.get();s.authorized=True;s.model=scope['model'];s.acp({'inputTokens':scope['tokens'],'outputTokens':1,'totalTokens':scope['tokens']+1})
            await asyncio.sleep(.005)
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'{}'})
        async def one(model,tokens):
            async def recv():return {'type':'http.request','body':b''}
            async def send(msg):pass
            await UsageMiddleware(app,self.ledger)({'type':'http','method':'POST','path':'/v1/messages','model':model,'tokens':tokens},recv,send)
        await asyncio.gather(one('one',100),one('two',200))
        d=self.ledger.query(time.time()-60,time.time()+1)
        self.assertEqual({r['model']:r['input'] for r in d['rows']},{'one':100,'two':200})
    async def test_storage_failure_does_not_break_response(self):
        async def app(scope,recv,send):
            CURRENT.get().authorized=True
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'{"ok":true}'})
        with patch.object(self.ledger,'write',side_effect=OSError('disk full')),self.assertLogs('acp-ant.usage',level='ERROR'):
            sent,_=await self.request(app)
        self.assertEqual(sent[-1]['body'],b'{"ok":true}')
    async def test_panel_auth_csrf_prices_and_query(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app),base_url='http://test') as client:
            q=f'/panel/api/usage?start={time.time()-60}&end={time.time()+1}'
            self.assertEqual((await client.get(q)).status_code,401)
            client.cookies.set(panel.COOKIE,panel._token(int(time.time())+60))
            with patch.object(core,'USAGE_LEDGER',self.ledger):
                self.assertEqual((await client.get(q)).status_code,200)
                self.assertEqual((await client.post('/panel/api/usage/prices',json={'prices':{}})).status_code,403)
                self.assertEqual((await client.post('/panel/api/usage/prices',headers={'X-ACP-Panel':'1'},json={'prices':{'a':{'input':-1}}})).status_code,400)
                self.assertEqual((await client.post('/panel/api/usage/prices',headers={'X-ACP-Panel':'1'},json={'prices':{}})).status_code,200)
    async def test_native_generation_records_real_usage_even_without_wire_usage(self):
        async def fake_generate(account,model,effort,system,blocks,deadline):
            yield 'text','hello'
            yield 'usage',json.dumps({'inputTokens':100,'outputTokens':20,'cachedReadTokens':80,'thoughtTokens':5,'totalTokens':120})
        acc=core.Account('fake',self.tmp.name)
        async def acquire(*args):return acc
        state=RequestUsage(authorized=True,model='m');token=CURRENT.set(state)
        try:
            with patch.object(core.POOL,'acquire',acquire),patch.object(core.POOL,'release'),patch.object(core,'run_on',fake_generate):
                async for _ in core.generate('m','',[{'type':'text','text':'PRIVATE PROMPT'}]):pass
        finally:CURRENT.reset(token)
        self.assertEqual((state.measured,state.values['input'],state.values['output']),(1,100,20))
        self.assertNotIn('PRIVATE',json.dumps(state.record()))

if __name__=='__main__':unittest.main()
