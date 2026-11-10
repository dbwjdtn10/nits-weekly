# 정부 R&D·입찰·지원사업 공고 → X2R 적합성 판정 → Discord 일일 리포트

평일 매일 아침, 최근 4일간 NTIS·나라장터·충북 기관(과기혁신원·지역혁신클러스터·테크노파크)·충남 기관(테크노파크·천안과학산업진흥원·천안문화재단·충남콘텐츠진흥원)·중기부·기업마당·NIPA·과기부에 등록된 공고를 수집하고 (이미 판정한 공고는 제외),
`company_profile.md` 기준으로 X2R이 제안 가능한지 판정해 Discord 채널로 발송한다.

```
┌ Claude Code 클라우드 routine (평일 09:00 KST, cron 0 0 * * 1-5 UTC) ┐
│ 1. ntis / g2b / boards(충북·충남 묶음, mss·bizinfo·nipa) → out/*/brief/<uid>.md │
│ 2. Claude가 company_profile.md 기준으로 판정 → out/results.json       │
│ 3. python discord_post.py   → Discord 웹훅 (적합·조건부 링크 목록만)    │
│ 3-b. python sheet_export.py → 구글 드라이브 엑셀에 적합 본공고 한 줄씩  │
│ 4. python seen_state.py mark → state/seen.json 커밋 (재판정 방지)       │
└─────────────────────────────────────────────────────────────────────────┘
```

