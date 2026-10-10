"""역할 C: 전체 실행 진입점.

B 변화 탐지 결과(JSON) → CrewAI 3단계 분석 → 근거 자동 대조(Program) → 담당자 검토(Human)
→ 반려 시 사유별 재작업 → 결과 파일과 review_history 저장

사용 예
  python main.py --mock                                   # 개발용 가상 입력 (examples/mock_input.json)
  python main.py --input results/어떤입력.json            # B가 만든 JSON 파일
  python main.py --rcept-no 20250312000123 --data-origin dart   # DB에서 B의 build_payload 호출
  python main.py --mock --no-review                       # 사람 검토 없이 1회 실행 (테스트 일괄 실행용)
  python main.py --mock --quiet                           # 진행 로그 생략, 보고서와 검토 화면만
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from crew import (ROOT, DisclosureCrew, build_inputs,  # crew가 .env와 CrewAI 경로를 먼저 설정한다
                  required_program_codes)

RESULTS_DIR = Path(os.getenv('RESULTS_DIR', 'results'))
RESULTS_DIR = RESULTS_DIR if RESULTS_DIR.is_absolute() else ROOT / RESULTS_DIR
REVIEW_DB = ROOT / 'data' / 'review.db'   # A의 dart.db는 건드리지 않고 별도 파일에 이력 저장
MAX_ROUNDS = 3


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)

REJECT_REASONS = {
    '1': ('근거 오류', 1, 'Task1(공시 사실 추출)부터 재작업'),
    '2': ('비교·해석 오류', 2, 'Task2(변화·영향 분석)부터 재작업'),
    '3': ('보고서 형식 오류', 3, 'Task3(보고서 작성)만 재작업'),
}
IMPORTANCE = {'1': '높음', '2': '중간', '3': '낮음', '4': '보류'}


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------
def load_payload(args) -> dict:
    if args.mock:
        return json.loads((ROOT / 'examples' / 'mock_input.json').read_text(encoding='utf-8'))
    if args.input:
        return json.loads(Path(args.input).read_text(encoding='utf-8'))
    from change_detector import build_payload
    db = Path(args.db) if args.db else ROOT / 'data' / 'dart.db'
    return build_payload(args.rcept_no, db, data_origin=args.data_origin)


# ---------------------------------------------------------------------------
# Program 검증: Agent가 쓴 근거 인용이 실제 입력에 있는지 대조
# ---------------------------------------------------------------------------
def _squash(text: str) -> str:
    return re.sub(r'\s+', '', text or '')


def _collect_quotes(node, out: list[str]):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ('quote', 'excerpt') and isinstance(value, str):
                out.append(value)
            else:
                _collect_quotes(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_quotes(value, out)


def check_evidence(facts: dict | None, payload: dict) -> list[dict]:
    """원문·Program 근거에서 찾을 수 없는 인용, 입력에 없는 접수번호를 찾아낸다."""
    if not isinstance(facts, dict):
        return [{'fact_id': '-', 'problem': 'Task1 결과가 구조화되지 않음'}]
    quotes = [payload.get('normalized_text') or '']
    _collect_quotes(payload.get('triggers'), quotes)
    _collect_quotes(payload.get('past_cases'), quotes)
    corpus = _squash('\n'.join(quotes))
    known_rcepts = {payload.get('rcept_no')} | {c.get('rcept_no') for c in payload.get('past_cases', [])}
    known_rcepts |= set(re.findall(r'"rcept_no":\s*"([^"]+)"', json.dumps(payload.get('triggers'), ensure_ascii=False)))

    problems = []
    for fact in facts.get('facts', []):
        quote = _squash(fact.get('evidence_quote'))
        if not quote or quote not in corpus:
            problems.append({'fact_id': fact.get('fact_id'), 'problem': '근거 인용이 원문에서 확인되지 않음',
                             'quote': fact.get('evidence_quote')})
        if fact.get('evidence_rcept_no') not in known_rcepts:
            problems.append({'fact_id': fact.get('fact_id'), 'problem': '입력에 없는 접수번호',
                             'rcept_no': fact.get('evidence_rcept_no')})
    return problems


# ---------------------------------------------------------------------------
# Human-in-the-Loop
# ---------------------------------------------------------------------------
def ask(prompt: str, choices: dict) -> str:
    while True:
        answer = input(prompt).strip()
        if answer in choices:
            return answer
        print('  보기 중 번호를 입력하세요:', ', '.join(choices))


def human_review(report: str, evidence_problems: list[dict]) -> dict:
    print('\n' + '=' * 70 + '\n[보고서 초안]\n' + '=' * 70)
    print(report)
    print('=' * 70)
    if evidence_problems:
        print('\n[Program 근거 대조 경고] 아래 항목은 원문과 일치하지 않습니다:')
        for p in evidence_problems:
            print(f"  - {p['fact_id']}: {p['problem']} {p.get('quote') or p.get('rcept_no') or ''}")
    else:
        print('\n[Program 근거 대조] 모든 FACT 인용이 원문·Program 근거에서 확인되었습니다.')

    decision = ask('\n검토 결과를 선택하세요 (1 승인 / 2 보류 / 3 반려): ', {'1': 0, '2': 0, '3': 0})
    if decision == '3':
        print('반려 사유: ' + ' / '.join(f'{k} {v[0]} → {v[2]}' for k, v in REJECT_REASONS.items()))
        reason = ask('사유 번호: ', REJECT_REASONS)
        comment = input('구체적으로 고칠 내용 (Agent에게 전달): ').strip() or '구체적 지시 없음'
        return {'decision': '반려', 'reason': REJECT_REASONS[reason][0],
                'restart_stage': REJECT_REASONS[reason][1], 'comment': comment}
    if decision == '2':
        comment = input('보류 사유: ').strip()
        return {'decision': '보류', 'importance': '보류', 'comment': comment}
    importance = ask('최종 중요도 (1 높음 / 2 중간 / 3 낮음 / 4 보류): ', IMPORTANCE)
    comment = input('검토 의견 (선택): ').strip()
    return {'decision': '승인', 'importance': IMPORTANCE[importance], 'comment': comment}


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------
def save_history(row: dict):
    REVIEW_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(REVIEW_DB) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS review_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rcept_no TEXT NOT NULL,
                corp_name TEXT, report_nm TEXT, data_origin TEXT,
                round INTEGER NOT NULL,
                decision TEXT NOT NULL,          -- 승인 / 보류 / 반려 / 미검토
                reject_reason TEXT, importance TEXT, comment TEXT,
                evidence_problems INTEGER,
                run_dir TEXT, reviewed_at TEXT NOT NULL
            )""")
        db.execute("""
            INSERT INTO review_history (rcept_no, corp_name, report_nm, data_origin, round, decision,
                reject_reason, importance, comment, evidence_problems, run_dir, reviewed_at)
            VALUES (:rcept_no, :corp_name, :report_nm, :data_origin, :round, :decision,
                :reject_reason, :importance, :comment, :evidence_problems, :run_dir, :reviewed_at)""", row)


