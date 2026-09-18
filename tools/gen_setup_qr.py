#!/usr/bin/env python3
"""生成「手机接入」二维码。

指向 HTTP 回源端口的 /setup，而不是 HTTPS 主站：手机此时还没装根证书，
扫 HTTPS 地址会先撞证书警告——先有鸡还是先有蛋。走 HTTP 打开引导页，
在页面里下载 CA、装完再跳 HTTPS 主站，全程零警告。

用法：
    python tools/gen_setup_qr.py            # 自动探测局域网 IP
    python tools/gen_setup_qr.py 192.168.1.9 8001
"""
import socket
import sys
from pathlib import Path

import qrcode

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "icons" / "setup-qr.png"


def lan_ip() -> str:
    """取默认出口网卡的地址。connect 走 UDP 不发包，只为让内核选出网卡。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    ip = sys.argv[1] if len(sys.argv) > 1 else lan_ip()
    port = sys.argv[2] if len(sys.argv) > 2 else "8001"
    url = f"http://{ip}:{port}/setup"

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#0b0a18", back_color="white")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)

    print(f"二维码 -> {url}")
    print(f"已写入 {OUT} ({OUT.stat().st_size}B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
