#!/usr/bin/env python3
"""
게시판형 지원사업 공고 수집기 (사이트별 어댑터).
  bizinfo       : 기업마당 지원사업 공고 — 소관이 충북·충남인 공고, 접수중
                  공공데이터포털 API https://www.data.go.kr/data/15157820/openapi.do (G2B_SERVICE_KEY, 활용신청 필요)
  bizinfo_other : 기업마당 — 충북·충남 외 14개 시·도, 접수중. bizinfo_keywords.txt로 공고명 1차 필터
  bizinfo_central : 기업마당 — 소관이 중앙부처인 공고(KOCCA·NIPA 등 국비 사업, 중기부 제외), 접수중. bizinfo_keywords.txt로 1차 필터
  mss     : 기업마당 — 소관이 중소벤처기업부인 공고 (같은 API)
  mss_site: 중소벤처기업부 홈페이지 사업공고  https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=310
  msit    : 과학기술정보통신부 사업공고  https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=121&mId=311
  nipa    : 정보통신산업진흥원 사업공고  https://www.nipa.kr/home/2-2
  cbist   : 충북과학기술혁신원 사업공고  https://www.cbist.or.kr/home/sub.do?mncd=1131
  riia    : 충북 지역혁신클러스터 사업공고  https://cb.riia.or.kr/board/businessAnnouncement
  cbtp    : 충북테크노파크 사업공고  https://www.cbtp.or.kr (board_id=saup_notice)
  ctp     : 충남테크노파크 사업공고·입찰공고  https://www.ctp.or.kr
  cistep  : 천안과학산업진흥원 공지사항·입찰공고  https://www.cistep.re.kr
  cfac    : 천안문화재단 모집공고·입찰공고  https://www.cfac.or.kr
  ccon    : 충남콘텐츠진흥원(옛 충남정보문화산업진흥원) 사업공고·입찰공고  https://ccon.kr

묶음(--group): 여러 사이트를 한 폴더에 모아 Discord에 이름 하나로 나가게 한다.
  chungbuk : cbist + riia + cbtp      chungnam : ctp + cistep + cfac + ccon      mss : mss_site + mss(API)
  묶음 안에서 한 사이트가 실패해도 나머지는 수집하고, 실패한 사이트는 announcements.json의 failed에 적는다.

지정 기간(기본: 지난주 월~일)에 등록된 공고의 상세 본문과 첨부를 내려받아
NTIS 수집기와 같은 형식(announcements.json / digest.md / brief/ / text/)으로 저장한다.
이미 판정한 공고(state/seen.json)와 접수 마감 공고는 자동 제외.
--dedupe 로 준 폴더(다른 소스 산출물)나 seen.json에 제목이 거의 같은 공고가 있으면 중복으로 보고 제외한다
(기업마당은 충북과기혁신원·중기부 공고를 재게시하므로).

사용:
  python boards_collect.py --group chungbuk --out out/chungbuk --dedupe out out/g2b
  python boards_collect.py --site mss --out out/mss --dedupe out out/chungbuk out/chungnam
  python boards_collect.py --site riia --from 2026-09-21 --to 2026-09-27 --out out/riia
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from pathlib import Path

from requests.adapters import HTTPAdapter

import cbist_collect
from cbist_collect import download_attachment
from g2b_collect import load_keywords, match_keywords
from ntis_collect import (
    attachment_log, attachment_sections, clean_ws, dday, default_range, extract_attachment, get, is_form_attachment, log,
    make_brief, now_kst, session, strip_tags,
)
from seen_state import load_seen, same_title, title_key

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
IMAGE_ONLY = "(본문이 이미지로만 게시됨 — 첨부 공고문 참조)"
# 선정·평가·입찰 결과 공고는 신청할 수 있는 공고가 아니므로 판정에 넘기지 않는다 (새로 추가한 기관 게시판에만 적용)
RESULT_TITLE = re.compile(r"^\s*\[결과\]|결과\s*(공고|안내|발표|알림)|(선정|평가|심의|심사|입찰|개찰)\s*결과|합격자\s*(발표|공고)|수상(작|자)\s*(공고|발표)")


class LegacyTLS(HTTPAdapter):
    """충북TP 서버는 TLS 설정이 오래돼 기본 보안 수준에서는 연결이 거부된다 (www는 DH_KEY_TOO_SMALL, 센터 서브도메인은
    WRONG_SIGNATURE_TYPE) — cbtp.or.kr 호스트에만 수준을 낮춘다. 인증서 검증은 그대로 한다."""

    def init_poolmanager(self, *args, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kw)


session.mount("https://www.cbtp.or.kr", LegacyTLS())


def body_text(fragment: str) -> str:
    t = strip_tags(fragment or "")
    return t if t or "<img" not in (fragment or "") else IMAGE_ONLY


def body_images(fragment: str, base: str) -> list[str]:
    """본문 조각 안의 이미지 주소(절대 경로, 최대 6장). 공고문이 이미지로만 올라온 글에서 판정용으로 내려받는다."""
    srcs = re.findall(r"""<img[^>]+src=["']([^"']+)""", fragment or "")
    return [urllib.parse.urljoin(base + "/", html.unescape(u)) for u in srcs][:6]


def save_images(urls: list[str], dest: Path) -> list[str]:
    """이미지를 dest에 01.jpg, 02.png … 로 저장하고 경로 목록을 돌려준다 (판정 때 Read 도구로 연다)."""
    saved = []
    for n, url in enumerate(urls, 1):
        try:
            r = session.get(url, headers=UA_HEADERS, timeout=60)
            r.raise_for_status()
            # Content-Type이 image/jpg처럼 제각각이라 파일 머리로 형식을 본다
            ext = next((e for sig, e in ((b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"))
                        if r.content.startswith(sig)), ".webp" if r.content[8:12] == b"WEBP" else "")
            if not ext or len(r.content) > 8 * 1024 * 1024:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            path = dest / f"{n:02d}{ext}"
            path.write_bytes(r.content)
            saved.append(path.as_posix())
        except Exception as e:  # noqa: BLE001
            log(f"  ! 본문 이미지 실패 {url[:80]}: {e}")
    return saved


def cut(seg: str, stops: tuple[str, ...]) -> str:
    """seg에서 가장 먼저 나오는 stop 앞까지."""
    idx = [i for i in (seg.find(s) for s in stops) if i >= 0]
    return seg[: min(idx)] if idx else seg


def dash_date(s: str) -> str:
    """'2026.09.28' / '2026-09-28 17:30' → '2026-09-28'."""
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s or "")
    return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else ""


def period(s: str) -> tuple[str, str]:
    """'2026-09-21 ~ 2026-10-08' → (시작, 마감). 날짜가 하나뿐이면 마감 미정."""
    ds = re.findall(r"\d{4}[.\-]\d{1,2}[.\-]\d{1,2}", s or "")
    return (dash_date(ds[0]) if ds else "", dash_date(ds[1]) if len(ds) > 1 else "")


def abs_url(base: str, href: str) -> str:
    href = html.unescape(href)
    return href if href.startswith("http") else base + "/" + href.lstrip("/")


# --------------------------------------------------------------------------- 기업마당 (공공데이터포털 API)
class Bizinfo:
    """공공데이터포털 "중소벤처기업부_중소기업 지원사업 공고 조회 서비스"(G2B_SERVICE_KEY와 같은 키, 활용신청 필요)로
    기업마당 접수중 공고를 등록일 내림차순으로 받는다. www.bizinfo.go.kr·www.mss.go.kr은 2026-10-20부터 클라우드 routine에서
    연결이 끊겨(Connection reset) 목록·본문을 API로 바꿨다. 첨부는 여전히 www.bizinfo.go.kr에 있어 클라우드에서는 못 받을 수 있다.
    소관기관명(jrsdInsttNm)이 지자체 소관이면 광역 시·도 이름, 국비 사업이면 부처 이름이라 이것으로 소스를 나눈다."""
    key, label = "bizinfo", "기업마당(충북·충남)"
    KEYWORDS = ""   # 키워드 1차 필터 파일 (없으면 전부 판정)
    API = "https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService"
    ROWS = 300
    AREAS = {"충청북도", "충청남도"}
    LOCAL = re.compile(r"(특별시|광역시|특별자치시|도)$")   # 광역 시·도 (전남광주통합특별시·강원특별자치도 포함)

    def __init__(self) -> None:
        key = os.environ.get("G2B_SERVICE_KEY", "")
        # "Encoding" 키(%2B, %3D 포함)를 넣어도 requests가 다시 인코딩하지 않도록 디코딩해 둔다
        self.service_key = urllib.parse.unquote(key) if "%" in key else key
        self.items: dict[str, dict] = {}

    def in_scope(self, row: dict) -> bool:
        return row["dept"] in self.AREAS

    def list_page(self, page: int) -> list[dict]:
        if not self.service_key:
            raise RuntimeError("G2B_SERVICE_KEY 환경변수(공공데이터포털 인증키)가 필요합니다.")
        body = get(self.API, params={"serviceKey": self.service_key, "dataType": "json", "pageNo": page,
                                     "numOfRows": self.ROWS}).json()["response"]["body"]
        rows = []
        for it in (body.get("items") or {}).get("item") or []:
            self.items[it["pblancId"]] = it
            start, end = period(it.get("reqstBeginEndDe", ""))
            rows.append({"uid": it["pblancId"], "title": clean_ws(it["pblancNm"]), "reg_date": dash_date(it["creatPnttm"]),
                         "start": start, "end": end, "status": "", "period_text": it.get("reqstBeginEndDe", ""),
                         "dept": it.get("jrsdInsttNm", ""), "agency": it.get("excInsttNm", ""),
                         "form": it.get("pldirSportRealmLclasCodeNm", "")})
        return rows

    def detail(self, row: dict) -> dict:
        it = self.items[row["uid"]]
        fields = [("사업개요", strip_tags(it.get("bsnsSumryCn") or "")), ("지원대상", it.get("trgetNm")),
                  ("사업신청 방법", it.get("reqstMthPapersCn")), ("문의처", it.get("refrncNm"))]
        body = "\n\n".join(f"[{k}]\n{v}" for k, v in fields if v)
        atts, stems = [], set()
        # 첨부(@로 이어 붙임) 다음에 본문출력파일(공고문 PDF) — 같은 공고문이 hwpx·pdf 두 벌이면 하나만 받는다
        files = list(zip((it.get("fileNm") or "").split("@"), (it.get("flpthNm") or "").split("@")))
        files.append((it.get("printFileNm") or "", it.get("printFlpthNm") or ""))
        for name, url in files:
            name, stem = clean_ws(name), re.sub(r"\.[A-Za-z0-9]+$", "", clean_ws(name))
            if not name or not url.startswith("http") or stem in stems:
                continue
            stems.add(stem)
            atts.append({"name": name, "url": url})
        return {"ntis_url": it.get("pblancUrl", ""), "source_url": it.get("rceptEngnHmpgUrl", ""), "body": body,
                "attachments": atts, "contact": clean_ws(it.get("refrncNm", ""))[:200]}


class BizinfoOther(Bizinfo):
    """충북·충남을 뺀 나머지 14개 시·도. 주당 70~150건으로 많다."""
    key, label = "bizinfo_other", "기업마당(타지역)"
    KEYWORDS = "bizinfo_keywords.txt"   # 대부분 해당 지역 소상공인 전용이라 역량 키워드로 1차 필터

    def in_scope(self, row: dict) -> bool:
        return bool(self.LOCAL.search(row["dept"])) and row["dept"] not in self.AREAS


class BizinfoCentral(Bizinfo):
    """소관이 중앙부처인 공고 (KOCCA·NIPA·산업부·과기정통부 등의 국비 사업, [충북] 말머리가 붙은 국비 공고 포함).
    중기부 소관은 Mss가 받고 여기서는 --dedupe out/mss로 빠진다."""
    key, label = "bizinfo_central", "기업마당(중앙부처)"
    KEYWORDS = "bizinfo_keywords.txt"

    def in_scope(self, row: dict) -> bool:
        return not self.LOCAL.search(row["dept"])


# --------------------------------------------------------------------------- 중소벤처기업부
class Mss(Bizinfo):
    """기업마당에서 소관이 중소벤처기업부인 공고 (창업진흥원·소진공·중진공·지방중기청·TP 수행분 포함).
    2026-10-19까지는 www.mss.go.kr 사업공고 게시판(cbIdx=310, uid는 게시글 번호)에서 받았다."""
    key, label = "mss", "중소벤처기업부"

    def in_scope(self, row: dict) -> bool:
        return row["dept"] == "중소벤처기업부"


class MssSite:
    """중소벤처기업부 홈페이지 사업공고 게시판(cbIdx=310). 본부가 직접 내는 공고 가운데 기업마당 API에 없는 것이 있어
    (2026-10-01~11-10 등록 21건 중 7건) API와 함께 받는다. 2026-10-20부터 클라우드에서는 연결이 끊겼던 사이트라
    묶음(--group mss)으로만 돌려, 실패해도 API 쪽은 그대로 수집되게 한다."""
    key, label = "mss_site", "중기부 홈페이지"
    KEYWORDS = ""
    SKIP_TITLE = RESULT_TITLE
    BASE = "https://www.mss.go.kr"
    LIST = f"{BASE}/site/smba/ex/bbs/List.do"
    VIEW = f"{BASE}/site/smba/ex/bbs/View.do"

    def list_page(self, page: int) -> list[dict]:
        h = get(self.LIST, params={"cbIdx": 310, "pageIndex": page}, headers=UA_HEADERS).text
        rows = []
        for m in re.finditer(r"<tr[^>]*onclick=\"doBbsFView\('310','(\d+)'[^>]*title=\"([^\"]*)\"(.*?)</tr>", h, re.S):
            tr = m[3]
            info = dict((clean_ws(k), clean_ws(strip_tags(v))) for k, v in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", tr, re.S))
            reg = re.search(r"<td>\s*(\d{4}\.\d{2}\.\d{2})\s*</td>", tr)
            start, end = period(info.get("신청기간", ""))
            rows.append({"uid": m[1], "title": clean_ws(m[2]), "reg_date": dash_date(reg[1]) if reg else "",
                         "start": start, "end": end, "status": "", "period_text": info.get("신청기간", ""),
                         "dept": "중소벤처기업부", "agency": "중소벤처기업부 " + info.get("담당부서", ""),
                         "form": "사업공고", "program": info.get("공고번호", "")})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.VIEW}?cbIdx=310&bcIdx={row['uid']}&parentSeq={row['uid']}"
        h = get(url, headers=UA_HEADERS).text
        ta = re.search(r'<textarea id="editContents"[^>]*>(.*?)</textarea>', h, re.S)
        body = strip_tags(html.unescape(ta[1])) if ta else ""
        atts, stems = [], set()
        fl = re.search(r'class="file_list">(.*?)</td>', h, re.S)
        for name, href in re.findall(r'<span class="name">(.*?)<em>.*?href="(/common/board/Download\.do\?[^"]+)"', fl[1] if fl else "", re.S):
            nm = clean_ws(strip_tags(name))
            stem = re.sub(r"\.[A-Za-z0-9]+$", "", nm)
            if stem in stems:   # 같은 공고문을 hwpx·pdf 두 벌로 올리는 경우가 많아 하나만 받는다
                continue
            stems.add(stem)
            atts.append({"name": nm, "url": abs_url(self.BASE, href)})
        return {"ntis_url": url, "source_url": "", "body": body, "attachments": atts, "contact": ""}


# --------------------------------------------------------------------------- 충북 지역혁신클러스터
class Riia:
    key, label = "riia", "충북 지역혁신클러스터"
    KEYWORDS = ""
    BASE = "https://cb.riia.or.kr"
    LIST = f"{BASE}/board/businessAnnouncement"

    def list_page(self, page: int) -> list[dict]:
        h = get(self.LIST, params={"page": page}, headers=UA_HEADERS).text
        rows = []
        for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            m = re.search(r'href="/board/businessAnnouncement/view/([0-9a-f-]+)"', tr)
            if not m:
                continue
            title = re.search(r'<span class="mr_5">(.*?)</span>', tr, re.S)
            per = re.search(r"(\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2})", tr)
            reg = re.findall(r"(\d{4}\.\d{2}\.\d{2})", tr)
            state = re.search(r'class="state[^"]*">\s*([^<]+)', tr)
            start, end = period(per[1] if per else "")
            rows.append({"uid": m[1], "title": clean_ws(strip_tags(title[1])) if title else "",
                         "reg_date": dash_date(reg[-1]) if reg else "", "start": start, "end": end,
                         "status": "종료" if state and "마감" in state[1] else clean_ws(state[1]) if state else "",
                         "period_text": per[1] if per else "", "dept": "충청북도", "agency": "충북 지역혁신클러스터",
                         "form": "사업공고"})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.LIST}/view/{row['uid']}"
        h = get(url, headers=UA_HEADERS).text
        atts = [{"name": clean_ws(strip_tags(n)), "url": abs_url(self.BASE, href)}
                for href, n in re.findall(r'<a class="file_download" href="([^"]+)"[^>]*>(.*?)</a>', h, re.S)]
        box = re.search(r'class="irpe_list_more_conbox[^"]*">(.*?)<div id="video_layer"', h, re.S)
        body = strip_tags(box[1]) if box else ""
        if box and not body and "<img" in box[1]:
            body = "(본문이 이미지로만 게시됨 — 첨부 공고문 참조)"
        return {"ntis_url": url, "source_url": "", "body": body, "attachments": atts, "contact": ""}


# --------------------------------------------------------------------------- 정보통신산업진흥원
class Nipa:
    """NIPA 사업공고. XR 센터 입주·쇼룸·가상융합 허브처럼 기업마당에 재게시되지 않는 XR 공고가 있다 (2026-10-13 점검)."""
    key, label = "nipa", "정보통신산업진흥원"
    KEYWORDS = ""   # 주 1~3건이라 전부 판정
    BASE = "https://www.nipa.kr"
    LIST = f"{BASE}/home/2-2"

    def list_page(self, page: int) -> list[dict]:
        h = get(self.LIST, params={"curPage": page}, headers=UA_HEADERS).text
        rows = []
        for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            m = re.search(r'<a href="/home/2-2/(\d+)">(.*?)</a>', tr, re.S)
            if not m:
                continue
            biz = re.search(r'<span class="box bluebox">(.*?)</span>', tr, re.S)
            per = re.search(r"신청기간\s*:\s*([\d\-: ~]+)", tr)
            dates = re.findall(r'<span class="bco">\s*(\d{4}-\d{2}-\d{2})\s*</span>', tr)
            start, end = period(per[1] if per else "")
            rows.append({"uid": m[1], "title": clean_ws(strip_tags(re.sub(r"<!--.*?-->", "", m[2], flags=re.S))),
                         "reg_date": dates[-1] if dates else "", "start": start, "end": end,
                         "status": "종료" if re.search(r"<b>\s*종료\s*</b>", tr) else "",
                         "period_text": clean_ws(per[1]) if per else "", "dept": "과학기술정보통신부", "agency": "정보통신산업진흥원",
                         "form": "사업공고", "program": clean_ws(strip_tags(biz[1])) if biz else ""})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.LIST}/{row['uid']}"
        h = get(url, headers=UA_HEADERS).text
        box = re.search(r">\s*내용\s*</td>\s*<td[^>]*>(.*?)</td>\s*</tr>", h, re.S)
        body = strip_tags(box[1]) if box else ""
        atts = [{"name": re.sub(r"\s*\(파일크기.*$", "", clean_ws(strip_tags(re.sub(r"<!--.*?-->", "", n, flags=re.S)))),
                 "url": abs_url(self.BASE, href)}
                for href, n in re.findall(r'<a href="(/comm/getFile\?[^"]+)">(.*?)</a>', h, re.S)]
        return {"ntis_url": url, "source_url": "", "body": body, "attachments": atts, "contact": ""}


