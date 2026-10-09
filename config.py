"""팀 공통 설정. 상대 경로는 항상 프로젝트 폴더 기준으로 해석합니다."""
from dataclasses import dataclass, field
import math
import os
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
DART_BASE_URL = 'https://opendart.fss.or.kr/api/'
REPORT_CODES = ('11013', '11012', '11014', '11011')
DISCLOSURE_TYPES = ('A', 'B', 'I')


def project_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    # repr에도 인증키를 노출하지 않습니다.
    dart_api_key: str = field(repr=False)
    db_path: Path = ROOT / 'data/dart.db'
    raw_dir: Path = ROOT / 'data/raw'
    results_dir: Path = ROOT / 'results'
    timeout: float = 30.0
    request_interval: float = 0.2
    max_retries: int = 2
    daily_limit: int = 20000

    def __post_init__(self):
        if not self.dart_api_key.strip():
            raise ValueError('.env에 DART_API_KEY를 설정하세요.')
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError('DART_TIMEOUT은 양의 유한한 수여야 합니다.')
        if not math.isfinite(self.request_interval) or self.request_interval < 0:
            raise ValueError('DART_REQUEST_INTERVAL은 음이 아닌 유한한 수여야 합니다.')
        if not 0 <= self.max_retries <= 5 or self.daily_limit <= 0:
            raise ValueError('재시도는 0~5회, 일일 호출 한도는 양수여야 합니다.')


def load_settings():
    load_dotenv(ROOT / '.env', override=False)
    return Settings(
        dart_api_key=os.getenv('DART_API_KEY', '').strip(),
        db_path=project_path(os.getenv('DART_DB_PATH', 'data/dart.db')),
        raw_dir=project_path(os.getenv('DART_RAW_DIR', 'data/raw')),
        results_dir=project_path(os.getenv('RESULTS_DIR', 'results')),
        timeout=float(os.getenv('DART_TIMEOUT', '30')),
        request_interval=float(os.getenv('DART_REQUEST_INTERVAL', '0.2')),
        max_retries=int(os.getenv('DART_MAX_RETRIES', '2')),
        daily_limit=int(os.getenv('DART_DAILY_LIMIT', '20000')),
    )
