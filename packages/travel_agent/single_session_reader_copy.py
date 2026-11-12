"""Presentation vocabulary only; no source, price or certainty changes."""
import re


def reader_text(text):
    return str(text).replace('失败出路', '应急方案').replace('未核验', '待确认')


def reader_markdown(text):
    """Keep full sentences; long list entries become readable paragraphs."""
    output, emergency = [], False
    for line in reader_text(text).splitlines():
        if re.match(r'^#{1,6}\s', line) or re.fullmatch(r'\*\*[^*]+\*\*', line.strip()):
            emergency = line.strip('* #') == '应急方案'
        bullet = re.match(r'^\s*(?:[-*]|\d+[.)])\s+(.+)$', line)
        if bullet:
            value = bullet[1]
            if emergency and '：' in value:
                trigger, response = value.split('：', 1)
                value = '遇上' + trigger + '时：' + response
            # 36 CJK characters leave room for three lines at 375 px. Longer
            # items retain every word but no longer pretend to be short bullets.
            line = '- ' + value if len(value) <= 36 else value
        output.append(line)
    return '\n'.join(output)
