#!/usr/bin/env python3
"""
하루 한 번 Discord에 한 줄 요약을 보낸다 (적합이 0건인 날도 routine이 돌았다는 표시).

  📋 10/01 공고 확인 — 나라장터 45 · NTIS 4 · 중기부 3 · 기업마당(충북·충남) 3건 중 적합 2건

인자: 표시이름=산출물폴더 (announcements.json / results.json 이 있는 폴더). 수집 0건인 소스는 줄에서 뺀다.
폴더에 announcements.json이 없으면 "수집 실패", 수집은 됐는데 results.json이 없으면 "미판정"으로 붙인다.

사용:
  python daily_summary.py NTIS=out 나라장터=out/g2b 충북과기원=out/cbist 지역혁신클러스터=out/riia \\
      중기부=out/mss "기업마당(충북·충남)=out/bizinfo" "기업마당(타지역)=out/bizinfo_other"
  python daily_summary.py ... --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from discord_post import log, send, today_kst


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="표시이름=산출물폴더")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", ""))
    a = ap.parse_args()

    parts, problems, n_fit = [], [], 0
    for spec in a.sources:
        label, _, folder = spec.partition("=")
        out = Path(folder)
        ann_p, res_p = out / "announcements.json", out / "results.json"
        if not ann_p.exists():
            problems.append(f"{label} 수집 실패")
            continue
        n = len(json.loads(ann_p.read_text("utf-8")).get("items", []))
        if n == 0:
            continue
        parts.append(f"{label} {n}")
        if not res_p.exists():
            problems.append(f"{label} 미판정")
            continue
        n_fit += sum(1 for r in json.loads(res_p.read_text("utf-8")).get("items", []) if r.get("verdict") == "적합")

    d = today_kst()
    body = f"{' · '.join(parts)}건 중 적합 **{n_fit}건**" if parts else "신규 공고 없음"
    line = f"📋 {d.month}/{d.day} 공고 확인 — {body}"
    if problems:
        line += f" (⚠ {', '.join(problems)})"

    if not a.webhook and not a.dry_run:
        log("DISCORD_WEBHOOK_URL 환경변수가 필요합니다.")
        return 2
    prefix = "https://discord.com/api/webhooks/"
    webhook = (prefix + a.webhook.split(prefix)[-1] if a.webhook.count(prefix) > 1 else a.webhook).strip()
    send(webhook, {"content": line, "allowed_mentions": {"parse": []}}, dry=a.dry_run)
    log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
