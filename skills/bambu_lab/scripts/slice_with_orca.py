"""按 A1 mini 重切，带树形支撑。

MCP 的 slice_stl 不暴露支撑开关，切出来的 enable_support=0；
而带 template_3mf_path 那次会走 Bambu Studio CLI，这台机器上它是坏的。
所以手工组配置：A1M 的 0.16 预设 + 树形支撑覆盖，直接喂 OrcaSlicer CLI。
"""
import json
import subprocess

BASE = r"D:\AI\dabai\data\orca\resources\profiles\BBL\process\0.20mm Standard @BBL A1M.json"
OVR = r"D:\AI\dabai\temp\bf_mm_process_a1m_sup.json"
MACHINE = r"C:\Users\wangxingfeng\AppData\Roaming\BambuStudio\system\BBL\machine\Bambu Lab A1 mini 0.4 nozzle.json"
FILAMENT = r"C:\Users\wangxingfeng\AppData\Roaming\BambuStudio\system\BBL\filament\Bambu PLA Basic @BBL A1M.json"
STL = r"D:\AI\dabai\data\bf_mm.stl"
ORCA = r"D:\AI\dabai\data\orca\orca-slicer.exe"
OUTDIR = r"D:\AI\dabai\temp"
OUTNAME = "bf_a1m_sup02.3mf"

cfg = json.load(open(BASE, encoding="utf-8"))
cfg.update({
    "name": "A1M 0.20 tree support",
    "from": "User",
    "layer_height": "0.2",
    "enable_support": "1",
    "support_type": "tree(auto)",
    "support_threshold_angle": "30",
    "support_style": "tree_slim",
})
json.dump(cfg, open(OVR, "w", encoding="utf-8"), indent=1)
print("OVR_WRITTEN", OVR, "keys=", len(cfg), flush=True)

cmd = [
    ORCA,
    "--slice", "0",
    "--outputdir", OUTDIR,
    "--export-3mf", OUTNAME,
    "--load-settings", f"{MACHINE};{OVR}",
    "--load-filaments", FILAMENT,
    "--allow-newer-file",
    "--ensure-on-bed",
    "--min-save",
    "--arrange", "1",
    STL,
]
print("RUN", flush=True)
r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
print("RC", r.returncode, flush=True)
print("STDOUT", (r.stdout or "")[-2500:], flush=True)
print("STDERR", (r.stderr or "")[-2500:], flush=True)
