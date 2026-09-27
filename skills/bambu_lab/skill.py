# -*- coding: utf-8 -*-
"""拓竹 3D 打印技能（bambu_lab）—— 把实机踩过的坑固化成可执行代码。

三条硬约束写进实现，不靠记忆：
1. FTPS 登录后必须 prot_p()。FTP_TLS.login 默认发 PROT C（声明数据连接明文），
   拓竹要求 PROT P；收到 PROT C 后一旦发起数据操作就断控制连接，客户端读响应
   拿到空串抛 EOFError —— 看着像「数据通道坏了」，实为加密协商被拒。
2. 单次连接、失败即退。端口超时/RST 后连续重试会把相邻端口一起拖死：实测连打
   8883 四十次后，原本正常的 990 也跟着超时，整机从 ARP 表消失。
3. 上传后必须回读校验。SIZE 显示完整不代表内容完整（可能只是预分配）；且拓竹
   收完最后一段常常不回 226，storbinary 会一直等到超时——这是已知行为，不是失败。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import socket
import ssl
import subprocess
import sys
import zipfile
from ftplib import FTP_TLS
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parents[2]
_CFG_PATH = _BASE_DIR / "data" / "bambu_lab.json"
# 凭据不入库：host / access_code 只从 data/bambu_lab.json 读（/data/ 被 .gitignore 忽略）。
# 这里只放缺省结构，空值由 bambu_ftp 拦下来报错，别让默认值变成别人的可用凭据。
_DEFAULT_CFG = {
    "host": "",
    "port": 990,
    "user": "bblp",
    "access_code": "",
    "cache_dir": "/cache",
    "model": "Bambu Lab A1",
    "nozzle": "0.4",
    "ports": [990, 8883, 3000, 322],
}


def _cfg() -> dict:
    """配置优先读 data/bambu_lab.json：代码会被版本更新覆盖，data/ 下的配置不会。"""
    cfg = dict(_DEFAULT_CFG)
    try:
        cfg.update(json.loads(_CFG_PATH.read_text(encoding="utf-8")) or {})
    except Exception:
        pass
    return cfg


class _ImplicitFTPS(FTP_TLS):
    """隐式 FTPS：连上即 TLS（990 端口），不发 AUTH TLS。"""

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host = host
        self.port = port or 990
        self.sock = socket.create_connection(
            (host, self.port), 10 if timeout == -999 else timeout)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


def _connect(cfg: dict):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    f = _ImplicitFTPS(context=ctx)
    f.connect(cfg["host"], int(cfg["port"]), 10)
    f.login(cfg["user"], cfg["access_code"])
    f.prot_p()  # 硬约束 1：缺这行，之后所有数据操作都报 EOFError
    return f


def _bye(f) -> None:
    try:
        f.quit()
    except Exception:
        try:
            f.close()
        except Exception:
            pass


def _hsize(n) -> str:
    if not isinstance(n, int):
        return "?"
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}GB"


def _md5_remote(f, remote: str):
    h = hashlib.md5()
    try:
        f.retrbinary(f"RETR {remote}", h.update, blocksize=262144)
    except Exception:
        return None
    return h.hexdigest()


# ---------------- bambu_ftp ----------------

def bambu_ftp(args) -> str:
    cfg = _cfg()
    if args.get("host"):
        cfg["host"] = str(args["host"]).strip()
    if args.get("access_code"):
        cfg["access_code"] = str(args["access_code"]).strip()
    action = str(args.get("action") or "probe").strip().lower()
    path = str(args.get("path") or "").strip()
    local = str(args.get("local") or "").strip()
    if not (cfg.get("host") and cfg.get("access_code")):
        return (f"✘ 缺 host / access_code：写进 {_CFG_PATH}（该文件不入库），"
                "别写回 skill.py 的 _DEFAULT_CFG")
    try:
        f = _connect(cfg)
    except Exception as e:
        return (f"✘ 连不上 {cfg['host']}:{cfg['port']} —— {type(e).__name__}: {e}\n"
                "别重试轰炸（会把整机打掉线），先 bambu_net_probe 看它还在不在网上。")
    try:
        if action == "probe":
            return (f"✔ FTPS 就绪 {cfg['host']}:{cfg['port']}｜{f.welcome}｜"
                    f"pwd={f.pwd()!r}（已 prot_p）")
        if action == "list":
            target = path or cfg["cache_dir"]
            names = f.nlst(target)
            lines = [f"✔ {target} 共 {len(names)} 项（{cfg['host']}）"]
            for n in names:
                full = n if n.startswith("/") else f"{target.rstrip('/')}/{n}"
                lines.append(f"   {Path(n).name:<34} {_hsize(f.size(full)) if _is_file(f, full) else '<目录>'}")
            return "\n".join(lines)
        if action == "size":
            if not path:
                return "✘ size 需要 path，例如 /cache/bf_final.3mf"
            n = f.size(path)
            if n is None:
                return f"✘ 不存在或无权限：{path}"
            return f"✔ {path} → {n} 字节（{_hsize(n)}）"
        if action == "download":
            if not (path and local):
                return "✘ download 需要 path 和 local"
            p = Path(local)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "wb") as fh:
                f.retrbinary(f"RETR {path}", fh.write, blocksize=262144)
            got = p.stat().st_size
            return f"✔ 已下载 {path} → {p}（{got} 字节 / {_hsize(got)}）"
        if action == "md5":
            if not path:
                return "✘ md5 需要 path（可选 local 做对比）"
            remote_md5 = _md5_remote(f, path)
            if remote_md5 is None:
                return f"✘ 读不到远端文件：{path}"
            msg = f"远端 {path} md5 = {remote_md5}"
            if local and Path(local).is_file():
                local_md5 = hashlib.md5(Path(local).read_bytes()).hexdigest()
                same = "一致 ✔" if local_md5 == remote_md5 else "不一致 ✘"
                msg += f"\n本地 {local} md5 = {local_md5} → {same}"
            return msg
        if action == "delete":
            if args.get("confirm") not in (True, "true", "True", 1, "1", "yes"):
                return "✘ 删除不可逆：确认后传 confirm=true"
            if not path:
                return "✘ delete 需要 path"
            f.delete(path)
            left = f.size(path)
            return (f"✔ 已删除 {path}" if left is None else f"⚠ 删除后 SIZE 仍返回 {left}，未生效")
        if action == "upload":
            if not (local and Path(local).is_file()):
                return f"✘ 本地文件不存在：{local}"
            return _do_upload(cfg, f, Path(local), path or f"/{Path(local).name}")
        return f"✘ 未知 action：{action}"
    except Exception as e:
        return f"✘ {action} 失败：{type(e).__name__}: {e}"
    finally:
        _bye(f)


def _is_file(f, remote: str) -> bool:
    try:
        return f.size(remote) is not None
    except Exception:
        return False


def _do_upload(cfg: dict, f, local: Path, remote: str) -> str:
    data = local.read_bytes()
    note = ""
    try:
        f.voidcmd("TYPE I")
        f.sock.settimeout(90)
        f.storbinary(f"STOR {remote}", io.BytesIO(data), blocksize=262144)
    except (socket.timeout, TimeoutError, EOFError, OSError) as e:
        # 硬约束 3：拓竹收完最后一段常不回 226，超时是已知行为，结论交给回读校验
        note = f"服务端未回 226（{type(e).__name__}）——已知行为，看回读结果"
    _bye(f)
    # 控制连接被超时污染后残留响应不可信，回读用新连接
    try:
        f2 = _connect(cfg)
    except Exception as e:
        return f"⚠ 已发送 {len(data)} 字节但回读连接失败：{e}（别重传，先 bambu_ftp size 探一下）"
    try:
        size = f2.size(remote)
        back = _md5_remote(f2, remote)
    finally:
        _bye(f2)
    local_md5 = hashlib.md5(data).hexdigest()
    ok = size == len(data) and back == local_md5
    head = "✔ 上传并校验通过" if ok else "⚠ 上传后校验不通过"
    return (f"{head}：{remote}\n"
            f"   本地 {len(data)} 字节 md5={local_md5}\n"
            f"   远端 {size} 字节 md5={back}\n"
            f"{'   ' + note if note else ''}").rstrip()


# ---------------- bambu_model_check ----------------

def _pick(text: str, key: str) -> str:
    """取字段值。单值字段是字符串，nozzle_diameter / filament_type 是数组，两种都要吃得下。"""
    m = re.search(r'"%s"\s*:\s*(?:\[\s*)?"([^"]*)"' % re.escape(key), text)
    return m.group(1).strip() if m else ""


def _meta(xml: str, key: str) -> str:
    """slice_info.config 是 XML，形如 <metadata key="prediction" value="30407"/>。"""
    m = re.search(r'<metadata key="%s" value="([^"]*)"' % re.escape(key), xml)
    return m.group(1).strip() if m else ""


def bambu_model_check(args) -> str:
    files = [x.strip() for x in str(args.get("file") or "").replace("\n", ",").split(",") if x.strip()]
    if not files:
        return "✘ 需要 file（本地 .3mf 路径）"
    blocks = []
    for fp in files:
        p = Path(fp)
        if not p.is_file():
            blocks.append(f"✘ 不存在：{fp}")
            continue
        try:
            with zipfile.ZipFile(p) as z:
                names = z.namelist()
                txt = z.read("Metadata/project_settings.config").decode("utf-8", "replace")
                slice_txt = ""
                for cand in ("Metadata/slice_info.config", "Metadata/slice_info.xml"):
                    if cand in names:
                        slice_txt = z.read(cand).decode("utf-8", "replace")
                        break
        except Exception as e:
            blocks.append(f"✘ 读不了 {fp}：{type(e).__name__}: {e}")
            continue
        model = _pick(txt, "printer_model")
        sid = _pick(txt, "printer_settings_id")
        # slice_info.config 是 XML：字段形如 <metadata key="prediction" value="30407"/>
        nozzle = _meta(slice_txt, "nozzle_diameters") or _pick(txt, "nozzle_diameter")
        # 属性前必须是空白：否则会先咬到 volume_type="Standard" 里的 type=
        fm = re.search(r'<filament\b[^>]*?\stype="([^"]+)"', slice_txt)
        fila = fm.group(1) if fm else _pick(txt, "filament_type")
        pred = _meta(slice_txt, "prediction")
        weight = _meta(slice_txt, "weight")
        gcodes = [n for n in names if n.lower().endswith(".gcode")]
        plates = [n for n in names if "plate" in n.lower()]
        lines = [f"📦 {p.name}（{_hsize(p.stat().st_size)}）",
                 f"   机型字段 printer_model   : {model or '未读到'}",
                 f"   切片配置 printer_settings: {sid or '未读到'}",
                 f"   喷嘴 {nozzle or '?'}｜耗材 {fila or '?'}"]
        if pred.isdigit():
            secs = int(pred)
            lines.append(f"   预估耗时 {secs} 秒 ≈ {secs / 3600:.1f} 小时")
        if weight:
            lines.append(f"   预估耗材 {weight} g（{fila or '?'}）")
        lines.append(f"   内含 gcode {len(gcodes)} 个｜plate 文件 {len(plates)} 个")
        if model and "mini" in model.lower() and "A1" in model:
            lines.append("   ⚠ 这是 A1 mini 的切片，A1 不能直接用，必须重切")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------- bambu_net_probe ----------------

def bambu_net_probe(args) -> str:
    cfg = _cfg()
    host = str(args.get("host") or cfg["host"]).strip()
    ports = [int(x) for x in str(args.get("ports") or ",".join(str(p) for p in cfg["ports"])).replace(" ", "").split(",") if x]
    win = sys.platform.startswith("win")
    ping = ["ping", "-n", "1", "-w", "1500", host] if win else ["ping", "-c", "1", "-W", "2", host]
    lines = [f"🔍 {host} 单次探测（绝不重试）"]
    enc = "gbk" if win else "utf-8"

    def _out(cmd) -> str:
        # 中文 Windows 控制台输出是 GBK：text=True 会按 utf-8 解码，在读取线程里
        # 抛 UnicodeDecodeError——异常被吞、stdout 变空，表现为「ARP 那行凭空消失」。
        return subprocess.run(cmd, capture_output=True, timeout=8).stdout.decode(enc, "replace")

    try:
        r = subprocess.run(ping, capture_output=True, timeout=8)
        lines.append(f"   ping : {'通' if r.returncode == 0 else '不通'}")
    except Exception as e:
        lines.append(f"   ping : 探测失败 {type(e).__name__}")
    try:
        arp = _out(["arp", "-a"])
        hit = [l.strip() for l in arp.splitlines() if host in l]
        lines.append(f"   ARP  : {hit[0] if hit else '无表项（说明它根本没在这张网里说过话）'}")
    except Exception as e:
        lines.append(f"   ARP  : 探测失败 {type(e).__name__}")
    for p in ports:
        s = socket.socket()
        s.settimeout(3)
        try:
            code = s.connect_ex((host, p))
            lines.append(f"   端口 {p:<5}: {'OPEN' if code == 0 else 'CLOSED'}")
        except Exception as e:
            lines.append(f"   端口 {p:<5}: 探测异常 {type(e).__name__}")
        finally:
            s.close()
    lines.append("   提示：OPEN 的 8883 在拓竹机型上也可能是路由器自身；判断是不是打印机看 ARP 的 MAC。")
    return "\n".join(lines)


HANDLERS = {
    "bambu_ftp": bambu_ftp,
    "bambu_model_check": bambu_model_check,
    "bambu_net_probe": bambu_net_probe,
}
