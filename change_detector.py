import argparse
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
from normalizer import normalize_xml, summarize_records

ROOT = Path(__file__).resolve().parent
AMOUNT_THRESHOLD = Decimal('0.20')
FINANCIAL_THRESHOLD = Decimal('0.15')
KEYWORDS = ('신규사업', '중단', '양수', '분할', '합병')
KEYWORD_EVIDENCE_LIMIT = 3
AMOUNT_LABELS = ('투자금액', '취득금액', '처분금액', '양수금액', '양도금액', '계약금액')

# 키워드는 변화 후보를 찾기 위한 신호다. 명백한 회계 항목명과 회사 연혁은
# 실제 사건 후보로 보내지 않고 제외 건수만 needs_verification에 남긴다.
CURRENT_EVENT_PATTERNS = (
    r'(?:영업|사업|생산|판매|서비스|계약|거래)\s*(?:을|를)?\s*중단(?:하|했|하기로|할|을\s*결정)',
    r'(?:분할|합병|양수)\s*(?:을|를)?\s*(?:결정|승인|계약|완료|추진|진행)',
    r'(?:분할|합병|양수)(?:하였|했|한다|하기로)',
)
HISTORY_SECTION_PATTERN = re.compile(r'(?:일반사항|회사(?:의)?\s*개요|연혁|납입자본)')
HISTORY_CONTEXT_PATTERN = re.compile(
    r'(?:설립|인적\s*분할|분할\s*전|회사\s*분할|분할회사|유상증자|전환권행사)'
)
UNITS = {'원': 1, '천원': 1000, '백만원': 1000000, '억원': 100000000, '조원': 1000000000000}
CORRECTION = re.compile(r'^\s*(?:\[(?:기재정정|첨부정정|정정)\]\s*)+')


def _number(value):
    try:
        number = Decimal(str(value).strip().replace(',', ''))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _value(number=None, raw=None, evidence=None, unit=None, currency=None):
    return {'value': format(number, 'f') if number is not None else None,
            'raw_value': raw, 'unit': unit, 'currency': currency, 'evidence': evidence}


def _path(path):
    if path is None:
        return None
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def _evidence(receipt, record):
    # 단축 본문에 없는 근거도 발견한 문장을 잘라내지 않고 전달한다.
    return {'kind': 'document', 'rcept_no': receipt, 'document_path': _path(record['path']),
            'section': record['section'], 'quote': record['text'], 'db_row': None, 'field': None}


def _issue(code, field, message):
    return {'code': code, 'field': field, 'message': message}


def compare_values(metric, current, previous, threshold, dimensions=None, reason=None):
    """반올림 전 Decimal로 판정한다. 누락·0 분모는 false 대신 null이다."""
    cur, prev = _number(current['value']), _number(previous['value'])
    if reason is None:
        if cur is None:
            reason = 'CURRENT_VALUE_MISSING'
        elif prev is None:
            reason = 'PREVIOUS_VALUE_MISSING'
        elif not current['unit'] or not current['currency']:
            reason = 'UNIT_UNKNOWN'
        elif (current['unit'], current['currency']) != (previous['unit'], previous['currency']):
            reason = 'COMPARISON_MISMATCH'
        elif prev == 0:
            reason = 'PREVIOUS_VALUE_ZERO'
    rate = None if reason else (cur - prev) / abs(prev)
    return {'metric': metric, 'status': 'not_comparable' if reason else 'evaluated',
            'triggered': abs(rate) >= threshold if rate is not None else None,
            'reason': reason, 'current': current, 'previous': previous,
            'change_rate': float(round(rate, 6)) if rate is not None else None,
            'threshold': float(threshold), 'dimensions': dimensions}


def extract_amounts(norm, receipt):
    """항목에 붙은 단일 금액만 허용한다. 미지원 표·복수 금액은 보류한다."""
    found, issues = {}, []
    pattern = r'([-+]?\d[\d,]*(?:\.\d+)?)\s*(조원|억원|백만원|천원|원)'
    for record in norm['records']:
        for label in AMOUNT_LABELS:
            if label not in record['text']:
                continue
            cells = record.get('cells')
            raw = record['text'].split(label, 1)[1].strip(' :|')
            if cells:
                indices = [i for i, cell in enumerate(cells) if cell.strip() == label]
                raw = cells[indices[0] + 1].strip() if len(indices) == 1 and indices[0] + 1 < len(cells) else ''
                if indices and any(c.strip() for c in cells[indices[0] + 2:]):
                    raw = ''  # 정정 전후 등 복수 값 열의 의미를 추측하지 않는다.
            match = re.fullmatch(pattern, raw)
            valid = match is not None and not record.get('ambiguous_table', False)
            value = _value(_number(match[1]) * UNITS[match[2]], raw,
                           _evidence(receipt, record), '원', 'KRW') if valid else _value(
                               raw=raw, evidence=_evidence(receipt, record))
            if label in found:
                value = _value(raw=raw, evidence=_evidence(receipt, record))
                valid = False  # 같은 값이어도 별개 투자 건일 수 있다.
            found[label] = value
            if not valid:
                issues.append(_issue('AMOUNT_UNRESOLVED', 'triggers.amount_changes',
                                     f'{receipt}: {label}의 단위·값·표 구조가 불명확하거나 여러 번 등장합니다.'))
    return found, issues


