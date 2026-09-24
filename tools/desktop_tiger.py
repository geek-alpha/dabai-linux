"""在桌面上画一只会眨眼的大老虎。

全屏透明置顶窗口 + Canvas 图形组合；点一下或按 Esc 收起来，2 分钟后自动消失。
"""
import math
import tkinter as tk

KEY = "#010203"          # 颜色键：窗口里这个颜色会被系统当作透明
TICK_MS = 50
LIFE_MS = 120_000
BLINK_EVERY = 64         # 帧数，约 3.2 秒眨一次
BLINK_SHAPE = (0.10, 0.04, 0.04, 0.12)   # 眨眼各帧的眼皮开合比例

ORANGE = "#F0A03C"
ORANGE_D = "#C9701A"
WHITE = "#FFF6E6"
BLACK = "#2B1A0D"
PINK = "#E8708C"


class DesktopTiger:
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

        self.cx, self.cy = self.w / 2, self.h / 2
        self.r = min(self.w, self.h) * 0.24
        self.t = 0
        self.dy = 0.0
        self.eyes = []       # (眼白 id, 瞳孔 id, 高光 id, ex, ey, rx, ry, prx, pry)

        self._draw()
        self.canvas.bind("<Button-1>", lambda e: self.close())
        self.root.bind("<Escape>", lambda e: self.close())
        self.root.after(TICK_MS, self._tick)
        self.root.after(LIFE_MS, self.close)

    # ---------- 绘制 ----------

    def _oval(self, x, y, rx, ry, **kw):
        return self.canvas.create_oval(x - rx, y - ry, x + rx, y + ry, **kw)

    def _line(self, pts, **kw):
        return self.canvas.create_line(*pts, **kw)

    def _draw(self):
        c, cx, cy, r = self.canvas, self.cx, self.cy, self.r

        # 耳朵（先画，后面被头盖住下半部）
        for s in (-1, 1):
            ex, ey = cx + s * r * 0.80, cy - r * 0.72
            self._oval(ex, ey, r * 0.38, r * 0.38, fill=ORANGE_D, outline=BLACK, width=r * 0.03)
            self._oval(ex, ey, r * 0.20, r * 0.20, fill=PINK, outline="")

        # 头
        self._oval(cx, cy, r * 1.10, r * 1.00, fill=ORANGE, outline=BLACK, width=r * 0.035)

        # 额头「王」字
        top = cy - r * 0.78
        for i, w in enumerate((0.36, 0.30, 0.24)):
            y = top + i * r * 0.15
            self._line((cx - r * w, y, cx + r * w, y), fill=BLACK, width=r * 0.055,
                       capstyle="round")
        self._line((cx, top - r * 0.05, cx, top + r * 0.35), fill=BLACK,
                   width=r * 0.055, capstyle="round")

        # 脸颊白毛
        for s in (-1, 1):
            self._oval(cx + s * r * 0.60, cy + r * 0.32, r * 0.46, r * 0.36,
                       fill=WHITE, outline="")
        self._oval(cx, cy + r * 0.62, r * 0.52, r * 0.30, fill=WHITE, outline="")

        # 脸侧条纹
        for s in (-1, 1):
            for k, (y0, y1) in enumerate(((-0.24, -0.16), (0.10, 0.16))):
                self._line((cx + s * r * 1.04, cy + r * y0, cx + s * r * 0.70, cy + r * y1),
                           fill=BLACK, width=r * 0.045, capstyle="round")

        # 眼睛
        for s in (-1, 1):
            ex, ey = cx + s * r * 0.42, cy - r * 0.16
            rx, ry, prx, pry = r * 0.24, r * 0.19, r * 0.125, r * 0.14
            w_id = self._oval(ex, ey, rx, ry, fill=WHITE, outline=BLACK, width=r * 0.025)
            p_id = self._oval(ex, ey, prx, pry, fill=BLACK, outline="")
            h_id = self._oval(ex - prx * 0.35, ey - pry * 0.45, prx * 0.3, pry * 0.3,
                              fill=WHITE, outline="")
            self.eyes.append((w_id, p_id, h_id, ex, ey, rx, ry, prx, pry))

        # 鼻子
        ny = cy + r * 0.22
        c.create_polygon(cx - r * 0.21, ny, cx + r * 0.21, ny, cx, ny + r * 0.24,
                         fill=PINK, outline=BLACK, width=r * 0.025, joinstyle="round")

        # 人中与嘴
        self._line((cx, ny + r * 0.24, cx, ny + r * 0.40), fill=BLACK,
                   width=r * 0.035, capstyle="round")
        for s in (-1, 1):
            self._line((cx, ny + r * 0.40, cx + s * r * 0.20, ny + r * 0.54,
                        cx + s * r * 0.36, ny + r * 0.40),
                       fill=BLACK, width=r * 0.035, smooth=True, capstyle="round")

        # 胡须：深色底线 + 白线，浅色壁纸上也不会消失
        for s in (-1, 1):
            for y0, y1 in ((0.26, 0.16), (0.36, 0.38), (0.46, 0.60)):
                pts = (cx + s * r * 0.46, cy + r * y0, cx + s * r * 1.16, cy + r * y1)
                self._line(pts, fill=BLACK, width=r * 0.055, capstyle="round")
                self._line(pts, fill=WHITE, width=r * 0.028, capstyle="round")

    # ---------- 动画 ----------

    def _set_eyes(self, k):
        for w_id, p_id, h_id, ex, ey, rx, ry, prx, pry in self.eyes:
            y = ey + self.dy
            self.canvas.coords(w_id, ex - rx, y - ry * k, ex + rx, y + ry * k)
            self.canvas.coords(p_id, ex - prx, y - pry * k, ex + prx, y + pry * k)
            self.canvas.coords(h_id, ex - prx * 0.65, y - pry * 0.75 * k,
                               ex - prx * 0.05, y - pry * 0.15 * k)

    def _tick(self):
        self.t += 1
        phase = self.t % BLINK_EVERY
        self._set_eyes(BLINK_SHAPE[phase] if phase < len(BLINK_SHAPE) else 1.0)

        # 整只老虎轻轻呼吸
        dy = math.sin(self.t * TICK_MS / 1000 * 1.6) * self.r * 0.018
        self.canvas.move("all", 0, dy - self.dy)
        self.dy = dy

        self.root.after(TICK_MS, self._tick)

    def close(self):
        self.root.destroy()


if __name__ == "__main__":
    DesktopTiger().root.mainloop()
