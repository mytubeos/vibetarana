'use strict';
const $ = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
let state = null, busy = false;
if (tg) { tg.ready(); tg.expand(); }
const param = new URLSearchParams(location.search).get('chat');
if (param && /^-\d+$/.test(param)) $('chat').value = param;
function notice(text) { $('notice').textContent = text; }
async function api(action, extra = {}) {
  if (!tg?.initData) throw new Error('Please open /app from your Telegram bot chat.');
  const response = await fetch('/api', {method:'POST', headers:{'Content-Type':'application/json','X-Telegram-Init-Data':tg.initData}, body:JSON.stringify({action, chat_id:$('chat').value, ...extra})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Request failed.');
  return result;
}
function node(tag, text, cls) { const el = document.createElement(tag); el.textContent = text; if(cls) el.className=cls; return el; }
function render(data) {
  state = data;
  $('connection').textContent='Connected'; $('groupTitle').textContent=data.chat;
  const current = data.queue[0];
  $('title').textContent=current?.title || 'Bring the room together.';
  $('byline').textContent=current ? `Requested by ${current.requester} · ${current.duration}` : 'Pick a song and let everyone listen.';
  $('state').textContent=data.needs_resume ? 'SAVED QUEUE · RESUME TO START' : current ? (data.paused ? 'PAUSED' : 'NOW PLAYING') : 'READY WHEN YOU ARE';
  $('count').textContent=Math.max(0,data.queue.length-1);
  $('queue').replaceChildren();
  data.queue.slice(1).forEach((track,i)=>{const li=node('li','','track'), detail=node('div','','detail');detail.append(node('strong',track.title),node('small',track.requester));li.append(node('span',String(i+2),'number'),detail,node('span',track.duration,'duration'));$('queue').append(li);});
  if(data.queue.length<2) $('queue').append(node('li','No upcoming songs. Add one above.','empty'));
  document.querySelectorAll('[data-action]').forEach(b=>b.disabled=!data.can_control);
  $('playButton').disabled=!data.can_request;
  $('playlists').replaceChildren();
  data.playlists.forEach(name=>{const b=node('button',name);b.disabled=!data.can_request;b.onclick=()=>mutate('load',{name});$('playlists').append(b);});
}
async function refresh() { try { render(await api('state')); notice('Queue is up to date.'); } catch(e) {notice(e.message);} }
async function mutate(action, extra={}) {
  if(busy || !state) return;
  busy=true; notice('Working…');
  try { await api(action,{request_id:crypto.randomUUID(),revision:state.revision,playback_id:state.playback_id,...extra}); await refresh(); }
  catch(e){notice(e.message);} finally{busy=false;}
}
$('groupForm').onsubmit=e=>{e.preventDefault();state=null;refresh();};
$('playForm').onsubmit=e=>{e.preventDefault();mutate('play',{query:$('query').value});};
$('refresh').onclick=refresh;
document.querySelectorAll('[data-action]').forEach(b=>{b.disabled=true;b.onclick=()=>mutate(b.dataset.action);});
async function ops(){try{const d=await api('ops');const values={'Healthy assistants':`${d.healthy}/${d.assistants}`,'Active groups':d.active_groups,'Free slots':d.free_slots,'Queued tracks':d.queue_tracks,'Play success':d.success_rate===null?'No samples':`${(d.success_rate*100).toFixed(1)}%`,'Start p50':d.successful_start_seconds_p50===null?'No samples':`${d.successful_start_seconds_p50}s`,'Start p95':d.successful_start_seconds_p95===null?'No samples':`${d.successful_start_seconds_p95}s`,'Uptime':`${Math.floor(d.uptime_seconds/60)} min`};$('stats').replaceChildren();Object.entries(values).forEach(([k,v])=>{const el=node('div','','metric');el.append(node('span',k),node('strong',String(v)));$('stats').append(el);});notice('Metrics refreshed.');}catch(e){notice(e.message);}}
$('listenTab').onclick=()=>{$('listener').hidden=false;$('operator').hidden=true;$('listenTab').classList.add('active');$('opsTab').classList.remove('active');};
$('opsTab').onclick=()=>{$('listener').hidden=true;$('operator').hidden=false;$('opsTab').classList.add('active');$('listenTab').classList.remove('active');ops();};
$('refreshOps').onclick=ops;
if($('chat').value && tg?.initData) refresh();