# --------------------------------------------------------------------------- 충북과학기술혁신원
class Cbist:
    """cbist_collect.py의 목록·상세 파서를 그대로 쓴다 (충북 묶음에 넣기 위한 어댑터)."""
    key, label = "cbist", "충북과학기술혁신원"
    KEYWORDS = ""

    def list_page(self, page: int) -> list[dict]:
        rows = cbist_collect.parse_rows(get(cbist_collect.LIST_URL, params={"mncd": cbist_collect.MNCD, "page": page}).text)
        for r in rows:
            r.update(period_text=f"{r['start']} ~ {r['end']}" if r["start"] else "", dept="충청북도", agency=self.label,
                     form="지원사업 공고")
        return rows

    def detail(self, row: dict) -> dict:
        d = cbist_collect.fetch_detail(row)
        return {k: d[k] for k in ("ntis_url", "source_url", "body", "attachments", "contact")}


# --------------------------------------------------------------------------- 충북테크노파크
class Cbtp:
    """사업공고 게시판. 목록에 등록일이 없고(접수기간·진행 상태만) 공지 글이 매 페이지 위에 고정된다."""
    key, label = "cbtp", "충북테크노파크"
    KEYWORDS = ""
    SKIP_TITLE = RESULT_TITLE
    NO_LIST_DATE = True
    PAGES = 1
    BASE = "https://www.cbtp.or.kr"
    BOARD = {"control": "bbs", "board_id": "saup_notice", "lm_uid": "387"}

    def _html(self, params: dict) -> str:
        r = get(f"{self.BASE}/index.php", params=params, headers=UA_HEADERS)
        r.encoding = "cp949"
        return r.text

    def list_page(self, page: int) -> list[dict]:
        h = self._html({**self.BOARD, "page": page})
        rows = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h[h.find('class="bbs_default_list"'):], re.S):
            m = re.search(r'<a href="[^"]*mode=view&amp;no=([A-Za-z0-9_]+)[^"]*">(.*?)</a>', tr, re.S)
            if not m:
                continue
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            start, end = period(strip_tags(tds[-1])) if tds else ("", "")
            rows.append({"uid": f"cbtp-{m[1]}", "title": clean_ws(strip_tags(m[2])), "reg_date": "", "start": start, "end": end,
                         "status": "종료" if "alt='완료'" in tr else "", "period_text": f"{start} ~ {end}" if start else "",
                         "dept": "충청북도", "agency": self.label, "form": "사업공고",
                         "program": clean_ws(strip_tags(tds[4])) if len(tds) > 5 else ""})
        return rows

    def detail(self, row: dict) -> dict:
        no = row["uid"].split("-", 1)[1]
        h = self._html({**self.BOARD, "mode": "view", "no": no})
        view = h[h.find('class="bbs_view"'):]
        reg = re.search(r"등록일</span></th>\s*<td>(\d{4}-\d{2}-\d{2})", view)   # 주석 처리돼 있지만 HTML에는 남아 있다
        box = re.search(r'<td class="substance"[^>]*>(.*?)</td>\s*</tr>\s*</tbody>', view, re.S)
        frag = re.sub(r'<!--.*?-->|<div class="btn_saup_go">.*?</div>', "", box[1], flags=re.S) if box else ""
        # 본문은 대개 이미지 한 장이고, 같은 내용이 화면낭독기용 숨은 글(div.blind)로 들어 있다. 제목이나 "없음"뿐이면 버린다
        blind = re.search(r'<div class="blind">(.*?)</div>', frag, re.S)
        hidden = strip_tags(blind[1]) if blind else ""
        if blind and (len(hidden) < 40 or clean_ws(hidden) == row["title"]):
            frag = frag.replace(blind[0], "")
        atts = []
        lst = re.search(r'<ul class="attach[^"]*">(.*?)</ul>', view, re.S)
        for href, n in re.findall(r'<a href="([^"]+)">(.*?)</a>', lst[1] if lst else "", re.S):
            href = html.unescape(href)
            if href.startswith("http"):   # 센터 게시판(no=contact_…) 글은 첨부가 센터 서브도메인(semicon.cbtp.or.kr 등)에 있다
                host = urllib.parse.urlsplit(href).netloc
                if host.endswith(".cbtp.or.kr"):
                    session.mount(f"https://{host}", LegacyTLS())
            else:                         # 본 게시판 첨부 주소의 file 값은 EUC-KR로 인코딩해야 서버가 찾는다
                pre, _, fname = href.partition("&file=")
                href = f"{self.BASE}{pre}&file={urllib.parse.quote(fname, encoding='cp949')}"
            atts.append({"name": re.sub(r"^\d+_", "", clean_ws(strip_tags(n))), "url": href})
        url = f"{self.BASE}/index.php?control=bbs&board_id=saup_notice&mode=view&no={no}&lm_uid=387"
        return {"ntis_url": url, "source_url": "", "body": body_text(frag), "attachments": atts, "contact": "",
                "body_images": body_images(frag, self.BASE), "reg_date": reg[1] if reg else row["start"]}


