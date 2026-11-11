#!/usr/bin/env python3
"""
적합 공고(사전규격 제외, 본공고만)를 구글 드라이브 폴더의 엑셀 한 장에 한 줄씩 쌓는다.

  열: 사업명(공고 링크) · 등록일 · 마감일 · 만들것 · 첨부파일(드라이브 폴더 링크) · 공고내용(공고내용.txt 링크를 글자 그대로)
  - 엑셀은 드라이브에서 내려받아 줄만 덧붙이고 같은 파일로 다시 올린다 → 링크가 바뀌지 않고, 사람이 고친 내용도 남는다.
  - 드라이브의 `첨부파일` 폴더 안에 공고마다 폴더를 만들어 첨부 전부를 올리고, 첨부파일 칸에 그 폴더 링크를 건다.
  - 이미 적합으로 올린 공고의 재공고는 새 줄 대신 그 줄의 마감일·링크만 고친다.
  - 같은 폴더에 `공고내용.txt`도 넣는다 — 공고 기본 정보 + 과업지시서·제안요청서·공고문 본문 전체(잘림 없이). HWP를 못 읽는
    프로그램(제안서 초안 작성 등)이 이 파일 하나만 읽으면 되도록. 양식·법령류는 이름만 적는다.

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

import ntis_collect as nc
from discord_post import end_date, gather_docs, log
from seen_state import same_title, title_key

HEADERS = ["사업명", "등록일", "마감일", "만들것", "첨부파일", "공고내용"]
WIDTHS = {"A": 48, "B": 12, "C": 12, "D": 70, "E": 14, "F": 60}
LINK = Font(color="0563C1", underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
TOP = Alignment(vertical="top")
MAX_UPLOAD = 30 * 1024 * 1024   # Apps Script 요청 한도 50MB (base64로 1.33배)
ATTACH = "첨부파일"   # 드라이브에서 공고별 첨부 폴더를 모아 두는 폴더 (drive_webapp.gs와 같은 이름)
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

    def upload(self, folder: str, name: str, path: Path, replace: bool = False) -> str:
        """파일 링크를 돌려준다. 같은 이름이 이미 있으면 그대로 두고, replace면 내용을 바꾼다."""
        mime = MIME.get(path.suffix.lower()) or mimetypes.guess_type(name)[0] or "application/octet-stream"   # 드라이브 미리보기용
        d = self.call(action="upload", folder=folder, name=name, mime=mime, replace=replace,
                      b64=base64.b64encode(path.read_bytes()).decode())
        if replace and d.get("existed"):
            log(f"  ! {name}: 웹앱이 옛 버전이라 내용을 바꾸지 못함(기존 파일 유지) — drive_webapp.gs를 새 버전으로 재배포해야 함")
        return d["url"]


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
        (self.root / ATTACH / name).mkdir(parents=True, exist_ok=True)
        return (self.root / ATTACH / name).resolve().as_uri()

    def upload(self, folder: str, name: str, path: Path, replace: bool = False) -> str:
        shutil.copyfile(path, self.root / ATTACH / folder / name)
        return (self.root / ATTACH / folder / name).resolve().as_uri()


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
        ws = wb.worksheets[0]
        for i, h in enumerate(HEADERS, 1):   # 나중에 추가된 열(공고내용)이 없는 예전 엑셀에는 머리글을 붙인다
            if ws.cell(1, i).value is None:
                ws.cell(1, i).value = h
                ws.cell(1, i).font, ws.cell(1, i).fill = Font(bold=True), PatternFill("solid", fgColor="D9E1F2")
                ws.cell(1, i).alignment = Alignment(horizontal="center", vertical="center")
                ws.column_dimensions[chr(64 + i)].width = WIDTHS[chr(64 + i)]
        return wb, ws
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


TEXT_NAME = "공고내용.txt"
TASK_DOC = re.compile(r"과업|제안\s*요청|규격|사양|RFP|내용서", re.I)
NOTICE_DOC = re.compile(r"공고")
EXT_ORDER = {".hwpx": 0, ".hwp": 1, ".docx": 2, ".pdf": 3, ".xlsx": 4, ".txt": 5}   # 같은 문서가 여러 형식이면 앞의 것을 읽는다 (PDF는 띄어쓰기가 깨진다)


def _pdf_pages(path: Path) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:  # noqa: BLE001
        return 0


def prespec_docs(it: dict, out: Path) -> list[tuple[dict, Path]]:
    """사전규격을 거친 나라장터 본공고는 첨부에 공고서만 있는 일이 많다 → 과업 문서가 없으면 사전규격의 규격서를 받아 온다."""
    no = (it.get("raw") or {}).get("bfSpecRgstNo") or ""
    key = os.environ.get("G2B_SERVICE_KEY", "")
    if not no or not key or any(TASK_DOC.search(a.get("name", "")) for a in it.get("attachments", [])):
        return []
    try:
        import urllib.parse

        import g2b_prespec_collect as pre
        r = pre.session.get(pre.URL, params={"serviceKey": urllib.parse.unquote(key) if "%" in key else key, "pageNo": 1, "numOfRows": 10,
                                             "inqryDiv": 2, "bfSpecRgstNo": no, "type": "json"}, timeout=60)
        rows = r.json()["response"]["body"].get("items") or []
        if not rows:
            return []
        d = pre.normalize(rows[-1])
        pre.download_docs(d, out / "bundle" / re.sub(r"[^0-9A-Za-z_-]", "_", it["uid"]) / "prespec")
        return [({"name": "(사전규격) " + a["name"]}, Path(a["path"])) for a in d["attachments"] if a.get("path")]
    except Exception as e:  # noqa: BLE001 — 못 받아도 본공고 첨부만으로 진행
        log(f"  ! 사전규격 문서 받기 실패 {no}: {str(e)[:120]}")
        return []


def notice_text(it: dict, r: dict, got: list[tuple[dict, Path]], missed: list[dict]) -> str:
    """공고 한 건의 내용을 txt 하나로: 기본 정보 → 첨부 목록(수록 상태) → 공고 본문 → 문서별 전문.
    과업지시서·제안요청서를 앞에, 양식·법령류는 이름만. 판정용 추출의 길이 상한은 여기서는 쓰지 않는다."""
    cap, zcap = nc.MAX_TEXT_PER_FILE, nc.ZIP_MAX_DOCS
    nc.MAX_TEXT_PER_FILE, nc.ZIP_MAX_DOCS = 5_000_000, 100
    try:
        status: list[tuple[str, str]] = []           # (첨부 이름, 수록 상태)
        docs: list[tuple[int, str, str]] = []        # (순서, 표시 이름, 본문)
        seen_stem: dict[str, str] = {}
        for att, p in sorted(got, key=lambda x: EXT_ORDER.get(x[1].suffix.lower(), 9)):
            name = att.get("name") or p.name
            stem = re.sub(r"\s+", "", Path(name).stem)
            if p.suffix.lower() not in nc.TEXT_EXTS | {".zip"}:
                status.append((name, "글자 추출 대상 아님 (원본 파일 참조)"))
                continue
            if nc.is_form_attachment(name) and not TASK_DOC.search(name):
                status.append((name, "이름만 (양식·법령·공통서류)"))
                continue
            if stem in seen_stem:
                status.append((name, f"생략 — '{seen_stem[stem]}'과 같은 문서의 다른 형식"))
                continue
            a = {"name": name}
            nc.extract_attachment(a, p)
            parts = [(label, t, form) for label, t, form in nc.attachment_docs(a) if not nc.FAILED_TEXT.match(t)]
            body = [(label, t) for label, t, form in parts if not form and sum('가' <= c <= '힣' for c in t) >= 100]   # 스캔본은 쪽 번호만 나온다
            if body and p.suffix.lower() == ".pdf" and sum('가' <= c <= '힣' for _, t in body for c in t) < 50 * _pdf_pages(p):
                body = []   # 화면 캡처로 만든 PDF(매뉴얼 등): 쪽 번호와 그림 속 번호만 글자로 나온다
            if not body:
                status.append((name, "이름만 (양식·법령·공통서류)" if parts and all(f for _, _, f in parts) else "읽지 못함 (스캔본·이미지·암호화 — 원본 파일 참조)"))
                continue
            seen_stem[stem] = name
            skipped = len(parts) - len(body)
            status.append((name, f"전체 수록 ({sum(len(t) for _, t in body):,}자)" + (f", 안의 양식 {skipped}개는 생략" if skipped else "")))
            for label, t in body:
                docs.append((0 if TASK_DOC.search(label) else 1 if NOTICE_DOC.search(label) else 2, label, t))
        status += [(a.get("name", "?"), "받지 못함 (공고 원문에서 직접 받아야 함)") for a in missed]
    finally:
        nc.MAX_TEXT_PER_FILE, nc.ZIP_MAX_DOCS = cap, zcap
    make = (r.get("card") or {}).get("make") or ""
    head = [f"# {it.get('title', '')}", "",
            f"- 공고 링크: {it.get('ntis_url', '')}",
            f"- 기관: {' / '.join(x for x in [it.get('dept'), it.get('agency')] if x) or '-'}",
            f"- 등록일: {it.get('announce_date') or '-'}  |  마감: {it.get('end') or '-'}",
            f"- 예산: {r.get('budget') or it.get('budget') or '-'}"]
    if make:
        head.append(f"- 만들 것: {make}")
    if r.get("conditions"):
        head += ["- 핵심 자격·조건 (판정 때 정리한 요약 — 정확한 문구는 아래 원문 확인):"] + [f"  - {c}" for c in r["conditions"]]
    lines = head + ["", "## 첨부 목록", ""] + ([f"- {n} — {st}" for n, st in status] or ["- (첨부 없음)"]) + [""]
    if (it.get("body") or "").strip():
        lines += ["## 공고 본문", "", it["body"].strip(), ""]
    for _, label, t in sorted(docs, key=lambda x: x[0]):
        lines += [f"## 첨부: {label}", "", t, ""]
    return "\n".join(lines)


def upload_docs(drive, it: dict, r: dict, out: Path, reg: dt.date | None, text_only: bool = False) -> tuple[str, int, str]:
    """공고 첨부와 공고내용.txt를 공고별 폴더에 올린다. 반환: (폴더 링크, 올린 첨부 개수 — txt 제외, 공고내용.txt 링크)
    text_only: 첨부는 다시 올리지 않고 공고내용.txt만 새로 만든다."""
    got, missed = gather_docs(it, out)
    got += prespec_docs(it, out)
    folder = safe_name((f"{reg:%y%m%d} " if reg else "") + it.get("title", it["uid"]), 70)
    url = drive.folder(folder)
    n, used, text_url = 0, {TEXT_NAME}, ""
    for att, p in ([] if text_only else got):
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
    try:
        txt = out / "bundle" / re.sub(r"[^0-9A-Za-z_-]", "_", it["uid"]) / TEXT_NAME
        txt.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text(notice_text(it, r, got, missed), "utf-8")
        text_url = drive.upload(folder, TEXT_NAME, txt, replace=True)
    except Exception as e:  # noqa: BLE001
        log(f"  ! {TEXT_NAME} 만들기·올리기 실패: {str(e)[:150]}")
    return url, n, text_url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("outs", nargs="+", help="수집 폴더들 (out, out/g2b, ...) — 없는 폴더·판정 없는 폴더는 건너뜀")
    ap.add_argument("--name", default="적합공고.xlsx", help="드라이브 폴더 안의 엑셀 파일 이름")
    ap.add_argument("--local", help="드라이브 대신 이 로컬 폴더에 만든다 (테스트)")
    ap.add_argument("--refresh-text", action="store_true",
                    help="이미 엑셀에 있는 공고도 공고내용.txt를 다시 만들어 바꿔 올리고 공고내용 칸을 채운다 (추출 방식을 고친 뒤)")
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
            if a.refresh_text:
                _, _, text_url = upload_docs(drive, it, r, out, end_date(it.get("announce_date", "")), text_only=True)
                if text_url:
                    set_link(ws.cell(rows[link], 6), text_url, text_url)
                    updated += 1
                    log(f"  공고내용 갱신: {it.get('title', '')[:50]}")
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
        folder, cnt, text_url = upload_docs(drive, it, r, out, reg)
        if text_url:   # 링크를 글자 그대로 적는다 — 프로그램이 칸의 글자만 읽어도 공고내용.txt를 찾을 수 있게
            set_link(ws.cell(n, 6), text_url, text_url)
            ws.cell(n, 6).alignment = TOP
        if cnt or not old:   # 첨부를 못 받은 공고(기업마당 등)도 폴더에 공고내용.txt는 들어 있다
            set_link(ws.cell(n, 5), f"첨부 {cnt}개" if cnt else "공고내용만", folder)
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
