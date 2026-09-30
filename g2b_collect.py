#!/usr/bin/env python3
"""
나라장터(G2B) 입찰공고 수집기 — 공공데이터포털 "조달청_나라장터 입찰공고정보서비스" Open API 사용.
  https://www.data.go.kr/data/15129394/openapi.do  (무료, 자동승인, 일 1,000회)

지정 기간(기본: 지난주 월~일)에 게시된 용역/물품 입찰공고를 받아 g2b_keywords.txt 의
포함/제외 키워드로 1차 필터링하고, NTIS 수집기와 같은 형식(announcements.json / digest.md / brief/)으로 저장한다.

환경변수:
  G2B_SERVICE_KEY   공공데이터포털 일반 인증키 (Decoding 키)

사용:
  python g2b_collect.py --out out/g2b
  python g2b_collect.py --from 2026-09-07 --to 2026-09-13 --out out/g2b
  python g2b_collect.py --types servc,thng      # 용역+물품 (기본: 용역만)
  python g2b_collect.py --no-filter             # 키워드 필터 없이 전부 저장(테스트)
"""
from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

KST = ZoneInfo("Asia/Seoul")


def now_kst() -> dt.datetime:
    return dt.datetime.now(KST)

from ntis_collect import FAILED_TEXT, TEXT_EXTS, attachment_docs, attachment_sections, extract_attachment, make_brief
from seen_state import seen_uids

BASE = "http://apis.data.go.kr/1230000/ad/BidPublicInfoService"
OPS = {
    "servc": "getBidPblancListInfoServcPPSSrch",   # 용역
    "thng": "getBidPblancListInfoThngPPSSrch",     # 물품
    "cnstwk": "getBidPblancListInfoCnstwkPPSSrch", # 공사
}
TYPE_LABEL = {"servc": "용역", "thng": "물품", "cnstwk": "공사"}
G2B_DETAIL = "https://www.g2b.go.kr:8101/ep/invitation/publish/bidInfoDtl.do?bidno={no}&bidseq={ord}"
NUM_ROWS = 999
session = requests.Session()
session.headers.update({"User-Agent": "X2R-G2B-collector/1.0"})


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- keywords
KW_SECTIONS = ("strong", "include", "weak", "exclude", "class_include", "class_exclude")


def load_keywords(path: Path) -> dict[str, list[str]]:
    """[strong] 단독 통과 + 제목 제외어 무시, [include] 단독 통과, [weak] 다른 키워드와 함께일 때만 통과,
    [exclude] 하나라도 있으면 제외, [class_include]/[class_exclude] 공공조달분류(중분류·세분류)로 무조건 통과/제외."""
    kw: dict[str, list[str]] = {s: [] for s in KW_SECTIONS}
    if not path.exists():
        return kw
    section = "include"
    for ln in path.read_text("utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"\[(" + "|".join(KW_SECTIONS) + r")\]", ln, re.I)
        if m:
            section = m.group(1).lower()
            continue
        kw[section].append(ln)
    return kw


def _kw_regex(k: str) -> re.Pattern:
    # 짧은 영문 약어(AI, AR, VR, XR, MR, DX, 3D…)는 영문자 사이에 끼면 매칭하지 않음 (training, software 오탐 방지)
    if re.fullmatch(r"[A-Za-z0-9\-]{1,4}", k):
        return re.compile(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])", re.I)
    return re.compile(re.escape(k), re.I)


def match_keywords(text: str, kw: dict[str, list[str]], classes: tuple[str, ...] = ()) -> tuple[list[str], list[str], bool]:
    """반환: (매칭된 키워드, 제외 사유, 통과 여부). classes: 공고의 공공조달분류명(중분류·세분류)."""
    cls = {c.strip() for c in classes if c and c.strip()}
    cls_bad = [c for c in kw.get("class_exclude", []) if c in cls]
    if cls_bad:
        return [], [f"분류:{c}" for c in cls_bad], False
    cls_ok = [f"분류:{c}" for c in kw.get("class_include", []) if c in cls]
    strong = [k for k in kw.get("strong", []) if _kw_regex(k).search(text)]
    inc = [k for k in kw["include"] if _kw_regex(k).search(text)]
    weak = [k for k in kw["weak"] if _kw_regex(k).search(text)]
    bad = [] if (strong or cls_ok) else [k for k in kw["exclude"] if _kw_regex(k).search(text)]
    ok = bool(cls_ok or strong or inc) or len(weak) >= 2
    return cls_ok + strong + inc + weak, bad, ok and not bad


