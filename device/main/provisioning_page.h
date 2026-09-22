/*
 * SPDX-License-Identifier: MIT
 *
 * 配网页 —— 内联成 `const char[]` 编进固件。
 *
 * 为什么不做成外部文件 / 不引 CDN：配网时手机连的是**板子自己的热点，
 * 根本没有外网**，任何外部资源（CDN 的 css/js/字体）都会白屏或超时。
 * 所以这一页必须自包含：内联 CSS、内联 JS、系统字体、零外部请求。
 *
 * 为什么不做成 SPIFFS 里的文件：那要动 partitions.csv（加 storage 分区）。
 * 这一页 gzip 前约 9 KB，3 MB 的 app 分区完全吃得下，不值得为它改分区表。
 *
 * 页面流程（对应 PROPOSAL §1.5）：
 *   ① 扫描并选择 WiFi（也可以手动填 SSID）
 *   ② 填服务器地址，可点「测试连接」当场验证
 *   ③ 设备名（多板场景区分用）
 *   ④ 高级：上报周期
 *   → 「保存并连接」：板子先试连，成功才写 NVS；失败当场红字说明原因
 */
#pragma once

static const char PROV_PAGE_HTML[] =
"<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
"<title>Ego Link 配网</title><style>"
"*{box-sizing:border-box}"
"body{margin:0;padding:16px;font:15px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;"
"background:#0f1115;color:#e8eaed;max-width:520px;margin:0 auto}"
"h1{font-size:19px;margin:0 0 4px}"
".sub{color:#9aa0a6;font-size:13px;margin-bottom:18px}"
"fieldset{border:1px solid #2a2f3a;border-radius:10px;padding:12px;margin:0 0 14px}"
"legend{color:#8ab4f8;font-size:13px;padding:0 6px}"
"label{display:block;font-size:13px;color:#9aa0a6;margin:8px 0 4px}"
"input,select{width:100%;padding:10px;border-radius:8px;border:1px solid #2a2f3a;"
"background:#171a21;color:#e8eaed;font-size:15px}"
"input:focus,select:focus{outline:2px solid #8ab4f8;outline-offset:0;border-color:transparent}"
"button{width:100%;padding:12px;border-radius:8px;border:0;font-size:15px;font-weight:600;"
"background:#8ab4f8;color:#0f1115;margin-top:10px}"
"button.sec{background:#2a2f3a;color:#e8eaed;font-weight:500;margin-top:6px}"
"button:disabled{opacity:.5}"
".row{display:flex;gap:8px}.row>*{flex:1}"
"#msg{margin-top:12px;padding:11px;border-radius:8px;font-size:14px;display:none;"
"white-space:pre-wrap;word-break:break-all}"
".ok{background:#14351f;color:#81c995;display:block!important}"
".err{background:#3b1d1d;color:#f28b82;display:block!important}"
".info{background:#1c2733;color:#8ab4f8;display:block!important}"
".hint{font-size:12px;color:#6b7280;margin-top:4px}"
"</style></head><body>"
"<h1>Ego Link 配网</h1>"
"<div class=\"sub\">填好后点「保存并连接」。板子会<strong>先试着连一次</strong>，"
"连上了才保存——所以失败了你能立刻知道是哪一步不对。</div>"

"<fieldset><legend>① 选择 WiFi</legend>"
"<label for=\"ssid\">WiFi 名称</label>"
"<div class=\"row\"><select id=\"ssid\" onchange=\"pickSsid()\">"
"<option value=\"\">扫描中…</option></select>"
"<button class=\"sec\" style=\"flex:0 0 90px\" onclick=\"scan()\">重新扫描</button></div>"
"<label for=\"ssid2\">或手动填写（隐藏网络）</label>"
"<input id=\"ssid2\" placeholder=\"留空则用上面的选择\" autocomplete=\"off\">"
"<label for=\"pass\">密码</label>"
"<div class=\"row\"><input id=\"pass\" type=\"password\" autocomplete=\"off\">"
"<button class=\"sec\" style=\"flex:0 0 70px\" onclick=\"togglePw()\" id=\"pwb\">显示</button></div>"
"<div class=\"hint\">开放网络请留空</div></fieldset>"

