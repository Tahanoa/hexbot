import unittest
from unittest.mock import Mock

from bot import Config, Telegram
from formatting import reply_parts


class FormattingTests(unittest.TestCase):
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
