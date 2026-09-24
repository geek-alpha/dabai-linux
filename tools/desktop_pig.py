"""在桌面上画一只会嗅鼻子的小猪。

全屏透明置顶窗口 + Canvas 图形组合；点一下或按 Esc 收起来，2 分钟后自动消失。
"""
import math
import tkinter as tk

KEY = "#010203"          # 颜色键：窗口里这个颜色会被系统当作透明
TICK_MS = 50
LIFE_MS = 120_000
BLINK_EVERY = 70         # 帧数，约 3.5 秒眨一次
BLINK_SHAPE = (0.12, 0.05, 0.05, 0.14)

PINK = "#F7B3C6"
PINK_D = "#E07C9E"
PINK_L = "#FFDCE6"
BLACK = "#3A1F27"
NOSTRIL = "#8E4761"


class DesktopPig:
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
        self.eyes = []        # (眼白 id, 瞳孔 id, 高光 id, ex, ey, rx, ry, prx, pry)
        self.sniff = []       # ("oval", id, x, y, rx, ry) / ("line", id, pts)

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
        bw = r * 0.03

        # 卷尾巴（先画，会被身子压住一截）
        tx, ty = cx + r * 1.02, cy + r * 0.52
        c.create_arc(tx - r * 0.24, ty - r * 0.24, tx + r * 0.24, ty + r * 0.24,
                     start=200, extent=300, style="arc", outline=PINK_D,
                     width=r * 0.07)

        # 耳朵：两只耷拉的三角耳
        for s in (-1, 1):
            c.create_polygon(cx + s * r * 0.30, cy - r * 0.70,
                             cx + s * r * 1.02, cy - r * 0.98,
                             cx + s * r * 0.92, cy - r * 0.16,
                             fill=PINK_D, outline=BLACK, width=bw,
                             joinstyle="round")
            c.create_polygon(cx + s * r * 0.46, cy - r * 0.62,
                             cx + s * r * 0.88, cy - r * 0.80,
                             cx + s * r * 0.82, cy - r * 0.32,
                             fill=PINK_L, outline="", joinstyle="round")

        # 头
        self._oval(cx, cy, r * 1.08, r * 0.98, fill=PINK, outline=BLACK, width=r * 0.035)

        # 腮红
        for s in (-1, 1):
            self._oval(cx + s * r * 0.74, cy + r * 0.26, r * 0.26, r * 0.17,
                       fill=PINK_D, outline="")

        # 眼睛：黑豆眼 + 高光
        for s in (-1, 1):
            ex, ey = cx + s * r * 0.40, cy - r * 0.24
            rx, ry, prx, pry = r * 0.15, r * 0.17, r * 0.15, r * 0.17
            w_id = self._oval(ex, ey, rx, ry, fill=BLACK, outline="")
            p_id = self._oval(ex, ey, prx, pry, fill=BLACK, outline="")
            h_id = self._oval(ex - prx * 0.30, ey - pry * 0.35, prx * 0.32, pry * 0.30,
                              fill="#FFFFFF", outline="")
            self.eyes.append((w_id, p_id, h_id, ex, ey, rx, ry, prx, pry))

        # 猪鼻子（会上下嗅动）
        nx, ny, nrx, nry = cx, cy + r * 0.28, r * 0.42, r * 0.30
        self.sniff.append(("oval", self._oval(nx, ny, nrx, nry, fill=PINK_D,
                                              outline=BLACK, width=bw), nx, ny, nrx, nry))
        for s in (-1, 1):
            hx = nx + s * r * 0.155
            self.sniff.append(("oval", self._oval(hx, ny, r * 0.055, r * 0.135,
                                                  fill=NOSTRIL, outline=""),
                               hx, ny, r * 0.055, r * 0.135))

        # 嘴：鼻子下方的小弧
        for s in (-1, 1):
            pts = (cx, ny + r * 0.44, cx + s * r * 0.16, ny + r * 0.56,
                   cx + s * r * 0.30, ny + r * 0.42)
            self.sniff.append(("line", self._line(pts, fill=BLACK, width=r * 0.035,
                                                  smooth=True, capstyle="round"), pts))

    # ---------- 动画 ----------

    def _set_eyes(self, k):
        for w_id, p_id, h_id, ex, ey, rx, ry, prx, pry in self.eyes:
            y = ey + self.dy
            self.canvas.coords(w_id, ex - rx, y - ry * k, ex + rx, y + ry * k)
            self.canvas.coords(p_id, ex - prx, y - pry * k, ex + prx, y + pry * k)
            self.canvas.coords(h_id, ex - prx * 0.62, y - pry * 0.65 * k,
                               ex - prx * 0.02, y - pry * 0.05 * k)

    def _set_sniff(self, dy):
        for item in self.sniff:
            if item[0] == "oval":
                _, i, x, y, rx, ry = item
                self.canvas.coords(i, x - rx, y + dy - ry, x + rx, y + dy + ry)
            else:
                _, i, pts = item
                self.canvas.coords(i, *[v + (dy if k % 2 else 0) for k, v in enumerate(pts)])

    def _tick(self):
        self.t += 1
        phase = self.t % BLINK_EVERY
        self._set_eyes(BLINK_SHAPE[phase] if phase < len(BLINK_SHAPE) else 1.0)

        # 整只小猪轻轻呼吸
        dy = math.sin(self.t * TICK_MS / 1000 * 1.6) * self.r * 0.018
        self.canvas.move("all", 0, dy - self.dy)
        self.dy = dy

        # 鼻子快速抽动，像在闻东西
        self._set_sniff(math.sin(self.t * TICK_MS / 1000 * 2.6) * self.r * 0.045)

        self.root.after(TICK_MS, self._tick)

    def close(self):
        self.root.destroy()


if __name__ == "__main__":
    DesktopPig().root.mainloop()