"<fieldset><legend>② 服务器地址</legend>"
"<input id=\"url\" placeholder=\"http://192.168.1.100:8000\" autocomplete=\"off\">"
"<div class=\"hint\">电脑上跑 server.py 的那台机器的地址（看 ipconfig 的 IPv4）</div>"
"<button class=\"sec\" onclick=\"testUrl()\">测试连接</button></fieldset>"

"<fieldset><legend>③ 设备名</legend>"
"<input id=\"device\" placeholder=\"留空 = 自动用 MAC 命名\" autocomplete=\"off\">"
"<div class=\"hint\">多块板同时用时，仪表盘靠这个名字区分；留空则按网卡 MAC 自动生成"
"（和热点名 EGO-LINK-XXXX 后四位一致）</div></fieldset>"

"<fieldset><legend>④ 高级</legend>"
"<label for=\"period\">上报周期（ms，留空用默认）</label>"
"<input id=\"period\" inputmode=\"numeric\" placeholder=\"500\" autocomplete=\"off\">"
"</fieldset>"

"<button id=\"save\" onclick=\"save()\">保存并连接</button>"
"<button class=\"sec\" onclick=\"clearCfg()\">清除配置并重启</button>"
"<div id=\"msg\"></div>"

"<script>"
"var $=function(i){return document.getElementById(i)};"
"function say(t,c){var m=$('msg');m.textContent=t;m.className=c||'info';}"
"function busy(b){$('save').disabled=b;$('save').textContent=b?'正在连接…':'保存并连接';}"
"function togglePw(){var p=$('pass');var s=p.type==='password';"
"p.type=s?'text':'password';$('pwb').textContent=s?'隐藏':'显示';}"
"function pickSsid(){var v=$('ssid').value;if(v)$('ssid2').value='';}"
"function scan(){var s=$('ssid');s.innerHTML='<option value=\"\">扫描中…</option>';"
"fetch('/scan').then(function(r){return r.json()}).then(function(d){"
"s.innerHTML='<option value=\"\">（请选择）</option>';"
"(d.nets||[]).forEach(function(n){var o=document.createElement('option');"
"o.value=n.ssid;o.textContent=n.ssid+(n.rssi?'  ('+n.rssi+'dBm)':'')+(n.open?'  开放':'');"
"s.appendChild(o)});"
"if(!(d.nets||[]).length)s.innerHTML='<option value=\"\">（没扫到，请手动填写）</option>';"
"}).catch(function(){s.innerHTML='<option value=\"\">（扫描失败，请手动填写）</option>'})}"
"function body(){var ssid=$('ssid2').value||$('ssid').value;"
"return 'ssid='+encodeURIComponent(ssid)+'&pass='+encodeURIComponent($('pass').value)"
"+'&url='+encodeURIComponent($('url').value)"
"+'&device='+encodeURIComponent($('device').value)"
"+'&period='+encodeURIComponent($('period').value)}"
"function post(path,extra){return fetch(path,{method:'POST',"
"headers:{'Content-Type':'application/x-www-form-urlencoded'},"
"body:body()+(extra||'')}).then(function(r){return r.json()})}"
"function testUrl(){var u=$('url').value;if(!u){say('请先填服务器地址','err');return}"
"say('正在测试 '+u+' …','info');"
"fetch('/testurl?url='+encodeURIComponent(u)).then(function(r){return r.json()})"
".then(function(d){d.ok?say('✓ 服务器可达（HTTP '+d.code+'）','ok')"
":say('✗ 连不上：'+(d.reason||'未知原因'),'err')})"
".catch(function(e){say('✗ 连不上：'+e,'err')})}"
"function save(){busy(true);say('正在连接 WiFi，最长等 15 秒…','info');"
"post('/save').then(function(d){busy(false);"
"if(d.ok){say('✓ 已连接，本机 IP '+d.ip+'\\n板子已保存配置并切换到正常模式，可以关掉这个页面了。','ok')}"
"else{say('✗ '+d.reason,'err')}}).catch(function(e){busy(false);say('✗ '+e,'err')})}"
"function clearCfg(){if(!confirm('清除配置并重启？重启后会重新进入配网模式。'))return;"
"fetch('/clear',{method:'POST'}).then(function(){say('已清除，板子正在重启…','info')})}"
"scan();"
"</script></body></html>";