def _report_type(name):
    return re.sub(r'\s*\(\d{4}\.\d{2}\)\s*$', '', CORRECTION.sub('', name)).strip()


def _years_before(date, years):
    day = datetime.strptime(date, '%Y%m%d')
    try:
        return day.replace(year=day.year - years).strftime('%Y%m%d')
    except ValueError:
        return day.replace(year=day.year - years, day=28).strftime('%Y%m%d')


def find_past_cases(current, disclosures, cursors=()):
    candidates = sorted([d for d in disclosures
                         if d['corp_code'] == current['corp_code']
                         and d['rcept_dt'] < current['rcept_dt']
                         and _report_type(d['report_nm']) == _report_type(current['report_nm'])],
                        key=lambda d: (d['rcept_dt'], d['rcept_no']), reverse=True)
    # 원공시 연결이 없는 정정공시는 고유 사건 수에 포함하지 않는다.
    unique = {d['rcept_no']: d for d in candidates if not CORRECTION.match(d['report_nm'])}
    count = sum(d['rcept_dt'] >= _years_before(current['rcept_dt'], 3) for d in unique.values())
    years = 3 if count >= 2 else 5
    start = _years_before(current['rcept_dt'], years)
    complete = any(c['corp_code'] == current['corp_code'] and c['pblntf_ty'] == current['pblntf_ty']
                   and c['scope_start'] <= start and c['checked_through'] >= current['rcept_dt'] for c in cursors)
    history = {'match_rule': 'same_corp_and_normalized_report_type_unique_events',
               'search_window_years': years, 'cases_in_three_years': count, 'requested_start': start,
               'end_exclusive': current['rcept_dt'], 'coverage_status': 'complete' if complete else 'unknown',
               'coverage_note': None if complete else '검색 기간 전체의 공시 목록 수집을 확인할 수 없습니다.'}
    return [d for d in unique.values() if d['rcept_dt'] >= start], history, candidates


def _periods_match(current, previous, year):
    def dates(text):
        return re.findall(r'(\d{4})[.\-/](\d{2})[.\-/](\d{2})', text or '')
    a, b = dates(current), dates(previous)
    return bool(a) and len(a) == len(b) and all(
        int(x[0]) == year and int(y[0]) == year - 1 and x[1:] == y[1:]
        for x, y in zip(a, b))


