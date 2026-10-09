"""개발용 가상 검증. 제출용 실제 LG전자 공시 3건 검증을 대체하지 않는다."""
import ast
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from jsonschema import Draft202012Validator

from change_detector import (ROOT, build_payload, compare_values, detect_changes,
                             detect_financial_changes, extract_amounts, _value)
from normalizer import normalize_xml


def disclosure(receipt='MOCK-CURRENT', date='20260301', name='사업보고서 (2025.12)', body='<P>일반 내용</P>'):
    return {'rcept_no': receipt, 'rcept_dt': date, 'report_nm': name, 'corp_code': '00401731',
            'corp_name': 'LG전자', 'pblntf_ty': 'A', 'source': {'source_url': None, 'document_paths': []},
            'norm': normalize_xml(('<DOCUMENT>' + body + '</DOCUMENT>').encode())}


def financial(current, **updates):
    return {'corp_code': '00401731', 'rcept_no': current['rcept_no'], 'bsns_year': '2025',
            'reprt_code': '11011', 'fs_div': 'CFS', 'sj_div': 'IS', 'row_no': 0, 'account_nm': '매출액',
            'thstrm_amount': '120', 'frmtrm_amount': '100', 'currency': 'KRW',
            'raw_json': json.dumps({'thstrm_dt': '2025.01.01 ~ 2025.12.31',
                                    'frmtrm_dt': '2024.01.01 ~ 2024.12.31'}), **updates}


