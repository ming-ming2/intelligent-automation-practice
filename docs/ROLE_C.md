# 역할 C 실행 안내 (CrewAI Agent · Task · Human 루프)

```text
B의 JSON(v1.0.0) → 공시 사실 추출 → 사업 변화·영향 분석 → 경쟁사 분석 보고서 작성
                        ↑ 수치가 필요할 때 Agent가 Program 도구 호출 (B의 계산 함수)
                 → Program 대조 3종 → 담당자 검토(승인/보류/반려) → 반려 사유별 재작업 → 저장
```

## 파일

| 파일 | 내용 |
| --- | --- |
| `crew.py` | Agent 3개, Task 3개, 출력 스키마(pydantic), Agent별 도구 연결, 사업 변화·영향 분석 가드레일, 부분 재실행 |
| `program_tools.py` | Agent가 호출하는 Program 도구 3개, Agent 출력과 Program 값의 대조 함수 |
| `main.py` | 입력 로딩, Crew 실행, Program 대조 3종, 담당자 검토 루프, 결과·이력 저장 |
| `tests/test_role_c.py` | 가짜 LLM으로 배선 검증 (OpenAI 호출·비용 없음) |

## Agent · Task (3주차 기획안 7페이지 대응)

| Agent | Task | 입력 | 출력 |
| --- | --- | --- | --- |
| ① Disclosure Analysis Agent | `extract_disclosure_facts` (공시 사실 추출) | 정규화 원문, 키워드 문맥 | `FactExtraction`: facts(원문 인용·접수번호), keyword_checks, unverifiable |
| ② Business Change Analyst | `analyze_business_changes` (사업 변화·영향 분석) | 공시 사실 추출 결과(context), 금액·재무 비교, 과거 사례, 검색 범위, Program 확인필요 | `ChangeAnalysis`: changes, interpretations(근거 id 필수), needs_verification, follow_up_checks |
| ③ Intelligence Report Agent | `write_intelligence_report` (경쟁사 분석 보고서 작성) | 공시 사실 추출·사업 변화·영향 분석 결과(context), 데이터 출처 안내 | 마크다운 보고서 (FACT / CHANGE / [해석] / 확인 필요 / 체크리스트) |

## Agent가 호출하는 Program 도구 (5주차, 교수 코멘트 반영)

4주차에는 B가 계산한 수치 전체(일반 사례 기준 약 4.4만 자)를 프롬프트에 붙였고, Agent가 그것을 읽어 옮겨 적었다.
개선 전 일반 사례에서 Agent가 연결 당기순이익 +49.1%를 "감소"로 쓰는 오류가 있었다(별도 기준 -66.5%와 혼동 추정).
5주차부터 프롬프트에는 숫자 없는 비교 항목 목록만 주고, Agent가 필요할 때 아래 도구로 값을 받아 쓴다.
도구는 LLM을 쓰지 않고 B의 `change_detector.compare_values`와 금액 단위 규칙으로 계산한다. B의 코드는 수정하지 않았다.

| 도구 | 사용 Agent | 돌려주는 값 |
| --- | --- | --- |
| `lookup_financial_change(metric, basis)` | ②, ③ | 연결/별도, 기간, 현재·이전 값, 변화율, 증감 방향, ±15% 기준 판정 |
| `lookup_amount_change(metric)` | ② | 공시 금액, 직전 동일 유형 대비 변화율, ±20% 판정, 비교 불가 사유 |
| `parse_amount_text(text)` | ①, ② | 원문 금액 표기의 원 단위 환산 (단일 숫자+단위만, 복합 단위는 해석 불가로 반환) |

## Program 검증 (고치지 않고 담당자에게 경고)

- FACT 인용 대조: 공시 사실 추출의 인용문이 원문·B 근거에 실제로 있는지, 접수번호가 입력에 있는지.
- CHANGE 변화율 대조: 분석 결과의 변화율이 같은 항목·기준의 Program 계산값과 같은지.
- 보고서 증감 방향 대조: 보고서 구절에서 매출·영업이익·순이익 뒤에 나온 증가/감소 표현이 Program 결과와 반대인지.
  '별도'가 있는 구절은 별도 기준, 없으면 연결 기준으로 본다. 개선 전 일반 보고서에 적용하면 요약·해석 2곳을 잡아낸다.
- 확인 필요 항목 보완: B가 넘긴 확인 필요 항목 코드가 Agent 출력에서 빠지면, Agent에게 재작성시키지 않고 B의 원래 코드·위치·문장을
  그대로 붙인다(문구 변경 없음, 판단 없음). 4주차 방식(재작성 요구, 3회 실패 시 중단)은 5주차 B의 `KEYWORD_EXCLUDED` 추가 후
  Agent가 해당 코드를 반복 누락해 일반 사례 실행이 중단되는 문제가 있었다(`results/after_일반_terminal.txt`).
- 신뢰도 '낮음' 해석과 공시 사실 추출의 확인 불가 항목에 대응하는 `AGENT_CHECK`가 없으면 Program이 추가한다.
- 반려 사유: 1 근거 오류 → 공시 사실 추출부터 / 2 비교·해석 오류 → 사업 변화·영향 분석부터 / 3 형식 오류 → 경쟁사 분석 보고서 작성만. 최대 3회.

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

- `results/<시각>_<label>_<접수번호>/`: input.json, roundN_extract_disclosure_facts.json, roundN_analyze_business_changes.json,
  roundN_write_intelligence_report.md, roundN_evidence_check.json, crew_log.txt(실행 로그), final_report.md(검토 결과 포함)
- `data/review.db`의 `review_history` 테이블: 회차별 판단·반려 사유·중요도·의견. A의 `dart.db`는 수정하지 않는다.

`--mock` 결과는 개발용이며 보고서 맨 위에 가상 데이터 경고가 붙는다. 제출용 테스트 3건은 실제 공시로 다시 실행한다.

Task 이름과 회차별 산출물 파일명은 수행 업무를 나타내는 이름을 사용한다. 기존 실행의 `roundN_task1_facts.json`, `roundN_task2_analysis.json`, `roundN_report.md`와 로그는 당시 기록으로 보존한다. 최종 보고서 이름은 `final_report.md`다.
