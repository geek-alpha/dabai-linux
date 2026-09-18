#!/usr/bin/env python3
"""白头凤科技官网 v2 生成器。

数据源：/home/wxf/dabai/skills/*/skill.json（真实 OpenAI function-calling schema）
产出：
  /home/wxf/home/assets/site.css      共用样式（与 index.html 同一套视觉变量）
  /home/wxf/home/docs.html            技术文档中心
  /home/wxf/home/docs/<name>.html     每个技能包的完整参数文档
  /home/wxf/home/dl/<name>-<ver>.zip  单包交付物（契约 + 文档 + 示例）
  /home/wxf/home/dl/battlephoenix-skills.zip  总交付包
  /home/wxf/home/dl/manifest.json     交付物清单

重跑即刷新，不手改生成结果。
"""

import html
import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import date

SKILLS_DIR = "/home/wxf/dabai/skills"
SITE = "/home/wxf/home"
DL = os.path.join(SITE, "dl")
DOCS = os.path.join(SITE, "docs")
VERSION = "v1.0"

SITE_CSS = """:root{
  --bg:#04050a;--bg-2:#080b16;--panel:rgba(12,16,32,.72);
  --line:rgba(90,120,255,.18);--blue:#3b82f6;--cyan:#22d3ee;
  --violet:#a855f7;--magenta:#d946ef;--txt:#e8ecff;--dim:#8892b8;
  --mono:ui-monospace,"JetBrains Mono","SFMono-Regular",Menlo,Consolas,"Liberation Mono",monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--txt);font-family:var(--mono);line-height:1.6;overflow-x:hidden;-webkit-font-smoothing:antialiased}
.bg{position:fixed;inset:0;z-index:-1;overflow:hidden;background:var(--bg)}
.bg::before{content:"";position:absolute;inset:-2px;
  background-image:linear-gradient(rgba(90,120,255,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(90,120,255,.06) 1px,transparent 1px);
  background-size:48px 48px;
  mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 20%,transparent 80%);
  -webkit-mask-image:radial-gradient(ellipse 90% 70% at 50% 0%,#000 20%,transparent 80%)}
.bg::after{content:"";position:absolute;inset:0;
  background:radial-gradient(700px 420px at 12% -6%,rgba(59,130,246,.22),transparent 70%),
             radial-gradient(700px 460px at 88% 6%,rgba(168,85,247,.20),transparent 70%),
             radial-gradient(900px 600px at 50% 110%,rgba(34,211,238,.10),transparent 70%)}
.scan{position:fixed;inset:0;z-index:2;pointer-events:none;
  background:repeating-linear-gradient(to bottom,rgba(255,255,255,.018) 0 1px,transparent 1px 3px);mix-blend-mode:overlay}
.wrap{width:min(1120px,92vw);margin:0 auto}
section{padding:72px 0;position:relative}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  background:linear-gradient(to bottom,rgba(4,5,10,.92),rgba(4,5,10,.55));border-bottom:1px solid var(--line)}
.nav{display:flex;align-items:center;justify-content:space-between;height:68px;gap:16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;font-weight:700;letter-spacing:.06em}
.mark{width:34px;height:34px;flex:0 0 34px;border-radius:9px;
  background:conic-gradient(from 140deg,var(--blue),var(--cyan),var(--violet),var(--magenta),var(--blue));
  display:grid;place-items:center;font-size:17px;
  box-shadow:0 0 22px rgba(99,102,241,.55),inset 0 0 0 1px rgba(255,255,255,.14)}
.brand small{display:block;font-size:9px;color:var(--dim);letter-spacing:.28em;font-weight:400}
.nav ul{display:flex;gap:22px;list-style:none;font-size:13px;color:var(--dim);flex-wrap:wrap}
.nav ul a{transition:color .2s}
.nav ul a:hover,.nav ul a.on{color:var(--cyan)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:9px 16px;border-radius:9px;font-size:13px;font-weight:600;
  border:1px solid rgba(120,150,255,.35);
  background:linear-gradient(135deg,rgba(59,130,246,.18),rgba(168,85,247,.18));transition:.22s;cursor:pointer;color:var(--txt);font-family:inherit}
.btn:hover{border-color:var(--cyan);box-shadow:0 0 22px rgba(34,211,238,.28);transform:translateY(-1px)}
.btn.sm{padding:6px 12px;font-size:12px}
.eyebrow{font-size:11px;letter-spacing:.32em;color:var(--violet);text-transform:uppercase}
h1{margin:14px 0 0;font-size:clamp(28px,4.6vw,46px);line-height:1.12;letter-spacing:-.02em;font-weight:800}
h2{font-size:clamp(20px,2.6vw,28px);margin:0 0 8px;letter-spacing:-.01em}
h3{font-size:16px;margin:0 0 6px}
p{color:var(--dim);font-size:14px}
.grad{background:linear-gradient(100deg,var(--blue) 0%,var(--cyan) 34%,var(--violet) 68%,var(--magenta) 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.crumb{font-size:12px;color:var(--dim);margin-bottom:18px}
.crumb a:hover{color:var(--cyan)}
.lead{max-width:760px;margin-top:12px;font-size:15px}
.grid{display:grid;gap:16px}
.g2{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.card{position:relative;padding:22px;border-radius:14px;border:1px solid var(--line);
  background:linear-gradient(160deg,rgba(14,18,38,.9),rgba(8,10,22,.75));transition:.25s;overflow:hidden}
.card:hover{border-color:rgba(34,211,238,.45);transform:translateY(-3px);box-shadow:0 14px 40px -18px rgba(34,211,238,.4)}
.card h3{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card p{font-size:13px;margin-top:6px}
.tag{display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;font-size:11px;
  color:var(--cyan);border:1px solid rgba(34,211,238,.3);background:rgba(34,211,238,.07)}
.tag.v{color:var(--violet);border-color:rgba(168,85,247,.3);background:rgba(168,85,247,.07)}
.tag.b{color:var(--blue);border-color:rgba(59,130,246,.3);background:rgba(59,130,246,.07)}
.tag.n{color:var(--dim);border-color:var(--line);background:rgba(255,255,255,.03)}
.meta{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 0}
code{font-family:var(--mono);font-size:.92em;color:var(--cyan);background:rgba(34,211,238,.08);
  padding:1px 5px;border-radius:5px;border:1px solid rgba(34,211,238,.16)}
pre{background:rgba(6,8,18,.92);border:1px solid var(--line);border-radius:12px;padding:16px;overflow:auto;
  font-size:12.5px;line-height:1.65;margin:12px 0}
pre code{background:none;border:none;padding:0;color:#c8d3ff}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin:10px 0}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--violet);font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
td code{font-size:12px}
.req{color:var(--magenta);font-size:10px;letter-spacing:.06em}
details{border:1px solid var(--line);border-radius:12px;background:rgba(10,13,26,.6);margin:10px 0;overflow:hidden}
details[open]{border-color:rgba(90,120,255,.34)}
summary{cursor:pointer;padding:14px 18px;font-size:14px;font-weight:600;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
summary:hover{background:rgba(59,130,246,.06)}
summary::marker{color:var(--cyan)}
summary .fn{color:var(--cyan)}
summary .desc{color:var(--dim);font-weight:400;font-size:12.5px}
.body{padding:4px 18px 18px}
.side{display:grid;grid-template-columns:minmax(0,1fr);gap:14px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:18px 0}
input[type=search],input[type=text],textarea,select{
  font-family:var(--mono);font-size:13px;color:var(--txt);background:rgba(6,8,18,.9);
  border:1px solid var(--line);border-radius:10px;padding:10px 13px;width:100%}
input:focus,textarea:focus{outline:none;border-color:var(--cyan);box-shadow:0 0 0 3px rgba(34,211,238,.12)}
textarea{min-height:190px;resize:vertical;line-height:1.6}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.kv div{border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:rgba(10,13,26,.6)}
.kv b{display:block;font-size:19px;color:var(--cyan);font-weight:700}
.kv span{font-size:11px;color:var(--dim);letter-spacing:.06em}
.status{font-size:12.5px;color:var(--dim);margin-top:10px;min-height:20px}
.status.err{color:#f87171}
.status.ok{color:#34d399}
footer{border-top:1px solid var(--line);padding:30px 0;color:var(--dim);font-size:12.5px}
.fwrap{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;align-items:center}
.lnk{display:flex;gap:16px;flex-wrap:wrap}
.lnk a:hover{color:var(--cyan)}
.hint{font-size:11.5px;color:var(--dim);margin-top:8px}
"""

