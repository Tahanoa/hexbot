import unittest
from unittest.mock import Mock

from bot import Config, Telegram
from formatting import reply_parts, with_assistant_footer


class FormattingTests(unittest.TestCase):
    def test_footer_link_offsets_after_emoji_and_markup(self):
        part, = reply_parts('😀 **سلام**', True)
        original = dict(part)
        result = with_assistant_footer(part, 'https://t.me/h_ex_bot')
        self.assertEqual(part, original)
        footer = result['entities'][-1]
        units = result['text'].encode('utf-16-le')
        self.assertEqual(units[footer['offset'] * 2:(footer['offset'] + footer['length']) * 2].decode('utf-16-le'), 'دستیار شخصی')
        self.assertEqual(footer['url'], 'https://t.me/h_ex_bot')

    def test_inline_code_and_utf16_offsets(self):
        part, = reply_parts('😀 نام مدل: `gemma3:12b` و **سلام**', True)
        self.assertEqual(part['text'], '😀 نام مدل: gemma3:12b و سلام')
        code, bold = part['entities']
        self.assertEqual(code, {'type': 'code', 'offset': 12, 'length': 10})
        self.assertEqual(bold['type'], 'bold')
        self.assertEqual(bold['length'], 4)

    def test_long_code_block_preserves_contents_and_entities(self):
        code = '😀<tag>& **literal**\n' * 250
        parts = list(reply_parts('```python\n' + code + '```', True))
        self.assertEqual(''.join(p['text'] for p in parts), code)
        for part in parts:
            self.assertLessEqual(len(part['text'].encode('utf-16-le')) // 2, 4000)
            self.assertEqual(part['entities'], [{'type': 'pre', 'language': 'python',
                'offset': 0, 'length': len(part['text'].encode('utf-16-le')) // 2}])

    def test_unclosed_fence_and_plain_text(self):
        part, = reply_parts('```\nprint(1)', True)
        self.assertEqual(part['text'], 'print(1)')
        part, = reply_parts('a_b <tag> & "quote" `unfinished', True)
        self.assertEqual(part['text'], 'a_b <tag> & "quote" `unfinished')
        self.assertNotIn('entities', part)
        self.assertEqual(list(reply_parts('**raw** `code`')), [{'text': '**raw** `code`'}])

    def test_telegram_sends_entities_without_parse_mode(self):
        telegram = Telegram(Config('test'))
        telegram.call = Mock()
        telegram.send(10, '`code`', formatted=True)
        telegram.call.assert_called_once_with('sendMessage', {'chat_id': 10,
            'text': 'code', 'entities': [{'type': 'code', 'offset': 0, 'length': 4}]})

    def test_long_reply_parts_prefer_paragraph_boundaries(self):
        source = 'a' * 1500 + '\n\n' + 'b' * 1500
        parts = list(reply_parts(source))
        self.assertEqual(parts[0]['text'], 'a' * 1500 + '\n\n')
        self.assertEqual(''.join(p['text'] for p in parts), source)

    def test_all_normal_reply_parts_reply_to_original_message(self):
        telegram = Telegram(Config('test'))
        telegram.call = Mock()
        telegram.send(10, 'x' * 4001, reply_to=42)
        self.assertEqual(telegram.call.call_count, 3)
        for call in telegram.call.call_args_list:
            self.assertEqual(call.args[1]['reply_parameters'],
                             {'message_id': 42, 'allow_sending_without_reply': True})
