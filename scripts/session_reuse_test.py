"""Offline session lifecycle and API regressions; no credentials or model calls."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
RUNTIME = tempfile.TemporaryDirectory()
os.environ['AGY_ROOT'] = RUNTIME.name
os.environ['AGY_GATEWAY_KEY'] = 'test-key'
import acp_ant as core
from acp_sessions import SessionCache, fingerprints
import httpx


def msg(role, text):
    return {'role': role, 'parts': [{'type': 'text', 'text': text}]}


class FakeACP:
    def __init__(self, acc):
        self.acc, self.alive, self.epoch = acc, True, 1
        self.sessions, self.turns, self.cwd = 0, {}, RUNTIME.name
        self.calls, self.answers = [], ['ACK']
        self.next_id = 0
        self.block = None
        self.compact = False
        self.stop_reason = 'end_turn'

    async def ensure(self):
        pass

    async def maybe_recycle(self):
        pass

    async def notify(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))

    async def request(self, method, params, timeout):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'session/new':
            self.next_id += 1
            return {'sessionId': str(self.next_id), 'configOptions': []}
        if method == 'session/prompt':
            if self.block:
                await self.block.wait()
            answer = self.answers.pop(0) if self.answers else 'ACK'
            if isinstance(answer, Exception):
                raise answer
            turn = self.turns[params['sessionId']]
            if self.compact:
                turn.on_update({'sessionUpdate': 'compaction_update'})
            turn.on_update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': answer}})
            return {'stopReason': self.stop_reason, 'usage': {'inputTokens': 10, 'outputTokens': 1}}
        return {}


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [patch.object(core, 'SESSION_REUSE', True), patch.object(core, 'SESSIONS', SessionCache()),
                        patch.object(core, 'MAX_PROMPT_BYTES', 180_000), patch.object(core, 'POOL', core.Pool())]
        for p in self.patches:
            p.start()
        self.acc = core.Account('test', RUNTIME.name)
        self.acp = FakeACP(self.acc)
        self.acc.acp = self.acp
        core.POOL.accounts['test'] = self.acc
        core.POOL.scan = lambda: None
        core.PIN.set(None)
        core.SESSION_PLAN.set(None)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        core.SESSION_PLAN.set(None)
        core.PIN.set(None)

    async def run_turn(self, messages, system='', model='gemini-test', headers=None, tools=None, effort=None):
        request = SimpleNamespace(headers=headers or {}, url=SimpleNamespace(path='/v1/chat/completions'))
        system, blocks = core.prepare_prompt(request, model, system, messages, tools or [], 'auto', effort=effort)
        return [item async for item in core.run_on(self.acc, model, effort, system, blocks, time.time() + 10)]

    def calls(self, method):
        return [p for m, p in self.acp.calls if m == method]

    async def seed(self):
        await self.run_turn([msg('user', 'FIRST')])
        return [msg('user', 'FIRST'), msg('assistant', 'ACK'), msg('user', 'SECOND')]

    async def test_exact_prefix_appends_only_new_message(self):
        history = await self.seed()
        await self.run_turn(history)
        self.assertEqual(len(self.calls('session/new')), 1)
        self.assertEqual(self.calls('session/prompt')[-1]['prompt'], [{'type': 'text', 'text': 'SECOND'}])
        self.assertEqual(len(core.SESSIONS.entries), 1)

    async def test_cumulative_large_history_uses_small_delta(self):
        await self.run_turn([msg('user', 'a' * 100_000)])
        await self.run_turn([msg('user', 'a' * 100_000), msg('assistant', 'ACK'), msg('user', 'b' * 100_000)])
        self.assertEqual(len(self.calls('session/new')), 1)
        self.assertEqual(len(self.calls('session/prompt')[-1]['prompt'][0]['text']), 100_000)

    async def test_oversized_cold_history_errors_without_model_call(self):
        with self.assertRaisesRegex(core.AcpError, 'context_length_exceeded'):
            await self.run_turn([msg('user', '中' * 70_000)])
        self.assertEqual(self.calls('session/prompt'), [])

    async def test_edit_or_compaction_rebuilds(self):
        history = await self.seed()
        history[0] = msg('user', 'EDITED')
        await self.run_turn(history)
        self.assertEqual(len(self.calls('session/new')), 2)

    async def test_changed_assistant_rebuilds(self):
        history = await self.seed()
        history[1] = msg('assistant', 'Not the actual answer')
        await self.run_turn(history)
        self.assertEqual(len(self.calls('session/new')), 2)

    async def test_namespace_isolates_identical_history(self):
        await self.run_turn([msg('user', 'FIRST')], headers={'x-acp-session': 'one'})
        await self.run_turn([msg('user', 'FIRST'), msg('assistant', 'ACK'), msg('user', 'SECOND')], headers={'x-acp-session': 'two'})
        self.assertEqual(len(self.calls('session/new')), 2)

    async def test_configuration_changes_rebuild(self):
        for change in ({'system': 'new policy'}, {'model': 'other'}, {'effort': 'high'}):
            with self.subTest(change=change):
                core.SESSIONS = SessionCache()
                before = len(self.calls('session/new'))
                history = await self.seed()
                await self.run_turn(history, **change)
                self.assertEqual(len(self.calls('session/new')) - before, 2)

    async def test_process_restart_invalidates(self):
        history = await self.seed()
        self.acp.epoch += 1
        await self.run_turn(history)
        self.assertEqual(len(self.calls('session/new')), 2)

    async def test_ttl_expires_and_closes(self):
        history = await self.seed()
        for record in core.SESSIONS.entries.values():
            record.touched -= 2000
        await self.run_turn(history)
        self.assertEqual(len(self.calls('session/new')), 2)
        self.assertEqual(self.calls('session/close')[0]['sessionId'], '1')

    async def test_cache_has_bounded_size(self):
        core.SESSIONS.maximum = 1
        await self.run_turn([msg('user', 'one')])
        await self.run_turn([msg('user', 'two')])
        self.assertEqual(len(core.SESSIONS.entries), 1)
        self.assertEqual(len(self.calls('session/close')), 1)

    async def test_failure_invalidates_leased_session(self):
        history = await self.seed()
        self.acp.answers = [core.AcpError('429 resource exhausted', 'QUOTA')]
        with self.assertRaises(core.AcpError):
            await self.run_turn(history)
        self.assertEqual(len(core.SESSIONS.entries), 0)
        self.assertTrue(self.calls('session/close'))

    async def test_explicit_stale_session_rebuilds_once(self):
        history = await self.seed()
        self.acp.answers = [core.AcpError('session not found', 'SERVER'), 'ACK']
        await self.run_turn(history)
        self.assertEqual(len(self.calls('session/new')), 2)
        self.assertEqual(len(core.SESSIONS.entries), 1)

    async def test_quota_failure_is_not_rewritten_or_replayed(self):
        history = await self.seed()
        self.acp.answers = [core.AcpError('quota exceeded', 'QUOTA')]
        with self.assertRaises(core.AcpError) as caught:
            await self.run_turn(history)
        self.assertEqual(caught.exception.kind, 'QUOTA')
        self.assertEqual(len(self.calls('session/new')), 1)

    async def test_cancel_releases_session_and_prompt_task(self):
        history = await self.seed()
        self.acp.block = asyncio.Event()
        task = asyncio.create_task(self.run_turn(history))
        for _ in range(30):
            if len(self.calls('session/prompt')) == 2:
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(core.SESSIONS.entries), 0)
        self.assertTrue(self.calls('session/cancel'))
        self.assertFalse(self.acp.turns)

    async def test_same_session_not_leased_twice(self):
        history = await self.seed()
        self.acp.block = asyncio.Event()
        a = asyncio.create_task(self.run_turn(history))
        b = asyncio.create_task(self.run_turn(history))
        for _ in range(30):
            if len(self.calls('session/prompt')) == 3:
                break
            await asyncio.sleep(0)
        self.acp.block.set()
        await asyncio.gather(a, b)
        ids = [p['sessionId'] for p in self.calls('session/prompt')[-2:]]
        self.assertEqual(len(set(ids)), 2)

    async def test_compaction_event_disables_reuse(self):
        self.acp.compact = True
        await self.seed()
        self.assertEqual(len(core.SESSIONS.entries), 0)

    async def test_unknown_stop_reason_does_not_cache(self):
        self.acp.stop_reason = 'cancelled'
        await self.seed()
        self.assertEqual(len(core.SESSIONS.entries), 0)

    async def test_cached_account_is_preferred_but_disabled_account_is_not(self):
        history = await self.seed()
        request = SimpleNamespace(headers={}, url=SimpleNamespace(path='/v1/chat/completions'))
        core.prepare_prompt(request, 'gemini-test', '', history, [], 'auto')
        other = core.Account('other', RUNTIME.name)
        core.POOL.accounts['other'] = other
        self.acc.last_pick = time.time()
        self.assertIs(core.POOL.pick(set()), self.acc)
        self.acc.enabled = False
        self.assertIs(core.POOL.pick(set()), other)

    async def test_off_header_does_not_cache(self):
        await self.run_turn([msg('user', 'FIRST')], headers={'x-acp-session': 'off'})
        self.assertEqual(len(core.SESSIONS.entries), 0)
        self.assertTrue(self.calls('session/close'))

    async def test_tool_loop_reuses_and_sends_id_bindings(self):
        tools = [{'name': 'read', 'description': 'read', 'parameters': {}}]
        self.acp.answers = ['<tool_calls>[{"name":"read","arguments":{"path":"a.txt"}}]</tool_calls>', 'done']
        messages = [msg('user', 'Read a.txt')]
        await self.run_turn(messages, tools=tools)
        messages += [{'role': 'assistant', 'parts': [{'type': 'tool_call', 'name': 'read', 'id': 'client_42', 'arguments': '{"path":"a.txt"}'}]},
                     {'role': 'tool', 'parts': [{'type': 'tool_result', 'name': 'read', 'id': 'client_42', 'text': 'file contents', 'is_error': False}]}]
        await self.run_turn(messages, tools=tools)
        self.assertEqual(len(self.calls('session/new')), 1)
        text = str(self.calls('session/prompt')[-1]['prompt'])
        self.assertIn('client_42', text)
        self.assertIn('a.txt', text)
        self.assertIn('file contents', text)
        self.assertNotIn('Read a.txt', text)
        messages += [msg('assistant', 'done'), msg('user', 'Next question')]
        await self.run_turn(messages, tools=tools)
        self.assertEqual(len(self.calls('session/new')), 1)
        self.assertEqual(self.calls('session/prompt')[-1]['prompt'][0]['text'], 'Next question')

    async def test_trimmed_historical_image_does_not_consume_current_budget(self):
        image = lambda name: {'type': 'image', 'mime': 'image/png', 'data': name}
        old = msg('user', 'x' * 5000)
        old['parts'].append(image('OLD'))
        current = {'role': 'user', 'parts': [image('NEW1'), image('NEW2')]}
        with patch.object(core, 'MAX_PROMPT_CHARS', 1000), patch.object(core, 'MAX_IMAGES', 2):
            _, blocks = core.to_acp('', [old, msg('assistant', 'ack'), current], [], 'auto')
        self.assertEqual([b['data'] for b in blocks if b['type'] == 'image'], ['NEW1', 'NEW2'])

    async def test_actual_http_protocols_and_streams(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app), base_url='http://test', headers={'Authorization': 'Bearer test-key'}) as client:
            for path in ('/v1/chat/completions', '/v1/messages', '/v1/responses'):
                for stream in (False, True):
                    with self.subTest(path=path, stream=stream):
                        core.SESSIONS = SessionCache()
                        before = len(self.calls('session/new'))
                        for history in ([{'role': 'user', 'content': 'FIRST'}], [{'role': 'user', 'content': 'FIRST'}, {'role': 'assistant', 'content': 'ACK'}, {'role': 'user', 'content': 'SECOND'}]):
                            body = {'model': 'gemini-test', 'stream': stream, ('input' if path.endswith('responses') else 'messages'): history}
                            response = await client.post(path, json=body)
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertNotIn('"error": {', response.text)
                        self.assertEqual(len(self.calls('session/new')) - before, 1)

    async def test_count_tokens_grows_with_untrimmed_request(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=core.app), base_url='http://test', headers={'Authorization': 'Bearer test-key'}) as client:
            values = []
            for n in (10, 30):
                messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'a' * 10_000} for i in range(n)]
                response = await client.post('/v1/messages/count_tokens', json={'messages': messages})
                values.append(response.json()['input_tokens'])
            self.assertGreater(values[1], values[0] * 2)

    async def test_result_id_bindings_distinguish_swaps(self):
        m = [{'role': 'assistant', 'parts': [dict(type='tool_call', id='a', name='read', arguments={}), dict(type='tool_call', id='b', name='read', arguments={})]},
             {'role': 'tool', 'parts': [dict(type='tool_result', id='a', name='read', text='one', is_error=False)]}]
        other = copy.deepcopy(m)
        other[-1]['parts'][0]['id'] = 'b'
        self.assertNotEqual(fingerprints(m), fingerprints(other))


if __name__ == '__main__':
    unittest.main()