def detect_financial_changes(current, financials):
    """현재 접수번호의 계정만 비교한다. 타 공시 재무를 자동 대체하지 않는다."""
    results, seen = [], set()
    for row in financials:
        if row['corp_code'] != current['corp_code'] or row['rcept_no'] != current['rcept_no']:
            continue
        raw = json.loads(row['raw_json'])
        # 분기·반기 손익: OpenDART는 thstrm_amount에 3개월치, thstrm_add_amount에 누적치를 준다.
        # thstrm_dt는 누적 기간(01.01~)으로 표기되므로 누적치를 써야 기간 표기와 값이 일치한다.
        cumulative = (row['reprt_code'] != '11011' and row['sj_div'] in {'IS', 'CIS'}
                      and _number(raw.get('thstrm_add_amount')) is not None
                      and _number(raw.get('frmtrm_add_amount')) is not None)
        fields = (('thstrm_add_amount', 'frmtrm_add_amount') if cumulative
                  else ('thstrm_amount', 'frmtrm_amount'))
        source = raw if cumulative else row
        key = (row['fs_div'], row['sj_div'], row['account_nm'], source[fields[0]], source[fields[1]])
        if key in seen:  # OpenDART가 같은 계정을 중복 반환하는 경우
            continue
        seen.add(key)
        current_period, previous_period = raw.get('thstrm_dt'), raw.get('frmtrm_dt')
        year = int(row['bsns_year'])
        reason = None if _periods_match(current_period, previous_period, year) else 'PERIOD_UNVERIFIED'
        if row['currency'] != 'KRW':
            reason = 'UNIT_UNKNOWN'
        if row['fs_div'] not in {'CFS', 'OFS'} or not row['sj_div']:
            reason = 'COMPARISON_MISMATCH'
        dates = re.findall(r'(\d{4})[.\-/](\d{2})[.\-/](\d{2})', current_period or '')
        if dates and ''.join(dates[-1]) > current['rcept_dt']:
            reason = 'PERIOD_UNVERIFIED'
        basis = ('instant' if row['sj_div'] == 'BS' else 'annual' if row['reprt_code'] == '11011'
                 else 'cumulative' if len(dates) == 2 and dates[0][1:] == ('01', '01')
                 and dates[-1][1] != '03' else 'quarter')
        dims = {'fs_div': row['fs_div'], 'sj_div': row['sj_div'], 'reprt_code': row['reprt_code'],
                'current_year': str(year), 'previous_year': str(year - 1),
                'current_period': current_period, 'previous_period': previous_period,
                'period_basis': basis if dates else None}
        def value(field):
            evidence = {'kind': 'financial_account', 'rcept_no': row['rcept_no'], 'document_path': None,
                        'section': None, 'quote': None, 'field': field,
                        'db_row': {'table': 'financial_accounts', **{k: row[k] for k in
                                   ('corp_code', 'bsns_year', 'reprt_code', 'row_no')}}}
            return _value(_number(source[field]), source[field], evidence, '원', row['currency'])
        results.append(compare_values(row['account_nm'], value(fields[0]), value(fields[1]),
                                      FINANCIAL_THRESHOLD, dims, reason))
    return results



def _keyword_pattern(word):
    return re.compile(r'\s*'.join(map(re.escape, word)))


def _has_current_event_language(text):
    return any(re.search(pattern, text) for pattern in CURRENT_EVENT_PATTERNS)


def _keyword_exclusion_reason(record, word, match, current_year):
    """명백한 비사건 키워드만 Program 단계에서 제외한다.

    - '중단'이 재무표의 계정명/열 이름에 쓰인 경우
    - '분할/합병'이 과거 설립·연혁 또는 납입자본 설명에 쓰인 경우

    부정문과 일반 사업 설명은 여기서 버리지 않는다. Task 1이 문맥을 보고
    실제 사건 여부를 false/null로 판정할 수 있도록 근거를 남긴다.
    """
    text = record.get('text') or ''
    section = record.get('section') or ''
    cells = record.get('cells') or []
    left, right = max(0, match.start() - 180), min(len(text), match.end() + 180)
    context = text[left:right]

    if word == '중단':
        # 실제로 "사업을 중단하였다/중단하기로 결정" 같은 문장이 있으면
        # 재무 주석 안에 있더라도 사건 문맥을 보존한다.
        if _has_current_event_language(context):
            return None
        # 재무제표·주석의 표 행에서 '중단영업 당기순손실',
        # '중단사업손익', '중단영업 매출액'처럼 계정/열 이름으로 쓰인 경우.
        if cells and re.search(
                r'중단\s*(?:영업|사업)?[^|]{0,24}'
                r'(?:손익|손실|이익|매출|비용|현금흐름|주당|법인세|리스|퇴직|자산|금융|계속영업|포함)',
                text):
            return '회계 항목명'
        # '| 계속영업 | 중단영업' 같은 짧은 표 머리글도 계정 문맥이다.
        if cells and re.search(r'중단\s*영업', text) and len(text) <= 120:
            return '회계 항목명'
        # 재무 주석의 짧은 섹션 제목 자체도 사건이 아니다.
        if not cells and '재무' in section and re.search(r'중단\s*영업', text) and len(text) <= 80:
            return '회계 항목명'

    if word in {'분할', '합병'}:
        years = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})(?=년|\D)', context)]
        has_old_year = any(y < current_year for y in years)
        # "2002년 ... 인적분할하여 설립" 등 회사 설립·연혁 설명.
        if HISTORY_CONTEXT_PATTERN.search(context) and (
                '설립' in context or has_old_year or HISTORY_SECTION_PATTERN.search(section)):
            return '과거 회사 연혁'
        # 납입자본 표의 과거 회사분할·합병/유상증자 이력.
        if '납입자본' in section:
            all_years = [int(y) for y in re.findall(r'(?<!\d)(20\d{2})(?=년|\D)', text)]
            if any(y < current_year for y in all_years):
                return '과거 회사 연혁'

    return None



