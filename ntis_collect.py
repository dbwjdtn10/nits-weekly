#!/usr/bin/env python3
"""
NTIS 국가R&D통합공고 수집기.

지정 기간(기본: 지난주 월~일)에 등록된 공고를 NTIS(https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do)에서
수집하고, 상세 페이지 본문과 첨부파일(PDF/HWP/HWPX/DOCX)을 내려받아 텍스트를 추출한다.
로그인/RSS 불필요.

출력 (기본 out/):
  announcements.json     - 공고 메타데이터 + 본문 + 첨부 텍스트 (기계용)
  digest.md              - 사람/LLM이 읽기 위한 전체 요약본
  brief/<uid>.md         - 공고별 요약본 (자격·대상·분야·규모·기간 관련 구간만 발췌) ← 판정용
  text/<uid>.md          - 공고별 전문 (본문 + 첨부 텍스트)
  attachments/<uid>/     - 원본 첨부파일

사용:
  python ntis_collect.py                      # 지난주 월~일
  python ntis_collect.py --from 2026-09-07 --to 2026-09-13
  python ntis_collect.py --days 7             # 오늘 포함 최근 7일
"""
from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo
import html
import io
import json
import re
import struct
import sys
import time
import zipfile
import zlib
from pathlib import Path

import requests

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)

from seen_state import seen_uids

BASE = "https://www.ntis.go.kr"
LIST_URL = f"{BASE}/rndgate/eg/un/ra/mng.do"
VIEW_URL = f"{BASE}/rndgate/eg/un/ra/view.do"
FILE_URL = f"{BASE}/rndgate/eg/cmm/file/download.do"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) X2R-NTIS-collector/1.0",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
MAX_TEXT_PER_FILE = 40_000   # 첨부 1개당 추출 텍스트 상한(문자)
MAX_ATTACH_BYTES = 30 * 1024 * 1024
TEXT_EXTS = {".pdf", ".hwp", ".hwpx", ".docx", ".xlsx", ".txt"}
ZIP_MAX_DOCS = 20            # ZIP 1개에서 추출할 내부 문서 수 상한

