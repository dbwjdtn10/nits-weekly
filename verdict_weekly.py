#!/usr/bin/env python3
"""
조건부·부적합 공고를 주(월~금) 단위 텍스트 파일로 모은다 — Discord에는 보내지 않는다.

  조건부/조건부_2026-11-16~2026-11-20.txt    ← 파일 이름이 그 주 월요일~금요일
  부적합/부적합_2026-11-16~2026-11-20.txt

routine(클라우드)이 평일마다 그날 판정한 공고를 이 파일에 더해 커밋하고, 회사 PC는 원할 때 git pull로 받는다
(조건부_받기.bat / 부적합_받기.bat).
파일은 매번 원본 기록(state/conditional.json, state/rejected.json)에서 통째로 다시 만들므로 같은 날 두 번 돌려도 겹치지 않는다.

인자: 표시이름=산출물폴더 (daily_summary.py와 같은 형식). announcements.json·results.json이 없는 폴더는 건너뛴다.

사용:
  python verdict_weekly.py NTIS=out 나라장터=out/g2b 충북=out/chungbuk ...
  python verdict_weekly.py --render                      # 새로 더하지 않고 이번 주 파일만 다시 만들기
  python verdict_weekly.py --render --week 2026-11-09    # 그 날짜가 든 주의 파일
  python verdict_weekly.py --verdict 조건부 ...           # 한 판정만
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from discord_post import period_line, today_kst

ROOT = Path(__file__).resolve().parent
# 판정 → (원본 기록 파일, 주간 파일 폴더). 기록 형식: {"2026-11-18": {"나라장터": [{uid, title, period, agency, reason, url, ...}]}}
VERDICTS = {"조건부": "conditional.json", "부적합": "rejected.json"}
KEEP_DAYS = 28                               # 지난 주 파일을 다시 만들 일이 있을 때를 위해 원본 기록을 남겨 두는 기간
WEEKDAY = "월화수목금토일"
RULE = "━" * 40


def label_day(d: dt.date, year: bool = True) -> str:
    return (d.isoformat() if year else f"{d.month}/{d.day}") + f"({WEEKDAY[d.weekday()]})"


def week_of(d: dt.date) -> tuple[dt.date, dt.date]:
    mon = d - dt.timedelta(days=d.weekday())
    return mon, mon + dt.timedelta(days=4)


def week_file(verdict: str, mon: dt.date, root: Path = ROOT) -> Path:
    return root / verdict / f"{verdict}_{mon.isoformat()}~{(mon + dt.timedelta(days=4)).isoformat()}.txt"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))   # 클라우드(리눅스)와 PC(윈도우)에서 줄바꿈이 달라지지 않게


def load_store(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def collect(specs: list[str], verdict: str) -> dict[str, list[dict]]:
    """산출물 폴더들에서 이번 실행의 그 판정 공고를 꺼낸다."""
    found: dict[str, list[dict]] = {}
    for spec in specs:
        label, _, folder = spec.partition("=")
        ann_p, res_p = Path(folder) / "announcements.json", Path(folder) / "results.json"
        if not (ann_p.exists() and res_p.exists()):
            continue
        ann = json.loads(ann_p.read_text("utf-8"))
        items = {str(it["uid"]): it for it in ann.get("items", [])}
        # 공고마다 기관이 다른 소스(지역 묶음·NTIS)는 기관 이름을 붙인다 — discord_post.verdict_txt와 같은 기준
        grouped = any(it.get("source") != ann.get("source") for it in items.values())
        for r in json.loads(res_p.read_text("utf-8")).get("items", []):
            it = items.get(str(r.get("uid")))
            if r.get("verdict") != verdict or not it:
                continue
            e = {"uid": str(r["uid"]), "title": it.get("title", "?"), "period": period_line(it),
                 "agency": it.get("agency", "") if grouped else "",
                 "reason": r.get("reason", ""), "url": it.get("ntis_url", "")}
            if verdict == "조건부":
                e.update(conditions=r.get("conditions") or [], action=r.get("action", ""),
                         budget=r.get("budget") or it.get("budget") or "")
            found.setdefault(label, []).append(e)
    return found


def merge(store: dict, day: str, found: dict[str, list[dict]]) -> int:
    """오늘 판정분을 기록에 더한다. 다른 날 이미 적힌 공고(다시 판정한 경우)는 처음 날짜를 그대로 둔다."""
    elsewhere = {(label, e["uid"]) for d, labels in store.items() if d != day for label, es in labels.items() for e in es}
    added = 0
    for label, entries in found.items():
        today = {e["uid"]: e for e in store.get(day, {}).get(label, [])}
        for e in entries:
            if (label, e["uid"]) in elsewhere:
                continue
            added += e["uid"] not in today
            today[e["uid"]] = e
        if today:
            store.setdefault(day, {})[label] = list(today.values())
    return added


def render(store: dict, mon: dt.date, verdict: str) -> tuple[str, int]:
    """그 주(월~일)에 그 판정을 받은 공고 전부를 날짜 → 소스 순서로 적는다. 반환: (파일 내용, 건수)"""
    mon_, fri = week_of(mon)
    days = sorted(d for d in store if mon_.isoformat() <= d <= (mon_ + dt.timedelta(days=6)).isoformat())
    counts = {d: sum(len(es) for es in store[d].values()) for d in days}
    total = sum(counts.values())
    out = [f"{verdict} 공고 모음 — {label_day(mon_)} ~ {label_day(fri)}",
           f"총 {total}건" + "".join(f" · {label_day(dt.date.fromisoformat(d), year=False)} {n}건" for d, n in counts.items()),
           f"평일마다 그날 {verdict}{'로' if verdict == '조건부' else '으로'} 판정한 공고가 아래에 날짜별로 더해집니다.", ""]
    for d in days:
        out += [RULE, f"{label_day(dt.date.fromisoformat(d))} 판정 — {counts[d]}건", RULE, ""]
        if not any(e.get("reason") for es in store[d].values() for e in es):   # 이 파일을 만들기 전에 판정한 날
            out += [f"(이날 판정분은 사유·접수 기간 기록이 없습니다 — 그날 Discord에 올라간 {verdict} 파일에 있습니다)", ""]
        for label, entries in store[d].items():
            out += [f"[{label}] {len(entries)}건", ""]
            for n, e in enumerate(entries, 1):
                out.append(f"{n}. {e.get('title', '?')}")
                if e.get("period") or e.get("agency"):
                    out.append("   " + " · ".join(x for x in (e.get("period"), e.get("agency")) if x))
                if e.get("reason"):
                    out.append(f"   사유: {e['reason']}")
                if e.get("conditions"):
                    out.append(f"   확인할 것: {'; '.join(e['conditions'])}")
                if e.get("action"):
                    out.append(f"   제안 액션: {e['action']}")
                if e.get("budget"):
                    out.append(f"   금액: {e['budget']}")
                if e.get("url"):
                    out.append(f"   링크: {e['url']}")
                out.append("")
    return "\n".join(out), total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", help="표시이름=산출물폴더")
    ap.add_argument("--verdict", choices=[*VERDICTS, "all"], default="all", help="모을 판정. 기본: 조건부·부적합 둘 다")
    ap.add_argument("--render", action="store_true", help="새로 더하지 않고 파일만 다시 만든다")
    ap.add_argument("--week", help="다시 만들 주에 든 날짜 (YYYY-MM-DD). 기본: 오늘")
    ap.add_argument("--state-dir", default=str(ROOT / "state"), help="원본 기록 폴더")
    ap.add_argument("--dir", default=str(ROOT), help="조건부/·부적합/ 폴더를 둘 곳")
    a = ap.parse_args()

    today = today_kst()
    mon = week_of(dt.date.fromisoformat(a.week) if a.week else today)[0]
    for verdict, fname in VERDICTS.items():
        if a.verdict not in ("all", verdict):
            continue
        state = Path(a.state_dir) / fname
        store = load_store(state)
        added = 0
        if not a.render:
            added = merge(store, today.isoformat(), collect(a.sources, verdict))
            cutoff = (week_of(today)[0] - dt.timedelta(days=KEEP_DAYS)).isoformat()
            store = {d: v for d, v in sorted(store.items()) if d >= cutoff}
            write(state, json.dumps(store, ensure_ascii=False, indent=1) + "\n")
        text, total = render(store, mon, verdict)
        path = week_file(verdict, mon, Path(a.dir))
        if total:
            write(path, text)
        print(f"{verdict} 주간 파일: {path.name} — 오늘 +{added}건, 이번 주 {total}건" + ("" if total else " (파일 만들지 않음)"),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
