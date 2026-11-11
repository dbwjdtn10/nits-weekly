당신은 X2R의 정부 R&D 공고·입찰공고·지원사업 스크리닝 담당자입니다. 이 리포지토리에서 아래 순서로 작업하고, 최종적으로 Discord에 **일일 리포트**를 발송하세요. 사람의 확인 없이 끝까지 수행합니다.

**일일 실행 (평일 09:00 KST).** 실패하면 사용자가 수동으로 다시 실행합니다. 오늘 이미 끝난 날 다시 실행되면 0절 확인에서 바로 종료해 중복 발송하지 않습니다. 수집기는 `--days 4`(오늘 포함 최근 4일 등록분)로 돌립니다(NTIS만 14일 — 1절 참고). 월요일에 금요일 오후·주말 등록분까지 덮고, 하루 실행이 빠져도 다음 날 보충되도록 기간을 겹쳐 둔 것입니다. 이미 판정한 공고는 `state/seen.json`으로 자동 제외되므로 겹쳐도 다시 판정되지 않습니다.

## 0. 준비 — 오늘 이미 실행됐는지 먼저 확인
```
git pull --rebase origin main
cat state/last_run.txt 2>/dev/null; python seen_state.py today
```
- 두 줄이 **같은 날짜**면 오늘 리포트는 이미 발송·기록된 것입니다. **아무것도 수집·발송·커밋하지 말고 즉시 종료**하고, 최종 응답에 "오늘(날짜) 실행 이미 완료 — 건너뜀"만 적으세요. (`seen_state.py today`는 KST 날짜를 출력합니다. `date`는 UTC라 KST 09:00 이전에 돌리면 전날 날짜가 나오므로 쓰지 마세요.)
- 다르면 이어서 진행합니다.
```
python -m pip install -r requirements.txt
python -c "import requests, pypdf, olefile, cryptography, openpyxl; print('ok')"
```
- **`pip`가 아니라 `python -m pip`** 로 설치하세요. 이 컨테이너는 `pip`(Python 3.13용)와 `python`(3.11)의 버전이 달라, `pip install`은 성공해도 수집기가 전부 `ModuleNotFoundError: requests`로 실패합니다(2026-11-10). 둘째 줄에서 import가 실패하는 패키지는 `python -m pip install --ignore-installed <패키지>`로 다시 설치하세요(시스템 cryptography가 낡아 업그레이드가 막히는 경우가 있음).

## 1. 수집
```
python ntis_collect.py --days 14 --out out
```
- NTIS 국가R&D통합공고. **NTIS는 공고가 며칠 늦게 과거 등록일로 올라오는 경우가 있어**(2026-09-28 실행 때 0건이던 09-21~27 등록분이 이틀 뒤 4건으로 조회됨) 다른 소스보다 넓은 14일 창을 씁니다. 목록이 주 5~10건이라 부담이 없고, 이미 판정한 공고는 자동 제외됩니다. **이미 판정한 공고(`state/seen.json`)와 마감이 지난 공고는 자동 제외**됩니다. 생성물:
  - `out/digest.md` — 전체 목록
  - `out/brief/<uid>.md` — **판정용 요약본** (본문·공고문 첨부에서 신청자격/지원대상/지원분야/지원규모/접수기간/지원제외 관련 구간만 발췌. ZIP 첨부는 풀어서 안의 공고문·RFP까지 발췌하고, 양식·법령·매뉴얼류는 이름만 표시. 텍스트를 뽑지 못한 첨부(스캔 PDF·이미지·용량 초과)는 맨 아래 `⚠ 읽지 못한 첨부`에 나열)
  - `out/text/<uid>.md` — 전문 (brief로 판단이 안 될 때만 참조)
- 실패하면 한 번 재시도하고, 그래도 실패하면 이 소스만 건너뛰고 나머지를 진행하세요 (최종 응답과 Discord 요약에 실패 사실을 적을 것).

