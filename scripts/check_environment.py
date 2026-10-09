"""Check the local team runtime without calling APIs or printing credentials."""
import importlib
from importlib.metadata import version
import os
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    'requests': 'requests',
    'python-dotenv': 'dotenv',
    'beautifulsoup4': 'bs4',
    'lxml': 'lxml.etree',
    'pydantic': 'pydantic',
    'PyYAML': 'yaml',
    'crewai': 'crewai',
}


def main():
    if sys.version_info[:2] != (3, 12):
        print('FAIL: Python 3.12가 필요합니다. 현재:', sys.version.split()[0])
        return 1
    print('Python:', sys.version.split()[0])
    failed = False
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        print('FAIL: 패키지를 먼저 설치하세요: python -m pip install -r requirements.txt')
        return 1

    # CrewAI initializes local storage during import. Keep it inside this project.
    os.environ.setdefault('CREWAI_STORAGE_DIR', str(ROOT / '.cache' / 'crewai'))
    os.environ.setdefault('OTEL_SDK_DISABLED', 'true')
    os.environ.setdefault('CREWAI_TELEMETRY_DISABLED', 'true')
    for package, module in PACKAGES.items():
        try:
            importlib.import_module(module)
            print(f'OK: {package} {version(package)}')
        except Exception as error:
            # Do not dump tracebacks or environment values that may contain keys.
            print(f'FAIL: {package} import ({type(error).__name__})')
            failed = True

    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE environment_check (value INTEGER)')
        db.execute('INSERT INTO environment_check VALUES (1)')
        assert db.execute('SELECT value FROM environment_check').fetchone() == (1,)
    print('OK: SQLite', sqlite3.sqlite_version)

    for key in ('DART_API_KEY', 'OPENAI_API_KEY'):
        print(f'{key}: ' + ('설정됨 (유효성 확인 전)' if os.getenv(key, '').strip() else '미설정'))
    print('외부 API 호출은 수행하지 않았습니다.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
