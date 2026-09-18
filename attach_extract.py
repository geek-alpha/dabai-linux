"""聊天附件正文抽取：让模型直接读到常见文档的内容，而不是只拿到一个路径。

为什么要有这一层：非图片附件原来只给模型「路径 + 用 read_file 打开」。
read_file 读 .docx/.xlsx/.pdf 只会拿到二进制垃圾——模型等于收到一个打不开的箱子。
这里在附件进提示词之前把正文抽出来：图片走 [[IMG:]] 看像素，文档走文本看内容。

依赖策略：pypdf 负责 PDF（能解 ToUnicode CMap，中文 PDF 也抽得出）；
docx/xlsx/pptx 本身就是 zip+XML，用标准库直接解，不为它们引依赖。
抽不出（扫描件 PDF、加密文档）返回空正文 + 一句说明，调用方回退成「给路径」。
"""
from __future__ import annotations

import html
import re
import zipfile
from pathlib import Path

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".sql",
    ".sh", ".bash", ".bat", ".ps1", ".py", ".rb", ".php", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".cs", ".css", ".scss", ".vue", ".srt", ".vtt", ".tex",
}
DOCX_EXT = {".docx"}
XLSX_EXT = {".xlsx", ".xlsm"}
PPTX_EXT = {".pptx"}
PDF_EXT = {".pdf"}
EXTRACTABLE = TEXT_EXT | DOCX_EXT | XLSX_EXT | PPTX_EXT | PDF_EXT

# 单个 XML 条目/PDF 页数上限：附件是用户上传的，zip 炸弹和 500 页 PDF 不能拖垮进程
_MAX_ZIP_ENTRY = 12_000_000
_MAX_PDF_PAGES = 40
_MAX_SHEET_ROWS = 2000

_BLANK_RE = re.compile(r"[ \t]*\n[ \t]*\n+")


def extract(path: Path, limit: int = 6000) -> tuple[str, str]:
    """抽取附件正文。返回 (正文, 给模型看的一句状态)；抽不出时正文为空串。"""
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return _text_file(path, limit)
        if ext in DOCX_EXT:
            return _ooxml(path, limit, r"^word/document\.xml$", "文档正文")
        if ext in PPTX_EXT:
            return _ooxml(path, limit, r"^ppt/slides/slide\d+\.xml$", "幻灯片文字")
        if ext in XLSX_EXT:
            return _xlsx(path, limit)
        if ext in PDF_EXT:
            return _pdf(path, limit)
    except Exception as e:  # 抽取只是锦上添花，绝不能因为一个坏文件打断整条消息
        return "", f"正文抽取失败（{type(e).__name__}）"
    return "", ""


# ===== 纯文本 =====

def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", "replace")  # 国内 txt/csv 常见编码


def _text_file(path: Path, limit: int) -> tuple[str, str]:
    body, cut = _clip(_decode(path.read_bytes()[: limit * 6 + 4096]), limit)
    if not body:
        return "", "文件是空的"
    return body, f"已读取全文{'（截断）' if cut else ''}"


# ===== docx / pptx（zip + XML） =====

def _xml_text(xml: str) -> str:
    xml = re.sub(r"<w:br\s*/>|<a:br\s*/>", "\n", xml)
    xml = re.sub(r"</(?:w:p|a:p|w:tr|w:tc|a:t)>", "\n", xml)
    xml = re.sub(r"<w:tab\s*/>", "\t", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def _ooxml(path: Path, limit: int, pattern: str, label: str) -> tuple[str, str]:
    parts = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(n for n in z.namelist() if re.match(pattern, n)):
            if z.getinfo(name).file_size > _MAX_ZIP_ENTRY:
                continue
            parts.append(_xml_text(z.read(name).decode("utf-8", "replace")))
    body, cut = _clip("\n".join(parts), limit)
    if not body:
        return "", f"{label}是空的"
    return body, f"已抽取{label}{'（截断）' if cut else ''}"


# ===== xlsx（共享字符串 + 行单元格） =====

def _cells(row_xml: str, shared: list[str]) -> str:
    out = []
    for m in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row_xml, re.S):
        attrs, inner = m.group(1), m.group(2) or ""
        tm = re.search(r't="([^"]+)"', attrs)
        v = re.search(r"<v>(.*?)</v>", inner, re.S)
        if tm and tm.group(1) == "s" and v:  # 共享字符串表下标
            try:
                out.append(shared[int(v.group(1))].strip())
            except (ValueError, IndexError):
                out.append("")
        elif tm and tm.group(1) == "inlineStr":
            out.append(_xml_text(inner).strip())
        else:
            out.append(html.unescape(v.group(1)).strip() if v else "")
    return "\t".join(out)


def _xlsx(path: Path, limit: int) -> tuple[str, str]:
    lines: list[str] = []
    with zipfile.ZipFile(path) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            shared = [_xml_text(s) for s in re.findall(r"<si>(.*?)</si>", sx, re.S)]
        sheets = sorted(n for n in z.namelist() if re.match(r"^xl/worksheets/sheet\d+\.xml$", n))
        for name in sheets:
            if z.getinfo(name).file_size > _MAX_ZIP_ENTRY:
                continue
            xml = z.read(name).decode("utf-8", "replace")
            lines.append(f"[工作表 {name.rsplit('/', 1)[-1]}]")
            rows = re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S)[:_MAX_SHEET_ROWS]
            lines.extend(_cells(r, shared) for r in rows)
    body, cut = _clip("\n".join(lines), limit)
    if not body:
        return "", "表格是空的"
    return body, f"已抽取 {len(lines)} 行表格{'（截断）' if cut else ''}"


# ===== pdf =====

def _pdf(path: Path, limit: int) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF 抽取需要 pypdf（未安装）"
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return "", "PDF 有密码，抽不出文字"
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        if i >= _MAX_PDF_PAGES or sum(len(p) for p in pages) > limit * 2:
            break
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    body, cut = _clip("\n".join(pages), limit)
    if not body:
        return "", f"PDF 共 {len(reader.pages)} 页但抽不出文字（多半是扫描件），只能用路径"
    return body, f"已抽取 PDF 前 {len(pages)} 页{'（截断）' if cut else ''}"


# ===== 通用 =====

def _clip(text: str, limit: int) -> tuple[str, bool]:
    text = _BLANK_RE.sub("\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if len(text) <= limit:
        return text, False
    return text[:limit], True