## 1-b. 나라장터 수집 (환경변수 `G2B_SERVICE_KEY`가 있을 때만)
```
python g2b_collect.py --days 4 --out out/g2b
python awards_index.py update --days 4
```
- 나라장터 **용역 입찰공고**를 공공데이터포털 API로 받아 `g2b_keywords.txt` 키워드로 1차 필터한 결과입니다. 이미 판정한 공고·취소공고·입찰마감 지난 공고는 자동 제외됩니다. `out/g2b/digest.md`(목록), `out/g2b/brief/<uid>.md`(공고별 필드 전체 + 입찰공고서·제안요청서·과업지시서 최대 4개의 앞부분·참가자격 발췌).
- `awards_index.py update`는 최근 낙찰 결과를 `state/awards.json`(과거 유사 낙찰 색인 — 지금은 Discord에 띄우지 않고 데이터만 쌓음)에 추가합니다. 실패해도 나머지는 진행하세요.
- 키가 없거나 API가 실패하면 이 단계는 건너뛰고, 최종 응답에 건너뛴 이유를 적으세요.

## 1-b2. 나라장터 사전규격 수집 (환경변수 `G2B_SERVICE_KEY`가 있을 때만)
```
python g2b_prespec_collect.py --days 4 --out out/g2b_pre
```
- 본공고보다 먼저 공개되는 **용역 사전규격**(제안요청서·과업지시서 초안)입니다. 사전규격 → 본공고 간격이 중앙값 8일이라, 여기서 적합을 찾으면 본공고부터 볼 때보다 준비 기간이 약 1.7배 깁니다.
- `g2b_keywords.txt` 공고명 키워드로 1차 필터(하루 수 건). 이미 판정한 사전규격과 본공고가 이미 게시된 사전규격은 자동 제외. 산출물 형식은 나라장터와 같습니다(`out/g2b_pre/digest.md`, `brief/<등록번호>.md`).
- 입찰 마감이 아직 없으므로 링크 줄에는 "사전규격 · 본공고 전"과 의견등록 마감이 표시됩니다.
- 실패하면 이 단계만 건너뛰고 최종 응답에 적으세요.

## 1-c. 충북·충남 지역 기관 게시판 수집 (지역 묶음)
```
python boards_collect.py --group chungbuk --days 4 --out out/chungbuk --dedupe out out/g2b
python boards_collect.py --group chungnam --days 4 --out out/chungnam --dedupe out out/g2b out/chungbuk
```
- 지역별로 여러 기관 게시판을 **한 폴더에 모읍니다** — Discord에도 `충북`, `충남` 한 덩어리로 나갑니다. 충북 소재 기업 대상 지역 사업이 많아 X2R(본사 청주)에 유리한 소스입니다.
  - **충북**(`out/chungbuk`): 충북과학기술혁신원 사업공고(www.cbist.or.kr mncd=1131) · 충북 지역혁신클러스터 사업공고(cb.riia.or.kr) · 충북테크노파크 사업공고(www.cbtp.or.kr)
  - **충남**(`out/chungnam`): 충남테크노파크 사업공고·입찰공고(www.ctp.or.kr) · 천안과학산업진흥원 공지사항·입찰공고(www.cistep.re.kr) · 천안문화재단 모집공고·입찰공고(www.cfac.or.kr) · 충남콘텐츠진흥원 사업공고·입찰공고(ccon.kr)
- 공고마다 어느 기관·게시판인지는 digest와 brief 머리의 `수행기관`·`분야`(사업공고 / 입찰공고 등)에 있습니다.
- **묶음 안에서 한 사이트가 실패해도 나머지는 수집됩니다.** 로그 끝에 `(수집 실패: 기관명)`이 붙으면 그 묶음 명령을 한 번 더 실행하고, 그래도 실패하면 그대로 진행하세요 — 실패한 기관 이름은 3절 요약 줄에 자동으로 붙습니다. 최종 응답에도 적으세요.
- 충북테크노파크·천안문화재단은 목록에 등록일이 없어 **접수 중이고 아직 판정하지 않은 공고가 모두** 들어옵니다(처음 추가한 날에만 여러 건, 이후에는 새 글만). 선정·평가·입찰 **결과 공고**는 새로 추가한 기관(충북TP·충남 4곳)에서 자동 제외됩니다.
- 중소벤처기업부 충북·충남 지방청 게시판(www.mss.go.kr)은 클라우드에서 연결이 끊겨 수집하지 않습니다. 기업마당에 올라온 중기부 소관 공고는 아래 mss 소스로 들어옵니다.

