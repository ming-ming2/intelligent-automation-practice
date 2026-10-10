# 역할 C 실행 안내 (CrewAI Agent · Task · Human 루프)

```text
B의 JSON(v1.0.0) → Task1 FACT 추출 → Task2 변화·영향 분석 → Task3 보고서 초안
                 → Program 근거 대조 → 담당자 검토(승인/보류/반려) → 반려 사유별 재작업 → 저장
```

## 파일

| 파일 | 내용 |
| --- | --- |
| `crew.py` | Agent 3개, Task 3개, 출력 스키마(pydantic), Task2 가드레일, 부분 재실행 |
| `main.py` | 입력 로딩, Crew 실행, 근거 대조, 담당자 검토 루프, 결과·이력 저장 |
| `tests/test_role_c.py` | 가짜 LLM으로 배선 검증 (OpenAI 호출·비용 없음) |

## Agent · Task (3주차 기획안 7페이지 대응)

| Agent | Task | 입력 | 출력 |
| --- | --- | --- | --- |
| ① Disclosure Analysis Agent | task1_fact_extraction | 정규화 원문, 키워드 문맥 | `FactExtraction`: facts(원문 인용·접수번호), keyword_checks, unverifiable |
| ② Business Change Analyst | task2_change_analysis | Task1 결과(context), 금액·재무 비교, 과거 사례, 검색 범위, Program 확인필요 | `ChangeAnalysis`: changes, interpretations(근거 id 필수), needs_verification, follow_up_checks |
| ③ Intelligence Report Agent | task3_intelligence_report | Task1·2 결과(context), 데이터 출처 안내 | 마크다운 보고서 (FACT / CHANGE / [해석] / 확인 필요 / 체크리스트) |

- Task2 가드레일: B의 `needs_verification` 코드가 하나라도 빠지면 최대 2회 재작성시킨다.
- Program 근거 대조: Task1의 인용문이 원문·B 근거에 실제로 있는지, 접수번호가 입력에 있는지 검사해 담당자에게 경고한다.
- 반려 사유: 1 근거 오류 → Task1부터 / 2 비교·해석 오류 → Task2부터 / 3 형식 오류 → Task3만. 최대 3회.

## 실행

```sh
source .venv/bin/activate
python tests/test_role_c.py                       # 배선 테스트 (키 불필요)
python main.py --mock                             # 가상 입력으로 실제 LLM 실행
python main.py --input 경로.json --label test2    # B가 만든 JSON 파일
python main.py --rcept-no 실제접수번호 --data-origin dart --label test1_일반
python main.py --mock --no-review                 # 사람 검토 없이 1회
python main.py --mock --quiet                     # 진행 로그 생략 (crew_log.txt에는 저장)
```

## 저장 위치

- `results/<시각>_<label>_<접수번호>/`: input.json, roundN_task1_facts.json, roundN_task2_analysis.json,
  roundN_report.md, roundN_evidence_check.json, crew_log.txt(실행 로그), final_report.md(검토 결과 포함)
- `data/review.db`의 `review_history` 테이블: 회차별 판단·반려 사유·중요도·의견. A의 `dart.db`는 수정하지 않는다.

`--mock` 결과는 개발용이며 보고서 맨 위에 가상 데이터 경고가 붙는다. 제출용 테스트 3건은 실제 공시로 다시 실행한다.
