# B → C 입력 JSON 규격 v1.0.0

이 문서를 역할 B의 출력 및 역할 C의 입력 계약으로 사용한다. 기계 검증 규격은
[`../schemas/b_to_c.schema.json`](../schemas/b_to_c.schema.json), 개발용 예시는
[`../examples/mock_input.json`](../examples/mock_input.json)이다.

## 책임과 검증 범위

B는 정규화된 원문, 계산 결과, 키워드 등장 문맥, 과거 사례, 자료 부족 사유만 출력한다.
FACT / CHANGE / INTERPRETATION / NEEDS_VERIFICATION은 C의 Agent 출력이다.
키워드 등장은 사건 발생이나 중요도 판정이 아니다. 최종 판단은 사람에게 남긴다.

**개발용 가상 데이터는 최종 제출 결과로 사용할 수 없다. API 복구 후 실제 LG전자 공시로
일반·복잡·애매한 사례 최소 3건을 다시 실행하고 원문 대조 및 CrewAI 통합 결과를 검증한다.**
JSON 스키마 통과는 형식 검증일 뿐 실제 데이터 정확성이나 제출 검증 완료를 의미하지 않는다.
날짜의 실재 여부, 변화율 계산의 정확성, 근거의 실재 여부, 모든 하위 근거의 data_origin 일치,
정규화 길이와 본문의 일치 등 필드 간 의미 검증은 B의 구현 및 테스트에서 별도로 수행한다.

## 최상위 필드

모든 필드는 항상 존재한다. 없는 단일 값은 null, 없는 목록은 []로 표현한다.

| 필드 | 의미 |
| --- | --- |
| schema_version | 고정값 `1.0.0`. 호환성을 깨는 변경은 버전을 올린다. |
| data_origin | `synthetic` 개발용 가상 / `dart` 실제 DART / `mixed` 혼합. 현재·과거·재무·근거 전체에 적용한다. |
| rcept_no | 현재 공시 접수번호. 실제 데이터는 14자리 문자열, 가상은 `MOCK-` 접두사 사용. |
| corp_code, corp_name | 기업 고유번호(8자리 문자열), 기업명. |
| report_nm, rcept_dt | 보고서명, 접수일 `YYYYMMDD`. |
| normalized_text | C에게 전달하는 텍스트, 최대 15,000자. |
| normalization | 처리 상태, 전체 길이, 생략 여부, 선택 섹션. |
| source | 공시 URL, XML 경로 목록. 경로는 프로젝트 루트 기준 상대 경로를 권장한다. |
| triggers.amount_changes | 금액 항목별 비교 배열. 투자금액·양수금액 등을 따로 담는다. |
| triggers.financial_changes | 재무 계정별 비교 배열. 연결·별도 등을 섞지 않는다. |
| triggers.keywords | 키워드 등장 근거 배열. 부정문도 문맥 그대로 전달한다. |
| past_cases | 대상 공시 이전 동일 유형 사례. 과거 공시의 근거·금액 요약을 포함한다. |
| history_search | 3→5년 검색 범위, 3년 사례 수, 목록 수집 범위 충족 여부. |
| needs_verification | 기계 처리용 code, 대상 field, 사람이 읽는 message. |

## 비교 항목

`amount_changes`와 `financial_changes`는 같은 비교 객체 구조를 쓴다.

- `metric`: 비교 항목명. 금액은 표의 항목명, 재무는 `account_nm`.
- `status`: `evaluated` 또는 `not_comparable`.
- `triggered`: 계산할 수 있으면 true/false, 비교 불가이면 null.
- `reason`: 정상 계산이면 null, 비교 불가이면 이유 코드.
- `current`, `previous`: 값, 단위, 통화, 원문 표기, 근거. 금액은 **단위 환산 후 십진 문자열**로 저장한다.
  예: 1,300억원 → `value="130000000000", unit="원", currency="KRW"`.
  문자열을 쓰는 이유는 큰 금액 및 소수의 JSON 소비 과정에서 정밀도를 보존하기 위함이다.
- `change_rate`: 비율. `0.30`은 30%이며 30이 아니다. 비교 불가이면 null.
- `threshold`: 금액은 0.20, 재무는 0.15.
- `dimensions`: 금액은 null. 재무는 fs_div, sj_div, reprt_code, current_year,
  previous_year, current_period, previous_period, period_basis를 명시한다.
- `evidence`: 해당 값의 출처. XML은 접수번호·파일·섹션·정확한 발췌문,
  재무는 접수번호·`financial_accounts` 행 키·원본 필드명을 남긴다.

계산식은 `(current - previous) / abs(previous)`로 고정한다. 음수 기저에서도 증감 방향을
유지하기 위한 **프로젝트의 계산 규칙**이며, 표준 재무 해석을 대신하지 않는다. 음수 기저 또는
부호 전환은 needs_verification에도 추가한다. 임계값 판정은 반올림 전 Decimal 값으로
`abs(change_rate) >= threshold`를 적용한다. JSON 표시용 변화율만 소수점 6자리로 반올림한다.

