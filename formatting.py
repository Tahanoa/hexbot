"""Render common model Markdown as Telegram text and UTF-16 entities."""
import re


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
    for start in range(0, len(text), 2000):
        end = start + 2000
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
