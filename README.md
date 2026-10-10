# intelligent-automation-practice
지능형자동화실습 팀플 — 경쟁사 공시 변화 탐지 및 분석 보고서 자동화

지능형자동화실습 팀 응소.net의 프로젝트입니다. 개발 환경, 프로젝트 구조와 실행 방법을 안내합니다.
원본 기획안: [기획안 PPT](docs/기획안.pptx)

입출력 계약: [B → C JSON 규격 v1.0.0](docs/B_TO_C_CONTRACT.md).
구현과 테스트 방법: [원문 정규화·변화 탐지](docs/ROLE_B.md), [CrewAI Agent·Task·담당자 검토](docs/ROLE_C.md).
[개발용 가상 입력](examples/mock_input.json)은 인터페이스 예시이며 실제 공시 검증 결과가 아닙니다.
제출용 테스트 3건은 API 복구 후 실제 LG전자 공시로 다시 실행하고 검증해야 합니다.

## 팀 공통 개발 환경

Python **3.12**와 각자의 **`.venv`**를 사용합니다. Docker는 현재 필수가 아닙니다.
공통 패키지는 `requirements.txt`로 설치하고, `.venv` 폴더 자체는 공유하지 않습니다.
SQLite는 Python 표준 라이브러리이므로 별도 DB 서버나 설치가 필요하지 않습니다.

### 최초 설치

프로젝트 최상위 폴더에서 실행합니다. Python 3.12가 먼저 설치되어 있어야 합니다.

macOS / Linux:

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Windows에서 활성화가 필요하면 `.\.venv\Scripts\Activate.ps1`을 사용합니다.
활성화하지 않아도 위처럼 가상환경의 Python을 직접 실행할 수 있습니다.
VS Code 등 에디터에서도 프로젝트의 `.venv` 인터프리터를 선택하세요.

### API 키 설정

`.env`가 **없는 경우에만** `.env.example`을 복사해 `.env`를 만듭니다.
이미 `.env`가 있으면 내용을 덮어쓰지 말고 필요한 변수만 추가합니다.

```dotenv
DART_API_KEY=개인_OpenDART_인증키
OPENAI_API_KEY=개인_OpenAI_API_키
```

OpenDART 데이터 수집에는 `DART_API_KEY`, OpenAI 기반 Agent 실행에는 `OPENAI_API_KEY`가 필요합니다.
키가 없어도 패키지 설치와 환경 점검은 할 수 있습니다.
실제 키와 `.env`는 공유하지 않고, 변수명과 기본값만 `.env.example`로 공유합니다.

### 공통 경로

| 용도 | 기본 경로 | 환경변수 |
| --- | --- | --- |
| SQLite DB | `data/dart.db` | `DART_DB_PATH` |
| 공시 원문 | `data/raw/` | `DART_RAW_DIR` |
| 실행 결과·보고서 | `results/` | `RESULTS_DIR` |
| 담당자 검토 이력 | `data/review.db` | - |
| CrewAI 모델 | `openai/gpt-4o-mini` | `CREWAI_MODEL` |

이 경로는 팀 공통 기본값입니다. 각 모듈에서 변수를 읽고, 상대 경로는
현재 터미널 위치가 아닌 **프로젝트 최상위 폴더 기준**으로 해석해야 합니다.
DB와 원문은 Git에서 제외하며, 필요한 자료는 같은 폴더 구조를 유지하여 별도로 공유합니다.
실행 결과를 커밋할 때는 API 키가 포함되지 않았는지 확인합니다.

### 환경 점검

macOS / Linux에서 가상환경 활성화 후:

```sh
python -m pip check
python scripts/check_environment.py
```