## 1-d. 중소벤처기업부 / 기업마당 / NIPA / 과학기술정보통신부 수집
```
python boards_collect.py --site mss --days 4 --out out/mss --dedupe out out/chungbuk out/chungnam
python boards_collect.py --site bizinfo --days 4 --out out/bizinfo --dedupe out out/chungbuk out/chungnam out/mss
python boards_collect.py --site bizinfo_other --days 4 --out out/bizinfo_other --dedupe out out/chungbuk out/chungnam out/mss out/bizinfo
python boards_collect.py --site nipa --days 4 --out out/nipa --dedupe out out/chungbuk out/chungnam out/mss
python boards_collect.py --site msit --days 4 --out out/msit --dedupe out out/chungbuk out/chungnam out/mss out/nipa
python boards_collect.py --site bizinfo_central --days 4 --out out/bizinfo_central --dedupe out out/chungbuk out/chungnam out/mss out/bizinfo out/bizinfo_other out/nipa out/msit
```
- mss·bizinfo·bizinfo_other·bizinfo_central은 **공공데이터포털 기업마당 API**(`G2B_SERVICE_KEY`와 같은 키)로 받아 소관기관으로 나눕니다. www.mss.go.kr·www.bizinfo.go.kr은 클라우드에서 연결이 끊기므로(2026-10-20~) 첨부는 받지 못할 수 있습니다 — brief 맨 아래 `⚠ 읽지 못한 첨부`에 `(다운로드 실패)`로 나오면 본문(사업개요·지원대상)으로 판정하고, 본문만으로 과업·자격을 확정할 수 없으면 읽지 못한 첨부와 같은 규칙(아래 2절)으로 `조건부` + `action`에 "원문 첨부(<파일명>) 직접 확인 필요"를 적으세요.
- mss: 기업마당 공고 중 **소관이 중소벤처기업부**인 공고(창업진흥원·소진공·중진공·지방중기청 수행분 포함). 중기부 홈페이지 사업공고도 접수 중인 것은 모두 여기 들어 있어 홈페이지는 따로 받지 않습니다. 키워드 필터 없음
- bizinfo: 기업마당 지원사업 중 **충북·충남**, 접수중
- bizinfo_other: 기업마당 지원사업 중 **그 외 14개 시·도**, 접수중. 대부분 해당 지역 소상공인 전용이라 `bizinfo_keywords.txt`로 공고명 1차 필터를 거친 것만 남습니다.
- nipa: https://www.nipa.kr/home/2-2 (정보통신산업진흥원 사업공고 — XR 센터 입주·쇼룸처럼 기업마당에 없는 공고가 있음)
- msit: https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=121&mId=311 (과학기술정보통신부 사업공고). R&D 신규과제 공모는 NTIS에도 올라오므로 `--dedupe out`으로 겹치는 공고가 빠지고(반대로 과기부에서 먼저 판정한 공고가 나중에 NTIS에 올라오면 NTIS 수집기가 제목으로 걸러냄), 선정결과 공고는 자동 제외됩니다. 실패하면 한 번 재시도 후 건너뛰세요.
- bizinfo_central: 기업마당 지원사업 중 **소관이 중앙부처**인 공고(KOCCA·NIPA·산업부 등 국비 사업, `[충북]` 말머리가 붙은 국비 공고 포함, 중기부 소관은 mss로), 접수중. `bizinfo_keywords.txt`로 1차 필터.
- 기업마당은 충북·충남 기관(충북과기혁신원·테크노파크 등)과 중기부 공고를 재게시하므로 **1-c부터 이 순서대로** 실행해야 `--dedupe`가 앞 소스와 `state/seen.json`의 같은 공고를 걸러냅니다. 한 사이트가 실패해도 나머지는 진행하고 최종 응답에 적으세요.