# --------------------------------------------------------------------------- api
def fetch_type(key: str, typ: str, date_from: str, date_to: str) -> list[dict]:
    """게시일시 기준(inqryDiv=1) 기간 내 공고 전체 페이지 수집."""
    url = f"{BASE}/{OPS[typ]}"
    bgn = date_from.replace("-", "") + "0000"
    end = date_to.replace("-", "") + "2359"
    items: list[dict] = []
    page = 1
    while True:
        params = {
            "serviceKey": key,
            "pageNo": page,
            "numOfRows": NUM_ROWS,
            "inqryDiv": 1,
            "inqryBgnDt": bgn,
            "inqryEndDt": end,
            "type": "json",
        }
        last_err = None
        for attempt in range(3):
            try:
                r = session.get(url, params=params, timeout=60)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                log(f"  ! {typ} p{page} 요청 실패({attempt + 1}/3): {e}  body={getattr(r, 'text', '')[:200]!r}")
                time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"G2B API 실패: {last_err}")
        resp = data.get("response", data)
        header = resp.get("header", {})
        if str(header.get("resultCode", "00")) not in ("00", "0"):
            raise RuntimeError(f"G2B API 오류: {header}")
        body = resp.get("body", {})
        rows = body.get("items") or []
        if isinstance(rows, dict):
            rows = rows.get("item") or []
        if isinstance(rows, dict):
            rows = [rows]
        total = int(body.get("totalCount") or 0)
        items.extend(rows)
        log(f"[{TYPE_LABEL[typ]}] {page}페이지 {len(rows)}건 (누적 {len(items)}/{total})")
        if not rows or len(items) >= total:
            break
        page += 1
        time.sleep(0.3)
    return items


def g(item: dict, *keys: str) -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, "", "null"):
            return str(v).strip()
    return ""


def won(v: str) -> str:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return v or ""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}억원"
    if n >= 10_000:
        return f"{n // 10_000:,}만원"
    return f"{n:,}원"


def normalize(item: dict, typ: str) -> dict:
    no, ord_ = g(item, "bidNtceNo"), g(item, "bidNtceOrd") or "00"
    atts = []
    for i in range(1, 11):
        u = g(item, f"ntceSpecDocUrl{i}")
        if u:
            atts.append({"name": g(item, f"ntceSpecFileNm{i}") or f"공고문{i}", "url": u})
    detail = g(item, "bidNtceDtlUrl") or G2B_DETAIL.format(no=no, ord=ord_)
    d = {
        "uid": f"{no}-{ord_}",
        "source": "g2b",
        "type": TYPE_LABEL[typ],
        "title": g(item, "bidNtceNm"),
        "dept": g(item, "dminsttNm"),                       # 수요기관
        "agency": g(item, "ntceInsttNm"),                   # 공고기관
        "form": g(item, "ntceKindNm"),                      # 일반/긴급/재공고 등
        "method": g(item, "cntrctCnclsMthdNm"),             # 계약방법
        "bid_method": g(item, "bidMethdNm"),
        "announce_date": g(item, "bidNtceDt")[:16],
        "start": g(item, "bidBeginDt")[:16],
        "end": g(item, "bidClseDt")[:16],
        "open_date": g(item, "opengDt")[:16],
        "budget": " / ".join(x for x in [
            f"기초금액 {won(g(item, 'presmptPrce'))}" if g(item, "presmptPrce") else "",
            f"배정예산 {won(g(item, 'asignBdgtAmt'))}" if g(item, "asignBdgtAmt") else "",
        ] if x),
        "presmpt_price": g(item, "presmptPrce"),
        "region_limit": g(item, "rgnLmtBidLocplcJdgmBssNm", "cmmnSpldmdCorpRgn", "prtcptPsblRgnNm"),
        "industry_limit": g(item, "indstrytyLmtYn"),
        "sme_only": g(item, "bidPrtcptLmtYn"),
        "contact": " ".join(x for x in [g(item, "ntceInsttOfclNm"), g(item, "ntceInsttOfclTelNo"), g(item, "ntceInsttOfclEmailAdrs")] if x),
        "ntis_url": detail,           # discord_post.py 호환 필드명 (상세 링크)
        "source_url": g(item, "bidNtceUrl"),
        "attachments": atts,
        "body": "",
        "raw": item,
    }
    return d