## 파일
| 파일 | 역할 |
|---|---|
| `ntis_collect.py` | 목록(POST 날짜검색) → 상세(view.do) → 첨부 다운로드(download.do) → PDF/HWP/HWPX/DOCX/XLSX 텍스트 추출(ZIP은 풀어서 내부 문서까지) → `brief/`(자격·대상·규모·기간 구간 발췌, 양식·법령·매뉴얼류 제외, 읽지 못한 첨부 표시) 생성. 로그인/RSS 불필요. 세 수집기가 공유하는 추출·발췌 로직도 여기 있음 |
| `g2b_collect.py` | 나라장터 입찰공고(용역) — 공공데이터포털 Open API로 지난주 게시분 수집 후 `g2b_keywords.txt`로 1차 필터, 입찰공고서·제안요청서·과업지시서 최대 4개(서약서·계약조건 등 공통서식 제외)를 받아 참가자격 발췌. `G2B_SERVICE_KEY` 필요 |
| `g2b_keywords.txt` | 나라장터 1차 필터 — 공공조달분류(`[class_exclude]`/`[class_include]`)로 업종째 거르고, 공고명 키워드(`[strong]`은 제외어 무시 / `[include]` / `[weak]` / `[exclude]`)로 판단 |
| `g2b_prespec_collect.py` | 나라장터 **용역 사전규격**(본공고 전 제안요청서·과업지시서 초안) — 공공데이터포털 "조달청_나라장터 사전규격정보서비스"(같은 키, 활용신청 필요). 공고명 키워드 필터 후 규격서 첨부를 받아 과업 개요·참가자격 발췌. 사전규격 → 본공고 중앙값 8일 앞섬. 본공고가 올라오면 `g2b_collect.py`가 `bfSpecRgstNo`로 연결해 "본공고 올라옴"으로 표시 |
| `awards_index.py` / `state/awards.json` | 나라장터 **용역 낙찰 이력 색인** (조달청_나라장터 낙찰정보서비스, 같은 키). API가 공고명 검색·한 달 초과 조회를 지원하지 않아 g2b_keywords 필터를 통과한 낙찰만 3년치 저장(`backfill` 최초 1회, `update` 매일). `lookup --title --agency`로 비슷한 사업의 낙찰사·낙찰률·참가 업체 수 조회 (Discord 카드에는 띄우지 않음) |
| `cbist_collect.py` | 충북과학기술혁신원 사업공고(cbist.or.kr mncd=1131) 목록·상세 파서. routine은 `boards_collect.py --group chungbuk`으로 부르고, 단독 실행도 가능 |
| `boards_collect.py` | 게시판형 공고 수집기. **지역 묶음** `--group chungbuk`(충북과기혁신원·충북 지역혁신클러스터·충북테크노파크) / `--group chungnam`(충남테크노파크·천안과학산업진흥원·천안문화재단·충남콘텐츠진흥원)은 여러 기관을 한 폴더에 모아 Discord에 `충북`·`충남`으로 나가게 한다(한 기관이 실패해도 나머지는 수집, 판정 기록은 기관별). `--site mss\|bizinfo_other\|bizinfo_central\|nipa\|msit`는 중기부·기업마당 지원사업(공공데이터포털 기업마당 API를 소관기관으로 나눔: 중기부 / 충북·충남 / 그 외 14개 시·도 / 중앙부처, 접수중 — 중기부 홈페이지 사업공고는 이 API에 모두 들어 있어 따로 받지 않음), NIPA, 과기부 사업공고. 등록분 본문·첨부 수집, `--dedupe`로 다른 소스·seen.json과 제목이 같은 재게시 공고 제외 |
| `bizinfo_keywords.txt` | 기업마당 타지역(`bizinfo_other`) 공고명 1차 필터 포함/제외 키워드 |
| `discord_post.py` | `out/results.json` + `out/announcements.json`을 Discord 웹훅으로 발송. 기본: **적합은 검토 카드**(만들 것·비슷한 실적·기간·평가·제출·확인할 점 — results.json `card`), 이미 보낸 적합 공고의 재공고는 "다시 올라온 공고" 한 줄, 조건부·부적합은 판정별 txt 첨부 (`--format embed`로 상세 embed+첨부, `--send`로 대상 판정 변경) |
| `daily_summary.py` | 매일 Discord 맨 앞 한 줄 요약 (`📋 10/01 공고 확인 — 나라장터 45 · NTIS 4건 중 적합 2건`). 적합 0건이어도 발송해 routine 동작 여부를 알 수 있게 함 |
| `sheet_export.py` | Discord 발송 뒤 **적합 본공고(사전규격 제외)를 구글 드라이브 폴더의 엑셀 `적합공고.xlsx`에 한 줄씩** 덧붙인다 (사업명·등록일·마감일·만들것·첨부파일). 엑셀은 내려받아 줄만 붙이고 같은 파일로 다시 올리므로 링크가 그대로이고 사람이 고친 내용도 남는다. 공고마다 드라이브에 폴더를 만들어 첨부를 올리고 첨부파일 칸에 폴더 링크를 건다(첨부를 못 받은 공고는 공고 원문 링크). 재공고는 새 줄 대신 기존 줄의 마감일을 고친다. `DRIVE_WEBAPP_URL`이 없으면 건너뜀. `--local 폴더`로 드라이브 없이 모양 확인 |
| `drive_webapp.gs` | 위 스크립트가 호출하는 Google Apps Script 웹앱 원본 — 아래 "구글 드라이브 연결" 참고 |
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
python boards_collect.py --group chungnam --days 7 --out out/chungnam   # 충남 묶음 (사이트 하나만: --site ctp)
```

## 클라우드 환경 설정 (routine 실행 전 1회)
claude.ai/code → 입력창 위 구름 아이콘(환경 이름) → Default 위 톱니바퀴:
- **Network access: Custom**, "Also include default list of common package managers" 체크, Allowed domains:
  ```
  www.ntis.go.kr
  discord.com
  apis.data.go.kr
  www.g2b.go.kr
  www.cbist.or.kr
  cb.riia.or.kr
  www.cbtp.or.kr
  *.cbtp.or.kr
  www.ctp.or.kr
  www.cistep.re.kr
  www.cfac.or.kr
  ccon.kr
  www.bizinfo.go.kr
  www.msit.go.kr
  www.nipa.kr
  script.google.com
  script.googleusercontent.com
  ```
  (`*.cbtp.or.kr`은 충북테크노파크 산하 센터 게시판의 첨부용 — semicon·pbi 등. `www.bizinfo.go.kr`은 기업마당 첨부용인데 2026-10-20부터 클라우드에서 연결이 끊긴다. `www.mss.go.kr`은 같은 이유로, 그리고 기업마당 API가 중기부 홈페이지 공고를 포괄해서 쓰지 않는다.)
- **Environment variables**:
  ```
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  G2B_SERVICE_KEY=공공데이터포털 일반 인증키(Decoding)   # https://www.data.go.kr/data/15129394/openapi.do 활용신청(자동승인). 기업마당 API(15157820)도 같은 키로 활용신청
  DRIVE_WEBAPP_URL=https://script.google.com/macros/s/.../exec   # 적합 공고 엑셀 정리용 (아래 "구글 드라이브 연결"). 없으면 엑셀 정리만 건너뜀
  ```
- **Setup script**: `pip install -r requirements.txt`

## 구글 드라이브 연결 (적합 공고 엑셀, 1회)
클라우드에서 드라이브에 파일을 올리려면 드라이브 주인 계정으로 만든 작은 웹앱이 필요하다 (`script.google.com`, `script.googleusercontent.com`을 허용 도메인에 추가).
1. 엑셀·첨부를 둘 드라이브 폴더를 열고 주소창의 링크를 복사한다.
2. https://script.google.com → **새 프로젝트** → 기본 코드를 지우고 `drive_webapp.gs` 내용을 전부 붙여 넣은 뒤, 맨 위 `FOLDER` 따옴표 안에 1의 폴더 링크를 넣고 저장.
3. 왼쪽 **서비스** 옆 `+` → **Drive API** → 추가. (엑셀 링크가 매일 바뀌지 않게 하는 데 필요)
4. 위쪽 함수 선택에서 `setup`을 고르고 **실행** → 권한 검토 → 계정 선택 → "고급" → "(안전하지 않음)으로 이동" → 허용. 실행 로그에 `연결된 폴더: (폴더 이름)`이 나오면 정상.
5. **배포 → 새 배포** → 유형 **웹 앱** → 실행 계정 **나**, 액세스 권한 **모든 사용자** → 배포 → **웹 앱 URL**(`.../exec`) 복사.
6. 그 URL을 클라우드 환경변수 `DRIVE_WEBAPP_URL`과 로컬 `test.env`(`DRIVE_WEBAPP_URL=...`)에 넣는다. Discord 웹훅처럼 주소를 아는 사람은 그 폴더에 파일을 올릴 수 있으므로 공유하지 않는다.
- 코드를 고치면 **배포 → 배포 관리 → 수정(연필) → 버전: 새 버전**으로 다시 배포해야 반영된다 (URL은 그대로).

## NTIS 사이트 메모 (2026-09 기준)
- 목록: `POST /rndgate/eg/un/ra/mng.do` — `searchCondition2/3=YYYY-MM-DD`(등록기간), `pageUnit=100`, `pageIndex=n`
- 상세: `GET /rndgate/eg/un/ra/view.do?roRndUid=<uid>&flag=rndList`
- 첨부: `POST /rndgate/eg/cmm/file/download.do` — `wfUid`, `roTextUid` (상세 페이지의 `fn_fileDownload(...)` 인자)
- RSS(`rss.do`)는 로그인 필요 → 사용하지 않음
- 수집기는 마감 지난 공고, `state/seen.json`에 있는 공고, 나라장터 수의계약 공고를 자동 제외 (`--include-closed`, `--no-seen`, `--include-private`로 해제)
- 페이지 구조가 바뀌면 `ntis_collect.py`의 `ROW_RE`, `fetch_detail()` 정규식을 수정