Windows에서는:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/check_environment.py
```

점검 스크립트는 Python 버전, 패키지 import, SQLite 동작과 키 설정 여부를 확인합니다.
키 값은 출력하지 않으며 외부 API를 호출하거나 키의 유효성을 검증하지 않습니다.
CrewAI 내부 저장 경로는 프로젝트의 `.cache/crewai`로 설정합니다.
Agent 실행 코드에서도 `.env` 로딩과 저장 경로 설정을 **CrewAI import 전에** 적용하세요.

## 프로젝트 구조

현재 저장소의 주요 파일과 경로입니다. DB와 원문 파일은 수집 시 생성됩니다.

```text
intelligent-automation-practice/
├── config.py                   # 환경변수 및 공통 설정
├── dart_collector.py           # OpenDART 수집·SQLite 저장 (역할 A)
├── normalizer.py               # 공시 원문 정규화 (역할 B)
├── change_detector.py          # 변화 후보 탐지, C 입력 JSON 생성 (역할 B)
├── crew.py                     # CrewAI Agent 3개·Task 3개 정의 (역할 C)
├── main.py                     # 전체 실행, 담당자 검토, 결과·이력 저장 (역할 C)
├── requirements.txt            # 공통 패키지
├── .env.example                # 환경변수 템플릿
├── scripts/
│   └── check_environment.py    # 개발 환경 점검
├── examples/
│   └── mock_input.json         # 개발용 가상 입력
├── schemas/
│   └── b_to_c.schema.json      # B → C 입력 JSON 스키마
├── tests/
│   ├── test_role_b.py          # 정규화·변화 탐지 테스트
│   └── test_role_c.py          # Agent·Task 연결 구조 테스트 (가짜 LLM, 비용 없음)
├── data/
│   ├── dart.db                 # 수집 시 생성되는 DB (Git 제외)
│   ├── review.db               # 담당자 검토 이력 DB (Git 제외)
│   └── raw/                    # 수집 원문 ZIP·XML (Git 제외)
├── results/                    # 실행 결과·보고서
└── docs/
    ├── 기획안.pptx             # 프로젝트 기획안
    ├── COLLECTOR.md            # 수집 옵션·DB 구조
    ├── API_INTEGRATION_TODO.md # 실제 API 검증 체크리스트
    ├── B_TO_C_CONTRACT.md      # B → C JSON 규격
    ├── ROLE_B.md               # 정규화·변화 탐지 안내
    └── ROLE_C.md               # CrewAI 실행 안내
