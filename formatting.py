"""Render common model Markdown as Telegram text and UTF-16 entities."""
import re

PRESENTATION_PROMPT = (
    "پاسخ را مرتب بنویس: هر موضوع در یک پاراگراف کوتاه و بین پاراگراف‌ها یک خط خالی. "
    "برای چند مورد مستقل از فهرست کوتاه و برای مراحل از شماره‌گذاری استفاده کن. "
    "عنوان کوتاه و بولد فقط وقتی چند بخش لازم است؛ پاسخ ساده را بی‌دلیل بخش‌بندی نکن. "
    "کد واقعی را در بلوک کد بگذار، اما متن معمولی را داخل بک‌تیک یا کوتیشن نگذار. "
    "از ایموجی معمولی به‌اندازه کم و متناسب با موضوع استفاده کن؛ شناسه یا تگ ایموجی پریمیوم نساز."
)


MARKUP = re.compile(
    r"```(?:(?P<language>[\w.+-]*)\n)?(?P<pre>.*?)(?:```|\Z)"
    r"|`(?P<code>[^`\n]+)`"
    r"|\*\*(?P<bold>[^*]+)\*\*"
    r"|__(?P<underline_bold>[^_]+)__"
    r"|~~(?P<strikethrough>[^~]+)~~"
    r"|(?<!\*)\*(?P<italic>[^*\n]+)\*(?!\*)",
    re.DOTALL,
)


def reply_parts(source, formatted=False):
    """Split after parsing, clipping entities so long code blocks stay valid."""
    text, spans, cursor = '', [], 0
    if formatted:
        for match in MARKUP.finditer(source):
            text += source[cursor:match.start()]
            kind = next(name for name in ('pre', 'code', 'bold', 'underline_bold',
                                         'strikethrough', 'italic')
                        if match.group(name) is not None)
            value = match.group(kind)
            entity = {'type': 'bold' if kind == 'underline_bold' else kind}
            if kind == 'pre' and match.group('language'):
                entity['language'] = match.group('language')
            spans.append((len(text), len(text) + len(value), entity))
            text += value
            cursor = match.end()
    text += source[cursor:]
    start = 0
    while start < len(text):
        end = min(start + 2000, len(text))
        if end < len(text):
            boundary = text.rfind('\n\n', start + 1000, end)
            if boundary >= 0:
                end = boundary + 2
            else:
                boundary = text.rfind('\n', start + 1000, end)
                if boundary >= 0:
                    end = boundary + 1
        part = text[start:end]
        entities = []
        for left, right, entity in spans:
            left, right = max(start, left), min(end, right)
            if left < right:
                entities.append(dict(entity,
                    offset=len(text[start:left].encode('utf-16-le')) // 2,
                    length=len(text[left:right].encode('utf-16-le')) // 2))
        payload = {'text': part}
        if entities:
            payload['entities'] = entities
        yield payload
        start = end


def with_assistant_footer(payload, url):
    """Add a trusted bot link outside model-generated formatting/code entities."""
    if not url:
        return payload
    lead, label = '\n\nتوسط ', 'دستیار شخصی'
    offset = len((payload['text'] + lead).encode('utf-16-le')) // 2
    return dict(payload, text=payload['text'] + lead + label,
                entities=list(payload.get('entities', [])) + [{'type': 'text_link',
                    'offset': offset, 'length': len(label.encode('utf-16-le')) // 2, 'url': url}],
                link_preview_options={'is_disabled': True})