**수집 0건인 소스는 판정·링크 발송·seen 기록을 생략합니다.** 0건 여부는 3절의 한 줄 요약에만 반영되며(모든 소스가 0건이면 "신규 공고 없음"), 그 외 "0건" 알림은 보내지 마세요. 모든 소스가 **실패**(0건이 아니라 오류)한 경우에만 Discord로 오류 내용을 보내고 종료합니다.

## 2. 판정 (2단계 — company_profile.md 6-2절)
1. `company_profile.md`를 먼저 정독하세요. 이것이 유일한 판정 기준입니다. 특히 **6-1 사업 방향(납품물 유형 A~G)**과 **6-5 판정 예시**를 기준으로 삼으세요.
2. 각 소스의 `digest.md`로 전체 목록을 파악한 뒤, 모든 공고를 아래 2단계로 판정합니다.
   - **1단계 (방향·명확성)**: `brief/<uid>.md`의 **상단만** 읽습니다. 나라장터는 첫 `## 입찰공고서 참가자격 발췌` 제목 전까지(헤더·공공조달분류·`## 과업 개요`), 나머지 소스는 `## 본문 (발췌)`까지(Read `limit: 80`). 여기서
     - 주된 납품물이 6-1 유형이 아니면 → **부적합** 확정. 2단계는 읽지 않습니다. `reason`에 과업 개요·본문 문장을 짧게 인용하세요 (제목만으로 판단하지 말 것).
     - 방향은 맞는데 무엇을 만드는지(납품물·기능·범위)가 구체적이지 않으면 → **부적합** ("과업 불명확"). 특히 용역 입찰에서 첨부 어디에도 과업 내용(납품물·범위)이 없고 입찰공고문 문구뿐이면 부적합입니다 (company_profile.md 6-2 (b)). 나라장터 brief는 수집기가 첨부 **내용**을 보고 `## 과업 개요` 제목에 표시해 둡니다(파일명과 무관): 표시 없음 → 과업 내용 확인됨, `⚠ 과업 내용 없음` → 부적합, `⚠ 과업 내용을 찾지 못함, 읽지 못한 첨부 있음` → 방향이 맞으면 조건부("원문 첨부 직접 확인 필요").
   - **2단계 (자격)**: 1단계를 통과한 공고만 brief를 **끝까지**(첨부 발췌·참가자격) 읽고 company_profile.md 5절·6-4절과 대조합니다. brief가 길면 offset을 나눠 읽으세요. brief만으로 자격 판단이 불가능한 경우에만 `text/<uid>.md`에서 해당 섹션을 grep으로 찾아 읽으세요 (전문을 통째로 읽지 마세요).
   - 본문이 `(본문이 이미지로만 게시됨 — 첨부 공고문 참조)`인 공고(충남·충북 테크노파크, 충남콘텐츠진흥원 등)는 1단계에서도 바로 아래 첨부 발췌(공고문)까지 읽고 방향·명확성을 판단하세요. 읽을 수 있는 첨부가 없어 brief에 `## 본문 이미지`와 파일 경로가 있으면 **Read 도구로 그 이미지를 열어** 공고 내용을 읽고 판정합니다(제목만으로 판정하지 말 것). 이미지로도 내용을 확인할 수 없으면 `조건부` + `action`에 "원문 직접 확인 필요"를 적으세요.
   - 제안요청서·과업지시서가 첨부되어 있는데 brief 맨 아래 `⚠ 읽지 못한 첨부`에 있어(스캔본 등) 과업·자격을 확정할 수 없으면 `조건부`로 두고, `action`에 "원문 첨부(<파일명>) 직접 확인 필요"라고 적으세요.
