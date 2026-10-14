"use strict";
const $ = id => document.getElementById(id);
let state = null, busy = false, initialized = false;
async function api(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", credentials: "same-origin",
    headers: body ? {"Content-Type":"application/json", "X-Clash-Go":"1"} : {}, body: body ? JSON.stringify(body) : undefined});
  const data = await response.json();
  if (!response.ok) {if (response.status === 401) showLogin(); throw new Error(data.error || "请求失败");}
  return data;
}
function hideSecrets() {
  document.querySelectorAll('[data-reveal]').forEach(button=>{
    $(button.dataset.reveal).type='password';button.textContent=button.dataset.show;
    button.setAttribute('aria-pressed','false');button.setAttribute('aria-label','显示'+button.dataset.label);
  });
}
function showLogin() {if(state){['pin','subscription','wifi-password','oix-token'].forEach(id=>$(id).value='');}hideSecrets();$('login').hidden=false;$('dashboard').hidden=true;$('connection').textContent='等待验证';state=null;initialized=false;}
function options(element, values, selected) {
  element.replaceChildren(...values.map(value => {const option=document.createElement('option');option.value=value;option.textContent=value;return option;}));
  element.value=selected;
}
function render(s) {
  state=s;$('login').hidden=true;$('dashboard').hidden=false;
  $('connection').textContent=s.enabled ? (s.running ? '代理运行中' : '代理异常') : (s.sharing ? '直连共享' : '已连接');
  $('proxy-title').textContent=s.enabled ? 'USB 代理已开启' : '代理已关闭';
  $('proxy-description').textContent=s.error || (s.subscription_saved ? (s.selected || '请选择节点') : '导入订阅后，为 USB 接入的手机或电脑启用代理。');
  $('proxy-toggle').textContent=s.enabled ? '关闭代理' : '开启代理';
  $('sharing-toggle').textContent=s.sharing ? '关闭共享' : '开启共享';
  $('wifi-name').textContent=s.wifi.ssid || '未连接';$('wifi-ip').textContent=s.interfaces.wlan0 || '—';$('usb-ip').textContent=s.interfaces.usb0 || '—';
  if (document.activeElement !== $('node')) options($('node'), s.nodes.length ? s.nodes : ['请先导入订阅'], s.selected || '请先导入订阅');
  $('select-node').disabled=!s.nodes.length || busy;$('node-count').textContent=s.nodes.length+' 个订阅节点';
  $('subscription-state').textContent=s.manual_subscription_saved ? (s.source==='subscription' ? '正在使用 · ' : '已保存 · ')+'密钥隐藏' : '订阅尚未配置';
  $('subscription-refresh').disabled=!s.manual_subscription_saved || busy;
  const o=s.oix || {}, a=o.account || {};
  $('oix-state').textContent=o.connected ? (o.active ? '正在使用托管节点' : '已登录 · 未启用') : '未登录';
  $('oix-account').hidden=!o.connected;
  $('oix-node-error').hidden=!o.node_error;$('oix-node-error').textContent=o.node_error || '';
  for(const [id,key] of Object.entries({'oix-plan':'plan','oix-expiry':'plan_time','oix-used':'used','oix-unused':'unused','oix-traffic':'traffic','oix-today':'today_used'}))$(id).textContent=a[key] || '—';
  $('oix-updated').textContent=o.updated_at ? '套餐更新于 '+new Date(o.updated_at*1000).toLocaleString() : '';
  $('node-count').textContent=s.nodes.length+' 个'+(s.source==='oix' ? '托管' : '订阅')+'节点';
  $('core-version').textContent=s.core_version;
  if (!initialized) {$('mode').value=s.mode;$('dns').value=s.dns.join(', ');$('domains').value=s.direct_domains.join('\n');initialized=true;}
  $('wifi-confirm').hidden=!s.wifi_pending;$('wifi-countdown').textContent='请确认新网络可用，'+s.wifi_seconds+' 秒后自动恢复原网络。';
}
async function refresh() {if(busy)return;try{render(await api('/api/status'));}catch(error){if(state)$('notice').textContent=error.message;}}
async function perform(work, message='已保存') {
  if(busy)return;busy=true;$('notice').textContent='正在应用，请稍候…';
  document.querySelectorAll('button').forEach(button=>button.disabled=true);
  try{await work();$('notice').textContent=message;}catch(error){$('notice').textContent=error.message;}
  finally{busy=false;document.querySelectorAll('button').forEach(button=>button.disabled=false);await refresh();}
}
const change=(action,value)=>api('/api/change',{action,value});
document.querySelectorAll('[data-reveal]').forEach(button=>button.addEventListener('click',()=>{
  const input=$(button.dataset.reveal), visible=input.type==='password';
  input.type=visible?'text':'password';button.textContent=visible?button.dataset.hide:button.dataset.show;
  button.setAttribute('aria-pressed',String(visible));button.setAttribute('aria-label',(visible?'隐藏':'显示')+button.dataset.label);
}));
$('login-form').addEventListener('submit',event=>{event.preventDefault();perform(async()=>{await api('/api/login',{pin:$('pin').value});$('pin').value='';hideSecrets();},'设备已连接');});
$('proxy-toggle').onclick=()=>perform(()=>change('enabled',!state.enabled),'代理状态已更新');
$('sharing-toggle').onclick=()=>perform(()=>change('sharing',!state.sharing),'USB 网络共享状态已更新');
$('select-node').onclick=()=>perform(()=>change('node',$('node').value),'节点已切换');
$('subscription-form').onsubmit=event=>{event.preventDefault();perform(async()=>{await change('subscription',$('subscription').value);$('subscription').value='';hideSecrets();},'订阅已导入，请选择节点并开启代理');};
$('subscription-refresh').onclick=()=>perform(()=>change('subscription_refresh',null),'订阅已更新并启用');
$('oix-form').onsubmit=event=>{event.preventDefault();perform(async()=>{await change('oix_login',$('oix-token').value);$('oix-token').value='';hideSecrets();},'oixCloud 已登录，托管节点已加载');};
$('oix-info').onclick=()=>perform(()=>change('oix_info',null),'套餐信息已更新');
$('oix-refresh').onclick=()=>perform(()=>change('oix_refresh',null),'托管节点已更新并启用');
$('oix-forget').onclick=()=>perform(async()=>{await change('oix_logout',null);$('oix-token').value='';hideSecrets();},'本机 oixCloud 登录已清除');
$('network-form').onsubmit=event=>{event.preventDefault();perform(()=>change('network',{mode:$('mode').value,dns:$('dns').value.split(/[,，\s]+/).filter(Boolean),direct_domains:$('domains').value.split(/[\s,，]+/).filter(Boolean)}));};
$('scan').onclick=()=>perform(async()=>{const result=await api('/api/wifi');options($('networks'),result.networks.map(item=>item.ssid),'');},'扫描完成，可选择或手动填写网络名称');
$('wifi-form').onsubmit=event=>{event.preventDefault();perform(async()=>{await api('/api/wifi',{ssid:$('ssid').value,password:$('wifi-password').value});$('wifi-password').value='';hideSecrets();},'已尝试连接，请在 60 秒内确认，否则恢复原网络');};
$('wifi-keep').onclick=()=>perform(()=>api('/api/wifi-confirm',{keep:true}),'Wi-Fi 已保存');
$('wifi-revert').onclick=()=>perform(()=>api('/api/wifi-confirm',{keep:false}),'已恢复原网络');
$('logout').onclick=()=>perform(async()=>{await api('/api/logout',{});showLogin();},'已退出');
refresh();setInterval(()=>{if(state)refresh();},4000);
