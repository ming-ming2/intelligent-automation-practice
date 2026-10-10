"""역할 C: CrewAI Agent·Task 정의.

3주차 설계(기획안 7페이지)를 그대로 옮긴다.
  Agent① Disclosure Analysis Agent  → Task1 공시 핵심 사실 추출 (FACT)
  Agent② Business Change Analyst    → Task2 변화 맥락·영향 분석 (CHANGE / INTERPRETATION / NEEDS_VERIFICATION)
  Agent③ Intelligence Report Agent  → Task3 검토용 보고서 초안

수치 계산·변화 탐지는 B(Program)가 끝낸 상태로 들어온다. Agent는 계산하지 않고 해석만 한다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

# CrewAI는 import 시점에 저장 경로를 정하므로, 반드시 import 전에 설정한다. (README 규칙)
load_dotenv(ROOT / '.env', override=False)
os.environ.setdefault('CREWAI_STORAGE_DIR', str(ROOT / '.cache' / 'crewai'))
os.environ.setdefault('OTEL_SDK_DISABLED', 'true')
os.environ.setdefault('CREWAI_TELEMETRY_DISABLED', 'true')

from crewai import Agent, Crew, LLM, Process, Task  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402


# ---------------------------------------------------------------------------
# Task 출력 스키마: FACT / CHANGE / INTERPRETATION / NEEDS_VERIFICATION 구분을 형식으로 강제
# ---------------------------------------------------------------------------
class Fact(BaseModel):
    fact_id: str = Field(description='F1, F2 ... 순번')
    statement: str = Field(description='원문으로 확인되는 사실 한 문장. 해석·평가 표현 금지')
    category: Literal['투자', '계약', '인수·매각', '지배구조·자회사', '자금조달', '재무', '사업변경', '기타']
    amount_raw: Optional[str] = Field(default=None, description='원문 표기 그대로의 금액 (예: 1,300억원). 없으면 null')
    date: Optional[str] = Field(default=None, description='원문에 적힌 일자. 없으면 null')
    evidence_quote: str = Field(description='근거가 되는 원문 문장을 그대로 복사. 요약·의역 금지')
    evidence_rcept_no: str = Field(description='근거가 속한 공시의 접수번호')


class KeywordCheck(BaseModel):
    keyword: str
    quote: str = Field(description='키워드가 등장한 원문 문장 그대로')
    is_actual_event: Optional[bool] = Field(
        default=None, description='문맥상 실제 사건이면 true, 부정문·가정이면 false, 판단 불가면 null')
    note: str = ''


class FactExtraction(BaseModel):
    rcept_no: str
    corp_name: str
    report_nm: str
    facts: list[Fact] = Field(default_factory=list)
    keyword_checks: list[KeywordCheck] = Field(default_factory=list)
    unverifiable: list[str] = Field(
        default_factory=list, description='원문만으로 확인할 수 없어 사실로 쓰지 않은 항목')


class Change(BaseModel):
    change_id: str = Field(description='C1, C2 ... 순번')
    metric: str = Field(description='비교 항목명 (Program 계산 결과의 metric)')
    previous: Optional[str] = Field(default=None, description='이전 값 (원문 표기)')
    current: Optional[str] = Field(default=None, description='현재 값 (원문 표기)')
    change_rate: Optional[str] = Field(default=None, description='Program이 계산한 변화율 그대로 (예: +30.0%). 직접 계산 금지')
    triggered: Optional[bool] = Field(default=None, description='Program 판정 그대로')
    description: str = Field(description='무엇이 어떻게 달라졌는지 사실 기반 설명')
    related_fact_ids: list[str] = Field(default_factory=list)


class Interpretation(BaseModel):
    statement: str = Field(description='삼성전자 DX 생활가전·전자 사업 관점의 해석. 추정임을 드러내는 표현 사용')
    impact_area: Literal['사업', '재무', '경쟁환경']
    based_on: list[str] = Field(description='근거가 된 fact_id 또는 change_id')
    confidence: Literal['낮음', '중간', '높음']


class VerificationItem(BaseModel):
    code: str = Field(description='Program 사유 코드 그대로, 또는 Agent가 추가한 경우 AGENT_CHECK')
    field: Optional[str] = None
    item: str = Field(description='무엇을 확인해야 하는지')
    why: str = Field(description='왜 공시만으로 확정할 수 없는지')
    origin: Literal['program', 'agent']


class ChangeAnalysis(BaseModel):
    rcept_no: str
    changes: list[Change] = Field(default_factory=list)
    interpretations: list[Interpretation] = Field(default_factory=list)
    needs_verification: list[VerificationItem] = Field(default_factory=list)
    follow_up_checks: list[str] = Field(
        default_factory=list, description='전략기획 담당자가 추가로 확인할 질문')


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
def make_llm() -> LLM:
    return LLM(model=os.getenv('CREWAI_MODEL', 'openai/gpt-4o-mini'), temperature=0.1)


def build_agents(llm, verbose: bool = True) -> dict[str, Agent]:
    common = dict(llm=llm, allow_delegation=False, verbose=verbose, max_iter=5)
    disclosure = Agent(
        role='경쟁사 공시 원문 팩트 추출 담당 (Disclosure Analysis Agent)',
        goal='경쟁사 공시 원문에서 원문으로 확인되는 사건·금액·일자·대상을 근거 문장과 함께 구조화하고, '
             '원문만으로 확인할 수 없는 내용은 사실과 분리한다.',
        backstory='삼성전자 DX부문(생활가전·전자) 전략기획팀에서 LG전자 등 경쟁사의 DART 공시를 1차 검토하는 '
                  '애널리스트다. 주요사항보고서와 사업·분기보고서의 서식에 익숙하다. 이 단계에서는 해석하지 않으며, '
                  '모든 사실에 원문 문장을 그대로 붙이는 것을 원칙으로 한다.',
        **common,
    )
    change = Agent(
        role='경쟁사 사업 변화 분석 담당 (Business Change Analyst)',
        goal='Program이 계산한 변화 후보와 과거 동일 유형 공시를 근거로, 과거 대비 무엇이 달라졌는지와 '
             '그것이 삼성전자 DX 생활가전·전자 사업에 주는 의미를 사실·해석·확인필요로 구분해 정리한다.',
        backstory='경쟁사 투자·사업재편 동향을 추적해 온 전략기획 실무자다. 숫자 계산은 Program이 이미 끝냈으므로 '
                  '직접 다시 계산하지 않는다. 비교가 불가능한 항목을 확정 사실처럼 쓰는 것을 가장 경계하며, '
                  '근거가 약한 해석은 확인필요로 돌린다.',
        **common,
    )
    report = Agent(
        role='경쟁사 인텔리전스 보고서 작성 담당 (Intelligence Report Agent)',
        goal='앞 단계의 FACT·CHANGE·INTERPRETATION·NEEDS_VERIFICATION을 전략기획 담당자가 5분 안에 '
             '검토·승인할 수 있는 보고서 초안으로 정리한다.',
        backstory='전략기획팀의 경쟁사 동향 보고서를 작성해 온 담당자다. 앞 단계에 없는 새로운 사실이나 해석을 '
                  '덧붙이지 않고, 사실과 해석이 섞이지 않도록 형식을 지킨다. 최종 중요도는 담당자가 정하므로 '
                  '보고서에 중요도를 단정하지 않는다.',
        **common,
    )
    return {'disclosure': disclosure, 'change': change, 'report': report}


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------
FEEDBACK_BLOCK = (
    '\n\n[담당자 재작업 요청]\n{reviewer_feedback}\n'
    '위 요청이 "없음"이 아니면 반드시 반영하여 이전 결과의 문제를 고친다.'
)

TASK1_DESCRIPTION = """\
[입력] 경쟁사 공시 1건
- 기업: {corp_name} / 보고서명: {report_nm} / 접수번호: {rcept_no} / 접수일: {rcept_dt}
- 데이터 출처: {data_origin}
- 원문 정규화 상태: {normalization_json}