# --------------------------------------------------------------------------- 충남테크노파크
class Ctp:
    key, label = "ctp", "충남테크노파크"
    KEYWORDS = ""
    SKIP_TITLE = RESULT_TITLE
    BASE = "https://www.ctp.or.kr"
    BOARDS = (("business/data.do", "사업공고"), ("community/bid.do", "입찰공고"))

    def list_page(self, page: int) -> list[dict]:
        rows = []
        for path, form in self.BOARDS:
            h = get(f"{self.BASE}/{path}", params={"pn": page}, headers=UA_HEADERS).text
            for tr in h[h.find("<tbody"):].split("<tr>")[1:]:
                m = re.search(r"<a\s+href='(\w+detail\.do)\?seq=(\d+)'>(.*?)</a>", tr, re.S)
                if not m:
                    continue
                dl = re.search(r"<span class='cod1[^']*'>(.*?)</span>", tr, re.S)   # 접수 마감 일시 (입찰공고는 비어 있다)
                reg = re.findall(r"<td class='d-none d-lg-table-cell'>(\d{4}-\d{2}-\d{2})</td>", tr)
                end = dash_date(dl[1]) if dl else ""
                rows.append({"uid": f"ctp-{m[2]}", "title": clean_ws(strip_tags(m[3])), "reg_date": reg[-1] if reg else "",
                             "start": "", "end": end, "status": "종료" if "list-end" in tr else "",
                             "period_text": f"~ {clean_ws(dl[1])}" if end else "", "dept": "충청남도", "agency": self.label,
                             "form": form, "board": f"{path.rsplit('/', 1)[0]}/{m[1]}"})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.BASE}/{row['board']}?seq={row['uid'].split('-', 1)[1]}"
        h = get(url, headers=UA_HEADERS).text
        i = h.find('<div class="boardContent">')
        frag = cut(h[i:], ('<div class="boardAttach">', '<div class="text-center">')) if i >= 0 else ""
        atts = [{"name": clean_ws(strip_tags(n)), "url": self.BASE + html.unescape(href)}
                for href, n in re.findall(r"<a class='d-block co66' href='(/boardfiledownload\.do\?seq=\d+)'>(.*?)</a>", h, re.S)]
        return {"ntis_url": url, "source_url": "", "body": body_text(frag), "attachments": atts, "contact": "",
                "body_images": body_images(frag, self.BASE)}


