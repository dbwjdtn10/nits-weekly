#!/usr/bin/env python3
"""
나라장터 용역 사전규격 수집기 — 본공고(입찰공고)보다 먼저 공개되는 제안요청서·과업지시서 초안.

2026-10-07~08 나라장터 본공고 28건 중 24건이 사전규격을 거쳤고, 사전규격 → 본공고 간격은 중앙값 8일(2~37일)이었다.
본공고부터 마감까지가 중앙값 12일이므로, 사전규격부터 보면 준비 기간이 약 20일로 늘어난다.

공공데이터포털 "조달청_나라장터 사전규격정보서비스"(G2B_SERVICE_KEY와 같은 키, 활용신청 필요)로 등록일 기준 수집 →
g2b_keywords.txt 공고명 키워드로 1차 필터(사전규격은 대부분 공공조달분류가 없어 분류 필터는 쓰지 않음) →
규격서 첨부(최대 5개)를 받아 과업 개요·참가자격 발췌. 출력 형식은 g2b_collect.py와 같다 (source "g2b_pre").
본공고가 올라오면 g2b_collect.py가 bfSpecRgstNo로 이 기록과 연결해 "사전규격 때 보낸 공고의 본공고"로 표시한다.

사용:
  python g2b_prespec_collect.py --days 4 --out out/g2b_pre
  python g2b_prespec_collect.py --from 2026-10-06 --to 2026-10-13 --out out/g2b_pre --no-seen
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from g2b_collect import (
    attachment_docs, attachment_sections, default_range, extract_attachment, g, g2b_excerpt, load_keywords, log,
    match_keywords, now_kst, session, task_overview, won, NUM_ROWS, TEXT_EXTS,
)
from seen_state import seen_uids

URL = "http://apis.data.go.kr/1230000/ao/HrcspSsstndrdInfoService/getPublicPrcureThngInfoServc"   # 용역


def fetch(key: str, date_from: str, date_to: str) -> list[dict]:
    """등록일시 기준(inqryDiv=1) 기간 내 사전규격 전체 페이지."""
    items: list[dict] = []
    page = 1
    while True:
        params = {"serviceKey": key, "pageNo": page, "numOfRows": NUM_ROWS, "inqryDiv": 1, "type": "json",
                  "inqryBgnDt": date_from.replace("-", "") + "0000", "inqryEndDt": date_to.replace("-", "") + "2359"}
        for attempt in range(3):
            try:
                r = session.get(URL, params=params, timeout=60)
                r.raise_for_status()
                body = r.json()["response"]["body"]
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError(f"사전규격 API 실패: {e} {getattr(r, 'text', '')[:200]}")
                time.sleep(3)
        rows = body.get("items") or []
        items += rows
        log(f"[사전규격] {page}페이지 {len(rows)}건 (누적 {len(items)}/{body.get('totalCount')})")
        if not rows or len(items) >= int(body.get("totalCount") or 0):
            return items
        page += 1


def normalize(p: dict) -> dict:
    no = g(p, "bfSpecRgstNo")
    urls = [g(p, f"specDocFileUrl{i}") for i in range(1, 6)]
    return {
        "uid": no,
        "source": "g2b_pre",
        "type": "용역",
        "title": g(p, "prdctClsfcNoNm"),
        "dept": g(p, "rlDminsttNm"),             # 실수요기관
        "agency": g(p, "orderInsttNm"),          # 발주(공고)기관
        "form": "사전규격",
        "method": g(p, "bsnsDivNm"),
        "announce_date": g(p, "rgstDt")[:16],
        "start": "",
        "end": "",                               # 입찰 마감은 본공고에서 정해진다
        "opinion_end": g(p, "opninRgstClseDt")[:16],
        "delivery": g(p, "dlvrTmlmtDt")[:10] if g(p, "dlvrTmlmtDt") else (f"{g(p, 'dlvrDaynum')}일" if g(p, "dlvrDaynum") not in ("", "0") else ""),
        "budget": f"배정예산 {won(g(p, 'asignBdgtAmt'))}" if g(p, "asignBdgtAmt") else "",
        "sw_biz": g(p, "swBizObjYn"),
        "contact": " ".join(x for x in [g(p, "ofclNm"), g(p, "ofclTelNo")] if x),
        "bid_numbers": g(p, "bidNtceNoList"),
        # 사전규격 상세 페이지는 공개 링크 형식이 없어, 확실히 열리는 첫 규격서 파일 링크를 쓴다 (discord_post.py 호환 필드명)
        "ntis_url": next((u for u in urls if u), ""),
        "source_url": "",
        "attachments": [{"name": f"규격서{i}", "url": u} for i, u in enumerate(urls, 1) if u],
        "body": "",
        "raw": p,
    }


def filename(r) -> str:
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    return urllib.parse.unquote(m.group(1)).strip() if m else ""


def download_docs(d: dict, dest_dir: Path) -> None:
    """규격서 URL에는 파일명이 없으므로 받아 보면서 이름을 정하고, 문서 형식(PDF/HWP/HWPX/DOCX/XLSX/ZIP)만 추출한다."""
    for a in d["attachments"]:
        try:
            r = session.get(a["url"], timeout=120, stream=True)
            r.raise_for_status()
            a["name"] = filename(r) or a["name"]
            if Path(a["name"]).suffix.lower() not in TEXT_EXTS | {".zip"}:
                r.close()
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / re.sub(r'[\/:*?"<>|]+', "_", a["name"])[:150]
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    if f.tell() > 30 * 1024 * 1024:
                        break
            a["path"] = str(path)
            extract_attachment(a, path)
        except Exception as e:  # noqa: BLE001
            log(f"  ! 규격서 다운로드/추출 실패 {a['name']}: {e}")
        time.sleep(0.3)


def write_outputs(items: list[dict], out: Path, date_from: str, date_to: str, n_total: int) -> None:
    (out / "brief").mkdir(parents=True, exist_ok=True)
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "announcements.json").write_text(
        json.dumps({"range": [date_from, date_to], "source": "g2b_pre", "total_before_filter": n_total,
                    "collected_at": now_kst().isoformat(), "items": items}, ensure_ascii=False, indent=1), "utf-8")
    lines = [f"# 나라장터 사전규격(용역) 수집 결과 ({date_from} ~ {date_to})", "",
             f"전체 {n_total}건 중 키워드 필터 통과 {len(items)}건 — 본공고 전 단계라 입찰 마감이 없다", ""]
    for it in items:
        lines += [f"## [{it['uid']}] {it['title']}",
                  f"- 수요기관/발주기관: {it['dept']} / {it['agency']}",
                  f"- 등록 {it['announce_date']} | 의견등록 마감 {it['opinion_end'] or '-'} | 납품기한 {it['delivery'] or '-'}",
                  f"- 금액: {it['budget'] or '-'} | 매칭 키워드: {', '.join(it['keywords'])}",
                  f"- 첨부({len(it['attachments'])}): " + ", ".join(a["name"] for a in it["attachments"]),
                  f"- 요약본: brief/{it['uid']}.md", ""]
    (out / "digest.md").write_text("\n".join(lines), "utf-8")
    for it in items:
        parts = [
            f"# (사전규격) {it['title']}", "",
            f"- 사전규격 등록번호: {it['uid']} (나라장터 > 사전규격 검색에서 이 번호로 찾을 수 있음)",
            f"- 수요기관/발주기관: {it['dept']} / {it['agency']}",
            f"- 업무구분: {it['method']} | SW사업 대상: {it['sw_biz'] or '-'}",
            f"- 등록 {it['announce_date']} | 의견등록 마감 {it['opinion_end'] or '-'} | 납품기한 {it['delivery'] or '-'}",
            f"- 금액: {it['budget'] or '-'}",
            f"- 담당: {it['contact'] or '-'}",
            "- ⚠ 사전규격 단계: 입찰 마감·참가자격·평가방식은 본공고에서 확정된다 (규격서에 있으면 그대로 대조)",
            f"- 첨부: " + (", ".join(a["name"] for a in it["attachments"]) or "-"),
            "",
        ]
        att_brief, att_text = attachment_sections(it["attachments"], excerpt=g2b_excerpt, heading="규격서 참가자격 발췌")
        ov_name, ov, found = task_overview(it["attachments"])
        unread = [a["name"] for a in it["attachments"]
                  if a.get("path") and sum(len(t) for _, t, _ in attachment_docs(a)) < 300]
        if found:
            overview = [f"## 과업 개요 ({ov_name})", "", ov, ""]
        elif unread:
            overview = [f"## 과업 개요 — ⚠ 과업 내용을 찾지 못함, 읽지 못한 첨부 있음 ({', '.join(unread)})", "", ov or "(텍스트 없음)", ""]
        else:
            where = f", {ov_name} 앞부분" if ov_name else ""
            overview = [f"## 과업 개요 — ⚠ 과업 내용 없음 (규격서에서 과업 내용·범위를 찾지 못함{where})", "", ov or "(첨부 텍스트 없음)", ""]
        (out / "brief" / f"{it['uid']}.md").write_text("\n".join(parts + overview + att_brief), "utf-8")
        (out / "text" / f"{it['uid']}.md").write_text("\n".join(parts + att_text), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--days", type=int)
    ap.add_argument("--out", default="out/g2b_pre")
    ap.add_argument("--keywords", default="g2b_keywords.txt")
    ap.add_argument("--no-seen", action="store_true", help="이미 판정한 사전규격도 다시 수집")
    ap.add_argument("--no-attach", action="store_true")
    ap.add_argument("--key", default=os.environ.get("G2B_SERVICE_KEY", ""))
    a = ap.parse_args()
    if not a.key:
        log("G2B_SERVICE_KEY 환경변수(공공데이터포털 인증키)가 필요합니다.")
        return 2
    if "%" in a.key:
        a.key = urllib.parse.unquote(a.key)
    date_from, date_to = (a.date_from, a.date_to) if a.date_from and a.date_to else default_range(a.days)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    kw = load_keywords(Path(a.keywords))

    raw = fetch(a.key, date_from, date_to)
    seen = set() if a.no_seen else seen_uids("g2b_pre")
    by_no: dict[str, dict] = {}
    for p in raw:   # 같은 등록번호가 변경 등록으로 여러 번 오면 마지막 변경분
        no = g(p, "bfSpecRgstNo")
        if no and (no not in by_no or g(p, "chgDt") > g(by_no[no], "chgDt")):
            by_no[no] = p
    items, n_seen, n_bid = [], 0, 0
    for p in by_no.values():
        if p["bfSpecRgstNo"] in seen:
            n_seen += 1
            continue
        if g(p, "bidNtceNoList"):   # 이미 본공고가 나간 사전규격은 본공고 수집(g2b_collect)이 맡는다
            n_bid += 1
            continue
        d = normalize(p)
        hit, bad, ok = match_keywords(d["title"], kw)
        if not ok:
            continue
        d["keywords"] = hit
        items.append(d)
    items.sort(key=lambda x: x["announce_date"])
    log(f"이미 판정 {n_seen}건, 본공고 이미 게시 {n_bid}건 제외 → 필터 통과 {len(items)}건")
    if not a.no_attach:
        for i, d in enumerate(items, 1):
            log(f"[{i}/{len(items)}] 규격서 수집 {d['uid']} {d['title'][:40]}")
            download_docs(d, out / "attachments" / d["uid"])
    write_outputs(items, out, date_from, date_to, len(by_no))
    log(f"완료: 전체 {len(by_no)}건 → 필터 통과 {len(items)}건 -> {out / 'digest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
