/**
 * 적합 공고 엑셀·첨부를 구글 드라이브 폴더에 올리는 웹앱 (sheet_export.py가 호출).
 * 설치 방법은 README.md "구글 드라이브 연결" 참고. 이 웹앱은 아래 폴더 안에서만 파일을 만들고 고친다.
 */
const FOLDER = '여기에 드라이브 폴더 링크를 붙여 넣으세요';   // 예: https://drive.google.com/drive/folders/1AbC...

const XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';
const ATTACH = '첨부파일';   // 공고별 첨부 폴더를 모아 두는 폴더 — 위 폴더에는 엑셀과 이 폴더만 남는다

function root_() {
  return DriveApp.getFolderById(FOLDER.match(/[-\w]{25,}/)[0]);
}

/** 공고별 첨부 폴더: 첨부파일 폴더 안에서 찾고, 예전에 바깥에 만들어진 것은 안으로 옮기고, 없으면 만든다. */
function noticeFolder_(root, name) {
  let it = root.getFoldersByName(ATTACH);
  const base = it.hasNext() ? it.next() : root.createFolder(ATTACH);
  it = base.getFoldersByName(name);
  if (it.hasNext()) return it.next();
  it = root.getFoldersByName(name);
  if (it.hasNext() && name !== ATTACH) return it.next().moveTo(base);
  return base.createFolder(name);
}

/** 처음 한 번 실행해 권한을 승인한다. 실행 로그에 폴더 이름이 나오면 연결된 것. */
function setup() {
  Logger.log('연결된 폴더: ' + root_().getName());
}

function doPost(e) {
  let out;
  try {
    out = handle_(JSON.parse(e.postData.contents));
  } catch (err) {
    out = {error: String(err)};
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

function handle_(q) {
  const root = root_();
  if (q.action === 'get') {            // 엑셀 내려받기
    const it = root.getFilesByName(q.name);
    if (!it.hasNext()) return {exists: false};
    return {exists: true, b64: Utilities.base64Encode(it.next().getBlob().getBytes())};
  }
  if (q.action === 'put') {            // 엑셀 올리기 — 이미 있으면 같은 파일의 내용만 바꿔 링크를 유지
    const blob = Utilities.newBlob(Utilities.base64Decode(q.b64), XLSX, q.name);
    const it = root.getFilesByName(q.name);
    if (!it.hasNext()) return {url: root.createFile(blob).getUrl()};
    const f = it.next();
    try {
      Drive.Files.update({}, f.getId(), blob, {supportsAllDrives: true});   // 편집기 왼쪽 "서비스"에 Drive API를 추가해야 동작 (공유 드라이브 안의 파일은 supportsAllDrives 필요)
      return {url: f.getUrl()};
    } catch (err) {                    // Drive 서비스가 없으면 새 파일로 교체 (파일 링크가 바뀜)
      const url = root.createFile(blob).getUrl();
      f.setTrashed(true);
      return {url: url, recreated: String(err)};
    }
  }
  if (q.action === 'folder') {         // 공고별 첨부 폴더 (첨부파일 폴더 안에 만든다)
    return {url: noticeFolder_(root, q.name).getUrl(), parent: ATTACH};
  }
  if (q.action === 'upload') {         // 첨부 올리기 — 같은 이름이 이미 있으면 그대로 둔다 (replace면 내용 교체)
    const folder = noticeFolder_(root, q.folder);
    const blob = Utilities.newBlob(Utilities.base64Decode(q.b64), q.mime || 'application/octet-stream', q.name);
    const it = folder.getFilesByName(q.name);
    if (!it.hasNext()) return {url: folder.createFile(blob).getUrl()};
    const f = it.next();
    if (!q.replace) return {url: f.getUrl(), existed: true};
    try {                                // replace: 같은 파일의 내용만 바꿔 링크를 유지 (공고내용.txt)
      Drive.Files.update({}, f.getId(), blob, {supportsAllDrives: true});
      return {url: f.getUrl(), replaced: true};
    } catch (err) {
      const url = folder.createFile(blob).getUrl();
      f.setTrashed(true);
      return {url: url, recreated: String(err)};
    }
  }
  throw new Error('알 수 없는 요청: ' + q.action);
}
