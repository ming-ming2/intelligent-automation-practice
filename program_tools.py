"""역할 C: Agent가 호출하는 Program 도구 (5주차 교수 코멘트 반영).

4주차까지는 Program(B)이 계산한 수치 전체를 프롬프트에 붙여 넣고 Agent가 읽어서 옮겨 적었다.
그 결과 Agent가 연결·별도 기준을 섞거나 증감 방향을 잘못 쓰는 일이 생겼다(개선 전 일반 사례:
연결 당기순이익 +49.1%인데 보고서 요약에 '순이익 감소').

5주차부터는 Agent가 수치가 필요할 때 아래 도구를 호출하고, 도구는 B의 Program 함수
(change_detector.compare_values, 금액 단위 규칙)로 계산한 값을 그대로 돌려준다.
도구는 LLM을 쓰지 않으며, 같은 입력에는 항상 같은 결과를 낸다.

이 파일에는 Agent 출력이 Program 값과 맞는지 대조하는 검증 함수도 둔다.
검증은 고치지 않고 불일치를 기록해 담당자에게 경고만 한다.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from change_detector import (AMOUNT_THRESHOLD, FINANCIAL_THRESHOLD, UNITS,
                             _number, compare_values)

BASIS = {'CFS': '연결', 'OFS': '별도'}
BASIS_CODE = {'연결': 'CFS', '별도': 'OFS'}
AMOUNT_PATTERN = re.compile(r'([-+]?\d[\d,]*(?:\.\d+)?)\s*(조원|억원|백만원|천원|원)')


def _norm(text: str) -> str:
    return re.sub(r'[\s()（）]', '', text or '').replace('손실', '')


def rate_display(rate) -> str:
    return '비교 불가' if rate is None else f'{float(rate) * 100:+.1f}%'


def direction(rate) -> str:
    if rate is None:
        return '비교 불가'
    return '증가' if rate > 0 else '감소' if rate < 0 else '변화 없음'


def _recompute(item: dict, threshold: Decimal) -> dict:
    """B의 compare_values로 다시 계산한다. B가 이미 비교 불가로 판정한 사유는 그대로 유지한다."""
    result = compare_values(item.get('metric'), item['current'], item['previous'], threshold,
                            item.get('dimensions'), item.get('reason'))
    dims = item.get('dimensions') or {}
    return {
        'metric': item.get('metric'),
        'basis': BASIS.get(dims.get('fs_div'), dims.get('fs_div') or '해당 없음'),
        'current_period': dims.get('current_period'),
        'previous_period': dims.get('previous_period'),
        'current_raw': item['current'].get('raw_value'),
        'previous_raw': item['previous'].get('raw_value'),
        'change_rate': result['change_rate'],
        'change_rate_display': rate_display(result['change_rate']),
        'direction': direction(result['change_rate']),
        'threshold': f"±{float(threshold) * 100:.0f}%",
        'triggered': result['triggered'],
        'status': result['status'],
        'reason': result['reason'],
        'evidence_rcept_no': (item['current'].get('evidence') or {}).get('rcept_no'),
    }


class ProgramData:
    """한 공시에 대한 B의 계산 결과. 도구와 검증 함수가 같은 원본을 본다."""

    def __init__(self, payload: dict):
        triggers = payload.get('triggers') or {}
        self.financial = [_recompute(i, FINANCIAL_THRESHOLD) for i in triggers.get('financial_changes', [])]
        self.amounts = [_recompute(i, AMOUNT_THRESHOLD) for i in triggers.get('amount_changes', [])]

    def find(self, rows: list[dict], metric: str, basis: Optional[str] = None) -> list[dict]:
        key = _norm(metric)
        exact = [r for r in rows if _norm(r['metric']) == key]
        found = exact or [r for r in rows if key and key in _norm(r['metric'])]
        if basis in BASIS_CODE:
            found = [r for r in found if r['basis'] == basis]
        return found

    def index(self) -> str:
        """프롬프트에 넣는 Program 결과 요약. 변화율·증감 방향·기준 판정만 넣고, 원 단위 값·기간은 도구로 조회한다."""
        lines = []
        for label, rows in (('재무', self.financial), ('금액', self.amounts)):
            for r in rows:
                if r['status'] == 'evaluated':
                    judged = f"변화율 {r['change_rate_display']} ({r['direction']}) / " \
                             f"Program 판정: {'기준 초과' if r['triggered'] else '기준 미만'}({r['threshold']})"
                else:
                    judged = f"변화율 비교 불가({r['reason']})"
                lines.append(f"- [{label}] {r['metric']} / {r['basis']} / {judged}")
        return '\n'.join(lines) or '- 없음'


# ---------------------------------------------------------------------------
# Agent 도구
# ---------------------------------------------------------------------------
class MetricQuery(BaseModel):
    metric: str = Field(description='조회할 항목명. 예: 영업이익, 당기순이익, 매출액, 처분금액')
    basis: Optional[str] = Field(default=None, description="'연결' 또는 '별도'. 비우면 둘 다 반환")


class FinancialChangeTool(BaseTool):
    name: str = 'lookup_financial_change'
    description: str = (
        'Program(변화 탐지 모듈)이 공시 재무 데이터로 계산한 전년 대비 변화를 조회한다. '
        '현재값·이전값·기간·변화율·증감 방향·±15% 기준 초과 여부·연결/별도 구분을 돌려준다. '
        '재무 수치나 증감을 쓰기 전에 반드시 이 도구로 확인한다.')
    args_schema: Type[BaseModel] = MetricQuery
    data: ProgramData = Field(exclude=True)

    model_config = {'arbitrary_types_allowed': True}

    def _run(self, metric: str, basis: Optional[str] = None) -> str:
        rows = self.data.find(self.data.financial, metric, basis)
        if not rows:
            return json.dumps({'result': '없음', 'message': f"'{metric}' 항목의 Program 계산 결과가 없습니다. "
                               '수치를 추정하지 말고 확인 필요로 남긴다.'}, ensure_ascii=False)
        return json.dumps(rows, ensure_ascii=False)


class AmountChangeTool(BaseTool):
    name: str = 'lookup_amount_change'
    description: str = (
        'Program이 공시 원문에서 추출한 금액(투자·취득·처분·계약금액 등)과 직전 동일 유형 공시 대비 변화를 조회한다. '
        '변화율·증감 방향·±20% 기준 초과 여부·비교 불가 사유를 돌려준다.')
    args_schema: Type[BaseModel] = MetricQuery
    data: ProgramData = Field(exclude=True)

    model_config = {'arbitrary_types_allowed': True}

    def _run(self, metric: str, basis: Optional[str] = None) -> str:
        rows = self.data.find(self.data.amounts, metric)
        if not rows:
            return json.dumps({'result': '없음', 'message': f"'{metric}' 금액의 Program 추출 결과가 없습니다."},
                              ensure_ascii=False)
        return json.dumps(rows, ensure_ascii=False)


class AmountText(BaseModel):
    text: str = Field(description='원문에 적힌 금액 표기 그대로. 예: 1,856,757,000,000원, 1,300억원')


class ParseAmountTool(BaseTool):
    name: str = 'parse_amount_text'
    description: str = (
        '원문 금액 표기를 Program의 단위 규칙(원·천원·백만원·억원·조원)으로 원 단위 숫자로 바꾼다. '
        '숫자 하나와 단위 하나로 된 표기만 해석하고, 복합 단위(1조 3,000억원)나 단위 없는 숫자는 해석하지 않는다.')
    args_schema: Type[BaseModel] = AmountText

    def _run(self, text: str) -> str:
        match = AMOUNT_PATTERN.fullmatch((text or '').strip())
        if not match:
            return json.dumps({'text': text, 'result': '해석 불가',
                               'message': '단일 숫자+단위 표기가 아니어서 Program 규칙으로 해석하지 않았습니다. '
                                          '원문 표기를 그대로 쓰고 확인 필요로 남긴다.'}, ensure_ascii=False)
        value = _number(match[1]) * UNITS[match[2]]
        return json.dumps({'text': text, 'value_won': format(value, 'f'), 'unit': '원'}, ensure_ascii=False)


def build_tools(payload: dict) -> dict[str, list[BaseTool]]:
    data = ProgramData(payload)
    finance, amount, parse = FinancialChangeTool(data=data), AmountChangeTool(data=data), ParseAmountTool()
    return {'data': data, 'disclosure': [parse], 'change': [finance, amount, parse], 'report': [finance]}


# ---------------------------------------------------------------------------
# 결정적 검증: Agent 출력 vs Program 값 (고치지 않고 경고만)
# ---------------------------------------------------------------------------
def _rate_text(value) -> Optional[str]:
    m = re.search(r'[-+]?\d+(?:\.\d+)?', str(value or ''))
    if not m:
        return None
    number = float(m[0])
    if '%' not in str(value) and abs(number) <= 1.5:   # 0.3 같은 비율 표기
        number *= 100
    return f'{number:+.1f}%'


def _digits(text) -> str:
    return re.sub(r'[^0-9-]', '', str(text or ''))


def apply_program_values(changes: list, data: ProgramData, make=dict) -> list[dict]:
    """CHANGE의 수치 칸은 Program 계산값만 허용한다 (사실 정확도 규칙).
    - 같은 항목·같은 기준(연결/별도)의 Program 결과가 하나로 정해지면: previous·current·change_rate·triggered를
      Program 값으로 채운다. Agent가 다른 값을 썼다면 Program 값으로 바꾸고, Agent가 쓴 값을 기록한다.
    - 연결/별도를 적지 않아 Program 결과가 둘 이상이면: 값을 채우지 않고 기록한다(어느 기준인지 추정하지 않음).
    - Program 결과에 없는 항목이면: Agent 내용을 그대로 두고 'Program 근거 없음'으로 기록한다.
    - description의 증가/감소 표현이 Program 방향과 반대면 문장은 고치지 않고 기록한다.
    - Program이 탐지한 변화 후보(기준 초과 재무 항목)와 공시 금액 항목이 CHANGE에 없으면 Program 값으로 추가하고 기록한다.
      (3주차 설계: 변화 후보 탐지는 Program, 설명은 Agent. Agent가 빠뜨려도 후보가 보고서에서 사라지지 않게)
    changes는 Change 객체(속성) 또는 dict 모두 받는다. 기록 목록을 돌려준다."""
    get = lambda c, k: getattr(c, k, None) if not isinstance(c, dict) else c.get(k)

    def put(c, k, v):
        if isinstance(c, dict):
            c[k] = v
        else:
            setattr(c, k, v)

    notes = []
    for change in changes or []:
        cid, metric, basis = get(change, 'change_id'), get(change, 'metric') or '', get(change, 'basis')
        rows = data.find(data.financial + data.amounts, metric, basis if basis in BASIS_CODE else None)
        if not rows:
            notes.append({'check': '수치 보정', 'item': cid, 'problem': f'{metric}: Program 계산 결과에 없는 항목 (수치 검증 불가)'})
            continue
        if len({r['basis'] for r in rows}) > 1 or len(rows) > 1:
            notes.append({'check': '수치 보정', 'item': cid,
                          'problem': f'{metric}: 연결/별도 기준이 없어 Program 값을 하나로 정할 수 없음 (값을 채우지 않음)',
                          'program': [f"{r['basis']} {r['change_rate_display']}" for r in rows]})
            continue
        row = rows[0]
        program = {'previous': row['previous_raw'], 'current': row['current_raw'],
                   'change_rate': row['change_rate_display'], 'triggered': row['triggered']}
        replaced = {}
        for key, value in program.items():
            written = get(change, key)
            same = (written in (None, '') or
                    (key == 'change_rate' and _rate_text(written) == value) or
                    (key in ('previous', 'current') and _digits(written) == _digits(value)) or
                    written == value)
            if not same:
                replaced[key] = written
            put(change, key, value)
        if basis not in BASIS_CODE and row['basis'] in BASIS_CODE:
            put(change, 'basis', row['basis'])
        if replaced:
            notes.append({'check': '수치 보정', 'item': cid,
                          'problem': f"{metric}({row['basis']}): Agent가 쓴 값이 Program 값과 달라 Program 값으로 바꿈",
                          'agent': replaced, 'program': program})
        written_dir = _first_direction(get(change, 'description') or '')
        if written_dir and row['direction'] in ('증가', '감소') and written_dir != row['direction']:
            notes.append({'check': '증감 방향 대조', 'item': cid,
                          'problem': f"{metric}({row['basis']}) 설명에 \"{written_dir}\"으로 기재",
                          'program': f"{row['change_rate_display']} ({row['direction']})"})
    covered = set()
    for change in changes or []:
        for r in data.find(data.financial + data.amounts, get(change, 'metric') or '', get(change, 'basis')):
            covered.add((r['metric'], r['basis']))
    required = [r for r in data.financial if r['triggered']] + list(data.amounts)
    for n, row in enumerate([r for r in required if (r['metric'], r['basis']) not in covered], start=1):
        if row['status'] == 'evaluated':
            desc = f"Program 탐지: {row['change_rate_display']} {row['direction']} " \
                   f"({'기준 초과' if row['triggered'] else '기준 미만'}, {row['threshold']}). Agent 설명 누락으로 Program이 추가"
        else:
            desc = f"Program 탐지: 비교 불가({row['reason']}). Agent 설명 누락으로 Program이 추가"
        changes.append(make(
            change_id=f'P{n}', metric=row['metric'],
            basis=row['basis'] if row['basis'] in BASIS_CODE else None,
            previous=row['previous_raw'] or None, current=row['current_raw'] or None,
            change_rate=row['change_rate_display'], triggered=row['triggered'],
            description=desc, related_fact_ids=[]))
        notes.append({'check': '수치 보정', 'item': f'P{n}',
                      'problem': f"{row['metric']}({row['basis']}): Program 변화 후보가 CHANGE에 없어 Program이 추가"})
    return notes


def check_change_rates(analysis: Optional[dict], data: ProgramData) -> list[dict]:
    """CHANGE에 적힌 변화율이 Program 계산값과 같은지 대조한다."""
    problems = []
    if not isinstance(analysis, dict):
        return problems
    for change in analysis.get('changes', []):
        rows = data.find(data.financial + data.amounts, change.get('metric', ''), change.get('basis'))
        if not rows:
            continue
        written = _rate_text(change.get('change_rate'))
        allowed = {r['change_rate_display'] for r in rows}
        if written is None:
            if '비교 불가' in allowed:
                continue
            problems.append({'check': '변화율 대조', 'item': change.get('change_id'),
                             'problem': f"{change.get('metric')} 변화율이 비어 있음", 'program': sorted(allowed)})
        elif written not in allowed:
            problems.append({'check': '변화율 대조', 'item': change.get('change_id'),
                             'problem': f"{change.get('metric')} 변화율 {change.get('change_rate')}이 Program 값과 다름",
                             'program': sorted(allowed)})
    return problems


DIRECTION_TERMS = {
    '영업이익': '영업이익',
    '당기순이익': '당기순이익(손실)',
    '순이익': '당기순이익(손실)',
    '매출': '매출액',
}
UP = ('증가', '상승', '개선', '확대')
DOWN = ('감소', '하락', '악화', '축소')


def _first_direction(text: str) -> Optional[str]:
    hits = [(text.find(w), '증가') for w in UP if w in text] + [(text.find(w), '감소') for w in DOWN if w in text]
    return min(hits)[1] if hits else None


def check_report_directions(report: str, data: ProgramData) -> list[dict]:
    """보고서에 적힌 증감 방향이 Program 계산 결과와 반대인지 찾는다.
    쉼표·마침표로 나눈 구절마다, 항목명 바로 뒤에 처음 나오는 증감 표현을 그 항목의 방향으로 본다.
    구절에 '별도'가 있으면 별도 기준, 없으면 연결 기준으로 대조한다. 증감 표현이 없으면 판단하지 않는다."""
    problems, seen = [], set()
    for clause in re.split(r'[\n.。,，;]', report or ''):
        basis = '별도' if '별도' in clause else '연결'
        for term, metric in DIRECTION_TERMS.items():
            start = 0
            while (pos := clause.find(term, start)) != -1:
                start = pos + len(term)
                before = clause[max(0, pos - 6):pos]
                if term == '순이익' and (before.endswith('당기') or '법인세차감전' in before):
                    continue
                if term == '매출' and clause.startswith('채권', start):
                    continue
                written = _first_direction(clause[start:])
                rows = [r for r in data.find(data.financial, metric, basis) if r['status'] == 'evaluated']
                if not written or not rows:
                    continue
                actual = rows[0]['direction']
                key = (clause.strip(), metric)
                if actual in ('증가', '감소') and actual != written and key not in seen:
                    seen.add(key)
                    problems.append({'check': '증감 방향 대조',
                                     'problem': f'보고서에 {basis} {metric} "{written}"으로 기재',
                                     'program': f"{basis} {metric} {rows[0]['change_rate_display']} ({actual})",
                                     'sentence': clause.strip()[:200]})
    return problems
