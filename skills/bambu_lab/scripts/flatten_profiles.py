import json
import os
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else r"D:\Bambu Studio\resources\profiles\BBL"
OUT = sys.argv[2] if len(sys.argv) > 2 else r"D:\AI\dabai\data\cli_cfg"
TARGETS = {
    "machine": "Bambu Lab A1 0.4 nozzle",
    "process": "0.12mm High Quality @BBL A1",
    "filament": "Bambu PLA Basic @BBL A1",
}
SKIP_KEYS = {"inherits", "name", "setting_id", "filament_id", "instantiation"}

index = {}
for dp, dn, fn in os.walk(ROOT):
    for f in fn:
        if not f.endswith(".json"):
            continue
        p = os.path.join(dp, f)
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if isinstance(d, dict) and "name" in d:
            index.setdefault(d["name"], (p, d))

print("INDEX_SIZE", len(index))


def resolve(name, chain=None):
    chain = chain or []
    if name in chain:
        raise RuntimeError("cycle: " + " -> ".join(chain + [name]))
    if name not in index:
        raise RuntimeError("missing profile: " + name)
    path, d = index[name]
    merged = {}
    parent = d.get("inherits")
    if parent:
        merged.update(resolve(parent, chain + [name]))
    for k, v in d.items():
        if k in SKIP_KEYS:
            continue
        merged[k] = v
    return merged


os.makedirs(OUT, exist_ok=True)
for kind, name in TARGETS.items():
    try:
        cfg = resolve(name)
    except Exception as e:
        print("FAIL", kind, name, e)
        continue
    cfg["name"] = name
    cfg["from"] = "system"
    cfg["instantiation"] = "true"
    outp = os.path.join(OUT, kind + ".json")
    with open(outp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)
    print("OK %-9s %-34s keys=%d -> %s" % (kind, name, len(cfg), outp))
    for k in ("printer_model", "printable_area", "printable_height", "layer_height", "filament_type", "nozzle_diameter"):
        if k in cfg:
            print("   ", k, "=", cfg[k])
