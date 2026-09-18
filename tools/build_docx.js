const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, AlignmentType, ShadingType, BorderStyle, LevelFormat, PageNumber, Footer, Header,
} = require("docx");

const SRC = process.argv[2];
const OUT = process.argv[3];
const md = fs.readFileSync(SRC, "utf-8").replace(/\r\n/g, "\n").replace(/✅/g, "○").replace(/❌/g, "✕");
const FONT = "Malgun Gothic";
const PAGE_W = 11906, MARGIN = 1134, CONTENT_W = PAGE_W - MARGIN * 2; // A4, 2cm margins

// ---- inline markdown → TextRuns (bold, code, ✅/❌ kept as text)
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), font: FONT, ...base }));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(new TextRun({ text: tok.slice(2, -2), bold: true, font: FONT, ...base }));
    else out.push(new TextRun({ text: tok.slice(1, -1), font: "Consolas", shading: { type: ShadingType.CLEAR, fill: "F2F2F2" }, ...base }));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), font: FONT, ...base }));
  return out;
}

function cell(text, { header = false, width, fill } = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: fill ? { type: ShadingType.CLEAR, fill, color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 90, right: 90 },
    verticalAlign: "center",
    children: [new Paragraph({ children: runs(text, { bold: header, size: header ? 19 : 18 }), spacing: { before: 0, after: 0 } })],
  });
}

function table(rows) {
  const ncol = rows[0].length;
  // 열 너비: 첫 열은 좁게, 마지막 열은 넓게
  let widths;
  if (ncol === 3) widths = [Math.round(CONTENT_W * 0.22), Math.round(CONTENT_W * 0.30), 0];
  else if (ncol === 2) widths = [Math.round(CONTENT_W * 0.3), 0];
  else widths = Array(ncol).fill(Math.floor(CONTENT_W / ncol));
  const used = widths.slice(0, -1).reduce((a, b) => a + b, 0);
  widths[ncol - 1] = CONTENT_W - used;
  const border = { style: BorderStyle.SINGLE, size: 4, color: "BFBFBF" };
  return new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: widths,
    borders: { top: border, bottom: border, left: border, right: border, insideHorizontal: border, insideVertical: border },
    rows: rows.map((r, i) => new TableRow({
      tableHeader: i === 0,
      children: r.map((c, j) => cell(c, { header: i === 0, width: widths[j], fill: i === 0 ? "DCE6F1" : undefined })),
    })),
  });
}

// ---- parse markdown blocks
const lines = md.split("\n");
const children = [];
let i = 0;
let titleDone = false;
while (i < lines.length) {
  const ln = lines[i];
  if (!ln.trim()) { i++; continue; }
  if (ln.startsWith("# ") && !titleDone) {
    titleDone = true;
    children.push(new Paragraph({ children: runs(ln.slice(2), { size: 36, bold: true }), alignment: AlignmentType.CENTER, spacing: { after: 120 } }));
    children.push(new Paragraph({ children: [new TextRun({ text: `작성일 ${new Date().toLocaleDateString("ko-KR")} · 공고 적합성 자동 판정용 기준 문서`, font: FONT, size: 18, color: "666666" })], alignment: AlignmentType.CENTER, spacing: { after: 240 } }));
    i++; continue;
  }
  if (ln.startsWith("## ")) { children.push(new Paragraph({ children: runs(ln.slice(3), { size: 26, bold: true, color: "1F3864" }), heading: HeadingLevel.HEADING_1, spacing: { before: 320, after: 120 } })); i++; continue; }
  if (ln.startsWith("### ")) { children.push(new Paragraph({ children: runs(ln.slice(4), { size: 22, bold: true, color: "2E5395" }), heading: HeadingLevel.HEADING_2, spacing: { before: 200, after: 80 } })); i++; continue; }
  if (ln.startsWith("> ")) {
    children.push(new Paragraph({ children: runs(ln.slice(2), { size: 18, italics: true, color: "595959" }), indent: { left: 360 }, border: { left: { style: BorderStyle.SINGLE, size: 12, color: "9DC3E6", space: 8 } }, spacing: { after: 60 } }));
    i++; continue;
  }
  if (ln.startsWith("|")) {
    const rows = [];
    while (i < lines.length && lines[i].startsWith("|")) {
      const cells = lines[i].slice(1, -1).split("|").map(s => s.trim());
      if (!cells.every(c => /^-+$/.test(c))) rows.push(cells);
      i++;
    }
    children.push(table(rows));
    children.push(new Paragraph({ spacing: { after: 80 } }));
    continue;
  }
  const bullet = ln.match(/^(\s*)- (.*)$/);
  if (bullet) {
    const level = Math.min(2, Math.floor(bullet[1].length / 2));
    children.push(new Paragraph({ children: runs(bullet[2], { size: 20 }), numbering: { reference: "bullets", level }, spacing: { after: 40 } }));
    i++; continue;
  }
  children.push(new Paragraph({ children: runs(ln, { size: 20 }), spacing: { after: 100 } }));
  i++;
}

const doc = new Document({
  creator: "X2R",
  title: "X2R 회사·기술 프로필",
  styles: { default: { document: { run: { font: FONT, size: 20 } } } },
  numbering: {
    config: [{
      reference: "bullets",
      levels: [0, 1, 2].map(l => ({ level: l, format: LevelFormat.BULLET, text: ["•", "◦", "▪"][l], alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 360 + 360 * l, hanging: 240 } } } })),
    }],
  },
  sections: [{
    properties: { page: { size: { width: PAGE_W, height: 16838 }, margin: { top: MARGIN, bottom: MARGIN, left: MARGIN, right: MARGIN } } },
    headers: { default: new Header({ children: [new Paragraph({ children: [new TextRun({ text: "엑스투알㈜ 회사·기술 프로필 — 공고 적합성 판정 기준", font: FONT, size: 16, color: "808080" })], alignment: AlignmentType.RIGHT })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ children: [new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 16, color: "808080" })], alignment: AlignmentType.CENTER })] }) },
    children,
  }],
});

Packer.toBuffer(doc).then(buf => { fs.writeFileSync(OUT, buf); console.log("wrote", OUT, buf.length); });