```

원문 정규화(normalizer.py)와 변화 탐지(change_detector.py)는 역할 B에서 구현되었습니다.
CrewAI Agent 실행(crew.py)과 전체 실행 진입점(main.py)은 역할 C에서 구현되었습니다.
현재는 개발용 가상 데이터로 단위 테스트와 Agent 실행, 담당자 반려·재작업 흐름을 확인했으며,
OpenDART API 복구 후 실제 LG전자 공시로 추가 검증할 예정입니다.

## 실행 방법과 구현 상태

`.env` 설정 후 가상환경에서 실행합니다.

1) 데이터 수집

```sh
python dart_collector.py --company LG전자 --start 20220101 --end 20251231
```

실행 옵션과 저장 구조는 [수집 모듈 안내](docs/COLLECTOR.md)를 참고하세요.

2) 분석 보고서 생성과 담당자 검토 (`OPENAI_API_KEY` 필요)

```sh
python tests/test_role_c.py                                   # 연결 구조 테스트 (키 불필요)
python main.py --mock                                         # 개발용 가상 입력으로 실행
python main.py --mock --quiet                                 # 진행 로그 생략
python main.py --rcept-no 실제접수번호 --data-origin dart --label test1_일반   # DB의 실제 공시로 실행
```

실행하면 Agent 3개가 차례로 분석한 뒤 보고서 초안이 출력되고, 담당자가 승인·보류·반려를 선택합니다.
결과는 `results/<시각>_<라벨>_<접수번호>/` 폴더에, 검토 이력은 `data/review.db`의 `review_history` 테이블에 저장됩니다.
자세한 내용은 [CrewAI 실행 안내](docs/ROLE_C.md)를 참고하세요.

실제 API 수집 검증은 남아 있으며, 확인 항목은 [API 검증 체크리스트](docs/API_INTEGRATION_TODO.md)에 정리되어 있습니다.

## 프로젝트 목적

삼성전자 DX부문 생활가전·전자 사업의 전략기획 담당자 관점에서 경쟁사의 투자·사업 변화를 모니터링합니다. 우선 경쟁사는 LG전자로 설정하고, OpenDART 공개 공시를 수집해 변화 후보와 근거가 있는 분석 보고서 초안을 제공합니다. 최종 중요도는 사람이 판단합니다.

## 분석 및 비교 범위

- 분석 대상: LG전자 우선, 필요 시 국내 전자·부품 기업으로 확장
- 조회 기간: 기본 최근 3개년, 동일 유형 비교 사례가 부족하면 최대 최근 5개년
- 공시 유형: 주요사항보고서, 사업·반기·분기보고서, 투자·취득·처분 관련 공시
- 비교 방식: LG전자의 현재 공시와 과거 유사 공시, 관련 재무항목의 전년 수치를 비교

삼성전자는 분석 결과를 활용하는 기업의 관점입니다. 삼성전자와 LG전자의 재무·공시를 직접 나란히 비교하는 기능은 현재 기획안에 명시되어 있지 않습니다.

## 전체 업무 흐름

1. 사용자가 분석 대상 기업, 기간, 공시 유형을 입력합니다.
2. Program이 OpenDART에서 공시를 수집하고 원문을 정규화합니다.
3. 최초 수집 자료를 SQLite/CSV에 누적 저장하고, 이후 신규 공시를 추가 조회합니다.
4. Program이 프로젝트 기준에 따라 변화 후보를 탐지합니다.
5. Agent가 공시의 핵심 사실과 근거를 추출하고, 변화 맥락과 영향·확인사항을 분석해 보고서 초안을 작성합니다.
6. Human이 원문 근거와 분석을 검토하고 중요도를 판단하며 승인·보류·반려합니다.
7. Program이 원문, 분석 결과, 사람의 판단과 피드백 이력을 저장합니다. 중요도가 낮은 공시도 보존합니다.

## 변화 후보 탐지 기준

아래 기준은 프로젝트에서 정의한 기준이며, 실제 기업 내부 기준을 의미하지 않습니다.

- 직전 유사 공시 대비 금액 변화가 ±20% 이상
- 관련 재무항목의 전년 대비 변화가 ±15% 이상
- 신규사업·사업 중단·사업재편 등의 명시적 표현 등장
- 신규 또는 반복 발생 여부 확인

## Agent 역할

| Agent | 역할 | 주요 입력 | 주요 출력 |
| --- | --- | --- | --- |
| Disclosure Analysis Agent | 공시 핵심 사실과 근거 추출 | 공시 원문, 기업정보, 접수번호, 키워드 등장 문맥 | FACT, 원문 근거, 키워드 실제 사건 여부, 확인 불가 정보 |
| Business Change Analyst | 변화 맥락·영향과 추가 확인사항 분석 | 첫 Agent 결과, 과거 공시, Program 계산 결과(변화 탐지 기준 적용) | CHANGE, INTERPRETATION, NEEDS_VERIFICATION |
| Intelligence Report Agent | 검토용 보고서 초안 작성 | 앞선 Agent 결과(변화 탐지 결과 포함) | 요약 보고서, 출처, 후속 확인사항 |

API 호출과 수치 계산은 Program이 수행하고, 문서 이해와 맥락 설명은 Agent가 담당합니다. 접수번호와 원문 근거를 단계 사이에 전달하고 분석 이력을 유지합니다.

Agent 출력의 신뢰성을 위해 Program이 다음을 검증합니다.

- Agent가 붙인 근거 문장이 실제 원문과 Program 근거에 있는지 대조하고, 없으면 담당자에게 경고합니다.
- Program이 넘긴 확인 필요 항목이 분석 결과에서 빠지면 Agent에게 재작성시킵니다.
- 신뢰도가 낮은 해석과 원문으로 확인할 수 없는 항목은 Agent가 누락해도 확인 필요 항목으로 자동 추가합니다.
- 변화율은 Agent가 계산하지 않고 Program 계산값을 그대로 사용합니다.

## 사람의 검토와 재작업

전략기획 담당자가 원문 근거의 일치 여부, 사실과 해석의 구분, 과도한 추론 여부를 검토합니다. 최종 중요도는 높음·중간·낮음·보류로 판단합니다.

- 근거 오류: 첫 번째 Agent부터 재작업
- 비교·해석 오류: 두 번째 Agent부터 재작업 (첫 번째 Agent 결과는 재사용)
- 보고서 형식 오류: 세 번째 Agent만 재작업

반려 시 담당자가 입력한 수정 지시가 해당 Agent에게 전달되며, 재작업은 최대 3회까지 진행합니다.
회차별 결과와 판단 이력은 모두 저장되어 반려 전후를 비교할 수 있습니다.

## 최종 산출물과 성공 기준

최종 보고서는 FACT(사실), CHANGE(변화), INTERPRETATION(해석), NEEDS_VERIFICATION(확인 필요)을 구분합니다. 원문 근거와 출처를 연결하고, 공개 공시만으로 확인할 수 없는 내용은 확인 필요로 표시합니다.

성공 기준은 원문 근거를 추적할 수 있고, 사실과 해석을 구분하며, 사람이 보고서를 검토하고 판단 이력을 확인할 수 있는 것입니다.

## 구현 시 구체화할 사항

- 공시 유형별 추출할 금액·일자·사업 항목과 과거 유사 공시의 매칭 기준
- 변화 계산에 필요한 구조화 데이터의 확보 방법과 Agent 사실 추출의 실행 순서
- 비교 기준값이 0이거나 자료가 부족한 경우의 처리
- ~~보고서 형식, 승인·반려 입력 방식, 재작업 시 후속 Agent 실행 범위~~ (역할 C에서 구현 완료)

이 문서는 기획 요약이며, 구현 완료를 의미하지 않습니다.
