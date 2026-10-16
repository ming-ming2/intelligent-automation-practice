"""역할 A: OpenDART 수집과 SQLite 저장. 원문 분석/변화 탐지는 수행하지 않습니다."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import time
import zipfile

from lxml import etree
import requests

from config import (DART_BASE_URL, DISCLOSURE_TYPES, REPORT_CODES, ROOT,
                    load_settings)


KST = timezone(timedelta(hours=9))
ERROR_MESSAGES = {
    '010': '등록되지 않은 인증키', '011': '사용할 수 없는 인증키',
    '012': '접근할 수 없는 IP', '013': '조회 데이터 없음',
    '014': '원문 파일 없음', '020': 'OpenDART 요청 제한 초과',
    '021': '조회 회사 수 초과', '100': '부적절한 요청값',
    '101': '부적절한 접근', '800': 'OpenDART 시스템 점검',
    '900': 'OpenDART 서버 오류', '901': '인증키 개인정보 보유기간 만료',
}
FATAL_CODES = {'010', '011', '012', '020', '800', '901', 'LOCAL_LIMIT'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS companies (
    corp_code TEXT PRIMARY KEY, corp_name TEXT NOT NULL,
    stock_code TEXT, modify_date TEXT, corp_eng_name TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS disclosures (
    rcept_no TEXT PRIMARY KEY, corp_code TEXT NOT NULL,
    corp_name TEXT NOT NULL, report_nm TEXT NOT NULL,
    rcept_dt TEXT NOT NULL, pblntf_ty TEXT NOT NULL,
    source_url TEXT NOT NULL, metadata_json TEXT NOT NULL,
    document_status TEXT NOT NULL DEFAULT 'pending',
    document_error TEXT, collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_disclosures_company_date
ON disclosures(corp_code, rcept_dt);
CREATE TABLE IF NOT EXISTS documents (
    rcept_no TEXT PRIMARY KEY REFERENCES disclosures(rcept_no),
    zip_path TEXT NOT NULL, files_json TEXT NOT NULL,
    sha256 TEXT NOT NULL, collected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_accounts (
    corp_code TEXT NOT NULL, bsns_year TEXT NOT NULL,
    reprt_code TEXT NOT NULL, row_no INTEGER NOT NULL,
    rcept_no TEXT, fs_div TEXT, sj_div TEXT, account_nm TEXT,
    thstrm_amount TEXT, frmtrm_amount TEXT, bfefrmtrm_amount TEXT,
    currency TEXT, raw_json TEXT NOT NULL,
    PRIMARY KEY(corp_code, bsns_year, reprt_code, row_no)
);
CREATE TABLE IF NOT EXISTS financial_queries (
    corp_code TEXT NOT NULL, bsns_year TEXT NOT NULL,
    reprt_code TEXT NOT NULL, status TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY(corp_code, bsns_year, reprt_code)
);
CREATE TABLE IF NOT EXISTS list_cursors (
    corp_code TEXT NOT NULL, pblntf_ty TEXT NOT NULL,
    scope_start TEXT NOT NULL, checked_through TEXT NOT NULL,
    PRIMARY KEY(corp_code, pblntf_ty, scope_start)
);
CREATE TABLE IF NOT EXISTS api_usage (
    request_date TEXT NOT NULL, key_id TEXT NOT NULL,
    requests INTEGER NOT NULL,
    PRIMARY KEY(request_date, key_id)
);
'''


class DartError(RuntimeError):
    """API 키를 포함하는 URL/응답 내용을 예외에 노출하지 않습니다."""
    def __init__(self, code, message=None):
        self.code = str(code)
        super().__init__(f'{self.code}: {message or ERROR_MESSAGES.get(self.code, "OpenDART 요청 실패")}')


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def timestamp():
    return datetime.now(KST).isoformat(timespec='seconds')


def validate_code(value, length, name):
    if not isinstance(value, str) or not re.fullmatch(rf'[0-9]{{{length}}}', value):
        raise ValueError(f'{name}: {length}자리 숫자 문자열이 필요합니다.')


def validate_period(start, end):
    for value in (start, end):
        validate_code(value, 8, '날짜')
        datetime.strptime(value, '%Y%m%d')
    if start > end:
        raise ValueError('시작일이 종료일보다 늦습니다.')


def xml_root(content):
    # DART 원문에는 DTD가 있을 수 있으므로 외부 참조/엔티티 확장은 금지합니다.
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        return etree.fromstring(content, parser)
    except etree.XMLSyntaxError:
        raise DartError('BAD_XML', '유효하지 않은 XML 응답') from None


class DartCollector:
    def __init__(self, settings=None, session=None):
        self.settings = settings or load_settings()
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.settings.db_path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)
        # 기존 4개 컬럼의 기업 캐시도 삭제 없이 새 응답 필드를 저장할 수 있게 합니다.
        company_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(companies)')}
        with self.db:
            for column in ('corp_eng_name', 'raw_json'):
                if column not in company_columns:
                    self.db.execute(f'ALTER TABLE companies ADD COLUMN {column} TEXT')
        self.db.commit()
        self.session = session or requests.Session()
        self.key_id = hashlib.sha256(self.settings.dart_api_key.encode()).hexdigest()[:16]
        self.api_requests = 0
        self._last_request = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()
        self.session.close()

    def _reserve_request(self):
        day = datetime.now(KST).strftime('%Y-%m-%d')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR IGNORE INTO api_usage VALUES (?, ?, 0)', (day, self.key_id))
            count = self.db.execute('SELECT requests FROM api_usage WHERE request_date=? AND key_id=?',
                                    (day, self.key_id)).fetchone()[0]
            if count >= self.settings.daily_limit:
                raise DartError('LOCAL_LIMIT', '로컬 일일 호출 한도에 도달했습니다.')
            self.db.execute('UPDATE api_usage SET requests=requests+1 WHERE request_date=? AND key_id=?',
                            (day, self.key_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.api_requests += 1

    def _request(self, endpoint, **params):
        for attempt in range(self.settings.max_retries + 1):
            delay = self.settings.request_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._reserve_request()
            self._last_request = time.monotonic()
            try:
                response = self.session.get(
                    DART_BASE_URL + endpoint,
                    params={'crtfc_key': self.settings.dart_api_key, **params},
                    timeout=self.settings.timeout,
                )
            except requests.RequestException:
                if attempt < self.settings.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise DartError('NETWORK', '네트워크 연결 실패 또는 시간 초과') from None
            status = response.status_code
            if status == 429 or status >= 500:
                response.close()
                if attempt < self.settings.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise DartError(f'HTTP_{status}', '서버 요청 실패; 다음 실행에서 재시도하세요.')
            if status != 200:
                response.close()
                raise DartError(f'HTTP_{status}', 'HTTP 요청 실패')
            content = response.content
            response.close()
            return content

    def _json(self, endpoint, **params):
        try:
            data = json.loads(self._request(endpoint, **params))
        except (ValueError, UnicodeDecodeError):
            raise DartError('BAD_JSON', '유효하지 않은 JSON 응답') from None
        if not isinstance(data, dict):
            raise DartError('BAD_JSON', 'JSON 객체 응답이 필요합니다.')
        if data.get('status') == '013':
            return {'status': '013', 'list': [], 'total_page': 0}
        if data.get('status') != '000':
            raise DartError(data.get('status', 'UNKNOWN'))
        if not isinstance(data.get('list'), list):
            raise DartError('BAD_JSON', '응답에 목록이 없습니다.')
        return data

    def _zip(self, endpoint, **params):
        content = self._request(endpoint, **params)
        if not zipfile.is_zipfile(BytesIO(content)):
            root = xml_root(content)
            raise DartError(root.findtext('status') or 'BAD_ZIP')
        return content

    def get_corp_code(self, company_name, refresh=False):
        """정확한 회사명으로 고유번호 조회. 성공한 기업 목록은 DB에 캐시합니다."""
        if not isinstance(company_name, str) or not company_name.strip():
            raise ValueError('회사명을 입력하세요.')
        name = company_name.strip()
        rows = self.db.execute('SELECT corp_code FROM companies WHERE corp_name=?', (name,)).fetchall()
        if refresh or not rows:
            content = self._zip('corpCode.xml')
            try:
                with zipfile.ZipFile(BytesIO(content)) as archive:
                    members = [n for n in archive.namelist() if n.lower().endswith('.xml')]
                    if len(members) != 1:
                        raise DartError('BAD_ZIP', '고유번호 XML 파일을 특정할 수 없습니다.')
                    root = xml_root(archive.read(members[0]))
            except (zipfile.BadZipFile, RuntimeError):
                raise DartError('BAD_ZIP', '고유번호 ZIP 손상') from None
            records = []
            for item in root.findall('list'):
                # 알려진 필드를 컬럼으로 저장하고, 전체 항목도 API 필드명 그대로 보존합니다.
                original = {child.tag: child.text or '' for child in item}
                records.append((item.findtext('corp_code'), item.findtext('corp_name'),
                                item.findtext('stock_code'), item.findtext('modify_date'),
                                item.findtext('corp_eng_name'), dumps(original)))
            if not records:
                raise DartError('BAD_XML', '고유번호 목록이 비어 있습니다.')
            with self.db:
                self.db.execute('DELETE FROM companies')
                self.db.executemany('''INSERT INTO companies
                    (corp_code, corp_name, stock_code, modify_date, corp_eng_name, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?)''', records)
            rows = self.db.execute('SELECT corp_code FROM companies WHERE corp_name=?', (name,)).fetchall()
        if len(rows) != 1:
            raise ValueError('정확히 일치하는 회사를 찾지 못했거나 동일 이름의 회사가 여러 개입니다.')
        return rows[0]['corp_code']

    def fetch_disclosures(self, corp_code, start_date, end_date, disclosure_type='A'):
        """정정 공시를 포함하여 모든 페이지의 목록을 조회합니다."""
        validate_code(corp_code, 8, 'corp_code')
        validate_period(start_date, end_date)
        if disclosure_type not in tuple('ABCDEFGHIJ'):
            raise ValueError('공시유형은 A~J 중 하나여야 합니다.')
        records = []
        page = 1
        while True:
            data = self._json('list.json', corp_code=corp_code, bgn_de=start_date,
                              end_de=end_date, pblntf_ty=disclosure_type,
                              last_reprt_at='N', page_no=page, page_count=100,
                              sort='date', sort_mth='asc')
            records.extend(data['list'])
            if page >= int(data.get('total_page', 0)):
                break
            page += 1
        return records

    def _save_disclosures(self, records, disclosure_type):
        added = 0
        with self.db:
            for item in records:
                validate_code(item['rcept_no'], 14, 'rcept_no')
                cursor = self.db.execute('''INSERT OR IGNORE INTO disclosures
                    (rcept_no, corp_code, corp_name, report_nm, rcept_dt, pblntf_ty,
                     source_url, metadata_json, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (item['rcept_no'], item['corp_code'], item['corp_name'], item['report_nm'],
                     item['rcept_dt'], disclosure_type,
                     'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=' + item['rcept_no'],
                     dumps(item), timestamp()))
                added += cursor.rowcount
        return added

    def _stored_path(self, path):
        try:
            return path.relative_to(ROOT).as_posix()
        except ValueError:
            return path.as_posix()

    def _local_path(self, stored):
        path = Path(stored)
        return path if path.is_absolute() else ROOT / path

    def document_available(self, rcept_no):
        row = self.db.execute('SELECT * FROM documents WHERE rcept_no=?', (rcept_no,)).fetchone()
        if not row:
            return False
        paths = [row['zip_path'], *json.loads(row['files_json'])]
        return bool(paths) and all(self._local_path(p).is_file() and self._local_path(p).stat().st_size > 0
                                   for p in paths)

    def download_document(self, rcept_no):
        """원문 ZIP을 저장·해제하고 DB에 파일 위치를 기록합니다."""
        validate_code(rcept_no, 14, 'rcept_no')
        if not self.db.execute('SELECT 1 FROM disclosures WHERE rcept_no=?', (rcept_no,)).fetchone():
            raise ValueError('공시 목록을 먼저 저장하세요.')
        if self.document_available(rcept_no):
            row = self.db.execute('SELECT files_json FROM documents WHERE rcept_no=?', (rcept_no,)).fetchone()
            return json.loads(row['files_json'])
        content = self._zip('document.xml', rcept_no=rcept_no)
        target = self.settings.raw_dir / rcept_no
        target.mkdir(parents=True, exist_ok=True)
        paths = []
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                files = [i for i in archive.infolist() if not i.is_dir()]
                if not files or sum(i.file_size for i in files) > 200 * 1024 * 1024:
                    raise DartError('BAD_ZIP', '원문 ZIP이 비어 있거나 해제 용량이 너무 큽니다.')
                if not any(i.filename.lower().endswith('.xml') for i in files):
                    raise DartError('BAD_ZIP', '원문 XML 파일이 없습니다.')
                for member in files:
                    name = member.filename
                    parts = PurePosixPath(name).parts
                    if PurePosixPath(name).is_absolute() or '..' in parts or '\\' in name or ':' in name:
                        raise DartError('BAD_ZIP', '허용되지 않은 ZIP 경로')
                    destination = target / name
                    if not destination.resolve().is_relative_to(target.resolve()):
                        raise DartError('BAD_ZIP', '허용되지 않은 ZIP 경로')
                # 완전히 읽고 CRC를 확인한 뒤 파일을 배치합니다.
                with tempfile.TemporaryDirectory(dir=target) as staging:
                    stage = Path(staging)
                    for member in files:
                        temporary = stage / member.filename
                        temporary.parent.mkdir(parents=True, exist_ok=True)
                        temporary.write_bytes(archive.read(member))
                    for member in files:
                        destination = target / member.filename
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        (stage / member.filename).replace(destination)
                        paths.append(self._stored_path(destination))
                    zipped = stage / 'download.zip'
                    zipped.write_bytes(content)
                    zipped.replace(target / 'original.zip')
        except (zipfile.BadZipFile, RuntimeError):
            raise DartError('BAD_ZIP', '원문 ZIP 손상') from None
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?)',
                            (rcept_no, self._stored_path(target / 'original.zip'), dumps(paths),
                             hashlib.sha256(content).hexdigest(), timestamp()))
            self.db.execute("UPDATE disclosures SET document_status='downloaded', document_error=NULL WHERE rcept_no=?",
                            (rcept_no,))
        return paths

    def fetch_financial_accounts(self, corp_code, year, report_code):
        validate_code(corp_code, 8, 'corp_code')
        if type(year) is not int or not 2015 <= year <= datetime.now(KST).year:
            raise ValueError('재무 사업연도는 2015년부터 현재 연도까지입니다.')
        if report_code not in REPORT_CODES:
            raise ValueError('지원하지 않는 재무 보고서 코드')
        return self._json('fnlttSinglAcnt.json', corp_code=corp_code,
                          bsns_year=str(year), reprt_code=report_code)['list']

    def _save_financials(self, corp_code, year, report_code, records):
        with self.db:
            self.db.execute('DELETE FROM financial_accounts WHERE corp_code=? AND bsns_year=? AND reprt_code=?',
                            (corp_code, str(year), report_code))
            for index, item in enumerate(records):
                self.db.execute('INSERT INTO financial_accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (corp_code, str(year), report_code, index,
                     item.get('rcept_no'), item.get('fs_div'), item.get('sj_div'), item.get('account_nm'),
                     item.get('thstrm_amount'), item.get('frmtrm_amount'), item.get('bfefrmtrm_amount'),
                     item.get('currency'), dumps(item)))
            self.db.execute('INSERT OR REPLACE INTO financial_queries VALUES (?, ?, ?, ?, ?)',
                            (corp_code, str(year), report_code,
                             'success' if records else 'no_data', timestamp()))

    def collect(self, company_name, start_date, end_date, *, disclosure_types=DISCLOSURE_TYPES,
                financial_years=None, report_codes=REPORT_CODES, max_documents=None,
                refresh_companies=False, refresh_financials=False, skip_financials=False,
                refresh_disclosures=False):
        validate_period(start_date, end_date)
        if not disclosure_types or any(t not in tuple('ABCDEFGHIJ') for t in disclosure_types):
            raise ValueError('공시유형은 A~J 중 하나 이상 지정하세요.')
        if max_documents is not None and (type(max_documents) is not int or max_documents < 0):
            raise ValueError('max_documents는 음이 아닌 정수여야 합니다.')
        if not report_codes or any(c not in REPORT_CODES for c in report_codes):
            raise ValueError('지원하지 않는 재무 보고서 코드')
        # 입력 기간의 마지막 연도를 기준으로 최소 3년, 최대 5년을 수집합니다.
        last_year = int(end_date[:4])
        first_year = max(2015, min(int(start_date[:4]), last_year - 2), last_year - 4)
        years = list(range(first_year, last_year + 1)) if financial_years is None else list(financial_years)
        if any(type(y) is not int or not 2015 <= y <= datetime.now(KST).year for y in years):
            raise ValueError('재무 사업연도는 2015년부터 현재 연도까지입니다.')
        before = self.api_requests
        corp_code = self.get_corp_code(company_name, refresh=refresh_companies)
        result = {'company': company_name, 'corp_code': corp_code, 'start_date': start_date,
                  'end_date': end_date, 'db_path': str(self.settings.db_path),
                  'disclosure_types': list(disclosure_types), 'financial_years': years,
                  'report_codes': list(report_codes), 'financial_collection_skipped': skip_financials,
                  'list_query_windows': [],
                  'new_disclosures': 0, 'existing_disclosures': 0, 'downloaded_documents': 0,
                  'skipped_documents': 0, 'pending_documents': 0,
                  'financial_rows_saved': 0, 'financial_queries_skipped': 0,
                  'financial_no_data': [], 'errors': []}
        for kind in dict.fromkeys(disclosure_types):
            cursor = self.db.execute('SELECT checked_through FROM list_cursors WHERE corp_code=? AND pblntf_ty=? AND scope_start=?',
                                     (corp_code, kind, start_date)).fetchone()
            query_start = start_date
            if cursor and not refresh_disclosures and cursor['checked_through'] <= end_date:
                # 마지막 확인 날짜를 포함해 조회: 같은 날짜에 추가된 접수도 놓치지 않습니다.
                query_start = max(start_date, cursor['checked_through'])
            result['list_query_windows'].append({'pblntf_ty': kind, 'start': query_start, 'end': end_date})
            records = self.fetch_disclosures(corp_code, query_start, end_date, kind)
            added = self._save_disclosures(records, kind)
            result['new_disclosures'] += added
            result['existing_disclosures'] += len(records) - added
            with self.db:
                self.db.execute('''INSERT INTO list_cursors VALUES (?, ?, ?, ?)
                    ON CONFLICT(corp_code, pblntf_ty, scope_start)
                    DO UPDATE SET checked_through=MAX(checked_through, excluded.checked_through)''',
                    (corp_code, kind, start_date, end_date))
        # 신규 목록이 없어도 이전에 실패/보류된 원문은 DB에서 찾아 다시 수집합니다.
        placeholders = ','.join('?' for _ in disclosure_types)
        receipt_numbers = [r['rcept_no'] for r in self.db.execute(
            f'''SELECT rcept_no FROM disclosures WHERE corp_code=? AND rcept_dt BETWEEN ? AND ?
                AND pblntf_ty IN ({placeholders}) ORDER BY rcept_no''',
            (corp_code, start_date, end_date, *disclosure_types)).fetchall()]
        attempted = 0
        for receipt in sorted(receipt_numbers):
            if self.document_available(receipt):
                result['skipped_documents'] += 1
                continue
            if max_documents is not None and attempted >= max_documents:
                result['pending_documents'] += 1
                continue
            attempted += 1
            try:
                self.download_document(receipt)
                result['downloaded_documents'] += 1
            except (DartError, OSError) as error:
                if isinstance(error, DartError) and error.code in FATAL_CODES:
                    raise
                message = str(error) if isinstance(error, DartError) else 'FILE_IO: 파일 저장 실패'
                with self.db:
                    self.db.execute("UPDATE disclosures SET document_status='failed', document_error=? WHERE rcept_no=?",
                                    (message, receipt))
                result['errors'].append({'stage': 'document', 'rcept_no': receipt, 'error': message})
        if not skip_financials:
            for year in dict.fromkeys(years):
                for code in dict.fromkeys(report_codes):
                    previous = self.db.execute('SELECT status FROM financial_queries WHERE corp_code=? AND bsns_year=? AND reprt_code=?',
                                               (corp_code, str(year), code)).fetchone()
                    if previous and previous['status'] == 'success' and not refresh_financials:
                        result['financial_queries_skipped'] += 1
                        continue
                    try:
                        records = self.fetch_financial_accounts(corp_code, year, code)
                        self._save_financials(corp_code, year, code, records)
                        result['financial_rows_saved'] += len(records)
                        if not records:
                            result['financial_no_data'].append({'bsns_year': year, 'reprt_code': code})
                    except DartError as error:
                        if error.code in FATAL_CODES:
                            raise
                        result['errors'].append({'stage': 'financial', 'bsns_year': year,
                                                 'reprt_code': code, 'error': str(error)})
        result['api_requests'] = self.api_requests - before
        result['status'] = ('completed_with_errors' if result['errors'] else
                            'partial' if result['pending_documents'] else 'completed')
        return result


def collect(company_name, start_date, end_date, **options):
    """예: collect('LG전자', '20220101', '20251231')."""
    with DartCollector() as collector:
        return collector.collect(company_name, start_date, end_date, **options)


def main():
    parser = argparse.ArgumentParser(description='OpenDART 공시·원문·재무 수집 (역할 A)')
    parser.add_argument('--company', default='LG전자')
    parser.add_argument('--start', default='20220101')
    parser.add_argument('--end', default='20251231')
    parser.add_argument('--types', nargs='+', choices=tuple('ABCDEFGHIJ'), default=list(DISCLOSURE_TYPES))
    parser.add_argument('--financial-years', nargs='+', type=int)
    parser.add_argument('--report-codes', nargs='+', choices=REPORT_CODES, default=list(REPORT_CODES))
    parser.add_argument('--max-documents', type=int, help='이번 실행에서 시도할 원문 수; 생략하면 전체')
    parser.add_argument('--skip-financials', action='store_true', help='목록·원문만 먼저 수집')
    parser.add_argument('--refresh-companies', action='store_true')
    parser.add_argument('--refresh-financials', action='store_true')
    parser.add_argument('--refresh-disclosures', action='store_true', help='목록 증분 조회 대신 전체 기간 재확인')
    args = parser.parse_args()
    try:
        result = collect(args.company, args.start, args.end, disclosure_types=args.types,
                         financial_years=args.financial_years, report_codes=args.report_codes,
                         max_documents=args.max_documents, skip_financials=args.skip_financials,
                         refresh_companies=args.refresh_companies, refresh_financials=args.refresh_financials,
                         refresh_disclosures=args.refresh_disclosures)
    except (DartError, ValueError, OSError, sqlite3.Error) as error:
        # 예외 traceback에는 requests의 인증키 URL이 남을 수 있으므로 출력하지 않습니다.
        message = str(error) if isinstance(error, (DartError, ValueError)) else type(error).__name__
        parser.exit(1, f'수집 실패: {message}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
