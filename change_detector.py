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
AMOUNT_LABELS = ('투자금액', '취득금액', '처분금액', '양수금액', '양도금액', '계약금액')
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
    results = []
    for row in financials:
        if row['corp_code'] != current['corp_code'] or row['rcept_no'] != current['rcept_no']:
            continue
        raw = json.loads(row['raw_json'])
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
            return _value(_number(row[field]), row[field], evidence, '원', row['currency'])
        results.append(compare_values(row['account_nm'], value('thstrm_amount'), value('frmtrm_amount'),
                                      FINANCIAL_THRESHOLD, dims, reason))
    return results


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
    keywords = [{'keyword': word, 'evidence': _evidence(current['rcept_no'], record)}
                for record in norm['records'] for word in KEYWORDS
                if re.search(r'\s*'.join(map(re.escape, word)), record['text'])]
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