class RoleBTests(unittest.TestCase):
    def run_case(self, current, past=(), rows=()):
        result = detect_changes(current, current['norm'], past, rows, data_origin='synthetic')
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        schema = json.loads((ROOT / 'schemas/b_to_c.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(set(result), set(schema['required']))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(result)
        return result

    def test_normal_case(self):
        current = disclosure()
        result = self.run_case(current, rows=[financial(current)])
        item = result['triggers']['financial_changes'][0]
        self.assertTrue(item['triggered'])
        self.assertEqual(item['change_rate'], .2)
        self.assertEqual(item['dimensions']['current_period'], '2025.01.01 ~ 2025.12.31')

    def test_complex_case(self):
        current = disclosure(body='<TABLE><TR><TD>투자금액</TD><TD>130억원</TD></TR></TABLE><P>현재 합병 계획은 없습니다.</P>')
        past = [disclosure('MOCK-PREV', '20250301', '사업보고서 (2024.12)', '<P>투자금액: 100억원</P>'),
                disclosure('MOCK-OLD', '20210301', '사업보고서 (2020.12)')]
        result = self.run_case(current, past)
        self.assertEqual(result['triggers']['amount_changes'][0]['change_rate'], .3)
        self.assertEqual(result['history_search']['search_window_years'], 5)
        self.assertEqual(len(result['past_cases']), 2)  # 윤년 포함 5년 경계
        self.assertEqual(result['triggers']['keywords'][0]['evidence']['quote'], '현재 합병 계획은 없습니다.')

    def test_ambiguous_case(self):
        result = self.run_case(disclosure(body='<P>투자금액: -</P>'))
        self.assertIsNone(result['triggers']['amount_changes'][0]['triggered'])
        self.assertEqual(result['past_cases'], [])
        self.assertTrue(result['needs_verification'])

    def test_thresholds_and_missing_values(self):
        for threshold in (Decimal('.20'), Decimal('.15')):
            for offset, expected in [(Decimal('-.1'), False), (Decimal('0'), True), (Decimal('.1'), True)]:
                current = _value(Decimal(100) + 100 * threshold + offset, unit='원', currency='KRW')
                item = compare_values('금액', current, _value(Decimal(100), unit='원', currency='KRW'), threshold)
                self.assertEqual(item['triggered'], expected)
        for raw in (None, '-', '', 'NaN', 'Infinity'):
            item = compare_values('금액', {'value': raw}, {'value': '100'}, Decimal('.2'))
            self.assertIsNone(item['triggered'])
        item = compare_values('금액', _value(Decimal(100), unit='원', currency='KRW'),
                              _value(Decimal(0), unit='원', currency='KRW'), Decimal('.2'))
        self.assertEqual(item['reason'], 'PREVIOUS_VALUE_ZERO')
        for current, previous, expected in [('80', '100', -.2), ('-80', '-100', .2)]:
            item = compare_values('금액', _value(Decimal(current), unit='원', currency='KRW'),
                                  _value(Decimal(previous), unit='원', currency='KRW'), Decimal('.2'))
            self.assertTrue(item['triggered'])
            self.assertEqual(item['change_rate'], expected)

    def test_xml_errors_and_missing(self):
        for content in (b'', b'not xml', b'<DOCUMENT><P>broken'):
            self.assertEqual(normalize_xml(content)['normalization']['status'], 'error')
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(normalize_xml(Path(directory) / 'absent.xml')['normalization']['status'], 'missing')

    def test_parent_section_and_empty_cells(self):
        body = '<P>' + '서론' * 8000 + '</P><SECTION><TITLE>사업의 내용</TITLE><SECTION><TITLE>제품</TITLE><P>보존할 문장</P></SECTION></SECTION><TABLE><TR><TD>A</TD><TD></TD><TD>B</TD></TR></TABLE>'
        norm = normalize_xml(('<DOCUMENT>' + body + '</DOCUMENT>').encode())
        self.assertIn('보존할 문장', norm['normalized_text'])
        self.assertLessEqual(len(norm['normalized_text']), 15000)
        self.assertIn('A |  | B', norm['full_text'])

    def test_ambiguous_amounts_are_not_guessed(self):
        for text in ('투자금액 | 자기자본 500억원 | 투자금액 100억원', '투자금액: 1조 3,000억원',
                     '투자금액: 1,300', '투자금액: -'):
            current = disclosure(body=f'<P>{text}</P>')
            values, issues = extract_amounts(current['norm'], current['rcept_no'])
            self.assertIsNone(values['투자금액']['value'])
            self.assertTrue(issues)

    def test_previous_document_missing_does_not_use_older_amount(self):
        current = disclosure(body='<P>투자금액: 130억원</P>')
        past = [disclosure('MOCK-PREV', '20250301', '사업보고서 (2024.12)'),
                disclosure('MOCK-OLD', '20240301', '사업보고서 (2023.12)', '<P>투자금액: 100억원</P>')]
        item = self.run_case(current, past)['triggers']['amount_changes'][0]
        self.assertEqual(item['reason'], 'PREVIOUS_VALUE_MISSING')

    def test_financial_scope_and_period(self):
        current = disclosure()
        future = financial(current, rcept_no='MOCK-FUTURE')
        self.assertEqual(detect_financial_changes(current, [future]), [])
        row = financial(current, raw_json=json.dumps({'thstrm_dt': '2025.09.30', 'frmtrm_dt': '2024.12.31'}))
        item = detect_financial_changes(current, [row])[0]
        self.assertIsNone(item['triggered'])
        self.assertEqual(item['reason'], 'PERIOD_UNVERIFIED')

    def test_correction_and_origin(self):
        current = disclosure(body='<P>투자금액: 130억원</P>')
        past = [disclosure('MOCK-PREV', '20250301', '[기재정정]사업보고서 (2024.12)', '<P>투자금액: 100억원</P>')]
        result = self.run_case(current, past)
        self.assertEqual(result['history_search']['cases_in_three_years'], 0)
        self.assertIsNone(result['triggers']['amount_changes'][0]['triggered'])
        with self.assertRaises(ValueError):
            detect_changes(current, current['norm'], data_origin='dart')

    def test_a_database_adapter_and_external_path(self):
        # A의 실제 SCHEMA를 사용하되 수집 모듈을 실행하거나 API를 호출하지 않는다.
        tree = ast.parse((ROOT / 'dart_collector.py').read_text(encoding='utf-8-sig'))
        schema = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'SCHEMA' for t in n.targets))
        with tempfile.TemporaryDirectory() as folder:
            xml, db_path = Path(folder) / 'sample.xml', Path(folder) / 'sample.db'
            xml.write_text('<DOCUMENT><P>신규사업 검토</P></DOCUMENT>', encoding='utf-8')
            with closing(sqlite3.connect(db_path)) as db:
                db.executescript(schema)
                db.execute('INSERT INTO disclosures VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           ('MOCK-CURRENT', '00401731', 'LG전자', '사업보고서', '20260301', 'A', '', '{}', 'downloaded', None, 'test'))
                db.execute('INSERT INTO documents VALUES (?,?,?,?,?)', ('MOCK-CURRENT', '', json.dumps([str(xml)]), '', 'test'))
                db.commit()
            result = build_payload('MOCK-CURRENT', db_path, data_origin='synthetic')
            self.assertEqual(result['normalization']['status'], 'ok')
            self.assertTrue(result['source']['document_paths'])
            self.assertEqual(result['triggers']['keywords'][0]['keyword'], '신규사업')


if __name__ == '__main__':
    unittest.main()
