import type { AppKernel } from '../types/app-kernel.js';

/* ============================================================
 *  聊天附件（图片 / 任意文件）
 *
 *  上传与发送解耦：选完文件立刻后台上传，拿到服务器路径后挂在输入框上方的
 *  待发条里；点发送时随文字一起走 WS 的 attachments 字段。这样大图上传不会
 *  卡住输入框，用户可以先打字、上传完再发。
 *
 *  三条入口：回形针按钮 / 粘贴（截图直达）/ 拖拽进聊天区。
 *  图片由服务端合成 [[IMG:]] 标记，模型直接看像素；其余类型只给路径，
 *  由工具按需读——不做格式解析，任何类型都能发。
 * ============================================================ */
export default (function init(App: AppKernel) {
  const MAX_FILES = 8;
  // 单批字节上限：8 个 30MB 文件凑出 240MB，一个请求会把网关和手机网络一起拖死
  const BATCH_BYTES = 24 * 1024 * 1024;

  App.attachPending = [];

  /** 按对象身份移除待发附件：并发上传时下标会变，不能用闭包里的 i；顺手释放本地预览 URL */
  function removePending(f: any) {
    const list: any[] = App.attachPending || [];
    const at = list.indexOf(f);
    if (at < 0) return;
    list.splice(at, 1);
    if (f.preview) { try { URL.revokeObjectURL(f.preview); } catch (e) { /* 已释放 */ } }
    App.renderAttachStrip();
  }

  /** 渲染待发条（上传中 / 已完成共用一份数据） */
  App.renderAttachStrip = function renderAttachStrip() {
    const strip = document.getElementById('attach-strip');
    if (!strip) return;
    const list: any[] = App.attachPending || [];
    if (!list.length) {
      strip.classList.remove('show');
      strip.innerHTML = '';
      return;
    }
    strip.classList.add('show');
    strip.innerHTML = '';
    list.forEach((f: any) => {
      const chip = document.createElement('div');
      chip.className = 'attach-chip' + (f.uploading ? ' uploading' : '');
      chip.title = (f.name || '文件') + (f.uploading ? ' · 上传中…' : '') + '\n点右上角 × 取消';
      // 上传中先拿本地 objectURL 当缩略图：选完文件立刻看得见，不用等网络回来
      const thumb = f.url || f.preview;
      if (thumb && (f.kind === 'image' || f.preview)) {
        const img = document.createElement('img');
        img.src = thumb;
        img.alt = f.name || '';
        chip.appendChild(img);
      } else {
        const ext = document.createElement('span');
        ext.className = 'attach-chip-ext';
        ext.textContent = String(f.ext || 'FILE').slice(0, 4).toUpperCase();
        chip.appendChild(ext);
      }
      if (f.uploading) {
        const tag = document.createElement('span');
        tag.className = 'attach-chip-tag';
        tag.textContent = '上传中';
        chip.appendChild(tag);
      }
      const del = document.createElement('button');
      del.className = 'attach-chip-del';
      del.type = 'button';
      del.textContent = '×';
      del.title = '取消这个附件';
      del.setAttribute('aria-label', '取消附件 ' + (f.name || ''));
      del.addEventListener('click', (ev: Event) => {
        ev.stopPropagation();
        removePending(f);
      });
      chip.appendChild(del);
      strip.appendChild(chip);
    });
  };

  /** 已上传完成的附件（发送时取；上传中的留在条上继续等） */
  App.takeAttachments = function takeAttachments() {
    return (App.attachPending || []).filter((f: any) => !f.uploading && f.path);
  };

  App.clearAttachments = function clearAttachments() {
    App.attachPending = [];
    App.renderAttachStrip();
  };

  /** 发一批并回填：占位符按原位置换成服务器返回的元数据 */
  async function uploadBatch(batch: File[], holders: any[]) {
    const fd = new FormData();
    batch.forEach((f) => fd.append('files', f, f.name));
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const data: any = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    // 按占位符原位置替换：期间用户又加了别的附件也不会错位。
    // 用户可能在上传途中点了 ×：已移除的占位符不再回填，否则「取消了又自己冒出来」
    const prev = App.attachPending || [];
    const alive = holders.filter((p: any) => prev.indexOf(p) >= 0);
    holders.forEach((p: any) => URL.revokeObjectURL(p.preview));
    if (!alive.length) { App.renderAttachStrip(); return { fresh: 0, dup: 0 }; }
    const at = prev.indexOf(alive[0]);
    const head = prev.slice(0, at);
    const tail = prev.slice(at + alive.length);
    // 同一张图被重复选中（点了两次回形针、粘贴与拖拽同时触发）只留一条
    const seen = new Set(head.concat(tail).map((x: any) => x.path).filter(Boolean));
    const fresh: any[] = [];
    let dup = 0;
    for (const f of (data.files || [])) {
      if (f && f.path && seen.has(f.path)) { dup += 1; continue; }
      if (f && f.path) seen.add(f.path);
      fresh.push(f);
    }
    App.attachPending = head.concat(fresh, tail);
    App.renderAttachStrip();
    return { fresh: fresh.length, dup };
  }

  /** 上传一批文件：先占位（立刻有反馈），成功后替换成服务器返回的元数据 */
  App.uploadFiles = async function uploadFiles(files: File[]) {
    const picked = (files || []).filter(Boolean);
    if (!picked.length) return;
    const room = MAX_FILES - (App.attachPending || []).length;
    if (room <= 0) {
      App.showToast(`最多同时挂 ${MAX_FILES} 个附件`);
      return;
    }
    const batch = picked.slice(0, room);
    if (picked.length > room) App.showToast(`一次最多 ${MAX_FILES} 个，已取前 ${room} 个`);
    const placeholders = batch.map((f) => ({
      name: f.name,
      size: f.size,
      uploading: true,
      kind: (f.type || '').startsWith('image/') ? 'image' : 'file',
      preview: URL.createObjectURL(f),
      ext: (f.name.split('.').pop() || '').toLowerCase(),
    }));
    App.attachPending.push(...placeholders);
    App.renderAttachStrip();

    // 按总字节切批：8 个 30MB 文件凑出 240MB，一个请求会把网关和手机网络一起拖死。
    // 串行发、每批回来立刻回填，用户能边传边看。
    const batches: File[][] = [];
    const holders: any[][] = [];
    let cur: File[] = [], curHold: any[] = [], bytes = 0;
    batch.forEach((f, i) => {
      if (cur.length && bytes + f.size > BATCH_BYTES) {
        batches.push(cur); holders.push(curHold);
        cur = []; curHold = []; bytes = 0;
      }
      cur.push(f); curHold.push(placeholders[i]); bytes += f.size;
    });
    if (cur.length) { batches.push(cur); holders.push(curHold); }

    let ok = 0, dup = 0, failed = '';
    for (let i = 0; i < batches.length; i++) {
      try {
        const r = await uploadBatch(batches[i], holders[i]);
        ok += r.fresh; dup += r.dup;
      } catch (e) {
        failed = (e as Error).message || String(e);
        holders[i].forEach((p: any) => {
          URL.revokeObjectURL(p.preview);
          const k = (App.attachPending || []).indexOf(p);
          if (k >= 0) App.attachPending.splice(k, 1);
        });
        App.renderAttachStrip();
      }
    }
    // 上传成功必须有回声，用户不用猜「到底传没传上」
    if (!ok && !dup) { App.showToast('上传失败：' + (failed || '未知错误')); return; }
    const parts = [`已添加 ${ok} 个文件`];
    if (dup) parts.push(`跳过 ${dup} 个重复`);
    if (failed) parts.push(`有失败：${failed}`);
    App.showToast(parts.join(' · ') + ' · 右上角 × 可取消');
  };

  App.initAttach = function initAttach() {
    if ((App as any)._attachReady) return;  // 幂等：重复调用不重复绑监听
    (App as any)._attachReady = true;
    // 回形针：走隐藏 input，不占布局
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.multiple = true;
    // 手机上的关键：不给 accept，iOS/Android 只弹相册，选不到 PDF/Word。
    // image/* 必须留着——去掉它就没有拍照和相册入口了
    picker.accept = 'image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.md,.csv,.tsv,'
      + '.json,.log,.xml,.yaml,.yml,.zip';
    picker.style.display = 'none';
    document.body.appendChild(picker);
    const btn = document.getElementById('attach-btn');
    if (btn) btn.addEventListener('click', () => picker.click());
    picker.addEventListener('change', () => {
      if (picker.files) App.uploadFiles(Array.from(picker.files));
      picker.value = '';
    });

    // 粘贴：剪贴板里真有文件才接管——纯文本粘贴完全不受影响
    document.addEventListener('paste', (e: ClipboardEvent) => {
      const files = e.clipboardData && e.clipboardData.files;
      if (!files || !files.length) return;
      e.preventDefault();
      App.uploadFiles(Array.from(files));
    });

    // 拖拽：拖进聊天面板即收；拖文本/拖链接不拦
    const zone = document.getElementById('chat-panel') || document.body;
    const hasFiles = (e: DragEvent) => !!(e.dataTransfer && e.dataTransfer.types
      && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') >= 0);
    zone.addEventListener('dragover', (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      document.body.classList.add('attach-drag');
    });
    zone.addEventListener('dragleave', () => document.body.classList.remove('attach-drag'));
    zone.addEventListener('drop', (e: DragEvent) => {
      document.body.classList.remove('attach-drag');
      const fs = e.dataTransfer && e.dataTransfer.files;
      if (!fs || !fs.length) return;
      e.preventDefault();
      App.uploadFiles(Array.from(fs));
    });

    App.renderAttachStrip();
  };

  // 模块自己起：app.ts 只 import 本文件，不会替我们调 init。
  // 脚本是 type="module"（defer 语义），执行时 #attach-btn 已在 DOM 里。
  App.initAttach();
});
