from pathlib import Path
import re
from lxml import etree

MAX_CHARS = 15000
SECTION_WORDS = ('사업의 내용', '주요 변동', '신규사업', '투자', '취득', '처분', '양수', '합병', '분할', '중단')
TITLES = {'TITLE', 'SUBTITLE', 'H1', 'H2', 'H3'}
TEXTS = {'P', 'PARA', 'LI', 'ITEM'}
CELLS = {'TD', 'TH', 'TE', 'TU', 'CELL'}


def _tag(node):
    return etree.QName(node).localname.upper() if isinstance(node.tag, str) else ''


def _text(node):
    return re.sub(r'\s+', ' ', ''.join(node.itertext())).strip()


def summarize_records(records, max_chars=MAX_CHARS, status='ok'):
    """여러 XML도 전체 기록을 합친 뒤 한 번만 길이를 제한한다."""
    if not 1 <= max_chars <= MAX_CHARS:
        raise ValueError('max_chars는 1~15000이어야 합니다.')
    full = '\n'.join(r['text'] for r in records)
    selected = records
    if len(full) > max_chars:
        selected = [r for r in records if any(
            word in ((r['section'] or '') + r['text']) for word in SECTION_WORDS)] or records
    parts, sections, remaining = [], [], max_chars
    for record in selected:
        if remaining <= 0:
            break
        part = record['text'][:remaining]
        parts.append(part)
        if record['section'] and record['section'] not in sections:
            sections.append(record['section'])
        remaining -= len(part) + 1
    text = '\n'.join(parts)
    return {'full_text': full, 'normalized_text': text, 'records': records,
            'normalization': {'status': status, 'original_length': len(full),
                              'truncated': text != full, 'selected_sections': sections}}


def normalize_xml(source, max_chars=MAX_CHARS):
    """경로 또는 XML bytes → 전체 텍스트, 전달용 텍스트, 위치·표 기록.

    손상 XML을 조용히 복구하지 않는다. 병합 셀은 표시해 금액 추출 시 보류한다.
    """
    path = None if isinstance(source, bytes) else Path(source)
    try:
        content = source if path is None else path.read_bytes()
        root = etree.fromstring(content, etree.XMLParser(
            recover=False, no_network=True, resolve_entities=False))
        if root is None or any(isinstance(n, etree._Entity) for n in root.iter()):
            raise ValueError('해석되지 않은 엔티티')
    except FileNotFoundError:
        return summarize_records([], max_chars, 'missing')
    except (OSError, etree.XMLSyntaxError, ValueError):
        return summarize_records([], max_chars, 'error')
    records = []

    def walk(node, section=None):
        tag = _tag(node)
        if not tag:
            return
        if tag == 'TR':
            cells = [n for n in node if _tag(n) in CELLS]
            texts = [_text(n) for n in cells]  # 빈 셀도 보존
            ambiguous = any(n.get('COLSPAN', n.get('colspan', '1')) != '1'
                            or n.get('ROWSPAN', n.get('rowspan', '1')) != '1'
                            for n in cells) or any(_tag(n) == 'TABLE' for n in node.iter())
            text = ' | '.join(texts)
            if text:
                records.append({'text': text, 'section': section, 'path': str(path) if path else None,
                                'cells': texts, 'ambiguous_table': ambiguous})
            return
        if tag in TITLES | TEXTS and not any(_tag(n) == 'TABLE' for n in node.iterdescendants()):
            text = _text(node)
            if text:
                records.append({'text': text, 'section': section, 'path': str(path) if path else None,
                                'cells': None, 'ambiguous_table': False})
            return
        # 제목 범위는 부모 안에 한정하며 하위 섹션에는 상위 제목을 보존한다.
        active = section
        for child in node:
            if _tag(child) in TITLES:
                active = ' > '.join(filter(None, [section, _text(child)]))
            walk(child, active)

    walk(root)
    if not records and _text(root):
        records.append({'text': _text(root), 'section': None, 'path': str(path) if path else None,
                        'cells': None, 'ambiguous_table': False})
    return summarize_records(records, max_chars, 'ok' if records else 'error')
