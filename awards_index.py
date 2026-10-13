#!/usr/bin/env python3
"""
나라장터 용역 낙찰 이력 색인 (state/awards.json) — 적합 공고 카드의 "과거 유사 발주" 근거.

낙찰정보 API(조달청_나라장터 낙찰정보서비스, G2B_SERVICE_KEY와 같은 키)는 공고명 검색이 안 되고 한 번에 한 달 범위만
조회되므로(용역 낙찰 월 ~6,500건), g2b_keywords.txt 공고명 필터를 통과한 낙찰만 저장해 두고 로컬에서 찾는다.

사용:
  python awards_index.py backfill --months 24          # 최초 1회 (월별 조회, 수 분)
  python awards_index.py update --days 4               # 매일: 최근 등록 낙찰 추가
  python awards_index.py lookup --title "디지털트윈 EV 정비 교육 콘텐츠" --agency "국민대학교"
      → 같은 수요기관 낙찰 + 공고명이 비슷한 낙찰을 최근순으로 출력 (판정 시 card.history 작성용)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from g2b_collect import g, load_keywords, log, match_keywords, now_kst, session, won, NUM_ROWS
from seen_state import _bigrams, title_key

URL = "http://apis.data.go.kr/1230000/as/ScsbidInfoService/getScsbidListSttusServc"   # 용역 낙찰
PATH = Path(__file__).resolve().parent / "state" / "awards.json"
KEEP_DAYS = 365 * 3


def fetch(key: str, d_from: dt.date, d_to: dt.date) -> list[dict]:
    """등록일 기준(inqryDiv=1) 낙찰 목록. 범위는 한 달 이내여야 한다."""
    items: list[dict] = []
    page = 1
    while True:
        params = {"serviceKey": key, "pageNo": page, "numOfRows": NUM_ROWS, "inqryDiv": 1, "type": "json",
                  "inqryBgnDt": d_from.strftime("%Y%m%d") + "0000", "inqryEndDt": d_to.strftime("%Y%m%d") + "2359"}
        for attempt in range(3):
            try:
                r = session.get(URL, params=params, timeout=90)
                r.raise_for_status()
                body = r.json()["response"]["body"]
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError(f"낙찰정보 API 실패 {d_from}~{d_to}: {e} {getattr(r, 'text', '')[:200]}")
                time.sleep(3)
        rows = body.get("items") or []
        items += rows
        if not rows or len(items) >= int(body.get("totalCount") or 0):
            return items
        page += 1


def compact(a: dict) -> dict:
    return {
        "title": g(a, "bidNtceNm"),
        "agency": g(a, "dminsttNm"),
        "winner": g(a, "bidwinnrNm"),
        "amount": g(a, "sucsfbidAmt"),
        "rate": g(a, "sucsfbidRate"),
        "bidders": g(a, "prtcptCnum"),
        "date": g(a, "fnlSucsfDate") or g(a, "rgstDt")[:10],
    }


def load() -> dict:
    if PATH.exists():
        return json.loads(PATH.read_text("utf-8"))
    return {"updated": "", "items": {}}


def save(data: dict) -> None:
    cutoff = (now_kst().date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    data["items"] = {k: v for k, v in data["items"].items() if v.get("date", "") >= cutoff}
    data["updated"] = now_kst().date().isoformat()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    # 한 줄에 한 건 — 매일 커밋 diff가 추가분만 보이도록
    body = ",\n".join(f"  {json.dumps(k, ensure_ascii=False)}: {json.dumps(v, ensure_ascii=False, sort_keys=True)}"
                      for k, v in sorted(data["items"].items()))
    PATH.write_text(f'{{"updated": "{data["updated"]}",\n "items": {{\n{body}\n }}\n}}\n', "utf-8")


def add(data: dict, rows: list[dict], kw: dict) -> int:
    n = 0
    for a in rows:
        if not g(a, "bidwinnrNm"):
            continue
        if not match_keywords(g(a, "bidNtceNm"), kw)[2]:
            continue
        k = f"{g(a, 'bidNtceNo')}-{g(a, 'bidNtceOrd') or '000'}"
        if k not in data["items"]:
            n += 1
        data["items"][k] = compact(a)
    return n


def month_ranges(months: int) -> list[tuple[dt.date, dt.date]]:
    end = now_kst().date()
    out = []
    while len(out) < months:
        start = end.replace(day=1)
        out.append((start, end))
        end = start - dt.timedelta(days=1)
    return out


def lookup(title: str, agency: str, limit: int) -> list[tuple[str, dict]]:
    """같은 수요기관 낙찰 → 공고명이 비슷한 낙찰 순. 공고명 유사도는 bigram Dice + 공통 키워드."""
    data = load()["items"]
    _, tn = title_key(title)
    bt = _bigrams(tn)
    ag = re.sub(r"\s", "", agency or "")
    scored = []
    for k, v in data.items():
        _, vn = title_key(v["title"])
        bv = _bigrams(vn)
        sim = 2 * len(bt & bv) / (len(bt) + len(bv)) if bt and bv else 0
        same_ag = bool(ag) and (ag in re.sub(r"\s", "", v["agency"]) or re.sub(r"\s", "", v["agency"]) in ag)
        if sim >= 0.25 or (same_ag and sim >= 0.1):
            scored.append((sim + (0.5 if same_ag else 0), k, v))
    scored.sort(key=lambda x: x[2]["date"], reverse=True)   # 같은 점수면 최근 낙찰 먼저
    scored.sort(key=lambda x: -x[0])
    return [(k, v) for _, k, v in scored[:limit]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill")
    b.add_argument("--months", type=int, default=24)
    u = sub.add_parser("update")
    u.add_argument("--days", type=int, default=4)
    lk = sub.add_parser("lookup")
    lk.add_argument("--title", required=True)
    lk.add_argument("--agency", default="")
    lk.add_argument("--limit", type=int, default=10)
    for p in (b, u):
        p.add_argument("--keywords", default="g2b_keywords.txt")
        p.add_argument("--key", default=os.environ.get("G2B_SERVICE_KEY", ""))
    a = ap.parse_args()

    if a.cmd == "lookup":
        rows = lookup(a.title, a.agency, a.limit)
        if not rows:
            print("(유사 낙찰 이력 없음)")
        for k, v in rows:
            rate = f"낙찰률 {float(v['rate']):.1f}%" if v.get("rate") else "낙찰률 -"
            print(f"{v['date']} | {v['agency']} | {v['title']} | 낙찰 {v['winner']} {won(v['amount'])} {rate} | 참가 {v['bidders'] or '-'}개사 | {k}")
        return 0

    if not a.key:
        log("G2B_SERVICE_KEY 환경변수가 필요합니다.")
        return 2
    if "%" in a.key:
        a.key = urllib.parse.unquote(a.key)
    kw = load_keywords(Path(a.keywords))
    data = load()
    if a.cmd == "backfill":
        total = 0
        for d_from, d_to in month_ranges(a.months):
            rows = fetch(a.key, d_from, d_to)
            n = add(data, rows, kw)
            total += n
            log(f"{d_from:%Y-%m}: 낙찰 {len(rows)}건 → 키워드 통과 신규 {n}건")
        log(f"backfill 완료: +{total}건 (총 {len(data['items'])}건)")
    else:
        today = now_kst().date()
        rows = fetch(a.key, today - dt.timedelta(days=a.days - 1), today)
        n = add(data, rows, kw)
        log(f"update: 최근 {a.days}일 낙찰 {len(rows)}건 → 신규 {n}건 (총 {len(data['items'])}건)")
    save(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
