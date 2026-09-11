"""
Mixamo 代理下载服务
- 用 Playwright 启动走 fq 代理的 Chromium 浏览器
- 前端通过 API 控制：启动浏览器、登录、下载动作、关闭
- 所有下载的 FBX 自动保存到 web/anim/ 对应分类目录
"""

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ========== fq 代理配置 ==========
FQ_ROOT = r"D:\AI\Chrome141_AllNew_2025.10.3"
PROXIES = {
    "clash":      {"server": "http://127.0.0.1:7890",  "port": 7890},
    "xray":       {"server": "socks5://127.0.0.1:1080", "port": 1080},
    "hysteria":   {"server": "socks5://127.0.0.1:1080", "port": 1080},
    "singbox":    {"server": "socks5://127.0.0.1:1080", "port": 1080},
    "naive":      {"server": "socks5://127.0.0.1:1080", "port": 1080},
    "hysteria2":  {"server": "socks5://127.0.0.1:1080", "port": 1080},
    "juicity":    {"server": "socks5://127.0.0.1:1080", "port": 1080},
    "mieru":      {"server": "socks5://127.0.0.1:3080", "port": 3080},
    "shadowquic": {"server": "socks5://127.0.0.1:4080", "port": 4080},
}
AUTO_START_CMD = ["fq.cmd", "start", "clash", "-NoChrome", "-NoElevate"]

SCRIPT_DIR = Path(__file__).parent
COOKIES_PATH = SCRIPT_DIR / "data" / "mixamo_cookies.json"
ANIM_DIR = SCRIPT_DIR / "web" / "anim"
CONFIG_PATH = ANIM_DIR / "animation-library.json"