# --------------------------------------------------------------------------- 천안과학산업진흥원
class Cistep:
    """지원사업 모집공고는 공지사항 게시판에 올라온다 ('지원사업 신청' 메뉴는 2025-05 이후 갱신이 없다)."""
    key, label = "cistep", "천안과학산업진흥원"
    KEYWORDS = ""
    SKIP_TITLE = RESULT_TITLE
    BASE = "https://www.cistep.re.kr"
    BOARDS = (("notice", "공지사항"), ("bidding", "입찰공고"))

    def list_page(self, page: int) -> list[dict]:
        rows = []
        for code, form in self.BOARDS:
            h = get(f"{self.BASE}/zboard/list.do", params={"lmCode": code, "pageIndex": page}, headers=UA_HEADERS).text
            for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
                m = re.search(r'<a href="/zboard/read\.do\?[^"]*pd_pkid=(\d+)"[^>]*>(.*?)</a>', tr, re.S)
                reg = re.search(r'class="date">\s*(\d{4}-\d{2}-\d{2})', tr)
                if not m:
                    continue
                rows.append({"uid": f"cistep-{m[1]}", "title": clean_ws(strip_tags(m[2])), "reg_date": reg[1] if reg else "",
                             "start": "", "end": "", "status": "", "period_text": "", "dept": "충청남도", "agency": self.label,
                             "form": form, "board": code})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.BASE}/zboard/read.do?lmCode={row['board']}&pd_pkid={row['uid'].split('-', 1)[1]}"
        h = get(url, headers=UA_HEADERS).text
        box = re.search(r'<td[^>]*class="bbs_detail"[^>]*>(.*?)</td>\s*</tr>\s*</tbody>', h, re.S)
        atts = [{"name": clean_ws(n), "url": self.BASE + html.unescape(href)}
                for href, n in re.findall(r'<a href="(/zboard/printFileDown\.do\?[^"]+)" title="(.*?) 다운로드"', h, re.S)]
        frag = box[1] if box else ""
        return {"ntis_url": url, "source_url": "", "body": body_text(frag), "attachments": atts, "contact": "",
                "body_images": body_images(frag, self.BASE)}


