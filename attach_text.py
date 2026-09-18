"""附件内容抽取：把用户发来的文件变成模型能直接读的文本。

设计原则：能低成本转成文本的（纯文本/代码/表格/PDF/Office）直接内联进上下文，
模型当轮就能处理，不用先调工具读一遍；给不出内容的（图片、压缩包、音视频、
超大文件）才退化成「路径 + 工具按需读」。

零新增依赖：PDF 走 pypdf（venv 已有），docx/xlsx/pptx 用标准库 zipfile 解 XML。
"""
import re
import zipfile
from pathlib import Path

# 明确是纯文本的扩展名：嗅探失败也按文本处理（用户自己知道发的是什么）
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".xml",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".sql", ".sh", ".bat",
    ".ps1", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".lua", ".r", ".m", ".css",
    ".scss", ".vue", ".svelte", ".diff", ".patch", ".srt", ".vtt", ".tex",
}
OOXML_EXT = {".docx", ".xlsx", ".xlsm", ".pptx"}
SNIFF_BYTES = 8192


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
             .replace("&apos;", "'").replace("&#39;", "'").replace("&amp;", "&"))


def decode_text(data: bytes, relaxed: bool = False):
    """字节 → 文本；判定为二进制（含 NUL / 控制字符过多）时返回 None。

    relaxed=True 用于「只读了文件开头一截」的场景：多字节字符可能被切碎，
    严格解码会抛异常，改用 ignore 丢尾字节。
    """
    err = "ignore" if relaxed else "strict"
    head = data[:SNIFF_BYTES]
    if b"\x00" in head:
        return None
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(enc, err)
        except UnicodeDecodeError:
            continue
    # latin-1 永不抛异常，所以先确认可打印比例够高，避免把二进制解成一堆乱码
    text = data.decode("latin-1")
    sample = text[:SNIFF_BYTES]
    if sample:
        bad = sum(1 for ch in sample if ch < " " and ch not in "\t\n\r")
        if bad / len(sample) > 0.05:
            return None
    return text


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return ""
    out = []
    for i, page in enumerate(reader.pages[:200]):
        try:
            body = (page.extract_text() or "").strip()
        except Exception:
            body = ""
        if body:
            out.append(f"--- 第 {i + 1} 页 ---\n{body}")
    return "\n\n".join(out)


def _ooxml_text(path: Path, ext: str) -> str:
    if ext == ".xlsx" or ext == ".xlsm":
        return _xlsx_text(path)
    if ext == ".docx":
        pattern, part_re = r"<w:t[^>]*>(.*?)</w:t>", r"word/document\.xml$"
    else:  # .pptx
        pattern, part_re = r"<a:t>(.*?)</a:t>", r"ppt/slides/slide\d+\.xml$"
    with zipfile.ZipFile(path) as z:
        parts = sorted(n for n in z.namelist() if re.search(part_re, n))
        out = []
        for n in parts[:60]:
            raw = z.read(n).decode("utf-8", "ignore")
            chunks = [_unescape(c) for c in re.findall(pattern, raw, re.S)]
            body = "".join(chunks).strip() if ext == ".docx" else "\n".join(
                c for c in chunks if c.strip())
            if body:
                out.append(body if ext == ".docx" else f"--- {Path(n).stem} ---\n{body}")
        return "\n\n".join(out)


def _xlsx_text(path: Path) -> str:
    """按行还原成 TSV：模型看表格，TSV 比 XML 省 token 且能直接读懂结构。"""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            raw = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            # 一个 <si> 是一格字符串，内部可能有多个 <t>（富文本分片）
            for si in re.findall(r"<si>(.*?)</si>", raw, re.S):
                shared.append(_unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
        sheets = sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
        out = []
        for n in sheets[:5]:
            raw = z.read(n).decode("utf-8", "ignore")
            rows = []
            for row in re.findall(r"<row[^>]*>(.*?)</row>", raw, re.S):
                cells = []
                for attrs, body in re.findall(r"<c([^>]*)>(.*?)</c>", row, re.S):
                    v = re.search(r"<v>(.*?)</v>", body, re.S)
                    if v:
                        val = _unescape(v.group(1))
                        if 't="s"' in attrs:
                            try:
                                val = shared[int(val)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(val)
                    else:
                        it = re.search(r"<t[^>]*>(.*?)</t>", body, re.S)
                        cells.append(_unescape(it.group(1)) if it else "")
                if any(c.strip() for c in cells):
                    rows.append("\t".join(cells))
            if rows:
                out.append(f"--- {Path(n).stem} ---\n" + "\n".join(rows))
        return "\n\n".join(out)


def extract(path: Path, limit: int):
    """抽取文件正文。返回 (文本, 说明)；给不出内容返回 (None, 原因)。"""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            body = _pdf_text(path)
            if not body:
                return None, "PDF 里没有可提取的文字（可能是扫描件，要看图请转成图片发）"
            return body, f"PDF 共 {len(body)} 字符"
        if ext in OOXML_EXT:
            body = _ooxml_text(path, ext)
            if not body:
                return None, "文档里没有可提取的文字"
            return body, f"{ext.lstrip('.')} 文档"
        if ext in TEXT_EXT or ext == "":
            # 只读够用的字节：30MB 的日志没必要整个读进内存再截掉 99%。
            # UTF-8 中文 3 字节/字符，留 6 倍余量；切碎了用 ignore 解码
            raw = path.read_bytes()
            cut = len(raw) > limit * 6
            if cut:
                raw = raw[: limit * 6]
            body = decode_text(raw, relaxed=cut)
            if body is None:
                return None, "不是文本文件"
            return body, f"文本，{len(body)} 字符"
        # 扩展名不认识：读头部嗅探，是文本就当文本用（.log2 / 无扩展名 / 各种导出文件）
        head = path.read_bytes()[:SNIFF_BYTES * 4]
        if decode_text(head) is not None:
            raw = path.read_bytes()
            cut = len(raw) > limit * 6
            body = decode_text(raw[: limit * 6] if cut else raw, relaxed=cut)
            if body is not None:
                return body, f"按内容识别为文本，{len(body)} 字符"
        return None, f"{ext or '未知'} 二进制文件"
    except zipfile.BadZipFile:
        return None, "文件损坏或不是有效的 Office 文档"
    except Exception as e:
        return None, f"解析失败（{type(e).__name__}）"


def clamp(body: str, limit: int):
    """超长截断：给模型看清结构即可，需要更多它自己会去读原文件。"""
    if len(body) <= limit:
        return body, False
    return body[:limit], True