QUAL_HIGH = re.compile(r"(직접생산|소프트웨어사업자|정보통신공사업|업종\s*코드|업종코드|면허|실적|소재지|지역\s*제한|지역제한|등록한 자|소지한 자|중소기업확인서|중소기업\s*확인서|공동수급|공동계약|자격요건|자격 요건)")
QUAL_GENERIC = re.compile(r"(입찰참가자격|참가자격|참가 자격|입찰에 참가할 수 있는 자|제한사항|참가제한|참여제한|참여자격)")


# 과업 설명 문서: 기관마다 이름이 달라 넓게 잡는다 (2026-09-30, 2주치 4,133건 첨부파일명 점검 — "제안 요청서" 띄어쓰기, 사양서, 사업설명서 등)
SPEC_DOC = re.compile(r"제안\s*요청|과업|RFP|시방|규격|사양서|(용역|사업|제안)\s*(설명|안내)서|작업\s*지시|지침서|요구\s*사항|발주\s*도서", re.I)
DOC_MAIN = re.compile(r"공고|" + SPEC_DOC.pattern, re.I)
DOC_BOILERPLATE = re.compile(r"서약|계약서|일반조건|특수조건|동의서|유의사항|개인정보|양식|서식|위임장|확약|신청서")


def download_notice_docs(d: dict, dest_dir: Path, max_files: int = 6) -> None:
    """입찰공고서·제안요청서·과업지시서(PDF/HWP/HWPX/ZIP 등) 최대 6개를 내려받아 텍스트 추출.
    공고·과업 문서를 우선하고 서약서·계약조건 같은 공통 서식은 받지 않으며, 같은 문서의 HWP/PDF 중복은 하나만."""
    cands = [a for a in d["attachments"] if Path(a["name"]).suffix.lower() in TEXT_EXTS | {".zip"}
             and (DOC_MAIN.search(a["name"]) or not DOC_BOILERPLATE.search(a["name"]))]
    # 공고서(참가자격)·과업 설명 문서(과업 내용) 우선. 둘은 같은 순위 — 한쪽만 받으면 판정이 안 된다
    cands.sort(key=lambda a: (0 if DOC_MAIN.search(a["name"]) else 1, 0 if a["name"].lower().endswith(".pdf") else 1))
    picked, stems = [], set()
    for a in cands:
        stem = Path(a["name"]).stem
        if stem not in stems:
            stems.add(stem)
            picked.append(a)
    for a in picked[:max_files]:
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r'[\/:*?"<>|]+', "_", a["name"])[:150]
            path = dest_dir / safe
            if not path.exists():
                r = session.get(a["url"], timeout=120, stream=True)
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        if f.tell() > 30 * 1024 * 1024:
                            break
            a["path"] = str(path)
            extract_attachment(a, path)
        except Exception as e:  # noqa: BLE001
            log(f"  ! 공고서 다운로드/추출 실패 {a['name']}: {e}")
        time.sleep(0.3)


def _windows(text: str, rx: re.Pattern, before: int, after: int) -> list[list[int]]:
    spans: list[list[int]] = []
    for m in rx.finditer(text):
        a, b = max(0, m.start() - before), min(len(text), m.end() + after)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def qual_excerpt(text: str, limit: int = 3500) -> str:
    """공고서 텍스트에서 참가자격 관련 구간만 발췌. 구체 자격 용어(직접생산·업종·면허·실적·소재지) 주변을 우선."""
    if not text:
        return ""
    high = _windows(text, QUAL_HIGH, 400, 900)
    generic = _windows(text, QUAL_GENERIC, 100, 700)
    picked: list[list[int]] = []
    total = 0
    for a, b in high + generic:
        if any(a < pb and b > pa for pa, pb in picked):   # 이미 포함된 구간 겹치면 생략
            continue
        picked.append([a, b])
        total += b - a
        if total > limit:
            break
    picked.sort()
    return "\n\n[…]\n\n".join(text[a:b].strip() for a, b in picked)


