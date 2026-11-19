"""Offline regressions: python3 scripts/pi_prompt_compat_test.py (no agy or network)."""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from pi_prompt_compat import COMPAT_SYSTEM, rewrite_pi_prompt

runtime = tempfile.TemporaryDirectory()
os.environ['AGY_ROOT'] = runtime.name
os.environ['AGY_GATEWAY_KEY'] = 'test-key'
os.environ.pop('AGY_PI_PROMPT_COMPAT', None)
import acp_ant as core

SYSTEM = (Path(__file__).parent / 'fixtures/pi_1_5_2_system.txt').read_text()
SUFFIX = SYSTEM[SYSTEM.index('</docs>') + len('</docs>'):]


class PiPromptCompatTest(unittest.TestCase):
    def test_default_scaffold_and_cwd(self):
        self.assertEqual(rewrite_pi_prompt(SYSTEM), COMPAT_SYSTEM + SUFFIX)

    def test_different_installation_paths(self):
        text = SYSTEM.replace('/opt/pi/node_modules/pi-coding-agent', '/home/example/.local/lib/pi')
        self.assertEqual(rewrite_pi_prompt(text), COMPAT_SYSTEM + SUFFIX)

    def test_expanded_inventory_and_additional_rules(self):
        # A four-tool-only fingerprint misses real pi setups with extensions.
        inventory = ''.join('- extension_%d: Additional client tool\n' % i for i in range(11))
        rules = ''.join('- CUSTOM %d: ask before deleting production files.\n' % i for i in range(14))
        text = SYSTEM.replace('</tools>', inventory + '</tools>')
        text = text.replace('</rules>', rules + '</rules>')
        rewritten = rewrite_pi_prompt(text)
        self.assertTrue(rewritten.startswith(COMPAT_SYSTEM))
        self.assertIn(rules, rewritten)
        self.assertTrue(rewritten.endswith(SUFFIX))

    def test_modified_rule_is_preserved(self):
        text = SYSTEM.replace('- Be concise in your responses', '- CUSTOM POLICY: never delete production data')
        self.assertIn('- CUSTOM POLICY: never delete production data', rewrite_pi_prompt(text))

    def test_additional_instructions_are_preserved_verbatim(self):
        suffix = '\n\nPROJECT POLICY\r\n  Keep secrets private.\nNever invent tool output.\n'
        self.assertEqual(rewrite_pi_prompt(SYSTEM + suffix), COMPAT_SYSTEM + SUFFIX + suffix)

    def test_unknown_prompts_and_incomplete_scaffolds_are_unchanged(self):
        for text in ('', 'Custom system prompt', 'Prefix policy\n' + SYSTEM,
                     SYSTEM.replace('pi, a coding agent harness', 'a different harness'),
                     SYSTEM.replace('</rules>', ''), SYSTEM.replace('</docs>', '')):
            with self.subTest(text=text[:50]):
                self.assertEqual(rewrite_pi_prompt(text), text)

    def test_custom_text_inside_inventory_or_docs_is_not_discarded(self):
        for tag in ('tools', 'docs'):
            text = SYSTEM.replace('</%s>' % tag, 'CUSTOM SECURITY POLICY\n</%s>' % tag)
            self.assertEqual(rewrite_pi_prompt(text), text)

    def test_duplicated_docs_or_missing_path_is_not_normalized(self):
        line = '- Main documentation: /opt/pi/node_modules/pi-coding-agent/README.md'
        for text in (SYSTEM.replace(line, ''), SYSTEM.replace(line, line + '\n' + line)):
            self.assertEqual(rewrite_pi_prompt(text), text)

    def test_rewrite_is_idempotent(self):
        text = rewrite_pi_prompt(SYSTEM)
        self.assertEqual(rewrite_pi_prompt(text), text)

    def test_default_is_disabled(self):
        self.assertFalse(core.PI_PROMPT_COMPAT)
        messages = [{'role': 'user', 'parts': [{'type': 'text', 'text': 'LATEST INPUT'}]}]
        with patch.object(core, 'PI_PROMPT_COMPAT', False):
            system, _ = core.to_acp(SYSTEM, messages, [], 'auto')
        self.assertEqual(system, SYSTEM)

    def test_conversion_preserves_messages_tools_and_extra_instructions(self):
        messages = [{'role': 'user', 'parts': [{'type': 'text', 'text': 'LATEST INPUT'}]}]
        tools = [{'name': 'read', 'description': 'Read file contents',
                  'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}]
        before = copy.deepcopy((messages, tools))
        with patch.object(core, 'PI_PROMPT_COMPAT', True):
            with self.assertLogs('acp-ant', level='INFO') as logs:
                system, blocks = core.to_acp(SYSTEM, messages, tools, ('tool', 'read'), 'EXTRA INSTRUCTION')
        self.assertTrue(system.startswith(COMPAT_SYSTEM))
        self.assertIn('"name": "read"', system)
        self.assertIn('"path": {"type": "string"}', system)
        self.assertIn('EXTRA INSTRUCTION', system)
        self.assertIn('LATEST INPUT', str(blocks))
        self.assertEqual((messages, tools), before)
        self.assertNotIn(SYSTEM, '\n'.join(logs.output))

    def test_all_antigravity_protocol_converters(self):
        requests = (
            (core.from_anthropic, {'system': [{'type': 'text', 'text': SYSTEM}],
                                   'messages': [{'role': 'user', 'content': 'hello'}]}),
            (core.from_openai, {'messages': [{'role': 'system', 'content': SYSTEM},
                                            {'role': 'user', 'content': 'hello'}]}),
            (core.from_responses, {'instructions': SYSTEM, 'input': 'hello'}),
        )
        for convert, body in requests:
            with self.subTest(converter=convert.__name__):
                system, messages, tools, choice = convert(body)[:4]
                with patch.object(core, 'PI_PROMPT_COMPAT', True):
                    normalized, blocks = core.to_acp(system, messages, tools, choice)
                self.assertTrue(normalized.startswith(COMPAT_SYSTEM))
                self.assertIn('hello', str(blocks))


if __name__ == '__main__':
    unittest.main()
