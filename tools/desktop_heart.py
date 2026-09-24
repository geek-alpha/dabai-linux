"""在桌面上画一颗会心跳的大爱心。

全屏透明置顶窗口 + 参数方程心形；点一下或按 Esc 收起来，2 分钟后自动消失。
"""
import math
import tkinter as tk

KEY = "#010203"      # 颜色键：窗口里这个颜色会被系统当作透明
TICK_MS = 33
BEAT_MS = 950        # 一次心跳的周期
LIFE_MS = 120_000


def heart_xy(t):
    return (16 * math.sin(t) ** 3,
            13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t))


def heart_points(cx, cy, scale, steps=280):
    pts = []
    for i in range(steps):
        x, y = heart_xy(2 * math.pi * i / steps)
        pts.append(cx + x * scale)
        pts.append(cy - y * scale)
    return pts


class DesktopHeart:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.w = self.root.winfo_screenwidth()
        self.h = self.root.winfo_screenheight()
        self.root.geometry(f"{self.w}x{self.h}+0+0")
        self.root.configure(bg=KEY)
        self.root.attributes("-transparentcolor", KEY)
        self.root.attributes("-topmost", True)
        self.canvas = tk.Canvas(self.root, width=self.w, height=self.h,
                                bg=KEY, highlightthickness=0)
        self.canvas.pack()
        self.scale = min(self.w, self.h) * 0.016
        # 心形在参数方程里的纵向中心偏上 2.75，补回来才是视觉居中
        self.cx, self.cy = self.w / 2, self.h / 2 + 2.75 * self.scale
        self.t = 0
        self.canvas.bind("<Button-1>", lambda e: self.close())
        self.root.bind_all("<Escape>", lambda e: self.close())
        self.root.after(LIFE_MS, self.close)
        self.tick()

    def tick(self):
        p = (self.t % BEAT_MS) / BEAT_MS
        pulse = 1 + 0.055 * abs(math.sin(2 * math.pi * p)) ** 2
        s = self.scale * pulse
        self.canvas.delete("all")
        for k, color in ((1.06, "#5c0a1c"), (1.0, "#ff1e46"),
                         (0.60, "#ff6f8c"), (0.26, "#ffd0d8")):
            self.canvas.create_polygon(heart_points(self.cx, self.cy, s * k),
                                       fill=color, outline="")
        self.t += TICK_MS
        self.root.after(TICK_MS, self.tick)

    def close(self):
        self.root.destroy()


DesktopHeart().root.mainloop()