[정규화된 원문]
{normalized_text}

[Program이 원문 전체에서 찾은 키워드 등장 문맥]
{keywords_json}

[할 일]
1. 원문에서 확인되는 사건(투자, 계약, 인수·매각, 자회사·지배구조 변화, 자금조달, 사업 변경 등)과
   금액·일자·대상을 fact로 정리한다. statement는 표 행을 그대로 옮기지 말고 "투자금액은 1,300억원이다"처럼
   주어와 서술어가 있는 문장으로 쓴다.
   category 기준: 시설·지분·사업에 돈을 쓰는 것은 투자, 유상증자·사채 발행·차입처럼 돈을 들여오는 것은 자금조달,
   타법인 주식·사업부 양수도는 인수·매각, 매출·이익 등 재무제표 수치는 재무.
2. 각 fact의 evidence_quote에는 원문 문장을 글자 그대로 복사한다. 요약하거나 고쳐 쓰지 않는다.
3. 키워드 등장 문맥마다 실제 사건인지 판단한다. "계획은 없습니다" 같은 부정문은 is_actual_event=false다.
   키워드가 나왔다는 사실만으로 사건이 발생했다고 쓰지 않는다.
4. 원문만으로 알 수 없는 것(목적의 구체적 내용, 원문에 없는 수치, 향후 계획 등)은 unverifiable에 적는다.
5. 원문이 비어 있거나 정규화 상태가 ok가 아니면 facts를 비우고 그 이유를 unverifiable에 적는다.
6. 추측, 업계 지식, 원문 밖의 정보로 fact를 만들지 않는다. 모든 문장은 한국어로 쓴다.""" + FEEDBACK_BLOCK

TASK2_DESCRIPTION = """\
[입력] 앞 단계(Task1)의 FACT 추출 결과는 context로 제공된다. 아래는 Program이 계산한 결과다.
- 접수번호: {rcept_no} / 기업: {corp_name} / 보고서명: {report_nm} / 데이터 출처: {data_origin}

