# NTIS 국가R&D통합공고 → X2R 적합성 판정 → Discord 주간 리포트

매주 월요일, 지난주에 NTIS(https://www.ntis.go.kr/rndgate/eg/un/ra/mng.do)에 등록된 공고를 수집하고,
`company_profile.md` 기준으로 X2R이 제안 가능한지 판정해 Discord 채널로 발송한다.

```
┌ Claude Code 클라우드 routine (매주 월 09:00 KST, cron 0 0 * * 1 UTC) ┐
│ 1. python ntis_collect.py   → out/digest.md, out/brief/<uid>.md, 첨부  │
│ 2. Claude가 company_profile.md 기준으로 판정 → out/results.json       │
│ 3. python discord_post.py   → Discord 웹훅 (embed + 첨부파일)          │
└─────────────────────────────────────────────────────────────────────────┘
```

## 파일
| 파일 | 역할 |
|---|---|
| `ntis_collect.py` | 목록(POST 날짜검색) → 상세(view.do) → 첨부 다운로드(download.do) → PDF/HWP/HWPX/DOCX 텍스트 추출 → `brief/`(자격·대상·규모·기간 구간 발췌) 생성. 로그인/RSS 불필요 |
| `g2b_collect.py` | 나라장터 입찰공고(용역) — 공공데이터포털 Open API로 지난주 게시분 수집 후 `g2b_keywords.txt`로 1차 필터. `G2B_SERVICE_KEY` 필요 |
| `g2b_keywords.txt` | 나라장터 1차 필터 포함/제외 키워드 |
| `discord_post.py` | `out/results.json` + `out/announcements.json`을 Discord 웹훅으로 발송 |
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
- 페이지 구조가 바뀌면 `ntis_collect.py`의 `ROW_RE`, `fetch_detail()` 정규식을 수정