# --------------------------------------------------------------------------- 그누보드 게시판 (천안문화재단·충남콘텐츠진흥원)
class Gnuboard:
    """상세·첨부 구조가 같은 그누보드 사이트 공통. 첨부는 같은 세션으로 글을 먼저 열어야 받을 수 있다(수집 순서가 그렇다)."""
    KEYWORDS = ""
    SKIP_TITLE = RESULT_TITLE
    BASE = ""
    BOARDS: tuple[tuple[str, str], ...] = ()
    DEPT = "충청남도"

    def board_html(self, board: str, page: int) -> str:
        return get(f"{self.BASE}/bbs/board.php", params={"bo_table": board, "page": page}, headers=UA_HEADERS).text

    def row(self, board: str, form: str, wr_id: str, title: str, **kw) -> dict:
        title = re.sub(r"\s*N?\s*새글\s*$", "", clean_ws(strip_tags(title)))   # 새 글 아이콘의 대체 글자
        return {"uid": f"{self.key}-{board}-{wr_id}", "title": title, "reg_date": "", "start": "", "end": "",
                "status": "", "period_text": "", "dept": self.DEPT, "agency": self.label, "form": form, **kw}

    def detail(self, row: dict) -> dict:
        _, board, wr_id = row["uid"].split("-")
        url = f"{self.BASE}/bbs/board.php?bo_table={board}&wr_id={wr_id}"
        h = get(url, headers=UA_HEADERS).text
        title = re.search(r'<span class="bo_v_tit">(.*?)</span>', h, re.S)   # 목록 제목은 길면 …로 잘린다
        reg = re.search(r"작성일</span>.{0,120}?(\d{2,4})-(\d{2})-(\d{2})", h, re.S)
        i = h.find('<div id="bo_v_con">')
        frag = cut(h[i:], ("<!-- } 본문 내용 끝", '<section id="bo_v_file"', 'id="bo_v_share"', 'id="bo_v_act"', "</section>")) if i >= 0 else ""
        atts = [{"name": clean_ws(strip_tags(n)), "url": html.unescape(href)}
                for href, n in re.findall(r'<a href="([^"]*download\.php\?[^"]+)" class="view_file_download"[^>]*>.*?<strong>(.*?)</strong>', h, re.S)]
        d = {"ntis_url": url, "source_url": "", "body": body_text(frag), "attachments": atts, "contact": "",
             "body_images": body_images(frag, self.BASE)}
        if title:
            d["title"] = clean_ws(strip_tags(title[1]))
        if reg:
            d["reg_date"] = f"{int(reg[1]) % 100 + 2000}-{reg[2]}-{reg[3]}"
        return d


