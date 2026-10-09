# OpenDART 실제 API 검증 체크리스트

## 진행 방침

팀은 먼저 API 호출 없이 각 모듈의 입출력 규격을 맞추고 수집·DB 저장과 후속 모듈을 구현합니다.
OpenDART 서비스 재개 후 실제 API 호출과 저장 결과를 검증합니다.
이 문서는 수집 모듈의 실제 API 검증 위치와 확인 사항을 정리합니다.

- 대상 기업: LG전자
- 확인한 DART 고유번호: `00401731` (8자리 문자열)
- 종목코드: `066570` (고유번호와 구분)
- 완료 기준: `collect("LG전자", "20220101", "20251231")`로 공시 목록·원문·재무를 저장하고 재실행 시 중복 없이 신규 자료를 추가

## 현재 코드 상태

`config.py`와 `dart_collector.py`에는 API 요청·응답 처리와 DB 저장 코드가 작성되어 있습니다.
실제 공시·원문·재무 전체 수집 검증은 완료되지 않았습니다.
현재 `dart_collector.py`의 일반 CLI 실행은 실제 API에 접근합니다.

## API 없이 먼저 맞출 데이터 규격

| 구분 | API 응답 형식 | 유지할 주요 항목 |
| --- | --- | --- |
| 기업 목록 | `corpCode.xml`의 ZIP 내 기업 목록 XML | corp_code, corp_name, corp_eng_name, stock_code, modify_date |
| 공시 목록 | `list.json`의 JSON | status, total_page, list, rcept_no, corp_code, corp_name, report_nm, rcept_dt |
| 공시 원문 | `document.xml`의 ZIP 내 XML 파일 | 접수번호별 파일 경로와 수집 상태 |
| 재무 주요계정 | `fnlttSinglAcnt.json`의 JSON | status, list, bsns_year, reprt_code, rcept_no, fs_div, sj_div, account_nm, 금액, currency |

수집 모듈과 후속 모듈 사이의 공시·재무 필드, SQLite 테이블과 원문 파일 경로를 맞춥니다.
원문 미수집 상태와 `013`(조회 결과 없음), 누락된 금액의 처리 방식을 정합니다.
실제 데이터가 확보되면 이 규격에 따라 저장하고 후속 모듈에서 읽습니다.
기업·재무 항목은 `raw_json`, 공시 항목은 `metadata_json`에서 API 원본 필드명으로 읽습니다.
각 항목의 보존 위치와 원문 읽기 방법은 [수집 모듈 안내](COLLECTOR.md)를 참고하세요.

## 기능별 실제 검증 항목

### 1. 설정과 공통 HTTP 요청

검증 대상 코드:

- `config.py`: `load_settings()`
- `dart_collector.py`: `DartCollector.__init__()`, `_request()`, `_json()`, `_zip()`

확인 항목:

- [ ] 구현된 `requests.Session` 요청 경로를 실제 API로 검증한다.
- [ ] `.env`의 `DART_API_KEY`를 사용한다.
- [ ] 공식 API 서버에 접근 가능한지 확인한다.
- [ ] 네트워크 시간 초과, HTTP 429/5xx, OpenDART 오류 코드가 처리되는지 확인한다.
- [ ] API 키를 콘솔·로그·DB·Git에 기록하지 않는지 확인한다.
- [ ] 로컬 일일 호출 횟수 제한이 적용되는지 확인한다.

### 2. 회사명에서 고유번호 조회

검증 대상 코드: `get_corp_code(company_name, refresh=False)`

API: `corpCode.xml` → ZIP 해제 → 기업 목록 XML 검색

- [ ] 실제 기업 목록에서 LG전자를 찾아 `00401731`인지 확인한다.
- [ ] 회사명 표기와 캐시 조회가 맞는지 확인한다.
- [ ] 고유번호·종목코드의 앞자리 0을 보존한다.

이 단계는 기업 목록에서 회사를 찾는 과정입니다.
회사명·대표자 등을 반환하는 `company.xml/json` 기업개황 조회와 구분합니다.

