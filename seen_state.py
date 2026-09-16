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
    m.add_argument("--source", required=True, choices=["ntis", "g2b"])
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
