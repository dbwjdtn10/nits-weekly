# 정부 R&D·입찰·지원사업 공고 → X2R 적합성 판정 → Discord 일일 리포트

평일 매일 아침, 최근 4일간 NTIS·나라장터·충북과기혁신원·충북 지역혁신클러스터·중기부·기업마당에 등록된 공고를 수집하고 (이미 판정한 공고는 제외),
`company_profile.md` 기준으로 X2R이 제안 가능한지 판정해 Discord 채널로 발송한다.

```
┌ Claude Code 클라우드 routine (평일 09:00 KST, cron 0 0 * * 1-5 UTC) ┐
│ 1. ntis / g2b / cbist / boards(riia·mss·bizinfo) → out/*/brief/<uid>.md │
│ 2. Claude가 company_profile.md 기준으로 판정 → out/results.json       │
│ 3. python discord_post.py   → Discord 웹훅 (적합·조건부 링크 목록만)    │
│ 4. python seen_state.py mark → state/seen.json 커밋 (재판정 방지)       │
└─────────────────────────────────────────────────────────────────────────┘
```

## 파일
| 파일 | 역할 |
|---|---|
| `ntis_collect.py` | 목록(POST 날짜검색) → 상세(view.do) → 첨부 다운로드(download.do) → PDF/HWP/HWPX/DOCX/XLSX 텍스트 추출(ZIP은 풀어서 내부 문서까지) → `brief/`(자격·대상·규모·기간 구간 발췌, 양식·법령·매뉴얼류 제외, 읽지 못한 첨부 표시) 생성. 로그인/RSS 불필요. 세 수집기가 공유하는 추출·발췌 로직도 여기 있음 |
| `g2b_collect.py` | 나라장터 입찰공고(용역) — 공공데이터포털 Open API로 지난주 게시분 수집 후 `g2b_keywords.txt`로 1차 필터, 입찰공고서·제안요청서·과업지시서 최대 4개(서약서·계약조건 등 공통서식 제외)를 받아 참가자격 발췌. `G2B_SERVICE_KEY` 필요 |
| `g2b_keywords.txt` | 나라장터 1차 필터 — 공공조달분류(`[class_exclude]`/`[class_include]`)로 업종째 거르고, 공고명 키워드(`[strong]`은 제외어 무시 / `[include]` / `[weak]` / `[exclude]`)로 판단 |
| `cbist_collect.py` | 충북과학기술혁신원 사업공고(cbist.or.kr mncd=1131) — 지난주 등록분 본문·첨부 수집. NTIS 수집기의 추출 로직 재사용 |
| `boards_collect.py` | 게시판형 공고 수집기 `--site riia\|mss\|bizinfo\|bizinfo_other` — 충북 지역혁신클러스터(cb.riia.or.kr), 중기부 사업공고(mss.go.kr cbIdx=310), 기업마당 지원사업(충북·충남 / 그 외 14개 시·도, 접수중). 등록분 본문·첨부 수집, `--dedupe`로 다른 소스·seen.json과 제목이 같은 재게시 공고 제외 |
| `bizinfo_keywords.txt` | 기업마당 타지역(`bizinfo_other`) 공고명 1차 필터 포함/제외 키워드 |
| `discord_post.py` | `out/results.json` + `out/announcements.json`을 Discord 웹훅으로 발송. 기본: **적합·조건부를 한 줄 링크 목록**으로, 부적합은 미언급 (`--format embed`로 상세 embed+첨부, `--send`로 대상 판정 변경) |
| `daily_summary.py` | 매일 Discord 맨 앞 한 줄 요약 (`📋 10/01 공고 확인 — 나라장터 45 · NTIS 4건 중 적합 2건`). 적합 0건이어도 발송해 routine 동작 여부를 알 수 있게 함 |
| `seen_state.py` / `state/seen.json` | 이미 판정한 공고 기록. 수집기가 자동으로 제외하며, routine이 매일 커밋 |
| `company_profile.md` | **X2R 프로필 + 판정 규칙** — 판정 품질을 좌우하므로 꼼꼼히 유지 |
| `ROUTINE_PROMPT.md` | 클라우드 routine에 넣는 프롬프트 원본 |

## 로컬 테스트
```bash
pip install -r requirements.txt
python ntis_collect.py --from 2026-09-07 --to 2026-09-13 --out out   # 특정 주
python ntis_collect.py --days 7                                        # 최근 7일
# out/results.json 을 손으로/Claude로 작성한 뒤
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
python discord_post.py --out out --dry-run
python discord_post.py --out out
set G2B_SERVICE_KEY=...
python g2b_collect.py --from 2026-09-07 --to 2026-09-13 --out out/g2b
python boards_collect.py --site bizinfo --from 2026-09-21 --to 2026-09-27 --out out/bizinfo
```

## 클라우드 환경 설정 (routine 실행 전 1회)
claude.ai/code → 입력창 위 구름 아이콘(환경 이름) → Default 위 톱니바퀴:
- **Network access: Custom**, "Also include default list of common package managers" 체크, Allowed domains:
  ```
  www.ntis.go.kr
  ntis.go.kr
  discord.com
  *.discord.com
  *.discordapp.com
  apis.data.go.kr
  www.g2b.go.kr
  g2b.go.kr
  www.cbist.or.kr
  cbist.or.kr
  cb.riia.or.kr
  www.mss.go.kr
  www.bizinfo.go.kr
  ```
- **Environment variables**:
  ```
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  G2B_SERVICE_KEY=공공데이터포털 일반 인증키(Decoding)   # https://www.data.go.kr/data/15129394/openapi.do 활용신청(자동승인)
  ```
- **Setup script**: `pip install -r requirements.txt`

## NTIS 사이트 메모 (2026-09 기준)
- 목록: `POST /rndgate/eg/un/ra/mng.do` — `searchCondition2/3=YYYY-MM-DD`(등록기간), `pageUnit=100`, `pageIndex=n`
- 상세: `GET /rndgate/eg/un/ra/view.do?roRndUid=<uid>&flag=rndList`
- 첨부: `POST /rndgate/eg/cmm/file/download.do` — `wfUid`, `roTextUid` (상세 페이지의 `fn_fileDownload(...)` 인자)
- RSS(`rss.do`)는 로그인 필요 → 사용하지 않음
- 수집기는 마감 지난 공고, `state/seen.json`에 있는 공고, 나라장터 수의계약 공고를 자동 제외 (`--include-closed`, `--no-seen`, `--include-private`로 해제)
- 페이지 구조가 바뀌면 `ntis_collect.py`의 `ROW_RE`, `fetch_detail()` 정규식을 수정