def port_listening(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    r = s.connect_ex(("127.0.0.1", port))
    s.close()
    return r == 0


def ensure_proxy(proto: Optional[str] = None, auto_start: bool = True) -> str:
    """返回可用的本地代理地址"""
    if proto:
        info = PROXIES.get(proto.lower())
        if not info:
            raise ValueError(f"未知协议 {proto}，可选: {', '.join(PROXIES)}")
        if port_listening(info["port"]):
            return info["server"]
        if not auto_start:
            raise RuntimeError(f"{proto} 端口未监听，请先 fq start {proto} -NoChrome")
        _fq_start(proto)
        return info["server"]

    for name, info in PROXIES.items():
        if port_listening(info["port"]):
            return info["server"]

    if not auto_start:
        raise RuntimeError("没有运行中的代理，请先 fq start <协议> -NoChrome")
    _fq_start("clash")
    return PROXIES["clash"]["server"]


def _fq_start(proto: str):
    info = PROXIES[proto]
    subprocess.run(AUTO_START_CMD, cwd=FQ_ROOT,
                   shell=(os.name != "nt"),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        if port_listening(info["port"]):
            return
        time.sleep(1)
    raise RuntimeError(f"fq start {proto} 后端口仍未监听")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"categories": {}}
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def get_anim_by_name(name: str) -> Optional[dict]:
    config = load_config()
    for cat_key, cat in config.get('categories', {}).items():
        for anim in cat.get('animations', []):
            if anim['name'] == name:
                return {**anim, 'category': cat_key, 'category_label': cat.get('label', cat_key)}
    return None


class MixamoDownloadService:
    """Mixamo 代理下载服务（单例）"""

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.proxy_server = None
        self.proto = None
        self.is_running = False
        self.is_logged_in = False
        self.download_queue = []
        self.current_download = None
        self.download_stats = {"success": 0, "failed": 0, "skipped": 0}
        self.logs = []
        self._task = None
        self._stop_event = asyncio.Event()

    def _log(self, msg: str, level: str = "info"):
        entry = {"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level}
        self.logs.append(entry)
        if len(self.logs) > 200:
            self.logs = self.logs[-200:]

    async def start(self, proto: Optional[str] = None):
        """启动浏览器（走代理）"""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright 未安装，请先 pip install playwright && playwright install chromium")
        if self.is_running:
            return {"status": "already_running"}

        self._log("启动代理浏览器...")
        self.proxy_server = ensure_proxy(proto, auto_start=True)
        self.proto = proto or "auto"
        self._log(f"代理地址: {self.proxy_server}")

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            proxy={"server": self.proxy_server},
        )
        self.context = await self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
        )

        # 加载 cookies（如果有）
        if COOKIES_PATH.exists():
            try:
                with open(COOKIES_PATH, 'r', encoding='utf-8') as f:
                    cookies = json.load(f)
                await self.context.add_cookies(cookies)
                self._log("已加载保存的 cookies")
            except Exception as e:
                self._log(f"加载 cookies 失败: {e}", "warn")

        self.page = await self.context.new_page()
        self.is_running = True
        self._stop_event = asyncio.Event()   # 全新启动，确保不携带历史停止标记

        # 验证登录状态
        await self._check_login()
        return {"status": "started", "proxy": self.proxy_server, "is_logged_in": self.is_logged_in}

    async def _check_login(self):
        """检查 Mixamo 登录状态（真实判据：页面上无 Log In / Sign Up 入口）"""
        if not self.page:
            self.is_logged_in = False
            return
        try:
            await self.page.goto("https://www.mixamo.com/", wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(3000)
            try:
                self.is_logged_in = await self._page_shows_login_area(self.page)
            except Exception:
                self.is_logged_in = False
            self._log(f"登录状态: {'已登录' if self.is_logged_in else '未登录'}")
        except Exception as e:
            self._log(f"检查登录状态失败: {e}", "error")
            self.is_logged_in = False

    async def _page_shows_login_area(self, page) -> bool:
        """页面真实登录判据：有 Log In / Sign In / Sign Up 按钮 = 未登录；无 = 已登录"""
        try:
            return await page.evaluate(
                """() => {
                const t = (document.body && document.body.innerText) || '';
                const hasCta = /\b(Log In|Sign In|Sign Up( for Free)?)\b/i.test(t);
                return !hasCta;
                }"""
            )
        except Exception:
            return False

    async def goto_mixamo(self):
        """跳转到 Mixamo 主页"""
        if not self.page:
            raise RuntimeError("浏览器未启动")
        await self.page.goto("https://www.mixamo.com/", wait_until="domcontentloaded", timeout=15000)
        return {"status": "ok"}

    async def save_cookies(self):
        """保存当前 cookies"""
        if not self.context:
            raise RuntimeError("浏览器未启动")
        cookies = await self.context.cookies()
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(COOKIES_PATH, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        self.is_logged_in = True
        self._log("Cookies 已保存")
        return {"status": "saved", "count": len(cookies)}

    async def check_login_cookies(self) -> bool:
        """真实登录判据（不导航，不打断用户在 Adobe 登录页的输入）"""
        if not self.context:
            return False
        try:
            cookies = await self.context.cookies()
        except Exception:
            return False
        names = sorted({c['name'] for c in cookies})
        self._log(f"[debug] cookies={names}")

        # 强信号：Adobe IMS 会话令牌存在 → 真登录
        if 'ims_sid' in names or 'idg_token' in names:
            self.is_logged_in = True
            self._log("[login] 检测到 Adobe IMS 会话令牌，已登录")
            return True

        # 页面判据：当前页已进入登录区（无 Log In / Sign Up 入口）
        if self.page:
            try:
                if await self._page_shows_login_area(self.page):
                    self.is_logged_in = True
                    self._log("[login] 页面无登录入口，已登录")
                    return True
            except Exception:
                pass

        # 其余埋点/同意 cookie 一律不算登录信号
        self.is_logged_in = False
        return False

    async def debug_dom(self, query: str = "Idle", interact: bool = False):
        """调试：dump 搜索页真实 DOM 结构，定位动作卡片 / 下载按钮 / 弹窗（用完即删）"""
        if not self.page:
            return {"error": "browser not running"}
        self._stop_event.set()   # 停掉当前批量，避免抢导航
        url = f"https://www.mixamo.com/#/?query={query or 'Idle'}"
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            self._log(f"[debug] goto warn: {e}", "warn")
        await self.page.wait_for_timeout(4500)
        info = await self.page.evaluate(
            """() => {
            const cls = e => (e.className && typeof e.className==='string') ? e.className : String(e.tagName);
            const cardCands = [];
            document.querySelectorAll('.product.product-animation').forEach(d => {
                cardCands.push({cls: cls(d), w: Math.round(d.getBoundingClientRect().width)});
            });
            const btns = [];
            document.querySelectorAll('button').forEach(b => {
                const t = (b.innerText||b.textContent||'').trim();
                if (t) btns.push({t: t.slice(0,40), cls: cls(b).slice(0,70)});
            });
            return {
                url: location.href,
                htmlLen: document.documentElement.outerHTML.length,
                motionCardCount: document.querySelectorAll('.product.product-animation').length,
                cardSample: cardCands.slice(0,5),
                buttons: [...new Map(btns.map(x=>[x.t,x])).values()].slice(0,30),
            };
            }"""
        )
        # ---- 交互模式：点第一个动作卡片 + 点 DOWNLOAD，dump 弹窗 ----
        if interact:
            modal = {}
            # 点第一个动作卡片
            try:
                await self.page.locator(".product.product-animation").first.click(timeout=6000)
                await self.page.wait_for_timeout(2500)
            except Exception as e:
                modal["click_card_err"] = str(e)[:120]
            # 点面板 DOWNLOAD：正常点击失败则 JS 强制点击
            dl_clicked = False
            try:
                btn = self.page.locator("button.btn-block.btn.btn-primary").filter(
                    has_text="DOWNLOAD").first
                await btn.click(timeout=6000)
                dl_clicked = True
            except Exception as e:
                modal["click_dl_err"] = str(e)[:90]
                try:
                    await self.page.evaluate(
                        "document.querySelector('button.btn-block.btn.btn-primary').click()")
                    dl_clicked = True
                except Exception as e2:
                    modal["click_dl_js_err"] = str(e2)[:90]
            if dl_clicked:
                # 下载设置面板是异步弹出，轮询等待 CANCEL/面板出现（最多 8s）
                for _ in range(16):
                    has_panel = await self.page.evaluate(
                        "Array.from(document.querySelectorAll('button')).some(b=>/cancel/i.test(b.innerText||''))")
                    if has_panel:
                        break
                    await self.page.wait_for_timeout(500)
                await self.page.wait_for_timeout(500)
            # dump 弹窗内的按钮/输入框/复选框/单选项，并抓含 skin/skeleton 的字样
            try:
                modal["after"] = await self.page.evaluate(
                    """() => {
                    const cls = e => (e.className && typeof e.className==='string') ? e.className : String(e.tagName);
                    const vis = e => !!(e.offsetParent || e.getClientRects().length);
                    const text = n => (n && (n.innerText||n.textContent||'')).trim()||'';
                    const labelOf = el => {
                      const p = el.closest('label');
                      if (p) return text(p).slice(0,60);
                      const prev = el.previousElementSibling; if(prev) return text(prev).slice(0,60);
                      const par = el.parentElement; if(par) return text(par).slice(0,120);
                      return '';
                    };
                    const selects=[];
                    document.querySelectorAll('select').forEach(s=>{ if(vis(s)) selects.push({val:(s.value||'').toString(),label:labelOf(s).slice(0,60),options:Array.from(s.options).map(o=>o.textContent.trim()).slice(0,8)});});
                    const checks=[];
                    document.querySelectorAll('input').forEach(s=>{ if(vis(s)&&(s.type==='checkbox'||s.type==='radio')) checks.push({type:s.type,checked:!!s.checked,label:labelOf(s).slice(0,60)});});
                    // 找到含 CANCEL 的那块面板并输出其文本
                    let panelText='';
                    const nc=Array.from(document.querySelectorAll('button')).find(b=>/cancel/i.test(b.innerText||''));
                    if(nc){ for(let d=nc; d && d!==document.body; d=d.parentElement){ if(d.getClientRects().length){ panelText=text(d).slice(0,900); if(panelText.length>60) break; } } }
                    // 收集含 skin/without/with 的可见文本
                    const hits=[];
                    document.querySelectorAll('body *').forEach(el=>{
                      if(el.children.length>0) return;
                      const t=text(el); if(!t||t.length>80) return;
                      if(/skin|without|with|skeleton|rig|mesh|body/i.test(t)) hits.push(t.slice(0,80));
                    });
                    // 页面当前是否停在动作详情面板（有 DOWNLOAD 按钮）
                    const dlBtn = Array.from(document.querySelectorAll('button')).find(b=>/^download$/i.test((b.innerText||'').trim()));
                    return { selects, checks, panelText, hits:[...new Set(hits)].slice(0,40), dlBtnVisible: dlBtn?vis(dlBtn):false };
                    }"""
                )
            except Exception as e:
                modal["after_err"] = str(e)[:120]
            info = {"grid": info, "modal": modal}
        return info

    async def download_animation(self, anim_name: str) -> dict:
        """下载单个动作"""
        if not self.page or not self.browser:
            raise RuntimeError("浏览器未启动")

        anim = get_anim_by_name(anim_name)
        if not anim:
            raise ValueError(f"未知动作: {anim_name}")

        target_path = ANIM_DIR / anim['file']
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            self._log(f"{anim_name} 已存在，跳过", "success")
            return {"status": "skipped", "name": anim_name, "path": str(target_path)}

        self._log(f"下载: {anim_name} (搜索: {anim.get('search', anim_name)})")
        self.current_download = anim_name

        try:
            search_term = Path(anim['file']).stem.replace('_', ' ')
            search_url = f"https://www.mixamo.com/#/?query={search_term.replace(' ', '+')}"

            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            await self.page.wait_for_timeout(3500)

            # 关闭可能遮挡的遮罩/引导层
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            for sel in (".modal", ".onboarding-popover", ".ReactModalPortal", "[class*='overlay']"):
                try:
                    close = self.page.locator(f"{sel} [class*='close'], {sel} button[class*='close'], {sel} [aria-label*='close']").first
                    if await close.count():
                        await close.click(timeout=800)
                except Exception:
                    pass
            await self.page.wait_for_timeout(800)

            # 等待动作卡片出现（SPA 加载，wait_for_selector 自动处理导航重试）
            card_ready = False
            try:
                await self.page.wait_for_selector(".product.product-animation", timeout=20000)
                card_ready = True
            except Exception:
                card_ready = False
            if not card_ready:
                self._log(f"动作卡片加载超时（20s）: {anim_name}", "error")
                self.download_stats["failed"] += 1
                return {"status": "failed", "name": anim_name, "error": "card timeout"}

            # 点第一个动作卡片：正常点击失败则用 JS 强制点击兜底
            card_ok = False
            for attempt in range(3):
                try:
                    card = self.page.locator(".product.product-animation").first
                    await card.scroll_into_view_if_needed(timeout=3000)
                    await card.click(timeout=4000)
                    card_ok = True
                    break
                except Exception:
                    await self.page.wait_for_timeout(1200)
            if not card_ok:
                try:
                    await self.page.evaluate(
                        "document.querySelector('.product.product-animation') && document.querySelector('.product.product-animation').click()")
                    card_ok = True
                except Exception:
                    card_ok = False
            if not card_ok:
                self._log(f"找不到动作卡片（可能被遮罩/未登录）: {anim_name}", "error")
                self.download_stats["failed"] += 1
                return {"status": "failed", "name": anim_name, "error": "card not found"}
            await self.page.wait_for_timeout(1800)

            # 面板 DOWNLOAD 按钮：正常点击失败则 JS 强制点击
            panel_loc = self.page.locator("button.btn-block.btn.btn-primary").filter(has_text="DOWNLOAD").first
            clicked_dl = False
            try:
                await panel_loc.click(timeout=5000)
                clicked_dl = True
            except Exception:
                try:
                    await self.page.evaluate(
                        "document.querySelector('button.btn-block.btn.btn-primary') && document.querySelector('button.btn-block.btn.btn-primary').click()")
                    clicked_dl = True
                except Exception:
                    clicked_dl = False
            if not clicked_dl:
                self._log(f"没有可点的 DOWNLOAD 按钮: {anim_name}", "error")
                self.download_stats["failed"] += 1
                return {"status": "failed", "name": anim_name, "error": "download button not found"}

            # 下载设置弹窗是异步弹出：轮询等待 CANCEL 按钮 / Skin 下拉框出现（最多 10s）
            panel_ready = False
            for _ in range(20):
                try:
                    ready = await self.page.evaluate(
                        """() => {
                        const hasCancel = Array.from(document.querySelectorAll('button')).some(b=>/cancel/i.test(b.innerText||''));
                        const hasSkin = Array.from(document.querySelectorAll('select')).some(s=>{
                          const opts=Array.from(s.options).map(o=>o.textContent.trim());
                          return opts.some(o=>/without skin/i.test(o));
                        });
                        return hasCancel && hasSkin;
                        }"""
                    )
                    if ready:
                        panel_ready = True
                        break
                except Exception:
                    pass
                await self.page.wait_for_timeout(500)
            if not panel_ready:
                self._log(f"下载设置弹窗未出现（10s 超时）: {anim_name}", "warn")
            else:
                self._log("下载设置弹窗已就绪", "info")

            # 若弹出了下载设置弹窗，输出其内容便于诊断，并尝试取消“皮肤/网格”类选项
            try:
                dlg_info = await self.page.evaluate(
                    """() => {
                    const dlg = document.querySelector('.modal') || document.querySelector('.modal-dialog') || document.querySelector('[role="dialog"]');
                    if (!dlg || !dlg.offsetParent) return null;
                    const boxes=[];
                    document.querySelectorAll('input[type=checkbox]').forEach(c=>{
                      const lbl=(c.closest('label')||{}).innerText||c.parentElement.innerText||'';
                      if(lbl.trim()) boxes.push({checked:c.checked,label:lbl.trim().slice(0,60)});
                    });
                    // 抓下载格式下拉框的选项文本
                    const sel=document.querySelector('#formControlsSelect');
                    const opts=sel?Array.from(sel.options).map(o=>o.text.trim()):[];
                    return {shown:true, text:(dlg.innerText||'').trim().slice(0,300), checkboxes:boxes, format_options:opts, sel_value:sel?sel.value:null};
                    }"""
                )
                if dlg_info and dlg_info.get("shown"):
                    self._log(f"下载设置弹窗: {dlg_info}")
            except Exception as e:
                self._log(f"解析下载设置弹窗失败: {str(e)[:100]}", "warn")

            # 选择下载格式为「without skin」：定位 Skin 下拉框（第2个 select）并选 Without Skin
            try:
                skin_ok = False
                # 用 JS 找出 Skin 下拉框在所有 select 中的索引
                idx = await self.page.evaluate(
                    """() => {
                    const all = Array.from(document.querySelectorAll('select'));
                    for (let i=0;i<all.length;i++){
                      const s=all[i];
                      const label=(s.closest('label')?.innerText||s.previousElementSibling?.innerText||s.parentElement?.innerText||'').trim();
                      const opts=Array.from(s.options).map(o=>o.textContent.trim());
                      if(/skin/i.test(label) || opts.some(o=>/without skin/i.test(o))) return i;
                    }
                    return -1;
                    }"""
                )
                if idx is not None and idx >= 0:
                    sel = self.page.locator("select").nth(idx)
                    # 方法1：Playwright select_option（模拟真实用户选择，触发 React onChange）
                    try:
                        await sel.select_option(label="Without Skin", timeout=3000)
                        skin_ok = True
                        self._log("已选择 Without Skin", "success")
                    except Exception:
                        try:
                            await sel.select_option(index=1, timeout=3000)
                            skin_ok = True
                            self._log("已选择 Without Skin (index=1)", "success")
                        except Exception as e:
                            self._log(f"select_option 失败: {str(e)[:100]}", "warn")
                if not skin_ok:
                    # 方法2：JS 直接设置 Skin 下拉框 value + 触发事件
                    js_pick = await self.page.evaluate(
                        """() => {
                        const all = Array.from(document.querySelectorAll('select'));
                        let skinSel=null;
                        for (const s of all){
                          const label=(s.closest('label')?.innerText||s.previousElementSibling?.innerText||s.parentElement?.innerText||'').trim();
                          const opts=Array.from(s.options).map(o=>o.textContent.trim());
                          if(/skin/i.test(label) || opts.some(o=>/without skin/i.test(o))){ skinSel=s; break; }
                        }
                        if(!skinSel) return {ok:false, reason:'no skin select'};
                        const target=Array.from(skinSel.options).find(o=>/without skin/i.test(o.textContent));
                        if(!target) return {ok:false, reason:'no without skin option'};
                        skinSel.value=target.value; target.selected=true;
                        ['change','input'].forEach(ev=>skinSel.dispatchEvent(new Event(ev,{bubbles:true})));
                        return {ok:true, value:skinSel.value, text:target.textContent.trim()};
                        }"""
                    )
                    if js_pick and js_pick.get("ok"):
                        skin_ok = True
                        self._log(f"JS 已选择: {js_pick.get('text')}", "success")
                if not skin_ok:
                    self._log("未能切换 Without Skin，继续尝试下载（可能仍带皮肤）", "warn")
            except Exception as e:
                self._log(f"选择 without skin 失败: {str(e)[:100]}", "warn")

            # 命中皮肤/网格类复选框则取消勾选（纯动作）
            try:
                await self.page.evaluate(
                    """() => {
                    document.querySelectorAll('input[type=checkbox]').forEach(c=>{
                      const lbl=((c.closest('label')||{}).innerText||c.parentElement.innerText||'')+' '+((c.closest('.modal')||{}).innerText||'');
                      if(/skin|mesh|body|rig|含|皮肤|模型|geometry/i.test(lbl)){
                        if(c.checked){ c.checked=false; c.removeAttribute('checked');
                          ['change','click','input'].forEach(ev=>c.dispatchEvent(new Event(ev,{bubbles:true}))); }
                      }
                    });
                    }"""
                )
                self._log("已取消勾选皮肤/网格类复选框", "info")
            except Exception as e:
                self._log(f"取消皮肤复选框失败: {str(e)[:100]}", "warn")

            # 监听并保存下载：先点面板/弹窗内 Download
            try:
                async with self.page.expect_download(timeout=35000) as download_info:
                    try:
                        dlg = self.page.locator(
                            ".modal .btn, .modal-dialog .btn, [role='dialog'] .btn, .modal-content .btn"
                        ).filter(has_text="Download").first
                        await dlg.click(timeout=3000)
                    except Exception:
                        try:
                            await panel_loc.click(timeout=3000)
                        except Exception:
                            pass
            except PlaywrightTimeoutError:
                self._log(f"{anim_name} 下载触发超时", "error")
                self.download_stats["failed"] += 1
                return {"status": "failed", "name": anim_name, "error": "download timeout"}

            download = await download_info.value
            await download.save_as(str(target_path))

            self._log(f"✓ 保存到: {anim['file']}", "success")
            self.download_stats["success"] += 1
            return {"status": "success", "name": anim_name, "path": str(target_path)}

        except PlaywrightTimeoutError:
            self._log(f"{anim_name} 超时", "error")
            self.download_stats["failed"] += 1
            return {"status": "failed", "name": anim_name, "error": "timeout"}
        except Exception as e:
            self._log(f"{anim_name} 失败: {e}", "error")
            self.download_stats["failed"] += 1
            return {"status": "failed", "name": anim_name, "error": str(e)}
        finally:
            self.current_download = None

    async def batch_download(self, names: list):
        """批量下载（异步任务）"""
        self._stop_event.clear()   # 清除历史 stop 标记，确保这次能真正开跑
        self.download_stats = {"success": 0, "failed": 0, "skipped": 0}
        self.download_queue = names.copy()

        for name in names:
            if self._stop_event.is_set():
                break
            self.download_queue.pop(0)
            await self.download_animation(name)
            await asyncio.sleep(0.5)

        self._log(f"批量下载完成: 成功{self.download_stats['success']} "
                  f"失败{self.download_stats['failed']} "
                  f"跳过{self.download_stats['skipped']}", "success")

    async def stop(self):
        """停止并关闭浏览器"""
        self._stop_event.set()
        if self.browser:
            try:
                await self.browser.close()
            except:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except:
                pass
        self.browser = None
        self.context = None
        self.page = None
        self.playwright = None
        self.is_running = False
        self.is_logged_in = False
        self._log("浏览器已关闭")
        return {"status": "stopped"}

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "is_logged_in": self.is_logged_in,
            "proxy": self.proxy_server,
            "proto": self.proto,
            "current_download": self.current_download,
            "queue_remaining": len(self.download_queue),
            "stats": self.download_stats.copy(),
            "logs": self.logs[-50:],
        }


# 全局单例
service = MixamoDownloadService()
