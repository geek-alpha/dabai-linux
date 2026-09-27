import ftplib
import json
import socket
import ssl
import os
from pathlib import Path

# 凭据只从 data/bambu_lab.json 读（/data/ 被 .gitignore 忽略）——代码里不放真值
_ROOT = Path(__file__).resolve().parents[3]
_CFG = json.loads((_ROOT / "data" / "bambu_lab.json").read_text(encoding="utf-8"))
HOST = _CFG["host"]
USER = _CFG.get("user", "bblp")
PWD = _CFG["access_code"]
LOCAL = str(_ROOT / "data" / "bf_final.3mf")
REMOTE = "baifengfeng_100mm.3mf"


def log(*a):
    print(*a, flush=True)


class ImplicitFTPS(ftplib.FTP_TLS):
    def connect(self, host="", port=0, timeout=-999, source_address=None):
        self.host = host
        self.port = port or 990
        self.sock = socket.create_connection((host, self.port), 10)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

f = ImplicitFTPS(context=ctx)
f.connect(HOST)
f.login(USER, PWD)
f.prot_p()
log("pwd=", f.pwd())

for target in ("/sdcard", "/cache", "/"):
    try:
        f.cwd(target)
    except Exception as e:
        log("CWD", target, "FAIL", type(e).__name__, e)
        continue
    log("CWD", target, "OK -> now", f.pwd())
    size = os.path.getsize(LOCAL)
    with open(LOCAL, "rb") as fh:
        try:
            f.storbinary(f"STOR {REMOTE}", fh, blocksize=65536)
            log("STOR OK ->", target, REMOTE, size, "bytes")
            log("LIST", f.nlst())
            break
        except Exception as e:
            log("STOR FAIL", target, type(e).__name__, e)

f.quit()
