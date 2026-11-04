import re
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


def read_grade_sheet():
    """등급 시트를 {이름: {능력치: 등급}} 으로 읽는다. 이름이 빈 행은 건너뛴다."""
    gc = gspread.service_account(filename=str(KEY_PATH))
    ws = gc.open_by_key(GRADE_SHEET_ID).worksheet(GRADE_TAB)
    out = {}
    for row in ws.get("B2:H200"):
        if not row or not row[0].strip():
            continue
        vals = (row + [""] * 7)[1:7]
        out[row[0].strip()] = {k: v.strip().upper() for k, v in zip(ABILITY_COLS, vals)}
    return out


GRADE_GID = 1988904302  # 등급 시트의 '회원정보의 사본' 탭 gid
GRADE_RANGE = "A1:J25"


def export_grade_sheet_png(out_png):
    """등급 시트 GRADE_RANGE 를 PDF 로 내보낸 뒤 PNG 한 장으로 바꾼다 (서비스 계정 권한 사용)."""
    import subprocess
    import requests
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_service_account_file(
        str(KEY_PATH),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly"])
    creds.refresh(Request())
    url = f"https://docs.google.com/spreadsheets/d/{GRADE_SHEET_ID}/export"
    params = {"format": "pdf", "gid": GRADE_GID, "range": GRADE_RANGE,
              "portrait": "false", "fitw": "true", "size": "A4"}
    r = requests.get(url, params=params, headers={"Authorization": f"Bearer {creds.token}"}, timeout=60)
    r.raise_for_status()
    pdf = out_png.with_suffix(".pdf")
    pdf.write_bytes(r.content)
    subprocess.run(["pdftoppm", "-png", "-r", "150", "-singlefile", str(pdf), str(out_png.with_suffix(""))],
                   check=True, timeout=60)
    pdf.unlink(missing_ok=True)
    return out_png


# ---- 2026년 결산 탭 (같은 스프레드시트 파일 안의 gid 탭) ----
LEDGER_SHEET_ID = "1a1j-EgDG6JxeNez3RzfgF7DCnhc5cVBFMnDoRMYiyvk"
LEDGER_GID = 164012503
DUES_AMOUNT = 5000
# 블록: (계 라벨 열, 첫 열, 끝 열), 1부터 시작
LEDGER_BLOCKS = {
    "이자": (19, 18, 20),   # R 날짜, S 항목, T 금액
    "지각": (23, 22, 24),   # V 날짜, W 이름, X 금액
    "노쇼": (27, 26, 28),   # Z 날짜, AA 이름, AB 금액
    "지출": (31, 30, 33),   # AD 날짜, AE 항목, AF 금액, AG 비고
}
LEDGER_NAME_COL = 2      # B 이름 (2행~27행)
LEDGER_DUES_COL = 3      # C 회비대상여부
LEDGER_MONTH_OFFSET = 3  # D = 1월 → 열 번호 = 3 + 월
# 이월금·잔액 칸: 글자(두 줄 병합) 바로 아래가 값이다. 선수 행을 끼워 넣으면 함께 내려가므로 글자로 찾는다.
LEDGER_CARRY = (4, "이월금")    # D31:D32 글자, D33 값
LEDGER_BALANCE = (10, "잔액")   # J31:J32 글자, J33 값
LEDGER_LABEL_ROW = 31           # 선수를 끼워 넣기 전 이월금 글자 행
LEDGER_MEMBER_END = 27          # 그때의 명단 마지막 행 (글자가 내려간 만큼 함께 내려간다)
_ledger_lock = threading.Lock()


def _ledger_label_row(ws, col, label):
    r = next((i for i, v in enumerate(ws.col_values(col), start=1) if i > 1 and v.strip() == label), None)
    if r is None:
        raise ValueError(f"결산 탭에서 '{label}' 글자를 찾지 못했어요.")
    return r


def _ledger_value_cell(ws, spec):
    """이월금/잔액 값 칸 (글자 두 줄 아래)."""
    col, label = spec
    return gspread.utils.rowcol_to_a1(_ledger_label_row(ws, col, label) + 2, col)


def _ledger_member_end(ws):
    """명단 마지막 행. 선수를 추가해 이월금 글자가 내려간 만큼 27행에서 늘어난다."""
    return LEDGER_MEMBER_END + _ledger_label_row(ws, *LEDGER_CARRY) - LEDGER_LABEL_ROW


def _ledger_spreadsheet():
    gc = gspread.service_account(filename=str(KEY_PATH))
    return gc.open_by_key(LEDGER_SHEET_ID)


def _ledger_ws():
    """올해(KST) 결산 탭을 쓴다. 아직 없으면 2026 탭(LEDGER_GID)을 쓴다."""
    from datetime import datetime, timedelta, timezone
    sh = _ledger_spreadsheet()
    year = datetime.now(timezone(timedelta(hours=9))).year
    try:
        return sh.worksheet(f"{year}년 결산")
    except gspread.WorksheetNotFound:
        return sh.get_worksheet_by_id(LEDGER_GID)


def ledger_gid():
    return _ledger_ws().id


def ensure_year_ledger(year):
    """year 결산 탭이 없으면 전년도 탭을 복제해 만든다.
    이월금(기본 D33)은 전년도 잔액(기본 J33)을 숫자로 넣고, 월별 회비와 기록 블록은 비운다. 이름·회비대상여부는 유지."""
    with _ledger_lock:
        sh = _ledger_spreadsheet()
        title = f"{year}년 결산"
        if any(w.title == title for w in sh.worksheets()):
            return False
        prev = sh.worksheet(f"{year - 1}년 결산")
        bal_cell = _ledger_value_cell(prev, LEDGER_BALANCE)
        balance = prev.acell(bal_cell, value_render_option="UNFORMATTED_VALUE").value
        if not isinstance(balance, (int, float)):
            raise ValueError(f"{year - 1}년 결산 {bal_cell}(잔액)을 숫자로 읽지 못했어요: {balance!r}")
        new = prev.duplicate(new_sheet_name=title)
        carry_cell = _ledger_value_cell(new, LEDGER_CARRY)
        new.batch_clear([f"D2:O{_ledger_member_end(new)}"])  # 명단 칸만 비운다 (이월금·잔액 글자는 그대로)
        requests = []
        for label_col, c1, c2 in LEDGER_BLOCKS.values():
            r = _ledger_total_row(new, label_col)
            if r > 2:  # 기록 줄을 위로 당겨서 계가 2행에 오게 한다 (블록 열만 이동)
                requests.append({"deleteRange": {"range": {"sheetId": new.id, "startRowIndex": 1,
                                                         "endRowIndex": r - 1, "startColumnIndex": c1 - 1,
                                                         "endColumnIndex": c2},
                                                "shiftDimension": "ROWS"}})
        if requests:
            new.spreadsheet.batch_update({"requests": requests})
        for kind, (label_col, _c1, _c2) in LEDGER_BLOCKS.items():
            _rewrite_total(new, kind, _ledger_total_row(new, label_col))
        new.update(values=[[balance]], range_name=carry_cell, value_input_option="USER_ENTERED")
    return True


def ledger_members():
    ws = _ledger_ws()
    return [n.strip() for n in ws.col_values(LEDGER_NAME_COL)[1:_ledger_member_end(ws)] if n.strip()]


def ledger_add(kind, values):
    """블록 kind 의 '계' 행 위치에 한 줄을 넣는다. 블록 열만 아래로 밀므로 다른 표는 그대로이고,
    합계 수식은 구글 시트가 범위를 자동으로 늘린다."""
    label_col, c1, c2 = LEDGER_BLOCKS[kind]
    with _ledger_lock:
        ws = _ledger_ws()
        col = ws.col_values(label_col)
        r = next((i for i, v in enumerate(col, start=1) if i > 1 and v.strip() == "계"), None)
        if r is None:
            raise ValueError(f"{kind} 블록에서 '계' 행을 찾지 못했어요.")
        ws.spreadsheet.batch_update({"requests": [{"insertRange": {
            "range": {"sheetId": ws.id, "startRowIndex": r - 1, "endRowIndex": r,
                      "startColumnIndex": c1 - 1, "endColumnIndex": c2},
            "shiftDimension": "ROWS"}}]})
        a1 = gspread.utils.rowcol_to_a1(r, c1) + ":" + gspread.utils.rowcol_to_a1(r, c2)
        ws.update(values=[values], range_name=a1, value_input_option="USER_ENTERED")
        _rewrite_total(ws, kind, r + 1)  # 계는 한 줄 아래로 내려갔으니 합계 범위를 다시 쓴다
    return r


def ledger_pay_dues(name, month):
    """회비 대상(O)인 선수의 해당 월 칸에 회비를 넣는다. 이미 값이 있으면 건드리지 않는다."""
    with _ledger_lock:
        ws = _ledger_ws()
        names = [n.strip() for n in ws.col_values(LEDGER_NAME_COL)]
        if name not in names[1:_ledger_member_end(ws)]:
            raise ValueError(f"결산 명단에 없는 이름이에요: {name}")
        row = names.index(name) + 1
        status = (ws.cell(row, LEDGER_DUES_COL).value or "").strip()
        if status != "O":
            raise ValueError(f"{name} 님은 회비 대상이 아니에요 (회비대상여부: {status or '-'})")
        col = LEDGER_MONTH_OFFSET + month
        cur = (ws.cell(row, col).value or "").strip()
        if cur:
            return f"{name} {month}월 회비는 이미 입력되어 있어요 ({cur})."
        ws.update(values=[[DUES_AMOUNT]], range_name=gspread.utils.rowcol_to_a1(row, col),
                  value_input_option="USER_ENTERED")
    return f"✅ {name} {month}월 회비 {DUES_AMOUNT:,}원 납부 처리 (결산 시트 반영)"


def _ledger_total_row(ws, label_col):
    col = ws.col_values(label_col)
    r = next((i for i, v in enumerate(col, start=1) if i > 1 and v.strip() == "계"), None)
    if r is None:
        raise ValueError("'계' 행을 찾지 못했어요.")
    return r


def ledger_view_range(kind):
    """캡처 범위: 회비는 명단 전체, 나머지는 블록 첫 행부터 '계' 행까지."""
    if kind == "회비":
        names = _ledger_ws().col_values(LEDGER_NAME_COL)
        last = max(i for i, v in enumerate(names, start=1) if i > 1 and v.strip())
        return f"A1:P{last + 1}"
    label_col, c1, c2 = LEDGER_BLOCKS[kind]
    r = _ledger_total_row(_ledger_ws(), label_col)
    return gspread.utils.rowcol_to_a1(1, c1) + ":" + gspread.utils.rowcol_to_a1(r, c2)


def export_range_png(sheet_id, gid, rng, out_png):
    """스프레드시트의 특정 탭·범위를 PDF 로 내보낸 뒤 PNG 한 장으로 만든다."""
    import subprocess
    import requests
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_service_account_file(
        str(KEY_PATH),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly"])
    creds.refresh(Request())
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {"format": "pdf", "gid": gid, "range": rng,
              "portrait": "false", "fitw": "true", "size": "A4"}
    r = requests.get(url, params=params, headers={"Authorization": f"Bearer {creds.token}"}, timeout=60)
    r.raise_for_status()
    pdf = out_png.with_suffix(".pdf")
    pdf.write_bytes(r.content)
    prefix = str(out_png.with_suffix(""))
    subprocess.run(["pdftoppm", "-png", "-r", "150", str(pdf), prefix], check=True, timeout=60)
    pages = sorted(Path(prefix).parent.glob(Path(prefix).name + "-*.png"))
    if not pages:
        raise RuntimeError("PDF 변환 결과가 없어요.")
    if len(pages) == 1:
        pages[0].replace(out_png)
    else:
        from PIL import Image
        imgs = [Image.open(p).convert("RGB") for p in pages]
        canvas = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs)), "white")
        y = 0
        for im in imgs:
            canvas.paste(im, (0, y))
            y += im.height
        canvas.save(out_png)
        for p in pages:
            p.unlink(missing_ok=True)
    pdf.unlink(missing_ok=True)
    return out_png


