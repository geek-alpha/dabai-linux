#!/usr/bin/env python3
"""resources.arsc 解析：id -> type/name 反查表。

不依赖 androguard（它对本机微信的 arsc 解析出 0 条）。只实现需要的部分：
全局字符串池 + 包内的 typeStrings/keyStrings 池 + ResTable_type 条目表。
ResTable_entry.key 就是资源名在 keyStrings 池里的下标，这是本表的全部秘密。

用法:
  python3 tools/resid_map.py build <resources.arsc> <out.json>
  python3 tools/resid_map.py lookup <out.json> 7f090158 7f09168c
"""
import json
import struct
import sys

RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC = 0x0202
RES_TABLE_LIBRARY = 0x0203

UTF8_FLAG = 0x000100
NO_ENTRY = 0xFFFFFFFF
FLAG_SPARSE = 0x01


def _len8(b: bytes, o: int):
    """UTF-8 池的可变长长度：最高位为续位标志。"""
    n = b[o]
    o += 1
    if n & 0x80:
        n = ((n & 0x7F) << 8) | b[o]
        o += 1
    return n, o


def _len16(b: bytes, o: int):
    n = struct.unpack_from("<H", b, o)[0]
    o += 2
    if n & 0x8000:
        n = ((n & 0x7FFF) << 16) | struct.unpack_from("<H", b, o)[0]
        o += 2
    return n, o


def parse_pool(b: bytes, o: int):
    """返回 (字符串列表, 下一个 chunk 的偏移)。"""
    _type, header_size, size = struct.unpack_from("<HHI", b, o)
    count, _style_count, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", b, o + 8)
    is_utf8 = bool(flags & UTF8_FLAG)
    offs = struct.unpack_from(f"<{count}I", b, o + header_size)
    base = o + strings_start
    out = []
    for off in offs:
        p = base + off
        if is_utf8:
            _n, p = _len8(b, p)
            blen, p = _len8(b, p)
            out.append(b[p:p + blen].decode("utf-8", "replace"))
        else:
            n, p = _len16(b, p)
            out.append(b[p:p + n * 2].decode("utf-16-le", "replace"))
    return out, o + size


def parse_type_chunk(b: bytes, o: int, key_pool, type_name, pkg_id, out):
    _type, header_size, size = struct.unpack_from("<HHI", b, o)
    type_id, flags, _res, entry_count, entries_start = struct.unpack_from("<BBHII", b, o + 8)
    # config 紧跟其后，长度可变；entry 偏移表从 o+header_size 开始
    off_base = o + header_size
    ent_base = o + entries_start
    for i in range(entry_count):
        if flags & FLAG_SPARSE:
            idx, eoff = struct.unpack_from("<HH", b, off_base + i * 4)
        else:
            eoff = struct.unpack_from("<I", b, off_base + i * 4)[0]
            idx = i
        if eoff == NO_ENTRY:
            continue
        eo = ent_base + eoff
        esize, eflags, key = struct.unpack_from("<HHI", b, eo)
        if key >= len(key_pool):
            continue
        res_id = (pkg_id << 24) | (type_id << 16) | idx
        out[f"{res_id:08x}"] = f"{type_name}/{key_pool[key]}"
        # 复杂条目（eflags & 0x0001）后面跟 ResTable_map_entry，本表不需要


def parse_package(b: bytes, o: int, out):
    _type, header_size, size = struct.unpack_from("<HHI", b, o)
    pkg_id = struct.unpack_from("<I", b, o + 8)[0]
    # ResTable_package: typeStrings@+268, lastPublicType@+272, keyStrings@+276
    type_strings_off = struct.unpack_from("<I", b, o + 268)[0]
    key_strings_off = struct.unpack_from("<I", b, o + 276)[0]
    type_pool, _ = parse_pool(b, o + type_strings_off)
    key_pool, _ = parse_pool(b, o + key_strings_off)
    end = o + size
    p = o + header_size
    while p + 8 <= end:
        ctype, _chs, csize = struct.unpack_from("<HHI", b, p)
        if csize == 0:
            break
        if ctype == RES_TABLE_TYPE:
            t_id = b[p + 8]
            t_name = type_pool[t_id - 1] if 0 < t_id <= len(type_pool) else f"0x{t_id:02x}"
            parse_type_chunk(b, p, key_pool, t_name, pkg_id, out)
        p += csize


def build(arsc_path: str) -> dict:
    b = open(arsc_path, "rb").read()
    out = {}
    p = 12  # 跳过 RES_TABLE 头
    while p + 8 <= len(b):
        ctype, _chs, csize = struct.unpack_from("<HHI", b, p)
        if csize == 0:
            break
        if ctype == RES_TABLE_PACKAGE:
            parse_package(b, p, out)
        p += csize
    return out


def from_device(pkg: str, out_path: str) -> dict:
    """从手机上的 APK 现场生成：pm path 拿 base.apk → 设备端 unzip 只抽 resources.arsc
    → pull 回来解析。只传 5MB 的 arsc，不拉几百 MB 的整个 APK。"""
    import subprocess as sp

    def sh(*args):
        r = sp.run(args, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(args[:3])} 失败: {r.stderr.strip()}")
        return r.stdout

    line = sh("adb", "shell", "pm", "path", pkg).strip()
    apk = line.split("package:")[-1].strip()
    if not apk:
        raise RuntimeError(f"{pkg} 未安装或 pm path 无输出")
    remote = f"/data/local/tmp/_resid_{pkg.split('.')[-1]}"
    sh("adb", "shell", f"rm -rf {remote}; mkdir -p {remote} && cd {remote} && "
                        f"unzip -o '{apk}' resources.arsc")
    local = "/tmp/_resid_pull.arsc"
    sh("adb", "pull", f"{remote}/resources.arsc", local)
    m = build(local)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, sort_keys=True)
    print(f"✓ {pkg}: {len(m)} 条 id -> 名称，写入 {out_path}")
    return m


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "build":
        m = build(sys.argv[2])
        with open(sys.argv[3], "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, sort_keys=True)
        print(f"✓ {len(m)} 条 id -> 名称，写入 {sys.argv[3]}")
    elif cmd == "from-device":
        from_device(sys.argv[2], sys.argv[3])
    elif cmd == "lookup":
        m = json.load(open(sys.argv[2], encoding="utf-8"))
        for q in sys.argv[3:]:
            print(f"{q} -> {m.get(q.lower(), '(未收录)')}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
