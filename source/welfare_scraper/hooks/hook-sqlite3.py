# Custom hook-sqlite3.py
# Python 3.14에서 PyInstaller의 기본 sqlite3 hook이 subprocess crash를 일으키므로
# hiddenimports만 명시적으로 지정하고 collect_submodules 호출을 생략합니다.

hiddenimports = ['sqlite3', '_sqlite3']
