기존 프로젝트 루트에 같은 경로로 덮어쓰세요.

change_detector.py
crew.py
tests/test_role_b.py
docs/ROLE_B.md
docs/B_TO_C_CONTRACT.md
results/B_KEYWORD_FILTER_VALIDATION.md

검증:
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_role_b.py -v
python change_detector.py 20251114001332 --db ".\data\dart.db" --data-origin dart > ".\results\role_b_after_keyword_filter.json"
Select-String -Path ".\results\role_b_after_keyword_filter.json" -Pattern "KEYWORD_EXCLUDED"