def ledger_pay_all(month):
    """회비 대상(O) 전원 중 해당 월 칸이 비어 있는 사람만 회비를 넣는다. 이미 입력된 칸은 건드리지 않는다."""
    with _ledger_lock:
        ws = _ledger_ws()
        names = ws.col_values(LEDGER_NAME_COL)
        last = max(i for i, v in enumerate(names, start=1) if i > 1 and v.strip())
        status = ws.col_values(LEDGER_DUES_COL)
        col = LEDGER_MONTH_OFFSET + month
        cells = ws.col_values(col)
        updates, paid = [], []
        for r in range(2, last + 1):
            name = names[r - 1].strip() if r - 1 < len(names) else ""
            st = status[r - 1].strip() if r - 1 < len(status) else ""
            cur = cells[r - 1].strip() if r - 1 < len(cells) else ""
            if name and st == "O" and not cur:
                updates.append({"range": gspread.utils.rowcol_to_a1(r, col), "values": [[DUES_AMOUNT]]})
                paid.append(name)
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
    if not paid:
        return f"{month}월 회비는 이미 모두 입력되어 있어요."
    return f"✅ {month}월 회비 완납 처리 {len(paid)}명 ({DUES_AMOUNT:,}원씩): {', '.join(paid)}"


LEDGER_AMOUNT_COL = {"이자": 20, "지각": 24, "노쇼": 28, "지출": 32}  # T, X, AB, AF


