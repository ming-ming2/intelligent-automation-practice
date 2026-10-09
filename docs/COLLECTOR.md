# OpenDART 수집 모듈 사용 안내

관련 코드: `config.py`, `dart_collector.py`.
원문 정규화·변화 탐지·CrewAI 실행은 이 모듈의 범위에 포함하지 않습니다.

## 실행

프로젝트 최상위에서 가상환경 활성화 후 실행합니다. `.env`의 `DART_API_KEY`가 필요합니다.

전체 수집:

```sh
python dart_collector.py --company LG전자 --start 20220101 --end 20251231
```

Python 함수로도 실행할 수 있습니다.

```python
from dart_collector import collect
summary = collect('LG전자', '20220101', '20251231')
print(summary)
```

기본 공시유형은 A(정기공시), B(주요사항보고), I(거래소공시)입니다.
`--types`로 변경할 수 있습니다. 거래소공시 중 OpenDART 원문이 제공되지 않는 건은
목록을 보존하고 원문 상태를 `failed`로 기록합니다. 분석 대상 여부는 후속 분석 단계에서 결정합니다.

원문 몇 건을 먼저 수집하려면:

```sh
python dart_collector.py --start 20220101 --end 20251231 --max-documents 3 --skip-financials
```

`--max-documents`는 이번 실행의 다운로드 **시도 수**입니다. 실패하면 성공 건수는 더 적을 수 있습니다.
재실행하면 이미 있는 원문을 건너뛰고 남은 원문부터 진행합니다.
목록은 지정 기간 전체를 저장하므로, 후속 모듈에서는 `document_status='downloaded'`인 자료를 먼저 읽으면 됩니다.

최소 실제 검증용:

```sh
python dart_collector.py --start 20250301 --end 20250331 --types A --max-documents 3 --financial-years 2024 --report-codes 11011
```

재무 수집 기본값은 종료일의 연도를 기준으로 3~5개 사업연도이며, 2015년 이전은 제외합니다.
`20220101~20251231`의 기본 재무 범위는 2022~2025년입니다.
재무 보고서는 1분기(11013), 반기(11012), 3분기(11014), 사업보고서(11011)를 모두 조회합니다.
사업연도와 공시 접수연도는 다릅니다. 2025년 재무의 사업보고서가 2026년에 제출되었어도
재무 API에서는 사업연도 2025로 조회합니다. 공시 목록의 접수 기간과 별개입니다.

## 재실행·오류 처리

- 공시 목록의 기본키는 접수번호입니다. 같은 공시는 중복 삽입하지 않습니다.
- 최초 조회 후 공시유형·시작일별 마지막 확인 날짜를 저장합니다. 다음 조회는 그 날짜를 포함해 이후 기간만 요청합니다.
- `--refresh-disclosures`는 전체 기간을 다시 확인합니다. 과거 자료가 나중에 추가되었거나 전체 재확인이 필요할 때 사용하세요.
- 목록 조회가 실패하면 그 조회 범위의 확인 날짜는 갱신하지 않습니다.
- 원문은 목록 조회와 별도로 상태를 기록합니다. 실패한 원문과 로컬 파일이 누락된 원문은 재시도합니다.
- 성공한 재무 조회는 재실행 시 건너뜁니다. 정정 재무를 다시 받으려면 `--refresh-financials`를 사용합니다.
- `013`은 자료 없음으로 처리합니다. 재무 자료 없음은 영구적으로 완료 처리하지 않고 다음 실행에서 다시 확인합니다.
- `014`는 원문 없음으로 기록합니다. `010/011/012/020/901`은 수집을 중단합니다.
- `800`은 시스템 점검으로 중단합니다. 점검 중에는 완료된 것으로 표시하지 않습니다.
- 네트워크 실패와 HTTP 429/5xx는 제한된 횟수만 재시도합니다. 인증키가 들어 있는 URL은 로그에 출력하지 않습니다.
- 로컬 호출 수는 KST 날짜·키 식별 해시별로 DB에 누적하며 기본 20,000건을 넘기지 않습니다.
  같은 키를 다른 컴퓨터나 도구에서 사용한 요청은 포함할 수 없으므로 서버의 `020`도 별도로 처리합니다.

선택 설정: `DART_TIMEOUT`(기본 30초), `DART_REQUEST_INTERVAL`(0.2초),
`DART_MAX_RETRIES`(2회), `DART_DAILY_LIMIT`(20,000건).
호출 한도는 인증키별 실제 설정에 따라 다를 수 있습니다.

## SQLite 구조