session = requests.Session()
session.headers.update(HEADERS)


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def strip_tags(fragment: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", fragment, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", t, flags=re.I)
    t = re.sub(r"</t[dh]>", " | ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t\r]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def get(url: str, retries: int = 3, **kw) -> requests.Response:
    for i in range(retries):
        try:
            r = session.get(url, timeout=60, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            log(f"  ! GET 실패({i + 1}/{retries}) {url}: {e}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET 실패: {url}")


def post(url: str, data: dict, retries: int = 3, **kw) -> requests.Response:
    for i in range(retries):
        try:
            r = session.post(url, data=data, timeout=120, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            log(f"  ! POST 실패({i + 1}/{retries}) {url}: {e}")
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"POST 실패: {url}")


# --------------------------------------------------------------------------- list
ROW_RE = re.compile(
    r"fn_view\('(?P<uid>\d+)'\).*?title=\"(?P<title>[^\"]*)\".*?"
    r"data-title=\"부처명\">(?P<dept>[^<]*)<.*?"
    r"data-title=\"접수일\">(?P<start>[^<]*)<.*?"
    r"data-title=\"마감일\">(?P<end>[^<]*)<",
    re.S,
)
STATUS_RE = re.compile(r"data-title=\"현황\">(.*?)</td>", re.S)


def fetch_list(date_from: str, date_to: str) -> list[dict]:
    """기간 내 공고 목록. 날짜는 YYYY-MM-DD."""
    rows: list[dict] = []
    page = 1
    while True:
        data = {
            "pageIndex": page,
            "pageUnit": 100,
            "searchCondition2": date_from,
            "searchCondition3": date_to,
            "searchFormList": "",
            "searchStatusList": "",
            "searchDeptList": "",
            "searchKeyword": "",
            "isFileSearch": "false",
            "isRoDptNameSearch": "false",
        }
        h = post(LIST_URL, data).text
        total_m = re.search(r'id="totalCount" value="(\d+)"', h)
        total = int(total_m.group(1)) if total_m else 0
        # 행 단위로 분할해 현황 정보까지 함께 파싱
        chunks = re.split(r"<tr>", h)
        n_before = len(rows)
        for c in chunks:
            m = ROW_RE.search(c)
            if not m:
                continue
            st = STATUS_RE.search(c)
            rows.append(
                {
                    "uid": m["uid"],
                    "title": clean_ws(m["title"]),
                    "dept": clean_ws(m["dept"]),
                    "status": clean_ws(strip_tags(st.group(1))) if st else "",
                    "start": clean_ws(m["start"]),
                    "end": clean_ws(m["end"]),
                }
            )
        got = len(rows) - n_before
        log(f"목록 {page}페이지: {got}건 (누적 {len(rows)}/{total})")
        if got == 0 or len(rows) >= total:
            break
        page += 1
        time.sleep(0.5)
    # 중복 제거(순서 유지)
    seen, uniq = set(), []
    for r in rows:
        if r["uid"] not in seen:
            seen.add(r["uid"])
            uniq.append(r)
    return uniq


# --------------------------------------------------------------------------- detail
def fetch_detail(uid: str) -> dict:
    h = get(VIEW_URL, params={"roRndUid": uid, "flag": "rndList"}).text
    d: dict = {"uid": uid, "ntis_url": f"{VIEW_URL}?roRndUid={uid}&flag=rndList"}

    m = re.search(r'<h1 class="ditail_tit">(.*?)</h1>', h, re.S)
    d["title"] = clean_ws(strip_tags(m.group(1))) if m else ""

    m = re.search(r"window\.open\('([^']+)'\)[^>]*>\s*공고문 바로가기", h)
    d["source_url"] = m.group(1) if m else ""

    # summary 항목들: <li><span>라벨 : </span>값</li>
    meta = {}
    for lab, val in re.findall(r"<li[^>]*>\s*<span>([^<]*?)\s*:\s*</span>(.*?)</li>", h, re.S):
        meta[clean_ws(lab)] = clean_ws(strip_tags(val))
    d["meta"] = meta
    d["form"] = meta.get("공고형태", "")
    d["dept"] = meta.get("부처명", "")
    d["agency"] = meta.get("공고기관명", "")
    d["announce_date"] = meta.get("공고일", "")
    d["start"] = meta.get("접수일", "")
    d["end"] = meta.get("마감일", "")
    d["program"] = meta.get("사업명", "")
    d["budget"] = meta.get("지원규모") or meta.get("사업규모") or (f"공고금액 {meta['공고금액']}" if meta.get("공고금액") else "")
    d["contact"] = meta.get("문의처", "")

    # 첨부파일
    atts = []
    for wf, ro, name in re.findall(
        r"fn_fileDownload\('(\d+)',\s*'([^']+)'\);\s*return false;\">(.*?)</a>", h, re.S
    ):
        atts.append({"wfUid": wf, "roTextUid": ro, "name": clean_ws(strip_tags(name))})
    d["attachments"] = atts

    # 본문: notice_view 영역에서 summary/첨부 이후 ~ 버튼 영역 이전
    body = ""
    m = re.search(r'<div class="notice_view">(.*)', h, re.S)
    if m:
        seg = m.group(1)
        # 첨부 블록 이후가 본문
        k = seg.find('class="summary_file"')
        if k > 0:
            seg = seg[k:]
            k2 = seg.find("</ul>")
            seg = seg[k2 + 5:] if k2 > 0 else seg
        seg = re.sub(r"<!--.*?-->", "", seg, flags=re.S)
        for stop in ['class="btn_area"', 'class="btn_list"', 'id="footer"', "<footer", "MY공고 추가", "<script"]:
            k = seg.find(stop)
            if k > 0:
                seg = seg[:k]
        body = strip_tags(seg)
    d["body"] = body
    return d


# --------------------------------------------------------------------------- attachments
def download_attachment(att: dict, dest_dir: Path) -> Path | None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", att["name"])[:150] or f"file_{att['wfUid']}"
    path = dest_dir / safe
    if path.exists() and path.stat().st_size > 0:
        return path
    r = post(FILE_URL, {"wfUid": att["wfUid"], "roTextUid": att["roTextUid"]}, stream=True)
    size, too_big = 0, False
    with open(path, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
            size += len(chunk)
            if size > MAX_ATTACH_BYTES:
                too_big = True
                break
    if size == 0 or too_big:
        path.unlink(missing_ok=True)
        if too_big:
            log(f"  ! 첨부 용량 초과({MAX_ATTACH_BYTES // 2**20}MB)로 생략: {att['name']}")
            att["skipped"] = "too_big"
        return None
    return path


def extract_pdf(path: Path) -> str:
    from pypdf import PdfReader

    out = []
    reader = PdfReader(str(path))
    for pg in reader.pages:
        try:
            out.append(pg.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
        if sum(map(len, out)) > MAX_TEXT_PER_FILE:
            break
    return "\n".join(out)


def _hwp_records(data: bytes):
    """HWP 5.0 레코드 스트림 순회 -> (tag_id, level, payload)."""
    i, n = 0, len(data)
    while i + 4 <= n:
        (hdr,) = struct.unpack_from("<I", data, i)
        i += 4
        tag = hdr & 0x3FF
        level = (hdr >> 10) & 0x3FF
        size = (hdr >> 20) & 0xFFF
        if size == 0xFFF:
            (size,) = struct.unpack_from("<I", data, i)
            i += 4
        yield tag, level, data[i : i + size]
        i += size


HWPTAG_PARA_TEXT = 0x10 + 51  # 67


def _hwp_para_text(payload: bytes) -> str:
    chars = []
    j, n = 0, len(payload) - 1
    while j < n:
        code = payload[j] | (payload[j + 1] << 8)
        if code < 32:
            if code in (10, 13):
                chars.append("\n")
                j += 2
            elif code == 0 or 24 <= code <= 31:
                j += 2
            else:  # 인라인/확장 컨트롤: 8글자(16바이트)
                if code == 9:
                    chars.append(" ")
                j += 16
        else:
            chars.append(chr(code))
            j += 2
    return "".join(chars)


def extract_hwp(path: Path) -> str:
    import olefile

    if not olefile.isOleFile(str(path)):
        # 확장자만 hwp인 hwpx/기타일 수 있음
        if zipfile.is_zipfile(str(path)):
            return extract_hwpx(path)
        return ""
    ole = olefile.OleFileIO(str(path))
    try:
        header = ole.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        compressed = bool(flags & 1)
        encrypted = bool(flags & 2)
        if encrypted:
            return "[암호화된 HWP 문서: 텍스트 추출 불가]"
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText"),
            key=lambda e: int(re.sub(r"\D", "", e[1]) or 0),
        )
        out = []
        for e in sections:
            raw = ole.openstream("/".join(e)).read()
            if compressed:
                try:
                    raw = zlib.decompress(raw, -15)
                except zlib.error:
                    raw = zlib.decompressobj(-15).decompress(raw)
            for tag, _lvl, payload in _hwp_records(raw):
                if tag == HWPTAG_PARA_TEXT:
                    out.append(_hwp_para_text(payload))
            if sum(map(len, out)) > MAX_TEXT_PER_FILE:
                break
        text = "\n".join(out)
        if not text.strip() and ole.exists("PrvText"):
            text = ole.openstream("PrvText").read().decode("utf-16le", "ignore")
        return text
    finally:
        ole.close()


def _xml_text(xml: bytes) -> str:
    s = xml.decode("utf-8", "ignore")
    s = re.sub(r"</(hp:p|w:p|hp:tr|w:tr)>", "\n", s)
    s = re.sub(r"</(hp:tc|w:tc)>", " | ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s)


def extract_hwpx(path: Path) -> str:
    out = []
    with zipfile.ZipFile(str(path)) as z:
        names = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml", n))
        for n in names:
            out.append(_xml_text(z.read(n)))
            if sum(map(len, out)) > MAX_TEXT_PER_FILE:
                break
    return "\n".join(out)


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(str(path)) as z:
        return _xml_text(z.read("word/document.xml"))


def _strip_xml(b: bytes) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", b.decode("utf-8", "ignore")))


def extract_xlsx(path: Path) -> str:
    """시트별 행을 ' | '로 이어 붙인 텍스트 (서식·수식 무시)."""
    out: list[str] = []
    with zipfile.ZipFile(str(path)) as z:
        names = z.namelist()
        # 태그에 네임스페이스 접두어(<x:row>)가 붙는 파일도 있다
        shared = [_strip_xml(si) for si in re.findall(rb"<(?:\w+:)?si\b[^>]*>(.*?)</(?:\w+:)?si>", z.read("xl/sharedStrings.xml"), re.S)] \
            if "xl/sharedStrings.xml" in names else []
        sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.sub(r"\D", "", n)))
        for n in sheets:
            for row in re.findall(rb"<(?:\w+:)?row\b[^>]*>(.*?)</(?:\w+:)?row>", z.read(n), re.S):
                row = re.sub(rb"<(?:\w+:)?c\b[^>]*/>", b"", row)   # 빈 셀
                cells = []
                for attrs, inner in re.findall(rb"<(?:\w+:)?c\b([^>]*)>(.*?)</(?:\w+:)?c>", row, re.S):
                    v = re.search(rb"<(?:\w+:)?v>(.*?)</(?:\w+:)?v>", inner, re.S)
                    if b't="s"' in attrs and v:
                        i = int(v.group(1))
                        cells.append(shared[i] if i < len(shared) else "")
                    elif b't="inlineStr"' in attrs:
                        cells.append(_strip_xml(inner))
                    elif v:
                        cells.append(v.group(1).decode("utf-8", "ignore"))
                if any(c.strip() for c in cells):
                    out.append(" | ".join(cells))
            if sum(map(len, out)) > MAX_TEXT_PER_FILE:
                break
    return "\n".join(out)


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    if info.flag_bits & 0x800:   # UTF-8 파일명 플래그
        return info.filename
    try:   # 한글 윈도우 압축 프로그램은 플래그 없이 CP949로 저장 → zipfile이 CP437로 잘못 해석
        return info.filename.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def extract_zip(path: Path) -> list[dict]:
    """ZIP 첨부를 풀어 내부 문서별 텍스트 [{name, text}]. 중첩 ZIP은 풀지 않는다."""
    dest = path.parent / f"{path.stem}_unzip"
    parts: list[dict] = []
    with zipfile.ZipFile(str(path)) as z:
        for info in z.infolist():
            name = _zip_member_name(info)
            if info.is_dir() or "__MACOSX" in name or Path(name).suffix.lower() not in TEXT_EXTS:
                continue
            if info.file_size > MAX_ATTACH_BYTES:
                log(f"  ! ZIP 내부 파일 용량 초과로 생략: {name}")
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
                p = dest / (f"{len(parts):02d}_" + re.sub(r'[\\/:*?"<>|]+', "_", Path(name).name)[:150])
                p.write_bytes(z.read(info))
                parts.append({"name": name, "text": extract_text(p)})
            except Exception as e:  # noqa: BLE001  (암호 ZIP 등)
                parts.append({"name": name, "text": f"[텍스트 추출 실패: {e}]"})
            if len(parts) >= ZIP_MAX_DOCS:
                break
    return parts


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            t = extract_pdf(path)
        elif ext == ".hwp":
            t = extract_hwp(path)
        elif ext == ".hwpx":
            t = extract_hwpx(path)
        elif ext == ".docx":
            t = extract_docx(path)
        elif ext == ".xlsx":
            t = extract_xlsx(path)
        elif ext == ".txt":
            t = path.read_text("utf-8", errors="ignore")
        else:
            return ""
    except Exception as e:  # noqa: BLE001
        log(f"  ! 텍스트 추출 실패 {path.name}: {e}")
        return f"[텍스트 추출 실패: {e}]"
    t = re.sub(r"[\ud800-\udfff]", "", t)   # HWP 추출 시 섞이는 깨진 서로게이트 제거
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", t)   # 제어문자(NUL 등)가 섞이면 grep이 바이너리로 취급
    t = re.sub(r"[ \t\r\xa0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    return t[:MAX_TEXT_PER_FILE]



# --------------------------------------------------------------------------- brief
BRIEF_KEYWORDS = re.compile(
    r"(신청\s*자격|지원\s*자격|참여\s*자격|응모\s*자격|신청\s*대상|지원\s*대상|공모\s*대상|참여\s*대상|"
    r"지원\s*분야|공모\s*분야|지원\s*내용|지원\s*규모|사업\s*규모|지원\s*예산|정부\s*지원|연구개발비|"
    r"사업\s*개요|사업\s*목적|추진\s*목적|접수\s*기간|신청\s*기간|공고\s*기간|제출\s*기한|접수\s*마감|"
    r"신청\s*방법|접수\s*방법|지원\s*제외|제외\s*대상|참여\s*제한|신청\s*제한|부채\s*비율|자본\s*잠식|"
    r"우대|가점|주관\s*연구개발기관|주관\s*기관|공동\s*연구개발기관|컨소시엄|중소기업|창업\s*기업|소재지|"
    r"기업부설연구소|민간\s*부담|기관\s*부담|기술료|평가\s*기준|선정\s*평가)"
)
BRIEF_WINDOW_BEFORE = 150
BRIEF_WINDOW_AFTER = 1400
BRIEF_MAX_PER_SOURCE = 9000
BRIEF_HARD_CAP = 12000   # 키워드가 촘촘해 구간이 하나로 이어져도 이 이상은 자른다


def make_brief(text: str, head: int = 1200) -> str:
    """긴 공고문에서 자격·대상·분야·규모·기간 관련 구간만 잘라낸다."""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= head + BRIEF_WINDOW_AFTER:
        return text
    spans: list[list[int]] = [[0, head]]
    for m in BRIEF_KEYWORDS.finditer(text):
        a, b = max(0, m.start() - BRIEF_WINDOW_BEFORE), min(len(text), m.end() + BRIEF_WINDOW_AFTER)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    out, total = [], 0
    for a, b in spans:
        seg = text[a:b].strip()
        if not seg:
            continue
        if total + len(seg) > BRIEF_HARD_CAP:
            out.append(seg[: BRIEF_HARD_CAP - total] + "\n[… 발췌 상한 — 이후는 text/ 전문 참조]")
            break
        out.append(seg)
        total += len(seg)
        if total > BRIEF_MAX_PER_SOURCE:
            break
    return "\n\n[…]\n\n".join(out)


def is_form_attachment(name: str) -> bool:
    """양식/서식/매뉴얼류 첨부는 brief에서 제외."""
    return bool(re.search(r"(양식|서식|신청서|계획서|증빙|매뉴얼|안내서\s*\(|동의서|체크리스트|FAQ|법률|법령|시행령|시행규칙|관련\s*규정|가이드라인|IRIS)", name, re.I)) and not re.search(r"공고문|공고|RFP|제안요청", name, re.I)


FAILED_TEXT = re.compile(r"^\[(텍스트 추출 실패|암호화된)")


def extract_attachment(att: dict, path: Path) -> None:
    """내려받은 첨부의 텍스트를 att['text']에, ZIP이면 내부 문서별로 att['parts']에 넣는다."""
    ext = path.suffix.lower()
    if ext == ".zip":
        try:
            att["parts"] = extract_zip(path)
        except zipfile.BadZipFile as e:
            att["text"] = f"[텍스트 추출 실패: ZIP 해제 불가 {e}]"
    elif ext in TEXT_EXTS:
        att["text"] = extract_text(path)


def attachment_docs(att: dict) -> list[tuple[str, str, bool]]:
    """첨부 → [(표시 이름, 텍스트, 양식류 여부)]. ZIP은 내부 문서별로 펼친다."""
    if "parts" in att:
        zip_is_form = is_form_attachment(att["name"])
        return [
            (f"{att['name']} › {p['name']}", p["text"],
             is_form_attachment(p["name"]) or (zip_is_form and not re.search(r"공고|RFP|제안요청", p["name"], re.I)))
            for p in att["parts"] if p["text"]
        ]
    return [(att["name"], att["text"], is_form_attachment(att["name"]))] if att.get("text") else []


def attachment_log(att: dict) -> str:
    docs = attachment_docs(att)
    n = sum(len(t) for _, t, _ in docs)
    return f"ZIP 내부 문서 {len(docs)}개, 텍스트 {n}자" if "parts" in att else f"텍스트 {n}자"


def attachment_sections(atts: list[dict], excerpt=make_brief, heading: str = "첨부 (발췌)") -> tuple[list[str], list[str]]:
    """첨부 목록 → (brief 줄, text 줄). brief는 양식류를 목록만 남기고, 읽지 못한 첨부를 따로 표시한다."""
    brief: list[str] = []
    text: list[str] = []
    forms: list[str] = []
    unread: list[str] = []
    for a in atts:
        docs = attachment_docs(a)
        if not docs:
            text += [f"## 첨부: {a['name']}", "", "(텍스트 추출 없음/불가)", ""]
            if a.get("path") or a.get("skipped"):   # 내려받았는데(또는 용량 초과로) 못 읽은 것
                unread.append(a["name"] + (" (용량 초과)" if a.get("skipped") == "too_big" else ""))
            continue
        for label, t, form in docs:
            text += [f"## 첨부: {label}", "", t, ""]
            if FAILED_TEXT.match(t):
                unread.append(f"{label} {t[:60]}")
            elif form:
                forms.append(label)
            else:
                brief += [f"## {heading}: {label}", "", excerpt(t), ""]
    if forms:
        brief += ["## 발췌 생략(양식류) — 필요시 text/ 전문 참조", ""] + [f"- {n}" for n in forms] + [""]
    if unread:
        brief += ["## ⚠ 읽지 못한 첨부 (이미지·스캔본·암호화·비지원 형식) — 자격·분야가 여기에만 있을 수 있음", ""] \
            + [f"- {n}" for n in unread] + [""]
    return brief, text


# --------------------------------------------------------------------------- main
def default_range(days: int | None) -> tuple[str, str]:
    today = now_kst().date()
    if days:
        return (today - dt.timedelta(days=days - 1)).isoformat(), today.isoformat()
    this_mon = today - dt.timedelta(days=today.weekday())
    last_mon = this_mon - dt.timedelta(days=7)
    last_sun = this_mon - dt.timedelta(days=1)
    return last_mon.isoformat(), last_sun.isoformat()


def dday(end: str) -> str:
    m = re.match(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", end or "")
    if not m:
        return ""
    e = dt.date(int(m[1]), int(m[2]), int(m[3]))
    n = (e - now_kst().date()).days
    return f"D-{n}" if n >= 0 else f"마감({-n}일 경과)"


def write_outputs(items: list[dict], out: Path, date_from: str, date_to: str) -> None:
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "brief").mkdir(parents=True, exist_ok=True)
    (out / "announcements.json").write_text(
        json.dumps(
            {"range": [date_from, date_to], "source": "ntis", "collected_at": now_kst().isoformat(), "items": items},
            ensure_ascii=False,
            indent=1,
        ),
        "utf-8",
    )
    lines = [f"# NTIS 국가R&D통합공고 수집 결과 ({date_from} ~ {date_to})", "", f"총 {len(items)}건", ""]
    for it in items:
        lines += [
            f"## [{it['uid']}] {it['title']}",
            f"- 부처/기관: {it['dept']} / {it['agency']}",
            f"- 공고형태: {it['form']} | 사업명: {it['program']}",
            f"- 공고일 {it['announce_date']} | 접수 {it['start']} ~ 마감 {it['end']} ({dday(it['end'])})",
            f"- 지원규모: {it['budget']}",
            f"- NTIS: {it['ntis_url']}",
            f"- 원문: {it['source_url']}",
            f"- 첨부({len(it['attachments'])}): " + ", ".join(a["name"] for a in it["attachments"]),
            f"- 요약본: brief/{it['uid']}.md | 전문: text/{it['uid']}.md",
            "",
        ]
    (out / "digest.md").write_text("\n".join(lines), "utf-8")

    for it in items:
        parts = [
            f"# {it['title']}",
            "",
            f"- UID: {it['uid']}",
            f"- 부처/기관: {it['dept']} / {it['agency']}",
            f"- 공고형태: {it['form']} | 사업명: {it['program']}",
            f"- 공고일 {it['announce_date']} | 접수 {it['start']} ~ 마감 {it['end']}",
            f"- 지원규모: {it['budget']} | 문의: {it['contact']}",
            f"- NTIS: {it['ntis_url']}",
            f"- 원문: {it['source_url']}",
            "",
            "## 본문",
            "",
            it["body"] or "(본문 없음)",
            "",
        ]
        # brief: 메타 + 본문 발췌 + 공고문류 첨부 발췌 (양식류 제외, ZIP 내부 문서 포함)
        att_brief, att_text = attachment_sections(it["attachments"])
        (out / "text" / f"{it['uid']}.md").write_text("\n".join(parts + att_text), "utf-8")
        bparts = parts[:10] + ["## 본문 (발췌)", "", make_brief(it["body"]) or "(본문 없음)", ""] + att_brief
        (out / "brief" / f"{it['uid']}.md").write_text("\n".join(bparts), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    ap.add_argument("--days", type=int, help="오늘 포함 최근 N일")
    ap.add_argument("--out", default="out")
    ap.add_argument("--no-attach", action="store_true", help="첨부파일 다운로드/추출 생략")
    ap.add_argument("--limit", type=int, help="상세 수집 건수 제한(테스트용)")
    ap.add_argument("--no-seen", action="store_true", help="state/seen.json 무시(이미 판정한 공고도 수집)")
    ap.add_argument("--include-closed", action="store_true", help="마감 지난 공고도 수집")
    a = ap.parse_args()

    if a.date_from and a.date_to:
        date_from, date_to = a.date_from, a.date_to
    else:
        date_from, date_to = default_range(a.days)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log(f"수집 기간: {date_from} ~ {date_to}  -> {out.resolve()}")

    rows = fetch_list(date_from, date_to)
    n_all = len(rows)
    if not a.no_seen:
        seen = seen_uids("ntis")
        rows = [r for r in rows if r["uid"] not in seen]
        log(f"이미 판정한 공고 제외: {n_all - len(rows)}건")
    if not a.include_closed:
        before = len(rows)
        rows = [r for r in rows if not dday(r["end"]).startswith("마감")]
        log(f"마감 지난 공고 제외: {before - len(rows)}건")
    if a.limit:
        rows = rows[: a.limit]
    log(f"상세 수집 대상 {len(rows)}건")

    items = []
    for i, r in enumerate(rows, 1):
        log(f"[{i}/{len(rows)}] {r['uid']} {r['title'][:50]}")
        try:
            d = fetch_detail(r["uid"])
        except Exception as e:  # noqa: BLE001
            log(f"  ! 상세 실패: {e}")
            d = {"uid": r["uid"], "title": r["title"], "meta": {}, "attachments": [], "body": "",
                 "ntis_url": f"{VIEW_URL}?roRndUid={r['uid']}&flag=rndList", "source_url": "",
                 "form": "", "agency": "", "announce_date": "", "program": "", "budget": "", "contact": ""}
        d["list_status"] = r["status"]
        d["dept"] = d.get("dept") or r["dept"]
        d["start"] = d.get("start") or r["start"]
        d["end"] = d.get("end") or r["end"]
        d["dday"] = dday(d["end"])
        if not a.no_attach:
            for att in d["attachments"]:
                try:
                    p = download_attachment(att, out / "attachments" / r["uid"])
                    if p:
                        att["path"] = str(p.relative_to(out))
                        att["size"] = p.stat().st_size
                        extract_attachment(att, p)
                        log(f"  + {p.name} ({att['size'] // 1024}KB, {attachment_log(att)})")
                except Exception as e:  # noqa: BLE001
                    log(f"  ! 첨부 실패 {att['name']}: {e}")
                time.sleep(0.3)
        items.append(d)
        time.sleep(0.5)

    write_outputs(items, out, date_from, date_to)
    log(f"완료: {len(items)}건 -> {out / 'announcements.json'}, {out / 'digest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
