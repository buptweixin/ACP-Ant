"""Private, bounded request ledger. Never persists prompts, headers or response text."""
from __future__ import annotations
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid

log = logging.getLogger('acp-ant.usage')
CURRENT = ContextVar('usage_request', default=None)
PATHS = {'/v1/chat/completions', '/v1/messages', '/v1/responses'}
FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'reasoning')


def number(value):
    try:
        return max(0, min(10**12, int(value or 0)))
    except (ValueError, TypeError, OverflowError):
        return 0


def acp_usage(u):
    """Resolve cache/reasoning inclusion against agy's reported total.

    ACP implementations disagree on inclusive counters. Only accept an exact
    reconciliation; preserve raw counters separately when accounting is unknown.
    """
    if not isinstance(u, dict) or not any(k in u for k in ('inputTokens', 'outputTokens')):
        return None
    inp,out,cache,write,thought=map(number,(u.get('inputTokens'),u.get('outputTokens'),
        u.get('cachedReadTokens'),u.get('cachedWriteTokens'),u.get('thoughtTokens')))
    total=number(u.get('totalTokens')) if 'totalTokens' in u else None
    for add_cache,add_thought in ((False,False),(True,False),(False,True),(True,True)):
        i=inp+(cache+write if add_cache else 0);o=out+(thought if add_thought else 0)
        if cache+write>i or thought>o: continue
        if total==i+o or (total is None and not (cache or write or thought)):
            return dict(zip(FIELDS,(i,o,cache,write,thought)))
    return None



def wire_usage(u, protocol):
    if not isinstance(u, dict) or u.get('estimated'):
        return None
    if protocol == '/v1/messages':
        if not any(k in u for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens')):
            return None
        result = {}
        # Anthropic streaming has input in message_start and cumulative output in
        # message_delta. Absence must not overwrite earlier counters with zero.
        if 'input_tokens' in u:
            result['input'] = number(u.get('input_tokens')) + number(u.get('cache_read_input_tokens')) + number(u.get('cache_creation_input_tokens'))
        for src, dest in [('output_tokens','output'), ('cache_read_input_tokens','cache_read'), ('cache_creation_input_tokens','cache_write')]:
            if src in u: result[dest] = number(u[src])
        return result
    inp = u.get('prompt_tokens', u.get('input_tokens'))
    out = u.get('completion_tokens', u.get('output_tokens'))
    if inp is None and out is None:
        return None
    details = u.get('prompt_tokens_details', u.get('input_tokens_details')) or {}
    output_details = u.get('completion_tokens_details', u.get('output_tokens_details')) or {}
    return {'input':number(inp), 'output':number(out), 'cache_read':number(details.get('cached_tokens')),
            'cache_write':0, 'reasoning':number(output_details.get('reasoning_tokens'))}


@dataclass
class RequestUsage:
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.time)
    path: str = ''
    model: str = ''
    provider: str = 'antigravity'
    accounts: list = field(default_factory=list)
    attempts: int = 0
    measured: int = 0
    missing_attempts: int = 0
    unverified_segments: int = 0
    values: dict = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    raw: list = field(default_factory=list)
    status: int = 0
    outcome: str = ''
    error_kind: str = ''
    done: bool = False
    authorized: bool = False
    closed: bool = False

    def acp(self, usage):
        if self.closed: return
        if not isinstance(usage, dict):
            self.unverified_segments += 1; return
        values = acp_usage(usage)
        if values is None: self.unverified_segments += 1
        if values is not None:
            self.measured += 1
            for k, v in values.items(): self.values[k] += v
        # Raw numerical counters only, capped independently of request length.
        if len(self.raw) < 64:
            self.raw.append({k:number(usage[k]) for k in ('inputTokens','outputTokens','cachedReadTokens','cachedWriteTokens','thoughtTokens','totalTokens') if k in usage})

    def event(self, obj):
        if not isinstance(obj, dict): return
        typ = obj.get('type','')
        if obj.get('error') or typ in ('error', 'response.failed', 'response.incomplete'):
            self.outcome = 'error'
            self.error_kind = self.error_kind or 'STREAM_ERROR'
        if self.provider != 'aistudio': return  # ACP wire output may contain gateway estimates.
        container = obj.get('response') or obj.get('message') or obj
        if not isinstance(container, dict): return
        values = wire_usage(container.get('usage'), self.path)
        if values is not None:
            self.measured = 1
            self.values.update(values)  # snapshots, not additive streaming deltas

    def record(self):
        return {'id':self.id, 'started':self.started, 'duration_ms':round((time.time()-self.started)*1000),
                'path':self.path, 'model':self.model[:200], 'provider':self.provider,
                'accounts':self.accounts, 'attempts':self.attempts,
                'http_status':self.status, 'outcome':self.outcome or ('success' if self.done and self.status < 400 else 'error'),
                'error_kind':self.error_kind, 'usage_state':('partial' if self.missing_attempts or self.unverified_segments or not self.done or self.outcome in ('error','cancelled') or self.status >= 400 else 'reported') if self.measured else 'unknown',
                **self.values, 'raw':self.raw}