class Cfac(Gnuboard):
    """목록에 작성일이 없고 공고기간·마감 표시만 있다."""
    key, label = "cfac", "천안문화재단"
    BASE = "https://www.cfac.or.kr"
    BOARDS = (("biz", "모집공고"), ("contest", "입찰공고"))
    NO_LIST_DATE = True
    PAGES = 1

    def list_page(self, page: int) -> list[dict]:
        rows = []
        for board, form in self.BOARDS:
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", self.board_html(board, page), re.S):
                m = re.search(r'<div class="bo_tit">\s*<a href="[^"]*wr_id=(\d+)[^"]*">(.*?)</a>', tr, re.S)
                if not m:
                    continue
                per = re.search(r"\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}", tr)
                step = re.search(r'class="btn_step\d*">([^<]*)', tr)
                start, end = period(per[0] if per else "")
                rows.append(self.row(board, form, m[1], m[2], start=start, end=end, period_text=per[0] if per else "",
                                     status="종료" if step and "마감" in step[1] else ""))
        return rows


class Ccon(Gnuboard):
    """충남콘텐츠진흥원 (옛 충남정보문화산업진흥원). 사업공고 글머리: 진행중 / 결과 / 종료."""
    key, label = "ccon", "충남콘텐츠진흥원"
    BASE = "https://ccon.kr"
    BOARDS = (("bsnt", "사업공고"), ("bnt", "입찰공고"))

    def list_page(self, page: int) -> list[dict]:
        rows = []
        for board, form in self.BOARDS:
            h = self.board_html(board, page)
            for li in h[h.find('id="bo_list"'):].split("<li")[1:]:
                m = re.search(r'<a href="[^"]*wr_id=(\d+)[^"]*" class="bo_subject">(.*?)</a>', li, re.S)
                if not m:
                    continue
                cate = re.search(r'class="bo_cate_link">([^<]+)', li)
                reg = re.search(r'class="bo_date">.*?(\d{4}-\d{2}-\d{2})', li, re.S)
                today = re.search(r'class="bo_date">.*?\d{1,2}:\d{2}', li, re.S)   # 오늘 올라온 글은 시각만 나올 수 있다
                rows.append(self.row(board, form, m[1], m[2], reg_date=reg[1] if reg else now_kst().date().isoformat() if today else "",
                                     status="종료" if cate and cate[1].strip() in ("결과", "종료") else ""))
        return rows


# --------------------------------------------------------------------------- 과학기술정보통신부
class Msit:
    """과기부 사업공고. 목록은 빈 칸을 스크립트가 채우는 구조라 스크립트 줄에서 제목·등록일·부서를 읽는다.
    R&D 신규과제 공모는 NTIS에도 올라오므로 --dedupe out 으로 겹치는 공고를 거른다."""
    key, label = "msit", "과학기술정보통신부"
    KEYWORDS = ""
    SKIP_TITLE = RESULT_TITLE
    BASE = "https://www.msit.go.kr"
    BOARD = {"sCode": "user", "mPid": "121", "mId": "311"}
    MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}

    def _date(self, s: str) -> str:
        """'2026. 11. 10' / '2026-11-10' / 'Nov 10, 2026' (요청 언어에 따라 달라진다) → '2026-11-10'."""
        m = re.match(r"\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})", s or "")
        if m:
            return f"{m[3]}-{self.MONTHS.get(m[1], 0):02d}-{int(m[2]):02d}"
        m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s or "")
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}" if m else ""

    def list_page(self, page: int) -> list[dict]:
        h = get(f"{self.BASE}/bbs/list.do", params={**self.BOARD, "pageIndex": page}, headers=UA_HEADERS).text
        ids = re.findall(r'onclick="fn_detail\((\d+)\);"', h)
        js = re.sub(r"/\*.*?\*/", "", h[h.find("board_paging"):], flags=re.S)
        titles = dict(re.findall(r'fn_detail\("(\d+)"\)"[^>]*><span>(.*?)</span>', js, re.S))   # 주석 줄에 제목이 있다
        live = "\n".join(ln for ln in js.splitlines() if not ln.strip().startswith("//"))

        def cell(name: str, i: int) -> str:
            vals = re.findall(rf"'{name}'\+'_{i}'\)\.html\('([^']*)'\)", live)
            return clean_ws(vals[-1]) if vals else ""

        rows = []
        for i, uid in enumerate(ids):
            if uid not in titles:
                continue
            rows.append({"uid": uid, "title": clean_ws(strip_tags(titles[uid])), "reg_date": self._date(cell("REG_DT", i)),
                         "start": "", "end": "", "status": "", "period_text": "", "dept": self.label,
                         "agency": f"{self.label} {cell('CHRG_DEPT_NM', i)}".strip(), "form": "사업공고"})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.BASE}/bbs/view.do?sCode=user&mPid=121&mId=311&bbsSeqNo=100&nttSeqNo={row['uid']}"
        h = get(url, headers=UA_HEADERS).text
        i = h.find('id="cont-wrap"')
        frag = cut(h[h.find(">", i) + 1:], ('<div class="view_file">', '<div class="board_btn', "<!-- END view_cont")) if i >= 0 else ""
        atts, stems = [], set()
        # 같은 문서를 hwp·odt·hwpx·pdf로 올려 둔다 — 문서마다 하나만 (odt는 기계판독용이라 제외)
        for no, order, name in re.findall(r"getExtension_path\('(\d+)', '(\d+)'\);\" title=\"새창 열림\">([^<]+)</a>", h):
            name = clean_ws(name)
            stem = re.sub(r"\.[A-Za-z0-9]+$", "", name)
            if name.lower().endswith(".odt") or stem in stems:
                continue
            stems.add(stem)
            atts.append({"name": name, "url": f"{self.BASE}/ssm/file/fileDown.do?atchFileNo={no}&fileOrd={order}&fileBtn=A"})
        return {"ntis_url": url, "source_url": "", "body": body_text(frag), "attachments": atts, "contact": "",
                "body_images": body_images(frag, self.BASE)}


