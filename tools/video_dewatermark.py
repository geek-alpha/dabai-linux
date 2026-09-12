#!/usr/bin/env python3
"""豆包 AI 视频去水印：逐帧擦掉固定位置的水印，再合回原音轨。

水印位置以 720x1280 为基准、按比例记录，输入分辨率不同时自动换算；
漂移范围已包含在框内，所以水印在框内小幅移动也能擦掉。

用法:
    video_dewatermark.py <输入.mp4> <输出.mp4>
    video_dewatermark.py in.mp4 out.mp4 --box 508,1184,190,72 --box 49,22,190,72
    video_dewatermark.py in.mp4 out.mp4 --rad 8 --telea

默认两个框（720x1280 基准，x,y,w,h）:
    508,1184,190,72   右下
    49,22,190,72      左上
"""
import argparse
import os
import subprocess
import sys

import cv2
import numpy as np

BASE_W, BASE_H = 720, 1280
DEFAULT_BOXES = [(508, 1184, 190, 72), (49, 22, 190, 72)]
PAD_RATIO = 12 / BASE_W


def parse_boxes(raw_boxes, W, H):
    src = raw_boxes or DEFAULT_BOXES
    sx, sy = W / BASE_W, H / BASE_H
    boxes = []
    for (bx, by, bw, bh) in src:
        x = int(round(bx * sx)); y = int(round(by * sy))
        w = int(round(bw * sx)); h = int(round(bh * sy))
        x = max(0, min(x, W - 1)); y = max(0, min(y, H - 1))
        w = max(1, min(w, W - x)); h = max(1, min(h, H - y))
        boxes.append((x, y, w, h))
    return boxes


def wipe_frame(fr, boxes, pad, rad, mode, W, H):
    for (bx, by, bw, bh) in boxes:
        x0 = max(0, bx - pad); y0 = max(0, by - pad)
        x1 = min(W, bx + bw + pad); y1 = min(H, by + bh + pad)
        sub = fr[y0:y1, x0:x1]
        mask = np.zeros(sub.shape[:2], np.uint8)
        mask[by - y0:by - y0 + bh, bx - x0:bx - x0 + bw] = 255
        fr[y0:y1, x0:x1] = cv2.inpaint(sub, mask, rad, mode)
    return fr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('out')
    ap.add_argument('--box', action='append', default=[],
                    help='x,y,w,h（720x1280 基准），可重复；不给就用默认两个')
    ap.add_argument('--rad', type=int, default=5, help='inpaint 半径，默认 5')
    ap.add_argument('--telea', action='store_true', help='用 TELEA 代替 NS 算法')
    ap.add_argument('--crf', type=int, default=20)
    args = ap.parse_args()

    boxes_raw = []
    for b in args.box:
        parts = [int(v) for v in b.replace(' ', '').split(',')]
        if len(parts) != 4:
            sys.exit(f'--box 要 x,y,w,h 四个数，收到: {b}')
        boxes_raw.append(tuple(parts))

    cap = cv2.VideoCapture(args.src)
    if not cap.isOpened():
        sys.exit(f'打不开输入: {args.src}')
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    boxes = parse_boxes(boxes_raw, W, H)
    pad = max(4, int(round(PAD_RATIO * W)))
    mode = cv2.INPAINT_TELEA if args.telea else cv2.INPAINT_NS

    out_dir = os.path.dirname(os.path.abspath(args.out)) or '.'
    silent = os.path.join(out_dir, '.silent-' + os.path.basename(args.out))
    vw = cv2.VideoWriter(silent, cv2.VideoWriter_fourcc(*'mp4v'), fps, (W, H))
    if not vw.isOpened():
        sys.exit(f'写不了中间文件: {silent}')

    n = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            vw.write(wipe_frame(fr, boxes, pad, args.rad, mode, W, H))
            n += 1
    finally:
        cap.release(); vw.release()

    if n == 0:
        os.path.exists(silent) and os.remove(silent)
        sys.exit('一帧都没读到，输入可能是坏文件')
    print(f'{n} 帧处理完  {W}x{H}  区域 {boxes}  pad={pad}')

    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', silent, '-i', args.src,
                    '-map', '0:v', '-map', '1:a?', '-c:v', 'libx264',
                    '-preset', 'veryfast', '-crf', str(args.crf),
                    '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-movflags', '+faststart', args.out], check=True)
    os.remove(silent)
    print('输出', args.out, os.path.getsize(args.out) // 1024, 'KB')


if __name__ == '__main__':
    main()