def _rewrite_total(ws, kind, total_row):
    """계 행 합계를 맨 윗줄부터 계 바로 위 줄까지로 다시 쓴다. 계 자신을 포함하면 순환 참조가 된다."""
    col = LEDGER_AMOUNT_COL[kind]
    letter = "".join(ch for ch in gspread.utils.rowcol_to_a1(1, col) if ch.isalpha())
    end = total_row - 1
    formula = f"=SUM({letter}2:{letter}{end})" if end >= 2 else "=0"
    ws.update(values=[[formula]], range_name=gspread.utils.rowcol_to_a1(total_row, col),
              value_input_option="USER_ENTERED")


def fix_ledger_totals():
    """현재 결산 탭 네 블록의 계 합계 수식을 바로잡는다 (여러 번 실행해도 안전)."""
    with _ledger_lock:
        ws = _ledger_ws()
        for kind, (label_col, _c1, _c2) in LEDGER_BLOCKS.items():
            _rewrite_total(ws, kind, _ledger_total_row(ws, label_col))


def ledger_set_dues_status(names, status, only_if=None):
    """결산 명단의 회비대상여부(C열)를 status 로 바꾼다. only_if 가 있으면 지금 값이 그것일 때만 바꾼다.
    반환 (바꾼 이름, 결산 명단에 없는 이름, 그대로 둔 이름)."""
    with _ledger_lock:
        ws = _ledger_ws()
        end = _ledger_member_end(ws)
        members = [n.strip() for n in ws.col_values(LEDGER_NAME_COL)][:end]
        current = ws.col_values(LEDGER_DUES_COL)
        changed, missing, kept, updates = [], [], [], []
        for name in names:
            if name not in members[1:]:
                missing.append(name)
                continue
            row = members.index(name, 1) + 1
            cur = current[row - 1].strip() if row - 1 < len(current) else ""
            if cur == status or (only_if is not None and cur != only_if):
                kept.append(name)
                continue
            updates.append({"range": gspread.utils.rowcol_to_a1(row, LEDGER_DUES_COL), "values": [[status]]})
            changed.append(name)
        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")
    return changed, missing, kept


