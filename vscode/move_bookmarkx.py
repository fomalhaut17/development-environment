#!/usr/bin/env python3
"""VS Code -> Cursor : Bookmark X (tommyme.bookmarkx) 북마크 데이터 옮기기

사용법
  python move_bookmarkx.py           # 미리보기 (아무것도 바꾸지 않음)
  python move_bookmarkx.py --apply   # 실제 복사 (대상 DB는 먼저 백업)

VS Code와 Cursor를 모두 종료한 상태에서 실행하세요.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

PATTERN = "%bookmarkx%"          # 확장 ID에 포함되는 문자열
APPLY = "--apply" in sys.argv

WINDOWS = os.name == "nt"
if WINDOWS:
    base = Path(os.environ.get("APPDATA", ""))
elif sys.platform == "darwin":
    base = Path.home() / "Library" / "Application Support"
else:
    base = Path.home() / ".config"

CODE = base / "Code" / "User"
CURSOR = base / "Cursor" / "User"


def running(image):
    """image: 'Code' 또는 'Cursor'"""
    try:
        if WINDOWS:
            exe = image + ".exe"
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                capture_output=True, text=True, errors="ignore",
            ).stdout
            return exe.lower() in out.lower()
        pattern = {"Code": "Visual Studio Code.app|/code/code", "Cursor": "Cursor.app|/cursor/cursor"}[image]
        return subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True
        ).returncode == 0
    except OSError:
        return False


def read_rows(db):
    """DB에서 Bookmark X 관련 키/값을 읽는다 (읽기 전용)."""
    if not db.is_file():
        return []
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT key, value FROM ItemTable WHERE key LIKE ?", (PATTERN,)
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  ! 읽기 실패: {db} ({e})")
        return []
    finally:
        con.close()


def snapshot(db, backup):
    """쓰기 전에 되돌릴 수 있는 백업을 만든다.

    단순 파일 복사로는 부족하다. Cursor 쪽 state.vscdb 는 journal_mode 가
    wal 이어서 아직 본체에 반영되지 않은 내용이 state.vscdb-wal 에 남아
    있을 수 있다. 본체만 복사해 두면 그 백업을 되돌려도 원래 상태가 아니다.

    sqlite 백업 API 는 WAL 까지 반영된 일관된 스냅샷을 만든다.
    만든 뒤 열어서 확인하고, 확인되지 않으면 예외를 던져 쓰기를 막는다.
    """
    # 읽기 전용(mode=ro)으로 열면 안 된다. WAL 모드 DB는 -shm 을 만들어야
    # 열리는데 읽기 전용에서는 그걸 못 만들어 실패할 수 있다.
    # 어차피 곧 쓰게 될 DB이므로 일반 연결로 열어 WAL 복구까지 맡긴다.
    con = sqlite3.connect(db)
    try:
        bck = sqlite3.connect(backup)
        try:
            con.backup(bck)
        finally:
            bck.close()
    finally:
        con.close()

    # 백업이 실제로 열리고 읽히는지 확인한다. 여기서 실패하면 쓰지 않는다.
    chk = sqlite3.connect(backup)
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("백업 무결성 검사 실패")
        return chk.execute("SELECT count(*) FROM ItemTable").fetchone()[0]
    finally:
        chk.close()


def write_rows(db, rows):
    backup = db.with_name(db.name + time.strftime(".bak-%Y%m%d-%H%M%S"))
    try:
        n = snapshot(db, backup)
    except (sqlite3.Error, OSError) as e:
        backup.unlink(missing_ok=True)
        print(f"  ! 백업을 만들지 못해 쓰기를 건너뜁니다: {e}")
        return False

    con = sqlite3.connect(db)
    try:
        con.executemany(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)", rows
        )
        con.commit()
    finally:
        con.close()
    print(f"  -> 복사 완료 (백업: {backup.name}, {n:,}행)")
    return True


def workspace_id(folder):
    """workspace.json에서 프로젝트 식별값(폴더 또는 워크스페이스 파일 경로)을 얻는다."""
    f = folder / "workspace.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    uri = data.get("folder") or data.get("workspace")
    if isinstance(uri, dict):          # 예전 형식 대비
        uri = uri.get("configPath") or uri.get("path")
    if not uri:
        return None
    return unquote(str(uri)).lower().rstrip("/")


def describe(rows):
    for key, value in rows:
        size = len(value) if value is not None else 0
        print(f"     키: {key}  (값 크기 {size:,}자)")


def main():
    if not CODE.is_dir():
        sys.exit(f"VS Code 폴더가 없습니다: {CODE}")
    if not CURSOR.is_dir():
        sys.exit(f"Cursor 폴더가 없습니다: {CURSOR}")

    if APPLY and (running("Code") or running("Cursor")):
        sys.exit("VS Code 또는 Cursor가 실행 중입니다. 모두 종료한 뒤 다시 실행하세요.")

    print("모드:", "실제 복사" if APPLY else "미리보기 (--apply 를 붙이면 실제 복사)")
    found = 0
    pending = 0

    # 1) 전역 저장소
    print("\n[전역 저장소]")
    rows = read_rows(CODE / "globalStorage" / "state.vscdb")
    if rows:
        found += 1
        describe(rows)
        target = CURSOR / "globalStorage" / "state.vscdb"
        if not target.is_file():
            pending += 1
            print("  ! Cursor 쪽 전역 DB가 없습니다. Cursor를 한 번 실행했다 종료하세요.")
        elif APPLY:
            write_rows(target, rows)
        else:
            print(f"  -> 복사 예정: {target}")
    else:
        print("  (Bookmark X 데이터 없음)")

    # 2) 워크스페이스별 저장소
    print("\n[워크스페이스별 저장소]")
    cursor_ws = {}
    ws_root = CURSOR / "workspaceStorage"
    if ws_root.is_dir():
        for d in ws_root.iterdir():
            wid = workspace_id(d)
            if wid:
                cursor_ws[wid] = d

    any_ws = False
    code_ws_root = CODE / "workspaceStorage"
    for d in sorted(code_ws_root.iterdir()) if code_ws_root.is_dir() else []:
        rows = read_rows(d / "state.vscdb")
        if not rows:
            continue
        any_ws = True
        found += 1
        wid = workspace_id(d)
        print(f"\n  프로젝트: {wid or '(알 수 없음)'}")
        describe(rows)
        target_dir = cursor_ws.get(wid) if wid else None
        target = target_dir / "state.vscdb" if target_dir else None
        if not target or not target.is_file():
            pending += 1
            print("  ! Cursor에서 이 프로젝트를 같은 방식으로 한 번 열었다 닫은 뒤 다시 실행하세요.")
        elif APPLY:
            write_rows(target, rows)
        else:
            print(f"  -> 복사 예정: {target}")
    if not any_ws:
        print("  (Bookmark X 데이터 없음)")

    print()
    if not found:
        print("DB에서 Bookmark X 데이터를 찾지 못했습니다. "
              "프로젝트 폴더의 파일(.vscode 등)에 저장되는 방식일 수 있습니다.")
    elif pending:
        print(f"복사하지 못한 항목이 {pending}건 있습니다. 위 안내를 따른 뒤 다시 실행하세요.")
    elif not APPLY:
        print("문제가 없어 보이면 --apply 를 붙여 다시 실행하세요.")
    else:
        print("완료. Cursor를 실행해 북마크를 확인하세요.")


if __name__ == "__main__":
    main()
