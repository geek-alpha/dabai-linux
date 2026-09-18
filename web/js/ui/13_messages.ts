import type { AppKernel, TokenStats } from '../types/app-kernel.js';
import type { UsageMessage } from '../types/ws-protocol.js';

export default (function init(App: AppKernel) {
  /* ============================================================
   *  消息渲染
   * ============================================================ */
  // 聊天面板收起时新消息到达 → 提示切换按钮
  App.notifyFullscreenChat = function notifyFullscreenChat() {
    const panel = document.getElementById('chat-panel');
    if (panel!.classList.contains('collapsed')) {
      App.chatToggle!.classList.add('has-new');
    }
  };
  App.isFullscreen = false;

  /* ============================================================
   *  消息内联媒体：识别文本里的图片/视频链接并渲染成可查看的内容
   *  （智能体画图 / 生成视频时，链接直接变成图片/视频）
   * ============================================================ */
  const MEDIA_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'bmp', 'mp4', 'webm', 'ogv', 'mov', 'm4v'];
  const MEDIA_URL_RE = /(?:https?:\/\/[^\s<>"'`]+|\/[^\s<>"'`]+|[A-Za-z]:\\[^\s<>"'`]+)\.(?:png|jpe?g|gif|webp|avif|bmp|mp4|webm|ogv|mov|m4v)(?:[?#][^\s<>"'`]*)?/gi;
  const MEDIA_TRAIL_RE = /[.,;:!?)\]}\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f\u201d\u2019'`*_]+$/;

  const ESC_MAP: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  // 纯字符串转义。原来每次建一个临时 div、写 textContent、读 innerHTML ——
  // 流式期间每帧都要转义整段文本，建/删 DOM 的成本白白摊在每帧预算里
  // （实测 escHtml 是流式期 JS 侧前三热点）。转义结果等价，且引号也一并转义，更安全。
  function escHtml(s: string | null | undefined) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
  }
  function isVideoUrl(url: string) {
    return /\.(mp4|webm|ogv|mov|m4v)(?:[?#]|$)/i.test(url);
  }

  // 从文本里提取图片/视频链接（供聊天框渲染 & 大屏媒体墙共用）
  App.extractMediaUrls = function extractMediaUrls(text: string) {
    if (!text) return [];
    const out: string[] = [];
    MEDIA_URL_RE.lastIndex = 0;
    let m;
    while ((m = MEDIA_URL_RE.exec(text)) !== null) {
      const url = String(m[0]).replace(MEDIA_TRAIL_RE, '');
      const ext = (url.match(/\.([a-z0-9]+)(?:[?#]|$)/i) || [])[1];
      if (ext && MEDIA_EXTS.includes(ext.toLowerCase()) && !out.includes(url)) out.push(url);
    }
    return out;
  };

  // 本地绝对路径 → /media 通用路由：浏览器把 /home/x/a.png 当同源相对路径请求，必然 404。
  // Windows 盘符与 Unix 绝对路径都要转；已是前端挂载前缀的（/static、/generated…）原样放行。
  const FRONT_ROUTE_RE = /^\/(?:static|media|generated|models|audio|backgrounds|anim|mpt|downloads|api|ws)(?:[/?#]|$)/;
  App.toMediaUrl = function toMediaUrl(raw: string) {
    const u = String(raw == null ? '' : raw).trim();
    if (!u || u.startsWith('//')) return u;
    const winAbs = /^[A-Za-z]:[\\/]/.test(u);
    const unixAbs = u.startsWith('/') && !FRONT_ROUTE_RE.test(u);
    if (!winAbs && !unixAbs) return u;
    // 先解码归一化再编码：renderMsgMedia 走 new URL() 归一化时，浏览器已把中文/空格
    // 转成 %XX，这里再 encodeURIComponent 会二次编码（%E8 → %25E8），服务端解出来是
    // 字面量路径 → /media 404 → <video> onerror 隐藏，中文名媒体一律显示不出来。
    let p = u.replace(/\\/g, '/');
    try { p = decodeURIComponent(p); } catch (e) { /* 非法转义序列原样保留 */ }
    return '/media?path=' + encodeURIComponent(p);
  };

  // 普通网址链接化（媒体链接已单独处理）：raw 文本段 → 转义 + 可点击 <a>
  const PLAIN_URL_RE = /https?:\/\/[^\s<>"'`\u3000\uff08\uff09]+/gi;
  function linkifySegment(seg: string) {
    if (!seg) return '';
    let out = '';
    let last = 0;
    PLAIN_URL_RE.lastIndex = 0;
    let m;
    while ((m = PLAIN_URL_RE.exec(seg)) !== null) {
      const url = String(m[0]).replace(MEDIA_TRAIL_RE, '');
      if (!url) continue;
      out += escHtml(seg.slice(last, m.index)) +
        '<a class="msg-link" href="' + escHtml(url) + '" target="_blank" rel="noopener">' + escHtml(url) + '</a>';
      last = m.index + url.length;
    }
    out += escHtml(seg.slice(last));
    return out;
  }

  // ---------- 轻量 Markdown 渲染（结构化展示 AI 回复） ----------
  // 支持：**加粗** / *斜体* / `行内代码` / ```代码块``` / # 标题 /
  // - 无序列表 / 1. 有序列表 / > 引用 / | 表格 | / [链接](url) / 普通网址 / 分隔线。
  // 全程先 HTML 转义再转换，杜绝注入。
  function inlineMd(s: string): string {
    let t = escHtml(s);
    const codes: string[] = [];
    const links: string[] = [];
    // 行内代码（先保护，避免被加粗/斜体/链接规则破坏）
    t = t.replace(/`([^`\n]+)`/g, (_m, c) => {
      codes.push('<code class="md-ic">' + c + '</code>');
      return '\uE000' + (codes.length - 1) + '\uE001';
    });
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    // [文字](网址) 链接（先保护，避免被下面的裸网址规则二次包裹）
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_m, txt, url) => {
      links.push('<a class="md-link" href="' + url + '" target="_blank" rel="noopener">' + txt + '</a>');
      return '\uE002' + (links.length - 1) + '\uE003';
    });
    // 裸网址链接化
    t = t.replace(/https?:\/\/[^\s<>\u3000\uff08\uff09]+/g,
      '<a class="md-link" href="$&" target="_blank" rel="noopener">$&</a>');
    t = t.replace(/\uE002(\d+)\uE003/g, (_m, n) => links[Number(n)] || '');
    t = t.replace(/\uE000(\d+)\uE001/g, (_m, n) => codes[Number(n)] || '');
    // 行内换行（由段落/引用/列表续行以 \n 传入）转成 <br>
    t = t.replace(/\n/g, '<br>');
    return t;
  }

  function mdToHtml(src: string): string {
    if (!src) return '';
    const lines = src.replace(/\r\n?/g, '\n').split('\n');
    const out: string[] = [];
    let inCode = false;
    let codeLang = '';
    const codeBuf: string[] = [];
    const flushCode = () => {
      if (codeBuf.length) {
        const cls = codeLang ? ' class="lang-' + escHtml(codeLang) + '"' : '';
        out.push('<pre class="md-code"><code' + cls + '>' + codeBuf.join('\n') + '</code></pre>');
        codeBuf.length = 0;
      }
      codeLang = '';
      inCode = false;
    };
    let para: string[] = [];
    const flushPara = () => {
      if (para.length) {
        out.push('<p class="md-p">' + inlineMd(para.join('\n')) + '</p>');
        para.length = 0;
      }
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const fm = line.match(/^```([\w+#-]*)\s*$/);
      if (fm) {
        if (inCode) flushCode();
        else { flushPara(); inCode = true; codeLang = fm[1] || ''; }
        continue;
      }
      if (inCode) { codeBuf.push(escHtml(line)); continue; }
      const t = line.trim();
      if (!t) { flushPara(); continue; }
      // 表格：连续 | 行，第二行为分隔线
      if (/^\|.*\|$/.test(t) && lines[i + 1] && /^\|[\s:|-]+\|$/.test(lines[i + 1].trim())) {
        flushPara();
        const rows: string[][] = [];
        let j = i;
        while (j < lines.length && /^\|.*\|$/.test(lines[j].trim())) {
          rows.push(lines[j].trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()));
          j++;
        }
        if (rows.length >= 2) {
          out.push('<table class="md-table"><thead><tr>' +
            rows[0].map(c => '<th>' + inlineMd(c) + '</th>').join('') +
            '</tr></thead>' + (rows.length > 2 ? '<tbody>' + rows.slice(2).map(r =>
              '<tr>' + r.map(c => '<td>' + inlineMd(c) + '</td>').join('') + '</tr>').join('') + '</tbody>' : '') +
            '</table>');
        } else {
          out.push('<p class="md-p">' + inlineMd(t) + '</p>');
        }
        i = j - 1;
        continue;
      }
      // 标题
      const hm = t.match(/^(#{1,4})\s+(.*)$/);
      if (hm) {
        flushPara();
        const lv = hm[1].length;
        out.push('<h' + lv + ' class="md-h md-h' + lv + '">' + inlineMd(hm[2]) + '</h' + lv + '>');
        continue;
      }
      // 引用
      if (t.startsWith('>')) {
        flushPara();
        const q: string[] = [t.replace(/^>\s?/, '')];
        while (i + 1 < lines.length && lines[i + 1].trim().startsWith('>')) {
          i++;
          q.push(lines[i].trim().replace(/^>\s?/, ''));
        }
        out.push('<blockquote class="md-quote">' + inlineMd(q.join('\n')) + '</blockquote>');
        continue;
      }
      // 无序列表（吸收后续缩进续行）
      const um = t.match(/^([-*+])\s+(.*)$/);
      if (um) {
        flushPara();
        const items: string[] = [um[2]];
        while (i + 1 < lines.length) {
          const nt = lines[i + 1].trim();
          const nm = nt.match(/^([-*+])\s+(.*)$/);
          if (nm) { items.push(nm[2]); i++; continue; }
          if (/^\d+\.\s+/.test(nt) || nt.startsWith('>') || nt.startsWith('#')
              || nt.startsWith('```')) break;
          if (nt) { items[items.length - 1] += '\n' + nt; i++; continue; }
          break;
        }
        out.push('<ul class="md-ul">' + items.map(x => '<li>' + inlineMd(x) + '</li>').join('') + '</ul>');
        continue;
      }
      // 有序列表
      const om = t.match(/^(\d+)\.\s+(.*)$/);
      if (om) {
        flushPara();
        const items: string[] = [om[2]];
        while (i + 1 < lines.length) {
          const nt = lines[i + 1].trim();
          const nm = nt.match(/^(\d+)\.\s+(.*)$/);
          if (nm) { items.push(nm[2]); i++; continue; }
          if (/^[-*+]\s+/.test(nt) || nt.startsWith('>') || nt.startsWith('#')
              || nt.startsWith('```')) break;
          if (nt) { items[items.length - 1] += '\n' + nt; i++; continue; }
          break;
        }
        out.push('<ol class="md-ol">' + items.map(x => '<li>' + inlineMd(x) + '</li>').join('') + '</ol>');
        continue;
      }
      // 分隔线
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) { flushPara(); out.push('<hr class="md-hr">'); continue; }
      para.push(line);
    }
    flushCode();
    flushPara();
    // 元素之间不保留换行：正文容器是 white-space: pre-wrap，
    // 输出里的缩进/换行会被当成真实空白显示成空行（流式与收尾渲染都会脏）
    return out.join('');
  }

  // 公开给音频/流式模块：正文与思维链共用同一套实时 Markdown 渲染
  App.mdToHtml = mdToHtml;

  // 把文本渲染进消息元素：Markdown 结构化 + 媒体链接内联成 <img>/<video>，其余转义
  App.renderMsgMedia = function renderMsgMedia(el: HTMLElement | null, text: string, asUser = false) {
    if (!el) return;
    // 服务器侧的图片标记（[[IMG:/abs/x.png]]：用户上传的图 / 工具截图）剥成同源
    // <img>，字面标记不给人看。历史消息从记忆里读回来时也带这个标记，
    // 不处理就会在旧会话里看到一串本地路径。
    const imgMarks: string[] = [];
    text = String(text || '').replace(/\[\[IMG:([^\]\n]+)\]\]/g, (_m, p) => {
      imgMarks.push(App.toMediaUrl ? App.toMediaUrl(String(p)) : String(p));
      return '';
    }).trim();
    if (!text && !imgMarks.length) { el.textContent = ''; return; }
    const urls = App.extractMediaUrls(text);
    let html = mdToHtml(text);
    // 媒体链接：把渲染出的普通链接升级成内联 <img>/<video>
    for (const rawUrl of urls) {
      // 同源绝对 URL 归一化为相对路径（https://host:port/xxx → /xxx）：
      // 与图片一致走同源加载，避免自签名证书在 <video> 里被浏览器拦截
      let url = rawUrl;
      try {
        const u = new URL(rawUrl, location.href);
        if (u.origin === location.origin) url = u.pathname + u.search + u.hash;
      } catch (e) { /* 非 URL 原样保留 */ }
      // 本地绝对路径（C:\...、/home/...）→ 走 /media 通用路由，让任意位置媒体都能显示
      url = App.toMediaUrl(url);
      const attrs = 'src="' + escHtml(url) + '"';
      const failFallback = 'this.style.display=\'none\';this.parentNode.classList.add(\'media-failed\')';
      const media = isVideoUrl(url)
        ? '<video class="msg-media msg-media-video" ' + attrs + ' controls playsinline preload="metadata" referrerpolicy="no-referrer" onerror="' + failFallback + '"></video>'
        : '<img class="msg-media msg-media-img" ' + attrs + ' alt="' + escHtml(url) + '" loading="lazy" referrerpolicy="no-referrer" onerror="' + failFallback + '">';
      const wrapped = '<a class="msg-media-link" href="' + escHtml(url) + '" target="_blank" rel="noopener" title="' + escHtml(url) + '">' + media + '</a>';
      // [文字](媒体url) 形式：mdToHtml 已生成完整锚点，必须整块替换——只替换 href 里的
      // URL 串会把 wrapped 塞进属性值，产出畸形 HTML（视频/图片被吞掉）。
      const anchorOpen = '<a class="md-link" href="' + escHtml(rawUrl) + '"';
      const a0 = html.indexOf(anchorOpen);
      const a1 = a0 >= 0 ? html.indexOf('</a>', a0) : -1;
      if (a1 >= 0) {
        html = html.slice(0, a0) + wrapped + html.slice(a1 + 4);
      } else if (html.includes(escHtml(rawUrl))) {
        html = html.split(escHtml(rawUrl)).join(wrapped);
      } else if (html.includes(escHtml(url))) {
        html = html.split(escHtml(url)).join(wrapped);
      }
    }
    el.innerHTML = html;
    // 标记剥出来的图贴在正文上方（点击可放大）
    if (imgMarks.length) {
      const box = document.createElement('div');
      // 用户附件图走等格网格；历史消息重载回来只有 [[IMG:]] 标记，也要跟刚发时长得一样
      box.className = asUser ? 'msg-attach imgs' : 'msg-attach';
      box.dataset.count = String(Math.min(imgMarks.length, 9));
      imgMarks.forEach((u) => {
        const a = document.createElement('a');
        a.className = 'msg-media-link';
        a.href = u;
        a.target = '_blank';
        a.rel = 'noopener';
        const img = document.createElement('img');
        img.className = 'msg-media msg-media-img';
        img.src = u;
        img.alt = '图片';
        img.loading = 'lazy';
        a.appendChild(img);
        box.appendChild(a);
      });
      el.prepend(box);
    }
    // 视频起播：浏览器默认不自动播放（黑框），消息渲染完成后尝试自动播放。
    // 注意：renderMsgMedia 执行时 el 可能还未 append 到 DOM（addAIMsg 先渲染后挂载），
    // 此时 play() 会被拒绝、loadedmetadata 也可能提前触发被 once 消费，
    // 所以用 setTimeout 延迟到挂载完成后再播，并保留事件兜底。
    const v = el.querySelector('video.msg-media-video') as HTMLVideoElement | null;
    if (v) {
      v.muted = true; // 自动播放必须静音（浏览器策略），用户可手动取消静音
      v.preload = 'auto';
      const tryPlay = () => {
        const p = v.play();
        if (p && p.catch) p.catch(() => {});
      };
      v.addEventListener('loadedmetadata', tryPlay, { once: true });
      v.addEventListener('loadeddata', tryPlay, { once: true });
      v.addEventListener('canplay', tryPlay, { once: true });
      // 延迟到元素挂载进 DOM 后再播（渲染时 el 可能尚未 append）
      setTimeout(tryPlay, 0);
      setTimeout(tryPlay, 300);
      setTimeout(tryPlay, 1000);
    }
  };

  // 大图查看器：点击消息内图片弹层放大
  App.openMediaViewer = function openMediaViewer(url: string | null) {
    if (!url) return;
    let ov = document.getElementById('media-viewer') as HTMLElement | null;
    if (!ov) {
      ov = document.createElement('div');
      ov.id = 'media-viewer';
      ov.className = 'media-viewer';
      ov.innerHTML = '<div class="media-viewer-backdrop"></div><img class="media-viewer-img" alt=""><button class="media-viewer-close" title="关闭">×</button>';
      ov.addEventListener('click', (e) => {
        const t = e.target as HTMLElement;
        if (t === ov || t.classList.contains('media-viewer-backdrop') || t.classList.contains('media-viewer-close')) {
          ov!.classList.remove('show');
        }
      });
      document.body.appendChild(ov);
    }
    const img = ov.querySelector('.media-viewer-img') as HTMLImageElement;
    img.onload = () => ov!.classList.add('show');
    img.onerror = () => { if (App.showToast) App.showToast('图片加载失败'); ov!.classList.remove('show'); };
    img.src = url;
  };

  // 限制消息 DOM 节点数量，防止长时间聊天导致 DOM 膨胀卡顿
  App._trimMessages = function _trimMessages() {
    const all = App.messagesEl!.querySelectorAll('.msg:not(.streaming):not(.typing)');
    if (all.length > 200) {
      // 移除最旧的一半消息；用户正在上方翻阅时保持视觉位置不跳动
      const el = App.messagesEl!;
      const prevTop = el.scrollTop;
      const prevH = el.scrollHeight;
      const removeCount = Math.floor(all.length / 2);
      for (let i = 0; i < removeCount; i++) {
        all[i].remove();
      }
      const dh = prevH - el.scrollHeight;
      if (dh > 0 && prevTop > 0) el.scrollTop = Math.max(0, prevTop - dh);
    }
  };

  /* ============================================================
   *  智能滚动：用户上滑翻阅历史时暂停自动滚底，
   *  新消息以计数 + 未读高亮提示；滚回底部自动恢复跟随。
   * ============================================================ */
  App._newMsgCount = 0;
  let _scrollBottomPending = false;

  App.isNearBottom = function isNearBottom() {
    const el = App.messagesEl!;
    return el.scrollHeight - el.scrollTop - el.clientHeight < 90;
  };

  function clearUnread() {
    App.messagesEl!.querySelectorAll('.msg.unread').forEach((n) => n.classList.remove('unread'));
  }

  // 登记"来了一条新消息"（仅在用户未在底部时产生提示）；各卡片模块追加内容时调用
  App.bumpNewMsg = function bumpNewMsg(el?: HTMLElement | null) {
    if (App.isNearBottom()) return;
    App._newMsgCount += 1;
    if (el && el.classList && el.classList.contains('msg')) el.classList.add('unread');
    App.updateScrollHint();
  };

  App.scrollToBottom = function scrollToBottom(force?: boolean) {
    if (force === true) {
      // 平滑滚动动画期间不显示"回到底部"提示，避免闪烁
      App._forceScrolling = true;
      if (App._forceScrollTimer) clearTimeout(App._forceScrollTimer);
      App._forceScrollTimer = setTimeout(() => { App._forceScrolling = false; App.updateScrollHint(); }, 650);
      App.messagesEl!.scrollTo({ top: App.messagesEl!.scrollHeight, behavior: 'smooth' });
      App._newMsgCount = 0;
      clearUnread();
    } else {
      // 正在底部跟随：直接贴底（auto 避免流式高频更新时平滑动画抖动）。
      // 流式每 token 都会调到这里，而 scrollHeight/clientHeight 读取会强制同步布局，
      // 因此合并到 rAF：一帧最多一次贴底 + 一次布局读取。
      if (!_scrollBottomPending) {
        _scrollBottomPending = true;
        requestAnimationFrame(() => {
          _scrollBottomPending = false;
          if (App.isNearBottom()) App.messagesEl!.scrollTop = App.messagesEl!.scrollHeight;
          App.updateScrollHint();
        });
      }
      return;
    }
    App.updateScrollHint();
  };

  App.updateScrollHint = function updateScrollHint() {
    if (App._forceScrolling) return;
    const atBottom = App.isNearBottom();
    if (atBottom && App._newMsgCount > 0) {
      App._newMsgCount = 0;
      clearUnread();
    }
    const hint = App.scrollHint;
    if (!hint) return;
    if (atBottom) {
      hint.classList.remove('show');
      return;
    }
    hint.classList.add('show');
    if (App._newMsgCount > 0) {
      hint.innerHTML = '↓ <span class="scroll-hint-badge">' + App._newMsgCount + '</span> 新消息';
    } else {
      hint.textContent = '↓';
    }
  };
  App.messagesEl!.addEventListener('scroll', App.updateScrollHint, { passive: true });
  App.scrollHint!.addEventListener('click', () => App.scrollToBottom(true));

  const EXT_TONES: Record<string, string> = {
    pdf: 'tone-red',
    doc: 'tone-blue', docx: 'tone-blue', rtf: 'tone-blue', pages: 'tone-blue',
    xls: 'tone-green', xlsx: 'tone-green', csv: 'tone-green', tsv: 'tone-green',
    ppt: 'tone-orange', pptx: 'tone-orange', key: 'tone-orange',
    zip: 'tone-amber', rar: 'tone-amber', '7z': 'tone-amber', tar: 'tone-amber', gz: 'tone-amber',
    png: 'tone-cyan', jpg: 'tone-cyan', jpeg: 'tone-cyan', gif: 'tone-cyan', webp: 'tone-cyan',
  };

  function fmtSize(n: any) {
    const b = Number(n) || 0;
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(b < 10240 ? 1 : 0) + ' KB';
    return (b / 1048576).toFixed(b < 10485760 ? 1 : 0) + ' MB';
  }

  /** 文件卡片：类型徽章 + 文件名（超长省略）+ 体积，整行成块。
      url 为空（历史里剥出来的老附件、越界路径）→ 降级成不可点的静态卡片，
      不要留个 href="" 的链接——点一下会把整页刷掉。 */
  function buildFileCard(a: any) {
    const f: any = document.createElement(a.url ? 'a' : 'span');
    f.className = 'msg-file';
    if (a.url) {
      f.href = a.url;
      f.target = '_blank';
      f.rel = 'noopener';
    }
    f.title = a.name || '文件';
    const ext = String(a.ext || (a.name || '').split('.').pop() || 'file').toUpperCase().slice(0, 4);
    const badge = document.createElement('span');
    badge.className = 'msg-file-badge ' + (EXT_TONES[ext.toLowerCase()] || 'tone-violet');
    badge.textContent = ext;
    const meta = document.createElement('span');
    meta.className = 'msg-file-meta';
    const nm = document.createElement('span');
    nm.className = 'msg-file-name';
    nm.textContent = a.name || '文件';
    const sz = document.createElement('span');
    sz.className = 'msg-file-size';
    sz.textContent = fmtSize(a.size);
    meta.appendChild(nm);
    meta.appendChild(sz);
    f.appendChild(badge);
    f.appendChild(meta);
    return f;
  }

  App.addUserMsg = function addUserMsg(text: string, fromVoice = false, attachments?: any[]) {
    const el = document.createElement('div');
    el.className = 'msg user';
    // 刚发出去的附件 + 历史里剥出来的附件：图片成组给网格缩略图、
    // 其它成组给文件卡片，点开看原件
    const atts = (attachments || []).filter((a: any) => a && (a.url || a.name));
    if (atts.length) {
      // 图片成组走等格网格、文件成组走整行卡片：混在同一个 flex 行里必然大小参差、基线乱。
      // 拿不到 URL 的图片进不了 <img>，当文件卡片展示，至少让用户知道发过这张图。
      const pics = atts.filter((a: any) => a.kind === 'image' && a.url);
      const docs = atts.filter((a: any) => a.kind !== 'image' || !a.url);
      if (pics.length) {
        const grid = document.createElement('div');
        grid.className = 'msg-attach imgs';
        grid.dataset.count = String(Math.min(pics.length, 9));
        pics.forEach((a: any) => {
          const link = document.createElement('a');
          link.className = 'msg-media-link';
          link.href = a.url;
          link.target = '_blank';
          link.rel = 'noopener';
          const img = document.createElement('img');
          img.className = 'msg-media msg-media-img';
          img.src = a.url;
          img.alt = a.name || '图片';
          img.loading = 'lazy';
          link.appendChild(img);
          grid.appendChild(link);
        });
        el.appendChild(grid);
      }
      if (docs.length) {
        const list = document.createElement('div');
        list.className = 'msg-files';
        docs.forEach((a: any) => list.appendChild(buildFileCard(a)));
        el.appendChild(list);
      }
    }
    // 只有附件没有文字时不塞空正文（气泡里只有图）
    if (text) {
      const body = document.createElement('div');
      body.className = 'msg-body';
      if (App.renderMsgMedia) App.renderMsgMedia(body, text, true);
      else body.textContent = text;
      el.appendChild(body);
    }
    if (fromVoice) el.title = '来自语音输入';
    App.messagesEl!.appendChild(el);
    App._trimMessages();
    App.scrollToBottom(true); // 用户自己的发言：始终跟到最新
    App.notifyFullscreenChat();
  };
  App.addAIMsg = function addAIMsg(text: string) {
    const el = document.createElement('div');
    el.className = 'msg ai';
    if (App.renderMsgMedia) App.renderMsgMedia(el, text);
    else el.textContent = text;
    App.messagesEl!.appendChild(el);
    App._trimMessages();
    App.bumpNewMsg(el); // 用户在翻历史：标记未读但不打扰
    App.scrollToBottom();
    App.notifyFullscreenChat();
    // 图片/视频链接同步投递到角色身后的任务直播大屏
    if (App.taskBoardAddMedia) {
      const urls = App.extractMediaUrls ? App.extractMediaUrls(text) : [];
      if (urls.length) App.taskBoardAddMedia(urls);
    }
    // 最近的交互信息 → 大屏全屏焦点（每条停留 1 分钟，无新信息回待机）
    if (App.taskBoardOnInteraction) {
      App.taskBoardOnInteraction({
        kind: 'msg',
        title: '💬 大白 · 刚刚回复',
        text,
        accent: '#7c5cff',
        tag: '💬 大白回复'
      });
    }
  };
  App.addSystemMsg = function addSystemMsg(text: string) {
    const el = document.createElement('div');
    el.className = 'msg system';
    el.textContent = text;
    App.messagesEl!.appendChild(el);
    App._trimMessages();
  };
  App.showTyping = function showTyping() {
    App.removeTyping();
    const el = document.createElement('div');
    el.className = 'msg ai typing';
    el.id = 'typing-indicator';
    el.innerHTML = '<span></span><span></span><span></span>';
    App.messagesEl!.appendChild(el);
    App.scrollToBottom();
  };
  App.removeTyping = function removeTyping() {
    const t = App.$('typing-indicator');
    if (t) t.remove();
  };

  /* ============================================================
   *  回合气泡：正文 + 内联工具块（编程工具式）
   *  一轮回复的正文按 Markdown 分段展示；工具调用以可展开的小块内联在
   *  对应正文段后面（不叫工具链、不编号），展开可见参数/结果/详细步骤；
   *  思考/推理内容一律不展示。
   * ============================================================ */
  App._turnMsgEl = null;
  let segEl: HTMLElement | null = null;  // 当前正文段（流式写入目标）
  let segSealed = false;                 // 当前正文段已封存（其后已有工具块）

  /** 新一轮回复开始：创建（或复用逻辑上最新的）回合气泡 */
  App.beginTurnBubble = function beginTurnBubble(sessionId?: string | null) {
    App.finishTurn(); // 上一轮若未收尾，先静态收尾
    const el = document.createElement('div');
    el.className = 'msg ai turn';
    el.dataset.session = sessionId || '';
    el.innerHTML = '<div class="turn-seg"></div>';
    App.messagesEl!.appendChild(el);
    App._turnMsgEl = el;
    segEl = el.querySelector('.turn-seg') as HTMLElement;
    segSealed = false;
    App._trimMessages();
    App.bumpNewMsg(el);
    App.scrollToBottom();
    App.notifyFullscreenChat();
    return el;
  };

  /** 获取当前正文段；已封存或缺失则新建一段，追加到气泡末尾 */
  App.ensureTurnSeg = function ensureTurnSeg(): HTMLElement | null {
    const b = App._turnMsgEl;
    if (!b || !document.body.contains(b)) return null;
    if (!segEl || !document.body.contains(segEl) || segSealed) {
      segEl = document.createElement('div');
      segEl.className = 'turn-seg';
      b.appendChild(segEl);
      segSealed = false;
    }
    return segEl;
  };

  /** 封存当前正文段：工具块将插入其后，后续正文另起一段 */
  App.sealTurnSeg = function sealTurnSeg() {
    if (segEl && document.body.contains(segEl)) {
      const raw = segEl.dataset.raw || '';
      if (!raw.trim()) {
        // 封存的是空段（工具先于正文到达）：移除，避免空占位
        segEl.remove();
        segEl = null;
      } else if (App.renderMsgMedia) {
        // 段落已完整：渲染最终 Markdown（流式期间保持纯文本，避免半截语法乱码）
        App.renderMsgMedia(segEl, raw);
      }
    }
    segSealed = true;
  };

  /** 旧 thinking_text 通道：保留为空操作（新链路走 reasoning 事件） */
  App.appendTurnThinking = function appendTurnThinking(_text: string) {};

  /** 思考中实时指示：回复气泡底部一行持续更新的 reasoning_content 尾段。
   *
   *  服务端已节流（~250ms / 只推尾部 180 字），这里只做轻量文本更新，
   *  不 Markdown、不朗读、不进正文；正文开始流式或整轮收尾时清除。
   *  推理先于 thinking 到达时兜底创建回合气泡，避免指示无处安放。
   */
  let reasoningEl: HTMLElement | null = null;
  App.handleReasoning = function handleReasoning(text: string, sessionId?: string | null) {
    if (!text) return;
    const b = App._turnMsgEl;
    if (!b || !document.body.contains(b)) {
      if (!App.beginTurnBubble) return;
      App.beginTurnBubble(sessionId || null);
    }
    const bubble = App._turnMsgEl;
    if (!bubble || !document.body.contains(bubble)) return;
    if (!reasoningEl || !document.body.contains(reasoningEl)) {
      reasoningEl = document.createElement('div');
      reasoningEl.className = 'turn-reasoning';
      reasoningEl.innerHTML =
        '<span class="turn-reasoning-dot"></span>' +
        '<span class="turn-reasoning-label">思考中</span>' +
        '<span class="turn-reasoning-text"></span>';
      bubble.appendChild(reasoningEl);
    }
    const txt = reasoningEl.querySelector('.turn-reasoning-text') as HTMLElement | null;
    if (txt) txt.textContent = text;
    App.scrollToBottom();
  };
  App.clearReasoningLine = function clearReasoningLine() {
    if (reasoningEl && document.body.contains(reasoningEl)) reasoningEl.remove();
    reasoningEl = null;
  };

  /** 回合状态行：网络波动自动重连等运行提示（气泡底部一行，随时更新/清除）。
   *  不打断流程、不朗读、不进正文；整轮收尾（finishTurn）时自动清除。 */
  let turnStatusEl: HTMLElement | null = null;
  App.setTurnStatus = function setTurnStatus(text: string) {
    if (!text) {
      App.clearTurnStatus();
      return;
    }
    let b = App._turnMsgEl;
    if (!b || !document.body.contains(b)) {
      // 状态先于回合气泡到达（刷新接管竞态）：兜底创建
      if (!App.beginTurnBubble) return;
      b = App.beginTurnBubble(App.currentReplySession || null);
    }
    if (!b || !document.body.contains(b)) return;
    if (!turnStatusEl || !document.body.contains(turnStatusEl)) {
      turnStatusEl = document.createElement('div');
      turnStatusEl.className = 'turn-status';
      turnStatusEl.innerHTML =
        '<span class="turn-status-icon">↻</span>' +
        '<span class="turn-status-text"></span>';
      b.appendChild(turnStatusEl);
    }
    const txt = turnStatusEl.querySelector('.turn-status-text') as HTMLElement | null;
    if (txt) txt.textContent = text;
    App.scrollToBottom();
  };
  App.clearTurnStatus = function clearTurnStatus() {
    if (turnStatusEl && document.body.contains(turnStatusEl)) turnStatusEl.remove();
    turnStatusEl = null;
  };

  /** 页面刷新/断线重连：后台对话轮仍在执行 → 用服务端进度快照重建回合气泡，
   *  后续 stream_text / tool_call / audio 事件经广播代理自然接上，
   *  用户看到的是「无缝继续」，不是从头再来。 */
  App.takeoverTurnInProgress = function takeoverTurnInProgress(m: any) {
    const sid = m && m.session_id ? String(m.session_id) : null;
    // 同一回合气泡已在展示（快速断线重连，DOM 未重建）：只对齐锚点，不重复建泡
    const cur = App._turnMsgEl;
    if (cur && document.body.contains(cur) && sid && cur.dataset.session === sid) return;
    App.currentReplySession = sid;
    App._interruptedSession = null; // 接管进行中的会话，解除旧栅栏
    // 刷新重连后按快照恢复「工具执行中」状态：此时语音同样只排队不打断
    App._turnInTools = !!(m && Array.isArray(m.tool_events) && m.tool_events.length);
    App.currentReplyText = String(m && m.full_text || '');
    App.currentReplySeg = '';
    App.audioQueue = [];
    App._streamTextOn = !!(m && m.full_text);
    App.removeTyping();
    App.setState(m && m.full_text ? App.State.SPEAKING : App.State.THINKING);
    App.beginTurnBubble(sid);
    if (App.toolChainBeginTurn) App.toolChainBeginTurn();
    // 思考指示尾部（有则显示；正文流出时会自动清除）
    if (m && m.reasoning_tail && App.handleReasoning) {
      App.handleReasoning(String(m.reasoning_tail), sid);
    }
    // 已流出正文一次性重建（写进当前正文段，后续增量自然续上）
    if (m && m.full_text && App.setTurnStreamText) {
      App.setTurnStreamText(String(m.full_text));
    }
    // 工具链事件按序重放：最后一个未出结果的工具保持「执行中」
    const evts = Array.isArray(m && m.tool_events) ? m.tool_events : [];
    for (const e of evts) {
      if (!e || typeof e.type !== 'string') continue;
      if (e.type === 'tool_call_start' && App.toolChainStart) {
        App.toolChainStart(e.tool_name, e.arguments, e.tool_desc);
        App._toolRunningSince = Date.now();
      } else if (e.type === 'tool_call_result' && App.toolChainResult) {
        App.toolChainResult(e.tool_name, e.result, e.success);
        App._toolRunningSince = 0;
      } else if (e.type === 'tool_call_progress' && App.toolChainProgress) {
        App.toolChainProgress(e.tool_name, e.elapsed, e.message);
      }
    }
    if (App.noteTurnActivity) App.noteTurnActivity();
    App.scrollToBottom(true);
    App.notifyFullscreenChat();
  };

  /* ============================================================
   *  长时间无响应看门狗：工具/思考期间没有任何实时事件
   *  （推理增量 / 工具心跳 / 正文流式）时，在回复气泡底部提示可能卡住，
   *  并提供一键中断（中断后说『继续』可恢复完整现场）。
   * ============================================================ */
  const STUCK_AFTER_MS = 25_000;      // 连续这么久无任何事件 → 提示
  const WATCH_INTERVAL_MS = 5_000;    // 看门狗轮询间隔
  const TOOL_MAX_MS = 5 * 60_000;     // 单个工具跑/排队超过 5 分钟 → 提示
  let stuckEl: HTMLElement | null = null;
  let stuckWatchStarted = false;

  App.noteTurnActivity = function noteTurnActivity() {
    App._lastTurnActivity = Date.now();
    App.clearStuckHint();
  };

  App.clearStuckHint = function clearStuckHint() {
    if (stuckEl && document.body.contains(stuckEl)) stuckEl.remove();
    stuckEl = null;
  };

  App.maybeWarnStuck = function maybeWarnStuck() {
    const b = App._turnMsgEl;
    const active = !!(App.currentReplySession
      || (b && document.body.contains(b) && b.classList.contains('streaming'))
      || App.currentState === App.State.THINKING
      || App.currentState === App.State.SPEAKING);
    if (!active) {
      App.clearStuckHint();
      return;
    }
    const idle = Date.now() - (App._lastTurnActivity || 0);
    const toolRunning = Date.now() - (App._toolRunningSince || 0);
    const toolTooLong = App._toolRunningSince > 0 && toolRunning > TOOL_MAX_MS;
    if (idle < STUCK_AFTER_MS && !toolTooLong) {
      App.clearStuckHint();
      return;
    }
    const warnText = toolTooLong
      ? '工具已运行 ' + Math.floor(toolRunning / 1000) + ' 秒仍无结果，可能卡住'
      : '已 ' + Math.floor(idle / 1000) + ' 秒无响应，可能卡住';
    if (stuckEl && document.body.contains(stuckEl)) {
      const txt = stuckEl.querySelector('.turn-stuck-text');
      if (txt) txt.textContent = warnText;
      return;
    }
    if (!b || !document.body.contains(b)) return;
    stuckEl = document.createElement('div');
    stuckEl.className = 'turn-stuck';
    stuckEl.innerHTML =
      '<span class="turn-stuck-icon">⚠</span>' +
      '<span class="turn-stuck-text">' + warnText + '</span>' +
      '<button class="turn-stuck-btn" type="button" title="中断当前回复，之后说『继续』可恢复">中断后说『继续』恢复</button>';
    const btn = stuckEl.querySelector('.turn-stuck-btn');
    if (btn) btn.addEventListener('click', () => {
      // 看门狗按钮 = 用户显式要停：force 让服务端即使正在跑工具任务也照取消
      if (App.triggerInterrupt) App.triggerInterrupt(true);
    });
    b.appendChild(stuckEl);
    App.scrollToBottom();
  };

  if (!stuckWatchStarted) {
    stuckWatchStarted = true;
    window.setInterval(() => App.maybeWarnStuck(), WATCH_INTERVAL_MS);
  }

  /** 整轮收尾：把所有正文段渲染成完整 Markdown + 中断标记 */
  App.finishTurn = function finishTurn(interrupted?: boolean) {
    const b = App._turnMsgEl;
    if (!b || !document.body.contains(b)) return;
    if (interrupted) b.classList.add('interrupted');
    if (App.clearReasoningLine) App.clearReasoningLine();
    if (App.clearTurnStatus) App.clearTurnStatus();
    if (App.clearStuckHint) App.clearStuckHint();
    b.querySelectorAll('.turn-seg').forEach((s) => {
      const raw = (s as HTMLElement).dataset.raw || '';
      if (raw && App.renderMsgMedia) App.renderMsgMedia(s as HTMLElement, raw);
    });
    // 回合气泡收尾后补复制按钮（流式期间正文未定型，此时才补）
    if (App.ensureCopyBtn) App.ensureCopyBtn(b);
  };

  /** 回复文本落点：回合气泡 → .turn-text；普通气泡 → 气泡本身 */
  App.turnTextContainer = function turnTextContainer() {
    const el = App.pendingAIMsgEl && document.body.contains(App.pendingAIMsgEl)
      ? App.pendingAIMsgEl
      : App._turnMsgEl;
    if (!el) return null;
    if (el.classList && el.classList.contains('turn')) {
      return (App.ensureTurnSeg ? App.ensureTurnSeg() : null);
    }
    return el;
  };

  /** 流式实时 Markdown 的「已完成前缀」：只有结构完整的部分才做块级渲染。
   *  边界 = 最后一个空行（段落结束）；若该前缀里还有未闭合的 ``` 围栏，
   *  则回退到围栏起点 —— 半截代码块保持纯文本，避免流式期间乱闪。 */
  function streamMdPrefix(raw: string): string {
    let cut = 0;
    const nl = raw.lastIndexOf('\n\n');
    if (nl >= 0) cut = nl + 2;
    const head = raw.slice(0, cut);
    if ((head.match(/```/g) || []).length % 2 === 1) cut = head.lastIndexOf('```');
    return raw.slice(0, cut);
  }

  /** 流式正文写入：已完成段落立刻渲染成结构化 Markdown（实时感），
   *  正在增长的最后一段只做行内渲染（加粗/行内代码/链接成对即生效，
   *  未闭合语法原样保留）。整轮收尾时由 finishTurn 再全量重渲染一次。 */
  function renderStreamMd(el: HTMLElement, raw: string) {
    const done = streamMdPrefix(raw);
    const tail = raw.slice(done.length);
    const kids = el.children;
    let doneEl = (kids.length && kids[0].classList.contains('md-done'))
      ? kids[0] as HTMLElement : null;
    if (!doneEl) {
      el.dataset.mdDone = '';
      if (done) {
        doneEl = document.createElement('div');
        doneEl.className = 'md-done';
        el.insertBefore(doneEl, el.firstChild);
      }
    }
    if (doneEl && done && el.dataset.mdDone !== String(done.length)) {
      doneEl.innerHTML = mdToHtml(done);
      el.dataset.mdDone = String(done.length);
    }
    let tailEl = (el.lastElementChild && el.lastElementChild.classList.contains('md-tail'))
      ? el.lastElementChild as HTMLElement : null;
    if (!tailEl) {
      tailEl = document.createElement('span');
      tailEl.className = 'md-tail';
      el.appendChild(tailEl);
    }
    // 尾段写入：纯文本时只改文本节点。
    // 原来无条件 tailEl.innerHTML = inlineMd(tail)：每帧重建 DOM 会让整块文本
    // 重新排版（布局缓存全废）—— 实测 6 秒流式里 4.6s 的 Layout 全出在这一行，
    // 同量文本改用 textContent 后降到 0.3s，帧 p50 从 22.2ms 回到 7.4ms。
    // 只有真的含行内标记（加粗/斜体/行内代码/链接）时才重建，且内容未变就不碰 DOM。
    if (!/[*`\[]/.test(tail)) {
      if (tailEl.textContent !== tail) tailEl.textContent = tail;
      if (tailEl.dataset.mdHtml) delete tailEl.dataset.mdHtml;
    } else {
      const html = inlineMd(tail);
      if (tailEl.dataset.mdHtml !== html) {
        tailEl.innerHTML = html;
        tailEl.dataset.mdHtml = html;
      }
    }
  }

  /** 流式正文写入：增量文本追加 + 实时 Markdown（已完成段落结构化、尾段行内） */
  App.setTurnStreamText = function setTurnStreamText(text: string) {
    const el = App.turnTextContainer();
    if (!el) return;
    const raw = (el.dataset.raw || '') + text;
    el.dataset.raw = raw;
    if (App.mdToHtml) renderStreamMd(el, raw);
    else el.textContent = raw;
    // 标记「正在写入的那一段」：光标要挂在真正在增长的元素上。
    // 回合气泡里正文在 .turn-seg（块级，且其后还可能跟着工具块），
    // 若把光标挂在气泡 ::after 上会另起一行，必须精确到当前段。
    if (!el.classList.contains('sp-live')) {
      const prev = el.parentElement
        ? el.parentElement.querySelectorAll('.sp-live')
        : null;
      if (prev) prev.forEach((p) => p.classList.remove('sp-live'));
      el.classList.add('sp-live');
    }
    // 节奏引擎：把这段增量交给 37_stream_pulse 测量（速率 / 节拍 / 呼吸），
    // 它只产出 CSS 变量，不碰 DOM，失败也不影响正文写入
    if (App.stream) App.stream.feed(text);
  };

  /** 最终回复全文渲染：流式时正文已完整显示，audio_end 的全文只含结论，
   *  不覆盖正文，避免丢掉过程段落与内联工具块；仅正文为空时兜底写入 */
  App.renderTurnText = function renderTurnText(text: string) {
    const b = App._turnMsgEl;
    if (!b || !document.body.contains(b)) return;
    let hasText = false;
    b.querySelectorAll('.turn-seg').forEach((s) => {
      if ((s.textContent || '').trim()) hasText = true;
    });
    if (!hasText && text) {
      const el = App.turnTextContainer();
      if (el && App.renderMsgMedia) App.renderMsgMedia(el, text);
      else if (el) el.textContent = text;
    }
  };

  /* ============================================================
   *  消息一键复制
   * ============================================================ */
  const COPY_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  const CHECK_ICON =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>';

  function copyTextToClipboard(text: string): Promise<void> {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    // 非安全上下文降级：隐藏 textarea + execCommand
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    ta.remove();
    if (!ok) return Promise.reject(new Error('copy failed'));
    return Promise.resolve();
  }

  // 给单条消息补复制按钮（打字中/流式中的消息跳过——文本尚未定型）
  App.ensureCopyBtn = function ensureCopyBtn(el: Node) {
    const e = el as HTMLElement;
    if (!e || !e.classList || !e.classList.contains('msg')) return;
    if (e.classList.contains('typing') || e.classList.contains('streaming')) return;
    if (e.querySelector('.msg-copy-btn')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'msg-copy-btn';
    btn.title = '复制本条消息';
    btn.setAttribute('aria-label', '复制本条消息');
    btn.innerHTML = COPY_ICON;
    e.classList.add('has-copy');
    e.appendChild(btn);
  };

  // 提取消息可见文本（忽略复制按钮本身；任务树卡片去掉工具栏/箭头/图标，保留状态徽章）
  function msgText(el: HTMLElement) {
    // 回合气泡：只取正文段（.turn-seg），不把思考/工具调用噪音一起复制
    if (el.classList && el.classList.contains('turn')) {
      const segs = el.querySelectorAll('.turn-seg');
      const parts: string[] = [];
      segs.forEach((s) => {
        const t = (s.textContent || '').trim();
        if (t) parts.push(t);
      });
      return parts.join('\n');
    }
    const clone = el.cloneNode(true) as HTMLElement;
    clone.querySelectorAll('.msg-copy-btn, .tt-tools, .tt-caret, .tt-status-icon').forEach((b) => b.remove());
    return (clone.innerText || clone.textContent || '').trim();
  }

  // 复制按钮点击（事件委托：清空/裁剪消息也不会泄漏监听器）
  App.messagesEl!.addEventListener('click', (e) => {
    const target = e.target as HTMLElement;
    // 回合气泡折叠段：点头部展开/收起推理 / 工具模块
    const turnHead = target.closest && target.closest('.turn-head') as HTMLElement | null;
    if (turnHead) {
      const sec = turnHead.parentElement;
      if (sec) {
        // 用户手动操作过（展开或折叠）：该段后续内容不再自动开合，尊重用户状态
        sec.dataset.userManaged = '1';
        const body = sec.querySelector('.turn-body') as HTMLElement | null;
        if (body) {
          body.hidden = !body.hidden;
          turnHead.setAttribute('aria-expanded', String(!body.hidden));
          sec.classList.toggle('open', !body.hidden);
        }
      }
      App.scrollToBottom();
      return;
    }
    // 点击消息内图片 → 弹大图查看
    if (target.closest && target.closest('.msg-media-img')) {
      const link = target.closest('.msg-media-link');
      if (link && App.openMediaViewer) App.openMediaViewer(link.getAttribute('href'));
      return;
    }
    const btn = target.closest('.msg-copy-btn') as HTMLElement | null;
    if (!btn) return;
    const msgEl = btn.closest('.msg') as HTMLElement | null;
    if (!msgEl) return;
    // 卡片类消息可挂 data-copy-text：只复制关键结果，而不是整卡文本
    const text = (btn.dataset.copyText && String(btn.dataset.copyText).trim()) || msgText(msgEl);
    if (!text) {
      if (App.showToast) App.showToast('没有可复制的内容');
      return;
    }
    copyTextToClipboard(text).then(() => {
      if (App.showToast) App.showToast('已复制');
      btn.classList.add('copied');
      btn.innerHTML = CHECK_ICON;
      btn.title = '已复制';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = COPY_ICON;
        btn.title = '复制本条消息';
      }, 1200);
    }).catch(() => {
      if (App.showToast) App.showToast('复制失败，请手动选择文本');
    });
  });

  // 观察消息容器：新气泡出现即补按钮；流式消息写完（移除 streaming 类）后补按钮
  const copyObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.type === 'childList') {
        m.addedNodes.forEach((n) => {
          if (n.nodeType === 1) App.ensureCopyBtn(n);
        });
      } else if (m.type === 'attributes') {
        App.ensureCopyBtn(m.target);
      }
    }
  });
  copyObserver.observe(App.messagesEl!, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['class']
  });

  /* ============================================================
   *  聊天框头部：消息计数 / 高度档位 / 收起
   * ============================================================ */
  App.updateChatHeadCount = function updateChatHeadCount() {
    const el = document.getElementById('chat-head-count');
    if (!el) return;
    const n = App.messagesEl!.querySelectorAll('.msg:not(.typing)').length;
    const s = String(n);
    if (el.textContent === s) return;   // 计数未变就不碰 DOM，避免无意义重排
    el.textContent = s;
    el.title = n + ' 条消息';
  };
  // 复用消息观察器：任何消息增删后同步计数。
  // 流式期间 tail 节点高频替换会疯狂触发回调，这里合并到 rAF（一帧最多重算一次），
  // 避免每个 token 都做一次全量 querySelectorAll。
  let headCountDirty = false;
  const countObserver = new MutationObserver(() => {
    if (headCountDirty) return;
    headCountDirty = true;
    requestAnimationFrame(() => {
      headCountDirty = false;
      App.updateChatHeadCount();
    });
  });
  countObserver.observe(App.messagesEl!, { childList: true, subtree: true });
  App.updateChatHeadCount();

  /* ============================================================
   *  上下文 / Token 用量统计（localStorage 持久化，跨刷新累计）
   * ============================================================ */
  const TOKEN_STATS_KEY = 'dabai.token_stats_v1';
  App.fmtTokens = function fmtTokens(n: number) {
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
    return String(n);
  };
  App._tokenStats = { context: 0, completion: 0, total: 0, rounds: 0, msgs: 0, cache_hit: 0, cache_miss: 0 };
  try {
    const saved = JSON.parse(localStorage.getItem(TOKEN_STATS_KEY) || 'null');
    if (saved && typeof saved === 'object') Object.assign(App._tokenStats, saved);
  } catch (e) {}
  App._lastUsage = null;
  App.handleUsageMessage = function handleUsageMessage(msg: UsageMessage) {
    App._lastUsage = msg;
    // 输入/输出分开累计：context 是「实际接受的上下文输入」逐轮累加，不再覆盖
    App._tokenStats.context += msg.prompt_tokens || 0;
    App._tokenStats.completion += msg.completion_tokens || 0;
    App._tokenStats.total += msg.total_tokens || 0;
    App._tokenStats.rounds += msg.rounds || 0;
    App._tokenStats.msgs += 1;
    App._tokenStats.cache_hit += msg.cache_hit_tokens || 0;
    App._tokenStats.cache_miss += msg.cache_miss_tokens || 0;
    try {
      localStorage.setItem(TOKEN_STATS_KEY, JSON.stringify(App._tokenStats));
    } catch (e) {}
    App.updateTokenMeter();
  };
  App.attachMsgTokenBadge = function attachMsgTokenBadge(el: HTMLElement) {
    if (!el || !el.classList || !el.classList.contains('msg') || !el.classList.contains('ai')) return;
    if (!App._lastUsage) return;
    if (el.querySelector('.msg-token-badge')) return;
    const u = App._lastUsage;
    const badge = document.createElement('span');
    badge.className = 'msg-token-badge';
    badge.textContent = '⸙ ' + App.fmtTokens(u.total_tokens || 0);
    badge.title = '本轮 输入 ' + App.fmtTokens(u.prompt_tokens || 0) + ' + 输出 ' +
      App.fmtTokens(u.completion_tokens || 0) + '，共 ' + App.fmtTokens(u.total_tokens || 0) + ' tokens';
    el.appendChild(badge);
    App._lastUsage = null; // 防止误挂到下一条回复
  };
  App.updateTokenMeter = function updateTokenMeter() {
    const meter = document.getElementById('chat-token-meter');
    if (!meter) return;
    const u = App._lastUsage;
    const st = App._tokenStats;
    const hit = st.cache_hit || 0;
    const miss = st.cache_miss || 0;
    const cachePct = (hit + miss) > 0 ? Math.round((hit / (hit + miss)) * 1000) / 10 : 0;
    const cacheTxt = cachePct > 0
      ? '\n缓存命中 ' + cachePct + '%（' + App.fmtTokens(hit) + ' / ' + App.fmtTokens(hit + miss) + '）'
      : '';
    if (u) {
      const win = u.context_window || 0;
      // 「当前上下文占用」必须用 context_tokens（本轮最后一次调用的 prompt），
      // 不能用 prompt_tokens（本轮所有调用的输入之和）——后者会算出 300%+。
      const ctxTokens = u.context_tokens || 0;
      const pct = win > 0 ? Math.round((ctxTokens / win) * 1000) / 10 : 0;
      const cacheBadge = cachePct > 0 ? ' · ⛁' + cachePct + '%' : '';
      meter.textContent = '输入 ' + App.fmtTokens(u.prompt_tokens || 0) +
        ' · 输出 ' + App.fmtTokens(u.completion_tokens || 0) + cacheBadge;
      meter.title = '本轮 输入 ' + App.fmtTokens(u.prompt_tokens || 0) +
        ' / 输出 ' + App.fmtTokens(u.completion_tokens || 0) +
        ' · 上下文占用 ' + pct + '%' +
        (win > 0 ? '（当前 ' + App.fmtTokens(ctxTokens) + ' / 窗口 ' + App.fmtTokens(win) + '）' : '') +
        '\n累计 输入 ' + App.fmtTokens(st.context) +
        ' / 输出 ' + App.fmtTokens(st.completion) +
        ' · 共 ' + App.fmtTokens(st.total) + ' tokens · ' + st.msgs + ' 条回复' +
        cacheTxt;
      return;
    }
    if (st.total > 0) {
      const cacheBadge = cachePct > 0 ? ' · ⛁' + cachePct + '%' : '';
      meter.textContent = '累计 输入 ' + App.fmtTokens(st.context) +
        ' / 输出 ' + App.fmtTokens(st.completion) + cacheBadge;
      meter.title = '累计 输入 ' + App.fmtTokens(st.context) +
        ' / 输出 ' + App.fmtTokens(st.completion) +
        ' · 共 ' + App.fmtTokens(st.total) + ' tokens · ' + st.msgs + ' 条回复' +
        cacheTxt;
      return;
    }
    meter.textContent = '—';
  };
  const tokenMeterEl = document.getElementById('chat-token-meter');
  if (tokenMeterEl) tokenMeterEl.addEventListener('click', () => {
    const u = App._lastUsage;
    const st = App._tokenStats;
    const win = u && u.context_window ? u.context_window : 0;
    const ctx = u ? App.fmtTokens(u.prompt_tokens || 0) : App.fmtTokens(st.context);
    const hit = st.cache_hit || 0;
    const miss = st.cache_miss || 0;
    const cachePct = (hit + miss) > 0 ? Math.round((hit / (hit + miss)) * 1000) / 10 : 0;
    App.showToast('累计 输入 ' + App.fmtTokens(st.context) +
      ' / 输出 ' + App.fmtTokens(st.completion) +
      ' · 共 ' + App.fmtTokens(st.total) + ' tokens' +
      ' · 本轮输入 ' + ctx + (win > 0 ? '/' + App.fmtTokens(win) : '') +
      (cachePct > 0 ? ' · 缓存命中 ' + cachePct + '%' : ''));
  });
  App.updateTokenMeter(); // 让持久化数据上屏

  /* 聊天框全屏与全屏透明：两个按钮、两条路，共用同一套「铺满屏幕」的外壳。
   *   ⤢ 不透明全屏：屏幕上只剩对话 —— 顺手把 3D 渲染整个停掉（省电，也真的安静）
   *   ◍ 全屏透明：面板半透明、渲染不停 —— 角色在字后面干活，安静但不隔绝
   * 两者互斥（都是「占满屏幕」，同时开只会打架）。 */
  App.chatFullscreen = false;
  App.chatGhost = false;

  /** 铺满 / 退出铺满：两种全屏共用的外壳（差别只在透明与是否静默） */
  const applyFsShell = (on: boolean) => {
    const panel = document.getElementById('chat-panel');
    const appEl = document.getElementById('app');
    if (!panel) return null;
    App.chatHeightLevel = on ? 1 : 0;
    // 清掉旧的高度档位类，统一走全屏
    panel.classList.remove('chat-h-md', 'chat-h-lg');
    panel.classList.toggle('chat-fs', on);
    // 进全屏必须先摘掉 collapsed：collapsed 自带 translateY(120%) + max-height:38dvh + opacity:0，
    // 而且 CSS 里排在 .chat-fs 之后会盖掉全屏几何 —— 实测点 ◍ 后面板仍下移 364px、高只有 304px，
    // 屏幕上就是「上半有字、下半空」。输入栏那份 collapsed 一起摘，否则它也不回位。
    if (on) {
      panel.classList.remove('collapsed');
      const controls = document.getElementById('controls');
      if (controls) controls.classList.remove('collapsed');
    }
    if (appEl) appEl.classList.toggle('chat-fullscreen', on);
    const toggle = document.getElementById('chat-toggle');
    if (toggle) toggle.classList.toggle('shifted', on);
    return panel;
  };

  /** 两个全屏按钮的激活态 / 图标 / 提示，一处同步，避免各自漏改 */
  const syncFsBtns = () => {
    const fsBtn = document.getElementById('chat-height-btn');
    if (fsBtn) {
      const on = App.chatFullscreen && !App.chatGhost;
      fsBtn.classList.toggle('active', on);
      fsBtn.textContent = on ? '⤡' : '⤢';
      fsBtn.title = on ? '退出全屏聊天（点击还原）' : '全屏聊天（点击展开）';
    }
    const ghBtn = document.getElementById('chat-ghost-btn');
    if (ghBtn) {
      ghBtn.classList.toggle('active', App.chatGhost);
      ghBtn.textContent = App.chatGhost ? '◉' : '◍';
      ghBtn.title = App.chatGhost
        ? '退出全屏透明（点击还原）'
        : '全屏透明：铺满屏幕，让角色透出来';
    }
  };

  App.setChatFullscreen = function setChatFullscreen(on: boolean) {
    const panel = applyFsShell(!!on);
    if (!panel) return;
    App.chatFullscreen = !!on;
    App.chatGhost = false;
    panel.classList.remove('chat-ghost');
    // 全屏 = 屏幕上只剩对话：非聊天框的一切渲染全部停掉（00_quiet 总闸）
    if (App.setQuiet) App.setQuiet(App.chatFullscreen);
    syncFsBtns();
    setTimeout(() => App.scrollToBottom(true), 320);
  };

  App.setChatGhost = function setChatGhost(on: boolean) {
    const panel = applyFsShell(!!on);
    if (!panel) return;
    App.chatGhost = !!on;
    App.chatFullscreen = !!on;
    panel.classList.toggle('chat-ghost', App.chatGhost);
    // 与不透明全屏的唯一分岔：渲染不停 —— 背景里得有东西，透明才有意义
    if (App.setQuiet) App.setQuiet(false);
    syncFsBtns();
    setTimeout(() => App.scrollToBottom(true), 320);
  };

  App.cycleChatHeight = function cycleChatHeight() {
    // 全屏透明下点 ⤢ = 切到不透明全屏（两个按钮像一组单选，不必先退出再进）
    App.setChatFullscreen(!App.chatFullscreen || App.chatGhost);
  };
  // 收起聊天（与右下角切换按钮行为一致）
  App.closeChatPanel = function closeChatPanel() {
    const panel = document.getElementById('chat-panel');
    const controls = document.getElementById('controls');
    if (!panel || panel.classList.contains('collapsed')) return;
    // 收起时把两种全屏态一起清掉：否则 chat-fs 的 fixed inset:0 会留在面板上
    if (App.chatFullscreen || App.chatGhost) App.setChatFullscreen(false);
    panel.classList.add('collapsed');
    controls!.classList.add('collapsed');
    if (App.chatToggle) App.chatToggle.classList.remove('shifted', 'has-new');
    setTimeout(App.onResize, 350);
  };
  const heightBtn = document.getElementById('chat-height-btn');
  if (heightBtn) heightBtn.addEventListener('click', App.cycleChatHeight);
  const ghostBtn = document.getElementById('chat-ghost-btn');
  if (ghostBtn) ghostBtn.addEventListener('click', () => App.setChatGhost(!App.chatGhost));
  const closeBtn = document.getElementById('chat-close-btn');
  if (closeBtn) closeBtn.addEventListener('click', App.closeChatPanel);
  const head = document.getElementById('chat-head');
  if (head) head.addEventListener('touchstart', () => {}, { passive: true }); // 避免头部滚动穿透

  /* ============================================================
   *  Toast
   * ============================================================ */
});