def ledger_names_with_status(status):
    """회비대상여부(C열)가 status 인 선수 이름 목록."""
    ws = _ledger_ws()
    end = _ledger_member_end(ws)
    names = ws.col_values(LEDGER_NAME_COL)
    current = ws.col_values(LEDGER_DUES_COL)
    return [names[r - 1].strip() for r in range(2, min(end, len(names)) + 1)
            if names[r - 1].strip() and r - 1 < len(current) and current[r - 1].strip() == status]


def ledger_unpaid(month):
    """회비 대상(O)이면서 해당 월 칸이 비어 있는 선수 이름 목록."""
    with _ledger_lock:
        ws = _ledger_ws()
        names = ws.col_values(LEDGER_NAME_COL)
        last = max(i for i, v in enumerate(names, start=1) if i > 1 and v.strip())
        status = ws.col_values(LEDGER_DUES_COL)
        cells = ws.col_values(LEDGER_MONTH_OFFSET + month)
    unpaid = []
    for r in range(2, last + 1):
        name = names[r - 1].strip() if r - 1 < len(names) else ""
        st = status[r - 1].strip() if r - 1 < len(status) else ""
        cur = cells[r - 1].strip() if r - 1 < len(cells) else ""
        if name and st == "O" and not cur:
            unpaid.append(name)
    return unpaid