def g2b_excerpt(text: str) -> str:
    """참가자격 구간 (문서 앞부분은 brief 맨 위 '과업 개요'에 따로 둔다)."""
    return qual_excerpt(text) or make_brief(text, head=0)[:3000]


# 우선순위: 과업 내용·범위(무엇을 만드는지) > 사업 개요·목적(왜 하는지)
OVERVIEW_HEADS = [
    re.compile(r"(과업|사업|용역|연구)\s*(내용|범위)|주요\s*과업|세부\s*과업|요구\s*사항"),
    re.compile(r"(사업|과업|용역|연구)\s*개요|(사업|연구)\s*목적|추진\s*배경"),
]
NOISE_LINE = re.compile(r"^(그림입니다|원본 그림의|[\s|·.\-─]*$)")
TOC_LINE = re.compile(r"(\.{2,}|·{2,}|…|\s)\s*\d{1,3}\s*$")


NOTICE_START = re.compile(r"입찰에\s*부치는\s*사항|용\s*역\s*명|공\s*고\s*명|사\s*업\s*명")


def _is_toc(t: str, pos: int) -> bool:
    """제목 뒤 몇 줄이 '… 12'처럼 쪽번호로 끝나거나, 대부분 짧은 제목 줄이면 목차."""
    lines = [ln.strip() for ln in t[pos:pos + 500].splitlines() if ln.strip()][:7]
    if "· · · ·" in t[pos:pos + 300] or sum(bool(TOC_LINE.search(ln)) for ln in lines) >= 3:
        return True
    # '1. 과업 대상 / 2. 과업 범위 / 3. …'처럼 번호 붙은 짧은 줄이 이어지면 목차 (표 칸의 짧은 줄은 번호가 없어 제외)
    return sum(bool(re.match(r"(\d{1,2}|[ⅠⅡⅢⅣⅤⅥⅦ]|[가-하])[.)]", ln)) and len(ln) <= 30 for ln in lines) >= 4


def _is_heading(t: str, pos: int) -> bool:
    """본문 문장 속 단어('과업내용의 현저한 변경…')가 아니라 제목인지.
    '4. 과업 내용', 'Ⅱ. 사업 개요', '□ 과업범위'처럼 번호·기호가 앞에 붙었거나(PDF는 줄바꿈 없이 이어짐), 줄 머리의 짧은 줄."""
    if re.search(r"(\d{1,2}|[ⅠⅡⅢⅣⅤⅥⅦ]|[가-하])[.)]\s*$|[□■○◦❍▣◎●]\s*$", t[max(0, pos - 6):pos]):
        return True
    ls = t.rfind("\n", 0, pos) + 1
    le = t.find("\n", pos)
    line = t[ls:le if le > 0 else len(t)]
    return pos - ls <= 12 and len(line.strip()) <= 40


TASK_TERMS = re.compile(r"산출물|납품|요구\s*사항|기능|구축|개발|제작|콘텐츠|시스템|수량|구성|화면|모듈|시나리오|데이터|설치|장비|플랫폼|영상|교육|범위")
BOILER_TERMS = re.compile(r"청렴계약|입찰참가자격|참가자격|낙찰자|개찰|계약보증|입찰보증|전자입찰|부정당|공동수급|하도급|지체상금|입찰서|가격입찰|적격심사")


def _task_score(t: str, pos: int) -> tuple[int, int]:
    """제목 위치부터 2,500자 구간의 (과업 단어 수, 공고 공통 문구 수)."""
    w = t[pos:pos + 2500]
    return len(TASK_TERMS.findall(w)), len(BOILER_TERMS.findall(w))


