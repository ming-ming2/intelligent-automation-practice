# 역할 B 실행 안내

역할 B는 API와 LLM을 호출하지 않는다. 정규화·수치 계산·근거·확인 필요 사항을 만들고,
C가 이를 받아 사실·변화·해석을 작성한다. Agent를 추가하지 않았다.

```text
A의 SQLite + XML → normalizer.py → change_detector.py → v1 JSON → C의 CrewAI
```

## 실행

프로젝트 폴더에서 기존 가상환경을 사용한다. API 키는 필요 없다.
테스트 의존성이 없으면 `python -m pip install -r requirements-dev.txt`로 설치한다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_role_b.py -v
.\.venv\Scripts\python.exe change_detector.py 실제접수번호 --db data/dart.db --data-origin dart
```

Python에서:

```python
from change_detector import build_payload
payload = build_payload("실제접수번호", "data/dart.db", data_origin="dart")
```

`data_origin`은 필수다. 개발용 DB는 synthetic, 혼합 자료는 mixed로 설정한다.
가상 식별자는 MOCK- 접두사를 쓴다. 숫자로 된 가짜 접수번호는 자동 판별할 수 없으므로
호출자가 자료 출처를 정확히 지정해야 한다. 실제 검증 완료 여부를 자동 승인하지 않는다.

DB 없이도 `normalize_xml(path_or_bytes)`와
`detect_changes(current, norm, past_disclosures, financials, cursors, data_origin=...)`로 실행할 수 있다.
입력 구성 예시는 `tests/test_role_b.py`의 disclosure/financial 함수와 3개 사례를 참고한다.

## 구현한 기준

- 표의 빈 셀과 행을 보존하고 상위 섹션 제목을 하위 내용에 전달한다.
- 긴 문서는 관련 섹션을 먼저 선택하며 여러 XML 전체에서 전달 본문을 15,000자로 제한한다.
- 금액·키워드 탐지는 자르기 전 기록에서 수행하고 정확한 원문 발췌를 별도 보존한다.
- 직전 같은 보고서 유형의 금액 대비 ±20%, 직접 연결된 재무 계정 전년 동기 대비 ±15%.
- Decimal로 임계값을 판정한다. 누락·0 분모·기간 불명은 null과 사유로 반환한다.
- 신규사업/중단/양수/분할/합병 등장 문맥을 반환한다. 부정문을 사건 발생으로 단정하지 않는다.
- 달력 기준 과거 3년의 사례가 2건 미만이면 5년으로 확장한다. 정기보고서 기간은 유형에서 분리한다.
- 원공시 연결이 확인되지 않은 정정공시는 사례 수에서 제외하고 관련 금액 비교는 보류한다.
- 직전 원문이 없어도 더 오래된 금액으로 대체하지 않는다. 현재 이후 공시는 비교하지 않는다.
- DB는 읽기 전용으로 열며 pending/failed 원문을 정상 수집 자료로 사용하지 않는다.

## 현재 제한과 실제 데이터 검증

의미가 확실한 항목 바로 뒤의 단일 금액만 계산한다. 병합 표, 표 상단에만 단위가 있는 값,
복합 단위(1조 3,000억원), 반복 항목·정정 전후 복수 열은 AMOUNT_UNRESOLVED로 보류한다.
실제 XML 샘플의 구조를 확인한 후 필요한 표 형식만 지원을 추가한다.

재무는 현재 접수번호에 직접 연결된 계정만 사용한다. 주요사항보고서에 재무 계정이 없으면
FINANCIAL_DATA_MISSING을 반환한다. 과거 재무를 임의로 현재 공시의 재무처럼 붙이지 않는다.
원본 thstrm_dt/frmtrm_dt로 전년 동기를 확인하지 못하면 PERIOD_UNVERIFIED로 보류한다.
타 공시 재무를 참고자료로 쓰려면 C와 별도 선택 기준을 정해야 한다.

엄격한 XML 파싱 실패와 외부 엔티티는 오류로 남긴다. 손상 문서를 조용히 복원해 분석하지 않는다.
같은 날의 선후는 판단하지 않아 과거 비교에서 제외한다. 수집 범위는 list_cursors로 확인한다.

**테스트 파일의 일반·복잡·애매한 사례는 전부 개발용 가상 데이터다. 제출 결과가 아니다.**
API 복구 및 A의 수집 성공 후 실제 LG전자 공시 최소 3건을 다시 실행한다.
접수번호·원문·JSON·CrewAI 로그·보고서·기대 결과 대조·잘된 점·문제점을 D와 함께 기록한다.

PPT 1장에는 위 흐름과 20%/15%/키워드/3→5년 기준을, 5장에는 표 구조·자료 누락·정정 연결·
재무 기간 검증의 제한 및 실제 공시 검증 결과를 설명하면 된다.