class ResponseObserver:
    """Parse bounded JSON/SSE copies without buffering or changing the response stream."""
    LIMIT = 1024 * 1024
    def __init__(self, state):
        self.state = state
        self.sse = False
        self.buf = b''
        self.drop_line = False
        self.data = []
        self.event_bytes = 0
        self.drop_event = False

    def parse(self, data):
        try: self.state.event(json.loads(data))
        except (ValueError, TypeError, UnicodeDecodeError): pass

    def feed(self, chunk, final=False):
        if not self.sse:
            if self.drop_line: return
            if len(self.buf)+len(chunk) > self.LIMIT:
                self.buf=b''; self.drop_line=True; return
            self.buf += chunk
            if final: self.parse(self.buf); self.buf=b''
            return
        self.buf += chunk
        while b'\n' in self.buf:
            line,self.buf=self.buf.split(b'\n',1)
            if self.drop_line:
                self.drop_line=False; continue
            line=line.rstrip(b'\r')
            if not line:
                if self.data and not self.drop_event: self.parse(b'\n'.join(self.data))
                self.data=[]; self.event_bytes=0; self.drop_event=False
            elif line.startswith(b'data:'):
                self.event_bytes += len(line)
                if self.event_bytes > self.LIMIT:
                    self.data=[];self.drop_event=True
                elif not self.drop_event: self.data.append(line[5:].lstrip(b' '))
        if len(self.buf)>self.LIMIT:
            self.buf=b'';self.drop_line=True;self.drop_event=True;self.data=[]
        if final and self.data and not self.drop_event: self.parse(b'\n'.join(self.data))


class UsageMiddleware:
    def __init__(self, app, ledger): self.app,self.ledger=app,ledger
    async def __call__(self, scope, receive, send):
        if scope['type']!='http' or scope.get('path') not in PATHS or scope.get('method')!='POST':
            return await self.app(scope, receive, send)
        state=RequestUsage(path=scope['path']); token=CURRENT.set(state); observer=ResponseObserver(state)
        async def recv():
            msg=await receive()
            if msg['type']=='http.disconnect' and not state.done: state.outcome='cancelled'
            return msg
        async def emit(msg):
            if msg['type']=='http.response.start':
                state.status=msg['status']
                observer.sse=any(k.lower()==b'content-type' and b'text/event-stream' in v.lower() for k,v in msg.get('headers',[]))
            elif msg['type']=='http.response.body':
                observer.feed(msg.get('body',b''), not msg.get('more_body',False))
            await send(msg)
            if msg['type']=='http.response.body' and not msg.get('more_body',False): state.done=True
        try:
            await self.app(scope,recv,emit)
        except asyncio.CancelledError:
            state.outcome='cancelled';raise
        except Exception:
            state.outcome='error';state.error_kind=state.error_kind or 'SERVER';raise
        finally:
            CURRENT.reset(token);state.closed=True
            if state.authorized:
                # Stats failures must never turn a successful model call into a 500.
                try:
                    import anyio
                    with anyio.CancelScope(shield=True):
                        await asyncio.to_thread(self.ledger.write,state.record())
                except Exception: log.exception('could not persist usage counters')


