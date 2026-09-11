"""截屏（跨平台）：mss → PIL.ImageGrab → pyautogui 三级回退。

- Windows：三者都可用，行为与改造前（pyautogui）等价；
- Linux/macOS：优先 mss（Wayland/X11 都快），其次 Pillow 的 ImageGrab；
- 无图形会话（纯 SSH / 无 DISPLAY）时抛出带诊断信息的 RuntimeError，
  绝不返回一张黑图让上层误判"截屏成功"。
"""
import os


def _capture():
    """返回 PIL.Image；按可用性从高到低尝试，全部失败则抛 RuntimeError。"""
    errors = []
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as e:
        errors.append(f"mss: {e.__class__.__name__}: {e}")
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        if img is not None:
            return img
        errors.append("PIL.ImageGrab: 返回空图像")
    except Exception as e:
        errors.append(f"PIL.ImageGrab: {e.__class__.__name__}: {e}")
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception as e:
        errors.append(f"pyautogui: {e.__class__.__name__}: {e}")
    raise RuntimeError(
        "截屏失败（所有后端均不可用）：" + "；".join(errors) +
        "。Linux 下需有图形会话（DISPLAY / WAYLAND_DISPLAY）并安装 mss 或 Pillow。")


def take_screenshot(output_path=None):
    """
    Take a screenshot and save it to a file.

    Args:
        output_path (str, optional): Path where screenshot will be saved.
                                    If not provided, saves to current directory
                                    with filename screenshot.png.

    Returns:
        str: Path to the saved screenshot file.
    """
    screenshot = _capture()

    # Generate default path if none provided
    if output_path is None:
        output_path = "screenshot.png"

    # Save screenshot to file
    screenshot.save(output_path)

    # Return the path where screenshot was saved
    return os.path.abspath(output_path)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        print(take_screenshot(out))
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