def task_overview(atts: list[dict], limit: int = 2000) -> tuple[str, str, bool]:
    """첨부 문서 '내용'에서 과업 내용·범위 구간을 찾는다 (파일명은 보지 않음). 1단계 방향·명확성 판정용.
    반환: (문서명, 발췌, 과업 내용 확인 여부).
    모든 문서에서 '과업 내용/범위/요구사항'(없으면 '사업 개요/목적') 제목을 찾아, 제목 뒤 구간에 과업 단어가 많고
    공고 공통 문구(청렴계약·입찰참가자격·개찰…)가 적은 곳을 고른다. 목차·계약조항 속 단어는 제목으로 치지 않는다.
    못 찾으면 입찰공고서의 '입찰에 부치는 사항/용역명'부터 보여주고 확인 여부 False."""
    docs = [(label, t) for a in atts for label, t, form in attachment_docs(a) if not form and not FAILED_TEXT.match(t)]
    best = None   # (점수, 문서명, 텍스트, 위치)
    for label, t in docs:
        for bonus, rx in ((5, OVERVIEW_HEADS[0]), (0, OVERVIEW_HEADS[1])):
            for m in rx.finditer(t):
                if not _is_heading(t, m.start()) or _is_toc(t, m.start()):
                    continue
                task, boiler = _task_score(t, m.start())
                if task < 6 or task <= 2 * boiler:   # 과업 단어가 적거나 공고 문구가 섞인 구간은 과업 설명이 아님
                    continue
                score = task - 2 * boiler + bonus
                if best is None or score > best[0]:
                    best = (score, label, t, m.start())
    found = best is not None
    if found:
        _, label, t, start = best
    elif docs:
        label, t = next(((lb, tx) for lb, tx in docs if NOTICE_START.search(tx)), docs[0])
        m = NOTICE_START.search(t)
        start = m.start() if m else 0
    else:
        return "", "", False
    lines = [ln.strip() for ln in t[start:start + limit * 2].splitlines()]
    body = "\n".join(ln for ln in lines if not NOISE_LINE.match(ln))
    return label, body[:limit].strip(), found


