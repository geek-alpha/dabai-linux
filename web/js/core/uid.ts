/**
 * 统一身份解析：URL 上的 ?uid=xxx 优先，其次 localStorage，最后新生成。
 *
 * 为什么需要它：局域网直连没有登录 cookie，后端只认前端 set_user 传来的
 * user_id，而 localStorage 按 origin 隔离——127.0.0.1 与 192.168.x.x 是
 * 两个存储桶，每台设备各自生成一个 u_* 身份桶，表现为「电脑和手机各聊各的，
 * 看不到同一条会话」。URL 带 ?uid= 可显式指定身份，并写回 localStorage
 * 保持粘性（后续不带参数访问也还是同一身份）。
 *
 * 公网路径不受影响：后端身份优先级是 cookie > 前端 user_id（见 server.py
 * 的 set_user 分支），公网 ws 已强制登录，此参数不构成越权入口。
 */
export function resolveUserId(): string {
  let override = '';
  try {
    override = (new URLSearchParams(location.search).get('uid') || '').trim();
  } catch (e) {
    override = '';
  }
  const stored = localStorage.getItem('dabai.userId') || '';
  const uid = override || stored || 'u_' + Date.now().toString(36);
  if (uid !== stored) localStorage.setItem('dabai.userId', uid);
  return uid;
}