SITES = {s.key: s for s in (Bizinfo, BizinfoOther, BizinfoCentral, Mss, MssSite, Riia, Nipa, Cbist, Cbtp, Ctp, Cistep, Cfac, Ccon, Msit)}
# 묶음: Discord에 이름 하나로 나간다. 판정 기록(seen.json)은 사이트별로 남는다.
# mss는 홈페이지를 먼저 받고(첨부를 받을 수 있다) 기업마당 API에서 같은 제목은 중복으로 거른다
GROUPS = {"chungbuk": ("충북", (Cbist, Riia, Cbtp)), "chungnam": ("충남", (Ctp, Cistep, Cfac, Ccon)),
          "mss": ("중기부", (MssSite, Mss))}


# --------------------------------------------------------------------------- 공통
def fetch_list(site, date_from: str, date_to: str, max_pages: int = 30) -> list[dict]:
    out: list[dict] = []
    no_date = getattr(site, "NO_LIST_DATE", False)
    for page in range(1, min(max_pages, getattr(site, "PAGES", max_pages)) + 1):
        rows = site.list_page(page)
        log(f"목록 {page}페이지: {len(rows)}건")
        if not rows:
            break
        out.extend(rows)
        # 상단 고정(공지) 글은 날짜가 오래돼도 매 페이지에 나올 수 있으므로 가장 최근 등록일 기준으로 판단
        regs = [r["reg_date"] for r in rows if r["reg_date"]]
        if regs and max(regs) < date_from:
            break
        time.sleep(0.5)
    seen, uniq = set(), []
    for r in out:
        if r["uid"] in seen:
            continue
        seen.add(r["uid"])
        # 기업마당 계열은 API 한 목록을 소관기관으로 나눠 쓴다
        if not getattr(site, "in_scope", lambda _: True)(r):
            continue
        # 목록에 등록일이 없는 사이트(NO_LIST_DATE)는 기간 대신 "접수 중이고 아직 판정하지 않은 공고"를 모두 받는다
        # (등록일은 상세에서 채운다). 처음 추가한 날에만 접수 중인 공고가 한꺼번에 들어온다.
        if (r["status"] != "종료") if no_date else (r["reg_date"] and date_from <= r["reg_date"] <= date_to):
            uniq.append(r)
    return uniq


def known_titles(dirs: list[str]) -> list[tuple[tuple[frozenset, str], str]]:
    """(제목 키, 출처) — --dedupe 폴더의 announcements.json + seen.json 전체."""
    out = []
    for d in dirs:
        p = Path(d) / "announcements.json"
        if p.exists():
            ann = json.loads(p.read_text("utf-8"))
            out += [(title_key(it.get("title", "")), f"{ann.get('source', d)} {it['uid']}") for it in ann.get("items", [])]
    for src, bucket in load_seen().items():
        out += [(title_key(v.get("title", "")), f"{src} {uid}") for uid, v in bucket.items()]
    return [(k, s) for k, s in out if len(k[1]) >= 8]


def duplicate_of(title: str, known: list[tuple[tuple[frozenset, str], str]]) -> str:
    key = title_key(title)
    return next((src for k, src in known if same_title(key, k)), "")


def write_outputs(key: str, label: str, items: list[dict], out: Path, date_from: str, date_to: str,
                  failed: list[str] | None = None) -> None:
    (out / "brief").mkdir(parents=True, exist_ok=True)
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "announcements.json").write_text(
        json.dumps({"range": [date_from, date_to], "source": key, "collected_at": now_kst().isoformat(),
                    "failed": failed or [], "items": items}, ensure_ascii=False, indent=1), "utf-8")
    lines = [f"# {label} 사업공고 수집 결과 ({date_from} ~ {date_to})", "", f"총 {len(items)}건", ""]
    if failed:
        lines += [f"⚠ 수집 실패: {', '.join(failed)}", ""]
    for it in items:
        lines += [f"## [{it['uid']}] {it['title']}",
                  f"- {it['agency']} | 등록 {it['reg_date']} | 접수 {it['period_text'] or '-'} ({it['dday'] or '마감 미정'})",
                  f"- 상세: {it['ntis_url']}",
                  f"- 첨부({len(it['attachments'])}): " + ", ".join(a["name"] for a in it["attachments"]),
                  f"- 요약본: brief/{it['uid']}.md", ""]
    (out / "digest.md").write_text("\n".join(lines), "utf-8")
    for it in items:
        head = [f"# {it['title']}", "", f"- UID: {it['uid']} ({it.get('site_label') or label})",
                f"- 소관 {it['dept']} | 수행기관 {it['agency']} | 분야 {it.get('form', '')}",
                f"- 등록 {it['reg_date']} | 접수 {it['period_text'] or '-'}",
                f"- 상세: {it['ntis_url']}"] + ([f"- 신청 사이트: {it['source_url']}"] if it.get("source_url") else []) + [""]
        att_brief, att_text = attachment_sections(it["attachments"])
        imgs = (["## 본문 이미지 (공고문이 이미지로만 올라와 있음 — Read 도구로 아래 파일을 열어 내용을 확인)", ""]
                + [f"- {p}" for p in it["images"]] + [""]) if it.get("images") else []
        parts = head + ["## 본문", "", it["body"] or "(본문 없음)", ""] + imgs + att_text
        (out / "text" / f"{it['uid']}.md").write_text("\n".join(parts), "utf-8")
        bparts = head + ["## 본문 (발췌)", "", make_brief(it["body"]) or "(본문 없음)", ""] + imgs + att_brief
        (out / "brief" / f"{it['uid']}.md").write_text("\n".join(bparts), "utf-8")


