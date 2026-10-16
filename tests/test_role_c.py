"""역할 C 연결 테스트: OpenAI를 호출하지 않는 가짜 LLM으로 Workflow 배선만 검증한다.

확인 항목
- 공시 사실 추출 → 사업 변화·영향 분석 → 경쟁사 분석 보고서 작성 순서와 context 전달
- 사업 변화·영향 분석 가드레일: Program 확인 필요 코드 누락 시 재시도
- 담당자 반려 시 지정 단계부터 재실행, 재작업 지시 전달
- 결과 파일과 review_history 저장, Program 근거 대조
실행: python -m unittest tests/test_role_c.py -v
"""
import json
import sqlite3
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings('ignore', category=DeprecationWarning)  # CrewAI 내부 경고 숨김

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import program_tools  # noqa: E402
import crew  # noqa: E402  (crew가 CrewAI 환경 변수를 먼저 설정)
from crewai.llms.base_llm import BaseLLM  # noqa: E402

MOCK = json.loads((ROOT / 'examples' / 'mock_input.json').read_text(encoding='utf-8'))

FACTS = {
    'rcept_no': 'MOCK-CURRENT-001', 'corp_name': 'LG전자', 'report_nm': '개발용 가상 투자 공시',
    'facts': [{'fact_id': 'F1', 'statement': '투자금액은 1,300억원이다.', 'category': '투자',
               'amount_raw': '1,300억원', 'date': None, 'evidence_quote': '투자금액 | 1,300억원',
               'evidence_rcept_no': 'MOCK-CURRENT-001'}],
    'keyword_checks': [{'keyword': '합병', 'quote': '현재 합병 계획은 없습니다.', 'is_actual_event': False,
                        'note': '부정문'}],
    'unverifiable': ['투자 목적'],
}
ANALYSIS_BAD = {'rcept_no': 'MOCK-CURRENT-001', 'changes': [], 'interpretations': [],
                'needs_verification': [], 'follow_up_checks': []}
ANALYSIS_OK = {
    'rcept_no': 'MOCK-CURRENT-001',
    'changes': [{'change_id': 'C1', 'metric': '투자금액', 'previous': '1,000억원', 'current': '1,300억원',
                 'change_rate': '+30.0%', 'triggered': True, 'description': '직전 대비 증가',
                 'related_fact_ids': ['F1']}],
    'interpretations': [{'statement': '투자 확대로 경쟁 압력이 커질 가능성이 있다.', 'impact_area': '경쟁환경',
                         'based_on': ['C1'], 'confidence': '낮음'}],
    'needs_verification': [
        {'code': c['code'], 'field': c['field'], 'item': c['message'], 'why': 'Program', 'origin': 'program'}
        for c in MOCK['needs_verification']],
    'follow_up_checks': ['투자 대상 사업은?'],
}


def _fin(metric, fs_div, cur, prev):
    def v(x):
        return {'value': x, 'raw_value': f'{int(x):,}', 'unit': '원', 'currency': 'KRW',
                'evidence': {'kind': 'financial_account', 'rcept_no': 'MOCK-CURRENT-001'}}
    return {'metric': metric, 'status': 'evaluated', 'triggered': None, 'reason': None,
            'current': v(cur), 'previous': v(prev), 'change_rate': None, 'threshold': 0.15,
            'dimensions': {'fs_div': fs_div, 'sj_div': 'IS', 'current_period': '2025.1Q~3Q',
                           'previous_period': '2024.1Q~3Q'}}


class FakeLLM(BaseLLM):
    """프롬프트 내용을 보고 Task를 구분해 정해진 답을 돌려준다."""

    def __init__(self):
        super().__init__(model='fake-model')
        self.prompts = []
        self.analysis_calls = 0
        self.facts_quote = '투자금액 | 1,300억원'

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None, **kwargs):
        text = messages if isinstance(messages, str) else '\n'.join(str(m.get('content', '')) for m in messages)
        self.prompts.append(text)
        if '[정규화된 원문]' in text:
            facts = dict(FACTS, facts=[dict(FACTS['facts'][0], evidence_quote=self.facts_quote)])
            body = json.dumps(facts, ensure_ascii=False)
        elif '[Program 비교 항목 목록' in text:
            self.analysis_calls += 1
            body = json.dumps(ANALYSIS_BAD if self.analysis_calls == 1 else ANALYSIS_OK, ensure_ascii=False)
        else:
            body = '# 경쟁사 공시 검토 보고서: LG전자 - 개발용 가상 투자 공시\n## 1. 한 줄 요약\n테스트'
        return f'Thought: done\nFinal Answer: {body}'

    def supports_function_calling(self):
        return False

    def supports_stop_words(self):
        return False

    def get_context_window_size(self):
        return 128000


class RoleCWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        main.RESULTS_DIR = Path(self.tmp.name) / 'results'
        main.REVIEW_DB = Path(self.tmp.name) / 'review.db'

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_with_reject_and_rerun(self):
        llm = FakeLLM()
        reviews = iter([
            {'decision': '반려', 'reason': '비교·해석 오류', 'restart_stage': 2, 'comment': '해석 근거 보강'},
            {'decision': '승인', 'importance': '중간', 'comment': 'ok'},
        ])
        run_dir = main.main(['--mock'], llm=llm, review_fn=lambda report, problems: next(reviews))

        analysis_prompts = [p for p in llm.prompts if '[Program 비교 항목 목록' in p]
        report_prompts = [p for p in llm.prompts if '보고서 초안을 마크다운으로' in p]
        # context: 사업 변화·영향 분석 프롬프트에 공시 사실 추출 결과가, 경쟁사 분석 보고서 작성에 공시 사실 추출·사업 변화·영향 분석 결과가 들어간다.
        self.assertIn('투자금액은 1,300억원이다.', analysis_prompts[0])
        self.assertIn('투자금액은 1,300억원이다.', report_prompts[0])
        self.assertIn('HISTORY_COVERAGE_UNKNOWN', report_prompts[0])
        # 가드레일: 첫 사업 변화·영향 분석 출력에 Program 코드가 없어도 멈추거나 재시도하지 않고 Program이 원문 그대로 보완
        round1 = json.loads((run_dir / 'round1_analyze_business_changes.json').read_text(encoding='utf-8'))
        filled = {v['code']: v['item'] for v in round1['needs_verification'] if v['origin'] == 'program'}
        for c in MOCK['needs_verification']:
            self.assertEqual(filled.get(c['code']), c['message'])
        self.assertNotIn('needs_verification에 없습니다', '\n'.join(analysis_prompts))
        # 반려(해석 오류) → 공시 사실 추출은 다시 돌지 않고 사업 변화·영향 분석부터, 재작업 지시 전달
        facts_prompts = [p for p in llm.prompts if '[정규화된 원문]' in p]
        self.assertEqual(len(facts_prompts), 1)
        self.assertTrue(any('해석 근거 보강' in p for p in analysis_prompts))
        # 저장
        for name in ('input.json', 'round1_write_intelligence_report.md', 'round2_analyze_business_changes.json', 'final_report.md'):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertIn('중요도: 중간', (run_dir / 'final_report.md').read_text(encoding='utf-8'))
        rows = sqlite3.connect(main.REVIEW_DB).execute(
            'SELECT round, decision FROM review_history ORDER BY id').fetchall()
        self.assertEqual(rows, [(1, '반려'), (2, '승인')])
        # Program 보완: 공시 사실 추출의 확인 불가 항목('투자 목적')이 AGENT_CHECK로 채워져 경쟁사 분석 보고서 작성까지 전달
        analysis = json.loads((run_dir / 'round1_analyze_business_changes.json').read_text(encoding='utf-8'))
        agent_items = [v for v in analysis['needs_verification'] if v['origin'] == 'agent']
        self.assertTrue(any(v['item'] == '투자 목적' for v in agent_items))
        self.assertTrue(any('투자 목적' in p for p in report_prompts))
        round2 = json.loads((run_dir / 'round2_analyze_business_changes.json').read_text(encoding='utf-8'))
        self.assertTrue(any(v['field'] == 'interpretations' and v['origin'] == 'agent'
                            for v in round2['needs_verification']))

    def test_program_tools_return_program_values(self):
        """교수 코멘트 반영: Agent가 호출하는 도구가 B의 계산 결과를 그대로 돌려주는지 확인"""
        payload = json.loads(json.dumps(MOCK))
        payload['triggers']['financial_changes'] = [
            _fin('당기순이익(손실)', 'CFS', '1946335000000', '1305119000000'),
            _fin('당기순이익(손실)', 'OFS', '376021000000', '1123278000000'),
        ]
        tools = program_tools.build_tools(payload)
        finance = tools['change'][0]
        cfs = json.loads(finance._run('당기순이익', '연결'))
        self.assertEqual((cfs[0]['change_rate_display'], cfs[0]['direction'], cfs[0]['triggered']), ('+49.1%', '증가', True))
        both = json.loads(finance._run('순이익'))
        self.assertEqual({r['basis'] for r in both}, {'연결', '별도'})
        amount = json.loads(tools['change'][1]._run('투자금액'))
        self.assertEqual(amount[0]['change_rate_display'], '+30.0%')
        parse = tools['change'][2]
        self.assertEqual(json.loads(parse._run('1,300억원'))['value_won'], '130000000000')
        self.assertEqual(json.loads(parse._run('1조 3,000억원'))['result'], '해석 불가')
        self.assertIn('없음', finance._run('영업외수익'))
        # 프롬프트 목록에는 숫자를 넣지 않는다
        self.assertNotIn('1946335', tools['data'].index())

    def test_agent_calls_program_tool_inside_crew(self):
        """Crew 실행 중 사업 변화·영향 분석 Agent가 도구를 호출하고 Program 값을 받는지 확인"""
        payload = json.loads(json.dumps(MOCK))
        payload['triggers']['financial_changes'] = [_fin('당기순이익(손실)', 'CFS', '1946335000000', '1305119000000')]
        received = []

        class ToolCallingLLM(FakeLLM):
            def call(self, messages, *args, **kwargs):
                text = messages if isinstance(messages, str) else '\n'.join(str(m.get('content', '')) for m in messages)
                if '[Program 비교 항목 목록' in text:
                    self.analysis_calls += 1
                    if self.analysis_calls == 1:
                        return ('Thought: 순이익 확인\nAction: lookup_financial_change\n'
                                'Action Input: {"metric": "당기순이익", "basis": "연결"}')
                    received.append('"change_rate_display": "+49.1%"' in text and '"direction": "증가"' in text)
                    return 'Thought: done\nFinal Answer: ' + json.dumps(ANALYSIS_OK, ensure_ascii=False)
                return super().call(messages, *args, **kwargs)

        crew_obj = crew.DisclosureCrew(payload, llm=ToolCallingLLM(), verbose=False)
        crew_obj.run(crew.build_inputs(payload))
        self.assertEqual(received, [True])

    def test_program_checks_flag_wrong_rate_and_direction(self):
        """Agent가 Program 값과 다른 변화율·증감 방향을 쓰면 경고하는지 확인 (개선 전 일반 사례 재현)"""
        payload = json.loads(json.dumps(MOCK))
        payload['triggers']['financial_changes'] = [
            _fin('영업이익', 'CFS', '2587381000000', '3284344000000'),
            _fin('당기순이익(손실)', 'CFS', '1946335000000', '1305119000000'),
            _fin('당기순이익(손실)', 'OFS', '376021000000', '1123278000000'),
        ]
        data = program_tools.ProgramData(payload)
        report = '영업이익과 당기순이익이 감소하였다.\n별도 기준 당기순이익은 감소했다.\n매출채권이 증가했다.'
        problems = program_tools.check_report_directions(report, data)
        self.assertEqual(len(problems), 1)
        self.assertIn('+49.1%', problems[0]['program'])
        analysis = {'changes': [
            {'change_id': 'C1', 'metric': '영업이익', 'basis': '연결', 'change_rate': '-21.2%'},
            {'change_id': 'C2', 'metric': '당기순이익(손실)', 'basis': '연결', 'change_rate': '-66.5%'},
        ]}
        rate_problems = program_tools.check_change_rates(analysis, data)
        self.assertEqual([p['item'] for p in rate_problems], ['C2'])

    def test_same_code_items_are_all_kept(self):
        """같은 코드가 두 건이면 Agent가 한 건만 옮겨도 나머지 한 건을 B 원문 그대로 붙인다 (5주차 최종 실행에서 발견)"""
        items = [{'code': 'AMOUNT_UNRESOLVED', 'field': 'f', 'message': '현재 공시: 계약금액 불명확'},
                 {'code': 'AMOUNT_UNRESOLVED', 'field': 'f', 'message': '직전 공시: 계약금액 불명확'}]
        guard = crew.program_codes_guardrail(items)
        analysis = crew.ChangeAnalysis(rcept_no='X', needs_verification=[
            crew.VerificationItem(code='AMOUNT_UNRESOLVED', item='현재 공시: 계약금액 불명확', why='', origin='program')])

        class Out:
            pydantic = analysis
            raw = analysis.model_dump_json()
        ok, out = guard(Out())
        texts = [v.item for v in out.pydantic.needs_verification]
        self.assertTrue(ok)
        self.assertEqual(sorted(texts), sorted(i['message'] for i in items))

    def test_change_numbers_come_only_from_program(self):
        """CHANGE 수치 칸은 Program 값만 허용: 빈 값 채우기, 틀린 값 교체·기록, 기준 불명 시 미기입, 후보 누락 시 추가"""
        payload = json.loads(json.dumps(MOCK))
        payload['triggers']['financial_changes'] = [
            _fin('영업이익', 'CFS', '2587381000000', '3284344000000'),
            _fin('당기순이익(손실)', 'CFS', '1946335000000', '1305119000000'),
            _fin('당기순이익(손실)', 'OFS', '376021000000', '1123278000000'),
        ]
        data = program_tools.ProgramData(payload)
        changes = [
            crew.Change(change_id='C1', metric='영업이익', basis='연결', description='기준 초과'),        # 빈 값 → 채움
            crew.Change(change_id='C2', metric='당기순이익(손실)', basis='연결', change_rate='-66.5%',
                        description='순이익 감소'),                                                        # 틀린 값·방향
            crew.Change(change_id='C3', metric='당기순이익(손실)', description='x'),                        # 기준 불명
        ]
        notes = program_tools.apply_program_values(changes, data, make=crew.Change)
        self.assertEqual((changes[0].change_rate, changes[0].current), ('-21.2%', '2,587,381,000,000'))
        self.assertEqual(changes[1].change_rate, '+49.1%')
        self.assertIsNone(changes[2].change_rate)
        kinds = [(n['item'], n['check']) for n in notes]
        self.assertIn(('C2', '수치 보정'), kinds)
        self.assertIn(('C2', '증감 방향 대조'), kinds)
        self.assertIn(('C3', '수치 보정'), kinds)
        added = [c for c in changes if c.change_id.startswith('P')]
        self.assertEqual([(c.metric, c.change_rate) for c in added], [('투자금액', '+30.0%')])  # MOCK 금액 항목
        self.assertNotIn('C1', [n['item'] for n in notes])  # 빈 칸 채우기는 경고 아님

    def test_out_of_list_labels_do_not_stop_run(self):
        """도구를 가진 Agent는 양식 강제가 꺼져 목록 밖 분류를 쓸 수 있다(5주차 실행에서 '중단', '지분·매각').
        멈추지 않고 '기타'로 두되 원래 값은 보존하고, 사실 내용은 그대로인지 확인"""
        fact = dict(FACTS['facts'][0], category='지분·매각')
        parsed = crew.FactExtraction.model_validate(dict(FACTS, facts=[fact]))
        self.assertEqual((parsed.facts[0].category, parsed.facts[0].category_original), ('기타', '지분·매각'))
        self.assertEqual(parsed.facts[0].evidence_quote, fact['evidence_quote'])
        interp = crew.Interpretation.model_validate(
            {'statement': 's', 'impact_area': '시장', 'based_on': ['C1'], 'confidence': '보통'})
        self.assertEqual((interp.impact_area, interp.impact_area_original), ('기타', '시장'))
        self.assertEqual((interp.confidence, interp.confidence_original), ('낮음', '보통'))
        item = crew.VerificationItem.model_validate({'code': 'X', 'item': 'i', 'why': 'w', 'origin': 'Program'})
        self.assertEqual(item.origin, 'program')
        kc = crew.KeywordCheck.model_validate({'keyword': '중단', 'quote': 'q', 'is_actual_event': '판단 불가'})
        self.assertIsNone(kc.is_actual_event)
        self.assertIn('판단 불가', kc.note)

    def test_evidence_check_flags_fabricated_quote(self):
        facts = dict(FACTS, facts=[dict(FACTS['facts'][0], evidence_quote='투자금액 | 2,000억원',
                                        evidence_rcept_no='99999999999999')])
        problems = main.check_evidence(facts, MOCK)
        self.assertEqual({p['problem'] for p in problems},
                         {'근거 인용이 원문에서 확인되지 않음', '입력에 없는 접수번호'})
        self.assertEqual(main.check_evidence(FACTS, MOCK), [])


if __name__ == '__main__':
    unittest.main(warnings='ignore')