def ledger_delete(kind, date_str, key, amount):
    """날짜·이름(이자는 생략)·금액이 모두 맞는 기록 한 줄을 지우고 블록을 위로 당긴다.
    같은 조건이 여러 개면 지우지 않고 오류로 행 번호를 알려 준다."""
    from datetime import date as _date
    label_col, c1, c2 = LEDGER_BLOCKS[kind]
    target = (_date.fromisoformat(date_str) - _date(1899, 12, 30)).days  # 시트 날짜 일련번호
    with _ledger_lock:
        ws = _ledger_ws()
        r = _ledger_total_row(ws, label_col)
        if r <= 2:
            raise ValueError(f"{kind} 기록이 없어요.")
        rng = gspread.utils.rowcol_to_a1(2, c1) + ":" + gspread.utils.rowcol_to_a1(r - 1, c2)
        block = ws.get(rng, value_render_option="UNFORMATTED_VALUE")
        width = c2 - c1 + 1
        matches = []
        for i, row in enumerate(block):
            row = list(row) + [""] * (width - len(row))
            d, name, amt = row[0], str(row[1]).strip(), row[2]
            if not isinstance(d, (int, float)) or int(d) != target:
                continue
            if key is not None and name != key:
                continue
            if not isinstance(amt, (int, float)) or int(amt) != amount:
                continue
            matches.append(i + 2)
        if not matches:
            raise ValueError("해당 기록을 찾지 못했어요. 날짜·이름·금액을 확인해 주세요.")
        if len(matches) > 1:
            rows = ", ".join(str(x) for x in matches)
            raise ValueError(f"같은 기록이 {len(matches)}개예요 (시트 {rows}행). 구글 시트에서 직접 지워 주세요.")
        row = matches[0]
        ws.spreadsheet.batch_update({"requests": [{"deleteRange": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": c1 - 1, "endColumnIndex": c2},
            "shiftDimension": "ROWS"}}]})
        _rewrite_total(ws, kind, r - 1)  # 계가 한 줄 위로 올라왔으니 합계를 다시 쓴다
    return row


def export_blocks_png(kinds, out_png):
    """여러 결산 블록을 각각 PNG로 찍어 세로로 이어 붙인 한 장을 만든다."""
    from PIL import Image
    parts = []
    for kind in kinds:
        part = out_png.with_name(f"{out_png.stem}_{kind}.png")
        export_range_png(LEDGER_SHEET_ID, _ledger_ws().id, ledger_view_range(kind), part)
        parts.append(part)
    imgs = [Image.open(p).convert("RGB") for p in parts]
    canvas = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs)), "white")
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        y += im.height
    canvas.save(out_png)
    for p in parts:
        p.unlink(missing_ok=True)
    return out_png