class Ledger:
    def __init__(self, path):
        self.path=path;self.lock=threading.RLock();self.db=None;self.last_prune=0
    def connection(self):
        if self.db is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)),exist_ok=True)
            fd=os.open(self.path,os.O_CREAT|os.O_RDWR,0o600);os.close(fd);os.chmod(self.path,0o600)
            self.db=sqlite3.connect(self.path,timeout=5,check_same_thread=False)
            self.db.row_factory=sqlite3.Row
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript('''CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, started REAL NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
                outcome TEXT NOT NULL, document TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_time ON requests(started);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
        return self.db
    def write(self, row):
        with self.lock:
            db=self.connection()
            db.execute('INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?)',
                (row['id'],row['started'],row['model'],row['provider'],row['outcome'],json.dumps(row,ensure_ascii=False)))
            now=time.time()
            if now-self.last_prune>3600:
                db.execute('DELETE FROM requests WHERE started < ?', (now-90*86400,))
                db.execute('DELETE FROM requests WHERE id IN (SELECT id FROM requests ORDER BY started DESC LIMIT -1 OFFSET 200000)')
                self.last_prune=now
            db.commit()
    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close(); self.db=None
    def prices(self):
        with self.lock:
            r=self.connection().execute("SELECT value FROM settings WHERE key='prices'").fetchone()
            return json.loads(r[0]) if r else {}
    def set_prices(self, prices):
        if not isinstance(prices,dict) or len(prices)>200: raise ValueError('最多配置 200 个模型')
        for model,rates in prices.items():
            if not isinstance(model,str) or not model.strip() or len(model)>200: raise ValueError('模型名无效')
            if not isinstance(rates,dict) or set(rates)!=set(FIELDS[:4]): raise ValueError('每个模型须设置 input/output/cache_read/cache_write 四项单价')
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1e6 for v in rates.values()):
                raise ValueError('单价必须是有限非负数（美元 / 百万 Token）')
        with self.lock:
            db=self.connection();db.execute("INSERT OR REPLACE INTO settings VALUES ('prices',?)",(json.dumps(prices),));db.commit()
    @staticmethod
    def decorate(r, prices):
        r=dict(r)
        # Caches and reasoning are subsets, never add them a second time.
        r['cache_read']=min(r['input'],r['cache_read'])
        r['cache_write']=min(max(0,r['input']-r['cache_read']),r['cache_write'])
        r['uncached_input']=max(0,r['input']-r['cache_read']-r['cache_write'])
        r['tokens']=r['input']+r['output']
        rates=prices.get(r['model'])
        r['cost']=None if rates is None or r['usage_state']=='unknown' else (
            r['uncached_input']*rates['input']+r['output']*rates['output']+
            r['cache_read']*rates['cache_read']+r['cache_write']*rates['cache_write'])/1e6
        return r
    def query(self, start, end, model='',provider='',outcome='',offset=0,limit=50):
        if not (math.isfinite(start) and math.isfinite(end) and 0<end-start<=90*86400): raise ValueError('请选择不超过 90 天的时间范围')
        clauses=['started>=?','started<?'];args=[start,end]
        for k,v in [('model',model),('provider',provider),('outcome',outcome)]:
            if v: clauses.append(k+'=?');args.append(v)
        with self.lock:
            db=self.connection();prices=self.prices()
            cursor=db.execute('SELECT document FROM requests WHERE '+' AND '.join(clauses)+' ORDER BY started DESC,id DESC',args)
            total=dict(requests=0,success=0,errors=0,cancelled=0,unknown=0,partial=0,unpriced=0,cost=0,**dict.fromkeys(FIELDS,0))
            buckets={};groups={};page=[];step=3600 if end-start<=2*86400 else 86400
            for record in cursor:
                r=self.decorate(json.loads(record[0]),prices)
                n=total['requests'];total['requests']+=1
                if offset<=n<offset+limit:page.append(r)
                total['success']+=r['outcome']=='success';total['errors']+=r['outcome']=='error';total['cancelled']+=r['outcome']=='cancelled'
                total['unknown']+=r['usage_state']=='unknown';total['partial']+=r['usage_state']=='partial';total['unpriced']+=r['cost'] is None
                total['cost']+=r['cost'] or 0
                for k in FIELDS:total[k]+=r[k]
                # Buckets align to requested local midnight, not server timezone.
                bucket=start+int((r['started']-start)//step)*step
                b=buckets.setdefault(bucket,dict(at=bucket,requests=0,tokens=0,cache_read=0))
                b['requests']+=1;b['tokens']+=r['tokens'];b['cache_read']+=r['cache_read']
                key=(r['provider'],r['model'])
                g=groups.setdefault(key,dict(provider=key[0],model=key[1],requests=0,tokens=0,cost=0,unpriced=0))
                g['requests']+=1;g['tokens']+=r['tokens'];g['cost']+=r['cost'] or 0;g['unpriced']+=r['cost'] is None
            options=[dict(r) for r in db.execute('SELECT DISTINCT model,provider FROM requests WHERE started>=? AND started<? ORDER BY model',(start,end))]
        total['tokens']=total['input']+total['output']
        total['cache_rate']=total['cache_read']/total['input'] if total['input'] else None
        total['uncached_tokens']=total['tokens']-total['cache_read']-total['cache_write']
        timeline=[buckets.get(at,dict(at=at,requests=0,tokens=0,cache_read=0)) for at in (start+i*step for i in range(math.ceil((end-start)/step)))]
        return dict(summary=total,series=timeline,groups=list(groups.values()),rows=page,options=options,offset=offset,limit=limit,step=step,prices=prices)
