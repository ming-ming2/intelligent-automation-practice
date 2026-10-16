import os
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

# ==================================
# 1. .env 파일 불러오기
# ==================================

env_path = Path(__file__).resolve().parent / ".env"

config = dotenv_values(env_path)

file_key = config.get("OPENAI_API_KEY")
system_key = os.environ.get("OPENAI_API_KEY")

# ==================================
# 2. API 키 설정 확인
# ==================================

print("=== API 환경변수 진단 ===")

print("1. .env 파일 존재:", env_path.exists())
print("2. .env 내부 API 키 존재:", bool(file_key))
print("3. 시스템 환경변수 API 키 존재:", bool(system_key))

if file_key and system_key:
    print("4. 두 API 키가 같은가?:", file_key == system_key)
else:
    print("4. API 키 비교 불가능")

# ==================================
# 3. 실제 OpenAI API 호출
# ==================================

if not file_key:
    raise SystemExit("오류: .env 파일에서 API 키를 찾을 수 없습니다.")

client = OpenAI(
    api_key=file_key,
    organization=config.get("OPENAI_ORG_ID") or None,
    project=config.get("OPENAI_PROJECT_ID") or None,
)

print("\n=== OpenAI API 연결 테스트 ===")

try:
    response = client.responses.create(
        model="gpt-4o-mini",
        input="숫자 1만 출력해줘.",
        max_output_tokens=32
    )

    print("API 호출 성공!")
    print("응답:", response.output_text)

except Exception as e:
    print("API 호출 실패!")
    print("오류 유형:", type(e).__name__)
    print("오류 내용:", str(e))