### 3. 기간·공시유형별 목록 조회

검증 대상 코드: `fetch_disclosures()`, `_save_disclosures()`, `collect()`의 목록 조회 부분

API: `list.json`

주요 요청값: `corp_code`, `bgn_de`, `end_de`, `pblntf_ty`, `page_no`, `page_count`, `last_reprt_at`

- [ ] 실제 응답의 모든 페이지를 조회한다.
- [ ] 정정 공시를 포함하도록 `last_reprt_at=N`을 유지한다.
- [ ] `disclosures`에 접수번호를 기본키로 저장한다.
- [ ] 처음에는 전체 기간을, 이후에는 `list_cursors`의 마지막 확인 날짜부터 조회한다.
- [ ] 마지막 확인 날짜와 같은 날에 추가된 공시도 놓치지 않는지 확인한다.
- [ ] `013`은 빈 목록으로 처리하고, 실패한 조회의 확인 날짜는 갱신하지 않는다.

### 4. 접수번호별 공시 원문 다운로드

검증 대상 코드: `download_document()`, `document_available()`, `collect()`의 원문 수집 부분

API: `document.xml` (`rcept_no`로 요청, ZIP 응답)

- [ ] 실제 원문 ZIP을 받는다.
- [ ] 압축 안의 파일명·인코딩·XML 형식을 확인한다.
- [ ] `data/raw/<접수번호>/`에 ZIP과 해제한 파일을 저장한다.
- [ ] `documents`에 파일 경로를, `disclosures`에 원문 수집 상태를 기록한다.
- [ ] 다운로드 실패와 파일 없음(`014`)은 목록을 보존하고 다음 실행에서 다시 시도한다.
- [ ] 원문 정규화 모듈이 실제 XML을 읽을 수 있는지 샘플 몇 건으로 확인한다.

### 5. 최근 3~5개년 재무 주요계정 수집

검증 대상 코드: `fetch_financial_accounts()`, `_save_financials()`, `collect()`의 재무 수집 부분

API: `fnlttSinglAcnt.json`

주요 요청값: `corp_code`, `bsns_year`, `reprt_code`

- [ ] 1분기 `11013`, 반기 `11012`, 3분기 `11014`, 사업보고서 `11011`을 구분한다.
- [ ] 연결 `CFS`와 별도 `OFS`를 모두 구분하여 보존한다.
- [ ] 금액·단위·기간과 응답 원본 `raw_json`을 저장한다.
- [ ] 자료 없음(`013`)은 0으로 만들지 않고, 다음 실행에서 재조회할 수 있게 한다.
- [ ] 정상 수집한 연도·보고서는 중복 요청을 건너뛴다.
- [ ] 정정 재무를 갱신할 때 `--refresh-financials`로 재조회할 수 있는지 확인한다.

## 실제 API 검증 순서

1. 기업 목록에서 LG전자 고유번호를 조회하는 경로를 검증한다.
2. 좁은 기간의 공시 목록과 원문 몇 건, 재무 한 보고서를 수집한다.
3. API 응답과 DB에 저장한 접수번호·수치·원문 파일을 대조한다.
4. 같은 범위를 다시 실행하여 중복이 생기지 않는지 확인한다.
5. 실패하거나 보류된 원문이 다음 실행에서 다시 처리되는지 확인한다.
6. `20220101~20251231` 전체 수집을 실행한다.
7. 실행이 종료된 뒤 DB·원문·사용 방법을 공유하고 후속 모듈에서 읽을 수 있는지 확인한다.

실제 입력의 금액·연결 구분·누락 값에 따라 후속 처리가 달라질 수 있으므로,
실제 API 검증에는 응답 형식과 저장 결과, 후속 모듈의 데이터 읽기 확인까지 포함합니다.

## 공식 문서

- [고유번호](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018)
- [기업개황](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019002)
- [공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- [공시 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003)
- [단일회사 주요계정](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019016)

현재 코드의 실행 옵션과 DB 구조는 [수집 모듈 안내](COLLECTOR.md)를 참고하세요.
