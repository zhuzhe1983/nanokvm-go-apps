'use strict';
const $ = id => document.getElementById(id);
let state = null, busy = false, configured = false, loading = false;
const showMessage = text => { $('message').textContent = text; $('message').hidden = !text; };
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json', 'X-Pi-Hole-Go': '1'},
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(65000)});
  const result = await response.json();
  if (response.status === 401) {
    $('login-panel').hidden = false; $('dashboard').hidden = true; state = null; configured = false;
  }
  if (!response.ok) throw new Error(result.error || '请求未完成');
  return result;
}
function render(s) {
  state = s;
  $('login-panel').hidden = true; $('dashboard').hidden = false;
  const paused = s.paused_seconds > 0;
  $('state').textContent = s.enabled ? paused ? `已暂停 · ${Math.ceil(s.paused_seconds / 60)} 分钟` : s.effective ? '正在拦截' : '等待就绪' : '拦截已关闭';
  $('state').classList.toggle('active', s.effective && !paused);
  $('route').textContent = s.route === 'clash' ? 'USB 设备 → Pi-hole 过滤 → Clash → Wi-Fi' : 'USB 设备 → Pi-hole 过滤 → Wi-Fi';
  $('sharing-note').textContent = s.error || (!s.sharing ? 'USB 共享尚未开启。请在 Clash Go 中打开 USB 共享。' : '过滤仅作用于 USB 连接设备的常规 DNS 请求。');
  $('toggle').textContent = s.enabled ? '关闭拦截' : '开启拦截';
  $('pause').textContent = paused ? '恢复拦截' : '暂停 5 分钟';
  $('pause').disabled = busy || !s.effective;
  $('toggle').disabled = busy;
  $('blocked').textContent = Number(s.stats.blocked || 0).toLocaleString();
  $('percent').textContent = Number(s.stats.percent || 0).toFixed(1) + '%';
  $('queries').textContent = `共 ${Number(s.stats.queries || 0).toLocaleString()} 次请求`;
  $('domains').textContent = Number(s.domain_count || 0).toLocaleString();
  $('updated').textContent = s.updated_at ? '规则载入时间：' + new Date(s.updated_at * 1000).toLocaleString() : '内置规则可离线使用';
  $('update').textContent = s.updating ? '正在更新…' : '立即更新';
  $('update').disabled = busy || s.updating;
  $('update-error').textContent = s.update_error || ''; $('update-error').hidden = !s.update_error;
  if (!configured) {
    $('auto-update').checked = s.auto_update; $('upstreams').value = s.upstreams.join(', ');
    $('allowlist').value = s.allowlist.join('\n'); $('denylist').value = s.denylist.join('\n'); configured = true;
  }
}
async function refresh() {
  if (loading) return;
  loading = true;
  try { render(await api('/api/status')); }
  catch (e) { if (state) showMessage('设备连接中断，请检查网络后重试。'); }
  finally { loading = false; }
}
async function act(work) {
  if (busy) return;
  busy = true; showMessage('');
  document.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try { await work(); await refresh(); }
  catch (e) { showMessage(e.message); }
  finally { busy = false; document.querySelectorAll('button').forEach(b => { b.disabled = false; }); if (state) render(state); }
}
$('show-pin').onclick = () => {
  const show = $('pin').type === 'password'; $('pin').type = show ? 'text' : 'password';
  $('show-pin').textContent = show ? '隐藏' : '显示'; $('show-pin').setAttribute('aria-pressed', String(show));
};
$('login-form').onsubmit = event => { event.preventDefault(); act(async () => {
  await api('/api/login', {pin: $('pin').value}); $('pin').value = ''; configured = false;
}); };
$('toggle').onclick = () => act(() => api('/api/change', {action: 'enabled', value: !state.enabled}));
$('pause').onclick = () => act(() => api('/api/change', {action: 'pause', value: state.paused_seconds > 0 ? 0 : 300}));
$('update').onclick = () => act(() => api('/api/update', {}));
$('settings-form').onsubmit = event => { event.preventDefault(); act(async () => {
  $('save-state').textContent = '正在保存…';
  try {
    await api('/api/settings', {auto_update: $('auto-update').checked,
      upstreams: $('upstreams').value.split(/[,，\s]+/).filter(Boolean), allowlist: $('allowlist').value, denylist: $('denylist').value});
    configured = false; $('save-state').textContent = '已保存';
  } catch (e) { $('save-state').textContent = '未保存'; throw e; }
}); };
$('settings-form').oninput = () => { $('save-state').textContent = '有未保存的修改'; };
$('logout').onclick = () => act(async () => { await api('/api/logout', {}); state = null; configured = false; });
refresh();
setInterval(() => { if (state && !busy) refresh(); }, 4000);
