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
           "action": "다음 액션 제안(선택)",
           "card": {"make": "만들 것"},   # 적합만 — 제목·마감·예산·기관 줄 아래 표시
           "changes": "재공고에서 바뀐 점(선택)"}
        ]
      }
  announcements.json 항목에 repost_of(이미 판정한 같은 공고)가 있고 이전·이번 모두 적합이면
  카드 대신 "다시 올라온 공고" 한 줄로 보낸다.
환경변수:
  DISCORD_WEBHOOK_URL   (필수)
사용:
  python discord_post.py --out out                 # 적합만 카드 + 공고 첨부 전부(HWP·PDF·서식)를 zip 하나로
                                                   # 조건부·부적합은 판정별 txt 파일로 첨부
  python discord_post.py --out out --dry-run       # 콘솔 출력만
  ※ 적합이 0건이어도 조건부·부적합이 있으면 헤더 + txt 파일은 발송. 판정 0건이면 아무것도 보내지 않음
  python discord_post.py --out out --format embed  # 상세 embed + 첨부파일 형식
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

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


def today_kst() -> dt.date:
    return dt.datetime.now(ZoneInfo("Asia/Seoul")).date()


def end_date(s: str) -> dt.date | None:
    """'2026.10.07' / '2026-09-30 10:00' / '2026-09-29' 등에서 날짜만 추출."""
    m = re.match(r"\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


# 검토 카드 항목 (results.json의 card 필드) — 마감·예산·기관 줄과 '만들 것'만 보면 자세히 볼지 정할 수 있어
# 나머지 항목(비슷한 실적·평가·확인할 점·판정 근거)은 읽는 데 방해만 된다는 피드백으로 뺐다
CARD_FIELDS = [("make", "📦 만들 것")]


MAX_ZIP = 9 * 1024 * 1024   # 공고 첨부 zip 상한 (Discord 무료 서버 업로드 한도 10MB)
DOC_MAIN = re.compile(r"공고|제안\s*요청|과업|규격|사양|지침|안내서|설명서")


def short_name(name: str, title: str) -> str:
    """첨부 이름에서 공고 제목과 겹치는 부분을 떼어 '1. 입찰공고문(수정).hwp'처럼 짧게 (Discord가 긴 파일명을 잘라 보여서)."""
    stem, ext = os.path.splitext(name)
    m = SequenceMatcher(None, stem, title, autojunk=False).find_longest_match(0, len(stem), 0, len(title))
    short = stem[:m.a] + " " + stem[m.a + m.size:] if m.size >= 8 else stem
    short = re.sub(r"[\s_]+", " ", short).strip(" _-") or stem
    return short + ext.lower()


def bundle_docs(it: dict, out: Path) -> tuple[Path | None, list[str], list[dict]]:
    """적합 공고의 첨부 전부(HWP·PDF·서식)를 zip 하나로 묶는다 — 나라장터 '다운로드' 버튼처럼 한 번에 받기.
    수집기가 판정용으로 고르느라 안 받은 파일(나라장터 HWP본·서식 등)은 여기서 받는다.
    반환: (zip, zip에 넣은 파일의 짧은 이름, 못 넣은 첨부 — 링크로 안내)"""
    dest = out / "bundle" / re.sub(r"[^0-9A-Za-z_-]", "_", it["uid"])
    got, missed = [], []
    for att in it.get("attachments", []):
        p = None
        if att.get("path"):
            p = Path(att["path"]) if Path(att["path"]).is_file() else out / att["path"]   # 수집기에 따라 out 기준 상대경로
        if not (p and p.is_file()) and att.get("url"):
            p = dest / re.sub(r'[\/:*?"<>|]+', "_", att.get("name") or "첨부")[:150]
            try:
                if not p.is_file():
                    r = requests.get(att["url"], headers={"User-Agent": "Mozilla/5.0 X2R-collector/1.0"}, timeout=120)
                    r.raise_for_status()
                    if "text/html" in r.headers.get("Content-Type", ""):   # 오류·로그인 페이지
                        raise ValueError("파일 대신 웹페이지 응답")
                    dest.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(r.content)
            except Exception as e:  # noqa: BLE001
                log(f"  ! 첨부 받기 실패 {att.get('name')}: {e}")
                p = None
        if p and p.is_file():
            got.append((att, p))
        else:
            missed.append(att)
    if not got:
        return None, [], missed
    got.sort(key=lambda x: 0 if DOC_MAIN.search(x[0].get("name", "")) else 1)   # 용량이 넘치면 공고문·과업 문서부터
    title = it.get("title", "")
    zpath = out / "bundle" / (re.sub(r'[\/:*?"<>|\[\]()]+', " ", f"첨부{len(got)}개 {title}").strip()[:40].strip() + ".zip")
    names, total, used = [], 0, set()
    zpath.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for att, p in got:
            size = p.stat().st_size
            if total + size > MAX_ZIP:
                missed.append(att)
                continue
            arc = att.get("name") or p.name
            while arc in used:
                arc = "_" + arc
            used.add(arc)
            z.write(p, arc)
            names.append(short_name(arc, title))
            total += size
    if not names:
        zpath.unlink()
        return None, [], missed
    if len(names) < len(got):   # 일부만 들어갔으면 zip 이름의 개수를 맞춘다
        zpath = zpath.rename(zpath.with_name(zpath.name.replace(f"첨부{len(got)}개", f"첨부{len(names)}개", 1)))
    return zpath, names, missed


def is_repost(it: dict, r: dict) -> bool:
    """이미 적합으로 보낸 공고가 재공고·정정공고로 다시 올라와 또 적합인 경우 → 카드 대신 한 줄."""
    return (it.get("repost_of") or {}).get("verdict") == "적합" and r.get("verdict") == "적합"


def short_date(s: str) -> str:
    d = end_date(s)
    return f"{d.month}/{d.day}" if d else s


def card_text(it: dict, r: dict, meta: str) -> str:
    """적합 공고 한 건 = 메시지 블록 하나: 제목 링크, 마감·금액·기관, 만들 것."""
    out = [f"🟢 **[{trunc(it.get('title', '?'), 90)}]({it['ntis_url']})**", meta]
    card = r.get("card") or {}
    for key, label in CARD_FIELDS:
        v = card.get(key)
        if isinstance(v, list):
            v = " · ".join(x for x in v if x)
        if v:
            out.append(f"> {label}: {trunc(v, 220)}")
    return "\n".join(out)


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
    ap.add_argument("--send", default="적합", help="링크로 보낼 판정 (콤마 구분). 기본: 적합 — 조건부·부적합은 판정별 txt 파일로 첨부")
    ap.add_argument("--format", default="links", choices=["links", "embed"], help="links: 공고당 한 줄 링크(기본) / embed: 상세 embed + 첨부")
    ap.add_argument("--no-rejected", action="store_true", help="부적합 목록 파일 첨부 생략")
    ap.add_argument("--send-empty", action="store_true", help="판정 0건이어도 헤더 발송. 기본은 발송 생략")
    ap.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", ""))
    ap.add_argument("--title", default="NTIS 국가R&D통합공고 일일 리포트", help="헤더 제목")
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
    reposts = [r for r in picked if is_repost(items.get(str(r["uid"]), {}), r)]
    picked = [r for r in picked if r not in reposts]
    n_fit = sum(1 for r in judged if r.get("verdict") == "적합")
    n_cond = sum(1 for r in judged if r.get("verdict") == "조건부")
    rejected = [r for r in judged if r.get("verdict") == "부적합"]
    unjudged = [u for u in items if u not in results]

    # 링크로도 파일로도 보낼 게 없으면 아무것도 보내지 않는다 (일일 실행이라 빈 리포트가 채널을 덮지 않도록).
    # 적합이 0건이어도 조건부·부적합이 있으면 헤더(적합 0건) + 판정별 txt 파일은 보낸다.
    attachable = [r for r in judged if r.get("verdict") not in send_set
                  and not (r.get("verdict") == "부적합" and a.no_rejected)]
    if not picked and not reposts and not attachable and not a.send_empty:
        log(f"발송 생략: 판정 0건 (수집 {len(items)}건)")
        return 0

    # 1) 헤더 — 한 줄로 짧게 (날짜·전체 건수는 daily_summary.py의 요약 줄이 담당)
    head = f"**{a.title}** — 적합 {len(picked)}건" + (f" (+ 이미 보낸 공고 {len(reposts)}건)" if reposts else "")

    # 2-a) links 형식: 공고당 한 줄 (제목 링크 · 마감 · 금액), 2000자 단위로 분할 전송
    if a.format == "links":
        # 남은 일수가 많은 공고가 위로 오도록 정렬 (마감일 없는 건은 맨 아래)
        def days_left(r: dict) -> int:
            it = items.get(str(r["uid"]), {})
            d = end_date(it.get("end", ""))
            return (d - today_kst()).days if d else -10_000
        picked.sort(key=days_left, reverse=True)

        def meta_line(it: dict, r: dict) -> str:
            budget = r.get("budget") or it.get("budget") or ""   # Claude가 공고문에서 읽은 예산 우선
            agency = trunc(it.get("agency") or it.get("dept") or "-", 30)
            if it.get("source") == "g2b_pre":   # 사전규격: 입찰 마감이 아직 없다
                return " / ".join([f"**사전규격 · 본공고 전**", f"의견등록 마감 {short_date(it.get('opinion_end', '')) or '-'}",
                                   trunc(budget, 40) or "금액 미정", agency, f"등록번호 {it['uid']}"])
            d = end_date(it.get("end", ""))
            n = (d - today_kst()).days if d else None
            dday = ("D-Day" if n == 0 else f"D-{n}" if n is not None and n > 0 else f"마감({-n}일 경과)" if n is not None else "마감 미정")
            end_txt = d.strftime("%Y.%m.%d") if d else "-"
            # 출력 순서: 디데이 / 마감일자 / 금액 / 공고업체
            return " / ".join([f"**{dday}**", end_txt, trunc(budget, 40) or "금액 미정", agency])

        lines: list[str | tuple[str, list[Path]]] = []   # 공고 첨부를 붙일 블록은 (텍스트, 파일)
        for r in picked:
            it = items.get(str(r["uid"]))
            if not it:
                continue
            if r.get("card"):
                txt = card_text(it, r, meta_line(it, r))
            else:   # 카드가 없는 판정(이전 형식)은 제목 링크 + 한 줄
                txt = f"• [{trunc(it.get('title','?'), 90)}]({it['ntis_url']})\n　{meta_line(it, r)}"
            zpath, names, missed = (None, [], []) if a.no_files else bundle_docs(it, out)
            if names:   # Discord는 긴 파일명을 잘라 보이므로 zip 안의 파일을 짧은 이름으로 적는다 (같은 문서의 HWP·PDF는 한 줄)
                kinds: dict[str, list[str]] = {}
                for n in names:
                    stem, ext = os.path.splitext(n)
                    kinds.setdefault(stem, []).append(ext.lstrip(".") or "?")
                txt += f"\n> 📎 첨부 {len(names)}개 (zip): " + trunc(" · ".join(f"{s} ({'·'.join(e)})" for s, e in kinds.items()), 400)
            if missed:
                txt += "\n> 📎 못 담은 파일은 링크로: " + " · ".join(
                    f"[{trunc(x['name'], 40)}]({x['url']})" if x.get("url") else trunc(x["name"], 40) for x in missed[:5])
            lines.append((txt, [zpath]) if zpath else txt)
        # 이미 보낸 공고: 사전규격 → 본공고(입찰 마감 확정)와 재공고·정정공고를 나눠 한 줄씩
        for from_pre, header in ((True, "📢 **본공고 올라옴** — 사전규격으로 먼저 보낸 공고 (입찰 마감 확정, 바뀐 점만 확인)"),
                                 (False, "🔁 **다시 올라온 공고** — 이미 적합으로 보낸 공고의 재공고·정정공고 (바뀐 점만 확인)")):
            group = [r for r in reposts if (items[str(r["uid"])]["repost_of"].get("source") == "g2b_pre") == from_pre]
            if not group:
                continue
            lines.append(header)   # 한 줄씩 넣어야 2000자 분할이 공고 단위로 된다
            for r in group:
                it = items[str(r["uid"])]
                changes = r.get("changes") or ""
                lines.append(f"• [{trunc(it.get('title','?'), 80)}]({it['ntis_url']}) — {short_date(it['repost_of'].get('judged_at', ''))} 발송분\n"
                             f"　{meta_line(it, r)}" + (f"\n　변경: {trunc(changes, 160)}" if changes else ""))
        # 링크로 보내지 않은 판정(조건부·부적합)은 메시지에 줄을 차지하지 않도록 판정별 텍스트 파일로 만들어
        # 마지막 메시지에 첨부 (클릭하면 Discord 안에서 열림)
        files: list[Path] = []
        for verdict in ("조건부", "부적합"):
            rs = [r for r in judged if r.get("verdict") == verdict and verdict not in send_set]
            if not rs or (verdict == "부적합" and a.no_rejected):
                continue
            rl = [f"{verdict} {len(rs)}건 — {a.title} ({date_from} ~ {date_to})", ""]
            for n, r in enumerate(rs, 1):
                it = items.get(str(r["uid"]), {})
                rl += [f"{n}. {it.get('title', '?')}", f"   사유: {r.get('reason', '')}"]
                if verdict == "조건부":
                    rl += [f"   확인할 것: {'; '.join(r.get('conditions') or [])}", f"   제안 액션: {r.get('action', '')}",
                           f"   마감: {it.get('end', '')} | 금액: {r.get('budget') or it.get('budget') or '-'}"]
                rl += [f"   링크: {it.get('ntis_url', '')}", ""]
            f = out / f"{verdict}_{date_from}_{date_to}.txt"
            f.write_text("\n".join(rl), "utf-8")
            files.append(f)
        # 2000자 단위로 나눠 보내되, 공고 첨부가 있는 카드는 그 카드에서 메시지를 끊어 파일이 카드 바로 아래 붙게 한다
        chunks: list[tuple[str, list[Path]]] = []
        buf = head
        for ln in lines:
            txt, docs = ln if isinstance(ln, tuple) else (ln, [])
            if len(buf) + len(txt) + 2 > 1900:
                chunks.append((buf, []))
                buf = ""
            buf += ("\n\n" + txt) if buf else txt
            if docs:
                chunks.append((buf, docs))
                buf = ""
        if buf or not chunks:
            chunks.append((buf, []))
        if files:   # 조건부·부적합 txt는 마지막 메시지에
            c, docs = chunks[-1]
            chunks[-1] = (c, docs + files)
        for c, fs in chunks:
            send(a.webhook, {"content": c, "allowed_mentions": {"parse": []}}, fs or None, dry=a.dry_run)
        log(f"완료: 적합 {len(picked)}건 + 재공고 {len(reposts)}건 발송 (첨부파일: 조건부 {n_cond}, 부적합 {len(rejected)})")
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
    if rejected and not a.no_rejected:
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
