"""지식 저장소 (내부 모듈). 파트 백데이터를 모으고 찾아본다.

- ``parser.py``  파일 → 시트 · 행 (Flask 와 무관)
- ``store.py``   DB · 파일 보관 · 검색 (Flask 와 무관)
- ``routes.py``  화면 · API (``bp``)
- ``panel.py``   통합 대시보드 패널 (``panel``)

parser · store 가 Flask 를 모르므로, 나중에 담당 WAS 로 떼어낼 때 두 파일을 그대로
옮기고 routes 만 새 WAS 에 맞추면 된다.
"""
