#!/usr/bin/env python3
"""
판정 결과(results.json)를 Discord 웹훅으로 발송한다.

입력:
  out/announcements.json  - ntis_collect.py 산출물
  out/results.json        - 판정 결과 (Claude가 작성). 형식:
      {
        "summary": "이번 주 한 줄 총평(선택)",
        "items": [
          {"uid": "1277074", "verdict": "적합" | "조건부" | "부적합",
           "score": 0-100, "reason": "판정 사유 1~3문장",
           "conditions": ["핵심 자격/조건 요약", ...],
           "action": "다음 액션 제안(선택)"}
        ]
      }
환경변수:
  DISCORD_WEBHOOK_URL   (필수)
사용:
  python discord_post.py --out out                 # 적합·조건부를 링크 목록으로 발송 (부적합 미언급)
  python discord_post.py --out out --dry-run       # 콘솔 출력만
  python discord_post.py --out out --format embed  # 상세 embed + 첨부파일 형식
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

MAX_FILE = 8 * 1024 * 1024        # Discord 기본 업로드 한도
MAX_FILES_PER_MSG = 10
COLOR = {"적합": 0x2ECC71, "조건부": 0xF1C40F, "부적합": 0x95A5A6}
EMOJI = {"적합": "🟢", "조건부": "🟡", "부적합": "⚪"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def send(webhook: str, payload: dict, files: list[Path] | None = None, dry: bool = False) -> None:
    if dry:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        if files:
            print("  files:", [f.name for f in files])
        return
    for attempt in range(5):
        if files:
            fh = []
            try:
                fh = [(f"files[{i}]", (f.name, open(f, "rb"))) for i, f in enumerate(files)]
                r = requests.post(webhook, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                                  files=fh, timeout=120)
            finally:
                for _, (_, fp) in fh:
                    fp.close()
        else:
            r = requests.post(webhook, json=payload, timeout=60)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "2")) + 0.5
            log(f"  rate limited, {wait}s 대기")
            time.sleep(wait)
            continue
        if r.status_code >= 400:
            log(f"  ! Discord 오류 {r.status_code}: {r.text[:300]}")
            if files and attempt == 0:
                log("  첨부 없이 재시도")
                files = None
                continue
            r.raise_for_status()
        time.sleep(1.0)
        return
    raise RuntimeError("Discord 전송 실패")


def trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_embed(it: dict, res: dict) -> dict:
    v = res.get("verdict", "부적합")
    g2b = it.get("source") == "g2b"
    fields = [
        {"name": "수요기관 / 공고기관" if g2b else "부처 / 공고기관", "value": trunc(f"{it.get('dept','')} / {it.get('agency','')}", 200) or "-", "inline": True},
        {"name": "입찰마감" if g2b else "접수 ~ 마감", "value": (f"{it.get('end','')}  **{it.get('dday','')}**" if g2b else f"{it.get('start','')} ~ {it.get('end','')}  **{it.get('dday','')}**"), "inline": True},
    ]
    if it.get("budget"):
        fields.append({"name": "금액" if g2b else "지원규모", "value": trunc(it["budget"], 100), "inline": True})
    if g2b:
        info = " | ".join(x for x in [it.get("type"), it.get("form"), it.get("method"), f"지역제한 {it['region_limit']}" if it.get("region_limit") else ""] if x)
        if info:
            fields.append({"name": "구분", "value": trunc(info, 200), "inline": False})
    if it.get("program"):
        fields.append({"name": "사업명", "value": trunc(it["program"], 200), "inline": False})
    if res.get("conditions"):
        fields.append({"name": "핵심 자격·조건", "value": trunc("\n".join(f"• {c}" for c in res["conditions"]), 1000), "inline": False})
    fields.append({"name": "판정 사유", "value": trunc(res.get("reason", ""), 1000) or "-", "inline": False})
    if res.get("action"):
        fields.append({"name": "제안 액션", "value": trunc(res["action"], 500), "inline": False})
    links = f"[{'나라장터 상세' if g2b else 'NTIS 상세'}]({it['ntis_url']})"
    if it.get("source_url"):
        links += f" · [원문 공고]({it['source_url']})"
    fields.append({"name": "링크", "value": links, "inline": False})
    return {
        "title": trunc(f"{EMOJI.get(v,'')} [{v} {res.get('score','')}] {it.get('title','')}", 256),
        "url": it["ntis_url"],
        "color": COLOR.get(v, 0x95A5A6),
        "fields": fields,
        "footer": {"text": f"{'나라장터' if g2b else 'NTIS'} {it['uid']} · 공고일 {it.get('announce_date','')}"},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-files", action="store_true")
    ap.add_argument("--send", default="적합,조건부", help="보낼 판정 (콤마 구분). 기본: 적합,조건부 (조건부도 적합으로 취급)")
    ap.add_argument("--format", default="links", choices=["links", "embed"], help="links: 공고당 한 줄 링크(기본) / embed: 상세 embed + 첨부")
    ap.add_argument("--with-rejected", action="store_true", help="부적합 공고 요약 목록도 발송")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", ""))
    ap.add_argument("--title", default="NTIS 국가R&D통합공고 주간 리포트", help="헤더 제목")
    a = ap.parse_args()

    if not a.webhook and not a.dry_run:
        log("DISCORD_WEBHOOK_URL 환경변수가 필요합니다.")
        return 2
    # 붙여넣기 실수로 프리픽스가 중복된 웹훅 URL 정규화
    prefix = "https://discord.com/api/webhooks/"
    if a.webhook.count(prefix) > 1:
        a.webhook = prefix + a.webhook.split(prefix)[-1]
    a.webhook = a.webhook.strip()
    out = Path(a.out)
    ann = json.loads((out / "announcements.json").read_text("utf-8"))
    res = json.loads((out / "results.json").read_text("utf-8"))
    items = {it["uid"]: it for it in ann["items"]}
    results = {str(r["uid"]): r for r in res.get("items", [])}
    date_from, date_to = ann.get("range", ["", ""])

    order = {"적합": 0, "조건부": 1, "부적합": 2}
    judged = sorted(results.values(), key=lambda r: (order.get(r.get("verdict"), 9), -float(r.get("score", 0) or 0)))
    send_set = {v.strip() for v in a.send.split(",") if v.strip()}
    picked = [r for r in judged if r.get("verdict") in send_set]
    n_fit = sum(1 for r in judged if r.get("verdict") == "적합")
    n_cond = sum(1 for r in judged if r.get("verdict") == "조건부")
    rejected = [r for r in judged if r.get("verdict") == "부적합"]
    unjudged = [u for u in items if u not in results]

    # 1) 헤더 (부적합은 언급하지 않음)
    head = f"## 📋 {a.title} ({date_from} ~ {date_to})\n수집 **{len(items)}건** 중 제안 가능 **{len(picked)}건**"
    if not picked:
        head += "\n\n이번 주는 X2R이 제안 가능한 공고가 없습니다."

    # 2-a) links 형식: 공고당 한 줄 (제목 링크 · 마감 · 금액), 2000자 단위로 분할 전송
    if a.format == "links":
        lines = []
        for r in picked:
            it = items.get(str(r["uid"]))
            if not it:
                continue
            extra = " · ".join(x for x in [
                f"마감 {it.get('end','')} {it.get('dday','')}".strip() if it.get("end") else "",
                trunc(it.get("budget", ""), 40),
                trunc(f"{it.get('dept','')}", 30),
            ] if x)
            lines.append(f"• [{trunc(it.get('title','?'), 90)}]({it['ntis_url']})" + (f"\n　{extra}" if extra else ""))
        buf = head
        for ln in lines:
            if len(buf) + len(ln) + 2 > 1900:
                send(a.webhook, {"content": buf, "allowed_mentions": {"parse": []}}, dry=a.dry_run)
                buf = ""
            buf += ("\n\n" + ln) if buf else ln
        send(a.webhook, {"content": buf, "allowed_mentions": {"parse": []}}, dry=a.dry_run)
        log(f"완료: {len(lines)}건 링크 발송 (적합 {n_fit}, 조건부 {n_cond}, 부적합 {len(rejected)} 미발송)")
        return 0

    send(a.webhook, {"content": head, "allowed_mentions": {"parse": []}}, dry=a.dry_run)

    # 2-b) embed 형식: 공고당 메시지 1개 (embed + 첨부)
    for r in picked:
        it = items.get(str(r["uid"]))
        if not it:
            log(f"  ! announcements에 없는 uid {r['uid']} 건너뜀")
            continue
        files: list[Path] = []
        skipped: list[str] = []
        if not a.no_files:
            for att in it.get("attachments", []):
                p = out / att.get("path", "") if att.get("path") else None
                if p and p.is_file() and p.stat().st_size <= MAX_FILE and len(files) < MAX_FILES_PER_MSG:
                    files.append(p)
                else:
                    skipped.append(att["name"])
        embed = build_embed(it, r)
        url_atts = [att for att in it.get("attachments", []) if att.get("url") and not att.get("path")]
        if url_atts:
            embed["fields"].append({"name": "공고문 첨부", "value": trunc("\n".join(f"[{att['name']}]({att['url']})" for att in url_atts[:8]), 1000), "inline": False})
            skipped = [s_ for s_ in skipped if s_ not in {att["name"] for att in url_atts}]
        if skipped:
            embed["fields"].append({"name": "미첨부(용량초과/실패) — NTIS에서 받기", "value": trunc("\n".join(f"• {s}" for s in skipped), 1000), "inline": False})
        send(a.webhook, {"embeds": [embed], "allowed_mentions": {"parse": []}}, files or None, dry=a.dry_run)
        log(f"  전송: [{r['verdict']}] {it['title'][:40]} (첨부 {len(files)})")

    # 3) 부적합 요약(간략 목록)
    if rejected and a.with_rejected:
        lines = ["**⚪ 부적합으로 판정된 공고**"]
        for r in rejected:
            it = items.get(str(r["uid"]), {})
            lines.append(f"• [{trunc(it.get('title','?'), 70)}]({it.get('ntis_url','')}) — {trunc(r.get('reason',''), 90)}")
        chunk, buf = [], ""
        for ln in lines:
            if len(buf) + len(ln) + 1 > 1900:
                chunk.append(buf)
                buf = ""
            buf += ln + "\n"
        chunk.append(buf)
        for c in chunk:
            send(a.webhook, {"content": c, "allowed_mentions": {"parse": []}}, dry=a.dry_run)

    if unjudged:
        send(a.webhook, {"content": "⚠️ 미판정: " + ", ".join(f"[{items[u]['title'][:40]}]({items[u]['ntis_url']})" for u in unjudged),
                         "allowed_mentions": {"parse": []}}, dry=a.dry_run)
    log(f"완료: {'/'.join(sorted(send_set))} {len(picked)}건 발송 (적합 {n_fit}, 조건부 {n_cond}, 부적합 {len(rejected)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
