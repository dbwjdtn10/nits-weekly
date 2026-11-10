#!/usr/bin/env python3
"""
적합 공고(사전규격 제외, 본공고만)를 구글 드라이브 폴더의 엑셀 한 장에 한 줄씩 쌓는다.

  열: 사업명(공고 링크) · 등록일 · 마감일 · 만들것 · 첨부파일(드라이브 폴더 링크)
  - 엑셀은 드라이브에서 내려받아 줄만 덧붙이고 같은 파일로 다시 올린다 → 링크가 바뀌지 않고, 사람이 고친 내용도 남는다.
  - 공고마다 드라이브에 폴더를 만들어 첨부 전부를 올리고, 첨부파일 칸에 그 폴더 링크를 건다.
  - 이미 적합으로 올린 공고의 재공고는 새 줄 대신 그 줄의 마감일·링크만 고친다.

드라이브 연결: 사용자 구글 계정의 Apps Script 웹앱(drive_webapp.gs)에 요청을 보낸다. 주소는 환경변수 DRIVE_WEBAPP_URL
(Discord 웹훅처럼 주소 자체가 비밀). 없으면 아무것도 하지 않고 끝낸다.

사용 (discord_post.py 다음에 실행 — 발송 때 받아 둔 첨부를 그대로 쓴다):
  python sheet_export.py out out/g2b out/chungbuk out/chungnam ...
  python sheet_export.py out/g2b --local out_test/drive     # 드라이브 대신 로컬 폴더에 만들어 보기
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from discord_post import end_date, gather_docs, log
from seen_state import same_title, title_key

HEADERS = ["사업명", "등록일", "마감일", "만들것", "첨부파일"]
WIDTHS = {"A": 48, "B": 12, "C": 12, "D": 70, "E": 14}
LINK = Font(color="0563C1", underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
MAX_UPLOAD = 30 * 1024 * 1024   # Apps Script 요청 한도 50MB (base64로 1.33배)
MIME = {".hwp": "application/x-hwp", ".hwpx": "application/hwp+zip"}


def safe_name(s: str, n: int = 80) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s)).strip()[:n].strip()


class Drive:
    """Apps Script 웹앱(drive_webapp.gs) 호출."""

    def __init__(self, url: str):
        self.url = url.strip()

    def call(self, **q) -> dict:
        err = ""
        for attempt in range(3):
            try:
                r = requests.post(self.url, data=json.dumps(q), headers={"Content-Type": "text/plain;charset=utf-8"}, timeout=300)
                if "application/json" not in r.headers.get("Content-Type", ""):
                    raise RuntimeError(f"웹앱 응답이 JSON이 아님 (HTTP {r.status_code}) — 배포의 액세스 권한이 '모든 사용자'인지, 주소가 /exec로 끝나는지 확인")
                d = r.json()
                if d.get("error"):
                    raise RuntimeError(f"웹앱 오류: {d['error']}")
                return d
            except Exception as e:  # noqa: BLE001
                err = re.sub(r"/macros/s/[^/\s'\"]+", "/macros/s/…", str(e))   # 주소가 비밀이라 로그에 남기지 않는다
                log(f"  ! 드라이브 요청 실패({q.get('action')}, {attempt + 1}/3): {err[:200]}")
                time.sleep(3)
        raise RuntimeError(err)

    def get_sheet(self, name: str) -> bytes | None:
        d = self.call(action="get", name=name)
        return base64.b64decode(d["b64"]) if d.get("exists") else None

    def put_sheet(self, name: str, data: bytes) -> str:
        d = self.call(action="put", name=name, b64=base64.b64encode(data).decode())
        if d.get("recreated"):
            log("  ! 엑셀을 새 파일로 교체함(파일 링크 바뀜) — 웹앱에 Drive API 서비스가 없거나 실패: " + str(d["recreated"])[:120])
        return d["url"]

    def folder(self, name: str) -> str:
        return self.call(action="folder", name=name)["url"]

    def upload(self, folder: str, name: str, path: Path) -> None:
        mime = MIME.get(path.suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream"   # 드라이브 미리보기용
        self.call(action="upload", folder=folder, name=name, mime=mime, b64=base64.b64encode(path.read_bytes()).decode())


class LocalDrive:
    """드라이브 대신 로컬 폴더 — 연결 없이 엑셀 모양을 확인할 때."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def get_sheet(self, name: str) -> bytes | None:
        p = self.root / name
        return p.read_bytes() if p.is_file() else None

    def put_sheet(self, name: str, data: bytes) -> str:
        (self.root / name).write_bytes(data)
        return str(self.root / name)

    def folder(self, name: str) -> str:
        (self.root / name).mkdir(exist_ok=True)
        return (self.root / name).resolve().as_uri()

    def upload(self, folder: str, name: str, path: Path) -> None:
        shutil.copyfile(path, self.root / folder / name)


def fits(outs: list[Path]):
    """적합 판정 본공고 (공고, 판정, 수집 폴더) — 사전규격 제외."""
    for out in outs:
        if not (out / "announcements.json").is_file() or not (out / "results.json").is_file():
            continue
        items = {it["uid"]: it for it in json.loads((out / "announcements.json").read_text("utf-8"))["items"]}
        for r in json.loads((out / "results.json").read_text("utf-8")).get("items", []):
            it = items.get(str(r["uid"]))
            if it and r.get("verdict") == "적합" and it.get("source") != "g2b_pre":
                yield it, r, out