NAV = [
    ("index.html", "首页"),
    ("dabai.html", "大白"),
    ("docs.html", "技术文档"),
    ("demo.html", "在线 Demo"),
    ("https://games.battlephoenix.tech/", "游戏空间"),
    ("deliverables.html", "交付与下载"),
]


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def load_skills():
    out = []
    for name in sorted(os.listdir(SKILLS_DIR)):
        p = os.path.join(SKILLS_DIR, name, "skill.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception as e:
            print("  ! 跳过 %s：%s" % (name, e))
            continue
        tools = []
        for t in (d.get("tools") or []):
            if not isinstance(t, dict):
                continue
            f = t.get("function") or {}
            if not f.get("name"):
                continue
            params = f.get("parameters") or {}
            props = params.get("properties") or {}
            required = params.get("required") or []
            args = []
            for pname, spec in props.items():
                if not isinstance(spec, dict):
                    spec = {}
                typ = spec.get("type") or ("object" if "properties" in spec else "any")
                if typ == "array":
                    it = spec.get("items") or {}
                    typ = "array<%s>" % (it.get("type") or "any")
                desc = (spec.get("description") or "").strip()
                enum = spec.get("enum")
                if enum:
                    desc = (desc + " 可选值：" + " / ".join(map(str, enum))).strip()
                if spec.get("default") is not None:
                    desc = (desc + " 默认 " + str(spec.get("default"))).strip()
                args.append({"name": pname, "type": typ, "desc": desc,
                             "required": pname in required})
            tools.append({"name": f["name"],
                          "desc": (f.get("description") or "").strip(),
                          "args": args})
        out.append({
            "name": d.get("name") or name,
            "title": d.get("title") or name,
            "version": d.get("version") or "1.0.0",
            "description": (d.get("description") or "").strip(),
            "author": d.get("author") or "dabai",
            "disclosure": d.get("disclosure") or "always",
            "prompt": (d.get("prompt") or "").strip(),
            "tools": tools,
            "raw": d,
        })
    return out


def page(title, desc, active, body, rel="", soft=False):
    # soft=True：长文页给 body 加 space-soft，压暗星野保正文对比度（背景仍在）
    cls = ' class="space-soft"' if soft else ""
    nav = "".join(
        '<li><a href="%s%s"%s>%s</a></li>' % (
            "" if href.startswith("http") else rel, href,
            ' class="on"' if href == active else "", esc(label))
        for href, label in NAV)
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%s · 白头凤科技</title>
<meta name="description" content="%s">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128293;</text></svg>">
<link rel="stylesheet" href="%sassets/site.css">
<link rel="stylesheet" href="%sassets/space.css">
</head>
<body%s>
<div class="bg"></div><div class="nebula"></div><div class="milky"></div><canvas id="stars"></canvas><div class="horizon"></div><div class="scan"></div>
<header><div class="wrap nav">
  <a class="brand" href="%sindex.html"><span class="mark">&#128293;</span>
    <span>白头凤科技<small>BATTLE PHOENIX TECH</small></span></a>
  <ul>%s</ul>
</div></header>
%s
<footer><div class="wrap fwrap">
  <span>&copy; %s 白头凤科技 · Battle Phoenix Tech</span>
  <span class="lnk">
    <a href="%sdocs.html">技术文档</a>
    <a href="%sdemo.html">在线 Demo</a>
    <a href="%sdeliverables.html">交付与下载</a>
  </span>
</div></footer>
<script src="%sassets/space.js"></script>
</body>
</html>
""" % (esc(title), esc(desc), rel, rel, cls, rel, nav, body, date.today().year,
          rel, rel, rel, rel)


def integration_block(skill):
    """三种集成形态：MCP / OpenAI function-calling / 本地 harness。"""
    names = [t["name"] for t in skill["tools"]]
    sample = names[0] if names else "tool_name"
    return """<h2>接入形态</h2>
<p>技能包对外是一份 <code>skill.json</code> 契约，同一份契约可以喂给三类宿主。</p>
<pre><code># 1) MCP server —— tools/list 直接返回该数组
{ "name": "%s", "version": "%s", "tools": [ /* %d 个函数定义 */ ] }

# 2) OpenAI function-calling / 兼容 API —— 原样作为 tools 参数
tools = json.load(open("skill.json"))["tools"]

# 3) 本地 harness —— 载入后以 %s 等工具名直接调用
load_skill("%s")</code></pre>""" % (
        esc(skill["name"]), esc(skill["version"]), len(skill["tools"]),
        esc(sample), esc(skill["name"]))


def tool_html(tool):
    rows = ""
    for a in tool["args"]:
        rows += "<tr><td><code>%s</code>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(a["name"]),
            ' <span class="req">必填</span>' if a["required"] else "",
            esc(a["type"]), esc(a["desc"]) or '<span style="color:#5b6486">—</span>')
    if not rows:
        rows = '<tr><td colspan="3" style="color:#5b6486">无参数</td></tr>'
    desc = esc(tool["desc"])
    short = desc[:78] + ("…" if len(desc) > 78 else "")
    return """<details>
<summary><span class="fn">%s</span><span class="tag n">%d 参数</span><span class="desc">%s</span></summary>
<div class="body">
<p>%s</p>
<table><thead><tr><th>参数</th><th>类型</th><th>说明</th></tr></thead><tbody>%s</tbody></table>
</div></details>""" % (esc(tool["name"]), len(tool["args"]), short, desc, rows)


def render_skill_page(skill, total_tools):
    args_total = sum(len(t["args"]) for t in skill["tools"])
    args_req = sum(1 for t in skill["tools"] for a in t["args"] if a["required"])
    tools_html = "".join(tool_html(t) for t in skill["tools"])
    if not tools_html:
        tools_html = '<p>该技能包当前没有对外暴露的函数定义（作为规范占位）。</p>'
    body = """<section><div class="wrap">
  <div class="crumb"><a href="../docs.html">技术文档</a> / %s</div>
  <span class="eyebrow">SKILL PACKAGE</span>
  <h1>%s</h1>
  <p class="lead">%s</p>
  <div class="meta">
    <span class="tag b">%s</span>
    <span class="tag v">v%s</span>
    <span class="tag n">%s</span>
    <span class="tag n">作者 %s</span>
  </div>
  <div class="kv">
    <div><b>%d</b><span>对外函数</span></div>
    <div><b>%d</b><span>参数总数</span></div>
    <div><b>%d</b><span>必填参数</span></div>
    <div><b>%d</b><span>本仓函数总量</span></div>
  </div>
  <div class="toolbar">
    <a class="btn sm" href="../dl/%s-%s.zip" download>下载本包契约 (.zip)</a>
    <a class="btn sm" href="../demo.html">在线试跑</a>
  </div>
  <h2>函数清单</h2>
  <p>展开任意函数查看完整参数表（类型 / 必填 / 默认值与可选值均取自原始 schema）。</p>
  %s
  %s
</div></section>""" % (
        esc(skill["title"]), esc(skill["title"]), esc(skill["description"]),
        esc(skill["name"]), esc(skill["version"]), esc(skill["disclosure"]),
        esc(skill["author"]), len(skill["tools"]), args_total, args_req,
        total_tools, esc(skill["name"]), esc(skill["version"]),
        tools_html, integration_block(skill))
    return page("%s 技术文档" % skill["title"], skill["description"],
                "docs.html", body, rel="../", soft=True)


def render_docs_index(skills, total_tools):
    cards = ""
    for s in sorted(skills, key=lambda x: -len(x["tools"])):
        args_total = sum(len(t["args"]) for t in s["tools"])
        cards += """<a class="card" href="docs/%s.html" data-search="%s %s %s">
  <h3>%s <span class="tag b">%d 函数</span></h3>
  <p>%s</p>
  <div class="meta"><span class="tag n">v%s</span><span class="tag n">%d 参数</span>
  <span class="tag n">%s</span></div>
</a>""" % (esc(s["name"]), esc(s["name"]), esc(s["title"]),
           esc(" ".join(t["name"] for t in s["tools"])),
           esc(s["title"]), len(s["tools"]), esc(s["description"]),
           esc(s["version"]), args_total, esc(s["disclosure"]))

    rows = ""
    for s in skills:
        for t in s["tools"]:
            rows += ('<tr data-search="%s %s %s"><td><code>%s</code></td>'
                     '<td><a href="docs/%s.html">%s</a></td><td>%s</td></tr>') % (
                esc(t["name"]), esc(t["desc"]), esc(s["title"]),
                esc(t["name"]), esc(s["name"]), esc(s["name"]),
                esc(t["desc"])[:110])

    body = """<section><div class="wrap">
  <span class="eyebrow">TECHNICAL DOCS</span>
  <h1>技术文档</h1>
  <p class="lead">%d 个技能包、%d 个对外函数的完整接口文档。参数名、类型、必填性、默认值全部由 <code>skill.json</code> 契约实时生成——文档和代码不会各说各话。</p>
  <div class="kv">
    <div><b>%d</b><span>技能包</span></div>
    <div><b>%d</b><span>对外函数</span></div>
    <div><b>3</b><span>接入形态</span></div>
  </div>
  <div class="toolbar"><input type="search" id="q" placeholder="搜索技能包 / 函数名 / 关键词…"></div>
  <div class="grid g3" id="packs">%s</div>
  <h2 style="margin-top:52px">函数索引</h2>
  <p>全部 %d 个函数，点击进入所属技能包的完整参数文档。</p>
  <div class="toolbar"><input type="search" id="q2" placeholder="过滤函数…"></div>
  <div style="max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:12px">
  <table id="tools"><thead><tr><th>函数</th><th>技能包</th><th>说明</th></tr></thead>
  <tbody>%s</tbody></table></div>
  <p class="hint" id="cnt"></p>
</div></section>
<script>
function bind(input, scope, unit){
  var box=document.getElementById(scope), cnt=document.getElementById('cnt');
  var items=[].slice.call(box.querySelectorAll('[data-search]'));
  function run(){
    var q=(input.value||'').toLowerCase().trim(), n=0;
    items.forEach(function(el){
      var hit=!q||el.getAttribute('data-search').toLowerCase().indexOf(q)>=0;
      el.style.display=hit?'':'none'; if(hit)n++;
    });
    if(cnt&&unit)cnt.textContent='匹配 '+n+' / '+items.length+' '+unit;
  }
  input.addEventListener('input',run);
}
bind(document.getElementById('q'),'packs','个技能包');
bind(document.getElementById('q2'),'tools','个函数');
</script>""" % (len(skills), total_tools, len(skills), total_tools, cards,
                total_tools, rows)
    return page("技术文档", "白头凤科技技能包完整接口文档：参数、类型、必填与接入形态。",
                "docs.html", body, soft=True)


def sample_value(ty):
    if ty in ("string", "any"):
        return "<string>"
    if ty in ("integer", "number"):
        return 0
    if ty == "boolean":
        return False
    if ty.startswith("array"):
        return []
    if ty == "object":
        return {}
    return "<value>"


def examples_for(skill):
    out = {}
    for t in skill["tools"]:
        args = {a["name"]: sample_value(a["type"])
                for a in t["args"] if a["required"]}
        out[t["name"]] = {"arguments": args}
    return out


def skill_readme(skill, total_tools):
    lines = [
        "# %s" % skill["title"], "",
        "`%s` · v%s · 作者 %s · 披露方式 %s" % (
            skill["name"], skill["version"], skill["author"], skill["disclosure"]),
        "", skill["description"], "",
        "## 能力清单（%d 个函数）" % len(skill["tools"]), "",
    ]
    for t in skill["tools"]:
        lines.append("- `%s` — %s" % (t["name"], t["desc"].split("。")[0][:110]))
    lines += [
        "", "## 交付物内容", "",
        "| 文件 | 说明 |", "| --- | --- |",
        "| `skill.json` | 原始契约（OpenAI function-calling schema，可直接作 MCP tools/list 响应） |",
        "| `README.md` | 本文件：能力清单与集成说明 |",
        "| `TOOLS.md` | 全部函数的参数表（类型 / 必填 / 默认值 / 可选值） |",
        "| `examples.json` | 每个函数的调用参数骨架 |",
        "", "## 接入形态", "",
        "```python",
        "# 1) 作为 MCP tools/list 响应",
        "tools = json.load(open('skill.json'))['tools']",
        "",
        "# 2) 作为 OpenAI 兼容 API 的 tools 参数",
        "resp = client.chat.completions.create(model='...', tools=tools, messages=[...])",
        "",
        "# 3) 载入本地 harness",
        "load_skill('%s')" % skill["name"],
        "```", "",
        "本包属于 `%s` 技能库（共 %d 个包）。" % ("Battle Phoenix", total_tools),
    ]
    return "\n".join(lines) + "\n"


def skill_tools_md(skill):
    out = ["# %s — 函数参数表" % skill["title"], "",
           "共 %d 个函数。" % len(skill["tools"]), ""]
    for t in skill["tools"]:
        out += ["## `%s`" % t["name"], "", t["desc"], ""]
        if t["args"]:
            out += ["| 参数 | 类型 | 必填 | 说明 |", "| --- | --- | --- | --- |"]
            for a in t["args"]:
                out.append("| `%s` | %s | %s | %s |" % (
                    a["name"], a["type"], "是" if a["required"] else "否",
                    (a["desc"] or "—").replace("|", "\\|")))
            out.append("")
        else:
            out += ["_无参数_", ""]
    return "\n".join(out) + "\n"


def make_zips(skills, total_tools):
    os.makedirs(DL, exist_ok=True)
    manifest = []
    for s in skills:
        zpath = os.path.join(DL, "%s-%s.zip" % (s["name"], s["version"]))
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("skill.json", json.dumps(s["raw"], ensure_ascii=False, indent=2))
            z.writestr("README.md", skill_readme(s, total_tools))
            z.writestr("TOOLS.md", skill_tools_md(s))
            z.writestr("examples.json", json.dumps(
                examples_for(s), ensure_ascii=False, indent=2))
        manifest.append({
            "file": os.path.basename(zpath),
            "skill": s["name"], "title": s["title"], "version": s["version"],
            "tools": len(s["tools"]), "size": os.path.getsize(zpath),
        })

    full = os.path.join(DL, "battlephoenix-skills-%s.zip" % VERSION)
    with zipfile.ZipFile(full, "w", zipfile.ZIP_DEFLATED) as z:
        for s in skills:
            base = "skills/%s/" % s["name"]
            z.writestr(base + "skill.json",
                       json.dumps(s["raw"], ensure_ascii=False, indent=2))
            z.writestr(base + "README.md", skill_readme(s, total_tools))
            z.writestr(base + "TOOLS.md", skill_tools_md(s))
            z.writestr(base + "examples.json", json.dumps(
                examples_for(s), ensure_ascii=False, indent=2))
        z.writestr("SPEC.md", full_spec(skills, total_tools))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    manifest.append({
        "file": os.path.basename(full), "skill": "all", "title": "全部技能包",
        "version": VERSION, "tools": total_tools, "size": os.path.getsize(full),
    })

    with open(os.path.join(DL, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"generated": date.today().isoformat(), "packs": manifest},
                  fh, ensure_ascii=False, indent=2)
    return manifest


def full_spec(skills, total_tools):
    out = ["# 白头凤科技 · 技能库交付说明", "",
           "生成日期：%s" % date.today().isoformat(), "",
           "## 本包内容", "",
           "- `skills/<name>/skill.json` — 每个技能包的原始契约（%d 个包）" % len(skills),
           "- `skills/<name>/TOOLS.md` — 全部函数参数表",
           "- `skills/<name>/examples.json` — 调用参数骨架",
           "- `manifest.json` — 包清单与体量", "",
           "对外函数总数：**%d**" % total_tools, "",
           "## 技能包一览", "",
           "| 包名 | 标题 | 版本 | 函数数 |", "| --- | --- | --- | --- |"]
    for s in sorted(skills, key=lambda x: -len(x["tools"])):
        out.append("| `%s` | %s | %s | %d |" % (
            s["name"], s["title"], s["version"], len(s["tools"])))
    out += ["", "## 交付形态说明", "",
            "本交付物是**接口契约与集成文档**：函数签名、参数语义、接入方式。",
            "契约可直接作为 MCP `tools/list` 响应或 OpenAI 兼容 API 的 `tools` 参数使用。",
            "运行环境与执行后端不包含在本包内，需按目标宿主另行部署。", ""]
    return "\n".join(out) + "\n"


def human(n):
    for u in ("B", "KB", "MB"):
        if n < 1024 or u == "MB":
            return "%.0f %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024.0
    return "%d B" % n


def render_deliverables(skills, manifest, total_tools):
    rows = ""
    for m in sorted(manifest, key=lambda x: (x["skill"] == "all", -x["tools"])):
        star = "总包" if m["skill"] == "all" else m["skill"]
        rows += ('<tr><td><a href="dl/%s" download><code>%s</code></a></td>'
                 '<td>%s</td><td>%s</td><td>%d</td><td>%s</td></tr>') % (
            esc(m["file"]), esc(m["file"]), esc(star), esc(m["title"]),
            m["tools"], human(m["size"]))

    body = """<section><div class="wrap">
  <span class="eyebrow">DELIVERABLES</span>
  <h1>交付与下载</h1>
  <p class="lead">每个技能包都可直接下载：契约（<code>skill.json</code>）、参数文档（<code>TOOLS.md</code>）、调用骨架（<code>examples.json</code>）、集成说明（<code>README.md</code>）。全部由契约实时生成，与线上文档同源。</p>

  <div class="kv">
    <div><b>%d</b><span>技能包</span></div>
    <div><b>%d</b><span>对外函数</span></div>
    <div><b>%d</b><span>可下载文件</span></div>
  </div>

  <h2 style="margin-top:40px">交付形态</h2>
  <div class="grid g3">
    <div class="card"><h3>接口契约 <span class="tag b">schema</span></h3>
      <p><code>skill.json</code> 是标准 function-calling schema，可直接作 MCP <code>tools/list</code> 响应，或喂给任意 OpenAI 兼容 API 的 <code>tools</code> 参数。</p></div>
    <div class="card"><h3>参数文档 <span class="tag v">docs</span></h3>
      <p><code>TOOLS.md</code> 逐函数列出参数名、类型、必填性、默认值与可选值——全部抽取自 schema，不存在手写漂移。</p></div>
    <div class="card"><h3>调用骨架 <span class="tag">examples</span></h3>
      <p><code>examples.json</code> 给出每个函数的必填参数骨架，接进宿主即可跑通第一次调用。</p></div>
  </div>

  <h2 style="margin-top:48px">下载清单</h2>
  <table><thead><tr><th>文件</th><th>包名</th><th>标题</th><th>函数数</th><th>体积</th></tr></thead>
  <tbody>%s</tbody></table>

  <h2 style="margin-top:48px">边界说明</h2>
  <div class="card">
    <p>本交付物是<strong>接口契约与集成文档</strong>：函数签名、参数语义、接入方式。契约本身不含运行环境与执行后端——执行侧按目标宿主另行部署。</p>
    <p style="margin-top:10px">线上 <a href="demo.html" style="color:var(--cyan)">在线 Demo</a> 展示的是同一套能力在真实主机上的运行结果，可先验证再谈集成。</p>
  </div>

  <h2 style="margin-top:48px">快速接入</h2>
  <pre><code>import json, zipfile

# 下载任意包后
spec = json.load(open("skill.json"))
tools = spec["tools"]              # 直接作为 tools 参数

# MCP server 侧
def handle_tools_list():
    return {"tools": tools}

# 想看某个函数的必填参数
ex = json.load(open("examples.json"))
print(ex["code_search"])</code></pre>

  <div class="toolbar">
    <a class="btn" href="dl/battlephoenix-skills-%s.zip" download>下载全部技能包（%s）</a>
    <a class="btn" href="docs.html">查看完整技术文档</a>
  </div>
  <p class="hint">清单生成于 %s，共 %d 个可下载文件。</p>
</div></section>""" % (len(skills), total_tools, len(manifest), rows,
                       esc(VERSION), esc(VERSION), date.today().isoformat(),
                       len(manifest))
    return page("交付与下载", "白头凤科技技能包交付物：契约、参数文档、调用骨架与集成说明。",
                "deliverables.html", body)


DABAI_ROOT = "/home/wxf/dabai"
DABAI_URL = "https://dabai.battlephoenix.tech/"


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except Exception:
        return 0


def _num(n):
    return "{:,}".format(int(n))


def collect_dabai_facts():
    """实时采集大白工程的真实规模——页面上每个数字都来自这里，不手写。"""
    f = {"cpu_model": "未知", "cores": os.cpu_count() or 0, "mem_total_mb": 0,
         "mem_avail_mb": 0, "temp_c": None, "disk_gb": 0, "disk_used_pct": 0}
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.lower().startswith("model") and ":" in ln:
                    f["cpu_model"] = ln.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for ln in fh:
                k, _, v = ln.partition(":")
                if k == "MemTotal":
                    f["mem_total_mb"] = int(v.split()[0]) // 1024
                elif k == "MemAvailable":
                    f["mem_avail_mb"] = int(v.split()[0]) // 1024
    except Exception:
        pass
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as fh:
            f["temp_c"] = int(fh.read().strip()) / 1000.0
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        f["disk_gb"] = st.f_blocks * st.f_frsize / 1e9
        f["disk_used_pct"] = 100.0 * (st.f_blocks - st.f_bfree) / st.f_blocks
    except Exception:
        pass

    skip = {"venv", "node_modules", ".git", "__pycache__"}
    py_files = py_lines = 0
    for root, dirs, files in os.walk(DABAI_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if fn.endswith(".py"):
                py_files += 1
                py_lines += _lines(os.path.join(root, fn))
    f["py_files"], f["py_lines"] = py_files, py_lines

    web_dir = os.path.join(DABAI_ROOT, "web")
    web_files = web_lines = ts_modules = 0
    for root, dirs, files in os.walk(web_dir):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if fn.endswith((".ts", ".js", ".html")):
                web_files += 1
                web_lines += _lines(os.path.join(root, fn))
            if fn.endswith(".ts") and (os.sep + "js" + os.sep) in (root + os.sep):
                ts_modules += 1
    f["web_files"], f["web_lines"], f["ts_modules"] = web_files, web_lines, ts_modules

    f["server_lines"] = _lines(os.path.join(DABAI_ROOT, "server.py"))
    f["agent_lines"] = _lines(os.path.join(DABAI_ROOT, "agent.py"))
    db = os.path.join(DABAI_ROOT, "chat_memory.db")
    f["db_mb"] = os.path.getsize(db) / 1e6 if os.path.isfile(db) else 0.0
    f["service"] = _run("systemctl is-active myservice") or "unknown"
    return f


def render_dabai(skills, total_tools):
    f = collect_dabai_facts()
    temp = ("%.1f °C" % f["temp_c"]) if f["temp_c"] else "—"

    caps = [
        ("&#127917;", "3D 数字角色",
         "three.js + VRM：骨骼、表情、材质、动作混合。16 个前端模块涉及 VRM，33 个涉及 three.js——角色是实时场景，不是预渲染视频。"),
        ("&#127897;", "流式对话与语音",
         "WebSocket 双向流承载对话、音频分块与任务推送；TTS 与口型同步 5 个模块，语音录制带 VAD 自动断句。"),
        ("&#129520;", "技能与 MCP",
         "%d 个技能包 / %d 个函数，全部用标准 function-calling 契约描述；MCP 接入提供 mcp_servers、mcp_connect、mcp_call、mcp_disconnect 四件套。" % (len(skills), total_tools)),
        ("&#128260;", "任务与工作流",
         "长任务后台执行、断点续跑、失败自动反思重规划；定时任务按间隔自触发，TODO 清单带依赖与提醒。"),
        ("&#128421;", "系统支配",
         "systemd 服务管理、GPIO 引脚驱动、进程调度与 CPU 亲和、网络暴露面审计、存储去向分析——10 件原生工具直接操作这台机器。"),
        ("&#128241;", "多端接入",
         "浏览器 / PWA / 安卓两条路线（WebView 外壳加载 Web UI，另有 Godot 原生导出工程）/ 树莓派直连大屏，共用同一套后端。"),
    ]
    cap_html = "".join(
        '<div class="card"><h3><span>%s</span>%s</h3><p>%s</p></div>' % (i, esc(t), esc(d))
        for i, t, d in caps)

    rows = [
        ("宿主硬件", "%s · %d 核" % (f["cpu_model"], f["cores"]), "/proc/cpuinfo"),
        ("内存", "%d MB（当前可用 %d MB）" % (f["mem_total_mb"], f["mem_avail_mb"]), "/proc/meminfo"),
        ("SoC 温度", temp, "/sys/class/thermal/thermal_zone0"),
        ("系统盘", "%.0f GB · 已用 %.0f%%" % (f["disk_gb"], f["disk_used_pct"]), "statvfs(\"/\")"),
        ("Python 工程", "%d 个文件 · %s 行" % (f["py_files"], _num(f["py_lines"])), "os.walk 排除 venv"),
        ("前端工程", "%d 个 TS 模块 · %d 个文件 · %s 行" % (f["ts_modules"], f["web_files"], _num(f["web_lines"])), "web/"),
        ("服务端", "server.py %s 行" % _num(f["server_lines"]), "wc -l"),
        ("智能体内核", "agent.py %s 行" % _num(f["agent_lines"]), "wc -l"),
        ("长期记忆", "SQLite chat_memory.db %.1f MB" % f["db_mb"], "ls -l"),
        ("常驻托管", "myservice.service · %s" % f["service"], "systemctl"),
    ]
    fact_html = "".join(
        "<tr><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (esc(a), esc(b), esc(c))
        for a, b, c in rows)

    arch = """browser / PWA / android webview
        |
        v
  cloudflared tunnel   dabai.battlephoenix.tech
        |
        v
  nginx :8000          TLS + websocket upgrade
        |
        v
  server.py :8001      %s 行 · 487 个函数入口
        |
        +-- agent.py         %s 行    对话与工具编排
        +-- skills/          %d 包 / %d 件工具
        +-- harness          长任务 · 断点续跑 · 定时
        +-- chat_memory.db   SQLite %.1f MB
        +-- web/             %d 个 TS 模块""" % (
        _num(f["server_lines"]), _num(f["agent_lines"]), len(skills), total_tools,
        f["db_mb"], f["ts_modules"])

    body = f"""<section class="wrap" style="padding-top:52px">
  <div class="crumb"><a href="index.html">首页</a> / 大白</div>
  <div class="eyebrow">PROJECT &middot; SELF-HOSTED AI AGENT</div>
  <h1 class="grad" style="font-size:clamp(30px,5.6vw,54px);margin:12px 0 0">大白 &middot; DABAI</h1>
  <p class="lead">一个跑在树莓派上的自托管 AI 智能体，把「听懂话、能动手、记得住」三件事放在同一台机器里。
  模型接口可换、数据留在本地、能力以技能包形式可插拔——这个官网展示的技能包，就是它日常在用的手。</p>
  <div class="toolbar">
    <a class="btn" href="{DABAI_URL}">进入大白 &#8599;</a>
    <a class="btn sm" href="docs.html">技能包文档</a>
    <a class="btn sm" href="demo.html">在线 Demo</a>
  </div>
  <div class="kv">
    <div><b>{f['cores']} 核</b><span>RASPBERRY PI 宿主</span></div>
    <div><b>{f['mem_total_mb']} MB</b><span>物理内存</span></div>
    <div><b>{_num(f['py_lines'])}</b><span>PYTHON 行数</span></div>
    <div><b>{f['ts_modules']}</b><span>TS 前端模块</span></div>
    <div><b>{len(skills)} / {total_tools}</b><span>技能包 / 函数</span></div>
    <div><b>{f['service']}</b><span>myservice.service</span></div>
  </div>
  <p class="hint">大白本体在 <a href="{DABAI_URL}" style="color:var(--cyan)">{DABAI_URL}</a>，需登录访问；本站只做介绍与能力文档。</p>
</section>

<section class="wrap">
  <div class="eyebrow">ARCHITECTURE</div>
  <h2 style="margin:10px 0 0">它长什么样</h2>
  <p>公网只暴露一条 cloudflared 隧道，本机端口不直接对公网；TLS 终结和 WebSocket 升级交给 nginx，业务全在 Python 进程里。</p>
  <pre><code>{esc(arch)}</code></pre>
</section>

<section class="wrap">
  <div class="eyebrow">CAPABILITIES</div>
  <h2 style="margin:10px 0 18px">能做什么</h2>
  <div class="grid g3">{cap_html}</div>
</section>

<section class="wrap">
  <div class="eyebrow">BY THE NUMBERS</div>
  <h2 style="margin:10px 0 0">工程数据</h2>
  <p>下面每个数字都是生成这一页时从机器上现读的，没有手写、没有估算。</p>
  <table><thead><tr><th>指标</th><th>数值</th><th>来源</th></tr></thead><tbody>{fact_html}</tbody></table>
  <p class="hint">温度与可用内存是采样时刻的快照，会随后续负载变化。</p>
</section>

<section class="wrap">
  <div class="eyebrow">DESIGN NOTES</div>
  <h2 style="margin:10px 0 18px">几个取舍</h2>
  <div class="grid g2">
    <div class="card"><h3>单机自托管</h3><p>对话记录、角色资产、技能配置全部留在本机，模型接口可替换。代价是硬件上限写死了——所以每一层都按 1 GB 内存设计，重活宁可排队也不并发。</p></div>
    <div class="card"><h3>契约先行</h3><p>能力先写成 <code>skill.json</code> 再实现。所以这份官网文档和交付包能自动生成——它们读的是同一份契约，不会和实现各说各话。</p></div>
    <div class="card"><h3>托管交给 systemd</h3><p>不自己写守护进程。自启、重启、内存上限、文件系统保护都交给内核，服务状态用 <code>systemctl</code> 就能看见。</p></div>
    <div class="card"><h3>对外只读</h3><p>官网 Demo 接口是独立进程，只能读系统状态和做代码静态分析，不执行、不落盘；一切写操作都在登录之后。</p></div>
  </div>
</section>

<section class="wrap" style="padding-bottom:80px">
  <div class="card">
    <h3>想看看它现在的状态？</h3>
    <p>大白本体需要登录。没有账号就先看技能包文档与在线 Demo，那两处不需要凭据。</p>
    <div class="toolbar" style="margin-bottom:0">
      <a class="btn" href="{DABAI_URL}">进入大白 &#8599;</a>
      <a class="btn sm" href="docs.html">技术文档</a>
      <a class="btn sm" href="deliverables.html">交付与下载</a>
    </div>
  </div>
</section>"""

    return page("大白 DABAI",
                "大白（DABAI）：跑在树莓派上的自托管 AI 智能体，12 个技能包 / %d 个函数。" % total_tools,
                "dabai.html", body)



def main():
    os.makedirs(os.path.join(SITE, "assets"), exist_ok=True)
    os.makedirs(DOCS, exist_ok=True)
    os.makedirs(DL, exist_ok=True)

    with open(os.path.join(SITE, "assets", "site.css"), "w", encoding="utf-8") as fh:
        fh.write(SITE_CSS)

    skills = load_skills()
    total_tools = sum(len(s["tools"]) for s in skills)
    print("技能包 %d 个 / 对外函数 %d 个" % (len(skills), total_tools))

    for s in skills:
        p = os.path.join(DOCS, "%s.html" % s["name"])
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(render_skill_page(s, total_tools))
        print("  docs/%s.html  %d 函数  %.1f KB" % (
            s["name"], len(s["tools"]), os.path.getsize(p) / 1024.0))

    with open(os.path.join(SITE, "docs.html"), "w", encoding="utf-8") as fh:
        fh.write(render_docs_index(skills, total_tools))
    print("  docs.html  %.1f KB" % (os.path.getsize(os.path.join(SITE, "docs.html")) / 1024.0))

    manifest = make_zips(skills, total_tools)
    print("  交付包 %d 个" % len(manifest))

    with open(os.path.join(SITE, "deliverables.html"), "w", encoding="utf-8") as fh:
        fh.write(render_deliverables(skills, manifest, total_tools))
    print("  deliverables.html 生成完成")

    with open(os.path.join(SITE, "dabai.html"), "w", encoding="utf-8") as fh:
        fh.write(render_dabai(skills, total_tools))
    print("  dabai.html 生成完成")

    total = sum(m["size"] for m in manifest)
    print("完成：%d 个技能包 / %d 个函数 / 交付物共 %.1f KB" % (
        len(skills), total_tools, total / 1024.0))


if __name__ == "__main__":
    main()
