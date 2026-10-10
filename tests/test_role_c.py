"""역할 C 연결 테스트: OpenAI를 호출하지 않는 가짜 LLM으로 Workflow 배선만 검증한다.

확인 항목
- Task1 → Task2 → Task3 순서와 context 전달
- Task2 가드레일: Program 확인 필요 코드 누락 시 재시도
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

import main  # noqa: E402  (crew가 CrewAI 환경 변수를 먼저 설정)
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


class FakeLLM(BaseLLM):
    """프롬프트 내용을 보고 Task를 구분해 정해진 답을 돌려준다."""

    def __init__(self):
        super().__init__(model='fake-model')
        self.prompts = []
        self.task2_calls = 0
        self.task1_quote = '투자금액 | 1,300억원'

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None, **kwargs):
        text = messages if isinstance(messages, str) else '\n'.join(str(m.get('content', '')) for m in messages)
        self.prompts.append(text)
        if '[정규화된 원문]' in text:
            facts = dict(FACTS, facts=[dict(FACTS['facts'][0], evidence_quote=self.task1_quote)])
            body = json.dumps(facts, ensure_ascii=False)
        elif '[Program 변화 탐지 결과' in text:
            self.task2_calls += 1
            body = json.dumps(ANALYSIS_BAD if self.task2_calls == 1 else ANALYSIS_OK, ensure_ascii=False)
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

        task2_prompts = [p for p in llm.prompts if '[Program 변화 탐지 결과' in p]
        task3_prompts = [p for p in llm.prompts if '보고서 초안을 마크다운으로' in p]
        # context: Task2 프롬프트에 Task1 결과가, Task3에 Task1·2 결과가 들어간다.
        self.assertIn('투자금액은 1,300억원이다.', task2_prompts[0])
        self.assertIn('투자금액은 1,300억원이다.', task3_prompts[0])
        self.assertIn('HISTORY_COVERAGE_UNKNOWN', task3_prompts[0])
        # 가드레일: 첫 Task2 출력에 Program 코드가 없어 재시도
        self.assertIn('needs_verification에 없습니다', '\n'.join(task2_prompts))
        # 반려(해석 오류) → Task1은 다시 돌지 않고 Task2부터, 재작업 지시 전달
        task1_prompts = [p for p in llm.prompts if '[정규화된 원문]' in p]
        self.assertEqual(len(task1_prompts), 1)
        self.assertTrue(any('해석 근거 보강' in p for p in task2_prompts))
        # 저장
        for name in ('input.json', 'round1_report.md', 'round2_task2_analysis.json', 'final_report.md'):
            self.assertTrue((run_dir / name).exists(), name)
        self.assertIn('중요도: 중간', (run_dir / 'final_report.md').read_text(encoding='utf-8'))
        rows = sqlite3.connect(main.REVIEW_DB).execute(
            'SELECT round, decision FROM review_history ORDER BY id').fetchall()
        self.assertEqual(rows, [(1, '반려'), (2, '승인')])
        # Program 보완: Task1의 확인 불가 항목('투자 목적')이 AGENT_CHECK로 채워져 Task3까지 전달
        analysis = json.loads((run_dir / 'round1_task2_analysis.json').read_text(encoding='utf-8'))
        agent_items = [v for v in analysis['needs_verification'] if v['origin'] == 'agent']
        self.assertTrue(any(v['item'] == '투자 목적' for v in agent_items))
        self.assertTrue(any('투자 목적' in p for p in task3_prompts))
        self.assertTrue(any(v['field'] == 'interpretations' for v in agent_items))

    def test_evidence_check_flags_fabricated_quote(self):
        facts = dict(FACTS, facts=[dict(FACTS['facts'][0], evidence_quote='투자금액 | 2,000억원',
                                        evidence_rcept_no='99999999999999')])
        problems = main.check_evidence(facts, MOCK)
        self.assertEqual({p['problem'] for p in problems},
                         {'근거 인용이 원문에서 확인되지 않음', '입력에 없는 접수번호'})
        self.assertEqual(main.check_evidence(FACTS, MOCK), [])


if __name__ == '__main__':
    unittest.main(warnings='ignore')