def ledger_wide_range():
    """R1부터 지출 계 행까지, 지출 계 오른쪽 한 칸(AG)까지의 범위."""
    r = _ledger_total_row(_ledger_ws(), LEDGER_BLOCKS["지출"][0])
    return f"R1:AG{r}"


GRADE_I_FORMULA = '=(COUNTIF(C{r}:H{r},"A")*4)+(COUNTIF(C{r}:H{r},"B")*3)+(COUNTIF(C{r}:H{r},"C")*2)+(COUNTIF(C{r}:H{r},"D")*1)'
GRADE_J_FORMULA = ('=IF(RANK(I{r},$I$2:$I${last},0)<=ROUNDUP(COUNT($I$2:$I${last})*0.02,0),"A+",'
                   'IF(RANK(I{r},$I$2:$I${last},0)<=ROUNDUP(COUNT($I$2:$I${last})*0.1,0),"A",'
                   'IF(RANK(I{r},$I$2:$I${last},0)<=ROUNDUP(COUNT($I$2:$I${last})*0.3,0),"B+",'
                   'IF(RANK(I{r},$I$2:$I${last},0)<=ROUNDUP(COUNT($I$2:$I${last})*0.6,0),"B",'
                   'IF(RANK(I{r},$I$2:$I${last},0)<=ROUNDUP(COUNT($I$2:$I${last})*0.9,0),"C","D")))))')


_RANK_END_RE = re.compile(r"(:\$I\$)(\d+)")


