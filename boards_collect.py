#!/usr/bin/env python3
"""
게시판형 지원사업 공고 수집기 (사이트별 어댑터).
  bizinfo       : 기업마당 지원사업 공고 — 소관이 충북·충남인 공고, 접수중
                  공공데이터포털 API https://www.data.go.kr/data/15157820/openapi.do (G2B_SERVICE_KEY, 활용신청 필요)
  bizinfo_other : 기업마당 — 충북·충남 외 14개 시·도, 접수중. bizinfo_keywords.txt로 공고명 1차 필터
  bizinfo_central : 기업마당 — 소관이 중앙부처인 공고(KOCCA·NIPA 등 국비 사업, 중기부 제외), 접수중. bizinfo_keywords.txt로 1차 필터
  mss     : 기업마당 — 소관이 중소벤처기업부인 공고 (같은 API)
  riia    : 충북 지역혁신클러스터 사업공고  https://cb.riia.or.kr/board/businessAnnouncement

지정 기간(기본: 지난주 월~일)에 등록된 공고의 상세 본문과 첨부를 내려받아
NTIS 수집기와 같은 형식(announcements.json / digest.md / brief/ / text/)으로 저장한다.
이미 판정한 공고(state/seen.json)와 접수 마감 공고는 자동 제외.
--dedupe 로 준 폴더(다른 소스 산출물)나 seen.json에 제목이 거의 같은 공고가 있으면 중복으로 보고 제외한다
(기업마당은 충북과기혁신원·중기부 공고를 재게시하므로).

사용:
  python boards_collect.py --site mss --out out/mss --dedupe out
  python boards_collect.py --site bizinfo --out out/bizinfo --dedupe out out/cbist out/riia out/mss
  python boards_collect.py --site riia --from 2026-09-21 --to 2026-09-27 --out out/riia
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from cbist_collect import download_attachment
from g2b_collect import load_keywords, match_keywords
from ntis_collect import (
    attachment_log, attachment_sections, clean_ws, dday, default_range, extract_attachment, get, log, make_brief,
    now_kst, strip_tags,
)
from seen_state import load_seen, same_title, title_key

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"}


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


SITES = {s.key: s for s in (Bizinfo, BizinfoOther, BizinfoCentral, Mss, Riia, Nipa)}


# --------------------------------------------------------------------------- 공통
def fetch_list(site, date_from: str, date_to: str, max_pages: int = 30) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
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
        if r["reg_date"] and date_from <= r["reg_date"] <= date_to and getattr(site, "in_scope", lambda _: True)(r):
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


def write_outputs(site, items: list[dict], out: Path, date_from: str, date_to: str) -> None:
    (out / "brief").mkdir(parents=True, exist_ok=True)
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "announcements.json").write_text(
        json.dumps({"range": [date_from, date_to], "source": site.key, "collected_at": now_kst().isoformat(), "items": items},
                   ensure_ascii=False, indent=1), "utf-8")
    lines = [f"# {site.label} 사업공고 수집 결과 ({date_from} ~ {date_to})", "", f"총 {len(items)}건", ""]
    for it in items:
        lines += [f"## [{it['uid']}] {it['title']}",
                  f"- {it['agency']} | 등록 {it['reg_date']} | 접수 {it['period_text'] or '-'} ({it['dday'] or '마감 미정'})",
                  f"- 상세: {it['ntis_url']}",
                  f"- 첨부({len(it['attachments'])}): " + ", ".join(a["name"] for a in it["attachments"]),
                  f"- 요약본: brief/{it['uid']}.md", ""]
    (out / "digest.md").write_text("\n".join(lines), "utf-8")
    for it in items:
        head = [f"# {it['title']}", "", f"- UID: {it['uid']} ({site.label})",
                f"- 소관 {it['dept']} | 수행기관 {it['agency']} | 분야 {it.get('form', '')}",
                f"- 등록 {it['reg_date']} | 접수 {it['period_text'] or '-'}",
                f"- 상세: {it['ntis_url']}"] + ([f"- 신청 사이트: {it['source_url']}"] if it.get("source_url") else []) + [""]
        att_brief, att_text = attachment_sections(it["attachments"])
        parts = head + ["## 본문", "", it["body"] or "(본문 없음)", ""] + att_text
        (out / "text" / f"{it['uid']}.md").write_text("\n".join(parts), "utf-8")
        bparts = head + ["## 본문 (발췌)", "", make_brief(it["body"]) or "(본문 없음)", ""] + att_brief
        (out / "brief" / f"{it['uid']}.md").write_text("\n".join(bparts), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", required=True, choices=sorted(SITES))
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

    site = SITES[a.site]()
    date_from, date_to = (a.date_from, a.date_to) if a.date_from and a.date_to else default_range(a.days)
    out = Path(a.out or f"out/{site.key}")
    out.mkdir(parents=True, exist_ok=True)
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
    if not a.no_seen:
        known = known_titles(a.dedupe)
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
            d = {**r, "source": site.key, "budget": "", "announce_date": r["reg_date"], "meta": {}, **site.detail(r)}
        except Exception as e:  # noqa: BLE001
            log(f"  ! 상세 실패: {e}")
            continue
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
        items.append(d)
        time.sleep(0.5)
    write_outputs(site, items, out, date_from, date_to)
    log(f"완료: {len(items)}건 -> {out / 'digest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