def write_round(run_dir: Path, rnd: int, outputs: dict, problems: list[dict]):
    (run_dir / f'round{rnd}_task1_facts.json').write_text(
        json.dumps(outputs['facts'], ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / f'round{rnd}_task2_analysis.json').write_text(
        json.dumps(outputs['analysis'], ensure_ascii=False, indent=2), encoding='utf-8')
    (run_dir / f'round{rnd}_report.md').write_text(str(outputs['report']), encoding='utf-8')
    (run_dir / f'round{rnd}_evidence_check.json').write_text(
        json.dumps(problems, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------------------------------------------------------------------------
def main(argv=None, llm=None, review_fn=human_review) -> Path:
    parser = argparse.ArgumentParser(description='경쟁사 공시 분석 Crew 실행')
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument('--mock', action='store_true', help='examples/mock_input.json 사용')
    src.add_argument('--input', help='B 형식(v1.0.0) JSON 파일 경로')
    src.add_argument('--rcept-no', help='DB에서 B의 build_payload로 입력 생성')
    parser.add_argument('--data-origin', choices=['dart', 'synthetic', 'mixed'], help='--rcept-no와 함께 필수')
    parser.add_argument('--db', help='DART DB 경로 (기본 data/dart.db)')
    parser.add_argument('--no-review', action='store_true', help='사람 검토 없이 1회 실행')
    parser.add_argument('--quiet', action='store_true', help='Agent 진행 로그를 화면에 출력하지 않음 (crew_log.txt에는 저장)')
    parser.add_argument('--label', default='', help='결과 폴더 이름에 붙일 표시 (예: test1_일반)')
    args = parser.parse_args(argv)
    if args.rcept_no and not args.data_origin:
        parser.error('--rcept-no를 쓸 때는 --data-origin이 필요합니다.')
    if llm is None and not os.getenv('OPENAI_API_KEY', '').strip():
        sys.exit('.env에 OPENAI_API_KEY를 설정하고 저장하세요.')

    payload = load_payload(args)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    name = '_'.join(x for x in (stamp, args.label, payload.get('rcept_no', 'unknown')) if x)
    run_dir = RESULTS_DIR / re.sub(r'[^\w\-가-힣]', '_', name)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'input.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    crew = DisclosureCrew(required_program_codes(payload), llm=llm, log_file=run_dir / 'crew_log.txt',
                          verbose=not args.quiet)
    feedback, start, review = '없음', 1, None
    for rnd in range(1, MAX_ROUNDS + 1):
        print(f'\n>>> Round {rnd}: Task{start}부터 실행')
        outputs = crew.run(build_inputs(payload, feedback), start_stage=start)
        problems = check_evidence(outputs['facts'], payload)
        write_round(run_dir, rnd, outputs, problems)

        review = ({'decision': '미검토', 'comment': '--no-review 실행'} if args.no_review
                  else review_fn(str(outputs['report']), problems))
        save_history({
            'rcept_no': payload.get('rcept_no'), 'corp_name': payload.get('corp_name'),
            'report_nm': payload.get('report_nm'), 'data_origin': payload.get('data_origin'),
            'round': rnd, 'decision': review['decision'], 'reject_reason': review.get('reason'),
            'importance': review.get('importance'), 'comment': review.get('comment'),
            'evidence_problems': len(problems), 'run_dir': _rel(run_dir),
            'reviewed_at': datetime.now().isoformat(timespec='seconds'),
        })
        if review['decision'] != '반려':
            break
        start = review['restart_stage']
        feedback = f"[{review['reason']}] {review['comment']}"
        if rnd == MAX_ROUNDS:
            print(f'\n재작업 {MAX_ROUNDS}회 한도에 도달했습니다. 담당자가 직접 검토하세요.')

    final = run_dir / 'final_report.md'
    final.write_text(
        str(outputs['report']).rstrip() + '\n\n---\n## 담당자 검토 결과\n'
        f"- 판단: {review['decision']}\n- 중요도: {review.get('importance') or '-'}\n"
        f"- 의견: {review.get('comment') or '-'}\n- 검토 회차: {rnd}\n",
        encoding='utf-8')
    print(f'\n완료: {_rel(final)}')
    return run_dir


if __name__ == '__main__':
    main()