def detect_changes(current, norm, past_disclosures=(), financials=(), cursors=(), *, data_origin):
    """순수 Python 입력 → v1 JSON dict. 과거 공시는 norm/source를 포함한다."""
    if data_origin not in {'dart', 'synthetic', 'mixed'}:
        raise ValueError('data_origin은 dart/synthetic/mixed 중 하나여야 합니다.')
    if data_origin == 'dart' and any(not re.fullmatch(r'\d{14}', d['rcept_no'] or '')
                                   for d in [current, *past_disclosures, *financials]):
        raise ValueError('가상 또는 누락 식별자를 실제 DART 데이터로 표시할 수 없습니다.')
    if data_origin == 'synthetic' and not current['rcept_no'].startswith('MOCK-'):
        raise ValueError('개발용 현재 접수번호는 MOCK-로 시작해야 합니다.')
    issues = []
    if norm['normalization']['status'] != 'ok':
        code = 'XML_PARSE_ERROR' if norm['normalization']['status'] == 'error' else 'DOCUMENT_MISSING'
        issues.append(_issue(code, 'normalized_text', '원문이 없거나 정상적으로 읽히지 않았습니다.'))
    past, history, candidates = find_past_cases(current, past_disclosures, cursors)
    if history['coverage_status'] != 'complete':
        issues.append(_issue('HISTORY_COVERAGE_UNKNOWN', 'history_search', history['coverage_note']))
    if not past:
        issues.append(_issue('PAST_CASES_MISSING', 'past_cases', '확인 가능한 과거 동일 유형 사례가 없습니다.'))
    if CORRECTION.match(current['report_nm']) or any(CORRECTION.match(d['report_nm']) for d in candidates):
        issues.append(_issue('CORRECTION_LINK_UNCONFIRMED', 'past_cases',
                             '정정공시를 사례 수에서 제외했습니다. 원공시 연결과 비교 대상을 확인하세요.'))
    amounts, warnings = extract_amounts(norm, current['rcept_no'])
    issues.extend(warnings)
    # 직전 공시가 읽히지 않아도 오래된 금액으로 대체하지 않는다.
    previous = candidates[0] if candidates else None
    old, warnings = extract_amounts(previous['norm'], previous['rcept_no']) if previous else ({}, [])
    issues.extend(warnings)
    if previous and previous['norm']['normalization']['status'] != 'ok':
        issues.append(_issue('DOCUMENT_MISSING', 'triggers.amount_changes', '직전 공시 원문을 읽지 못했습니다.'))
    reason = 'CORRECTION_LINK_UNCONFIRMED' if CORRECTION.match(current['report_nm']) or (
        previous and CORRECTION.match(previous['report_nm'])) else None
    changes = [compare_values(metric, amounts.get(metric, _value()), old.get(metric, _value()),
                              AMOUNT_THRESHOLD, reason=reason) for metric in sorted(amounts.keys() | old.keys())]
    if not amounts:
        issues.append(_issue('CURRENT_VALUE_MISSING', 'triggers.amount_changes', '현재 공시에서 비교할 금액 항목을 찾지 못했습니다.'))
    financial = detect_financial_changes(current, financials)
    if not financial:
        issues.append(_issue('FINANCIAL_DATA_MISSING', 'triggers.financial_changes',
                             '현재 접수번호에 직접 연결된 재무 계정이 없습니다.'))
    for field, comparisons in [('amount_changes', changes), ('financial_changes', financial)]:
        for item in comparisons:
            if item['reason']:
                issues.append(_issue(item['reason'], f'triggers.{field}', f"{item['metric']}: 비교 불가 ({item['reason']})"))
            prev, cur = _number(item['previous']['value']), _number(item['current']['value'])
            if prev is not None and prev < 0:
                issues.append(_issue('NEGATIVE_BASE', f'triggers.{field}', f"{item['metric']}: 음수 기저의 변화율 해석이 필요합니다."))
            if prev is not None and cur is not None and prev * cur < 0:
                issues.append(_issue('SIGN_CHANGE', f'triggers.{field}', f"{item['metric']}: 부호가 전환되었습니다."))
    keywords, hits, excluded = [], {}, {}
    current_year = int(current['rcept_dt'][:4])
    for record in norm['records']:
        for word in KEYWORDS:
            matches = list(_keyword_pattern(word).finditer(record['text']))
            if not matches:
                continue
            included = False
            for match in matches:
                reason = _keyword_exclusion_reason(record, word, match, current_year)
                if reason:
                    key = (word, reason)
                    excluded[key] = excluded.get(key, 0) + 1
                else:
                    included = True
            # 같은 record에 포함 가능한 등장 문맥이 하나라도 있으면 근거는 한 번만 전달한다.
            if included:
                hits[word] = hits.get(word, 0) + 1
                if hits[word] <= KEYWORD_EVIDENCE_LIMIT:
                    keywords.append({'keyword': word, 'evidence': _evidence(current['rcept_no'], record)})
    for (word, reason), count in sorted(excluded.items()):
        issues.append(_issue('KEYWORD_EXCLUDED', 'triggers.keywords',
                             f"'{word}' 등장 {count}건을 {reason}으로 제외했습니다."))
    for word, count in hits.items():
        if count > KEYWORD_EVIDENCE_LIMIT:
            issues.append(_issue('KEYWORD_EVIDENCE_TRUNCATED', 'triggers.keywords',
                                 f"'{word}' 유효 후보 {count}건 중 앞 {KEYWORD_EVIDENCE_LIMIT}건만 전달했습니다."))
    issues = list({(i['code'], i['field'], i['message']): i for i in issues}.values())  # 동일 경고 중복 제거
    return {'schema_version': '1.0.0', 'data_origin': data_origin,
            **{k: current[k] for k in ('rcept_no', 'corp_code', 'corp_name', 'report_nm', 'rcept_dt')},
            'normalized_text': norm['normalized_text'], 'normalization': norm['normalization'],
            'source': current['source'], 'triggers': {'amount_changes': changes,
            'financial_changes': financial, 'keywords': keywords},
            'past_cases': [{**{k: d[k] for k in ('rcept_no', 'report_nm', 'rcept_dt', 'source')},
                            'excerpt': d['norm']['normalized_text'][:500] or None} for d in past],
            'history_search': history, 'needs_verification': issues}


