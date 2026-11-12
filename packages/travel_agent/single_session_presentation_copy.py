"""Lossless deterministic reader projection; never strip certainty qualifiers."""
import html
from html.parser import HTMLParser
import re


_PENDING = re.compile(r'待确认|待复核|待核|未核|未交叉确认|尚待确认|未知|尚未确认')
_S1 = re.compile(r'S1|班次|末班|首班|开航|停航|停运|闭园|封路|开放|营业|预约|入园|入境|许可')


class _Copy(HTMLParser):
    """Transform complete text blocks, leaving structural/timeline nodes intact."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output, self.pending, self.seen = [], [], set()
        self.capture = None
        self.depth = 0
        self.head = None
        self.heading = ''
        self.scope = ''
        self.in_corrections = False
        self.corrections_depth = 0
        self.block_tag = ''
        self.divs = []
        self.tier_count = 0
        self.tier_pointers = set()

    def emit(self, text):
        if self.capture is not None:
            self.capture.append(text)
        else:
            self.output.append(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'div':
            tier = self.divs[-1] if self.divs else None
            if 'budget-tier' in values.get('class', '').split():
                self.tier_count += 1
                tier = self.tier_count
            self.divs.append(tier)
        if tag == 'section':
            if 'trip-corrections' in values.get('class', '').split():
                self.in_corrections = True
            if self.in_corrections:
                self.corrections_depth += 1
        if tag in ('h2', 'h3', 'h4'):
            self.head, self.heading = tag, ''
        if self.capture is None and tag in ('p', 'dd', 'li', 'table'):
            self.capture, self.depth = [], 0
            self.block_tag = tag
        if self.capture is not None and tag not in ('img', 'br', 'hr', 'input', 'meta', 'link'):
            self.depth += 1
        self.emit(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.emit('</' + tag + '>')
        if tag == self.head:
            self.scope, self.head = self.heading, None
        if self.capture is not None:
            self.depth -= 1
            if self.depth == 0:
                fragment = ''.join(self.capture)
                self.capture = None
                plain = html.unescape(re.sub(r'<[^>]+>', '', fragment)).strip()
                # Headings supply the entity/day when the source sentence omits it.
                key = plain
                if _PENDING.search(plain) and not _S1.search(plain) and not self.in_corrections:
                    if self.block_tag == 'li':
                        fragment = '<ul>' + fragment + '</ul>'
                    elif self.block_tag == 'dd':
                        fragment = '<dl>' + fragment + '</dl>'
                    tier = self.divs[-1] if self.divs else None
                    self.pending.append((self.scope, fragment, tier))
                    tag = self.block_tag if self.block_tag != 'table' else 'p'
                    if tier is None or tier not in self.tier_pointers:
                        self.output.append('<' + tag + ' class="pending-pointer">相关条件见⑤「出发前先核这几条」。</' + tag + '>')
                        if tier is not None:
                            self.tier_pointers.add(tier)
                elif len(plain) >= 16 and key in self.seen:
                    tag = self.block_tag if self.block_tag != 'table' else 'p'
                    self.output.append('<' + tag + ' class="fact-pointer">同项说明见前文。</' + tag + '>')
                else:
                    self.seen.add(key)
                    self.output.append(fragment)
        if tag == 'div' and self.divs:
            self.divs.pop()
        if tag == 'section' and self.in_corrections:
            self.corrections_depth -= 1
            if not self.corrections_depth:
                self.in_corrections = False

    def handle_data(self, data):
        if self.head:
            self.heading += data
        self.emit(data)

    def handle_entityref(self, name):
        self.emit('&' + name + ';')

    def handle_charref(self, name):
        self.emit('&#' + name + ';')

    def handle_startendtag(self, tag, attrs):
        self.emit(self.get_starttag_text())


def concentrate(visual):
    parser = _Copy()
    parser.feed(visual)
    parser.close()
    if parser.capture is not None:
        raise ValueError('presentation.html: balanced text-block rule violated')
    output = ''.join(parser.output)
    if parser.pending:
        groups, seen = {}, set()
        for scope, fragment, tier in parser.pending:
            if (scope, fragment, tier) in seen:
                continue
            seen.add((scope, fragment, tier))
            key = ('tier', tier) if tier is not None else ('item', len(groups))
            if key not in groups:
                label = scope.removesuffix('（主档）') if tier is not None else scope
                if tier is not None and not label.endswith('档'):
                    label += '档'
                groups[key] = (label, [])
            groups[key][1].append(fragment)
        rows = ['<div class="pending-condition"><strong>' + html.escape(label)
                + '</strong>' + ''.join(fragments) + '</div>'
                for label, fragments in groups.values()]
        addition = '<div class="concentrated-pending"><h3>待核条件</h3>' + ''.join(rows) + '</div>'
        marker = '<section class="trip-corrections">'
        if marker in output:
            output = output.replace(marker, marker + addition, 1)
        else:
            output += marker + '<h2>出发前先核这几条</h2>' + addition + '</section>'
    return output


def media_records_html(records, degradations):
    rows = []
    for record in records:
        # JSON retains exact provider metadata; escaped text cannot inject HTML.
        import json
        rows.append('<div class="media-record"><pre>' + html.escape(
            json.dumps(record, ensure_ascii=False, sort_keys=True)) + '</pre></div>')
    for item in degradations:
        import json
        rows.append('<p class="presentation-degradation">呈现降级：' + html.escape(
            json.dumps(item, ensure_ascii=False, sort_keys=True)) + '</p>')
    return ''.join(rows)