[Program 변화 탐지 결과 - 금액 비교 (기준 ±20%)]
{amount_changes_json}

[Program 변화 탐지 결과 - 재무 비교 (기준 ±15%)]
{financial_changes_json}

[과거 동일 유형 공시]
{past_cases_json}

[과거 사례 검색 범위]
{history_search_json}

[Program이 남긴 확인 필요 사항]
{program_needs_verification_json}

[할 일]
1. changes: Program 비교 결과의 각 항목을 옮긴다. change_rate에는 Program이 준 change_rate_display
   (예: +30.0%)를 그대로 쓰고, triggered도 Program 값을 그대로 쓴다. 직접 계산하지 않는다. status가 not_comparable이거나 triggered가 null인 항목은 "비교 불가"라고 설명하고
   증가·감소로 단정하지 않는다. Task1의 관련 fact_id를 연결한다.
2. interpretations: 확인된 사실과 변화가 삼성전자 DX 생활가전·전자 사업에 줄 수 있는 영향을
   사업/재무/경쟁환경 중 하나로 분류해 쓴다. 반드시 근거 fact_id 또는 change_id를 based_on에 적고,
   "~일 가능성이 있다"처럼 추정임을 드러낸다. 근거 없는 해석은 쓰지 않는다.
   경쟁사의 투자·사업 확대는 삼성전자 입장에서 경쟁 압력(위협) 요인으로 먼저 검토하고, 우리 회사에 유리하다고
   볼 근거가 공시에 없으면 긍정적이라고 쓰지 않는다. 금액 하나만으로 내린 해석의 confidence는 낮음으로 둔다.
3. needs_verification: Program이 남긴 확인 필요 사항을 하나도 빠뜨리지 말고 code를 그대로 유지해
   origin=program으로 옮긴다. Task1의 unverifiable 항목은 하나씩 모두 code=AGENT_CHECK, origin=agent로 추가하고,
   confidence가 낮음인 해석도 무엇을 확인하면 확정할 수 있는지 AGENT_CHECK로 추가한다.
4. 과거 사례가 비어 있어도 검색 범위(coverage_status)가 complete가 아니면 "과거에 없던 신규 사건"이라고 쓰지 않는다.
5. follow_up_checks: 담당자가 추가로 확인할 질문을 2~5개 쓴다. 모든 문장은 한국어로 쓴다.""" + FEEDBACK_BLOCK

TASK3_DESCRIPTION = """\
[입력] Task1(FACT)과 Task2(CHANGE·INTERPRETATION·NEEDS_VERIFICATION) 결과는 context로 제공된다.
- 기업: {corp_name} / 보고서명: {report_nm} / 접수번호: {rcept_no} / 접수일: {rcept_dt}
- 원문 링크: {source_url}
- 데이터 출처 안내: {data_origin_notice}

[할 일]
삼성전자 DX부문 전략기획 담당자가 검토할 경쟁사 공시 보고서 초안을 마크다운으로 작성한다.
1. 앞 단계에 없는 사실·수치·해석을 새로 만들지 않는다.
2. 사실(FACT)과 해석(INTERPRETATION)을 다른 절에 쓰고, 해석 문장 앞에는 [해석]을 붙인다.
3. 모든 사실에는 fact_id와 근거 원문 인용, 접수번호를 함께 적는다.
4. 확인 필요 항목과 담당자 후속 확인 질문(follow_up_checks)을 하나도 빠짐없이 옮긴다.
   변화율은 Task2의 표시값(예: +30.0%)을 그대로 쓴다.
   Task1의 keyword_checks는 키워드별로 근거 문장과 함께 is_actual_event가 true면 '실제 사건',
   false면 '실제 사건 아님(부정문)', null이면 '판단 불가'로 적는다.