def collect(site, a, out: Path, date_from: str, date_to: str, known: list) -> list[dict]:
    """한 사이트의 목록 → 제외(이미 판정·마감·결과 공고·중복·키워드) → 상세·첨부. 묶음에서는 사이트마다 한 번씩 부른다."""
    log(f"[{site.label}] 수집 기간: {date_from} ~ {date_to} -> {out.resolve()}")
    rows = fetch_list(site, date_from, date_to)
    log(f"기간 내 등록 {len(rows)}건")
    if not a.no_seen:
        seen = set((load_seen().get(site.key) or {}).keys())
        before = len(rows)
        rows = [r for r in rows if r["uid"] not in seen]
        log(f"이미 판정한 공고 제외: {before - len(rows)}건")
    if not a.include_closed:
        before = len(rows)
        rows = [r for r in rows if r["status"] != "종료" and not dday(r["end"]).startswith("마감")]
        log(f"마감/종료 공고 제외: {before - len(rows)}건")
    skip = getattr(site, "SKIP_TITLE", None)
    if skip:
        before = len(rows)
        rows = [r for r in rows if not skip.search(r["title"])]
        log(f"결과 공고 제외: {before - len(rows)}건")
    if not a.no_seen:
        kept = []
        for r in rows:
            dup = duplicate_of(r["title"], known)
            if dup:
                log(f"  중복 제외: {r['title'][:50]} (= {dup})")
            else:
                kept.append(r)
        rows = kept
    if site.KEYWORDS and not a.no_filter:
        kw = load_keywords(Path(__file__).resolve().parent / site.KEYWORDS)
        kept = []
        for r in rows:
            hit, bad, ok = match_keywords(r["title"], kw)
            if ok:
                r["keywords"] = hit
                kept.append(r)
            else:
                log(f"  키워드 제외: {r['title'][:50]}" + (f" (제외어 {', '.join(bad)})" if bad else ""))
        log(f"키워드 필터({site.KEYWORDS}) 제외: {len(rows) - len(kept)}건")
        rows = kept
    log(f"상세 수집 대상 {len(rows)}건")

    items = []
    for i, r in enumerate(rows, 1):
        log(f"[{i}/{len(rows)}] {r['uid']} {r['title'][:50]}")
        try:
            d = {**r, "source": site.key, "site_label": site.label, "budget": "", "meta": {}, **site.detail(r)}
        except Exception as e:  # noqa: BLE001
            log(f"  ! 상세 실패: {e}")
            continue
        d["announce_date"] = d["reg_date"]   # 목록에 등록일이 없는 사이트는 상세에서 채운다
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
                    att["skipped"] = "download_failed"
                    log(f"  ! 첨부 실패 {att['name']}: {e}")
                time.sleep(0.3)
        # 공고문이 이미지로만 올라왔고 읽을 수 있는 첨부(양식 제외)도 없으면 이미지를 받아 둔다 — 판정 때 직접 열어 본다
        imgs = d.pop("body_images", [])
        readable = any((att.get("text") or att.get("parts")) and not is_form_attachment(att["name"]) for att in d["attachments"])
        if d["body"] == IMAGE_ONLY and not readable and not a.no_attach:
            d["images"] = save_images(imgs, out / "images" / r["uid"])
            log(f"  + 본문 이미지 {len(d['images'])}장")
        items.append(d)
        time.sleep(0.5)
    return items


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--site", choices=sorted(SITES))
    which.add_argument("--group", choices=sorted(GROUPS), help="지역 묶음 — 여러 사이트를 한 폴더로")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--days", type=int)
    ap.add_argument("--out")
    ap.add_argument("--dedupe", nargs="*", default=[], help="중복 확인할 다른 소스 산출물 폴더 (announcements.json)")
    ap.add_argument("--no-attach", action="store_true")
    ap.add_argument("--no-seen", action="store_true")
    ap.add_argument("--include-closed", action="store_true")
    ap.add_argument("--no-filter", action="store_true", help="키워드 1차 필터 끄기 (bizinfo_other)")
    a = ap.parse_args()

    key, label, classes = (a.group, *GROUPS[a.group]) if a.group else (a.site, SITES[a.site].label, (SITES[a.site],))
    date_from, date_to = (a.date_from, a.date_to) if a.date_from and a.date_to else default_range(a.days)
    out = Path(a.out or f"out/{key}")
    out.mkdir(parents=True, exist_ok=True)

    known = [] if a.no_seen else known_titles(a.dedupe)
    items, failed = [], []
    for cls in classes:
        try:
            got = collect(cls(), a, out, date_from, date_to, known)
        except Exception as e:  # noqa: BLE001
            if not a.group:   # 단일 사이트는 예전처럼 실패로 끝낸다 (announcements.json이 없으면 요약에 "수집 실패")
                raise
            log(f"! [{cls.label}] 수집 실패 — 묶음의 나머지 사이트는 계속: {e}")
            failed.append(cls.label)
            continue
        items += got
        # 같은 묶음의 뒤 사이트가 같은 공고를 다시 올린 경우(예: 충남TP 공고를 천안과학산업진흥원이 재게시)를 거른다
        known += [(k, f"{cls.key} {it['uid']}") for it in got if len((k := title_key(it["title"]))[1]) >= 8]
    write_outputs(key, label, items, out, date_from, date_to, failed)
    log(f"완료: {len(items)}건 -> {out / 'digest.md'}" + (f" (수집 실패: {', '.join(failed)})" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
