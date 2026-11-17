"""Offline: an AI Studio account whose page Google refuses with 403 / code 7 is restarted (disable + enable, same
settings), its permission cooldowns cleared, and the failed request tried once more. Fake AIStudio2API, no network.
Usage: python scripts/studio_heal_test.py"""
import asyncio
import json
import logging
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
import httpx
import studio_provider as sp

checks = 0


def ok(cond, label):
    global checks
    assert cond, label
    checks += 1
    print('PASS', label)


DENY = '权限拒绝: AI Studio GenerateContent 返回 PERMISSION_DENIED、协议错误码 7: The caller does not have permission'
QUOTA = '每日限额: AI Studio GenerateContent 返回 HTTP 429、协议错误码 8: limit: 50'


def account(i, **kw):
    a = {'id': i, 'label': i, 'enabled': True, 'state': 'ready', 'proxy': 'socks5://u:SECRET@1.2.3.4:1080',
         'egress': 'socks5://u:SECRET@1.2.3.4:1080', 'locale': 'en-US', 'timezone': 'America/Los_Angeles',
         'models': ['gemini-4-hydrogen'], 'benefit_tier': 'Free', 'message': '', 'active_requests': 0}
    a.update(kw)
    return a


S = {}


def reset(accounts, cooldowns, deny_calls=0, fail_enable=0):
    S.clear()
    S.update(accounts=accounts, cooldowns=cooldowns, puts=[], deletes=[], gen=[], deny_calls=deny_calls,
             fail_enable=fail_enable)


def handler(request):
    path, method = request.url.path, request.method
    if path == '/api/auth/login':
        return httpx.Response(200, json={'ok': True})
    if path == '/api/accounts' and method == 'GET':
        return httpx.Response(200, json={'accounts': S['accounts']})
    if path == '/api/status':
        return httpx.Response(200, json={'state': 'RUNNING', 'accounts': {'ready': 2}})
    if path == '/api/cooldowns':
        return httpx.Response(200, json={'cooldowns': S['cooldowns']})
    if path == '/api/usage':
        return httpx.Response(200, json={'groups': {'account': []}})
    if method == 'PUT' and path.startswith('/api/accounts/'):
        body = json.loads(request.content)
        if body['enabled'] and S['fail_enable']:
            S['fail_enable'] -= 1
            return httpx.Response(500, json={'error': 'busy'})
        S['puts'].append((path.split('/')[3], body))
        return httpx.Response(200, json={'account': {}})
    if method == 'DELETE' and '/cooldowns/' in path:
        acc, scope = path.split('/')[3], path.rsplit('/', 1)[1]
        S['deletes'].append((acc, scope))
        S['cooldowns'] = [c for c in S['cooldowns'] if not (c['account_id'] == acc and c['scope'] == scope)]
        return httpx.Response(204)
    if path == '/v1/chat/completions':
        S['gen'].append(request.headers.get('x-aistudio-account', ''))
        if S['deny_calls']:
            S['deny_calls'] -= 1
            return httpx.Response(503, json={'error': {'message': 'Service unavailable: ' + DENY.split(': ', 1)[1]}})
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
    return httpx.Response(404)


def provider():
    p = sp.StudioProvider({'base_url': 'http://127.0.0.1:9', 'api_key': 'sidecar-key', 'admin_password': 'pw'})
    p._client = lambda timeout=290: httpx.AsyncClient(base_url=p.base, transport=httpx.MockTransport(handler))
    p.notes = []
    p.on_event = lambda email, text: p.notes.append((email, text))
    return p


def cd(acc, scope, reason):
    return {'account_id': acc, 'model_id': scope, 'scope': scope, 'until': '2026-11-17T16:00:00Z', 'reason': reason}


class Capture(logging.Handler):
    lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


logging.getLogger('acp-ant.studio').addHandler(Capture())
sleep = asyncio.sleep


async def nosleep(_):
    await sleep(0)
sp.asyncio.sleep = nosleep
BODY = {'model': 'aistudio/gemini-4-hydrogen', 'messages': [{'role': 'user', 'content': 'hi'}]}