| 테이블 | 용도 | 주요 컬럼 |
| --- | --- | --- |
| companies | 고유번호 캐시 | corp_code, corp_name, corp_eng_name, stock_code, modify_date, raw_json |
| disclosures | 공시 목록·원문 수집 상태 | rcept_no, corp_code, report_nm, rcept_dt, pblntf_ty, source_url, document_status, document_error, metadata_json |
| documents | 원문 ZIP·해제 파일 위치 | rcept_no, zip_path, files_json, sha256 |
| financial_accounts | 재무 원본 계정 | corp_code, bsns_year, reprt_code, fs_div, sj_div, account_nm, thstrm_amount, frmtrm_amount, bfefrmtrm_amount, currency, raw_json |
| financial_queries | 연도·보고서별 수집 상태 | corp_code, bsns_year, reprt_code, status |
| list_cursors | 목록 증분 조회 범위 | corp_code, pblntf_ty, scope_start, checked_through |
| api_usage | 로컬 일일 호출 수 | request_date, key_id, requests |

금액은 쉼표·대시를 포함한 원본 문자열 그대로 저장합니다. `raw_json`에는 계정의 모든 응답 필드가 남아 있습니다.
재무 비교 시 연결(CFS)·별도(OFS), 보고서 기간과 통화를 맞추고, 대시를 임의로 0으로 바꾸지 않아야 합니다.

API 필드 보존 위치:

- 기업 목록: `companies.raw_json`에 XML의 각 기업 항목을 원래 필드명으로 저장합니다. `corp_eng_name`도 별도 컬럼으로 저장합니다.
- 공시 목록: `disclosures.metadata_json`에 `list.json`의 각 공시 항목 전체를 저장합니다. `stock_code`, `corp_cls`, `flr_nm`, `rm`도 여기에서 읽습니다.
- 재무: `financial_accounts.raw_json`에 각 계정 항목 전체를 저장합니다. `thstrm_dt`, `frmtrm_dt`, `thstrm_add_amount`, `frmtrm_add_amount` 등은 JSON을 파싱해 읽습니다.
- 원문: ZIP과 압축 해제한 XML bytes를 그대로 보존합니다. 파일 경로와 수집 상태는 DB에 추가로 기록합니다.

`pblntf_ty`는 조회에 사용한 공시유형을 별도로 기록한 값입니다. 원본 API 항목을 바꾸지 않습니다.
응답의 `status`, `message`, 페이지 정보는 요청 처리에 사용하며, 전체 응답 묶음 자체를 DB에 저장하는 방식은 아닙니다.

기존 DB에는 기업 영문명·원본 JSON 컬럼을 자동 추가합니다. 이미 저장된 기업 항목의 새 필드는
실제 API에 접근할 수 있을 때 `--refresh-companies`로 다시 조회해야 채워집니다.

공시 원문 읽기 예시:

```python
import json
import sqlite3
from pathlib import Path
from config import ROOT, load_settings

settings = load_settings()
with sqlite3.connect(settings.db_path) as db:
    db.row_factory = sqlite3.Row
    rows = db.execute('''
        SELECT d.rcept_no, d.report_nm, d.rcept_dt, d.source_url, f.files_json
        FROM disclosures d JOIN documents f USING(rcept_no)
        WHERE d.document_status='downloaded'
        ORDER BY d.rcept_dt
    ''').fetchall()
    for row in rows:
        for stored in json.loads(row['files_json']):
            path = Path(stored)
            path = path if path.is_absolute() else ROOT / path
            if path.suffix.lower() == '.xml':
                print(row['rcept_no'], path)
                # 원문 정규화 모듈에 경로 또는 원본 bytes를 전달합니다.
```

기본 설정으로 수집한 파일 경로는 프로젝트 기준 상대 경로입니다.
데이터를 공유할 때는 `data/dart.db`와 `data/raw/`를 함께 전달하고 같은 상대 폴더 구조를 유지하세요.
수집 중에는 DB를 복사하지 말고 실행이 종료된 뒤 복사합니다. API 키는 전달하지 않습니다.
환경변수에 외부 절대 경로를 지정했다면 데이터를 받는 환경에 맞춰 경로를 조정해야 합니다.

## 현재 검증 상태

2026-10-09 기준 실제 수집은 `corpCode.xml`이 `800`을 반환하여 중단되었습니다.
별도의 `company.json` 조회에서는 LG전자 고유번호 `00401731`, 종목코드 `066570`과 정상 응답을 확인했습니다.
실제 공시 목록·원문·재무 전체 수집 성공은 아직 검증하지 못했습니다.
OpenDART 공식 공지의 점검 예정 종료는 **2026-10-11 18:00 KST**이며 고유번호·공시 원문 API가 중단 대상입니다.
서비스 재개 후 최소 검증용 명령을 두 번 실행해 DB 건수와 원문을 확인하고, 그 다음 전체 수집을 실행해야 합니다.
실제 공시 3건·CrewAI 실행 평가는 전체 워크플로 통합 후 수행합니다.

공식 문서:

- [고유번호](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018)
- [공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)
- [공시 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003)
- [단일회사 주요계정](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019016)
- [점검 안내](https://opendart.fss.or.kr/)