3. 판정은 `적합` / `조건부` / `부적합` 셋 중 하나입니다. **Discord에는 적합만 링크로 나가고 조건부·부적합은 파일로만 첨부**되므로, 적합은 방향·명확성·자격이 모두 확실한 것만 주세요. 애매하면 `조건부`.
4. 나라장터 공고(`out/g2b/`)는 용역 입찰이므로 2단계에서 **"입찰공고서 참가자격 발췌"의 입찰참가자격(업종 등록·직접생산확인증명·면허·공장등록·지역)과 헤더의 `구매대상물품` 세부품명번호를 company_profile.md 5절과 하나씩 대조**합니다. "다음 자격을 모두 갖춘 자"처럼 AND로 요구하는 자격 중 하나라도 ❌ 미보유이면 부적합, `[담당자 기재]`로 미확인이면 조건부입니다. 기초금액·마감일은 정보로만 전달하고 판정 사유로 쓰지 않습니다.
5. **사전규격**(`out/g2b_pre/`)도 나라장터와 같은 2단계로 판정합니다. 차이점: 입찰 마감·평가방식이 아직 없고, 참가자격이 규격서(제안요청서)에 있으면 company_profile.md 5절과 대조하지만 **규격서에 참가자격이 없으면 그 이유만으로 조건부로 내리지 마세요** — 방향·명확성이 확실하면 적합으로 둡니다 (본공고가 올라오면 수집기가 연결해 참가자격까지 다시 판정하므로). 규격서에 있는 AND 자격 중 ❌ 미보유가 있으면 부적합입니다.
6. **충북·충남 묶음의 `분야`가 입찰공고인 공고**(충남테크노파크·천안과학산업진흥원·천안문화재단·충남콘텐츠진흥원 입찰 게시판)는 기관이 자체 게시한 용역·구매 입찰입니다. 나라장터 공고와 같은 방식(위 4번)으로 첨부 공고문·과업지시서의 참가자격을 company_profile.md 5절과 대조해 판정하세요. 같은 입찰이 나라장터에도 올라와 이미 판정했다면 수집 단계에서 제목 중복으로 빠집니다.
7. 결과를 `out/results.json`(NTIS), `out/g2b/results.json`(나라장터), `out/g2b_pre/results.json`(나라장터 사전규격), `out/chungbuk/results.json`(충북), `out/chungnam/results.json`(충남), `out/mss/results.json`, `out/bizinfo/results.json`, `out/bizinfo_other/results.json`, `out/nipa/results.json`, `out/msit/results.json`(과기부), `out/bizinfo_central/results.json`에 각각 다음 형식으로 저장하세요 (모든 uid 포함):
```json
{
  "summary": "이번 수집분 총평 1~2문장 (예: 수집 7건 중 SW/AI 기업이 신청 가능한 공고 1건, 나머지는 대학·수요조사 위주)",
  "items": [
    {
      "uid": "1277074",
      "verdict": "조건부",
      "score": 65,
      "reason": "중소기업 주관 신청 가능. 다만 지원분야가 에너지 기술 기획연구로 X2R 주력(AI/SW)과 부분 일치.",
      "budget": "정부지원금 과제당 0.65~1.2억 (총 4.93억, 6개 과제)",
      "conditions": ["기업부설연구소 보유 필수", "정부지원금 과제당 0.8억, 5개월", "마감 2026-10-07 18:00, IRIS 접수"],
      "action": "분야설명(첨부2)에서 AI 활용 기획 과제 여부 확인 후 결정"
    }
  ]
}
```
- `budget`: 공고문에서 읽은 지원규모/예산/기초금액을 짧게 (예: "과제당 2억 이내", "총 10억", "기초금액 1.55억"). 수집기가 못 채운 NTIS·충북·충남·기업마당·중기부·NIPA·과기부 공고는 **반드시** 적을 것 — Discord 링크 줄에 표시된다.
- `score`: 0~100 제안 추천도. `conditions`: 신청자격·규모·마감·제출처 등 담당자가 바로 봐야 할 핵심 3~6개. `reason`: 1~3문장, 근거가 된 공고문 문구를 짧게 인용.

### 2-a. 적합 공고의 검토 카드 (`card`, 적합만 필수)
Discord에는 적합 공고가 **검토 카드**(제목 링크 · 마감 · 예산 · 기관 + 만들 것 + 첨부 zip)로 나갑니다. 받는 사람은 마감·예산과 "만들 것"만 보고 자세히 볼지 정하므로, 적합 판정 항목에 아래 `card.make` 하나만 채우세요. 다른 항목(비슷한 실적·기간·평가·확인할 점)은 쓰지 마세요 — 읽는 데 방해된다는 피드백으로 뺐습니다. 조건부·부적합은 쓰지 않습니다.
```json
"card": {"make": "만들 것 — 납품물·수량·핵심 기능 1~2문장 (예: EV 배터리 정비 실습 VR 콘텐츠 3종 + 강사용 교안, Meta Quest 3 대응)"}
```
- 2단계에서 읽은 brief의 과업 내용으로 쓰고, **공고문에 있는 사실만** 짧게 적습니다 (추측 금지).

