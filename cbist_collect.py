#!/usr/bin/env python3
"""
충북과학기술혁신원(CBIST) 사업공고 수집기.
  https://www.cbist.or.kr/home/sub.do?mncd=1131

지정 기간(기본: 지난주 월~일)에 등록된 공고를 목록에서 골라 상세 본문과 첨부(PDF/HWP/HWPX/DOCX)를 내려받아
NTIS 수집기와 같은 형식(announcements.json / digest.md / brief/ / text/)으로 저장한다.
이미 판정한 공고(state/seen.json)와 접수 마감(종료) 공고는 자동 제외.

사용:
  python cbist_collect.py --out out/cbist
  python cbist_collect.py --from 2026-09-07 --to 2026-09-13 --out out/cbist
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path

from ntis_collect import (
    KST, MAX_ATTACH_BYTES, TEXT_EXTS, clean_ws, default_range, dday, extract_text, get, is_form_attachment,
    log, make_brief, now_kst, session, strip_tags,
)
from seen_state import seen_uids

BASE = "https://www.cbist.or.kr"
LIST_URL = f"{BASE}/home/sub.do"
MNCD = "1131"
SOURCE = "cbist"


# --------------------------------------------------------------------------- list
def parse_rows(h: str) -> list[dict]:
    rows = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        tr = m.group(1)
        vm = re.search(r'href="\?mncd=' + MNCD + r'&(?:amp;)?mode=view&(?:amp;)?no=(\d+)[^"]*"', tr)
        if not vm:
            continue
        cells = [clean_ws(strip_tags(c)) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        text = " | ".join(cells)
        title_m = re.search(r'mode=view&(?:amp;)?no=\d+[^"]*"[^>]*>(.*?)</a>', tr, re.S)
        title = clean_ws(strip_tags(title_m.group(1))) if title_m else ""
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
        # 등록일, 접수시작 - 접수종료 순으로 나옴 (없는 경우도 있음)
        reg = dates[0] if dates else ""
        period = re.search(r"(\d{4}-\d{2}-\d{2})\s*-\s*(\d{4}-\d{2}-\d{2})", text)
        start, end = (period.group(1), period.group(2)) if period else ("", "")
        status = "종료" if "종료" in text else ("진행중" if "진행중" in text else "")
        rows.append({"uid": vm.group(1), "title": title, "reg_date": reg, "start": start, "end": end, "status": status})
    return rows


def fetch_list(date_from: str, date_to: str, max_pages: int = 5) -> list[dict]:
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        h = get(LIST_URL, params={"mncd": MNCD, "page": page}).text
        rows = parse_rows(h)
        log(f"목록 {page}페이지: {len(rows)}건")
        if not rows:
            break
        out.extend(rows)
        # 가장 오래된 등록일이 기간 시작보다 이전이면 중단
        regs = [r["reg_date"] for r in rows if r["reg_date"]]
        if regs and min(regs) < date_from:
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


# --------------------------------------------------------------------------- detail
def fetch_detail(row: dict) -> dict:
    url = f"{LIST_URL}?mncd={MNCD}&mode=view&no={row['uid']}"
    h = get(url).text
    d = dict(row)
    d.update({"source": SOURCE, "ntis_url": url, "source_url": "", "dept": "충청북도", "agency": "충북과학기술혁신원",
              "form": "지원사업 공고", "program": "", "budget": "", "contact": "", "announce_date": row["reg_date"], "meta": {}})
    atts = []
    for href, name in re.findall(r'href="([^"]*board/download\.do[^"]*)"[^>]*>(.*?)</a>', h, re.S):
        nm = clean_ws(strip_tags(name))
        if not nm:
            continue
        atts.append({"name": nm, "url": f"{BASE}/home/{html.unescape(href).lstrip('/')}" if not href.startswith("http") else html.unescape(href)})
    d["attachments"] = atts
    # 본문: 첨부 블록 이후 ~ 이전글/다음글 이전
    body = ""
    i = h.find('class="board_view"')
    seg = h[i:] if i > 0 else h
    k = seg.rfind("download.do")
    if k > 0:
        k2 = seg.find("</tr>", k)
        seg = seg[k2:] if k2 > 0 else seg[k:]
    for stop in ["이전글", "다음글", 'class="btn_area"', "<footer", 'id="footer"']:
        j = seg.find(stop)
        if j > 0:
            seg = seg[:j]
    body = strip_tags(seg)
    d["body"] = body
    return d


def download_attachment(att: dict, dest_dir: Path) -> Path | None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", att["name"])[:150] or "file"
    path = dest_dir / safe
    if path.exists() and path.stat().st_size > 0:
        return path
    r = session.get(att["url"], timeout=120, stream=True)
    r.raise_for_status()
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
        return None
    return path


# --------------------------------------------------------------------------- output
def write_outputs(items: list[dict], out: Path, date_from: str, date_to: str) -> None:
    (out / "brief").mkdir(parents=True, exist_ok=True)
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "announcements.json").write_text(
        json.dumps({"range": [date_from, date_to], "source": SOURCE, "collected_at": now_kst().isoformat(), "items": items},
                   ensure_ascii=False, indent=1), "utf-8")
    lines = [f"# 충북과학기술혁신원 사업공고 수집 결과 ({date_from} ~ {date_to})", "", f"총 {len(items)}건", ""]
    for it in items:
        lines += [f"## [{it['uid']}] {it['title']}",
                  f"- 등록 {it['reg_date']} | 접수 {it['start']} ~ {it['end']} ({it['dday']}) | {it['status']}",
                  f"- 상세: {it['ntis_url']}",
                  f"- 첨부({len(it['attachments'])}): " + ", ".join(a["name"] for a in it["attachments"]),
                  f"- 요약본: brief/{it['uid']}.md", ""]
    (out / "digest.md").write_text("\n".join(lines), "utf-8")
    for it in items:
        head = [f"# {it['title']}", "", f"- UID: {it['uid']} (충북과학기술혁신원)",
                f"- 등록 {it['reg_date']} | 접수 {it['start']} ~ {it['end']} | {it['status']}",
                f"- 상세: {it['ntis_url']}", ""]
        parts = head + ["## 본문", "", it["body"] or "(본문 없음)", ""]
        for a in it["attachments"]:
            parts += [f"## 첨부: {a['name']}", "", a.get("text") or "(텍스트 추출 없음/불가)", ""]
        (out / "text" / f"{it['uid']}.md").write_text("\n".join(parts), "utf-8")
        bparts = head + ["## 본문 (발췌)", "", make_brief(it["body"]) or "(본문 없음)", ""]
        for a in it["attachments"]:
            t = a.get("text") or ""
            if t and not is_form_attachment(a["name"]):
                bparts += [f"## 첨부 (발췌): {a['name']}", "", make_brief(t), ""]
        (out / "brief" / f"{it['uid']}.md").write_text("\n".join(bparts), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--days", type=int)
    ap.add_argument("--out", default="out/cbist")
    ap.add_argument("--no-attach", action="store_true")
    ap.add_argument("--no-seen", action="store_true")
    ap.add_argument("--include-closed", action="store_true")
    a = ap.parse_args()

    date_from, date_to = (a.date_from, a.date_to) if a.date_from and a.date_to else default_range(a.days)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log(f"수집 기간: {date_from} ~ {date_to} -> {out.resolve()}")

    rows = fetch_list(date_from, date_to)
    n_all = len(rows)
    if not a.no_seen:
        seen = seen_uids(SOURCE)
        rows = [r for r in rows if r["uid"] not in seen]
        log(f"이미 판정한 공고 제외: {n_all - len(rows)}건")
    if not a.include_closed:
        before = len(rows)
        rows = [r for r in rows if r["status"] != "종료" and not dday(r["end"]).startswith("마감")]
        log(f"마감/종료 공고 제외: {before - len(rows)}건")
    log(f"상세 수집 대상 {len(rows)}건")

    items = []
    for i, r in enumerate(rows, 1):
        log(f"[{i}/{len(rows)}] {r['uid']} {r['title'][:50]}")
        try:
            d = fetch_detail(r)
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
                        if p.suffix.lower() in TEXT_EXTS:
                            att["text"] = extract_text(p)
                            log(f"  + {p.name} ({att['size'] // 1024}KB, 텍스트 {len(att.get('text', ''))}자)")
                except Exception as e:  # noqa: BLE001
                    log(f"  ! 첨부 실패 {att['name']}: {e}")
                time.sleep(0.3)
        items.append(d)
        time.sleep(0.5)
    write_outputs(items, out, date_from, date_to)
    log(f"완료: {len(items)}건 -> {out / 'digest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
