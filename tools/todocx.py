#!/usr/bin/env python3
"""Engine resume HTML -> styled .docx (python-docx); cover HTML (letter + 30-60-90) -> .docx (pandoc).
    python3 todocx.py <resume.html> <cover.html> <out_resume.docx> <out_cover.docx>"""
import html as H, re, subprocess, sys
from pathlib import Path
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

def clean(x):
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", x))).strip()

def resume_docx(src, out):
    t = Path(src).read_text(encoding="utf-8")
    t = re.sub(r"<(style|script|title)[^>]*>.*?</\1>", "", t, flags=re.S | re.I)
    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Inches(0.6); s.left_margin = s.right_margin = Inches(0.7)
    st = doc.styles["Normal"]; st.font.name = "Arial"; st.font.size = Pt(10.5)
    st.paragraph_format.space_after = Pt(2)
    grey = RGBColor(0x55, 0x55, 0x55); navy = RGBColor(0x1F, 0x3A, 0x5F)
    for m in re.finditer(r"<(h1|h2|h3|p|li|tr)\b[^>]*>(.*?)</\1>", t, re.S | re.I):
        tag, inner = m.group(1).lower(), m.group(2)
        if tag == "tr":
            cells = re.findall(r"<td[^>]*>(.*?)</td>", inner, re.S)
            if len(cells) >= 2:
                p = doc.add_paragraph(); r = p.add_run(clean(cells[0]) + ": "); r.bold = True; p.add_run(clean(cells[1]))
            continue
        text = clean(inner)
        if not text:
            continue
        if tag == "h1":
            p = doc.add_paragraph(); r = p.add_run(text); r.bold = True; r.font.size = Pt(20)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif tag == "h2":
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(10)
            r = p.add_run(text.upper()); r.bold = True; r.font.size = Pt(11); r.font.color.rgb = navy
        elif tag == "h3":
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(6); r = p.add_run(text); r.bold = True
        elif tag == "li":
            doc.add_paragraph(text, style="List Bullet")
        else:
            p = doc.add_paragraph()
            r = p.add_run(text)
            if "color:#555" in m.group(0):
                r.font.color.rgb = grey; r.italic = True
                if "|" in text and "@" in text:
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER; r.italic = False
    doc.save(out)

def cover_docx(src, out):
    t = Path(src).read_text(encoding="utf-8")
    letter, _, plan = t.partition("<!--PLAN-->")
    tmp = Path(out).with_suffix(".tmp.html")
    # letter, page break, plan; pandoc keeps headings, paragraphs and lists, drops the slide CSS
    tmp.write_text(letter.split("</body>")[0] + "<p>&nbsp;</p><h1>30-60-90 Day Plan</h1>" +
                   re.sub(r"<(style|script)[^>]*>.*?</\1>", "", plan, flags=re.S | re.I), encoding="utf-8")
    subprocess.run(["pandoc", "-f", "html", "-t", "docx", "-o", out, str(tmp)], check=True)
    tmp.unlink()

if __name__ == "__main__":
    resume_docx(sys.argv[1], sys.argv[3]); cover_docx(sys.argv[2], sys.argv[4]); print("ok", sys.argv[3], sys.argv[4])