def open_book(data: bytes | None):
    if data:
        wb = load_workbook(io.BytesIO(data))
        return wb, wb.worksheets[0]
    wb = Workbook()
    ws = wb.active
    ws.title = "적합 공고"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
        c.alignment = Alignment(horizontal="center", vertical="center")
    for col, w in WIDTHS.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    return wb, ws


def set_link(cell, text: str, url: str) -> None:
    cell.value = text
    cell.hyperlink = url
    cell.font = LINK


def set_date(cell, d: dt.date | None, raw: str) -> None:
    cell.value = d or (raw or None)
    cell.number_format = "yyyy-mm-dd"
    cell.alignment = TOP


def upload_docs(drive, it: dict, out: Path, reg: dt.date | None) -> tuple[str, int]:
    """공고 첨부를 공고별 폴더에 올린다. 반환: (폴더 링크, 올린 개수)"""
    got, _ = gather_docs(it, out)
    if not got:
        return "", 0
    folder = safe_name((f"{reg:%y%m%d} " if reg else "") + it.get("title", it["uid"]), 70)
    url = drive.folder(folder)
    n, used = 0, set()
    for att, p in got:
        name = safe_name(att.get("name") or p.name, 150)
        if name in used or p.stat().st_size > MAX_UPLOAD:
            log(f"  ! 올리지 않음(이름 중복·용량 초과): {name}")
            continue
        used.add(name)
        try:
            drive.upload(folder, name, p)
            n += 1
        except Exception:  # noqa: BLE001 — 한 파일이 실패해도 줄은 남긴다 (오류는 call()이 이미 기록)
            pass
    return url, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outs", nargs="+", help="수집 폴더들 (out, out/g2b, ...) — 없는 폴더·판정 없는 폴더는 건너뜀")
    ap.add_argument("--name", default="적합공고.xlsx", help="드라이브 폴더 안의 엑셀 파일 이름")
    ap.add_argument("--local", help="드라이브 대신 이 로컬 폴더에 만든다 (테스트)")
    a = ap.parse_args()

    url = os.environ.get("DRIVE_WEBAPP_URL", "").strip()
    if not a.local and not url:
        log("엑셀 정리 건너뜀: DRIVE_WEBAPP_URL 환경변수가 없습니다.")
        return 0
    todo = list(fits([Path(o) for o in a.outs]))
    if not todo:
        log("엑셀 정리: 오늘 적합 본공고 없음 — 엑셀은 그대로")
        return 0
    drive = LocalDrive(Path(a.local)) if a.local else Drive(url)
    wb, ws = open_book(drive.get_sheet(a.name))
    rows = {(ws.cell(n, 1).hyperlink.target if ws.cell(n, 1).hyperlink else ""): n for n in range(2, ws.max_row + 1)}
    added = updated = 0
    for it, r, out in todo:
        link = it.get("ntis_url", "")
        if link in rows:   # 같은 날 다시 실행된 경우
            continue
        reg, end = end_date(it.get("announce_date", "")), end_date(it.get("end", ""))
        old = None
        if (it.get("repost_of") or {}).get("verdict") == "적합" and it["repost_of"].get("source") != "g2b_pre":
            key = title_key(it.get("title", ""))   # 이미 올린 공고의 재공고·정정공고 → 그 줄을 고친다
            old = next((n for n in range(2, ws.max_row + 1) if same_title(key, title_key(str(ws.cell(n, 1).value or "")))), None)
        n = old or ws.max_row + 1
        set_link(ws.cell(n, 1), it.get("title", ""), link)
        ws.cell(n, 1).alignment = WRAP
        set_date(ws.cell(n, 3), end, it.get("end", ""))
        make = (r.get("card") or {}).get("make") or ""
        if not old:
            set_date(ws.cell(n, 2), reg, it.get("announce_date", ""))
        if make or not old:
            ws.cell(n, 4).value = make
            ws.cell(n, 4).alignment = WRAP
        folder, cnt = upload_docs(drive, it, out, reg)
        if cnt:
            set_link(ws.cell(n, 5), f"첨부 {cnt}개", folder)
        elif not old:   # 받지 못한 첨부(기업마당 등)는 공고 원문에서
            set_link(ws.cell(n, 5), "원문에서 받기", link)
        ws.cell(n, 5).alignment = TOP
        rows[link] = n
        added, updated = added + (not old), updated + bool(old)
        log(f"  {'고침' if old else '추가'}: {it.get('title', '')[:50]} (첨부 {cnt}개)")
    if added or updated:
        buf = io.BytesIO()
        wb.save(buf)
        where = drive.put_sheet(a.name, buf.getvalue())
        log(f"엑셀 정리 완료: {added}줄 추가, {updated}줄 고침 → {a.name}" + (f" ({where})" if a.local else ""))
    else:
        log("엑셀 정리: 새로 올릴 공고 없음 (이미 올라가 있음)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
