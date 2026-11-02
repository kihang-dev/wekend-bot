import threading
from pathlib import Path

import gspread

HERE = Path(__file__).resolve().parent
KEY_PATH = HERE / "google-key.json"
_lock = threading.Lock()

# ---- 회원정보 시트 (봇 -> 시트) ----
SHEET_ID = "1a1j-EgDG6JxeNez3RzfgF7DCnhc5cVBFMnDoRMYiyvk"
MEMBER_TAB = "회원정보"

HEADERS = ["순번", "이름", "포지션1", "포지션2", "출생년", "상의", "하의",
           "등번호", "이니셜", "회비대상여부", "연락처", "금액", "납부금액", "가입일"]


def member_to_row(seq, m):
    """회원 한 명을 시트 한 행(HEADERS 순서)으로 바꾼다.
    TODO: m 의 실제 키 이름에 맞춰 채운다 (members.py 구조 확인 후)."""
    return [
        seq,
        m.get("name", ""),
        m.get("pos1", ""),
        m.get("pos2", ""),
        m.get("birth", ""),
        m.get("top", ""),
        m.get("bottom", ""),
        m.get("number", ""),
        m.get("initial", ""),
        m.get("dues_target", ""),
        m.get("phone", ""),
        m.get("dues_amount", ""),
        m.get("dues_paid", ""),
        m.get("joined", ""),
    ]


def _write_members(members):
    gc = gspread.service_account(filename=str(KEY_PATH))
    ws = gc.open_by_key(SHEET_ID).worksheet(MEMBER_TAB)
    rows = [member_to_row(i + 1, m) for i, m in enumerate(members)]
    if rows:
        ws.update(values=rows, range_name=f"A2:N{len(rows) + 1}")
    ws.batch_clear([f"A{len(rows) + 2}:N1000"])


def sync_members_async(members):
    def job():
        with _lock:
            try:
                _write_members(members)
            except Exception as e:
                print("시트 회원정보 동기화 실패:", repr(e))
    threading.Thread(target=job, daemon=True).start()


# ---- 등급 시트 (봇 <-> 시트, 능력치 C~H열) ----
GRADE_SHEET_ID = "1YFgienZCjTqiRMqMz4T0ADH02R3x38HmhkuSTDA8hvA"
GRADE_TAB = "회원정보의 사본"
ABILITY_COLS = ["패스", "리턴", "수비", "드리블", "볼소유", "골결정력"]  # C~H열 순서


def _write_abilities(name, abilities):
    """abilities: {"패스": "A", "리턴": "B", ...} 바뀐 항목만 넣어도 된다."""
    gc = gspread.service_account(filename=str(KEY_PATH))
    ws = gc.open_by_key(GRADE_SHEET_ID).worksheet(GRADE_TAB)
    names = ws.col_values(2)  # B열(이름)
    if name not in names:
        raise ValueError(f"등급 시트에 없는 이름: {name}")
    row = names.index(name) + 1
    current = ws.row_values(row)
    values = [current[2 + i] if len(current) > 2 + i else "" for i in range(6)]
    for key, val in abilities.items():
        values[ABILITY_COLS.index(key)] = val
    ws.update(values=[values], range_name=f"C{row}:H{row}")


def sync_abilities_async(name, abilities):
    def job():
        with _lock:
            try:
                _write_abilities(name, abilities)
            except Exception as e:
                print("시트 등급 동기화 실패:", repr(e))
    threading.Thread(target=job, daemon=True).start()


def _clear_grade_row(name):
    gc = gspread.service_account(filename=str(KEY_PATH))
    ws = gc.open_by_key(GRADE_SHEET_ID).worksheet(GRADE_TAB)
    names = ws.col_values(2)
    if name not in names:
        return
    row = names.index(name) + 1
    ws.batch_clear([f"B{row}:H{row}"])


def sheet_clear_grade_row_async(name):
    def job():
        with _lock:
            try:
                _clear_grade_row(name)
            except Exception as e:
                print("시트 등급 행 삭제 실패:", repr(e))
    threading.Thread(target=job, daemon=True).start()