5. 중요도(높음/중간/낮음)는 쓰지 않는다. 담당자가 결정한다.
6. 데이터 출처 안내가 "없음"이 아니면 보고서 제목 바로 아래에 "> ⚠️ "로 시작하는 인용문 한 줄로 표시한다.""" + FEEDBACK_BLOCK

TASK3_EXPECTED = """\
한국어 마크다운 보고서 (코드블록으로 감싸지 않음). 제목 줄은 아래 목록의 따옴표 안 문구와 똑같이 쓰고,
괄호 속 작성 안내는 제목에 붙이지 말고 그 절의 본문으로만 반영한다.
- "# 경쟁사 공시 검토 보고서: (기업명) - (보고서명)" 바로 아래 데이터 출처 경고 인용문, 그 아래 접수번호·접수일·원문 링크 표
- "## 1. 한 줄 요약"
- "## 2. 무슨 일이 발생했나 (FACT)" (fact_id, 사실, 근거 인용, 접수번호)
- "### 키워드 등장 검토" (키워드, 실제 사건 여부를 '실제 사건' / '실제 사건 아님(부정문)' / '판단 불가' 중 하나로, 근거 문장)
- "## 3. 과거와 무엇이 달라졌나 (CHANGE)" (이전 값, 현재 값, Program 변화율, 비교 가능 여부)
- "## 4. 우리 회사 관점의 시사점 (INTERPRETATION)" ([해석] 표시, 영향 영역, 근거 id, 신뢰도)
- "## 5. 추가 확인 필요 (NEEDS_VERIFICATION)" (전부 옮긴다. origin이 program이면 "[Program] 사유 코드", agent이면 "[Agent 확인]"으로 앞에 표시하고, 확인할 내용과 이유를 적는다)
- "### 담당자 후속 확인 질문" (follow_up_checks 전부)
- "## 6. 담당자 검토 체크리스트" (근거 일치, 사실·해석 구분, 과도한 추론 여부 체크박스)"""


def program_codes_guardrail(required_codes: set[str], facts_task=None):
    """Task2 가드레일 (Program 검증).
    1) Program이 넘긴 확인 필요 코드가 빠지면 Agent에게 재작성시킨다.
    2) 신뢰도 '낮음' 해석과 Task1의 확인 불가 항목에 대응하는 AGENT_CHECK가 없으면 Program이 직접 채운다.
       LLM이 지시를 놓쳐도 확인 필요 항목이 보고서에서 누락되지 않게 하기 위함이다."""
    def check(output):
        result = output.pydantic
        if result is None:
            try:
                result = ChangeAnalysis.model_validate_json(output.raw)
            except Exception:
                return False, 'ChangeAnalysis 형식의 JSON으로 다시 출력하세요.'
        got = {v.code for v in result.needs_verification if v.origin == 'program'}
        missing = sorted(required_codes - got)
        if missing:
            return False, (f'Program의 확인 필요 코드 {missing}가 needs_verification에 없습니다. '
                           'origin=program으로 모두 포함해 다시 작성하세요.')

        agent_text = ' '.join(f'{v.item} {v.why}' for v in result.needs_verification if v.origin == 'agent')
        added = []
        for interp in result.interpretations:
            if interp.confidence == '낮음' and interp.statement[:15] not in agent_text:
                added.append(VerificationItem(
                    code='AGENT_CHECK', field='interpretations',
                    item=f'해석 검증: {interp.statement}',
                    why=f'근거({", ".join(interp.based_on)})만으로는 신뢰도가 낮아 추가 자료로 확인이 필요합니다.',
                    origin='agent'))
        facts = getattr(getattr(facts_task, 'output', None), 'pydantic', None)
        for text in getattr(facts, 'unverifiable', []) or []:
            if text[:15] not in agent_text:
                added.append(VerificationItem(
                    code='AGENT_CHECK', field='facts.unverifiable', item=text,
                    why='공시 원문만으로는 확인할 수 없는 내용입니다.', origin='agent'))
        if added:
            result.needs_verification.extend(added)
            output.pydantic = result
            output.raw = result.model_dump_json()
        return True, output
    return check


class DisclosureCrew:
    """세 Task를 묶고, 담당자 반려 시 지정 단계부터 다시 실행한다."""

    STAGES = ('facts', 'analysis', 'report')

    def __init__(self, required_codes: set[str], llm=None, log_file: Path | None = None, verbose: bool = True):
        self.verbose = verbose
        self.agents = build_agents(llm or make_llm(), verbose)
        self.log_file = log_file

        self.task_facts = Task(
            name='task1_fact_extraction',
            description=TASK1_DESCRIPTION,
            expected_output='FactExtraction 형식: rcept_no, corp_name, report_nm, facts(근거 원문 포함), '
                            'keyword_checks, unverifiable',
            agent=self.agents['disclosure'],
            output_pydantic=FactExtraction,
        )
        self.task_analysis = Task(
            name='task2_change_analysis',
            description=TASK2_DESCRIPTION,
            expected_output='ChangeAnalysis 형식: rcept_no, changes, interpretations(based_on 필수), '
                            'needs_verification(Program 코드 전부 포함), follow_up_checks',
            agent=self.agents['change'],
            context=[self.task_facts],                       # Task1 → Task2
            output_pydantic=ChangeAnalysis,
            guardrail=program_codes_guardrail(required_codes, self.task_facts),
            guardrail_max_retries=2,
        )
        self.task_report = Task(
            name='task3_intelligence_report',
            description=TASK3_DESCRIPTION,
            expected_output=TASK3_EXPECTED,
            agent=self.agents['report'],
            context=[self.task_facts, self.task_analysis],   # Task1·2 → Task3
        )
        self.tasks = [self.task_facts, self.task_analysis, self.task_report]

    def run(self, inputs: dict, start_stage: int = 1) -> dict:
        """start_stage=1: 전체, 2: Task2부터, 3: Task3만.
        앞 Task 객체는 직전 실행 결과(output)를 보존하므로 context로 그대로 전달된다."""
        if start_stage > 1 and any(t.output is None for t in self.tasks[:start_stage - 1]):
            raise RuntimeError('앞 단계 결과가 없어 부분 재실행을 할 수 없습니다. 처음부터 실행하세요.')
        crew = Crew(
            agents=list(self.agents.values()),
            tasks=self.tasks[start_stage - 1:],
            process=Process.sequential,
            verbose=self.verbose,
            output_log_file=str(self.log_file) if self.log_file else False,
        )
        crew.kickoff(inputs=inputs)
        return self.outputs()

    def outputs(self) -> dict:
        def dump(task):
            if task.output is None:
                return None
            if task.output.pydantic is not None:
                return task.output.pydantic.model_dump()
            return task.output.raw
        return {name: dump(task) for name, task in zip(self.STAGES, self.tasks)}


# ---------------------------------------------------------------------------
# B의 JSON(v1.0.0) → Task 입력 변수
# ---------------------------------------------------------------------------
def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _with_display(items: list) -> list:
    """Program 변화율(0.3)을 사람이 읽는 표시값(+30.0%)으로 함께 넘긴다. Agent가 계산하지 않도록."""
    out = []
    for item in items or []:
        item = dict(item)
        rate = item.get('change_rate')
        item['change_rate_display'] = '비교 불가' if rate is None else f'{float(rate) * 100:+.1f}%'
        out.append(item)
    return out


def build_inputs(payload: dict, reviewer_feedback: str = '없음') -> dict:
    triggers = payload.get('triggers') or {}
    origin = payload.get('data_origin')
    notice = {
        'synthetic': '개발용 가상 데이터로 실행한 결과입니다. 실제 LG전자 공시가 아니며 제출용 검증에 사용할 수 없습니다.',
        'mixed': '실제 공시와 가상 데이터가 섞인 결과입니다. 제출용 검증에 사용할 수 없습니다.',
    }.get(origin, '없음')
    return {
        'rcept_no': payload.get('rcept_no') or '',
        'corp_name': payload.get('corp_name') or '',
        'report_nm': payload.get('report_nm') or '',
        'rcept_dt': payload.get('rcept_dt') or '',
        'data_origin': origin or 'unknown',
        'data_origin_notice': notice,
        'source_url': (payload.get('source') or {}).get('source_url') or '없음',
        'normalized_text': payload.get('normalized_text') or '(원문 없음)',
        'normalization_json': _j(payload.get('normalization')),
        'keywords_json': _j(triggers.get('keywords', [])),
        'amount_changes_json': _j(_with_display(triggers.get('amount_changes', []))),
        'financial_changes_json': _j(_with_display(triggers.get('financial_changes', []))),
        'past_cases_json': _j(payload.get('past_cases', [])),
        'history_search_json': _j(payload.get('history_search')),
        'program_needs_verification_json': _j(payload.get('needs_verification', [])),
        'reviewer_feedback': reviewer_feedback,
    }


def required_program_codes(payload: dict) -> set[str]:
    return {item['code'] for item in payload.get('needs_verification', []) if item.get('code')}