def dday(end: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", end or "")
    if not m:
        return ""
    n = (dt.date(int(m[1]), int(m[2]), int(m[3])) - now_kst().date()).days
    return f"D-{n}" if n >= 0 else f"마감({-n}일 경과)"


def default_range(days: int | None) -> tuple[str, str]:
    today = now_kst().date()
    if days:
        return (today - dt.timedelta(days=days - 1)).isoformat(), today.isoformat()
    this_mon = today - dt.timedelta(days=today.weekday())
    return (this_mon - dt.timedelta(days=7)).isoformat(), (this_mon - dt.timedelta(days=1)).isoformat()


# --------------------------------------------------------------------------- output
def write_outputs(items: list[dict], out: Path, date_from: str, date_to: str, n_total: int) -> None:
    (out / "brief").mkdir(parents=True, exist_ok=True)
    (out / "text").mkdir(parents=True, exist_ok=True)
    (out / "announcements.json").write_text(
        json.dumps({"range": [date_from, date_to], "source": "g2b", "total_before_filter": n_total,
                    "collected_at": now_kst().isoformat(), "items": items}, ensure_ascii=False, indent=1),
        "utf-8",
    )
    lines = [f"# 나라장터 입찰공고 수집 결과 ({date_from} ~ {date_to})", "",
             f"전체 {n_total}건 중 키워드 필터 통과 {len(items)}건", ""]
    for it in items:
        lines += [
            f"## [{it['uid']}] ({it['type']}) {it['title']}",
            f"- 수요기관/공고기관: {it['dept']} / {it['agency']}",
            f"- 공고종류: {it['form']} | 계약방법: {it['method']} | 입찰방식: {it['bid_method']}",
            f"- 게시 {it['announce_date']} | 입찰마감 {it['end']} ({it['dday']}) | 개찰 {it['open_date']}",
            f"- 금액: {it['budget'] or '-'} | 지역제한: {it['region_limit'] or '-'}",
            f"- 매칭 키워드: {', '.join(it['keywords'])}",
            f"- 상세: {it['ntis_url']}",
            f"- 첨부({len(it['attachments'])}): " + ", ".join(a["name"] for a in it["attachments"]),
            f"- 요약본: brief/{it['uid']}.md",
            "",
        ]
    (out / "digest.md").write_text("\n".join(lines), "utf-8")
    for it in items:
        parts = [
            f"# ({it['type']}) {it['title']}", "",
            f"- 공고번호: {it['uid']}",
            f"- 수요기관/공고기관: {it['dept']} / {it['agency']}",
            f"- 공고종류: {it['form']} | 계약방법: {it['method']} | 입찰방식: {it['bid_method']}",
            f"- 게시 {it['announce_date']} | 입찰 {it['start']} ~ {it['end']} | 개찰 {it['open_date']}",
            f"- 금액: {it['budget'] or '-'}",
            f"- 지역제한: {it['region_limit'] or '-'} | 업종제한: {it['industry_limit'] or '-'} | 참가제한: {it['sme_only'] or '-'}",
            f"- 담당: {it['contact'] or '-'}",
            f"- 상세: {it['ntis_url']}",
            f"- 첨부: " + (", ".join(a["name"] for a in it["attachments"]) or "-"),
        ]
        # API 원본 필드는 brief의 절반 이상을 차지하지만 판정에 쓰는 건 몇 개뿐 → brief엔 요약 한 줄, 전체는 text/에만
        r = it["raw"]
        cls = " > ".join(x.strip() for x in [r.get("pubPrcrmntLrgClsfcNm"), r.get("pubPrcrmntMidClsfcNm"), r.get("pubPrcrmntClsfcNm")] if x and x.strip())
        parts.append("- " + " | ".join(x for x in [
            f"공공조달분류: {cls or '-'}",
            f"용역구분: {r['srvceDivNm']}" if r.get("srvceDivNm") else "",
            f"낙찰방법: {r['sucsfbidMthdNm']}" if r.get("sucsfbidMthdNm") else "",
            f"공동수급: {r['cmmnSpldmdMethdNm']}" if r.get("cmmnSpldmdMethdNm") else "",
            f"지역제한 판단기준: {r['rgnLmtBidLocplcJdgmBssNm']}" if r.get("rgnLmtBidLocplcJdgmBssNm") else "",
            f"구매대상물품: {r['purchsObjPrdctList']}" if r.get("purchsObjPrdctList") else "",
        ] if x))
        parts.append("")
        raw_lines = ["## 원본 필드", "",
                     "\n".join(f"- {k}: {v}" for k, v in r.items() if v not in (None, "", "null") and not k.startswith("ntceSpec")), ""]
        att_brief, att_text = attachment_sections(it["attachments"], excerpt=g2b_excerpt, heading="입찰공고서 참가자격 발췌")
        ov_name, ov, found = task_overview(it["attachments"])
        # 내려받았는데 텍스트를 (거의) 못 뽑은 첨부 (스캔 PDF·이미지로 된 HWPX 등)
        unread = [a["name"] for a in it["attachments"]
                  if a.get("path") and sum(len(t) for _, t, _ in attachment_docs(a)) < 300]
        if found:
            overview = [f"## 과업 개요 ({ov_name})", "", ov, ""]
        elif unread:   # 과업 내용이 못 읽은 첨부에 있을 수 있음 → 사람이 열어보면 되므로 조건부 (6-3)
            overview = [f"## 과업 개요 — ⚠ 과업 내용을 찾지 못함, 읽지 못한 첨부 있음 ({', '.join(unread)})", "", ov or "(텍스트 없음)", ""]
        else:   # 첨부 어디에도 과업 내용·범위가 없음 (공고서 문구뿐) → company_profile.md 6-2 (b): 부적합
            where = f", {ov_name} 앞부분" if ov_name else ""
            overview = [f"## 과업 개요 — ⚠ 과업 내용 없음 (첨부 문서에서 과업 내용·범위를 찾지 못함{where})", "", ov or "(첨부 텍스트 없음)", ""]
        (out / "brief" / f"{it['uid']}.md").write_text("\n".join(parts + overview + att_brief), "utf-8")
        (out / "text" / f"{it['uid']}.md").write_text("\n".join(parts + raw_lines + att_text), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--days", type=int)
    ap.add_argument("--out", default="out/g2b")
    ap.add_argument("--types", default="servc", help="servc,thng,cnstwk 중 콤마 구분")
    ap.add_argument("--keywords", default="g2b_keywords.txt")
    ap.add_argument("--no-filter", action="store_true")
    ap.add_argument("--max-price", type=float, default=3_000_000_000, help="기초금액 상한(원). 초과 공고 제외")
    ap.add_argument("--no-seen", action="store_true", help="state/seen.json 무시(이미 판정한 공고도 수집)")
    ap.add_argument("--include-closed", action="store_true", help="입찰마감 지난 공고도 수집")
    ap.add_argument("--no-attach", action="store_true", help="입찰공고서 다운로드/추출 생략")
    ap.add_argument("--key", default=os.environ.get("G2B_SERVICE_KEY", ""))
    a = ap.parse_args()

    if not a.key:
        log("G2B_SERVICE_KEY 환경변수(공공데이터포털 인증키)가 필요합니다.")
        return 2
    # 공공데이터포털 "Encoding" 키(%2B, %3D 포함)를 넣어도 requests가 다시 인코딩하지 않도록 디코딩해 둔다
    if "%" in a.key:
        a.key = urllib.parse.unquote(a.key)
    date_from, date_to = (a.date_from, a.date_to) if a.date_from and a.date_to else default_range(a.days)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    kw = load_keywords(Path(a.keywords))
    log(f"수집 기간: {date_from} ~ {date_to}  유형: {a.types}  키워드 {len(kw['include'])}+약 {len(kw['weak'])}/제외 {len(kw['exclude'])} -> {out.resolve()}")

    raw_all: list[tuple[str, dict]] = []
    for typ in [t.strip() for t in a.types.split(",") if t.strip()]:
        if typ not in OPS:
            log(f"  ! 알 수 없는 유형 {typ}")
            continue
        for it in fetch_type(a.key, typ, date_from, date_to):
            raw_all.append((typ, it))

    # 같은 공고번호는 최신 차수(재공고/변경공고)만 남긴다
    by_no: dict[str, dict] = {}
    for typ, it in raw_all:
        d = normalize(it, typ)
        no = d["uid"].rsplit("-", 1)[0]
        if no not in by_no or d["uid"] > by_no[no]["uid"]:
            by_no[no] = d
    seen = set() if a.no_seen else seen_uids("g2b")
    items, n_seen, n_closed = [], 0, 0
    for d in by_no.values():
        if "취소" in d["form"]:   # 취소공고 제외
            continue
        if d["uid"] in seen or d["uid"].rsplit("-", 1)[0] in {u.rsplit("-", 1)[0] for u in seen}:
            n_seen += 1
            continue
        if not a.include_closed:
            end = d["end"] or d["open_date"]
            if end and end < now_kst().strftime("%Y-%m-%d %H:%M"):
                n_closed += 1
                continue
        raw = d["raw"]
        hit, bad, ok = match_keywords(d["title"], kw, (raw.get("pubPrcrmntMidClsfcNm") or "", raw.get("pubPrcrmntClsfcNm") or ""))
        if not a.no_filter and not ok:
            continue
        try:
            if a.max_price and float(d["presmpt_price"] or 0) > a.max_price:
                continue
        except ValueError:
            pass
        d["keywords"] = hit
        if not d["end"]:
            d["end"] = d["open_date"]
        d["dday"] = dday(d["end"])
        items.append(d)
    items.sort(key=lambda x: x["end"])
    log(f"이미 판정한 공고 제외 {n_seen}건, 마감 지난 공고 제외 {n_closed}건")
    if not a.no_attach:
        for i, d in enumerate(items, 1):
            log(f"[{i}/{len(items)}] 공고서 수집 {d['uid']} {d['title'][:40]}")
            download_notice_docs(d, out / "attachments" / d["uid"])
    write_outputs(items, out, date_from, date_to, len(raw_all))
    log(f"완료: 전체 {len(raw_all)}건 → 필터 통과 {len(items)}건 -> {out / 'digest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