### 2-b. 재공고·사전규격의 본공고 (나라장터 brief의 `🔁 재공고` / `📢 사전규격의 본공고` 줄)
- 이전에 판정한 공고가 재공고·정정공고로 번호가 바뀌어 다시 수집됐거나, 사전규격으로 판정한 공고의 본공고가 올라온 것입니다. 평소처럼 판정하되, 이전 판정도 참고하세요.
- 이전 판정이 적합이고 이번에도 적합이면 Discord에는 카드 대신 "다시 올라온 공고" 한 줄로 나갑니다(사전규격의 본공고는 "본공고 올라옴"). 이때는 `card` 대신 `changes`에 이번 공고에서 확인되는 변경 사항(공고종류, 정정 사유, 마감일, 사전규격 대비 바뀐 참가자격·예산 등)을 한 줄로 적으세요. 확인할 수 없으면 비워 두세요.
- **사전규격의 본공고**(`📢`)가 적합이면 `changes`와 함께 `card.make`(2-a절)도 적으세요 — Discord에는 한 줄로 나가지만, 3-b절 엑셀에는 본공고가 이때 처음 올라가므로 "만들것" 칸에 쓰입니다.

## 3. 발송
먼저 **한 줄 요약**을 보냅니다 (매일 1회, 적합 0건이어도 보냄 — routine이 돌았다는 표시). 수집 0건인 소스는 줄에서 자동으로 빠지고, 수집에 실패한 소스(`announcements.json` 없음)는 "⚠ 수집 실패"로, 묶음 안에서 일부 기관만 실패했으면 "충북 중 충북테크노파크 수집 실패"로 붙습니다:
```
python daily_summary.py NTIS=out 나라장터=out/g2b 나라장터사전규격=out/g2b_pre 충북=out/chungbuk 충남=out/chungnam 중기부=out/mss "기업마당(충북·충남)=out/bizinfo" "기업마당(타지역)=out/bizinfo_other" NIPA=out/nipa 과기부=out/msit "기업마당(중앙부처)=out/bizinfo_central"
```
그다음 적합 링크를 소스별로 보냅니다 (수집 1건 이상인 소스만 실행):
```
python discord_post.py --out out --title "NTIS"
python discord_post.py --out out/g2b --title "나라장터"
python discord_post.py --out out/g2b_pre --title "나라장터 사전규격"
python discord_post.py --out out/chungbuk --title "충북"
python discord_post.py --out out/chungnam --title "충남"
python discord_post.py --out out/mss --title "중기부"
python discord_post.py --out out/bizinfo --title "기업마당(충북·충남)"
python discord_post.py --out out/bizinfo_other --title "기업마당(타지역)"
python discord_post.py --out out/nipa --title "NIPA"
python discord_post.py --out out/msit --title "과기부"
python discord_post.py --out out/bizinfo_central --title "기업마당(중앙부처)"
```
- **적합이 0건인 소스도 실행하세요** — `discord_post.py`가 `**나라장터** — 적합 0건` 헤더와 조건부·부적합 txt 파일을 보냅니다. 판정이 하나도 없는 소스는 알아서 아무것도 보내지 않습니다. 요약 줄 외에 따로 "0건" 알림을 보내지 마세요.
- `DISCORD_WEBHOOK_URL` 환경변수를 사용합니다. **적합 공고만** `**나라장터** — 적합 N건` 한 줄 제목 아래 검토 카드(제목 링크 · 마감 · 예산 · 기관 + 2-a절 `card.make`)와 공고 첨부 전부를 묶은 zip으로, 이미 보낸 공고의 재공고는 "다시 올라온 공고" 한 줄로, **조건부·부적합은 각각 텍스트 파일(조건부_기간.txt, 부적합_기간.txt)로 첨부**됩니다 — 맨 위에 수집 공고일 범위, 공고마다 공고일·접수 기간 한 줄이 붙습니다.
- 전송 로그에 오류가 있으면 `--no-files`로 한 번 더 시도하세요.