async def main():
    # 1. background check restarts only the refused account
    reset([account('bad@x'), account('good@x'), account('busy@x', active_requests=1), account('off@x', enabled=False)],
          [cd('bad@x', 'gemini-4-hydrogen', DENY), cd('bad@x', 'gemini-3.8-flash', DENY), cd('good@x', 'gemini-4-hydrogen', QUOTA),
           cd('busy@x', 'gemini-4-hydrogen', DENY), cd('off@x', 'gemini-4-hydrogen', DENY)])
    p = provider()
    rows, err = await p.accounts(force=True)
    ok([(a, b['enabled']) for a, b in S['puts']] == [('bad@x', False), ('bad@x', True)],
       'the refused account is switched off and on again; busy, disabled and quota-limited accounts are left alone')
    ok(S['puts'][1][1] == {'label': 'bad@x', 'enabled': True, 'proxy': 'socks5://u:SECRET@1.2.3.4:1080', 'locale': 'en-US',
                           'timezone': 'America/Los_Angeles'}, 'proxy, locale and time zone are kept exactly')
    ok(sorted(S['deletes']) == [('bad@x', 'gemini-3.8-flash'), ('bad@x', 'gemini-4-hydrogen')],
       'its permission cooldowns are cleared; the quota cooldown of the other account is not')
    bad = next(r for r in rows if r['email'] == 'bad@x')
    ok(bad['ready'] and bad['error'] == '' and not any(l['kind'] == '权限拒绝' for l in bad['quota']['limits']),
       'the panel row is read again after the repair')
    ok(p.notes and p.notes[0][0] == 'bad@x' and '权限码 7' in p.notes[0][1], 'the repair is written to the activity log')
    ok(not any('SECRET' in x for x in Capture.lines + [t for _, t in p.notes]), 'no proxy credential in logs or events')

    # 2. not again within HEAL_GAP
    S['cooldowns'] = [cd('bad@x', 'gemini-4-hydrogen', DENY)]
    S['puts'].clear()
    await p.accounts(force=True)
    ok(S['puts'] == [], 'one restart per account per HEAL_GAP at most')

    # 3. after HEAL_TRIES restarts in a row the account is left alone and the panel explains
    for _ in range(sp.HEAL_TRIES - 1):
        p.heals['bad@x']['at'] -= sp.HEAL_GAP + 1
        S['cooldowns'] = [cd('bad@x', 'gemini-4-hydrogen', DENY)]
        await p.accounts(force=True)
    ok(p.heals['bad@x']['count'] == sp.HEAL_TRIES, 'restarts are counted while the refusal keeps coming back')
    p.heals['bad@x']['at'] -= sp.HEAL_GAP + 1
    S['cooldowns'] = [cd('bad@x', 'gemini-4-hydrogen', DENY)]
    S['puts'].clear()
    rows, _ = await p.accounts(force=True)
    bad = next(r for r in rows if r['email'] == 'bad@x')
    ok(S['puts'] == [] and 'aistudio.google.com' in bad['error'], 'then it pauses and the panel tells the owner what to check')
    p.heals['bad@x']['at'] -= sp.HEAL_PAUSE + 1
    await p.accounts(force=True)
    ok(len(S['puts']) == 2 and p.heals['bad@x']['count'] == 1, 'after HEAL_PAUSE it tries again from one')

    # 4. a request refused with code 7 is repaired and retried once
    reset([account('bad@x'), account('good@x')], [cd('bad@x', 'gemini-4-hydrogen', DENY)], deny_calls=1)
    p = provider()
    r = await p.forward('/v1/chat/completions', BODY)
    ok(r.status_code == 200 and len(S['gen']) == 2 and [a for a, _ in S['puts']] == ['bad@x', 'bad@x'],
       'a refused request triggers the restart and succeeds on the single retry')
    reset([account('bad@x')], [cd('bad@x', 'gemini-4-hydrogen', DENY)], deny_calls=5)
    p = provider()
    r = await p.forward('/v1/chat/completions', dict(BODY, stream=True))
    ok(r.status_code == 503 and len(S['gen']) == 2, 'still refused after the restart: the error is returned, no retry loop')

    # 5. pinned to an account that was not the one restarted -> no retry on someone else
    reset([account('bad@x'), account('pin@x', active_requests=1)],
          [cd('bad@x', 'gemini-4-hydrogen', DENY), cd('pin@x', 'gemini-4-hydrogen', DENY)], deny_calls=1)
    p = provider()
    r = await p.forward('/v1/chat/completions', BODY, None, 'pin@x')
    ok(r.status_code == 503 and S['gen'] == ['pin@x'], 'a pinned request is retried only when its own account was restarted')

    # 6. quota errors and nothing to repair -> no restart, no retry
    reset([account('good@x')], [cd('good@x', 'gemini-4-hydrogen', QUOTA)])
    p = provider()
    S['deny_calls'] = 1
    r = await p.forward('/v1/chat/completions', BODY)
    ok(r.status_code == 503 and len(S['gen']) == 1 and S['puts'] == [], 'nothing to restart -> the original error, one call')

    # 7. a failing re-enable is retried so the account is never left switched off
    reset([account('bad@x')], [cd('bad@x', 'gemini-4-hydrogen', DENY)], fail_enable=1)
    p = provider()
    await p.accounts(force=True)
    ok([b['enabled'] for _, b in S['puts']] == [False, True], 're-enable is retried after a failure')
    reset([account('bad@x')], [cd('bad@x', 'gemini-4-hydrogen', DENY)], fail_enable=9)
    p = provider()
    await p.accounts(force=True)
    ok(any('手动启用' in t for _, t in p.notes) and S['deletes'] == [], 'if it cannot be re-enabled the owner is told')

    ok(sp.DENIED.search(DENY) and not sp.DENIED.search(QUOTA) and not sp.DENIED.search('协议错误码 70'),
       'only permission refusals match, not quota (code 8)')


asyncio.run(main())
print('%d/%d passed' % (checks, checks))
