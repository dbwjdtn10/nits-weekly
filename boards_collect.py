#!/usr/bin/env python3
"""
게시판형 지원사업 공고 수집기 (사이트별 어댑터).
  bizinfo       : 기업마당 지원사업 공고 — 충북·충남 지역, 접수중
                  https://www.bizinfo.go.kr/sii/siia/selectSIIA200View.do
  bizinfo_other : 기업마당 — 충북·충남 외 14개 시·도, 접수중. bizinfo_keywords.txt로 공고명 1차 필터
  mss     : 중소벤처기업부 사업공고  https://www.mss.go.kr/site/smba/ex/bbs/List.do?cbIdx=310
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
import re
import sys
import time
from pathlib import Path

from cbist_collect import download_attachment
from g2b_collect import load_keywords, match_keywords
from ntis_collect import (
    attachment_log, attachment_sections, clean_ws, dday, default_range, extract_attachment, get, log, make_brief,
    now_kst, strip_tags,
)
from seen_state import load_seen

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


# --------------------------------------------------------------------------- 기업마당
class Bizinfo:
    key, label = "bizinfo", "기업마당(충북·충남)"
    KEYWORDS = ""   # 키워드 1차 필터 파일 (없으면 전부 판정)
    BASE = "https://www.bizinfo.go.kr"
    LIST = f"{BASE}/sii/siia/selectSIIA200View.do"
    VIEW = f"{BASE}/sii/siia/selectSIIA200Detail.do"
    AREAS = "6430000,6440000"   # 충북, 충남
    # schEndAt=N: 접수 마감 제외 / 등록일 내림차순
    PARAMS = {"rows": 15, "schEndAt": "N", "orderGb": 1, "sort": "desc", "condition": "searchPblancNm", "condition1": "AND"}

    def list_page(self, page: int) -> list[dict]:
        h = get(self.LIST, params={**self.PARAMS, "schAreaDetailCodes": self.AREAS, "cpage": page}, headers=UA_HEADERS).text
        rows = []
        for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            m = re.search(r"pblancId=(PBLN_\d+)", tr)
            if not m:
                continue
            cells = [clean_ws(strip_tags(c)) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            # 번호 | 지원분야 | 제목 | 신청기간 | 소관부처 | 사업수행기관 | 등록일 | 조회
            if len(cells) < 7:
                continue
            start, end = period(cells[3])
            rows.append({"uid": m[1], "title": cells[2], "reg_date": dash_date(cells[6]), "start": start, "end": end,
                         "status": "", "period_text": cells[3], "dept": cells[4], "agency": cells[5], "form": cells[1]})
        return rows

    def detail(self, row: dict) -> dict:
        url = f"{self.VIEW}?pblancId={row['uid']}"
        h = get(url, headers=UA_HEADERS).text
        fields = {clean_ws(strip_tags(k)): strip_tags(v) for k, v in re.findall(
            r'<span class="s_title">([^<]*)</span>\s*<div class="txt"[^>]*>(.*?)</div>\s*</li>', h, re.S)}
        body = "\n\n".join(f"[{k}]\n{v}" for k, v in fields.items() if v)
        site = re.search(r'<span class="s_title">사업신청 사이트</span>.*?href="(http[^"]+)"', h, re.S)
        atts = []
        for name, href in re.findall(r'<div class="file_name">(.*?)</div>.*?href="(/cmm/fms/fileDown\.do\?[^"]+)"', h, re.S):
            atts.append({"name": clean_ws(strip_tags(name)), "url": abs_url(self.BASE, href)})
        return {"ntis_url": url, "source_url": html.unescape(site[1]) if site else "", "body": body,
                "attachments": atts, "contact": clean_ws(fields.get("문의처", ""))[:200]}


class BizinfoOther(Bizinfo):
    """충북·충남을 뺀 나머지 14개 시·도. 주당 70~150건으로 많다."""
    key, label = "bizinfo_other", "기업마당(타지역)"
    KEYWORDS = "bizinfo_keywords.txt"   # 대부분 해당 지역 소상공인 전용이라 역량 키워드로 1차 필터
    # 서울 부산 대구 인천 광주·전남 대전 울산 세종 경기 강원 전북 경북 경남 제주
    AREAS = "6110000,6260000,6270000,6280000,6130000,6300000,6310000,5690000,6410000,6420000,6450000,6470000,6480000,6500000"


# --------------------------------------------------------------------------- 중소벤처기업부
class Mss:
    key, label = "mss", "중소벤처기업부"
    KEYWORDS = ""
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


SITES = {s.key: s for s in (Bizinfo, BizinfoOther, Mss, Riia)}


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
        if r["reg_date"] and date_from <= r["reg_date"] <= date_to:
            uniq.append(r)
    return uniq


def title_key(t: str) -> tuple[frozenset, str]:
    """제목 → (차수 집합, 정규화 본문). 말머리·연도·지역명·'모집 공고' 등을 떼어 게시처마다 다른 표기를 맞춘다.
    차수(2차, 3차)는 다른 공고를 가르는 핵심이라 따로 비교한다."""
    t = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", t or "")          # [충북] 같은 말머리
    t = re.sub(r"(20\d{2}|\d{2})\s*년도?", "", t)
    ords = frozenset(re.findall(r"제?\s*(\d+)\s*차", t))
    t = re.sub(r"제?\s*\d+\s*차", "", t)
    t = re.sub(r"충청북도|충청남도|충북|충남|수혜기업|참여기업|모집|재공고|공고|안내|수정", "", t)
    return ords, re.sub(r"[^0-9A-Za-z가-힣]", "", t)


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


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
    ords, n = title_key(title)
    if len(n) < 8:
        return ""
    bn = _bigrams(n)
    for (o, t), src in known:
        if o != ords:
            continue
        # seen.json 제목은 80자에서 잘려 있으므로 앞부분 포함도 같은 공고로 본다
        if n == t or (min(len(n), len(t)) >= 20 and (n.startswith(t) or t.startswith(n))):
            return src
        bt = _bigrams(t)
        if 2 * len(bn & bt) / (len(bn) + len(bt)) >= 0.8:   # Dice 계수: 어순이 바뀌어도 잡힌다
            return src
    return ""


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
                    log(f"  ! 첨부 실패 {att['name']}: {e}")
                time.sleep(0.3)
        items.append(d)
        time.sleep(0.5)
    write_outputs(site, items, out, date_from, date_to)
    log(f"완료: {len(items)}건 -> {out / 'digest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