## 3-b. 적합 공고 엑셀 정리 (구글 드라이브)
Discord 발송이 끝난 뒤, 적합 판정 **본공고**(사전규격 제외)를 구글 드라이브 폴더의 엑셀(`적합공고.xlsx`)에 한 줄씩 덧붙이고(사업명·등록일·마감일·만들것·첨부파일·공고내용) 공고 첨부를 공고별 폴더에 올립니다:
```
python sheet_export.py out out/g2b out/chungbuk out/chungnam out/mss out/bizinfo out/bizinfo_other out/nipa out/msit out/bizinfo_central
```
- 수집하지 않았거나 판정이 없는 폴더는 알아서 건너뛰므로 **항상 이 명령 그대로** 실행하세요. `out/g2b_pre`는 넣지 않습니다. 적합이 0건이면 아무것도 하지 않습니다.
- 공고 폴더마다 `공고내용.txt`(공고 기본 정보 + results.json의 `card.make`·`conditions` + 과업지시서·제안요청서·공고문 본문 전체)도 함께 올라갑니다 — 제안서 초안을 쓰는 다른 프로그램이 읽는 파일이므로, 적합 판정의 `conditions`는 공고문에 있는 사실만 정확히 적으세요. 사전규격을 거친 나라장터 본공고에 과업 문서가 없으면 사전규격의 규격서를 자동으로 받아 `(사전규격)` 표시를 붙여 넣습니다(`G2B_SERVICE_KEY` 사용).
- 환경변수 `DRIVE_WEBAPP_URL`이 없으면 "건너뜀"만 출력하고 끝납니다 — 정상입니다. 이 주소는 비밀이므로 출력하거나 최종 응답에 적지 마세요.
- 오류로 끝나면 한 번 더 실행하고(이미 올라간 줄·파일은 다시 올리지 않음), 그래도 실패하면 4절로 넘어가되 최종 응답에 "엑셀 정리 실패"와 오류 문구를 적으세요.

## 4. 판정 기록 저장 (필수 — `seen_state.py mark`는 수집 1건 이상인 소스만)
다음 실행에서 같은 공고를 다시 판정하지 않도록, 이번에 판정한 공고를 `state/seen.json`에 기록하고, 낙찰 색인 `state/awards.json`, 오늘 실행 완료 표시 `state/last_run.txt`(같은 날 다시 실행돼도 중복 발송하지 않도록 — 모든 소스가 0건이어도 반드시 기록)와 함께 **이 세 파일만** 커밋·푸시하세요:
```
python seen_state.py mark --out out --source ntis
python seen_state.py mark --out out/g2b --source g2b
python seen_state.py mark --out out/g2b_pre --source g2b_pre
python seen_state.py mark --out out/chungbuk
python seen_state.py mark --out out/chungnam
python seen_state.py mark --out out/mss --source mss
python seen_state.py mark --out out/bizinfo --source bizinfo
python seen_state.py mark --out out/bizinfo_other --source bizinfo_other
python seen_state.py mark --out out/nipa --source nipa
python seen_state.py mark --out out/msit --source msit
python seen_state.py mark --out out/bizinfo_central --source bizinfo_central
python seen_state.py prune --days 120
python seen_state.py today > state/last_run.txt
git add state/seen.json state/awards.json state/last_run.txt
git diff --cached --quiet || git -c user.name="ntis-routine" -c user.email="routine@x2r.kr" commit -m "state: $(python seen_state.py today) 판정 기록"
git pull --rebase origin main && git push origin main
```
- `out/` 등 다른 파일은 절대 커밋하지 마세요. push가 실패하면 `git pull --rebase` 후 한 번 더 시도하고, 그래도 실패하면 최종 응답에 명시하세요.

## 5. 마무리
- 최종 응답에 소스별 수집 건수(제외 건수 포함), 판정 분포, 발송 결과, 엑셀 정리 결과(추가·고침 줄 수 또는 건너뜀·실패), seen.json 커밋 여부를 요약하세요.