def build_payload(rcept_no, db_path=ROOT / 'data/dart.db', *, data_origin):
    """A의 DB를 읽기 전용으로 연결한다. API 키와 API 호출은 필요하지 않다."""
    path = Path(db_path)
    path = path if path.is_absolute() else ROOT / path
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM disclosures WHERE rcept_no=?', (rcept_no,)).fetchone()
        if row is None:
            raise ValueError(f'접수번호 {rcept_no}가 DB에 없습니다.')
        current = dict(row)

        def load(disclosure):
            files = db.execute('SELECT files_json FROM documents WHERE rcept_no=?',
                               (disclosure['rcept_no'],)).fetchone()
            paths = [Path(p) if Path(p).is_absolute() else ROOT / p
                     for p in json.loads(files['files_json']) if Path(p).suffix.lower() == '.xml'] if files else []
            parts = [normalize_xml(p) for p in paths] if disclosure['document_status'] == 'downloaded' else []
            statuses = [p['normalization']['status'] for p in parts]
            status = 'error' if 'error' in statuses else 'missing' if not parts or 'missing' in statuses else 'ok'
            norm = summarize_records([r for p in parts for r in p['records']], status=status)
            return {**disclosure, 'norm': norm, 'source': {'source_url': disclosure['source_url'],
                    'document_paths': [_path(p) for p in paths if p.is_file()]}}

        current = load(current)
        rows = [dict(r) for r in db.execute('SELECT * FROM disclosures WHERE corp_code=? AND rcept_dt<?',
                                            (current['corp_code'], current['rcept_dt']))]
        past = [load(r) for r in rows if _report_type(r['report_nm']) == _report_type(current['report_nm'])]
        financials = [dict(r) for r in db.execute('SELECT * FROM financial_accounts WHERE corp_code=? AND rcept_no=?',
                                                 (current['corp_code'], rcept_no))]
        cursors = [dict(r) for r in db.execute('SELECT * FROM list_cursors WHERE corp_code=?', (current['corp_code'],))]
        return detect_changes(current, current['norm'], past, financials, cursors, data_origin=data_origin)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='역할 B: 로컬 공시 정규화·변화 탐지')
    parser.add_argument('rcept_no')
    parser.add_argument('--db', default=str(ROOT / 'data/dart.db'))
    parser.add_argument('--data-origin', required=True, choices=['dart', 'synthetic', 'mixed'])
    args = parser.parse_args()
    print(json.dumps(build_payload(args.rcept_no, args.db, data_origin=args.data_origin),
                     ensure_ascii=False, indent=2, allow_nan=False))