이전 값 0, 누락, 단위 불명, 비교 항목 의미 불일치는 `not_comparable`로 처리한다.
0→0도 변화율 계산을 생략한다. 대시·빈 문자열을 0으로 바꾸지 않는다.
금액 의미나 단위가 명확하지 않으면 숫자만 추출해서 비교하지 않는다.

재무는 같은 계정·fs_div·sj_div·reprt_code·통화·단위·기간 기준을 맞춘다.
당기/전기 기간은 실제 원본 필드로 확인한다. 분기 단독과 누적을 혼합하지 않는다.
현재 공시 이후에 발표된 재무 자료를 과거 시점 분석에 사용하지 않는다.

## 근거 및 정규화

근거는 하나의 공시에 속하며 `rcept_no`를 반드시 포함한다. 문서 근거는 `kind=document`,
재무 DB 근거는 `kind=financial_account`로 구분한다. 가상 데이터의 파일도 실제 fixture가
있을 때만 경로를 기입한다. 존재하지 않는 파일 경로나 DART URL을 만들지 않는다.

`normalization.status`는 `ok / missing / error`. 원문 누락·파싱 실패는 빈 텍스트와
needs_verification 사유로 처리한다. `original_length`는 전체 정규화 텍스트 길이,
`truncated`는 전달 텍스트에서 내용이 생략되었는지 여부다.

키워드 검색과 금액 추출은 길이 제한 전 전체 텍스트에서 수행한다. C에 전달된 본문에서
생략된 부분의 근거도 각 evidence.quote에 보존한다. 경로는 `documents.files_json`을 읽고
현재 작업 디렉터리가 아닌 프로젝트 루트 기준으로 해석한다.

## 과거 사례 검색

- 기준은 현재 공시일이다. 현재 공시와 미래 공시는 제외한다. 같은 날의 선후를 확인할 수
  없으면 과거 사례로 자동 포함하지 않는다.
- 같은 기업·정규화한 보고서 유형으로 찾는다. 단순히 A/B/I 공시 분류만 같다고 유사 사례가 아니다.
- 보고서명에서 정정 접두사·정기보고서 기간만 정규화하고, `합병`·`양수` 등 세부 유형은 보존한다.
- 원공시와 정정공시의 연결을 확인한 경우에만 한 사건으로 센다. 연결이 불명확하면 검토 대상으로 남긴다.
- 과거 3년의 고유 사례가 2건 미만이면 총 검색 창을 5년으로 확장한다. 3년 결과와 5년 결과를
  중복 누적하지 않는다. 5년 전체에서도 2건이 안 되면 부족한 상태 그대로 반환한다.
- `history_search.coverage_status`는 `complete / incomplete / unknown`. 이것은 **공시 목록의
  검색 범위**만 의미한다. 원문 확보 여부는 각 사례의 source와 needs_verification에서 확인한다.
- DB 최소 날짜만으로 수집 완료를 추정하지 않는다. `list_cursors` 등의 수집 이력으로
  대상 유형 전체 기간이 확인될 때만 complete로 표시한다.
- 목록 범위가 부족하면 `past_cases=[]`를 “과거 사건 없음”으로 해석하지 않는다.

## 권장 사유 코드

`DOCUMENT_MISSING`, `XML_PARSE_ERROR`, `CURRENT_VALUE_MISSING`, `PREVIOUS_VALUE_MISSING`,
`PREVIOUS_VALUE_ZERO`, `UNIT_UNKNOWN`, `COMPARISON_MISMATCH`, `NEGATIVE_BASE`,
`SIGN_CHANGE`, `FINANCIAL_DATA_MISSING`, `HISTORY_COVERAGE_INCOMPLETE`,
`HISTORY_COVERAGE_UNKNOWN`, `CORRECTION_LINK_UNCONFIRMED`.

예: 금액이 없으면 value=null, change_rate=null, triggered=null로 반환하고
needs_verification에 원인을 추가한다. 비교 대상으로 선택할 재무 계정 자체가 없으면
financial_changes=[]와 FINANCIAL_DATA_MISSING을 반환한다.

## C에게 전달하는 방식

```python
import json

# C의 Task description에서 {disclosure_json}을 참조하도록 맞춘다.
crew.kickoff(inputs={"disclosure_json": json.dumps(payload, ensure_ascii=False)})
```

C는 data_origin을 보고 개발 실행 여부를 보고서에도 표시한다. 근거가 없거나 비교 불가한
항목을 확정 사실로 바꾸지 않는다. B의 needs_verification을 C의 NEEDS_VERIFICATION에 전달한다.

## 제출용 검증 기록은 별도 관리

B의 입력 JSON에는 `submission_ready=true` 같은 자기 승인 필드를 두지 않는다.
D가 실제 공시 3건에 대해 접수번호, 입력 JSON, 실행 로그, Agent별 출력, 최종 보고서,
기대 결과 대조, 원문 대조 결과, 잘된 점·문제점을 별도 결과 파일로 기록한다.
`data_origin=dart`는 출처 표시일 뿐 검증 완료 표시가 아니다. 가상 정보가 하나라도 섞이면
mixed이며 제출용 실제 검증 3건에 포함할 수 없다. 개발 fixture 테스트는 이 실증을 대체하지 못한다.
