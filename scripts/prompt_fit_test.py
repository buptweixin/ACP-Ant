"""Offline: tool calls whose JSON contains raw newlines (#3) and long histories that must keep the newest input (#4).
Usage: python scripts/prompt_fit_test.py   (no agy, no network)"""
import os
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
os.environ.setdefault('AGY_ROOT', tempfile.mkdtemp())
os.environ.setdefault('AGY_GATEWAY_KEY', 'test-key')
import acp_ant as core

passed = 0


def ok(cond, label):
    global passed
    assert cond, label
    passed += 1
    print('PASS', label)


raw = 'Running it now.\n<tool_calls>[{"name": "terminal", "arguments": {"command": "python3 -c \\"\nimport os\nprint(1)\n\\""}}]</tool_calls>'
text, calls = core.parse_tool_calls(raw)
ok(calls and calls[0]['name'] == 'terminal' and '\nimport os\n' in (calls[0]['arguments']['command'] if isinstance(calls[0]['arguments'], dict)
                                                                      else calls[0]['arguments']) and '<tool_calls>' not in text,
   'tool call with real newlines inside a string is parsed, not leaked as text (#3)')
text, calls = core.parse_tool_calls('<tool_calls>[{"name": "write", "arguments": {"content": "a\tb\nc"}}]</tool_calls>')
ok(calls and calls[0]['arguments']['content'] == 'a\tb\nc', 'tabs and newlines in values survive')


def user(t):
    return {'role': 'user', 'parts': [{'type': 'text', 'text': t}]}


def assistant(t):
    return {'role': 'assistant', 'parts': [{'type': 'text', 'text': t}]}


def joined(blocks):
    return ''.join(b.get('text', '') for b in blocks if b['type'] == 'text')


small = [user('hi'), assistant('hello'), user('LATEST-QUESTION')]
_, blocks = core.to_acp('', small, [], 'auto')
ok('left out' not in joined(blocks) and 'hello' in joined(blocks), 'a short conversation is untouched')

core.MAX_PROMPT_CHARS = 20_000
long = []
for i in range(40):
    long += [user('old question %d ' % i + 'x' * 800), assistant('old answer %d ' % i + 'y' * 800)]
long.append(user('LATEST-QUESTION please answer this one'))
_, blocks = core.to_acp('system words', long, [], 'auto')
out = joined(blocks)
ok(out.rstrip().endswith('LATEST-QUESTION please answer this one') and len(out) <= 20_000,
   'a too-long history is cut from the oldest end; the newest input is complete and last (#4)')
ok('earlier message(s) were left out' in out and 'old answer 39' in out and 'old question 0 ' not in out,
   'the model is told that older messages were left out; the most recent history is kept')

tool_hist = [user('run it'), {'role': 'assistant', 'parts': [{'type': 'tool_call', 'id': 't1', 'name': 'terminal', 'arguments': {'cmd': 'ls'}}]},
             {'role': 'tool', 'parts': [{'type': 'tool_result', 'id': 't1', 'name': 'terminal', 'text': 'Z' * 60_000, 'is_error': False}]},
             assistant('done'), user('LATEST')]
core.MAX_PROMPT_CHARS = 30_000
_, blocks = core.to_acp('', tool_hist, [], 'auto')
out = joined(blocks)
ok('earlier tool output left out' in out and out.count('Z') < 25_000 and 'run it' in out and out.rstrip().endswith('LATEST'),
   'a huge old tool output is shortened (head + tail) before whole messages are dropped')

core.MAX_PROMPT_CHARS = 5_000
big_now = [user('a'), assistant('b'), user('N' * 9_000)]
_, blocks = core.to_acp('', big_now, [], 'auto')
ok(joined(blocks).count('N') == 9_000, 'the latest input is never shortened, even when it alone exceeds the budget')
core.MAX_PROMPT_CHARS = 0
_, blocks = core.to_acp('', long, [], 'auto')
ok('left out' not in joined(blocks), 'AGY_MAX_PROMPT_CHARS=0 turns the limit off')
print('%d/%d passed' % (passed, passed))