def _extend_rank_ranges(ws, last):
    """J열 등급 수식의 순위 범위 끝(:$I$숫자)이 last 보다 작으면 last 로 늘린다.
    수식의 나머지 부분은 그대로 두고, 바뀐 칸만 다시 쓴다. 바꾼 칸 수를 돌려준다."""
    formulas = ws.get(f"J2:J{last}", value_render_option="FORMULA")
    updates = []
    for r, row in enumerate(formulas, start=2):
        f = row[0] if row else ""
        if not isinstance(f, str) or not f.startswith("="):
            continue
        g = _RANK_END_RE.sub(lambda m: m.group(1) + str(last) if int(m.group(2)) < last else m.group(0), f)
        if g != f:
            updates.append({"range": f"J{r}", "values": [[g]]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    return len(updates)


def add_member_rows(name, grades6, first, second):
    """신규 선수를 등급 시트(행 추가 + 합산·등급 수식)와 결산 명단(A~P열 행 추가, 회비대상 O)에 넣는다."""
    out = []
    with _lock:
        gc = gspread.service_account(filename=str(KEY_PATH))
        gws = gc.open_by_key(GRADE_SHEET_ID).worksheet(GRADE_TAB)
        gnames = [n.strip() for n in gws.col_values(2)]
        if name in gnames:
            out.append("등급 시트: 이미 있어요")
        else:
            L = max((i for i, v in enumerate(gnames, start=1) if i > 1 and v), default=1)
            new = L + 1
            requests = [{"insertDimension": {
                "range": {"sheetId": gws.id, "dimension": "ROWS", "startIndex": new - 1, "endIndex": new},
                "inheritFromBefore": True}}]
            if L >= 2:  # 합산(I)·등급(J) 수식은 바로 윗줄 수식을 복사한다 (C{r} 같은 상대 참조는 새 행에 맞게 바뀜)
                requests.append({"copyPaste": {
                    "source": {"sheetId": gws.id, "startRowIndex": L - 1, "endRowIndex": L,
                               "startColumnIndex": 8, "endColumnIndex": 10},
                    "destination": {"sheetId": gws.id, "startRowIndex": new - 1, "endRowIndex": new,
                                    "startColumnIndex": 8, "endColumnIndex": 10},
                    "pasteType": "PASTE_FORMULA"}})
            gws.spreadsheet.batch_update({"requests": requests})
            values = [{"range": f"A{new}:H{new}", "values": [[new - 1, name] + [grades6[k] for k in ABILITY_COLS]]},
                      {"range": f"K{new}:L{new}", "values": [[first, second]]}]
            if L < 2:  # 복사할 윗줄이 없으면 기본 수식을 넣는다
                values.append({"range": f"I{new}:J{new}", "values": [[GRADE_I_FORMULA.format(r=new),
                                                                       GRADE_J_FORMULA.format(r=new, last=new)]]})
            gws.batch_update(values, value_input_option="USER_ENTERED")
            fixed = _extend_rank_ranges(gws, new)
            out.append(f"등급 시트: {new}행에 추가 (등급 수식 {fixed}개 범위를 {new}행까지 늘림)")
    with _ledger_lock:
        lws = _ledger_ws()
        lnames = [n.strip() for n in lws.col_values(LEDGER_NAME_COL)]
        if name in lnames:
            out.append("결산 명단: 이미 있어요")
        else:
            end = _ledger_member_end(lws)
            L = max(i for i, v in enumerate(lnames, start=1) if i > 1 and v and i <= end)
            lws.spreadsheet.batch_update({"requests": [{"insertRange": {
                "range": {"sheetId": lws.id, "startRowIndex": L - 1, "endRowIndex": L,
                          "startColumnIndex": 0, "endColumnIndex": 16},
                "shiftDimension": "ROWS"}}]})
            lws.update(values=[["=ROW()-1", name, "O"]], range_name=f"A{L}:C{L}", value_input_option="USER_ENTERED")
            lws.update(values=[[f"=SUM(D{L}:O{L})"]], range_name=f"P{L}", value_input_option="USER_ENTERED")
            out.append(f"결산 명단: {L}행에 추가 (회비대상 O)")
    return "\n".join(out)


def remove_member_rows(name):
    """명단삭제: 등급 시트에서 선수 행을 통째로 지우고(아래 행은 위로, A열 번호 다시 매김),
    올해 결산 명단에서도 A~P열 행을 지운다(아래 명단·합계·이월금/잔액 칸이 위로 올라감).
    시트마다 따로 시도하고, 결과 줄을 모아 돌려준다."""
    out = []
    try:
        with _lock:
            gc = gspread.service_account(filename=str(KEY_PATH))
            gws = gc.open_by_key(GRADE_SHEET_ID).worksheet(GRADE_TAB)
            gnames = [n.strip() for n in gws.col_values(2)]
            if name not in gnames[1:]:
                out.append("등급 시트: 없는 이름이라 그대로")
            else:
                row = gnames.index(name, 1) + 1
                gws.spreadsheet.batch_update({"requests": [{"deleteDimension": {
                    "range": {"sheetId": gws.id, "dimension": "ROWS", "startIndex": row - 1, "endIndex": row}}}]})
                rest = [n.strip() for n in gws.col_values(2)]
                last = max((i for i, v in enumerate(rest, start=1) if i > 1 and v), default=1)
                if last >= row:  # 지운 행 아래 번호(A열)를 1부터 다시 매긴다
                    gws.update(values=[[r - 1] for r in range(row, last + 1)], range_name=f"A{row}:A{last}")
                out.append(f"등급 시트: {row}행 삭제")
    except Exception as e:
        out.append(f"⚠️ 등급 시트 삭제 실패: {e}")
    try:
        with _ledger_lock:
            lws = _ledger_ws()
            end = _ledger_member_end(lws)
            lnames = [n.strip() for n in lws.col_values(LEDGER_NAME_COL)][:end]
            if name not in lnames[1:]:
                out.append("결산 명단: 없는 이름이라 그대로")
            else:
                row = lnames.index(name, 1) + 1
                lws.spreadsheet.batch_update({"requests": [{"deleteRange": {
                    "range": {"sheetId": lws.id, "startRowIndex": row - 1, "endRowIndex": row,
                              "startColumnIndex": 0, "endColumnIndex": 16},
                    "shiftDimension": "ROWS"}}]})
                out.append(f"결산 명단: {row}행 삭제 (그 선수의 올해 회비 기록도 함께 삭제)")
    except Exception as e:
        out.append(f"⚠️ 결산 명단 삭제 실패: {e}")
    return "\n".join(out)
