#!/usr/bin/env python3
"""
이미 판정한 공고 기록(state/seen.json) 관리.

  state/seen.json 형식:
    {"ntis": {"<uid>": {"verdict": "적합", "judged_at": "2026-09-16", "title": "..."}}, "g2b": {...}}

사용:
  # 수집기에서: 이미 본 uid 집합 얻기
  from seen_state import load_seen, seen_uids
  # 판정·발송 후: results.json 을 seen.json 에 병합
  python seen_state.py mark --out out --source ntis
  python seen_state.py mark --out out/g2b --source g2b
  python seen_state.py prune --days 120        # 오래된 기록 정리
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "state" / "seen.json"


def load_seen(path: Path = DEFAULT_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def seen_uids(source: str, path: Path = DEFAULT_PATH) -> set[str]:
    return set((load_seen(path).get(source) or {}).keys())


def title_key(t: str) -> tuple[frozenset, str]:
    """제목 → (차수 집합, 정규화 본문). 말머리·연도·지역명·'모집 공고'·'(수정)' 등을 떼어 게시처·재공고마다 다른 표기를 맞춘다.
    차수(2차, 3차)는 다른 공고를 가르는 핵심이라 따로 비교한다."""
    t = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", t or "")          # [충북] 같은 말머리
    t = re.sub(r"(20\d{2}|\d{2})\s*년도?", "", t)
    ords = frozenset(re.findall(r"제?\s*(\d+)\s*차", t))
    t = re.sub(r"제?\s*\d+\s*차", "", t)
    t = re.sub(r"충청북도|충청남도|충북|충남|수혜기업|참여기업|모집|재공고|공고|안내|수정|긴급", "", t)
    return ords, re.sub(r"[^0-9A-Za-z가-힣]", "", t)


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def same_title(a: tuple[frozenset, str], b: tuple[frozenset, str]) -> bool:
    """title_key 둘이 같은 공고인지. 너무 짧은 제목(8자 미만)은 비교하지 않는다."""
    (oa, na), (ob, nb) = a, b
    if oa != ob or len(na) < 8 or len(nb) < 8:
        return False
    # seen.json 제목은 80자에서 잘려 있고, 재공고는 끝에 '용역'이 붙기도 하므로 앞부분 포함도 같은 공고로 본다
    if na == nb or (min(len(na), len(nb)) >= 20 and (na.startswith(nb) or nb.startswith(na))):
        return True
    ba, bb = _bigrams(na), _bigrams(nb)
    return 2 * len(ba & bb) / (len(ba) + len(bb)) >= 0.8   # Dice 계수: 어순이 바뀌어도 잡힌다


def find_seen(sources: tuple[str, ...], title: str, numbers: tuple[str, ...] = (), path: Path = DEFAULT_PATH) -> dict | None:
    """이미 판정한 같은 공고(재공고·정정공고로 번호가 바뀐 것, 사전규격 → 본공고 포함)의 seen 기록. 없으면 None.
    numbers: 이 공고가 가리키는 이전 번호들 (나라장터 befBidBbancNo·bfSpecRgstNo 등) — uid 앞부분과 일치하면 같은 공고."""
    data = load_seen(path)
    key = title_key(title)
    hits = [(src, uid, v) for src in sources for uid, v in (data.get(src) or {}).items()
            if (numbers and uid.rsplit("-", 1)[0] in numbers) or same_title(key, title_key(v.get("title", "")))]
    if not hits:
        return None
    src, uid, v = min(hits, key=lambda h: h[2].get("judged_at", ""))   # 처음 판정(발송)한 기록
    return {"source": src, "uid": uid, **v}


def save_seen(data: dict, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")


def mark(out: Path, source: str, path: Path = DEFAULT_PATH) -> int:
    ann = json.loads((out / "announcements.json").read_text("utf-8"))
    titles = {it["uid"]: it.get("title", "") for it in ann["items"]}
    res_path = out / "results.json"
    results = json.loads(res_path.read_text("utf-8")).get("items", []) if res_path.exists() else []
    verdicts = {str(r["uid"]): r.get("verdict", "") for r in results}
    data = load_seen(path)
    bucket = data.setdefault(source, {})
    today = dt.date.today().isoformat()
    n = 0
    for uid, title in titles.items():
        if uid in bucket:
            continue
        bucket[uid] = {"verdict": verdicts.get(uid, "미판정"), "judged_at": today, "title": title[:80]}
        n += 1
    save_seen(data, path)
    print(f"seen.json: {source} +{n}건 (총 {len(bucket)}건)", file=sys.stderr)
    return n


def prune(days: int, path: Path = DEFAULT_PATH) -> None:
    data = load_seen(path)
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    removed = 0
    for src, bucket in data.items():
        for uid in [u for u, v in bucket.items() if v.get("judged_at", "") < cutoff]:
            del bucket[uid]
            removed += 1
    save_seen(data, path)
    print(f"seen.json: {removed}건 정리", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mark")
    m.add_argument("--out", required=True)
    m.add_argument("--source", required=True, choices=["ntis", "g2b", "g2b_pre", "cbist", "riia", "mss", "bizinfo", "bizinfo_other"])
    m.add_argument("--state", default=str(DEFAULT_PATH))
    p = sub.add_parser("prune")
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--state", default=str(DEFAULT_PATH))
    a = ap.parse_args()
    if a.cmd == "mark":
        mark(Path(a.out), a.source, Path(a.state))
    else:
        prune(a.days, Path(a.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
