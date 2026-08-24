// This is the dashboard's brain — fetches check-in data from the API and renders the
// tables/charts/calendars/leave forms you see on screen. Used to be a giant inline
// <script> tag inside rto-ui.html; pulled out here so it's actually diffable and lintable.
// The file already has section dividers (search for "// --") marking the major chunks:
// utils, date helpers, nav, dashboard, today, my status, history, calendar, compliance,
// leave management, anomalies, override, team, roles, config, init, team assignment, search.

const API = window.location.origin;
const TODAY = (()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;})();
let MY_ID='', MY_TOKEN='', MY_ROLE='employee', MY_NAME='', MY_TEAM='';
let MY_MANAGED_TEAMS=null; // null=all, array=restricted teams
let DASH_TEAM_FILTER='';   // currently selected team filter on dashboard
let INSIGHTS_EMP_ID='';    // currently viewed employee on Insights page ('' = self)
let APP_SETTINGS={ show_split_timestamps: true }; // server-persisted display prefs

async function put(p,b){
  try{
    const r=await fetch(API+p,{method:'PUT',
      headers:{'Content-Type':'application/json','X-Employee-Id':MY_ID||'','X-Device-Token':MY_TOKEN||''},
      body:JSON.stringify(b)});
    return{ok:r.ok,data:await r.json()};
  }catch(e){return{ok:false,data:{}};}
}

// Returns team query param based on role and selected filter
function getTeamParam(selectedTeam){
  if(MY_ROLE==='employee') return MY_TEAM?`&team=${encodeURIComponent(MY_TEAM)}`:'';
  if(selectedTeam) return `&team=${encodeURIComponent(selectedTeam)}`;
  return ''; // manager/admin with no filter = all their teams (server filters)
}

// Build team filter options for manager/admin
function teamFilterOpts(teams, selectedTeam, includeAll=true){
  const allLabel = MY_ROLE==='admin'?'All teams':'All my teams';
  let opts = includeAll?`<option value="">${allLabel}</option>`:'';
  (teams||[]).forEach(t=>{
    opts+=`<option value="${t}"${t===selectedTeam?' selected':''}>${t}</option>`;
  });
  return opts;
}
const LEAVE_TYPES={
  annual:{label:'Annual Leave',icon:'plane'},
  casual:{label:'Casual Leave',icon:'calendar-days'},
  sick:{label:'Sick Leave',icon:'stethoscope'},
  public_holiday:{label:'Public Holiday',icon:'sparkles'},
  optional_holiday:{label:'Optional Holiday',icon:'calendar-plus'},
  half_day_am:{label:'Half Day AM',icon:'sunrise'},
  half_day_pm:{label:'Half Day PM',icon:'sunset'},
  other:{label:'Other',icon:'file-text'},
};
const pageTitles={dashboard:'Dashboard',today:"Today's Status",mystatus:'My Status',history:'History',compliance:'WFO Compliance',leavemgmt:'Leave Management',teamassign:'Team Access',anomalies:'Anomalies',override:'Override',team:'Team',roles:'Role Management',config:'Configuration',insights:'Insights',rhythm:'Team Rhythm'};

// -- UTILS ----------------------------------------------
async function get(p){
  try{
    const eid = MY_ID || localStorage.getItem('_sk_ei') || '';
    const token = MY_TOKEN || localStorage.getItem('_sk_dt') || '';
    const headers = {};
    if(eid) headers['X-Employee-Id']=eid;
    if(token) headers['X-Device-Token']=token;
    const r=await fetch(API+p,{headers});
    return r.ok?await r.json():null;
  }catch{return null;}
}
async function post(p,b){
  try{
    const r=await fetch(API+p,{method:'POST',
      headers:{'Content-Type':'application/json','X-Employee-Id':MY_ID||'','X-Device-Token':MY_TOKEN||''},
      body:JSON.stringify(b)});
    return{ok:r.ok,data:await r.json()};
  }catch(e){return{ok:false,data:{error:e.message}};}
}
async function del(p,b={}){
  try{
    const r=await fetch(API+p,{method:'DELETE',
      headers:{'Content-Type':'application/json','X-Employee-Id':MY_ID||'','X-Device-Token':MY_TOKEN||''},
      body:JSON.stringify(b)});
    return{ok:r.ok,data:await r.json()};
  }catch(e){return{ok:false,data:{}};}
}
async function patch(p,b={}){
  try{
    const r=await fetch(API+p,{method:'PATCH',
      headers:{'Content-Type':'application/json','X-Employee-Id':MY_ID||'','X-Device-Token':MY_TOKEN||''},
      body:JSON.stringify(b)});
    return{ok:r.ok,data:await r.json()};
  }catch(e){return{ok:false,data:{}};}
}
// Renders an LLM-generated narrative sentence, or nothing at all if one
// isn't available — the raw numbers around this block already stand on
// their own, so absence is never a broken state, just a plainer one.
// Styled distinctly (italic, left accent bar) so it visibly reads as
// commentary rather than another data field, given it's paraphrase, not
// a new source of truth.
function narrativeBlock(text){
  if(!text) return '';
  const esc = String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  // Blue, not the page's accent orange — orange already means "at risk" on
  // these pages (badges, targets-missed rows), and this box sitting right
  // above those in the same color read as another warning rather than
  // commentary. Bottom-margin only (no top) so it sits flush under the
  // title/legend above it instead of adding an extra, uneven gap.
  return `<div style="font-size:12px;font-style:italic;color:var(--tx2);line-height:1.5;
              padding:8px 12px;margin:0 0 14px;border-left:2px solid var(--blue);
              border-radius:0 6px 6px 0;background:var(--bg2)">
    ${esc}
  </div>`;
}

function toggleComplianceLegend(){
  const el = document.getElementById('compliance-legend-text');
  if(!el) return;
  const expanded = el.style.maxWidth !== '0px' && el.style.maxWidth !== '';
  el.style.maxWidth = expanded ? '0px' : '900px';
  el.style.opacity  = expanded ? '0' : '1';
  el.style.whiteSpace = expanded ? 'nowrap' : 'normal';
}

function renderSplitLabel(label){
  if(!label) return '';
  const parts = label.split(' → ');
  // Timestamps shown only when the admin setting is enabled AND viewer is manager/admin
  const showTime = APP_SETTINGS.show_split_timestamps && (MY_ROLE==='manager'||MY_ROLE==='admin');
  const html = parts.map((part, i) => {
    const tokens = part.trim().split(' ');
    const status = tokens[0].toLowerCase();
    const time   = tokens[1] || null;
    const cls    = status==='wfo'?'seg-wfo':status==='wfh'?'seg-wfh':'seg-other';
    const arrow  = i > 0 ? '<span class="seg-arrow" style="color:var(--tx3);font-size:9px;margin:0 2px">&#8594;</span>' : '';
    const timeBadge = (time && showTime) ? `<span class="seg-time">${time}</span>` : '';
    return `${arrow}<span class="${cls}" style="font-size:10px;font-family:var(--mono);font-weight:500">${status.toUpperCase()}</span>${timeBadge}`;
  }).join('');
  return `<span class="split-pill">${html}</span>`;
}
// Strip timestamps from a raw split_label string when the setting/role says not to show them.
// Used for title attributes and any path that doesn't go through renderSplitLabel().
function splitLabelForDisplay(label){
  if(!label) return '';
  if(APP_SETTINGS.show_split_timestamps && (MY_ROLE==='manager'||MY_ROLE==='admin')) return label;
  // Remove " HH:MM" tokens — e.g. "WFH 07:21 → WFO 08:57" → "WFH → WFO"
  return label.replace(/\s+\d{2}:\d{2}/g, '');
}
function fmt(d){if(!d)return'-';return new Date(d).toLocaleTimeString('en-IN',{hour:'numeric',minute:'2-digit',hour12:true,timeZone:'Asia/Kolkata'});}
// Masks the last two octets of any IPv4 address it finds — works on a bare
// IP string ("192.168.1.2" -> "192.168.X.X") and on free text that happens
// to have one embedded in it (anomaly/flag descriptions), since it's just
// a regex swap, not a strict field parser.
function redactIp(s){if(s==null)return s;return String(s).replace(/\b(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}\b/g,'$1.$2.X.X');}
function colorFor(id){const C=['#7c3aed','#2563eb','#059669','#d97706','#dc2626','#0891b2','#db2777'];let h=0;for(const c of(id||''))h=(h*31+c.charCodeAt(0))%C.length;return C[h];}
function iniOf(n){return(n||'?').split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();}
function chipS(s,sl){
  if(s==='split'){const splitCls=sl&&sl.includes('WFO')?'c-split c-split-wfo':'c-split';return`<span class="chip ${splitCls}" title="${splitLabelForDisplay(sl)||''}">${svgI('split',11)} Split</span>`;}
  if(s==='wfo')return`<span class="chip c-wfo">${svgI('building')} WFO</span>`;
  if(s==='wfh')return`<span class="chip c-wfh">${svgI('home')} WFH</span>`;
  if(s==='vpn_ambiguous')return`<span class="chip c-vpn">${svgI('shield-check')} VPN</span>`;
  if(s&&LEAVE_TYPES[s])return`<span class="chip c-leave">${svgI(LEAVE_TYPES[s].icon)} ${LEAVE_TYPES[s].label}</span>`;
  return s?`<span class="chip" style="background:var(--bg2);color:var(--tx3)">${s}</span>`:'-';
}
function chipC(c){if(c==='high')return`<span class="chip c-hi">High</span>`;if(c==='medium')return`<span class="chip c-md">Med</span>`;if(c==='low')return`<span class="chip c-lo">Low</span>`;return c?`<span class="chip" style="background:var(--bg2);color:var(--tx3)">${c}</span>`:'-';}
function svgI(name,sz=12){return`<svg xmlns="http://www.w3.org/2000/svg" width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" data-lucide="${name}"></svg>`;}
function notice(type,msg){const icons={ok:'check-circle',warn:'triangle-alert',err:'x-circle',info:'info'};return`<div class="notice ${type}" style="margin-top:10px">${svgI(icons[type]||'info')}<span>${msg}</span></div>`;}
// reIcons defined in INIT section
function monthOpts(selId,count=6){const sel=document.getElementById(selId);if(!sel)return;const now=new Date();sel.innerHTML=Array.from({length:count},(_,i)=>{const d=new Date(now.getFullYear(),now.getMonth()-i,1);const v=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`;const l=d.toLocaleDateString('en-GB',{month:'long',year:'numeric'});return`<option value="${v}"${i===0?' selected':''}>${l}</option>`;}).join('');}

// -- DATE RANGE HELPERS --------------------------------
function nextWorkday(){
  // Returns TODAY if weekday, or next Monday if weekend - never past
  const[y,m,d]=TODAY.split('-').map(Number);
  const dow=new Date(y,m-1,d).getDay();
  let offset=0;
  if(dow===6)offset=2;else if(dow===0)offset=1;
  const nd=new Date(y,m-1,d+offset);
  return `${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,'0')}-${String(nd.getDate()).padStart(2,'0')}`;
}

function getWorkingDays(from,to){
  if(!from||!to)return[];
  const[fy,fm,fd]=from.split('-').map(Number);
  const[ty,tm,td]=to.split('-').map(Number);
  const[toy,tom,tod]=TODAY.split('-').map(Number);
  const f=new Date(fy,fm-1,fd),t=new Date(ty,tm-1,td);
  const todayD=new Date(toy,tom-1,tod);
  // Reject past dates - from must be >= today
  if(f<todayD||f>t)return[];
  const days=[];
  for(const d=new Date(f);d<=t;d.setDate(d.getDate()+1)){
    const dow=d.getDay();
    if(dow!==0&&dow!==6){
      const y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),day=String(d.getDate()).padStart(2,'0');
      days.push(`${y}-${m}-${day}`);
    }
  }
  return days;
}
function syncToDate(fromId, toId){
  // When from-date changes, ensure to-date is not before from-date
  const fromEl=document.getElementById(fromId);
  const toEl=document.getElementById(toId);
  if(!fromEl||!toEl)return;
  toEl.min=fromEl.value;  // to-date can't be before from-date
  if(toEl.value && toEl.value < fromEl.value) toEl.value=fromEl.value;
}

function syncToDate(fromId,toId){
  const fromEl=document.getElementById(fromId);
  const toEl=document.getElementById(toId);
  if(!fromEl||!toEl)return;
  // Enforce today as absolute minimum on both
  if(fromEl.value<TODAY){fromEl.value=TODAY;}
  fromEl.min=TODAY;
  toEl.min=fromEl.value;
  if(!toEl.value||toEl.value<fromEl.value)toEl.value=fromEl.value;
}

function updateLeaveDayPreview(){
  const from=document.getElementById('lm-date-from')?.value;
  const to=document.getElementById('lm-date-to')?.value;
  const el=document.getElementById('lm-day-preview');
  if(!el)return;
  if(!from||!to){el.innerHTML='';return;}
  const days=getWorkingDays(from,to);
  if(days.length){
    el.style.color='var(--accl)';
    el.innerHTML=`${days.length} working day${days.length!==1?'s':''} selected (weekends excluded)`;
  } else {
    el.style.color='var(--red)';
    el.innerHTML=svgI('triangle-alert',11)+' No working days in range';
    reIcons();
  }
}
function updateMyLeaveDayPreview(){
  const from=document.getElementById('my-leave-date-from')?.value;
  const to=document.getElementById('my-leave-date-to')?.value;
  const el=document.getElementById('my-day-preview');
  if(!el)return;
  if(!from||!to){el.innerHTML='';return;}
  const days=getWorkingDays(from,to);
  if(days.length){
    el.style.color='var(--accl)';
    el.innerHTML=`${days.length} working day${days.length!==1?'s':''} selected`;
  } else {
    el.style.color='var(--red)';
    el.innerHTML=svgI('triangle-alert',11)+' No working days in range';
    reIcons();
  }
}

// -- NAV -----------------------------------------------
function buildNav(){
  const nav=document.getElementById('sb-nav');
  const isMgr=MY_ROLE==='manager'||MY_ROLE==='admin';
  const isAdm=MY_ROLE==='admin';
  let html=`<div class="nav-grp">Overview</div>
  <div class="nav-item active" onclick="nav('dashboard')"><i data-lucide="layout-dashboard"></i>Dashboard</div>`;
  if(MY_ROLE==='employee'){
    html+=`<div class="nav-item" onclick="nav('mystatus')"><i data-lucide="user"></i>My Status</div>`;
  }else{
    html+=`<div class="nav-item" onclick="nav('today')"><i data-lucide="monitor-check"></i>Today's Status</div>
    <div class="nav-item" onclick="nav('history')"><i data-lucide="history"></i>History</div>`;
  }
  if(isMgr){
    html+=`<div class="nav-grp" style="margin-top:8px">Management</div>
    <div class="nav-item" onclick="nav('compliance')"><i data-lucide="bar-chart-3"></i>Compliance</div>
    <div class="nav-item" onclick="nav('leavemgmt')"><i data-lucide="calendar-days"></i>Leave</div>
    <div class="nav-item" onclick="nav('anomalies')"><i data-lucide="scan-search"></i>Anomalies <span class="nav-badge" id="nb-anomalies">0</span></div>
    <div class="nav-item" onclick="nav('override')"><i data-lucide="square-pen"></i>Override</div>
    <div class="nav-item" onclick="nav('team')"><i data-lucide="users"></i>Team</div>
    <div class="nav-item" onclick="nav('teamassign')"><i data-lucide="network"></i>Team Access</div>`;
  }
  // Insights + Team Rhythm — visible to all roles
  html+=`<div class="nav-grp" style="margin-top:8px">Analytics</div>
  <div class="nav-item" onclick="nav('insights')"><i data-lucide="sparkles"></i>Insights</div>
  <div class="nav-item" onclick="nav('rhythm')"><i data-lucide="activity"></i>Team Rhythm</div>`;
  if(isAdm){
    html+=`<div class="nav-grp" style="margin-top:8px">Admin</div>
    <div class="nav-item" onclick="nav('roles')"><i data-lucide="shield-check"></i>Roles</div>
    <div class="nav-item" onclick="nav('config')"><i data-lucide="settings-2"></i>Config</div>`;
  }
  nav.innerHTML=html;
  reIcons();
}

function buildTopbarActions(){
  const el=document.getElementById('topbar-actions');
  const isMgr=MY_ROLE==='manager'||MY_ROLE==='admin';
  el.innerHTML=`<button class="btn btn-ghost btn-sm" onclick="refreshAll()"><i data-lucide="refresh-cw"></i>Refresh</button>${isMgr?`<button class="btn btn-ghost btn-sm" onclick="exportCSV()"><i data-lucide="download"></i>Export CSV</button>`:''} ${isMgr?`<button class="btn btn-acc btn-sm" onclick="nav('override')"><i data-lucide="square-pen"></i>Override</button>`:''}`;
  reIcons();
}

function showUnregisteredState(msg='This browser is not linked to a registered employee.'){
  MY_ID=''; MY_TOKEN=''; MY_ROLE='employee'; MY_NAME=''; MY_TEAM=''; MY_MANAGED_TEAMS=null; INSIGHTS_EMP_ID='';
  document.getElementById('sb-ava').textContent='?';
  document.getElementById('sb-ava').style.background='';
  document.getElementById('sb-name').textContent='Not registered';
  document.getElementById('sb-id').textContent='Open your registration link';
  const badge=document.getElementById('sb-role-badge');
  badge.textContent='Guest';
  badge.className='u-role';
  document.getElementById('sb-nav').innerHTML='<div class="nav-grp">Overview</div><div class="nav-item active"><i data-lucide="layout-dashboard"></i>Dashboard</div>';
  document.getElementById('topbar-actions').innerHTML='';
  ['st-wfo','st-wfh','st-amb','st-flg'].forEach(id=>{const el=document.getElementById(id);if(el)el.textContent='-';});
  const sub=document.getElementById('st-wfo-sub'); if(sub)sub.textContent='Registration required';
  const sub2=document.getElementById('st-wfh-sub'); if(sub2)sub2.textContent='Registration required';
  const empty=`<div class="notice warn" style="margin:0">${svgI('triangle-alert')}<span>${msg} Register from the RTO agent link on this device, then reopen the dashboard.</span></div>`;
  const bar=document.getElementById('bar-chart'); if(bar)bar.innerHTML=empty;
  const donut=document.getElementById('donut-wrap'); if(donut)donut.innerHTML=empty;
  const body=document.getElementById('dash-tbody'); if(body)body.innerHTML=`<tr><td colspan="6">${empty}</td></tr>`;
  reIcons();
}

function nav(id){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const page=document.getElementById('page-'+id);
  if(!page)return;
  page.classList.add('active');
  // Match nav item by onclick
  document.querySelectorAll('.nav-item').forEach(n=>{if(n.getAttribute('onclick')===`nav('${id}')`)n.classList.add('active');});
  document.getElementById('pg-title').textContent=pageTitles[id]||id;
  if(id==='dashboard')loadDashboard();
  if(id==='today')loadToday();
  if(id==='mystatus')loadMyStatus();
  if(id==='history')initHistory();
  if(id==='compliance')loadCompliance();
  if(id==='leavemgmt')initLeaveManagement();
  if(id==='anomalies')loadAnomalies();
  if(id==='team')loadTeam();
  if(id==='roles')loadRoles();
  if(id==='teamassign')loadTeamAssign();
  if(id==='config')loadConfig();
  if(id==='insights')loadInsights();
  if(id==='rhythm')loadRhythm();
  setTimeout(reIcons,150);
}

// -- SIDEBAR USER --------------------------------------
async function loadSidebarUser(){
  // Check for auth handoff in URL — agent deposits this after syncing token.
  // Consuming it auto-populates localStorage so browser is instantly logged in.
  const urlParams = new URLSearchParams(window.location.search);
  const handoff = urlParams.get('auth');
  if(handoff){
    try{
      const hr = await fetch(`/api/auth-handoff/${handoff}`);
      if(hr.ok){
        const hd = await hr.json();
        if(hd.api_token && hd.employee_id){
          localStorage.setItem('_sk_dt', hd.api_token);
          localStorage.setItem('_sk_ei', hd.employee_id);
          localStorage.setItem('_sk_host', hd.hostname || '');
          // Remove ?auth= from URL cleanly without reload
          const clean = window.location.pathname;
          window.history.replaceState({}, '', clean);
        }
      }
    } catch(e){}
  }

  // Migrate old localStorage key names to new ones (one-time)
  const oldEid   = localStorage.getItem('rto_my_employee_id');
  const oldToken = localStorage.getItem('rto_device_token');
  const oldHost  = localStorage.getItem('rto_hostname');
  if(oldEid && !localStorage.getItem('_sk_ei')){
    localStorage.setItem('_sk_ei', oldEid);
    localStorage.removeItem('rto_my_employee_id');
  }
  if(oldToken && !localStorage.getItem('_sk_dt')){
    localStorage.setItem('_sk_dt', oldToken);
    localStorage.removeItem('rto_device_token');
  }
  if(oldHost && !localStorage.getItem('_sk_host')){
    localStorage.setItem('_sk_host', oldHost);
    localStorage.removeItem('rto_hostname');
  }
  const stored = localStorage.getItem('_sk_ei');
  let storedToken = localStorage.getItem('_sk_dt');

  // No employee ID at all — show unregistered
  if(!stored){ showUnregisteredState(); return false; }

  // Have employee ID but no token — try token-refresh silently
  // This handles existing users and users who cleared localStorage
  if(stored && !storedToken){
    try {
      // Try stored hostname first, then look it up by employee ID
      let h = localStorage.getItem('_sk_host') || '';
      if(!h){
        // Find hostname by querying device info for this employee
        const devR = await fetch(`/api/device-by-employee/${stored}`).catch(()=>null);
        if(devR && devR.ok){
          const devD = await devR.json();
          h = devD.hostname || '';
          if(h) localStorage.setItem('_sk_host', h);
        }
      }
      if(h){
        const tr = await fetch(`/api/token-refresh/${h}`, {method:'POST'});
        if(tr.ok){
          const td = await tr.json();
          if(td.api_token){
            storedToken = td.api_token;
            localStorage.setItem('_sk_dt', storedToken);
          }
        }
      }
    } catch(e){}
    // Still no token after refresh attempt
    if(!storedToken){ showUnregisteredState(); return false; }
  }

  MY_ID = stored;
  MY_TOKEN = storedToken;
  const meResp = await fetch(`${API}/api/me`,{headers:{'X-Employee-Id':stored,'X-Device-Token':storedToken}});
  if(!meResp.ok){
    localStorage.removeItem('_sk_ei');
    localStorage.removeItem('_sk_dt');
    showUnregisteredState('Saved employee identity is no longer registered.');
    return false;
  }
  const me = await meResp.json();
  MY_ID=me.employee_id; MY_ROLE=me.role||'employee'; MY_NAME=me.employee_name; MY_TEAM=me.team||'';
  // Clear caches so subsequent calls use the correct employee ID with proper filtering
  _allTeamsCache = null;
  _allEmployeesCache = null;
  // Load managed teams for manager/admin
  if(MY_ROLE==='manager'||MY_ROLE==='admin'){
    try{
      const mt=await fetch(`${API}/api/managed-teams/${MY_ID}`,{headers:{'X-Employee-Id':MY_ID,'X-Device-Token':MY_TOKEN}});
      if(mt.ok){const mtd=await mt.json();MY_MANAGED_TEAMS=mtd.managed_teams||null;}
    }catch(e){}
  }
  const ini=iniOf(me.employee_name),color=colorFor(me.employee_id);
  document.getElementById('sb-ava').textContent=ini;
  document.getElementById('sb-ava').style.background=color;
  document.getElementById('sb-name').textContent=me.employee_name;
  document.getElementById('sb-id').textContent=me.employee_id;
  const badge=document.getElementById('sb-role-badge');
  badge.textContent=MY_ROLE.charAt(0).toUpperCase()+MY_ROLE.slice(1);
  badge.className=`u-role role-${MY_ROLE}`;
  if(MY_ROLE==='admin'){
    badge.style.cursor='pointer';
    badge.title='Open DB Admin';
    badge.onclick=()=>window.open('/admin','_blank');
  }
  buildNav(); buildTopbarActions();
  if(MY_ROLE!=='employee'){
    const s=await get('/api/stats');
    if(s){const b=document.getElementById('nb-anomalies');if(b)b.textContent=s.flagged;}
  }
  return true;
}

// -- DASHBOARD -----------------------------------------
async function loadDashboard(){
  // Setup filter bar — managers/admins only
  // Employees never see the team filter or global employee search
  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';
  const filterBar = document.getElementById('dash-filter-bar');
  const filterSel = document.getElementById('dash-team-filter');
  const searchWrap = document.getElementById('dash-search-wrap');
  if(isMgr){
    if(filterBar) filterBar.style.display='block';
    if(searchWrap) searchWrap.style.display='flex';
    if(filterSel){
      const teams = MY_MANAGED_TEAMS || await _getAllTeams();
      filterSel.innerHTML = teamFilterOpts(teams, DASH_TEAM_FILTER);
    }
  } else {
    // Employee: always hide filter bar and search
    if(filterBar) filterBar.style.display='none';
    if(searchWrap) searchWrap.style.display='none';
  }
  // Employees always scope to their own team; managers use DASH_TEAM_FILTER (or no filter = all managed)
  const tp = MY_ROLE === 'employee' && MY_TEAM
    ? `?team=${encodeURIComponent(MY_TEAM)}`
    : DASH_TEAM_FILTER ? `?team=${encodeURIComponent(DASH_TEAM_FILTER)}` : '';
  const[stats,today,week]=await Promise.all([
    get('/api/stats'+tp),
    get('/api/today'+tp),
    get('/api/week'+tp)
  ]);
  if(stats){
    document.getElementById('st-wfo').textContent=stats.wfo;
    document.getElementById('st-wfh').textContent=stats.wfh;
    document.getElementById('st-amb').textContent=stats.ambiguous;
    document.getElementById('st-flg').textContent=stats.flagged;
    document.getElementById('st-wfo-sub').textContent=stats.wfo===1?'1 person':`${stats.wfo} people`;
    // On a public holiday, show holiday label instead of WFH count subtitle
    if(stats.is_public_holiday){
      document.getElementById('st-wfh-sub').textContent='Public Holiday';
      document.getElementById('st-wfh').style.color='var(--amber)';
    } else {
      document.getElementById('st-wfh-sub').textContent=stats.wfh===1?'1 person':`${stats.wfh} people`;
      document.getElementById('st-wfh').style.color='';
    }
  }
  if(week?.week){
    const maxT=Math.max(...week.week.map(d=>d.wfo+d.wfh+d.ambiguous),1),H=64;
    document.getElementById('bar-chart').innerHTML=week.week.map(d=>{
      const t=d.wfo+d.wfh+d.ambiguous;
      const wH=Math.round((d.wfo/maxT)*H),fH=Math.round((d.wfh/maxT)*H),aH=Math.round((d.ambiguous/maxT)*H);
      const day=new Date(d.date+'T12:00:00').toLocaleDateString('en-GB',{weekday:'short'});
      // Public holiday: show amber bar at minimum height so day is visible
      if(d.is_public_holiday){
        const phH=Math.max(wH, 8); // at least 8px so holiday is visible
        return`<div class="bar-grp"><div class="bar-stack" style="height:${phH}px;opacity:0.5"><div class="bar-seg" style="height:${phH}px;background:var(--amber)"></div></div><div class="bar-lbl" style="color:var(--amber)">${day}</div></div>`;
      }
      return`<div class="bar-grp"><div class="bar-stack" style="height:${wH+fH+aH}px"><div class="bar-seg" style="height:${aH}px;background:var(--amber);opacity:0.8"></div><div class="bar-seg" style="height:${fH}px;background:var(--blue);opacity:0.8"></div><div class="bar-seg" style="height:${wH}px;background:var(--green);opacity:0.8"></div></div><div class="bar-lbl">${day}</div></div>`;
    }).join('');
  }
  if(stats){
    const total=stats.wfo+stats.wfh+stats.ambiguous,circ=238;
    const wfoA=total?Math.round((stats.wfo/total)*circ):0,wfhA=total?Math.round((stats.wfh/total)*circ):0,ambA=total?Math.round((stats.ambiguous/total)*circ):0;
    document.getElementById('donut-total').textContent=`${total} total`;
    document.getElementById('donut-wrap').innerHTML=`<svg width="100" height="100" viewBox="0 0 100 100"><circle cx="50" cy="50" r="38" fill="none" stroke="var(--bg2)" stroke-width="14"/><circle cx="50" cy="50" r="38" fill="none" stroke="var(--green)" stroke-width="14" stroke-dasharray="${wfoA} ${circ}" stroke-dashoffset="0" stroke-linecap="round"/><circle cx="50" cy="50" r="38" fill="none" stroke="var(--blue)" stroke-width="14" stroke-dasharray="${wfhA} ${circ}" stroke-dashoffset="${-wfoA}" stroke-linecap="round"/><circle cx="50" cy="50" r="38" fill="none" stroke="var(--amber)" stroke-width="14" stroke-dasharray="${ambA} ${circ}" stroke-dashoffset="${-(wfoA+wfhA)}" stroke-linecap="round"/><text x="50" y="46" text-anchor="middle" font-family="Instrument Serif" font-size="16" fill="var(--tx)">${total}</text><text x="50" y="58" text-anchor="middle" font-family="Geist Mono" font-size="7" fill="var(--tx3)">TODAY</text></svg><div class="donut-legend"><div class="leg-item"><div class="leg-dot" style="background:var(--green)"></div><span style="color:var(--tx2)">In office</span><span class="leg-val">${stats.wfo}</span></div><div class="leg-item"><div class="leg-dot" style="background:var(--blue)"></div><span style="color:var(--tx2)">WFH</span><span class="leg-val">${stats.wfh}</span></div><div class="leg-item"><div class="leg-dot" style="background:var(--amber)"></div><span style="color:var(--tx2)">Pending</span><span class="leg-val">${stats.ambiguous}</span></div></div>`;
  }
  if(today?.checkins){
    // Employees only see their own team's check-ins in Recent section
    const visibleCheckins = (MY_ROLE==='employee' && MY_TEAM)
      ? today.checkins.filter(c => c.team === MY_TEAM || c.employee_id === MY_ID)
      : today.checkins;
    document.getElementById('dash-tbody').innerHTML=visibleCheckins.length
      ?visibleCheckins.map(c=>{const color=colorFor(c.employee_id),ini=iniOf(c.employee_name);
        const status=c.display_status||c.status;
        return`<tr><td><div style="display:flex;align-items:center;gap:9px"><div style="width:26px;height:26px;border-radius:50%;background:${color};display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;color:#fff">${ini}</div><div><div style="font-size:12px;font-weight:500">${c.employee_name}</div><div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${c.employee_id}</div></div></div></td><td><div style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap">${chipS(status,c.split_label)}${renderSplitLabel(c.split_label)}</div></td><td><code style="font-size:10px;background:var(--bg2);padding:2px 6px;border-radius:4px">${redactIp(c.lan_ip)||'-'}</code></td><td style="font-family:var(--mono);font-size:10px;color:${c.vpn_active?'var(--amber)':'var(--tx3)'}">${c.vpn_active?'<span style="color:var(--amber);display:inline-flex;align-items:center;gap:3px">' + svgI('lock',11) + ' On</span>':'<span style="color:var(--tx3)">Off</span>'}</td><td>${chipC(c.confidence)}</td><td style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${fmt(c.timestamp)}</td></tr>`;
      }).join('')
      :'<tr><td colspan="6" style="text-align:center;color:var(--tx3);padding:24px;font-family:var(--mono);font-size:11px">No check-ins today yet.</td></tr>';
    reIcons();
  }
}

// -- TODAY ---------------------------------------------
async function loadToday(){
  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';
  const todayFilter = document.getElementById('today-team-filter');
  if(isMgr && todayFilter){
    todayFilter.style.display='block';
    const teams = MY_MANAGED_TEAMS || await _getAllTeams();
    if(todayFilter.options.length<=1) todayFilter.innerHTML=teamFilterOpts(teams, '');
  }
  const selectedTeam = todayFilter?.value||'';
  const url = selectedTeam
    ? `/api/today?team=${encodeURIComponent(selectedTeam)}`
    : '/api/today';
  const d=await get(url);
  const grid=document.getElementById('today-grid');
  if(!d?.checkins?.length){grid.innerHTML='<div class="loading">No check-ins today.</div>';return;}
  grid.innerHTML=d.checkins.map(c=>{
    const color=colorFor(c.employee_id),ini=iniOf(c.employee_name);
    const status=c.display_status||c.status; // split for pill, dominant for compliance
    const leaveHtml=(c.leaves||[]).map(l=>`<div class="leave-tag">${svgI(LEAVE_TYPES[l.leave_type]?.icon||'calendar')} ${l.label}</div>`).join('');
    return`<div class="person-card${c.flagged?' flagged':''}"><div class="pc-top"><div class="pc-ava" style="background:${color}">${ini}</div><div><div class="pc-name">${c.employee_name}</div><div class="pc-id">${c.employee_id}</div></div></div><div style="display:flex;flex-direction:column;align-items:flex-start;gap:4px;margin-top:4px">${chipS(status,c.split_label)}${renderSplitLabel(c.split_label)}</div>${leaveHtml}<div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${redactIp(c.lan_ip)||'-'}</div></div>`;
  }).join('');
  reIcons();
}

// -- MY STATUS -----------------------------------------
async function loadMyStatus(){
  if(!MY_ID)return;
  monthOpts('my-hist-month');
  monthOpts('team-leave-month');
  const today=await get('/api/today');
  const me=today?.checkins?.find(c=>c.employee_id===MY_ID);
  const el=document.getElementById('my-status-content');
  if(me){
    const s=me.display_status||me.status,dom=me.dominant_status||s,color=dom==='wfo'?'var(--green)':dom==='wfh'?'var(--blue)':'var(--amber)';
    el.innerHTML=`
      <div style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px">
        ${chipS(s, me.split_label)}${renderSplitLabel(me.split_label)}
      </div>
      <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-bottom:6px">LAN: ${redactIp(me.lan_ip)||'-'} &middot; ${fmt(me.timestamp)}</div>
      <div>${chipC(me.confidence)}</div>`;
  }else{el.innerHTML=`<div style="color:var(--tx3);font-family:var(--mono);font-size:12px;padding:16px 0">No check-in recorded today yet.</div>`;}
  buildLeaveApplyForm();
  loadMyHistory();
  loadTeamTodayStatus();
  loadTeamLeaveCalendar();
}

async function loadTeamTodayStatus(){
  const el = document.getElementById('team-today-content');
  if(!el) return;
  el.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;

  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';

  if(isMgr){
    // Manager/admin: use /api/today which already filters by managed_teams server-side
    const label = MY_MANAGED_TEAMS?.length
      ? MY_MANAGED_TEAMS.join(' + ') + ' - Today'
      : 'All Teams - Today';
    const titleEl = document.getElementById('team-today-title');
    if(titleEl) titleEl.textContent = label;
    const data = await get('/api/today');
    const members = (data?.checkins||[]).map(c=>({
      employee_id:c.employee_id, employee_name:c.employee_name,
      status:c.display_status||c.status, split_label:c.split_label,
      confidence:c.confidence, timestamp:c.timestamp, lan_ip:c.lan_ip, team:c.team
    }));
    _renderTeamTodayMembers(el, members, label);
    return;
  }

  // Employee: show only own registered team
  let myTeam = null;
  try {
    const meR = await fetch('/api/me', {headers:{'X-Employee-Id':MY_ID,'X-Device-Token':MY_TOKEN}});
    if(meR.ok){ const meData = await meR.json(); myTeam = meData?.team || null; }
  } catch(e) {}

  if(!myTeam){
    el.innerHTML = `<div style="color:var(--tx3);font-family:var(--mono);font-size:12px;padding:12px 0">No team assigned.</div>`;
    return;
  }
  document.getElementById('team-today-title').textContent = `${myTeam} - Today`;

  const data = await get(`/api/today/team?team=${encodeURIComponent(myTeam)}`);
  // Override status for public holidays — same logic as /api/today
  const todayPH = await get('/api/stats');  // already fetched, has is_public_holiday
  const members = (data?.members || []).map(m => {
    if(todayPH?.is_public_holiday && m.status !== 'wfo'){
      return {...m, status:'public_holiday', split_label:null};
    }
    return m;
  });

  if(!members.length){
    el.innerHTML = `<div style="color:var(--tx3);font-family:var(--mono);font-size:12px;padding:12px 0">No team members found.</div>`;
    return;
  }

  const STATUS_META = {
    wfo:           {label:'WFO',       color:'var(--green)', bg:'var(--gbg)',  icon:'building-2'},
    wfh:           {label:'WFH',       color:'var(--blue)',  bg:'var(--bbg)',  icon:'home'},
    not_checked_in:{label:'No check-in',color:'var(--tx3)', bg:'var(--bg3)',  icon:'clock'},
    annual:        {label:'Annual Leave',color:'var(--amber)',bg:'var(--abg)', icon:'plane'},
    casual:        {label:'Casual Leave',color:'var(--amber)',bg:'var(--abg)', icon:'calendar-days'},
    sick:          {label:'Sick Leave', color:'var(--red)',  bg:'var(--rbg)',  icon:'stethoscope'},
    public_holiday:{label:'Holiday',   color:'var(--amber)', bg:'var(--abg)', icon:'sparkles'},
    optional_holiday:{label:'Opt Holiday',color:'var(--amber)',bg:'var(--abg)',icon:'calendar-plus'},
    half_day_am:   {label:'Half Day AM',color:'var(--amber)',bg:'var(--abg)', icon:'sunrise'},
    half_day_pm:   {label:'Half Day PM',color:'var(--amber)',bg:'var(--abg)', icon:'sunset'},
  };

  // Sort: WFO first, then WFH, then leave, then not checked in; me always first
  const order = {wfo:0, wfh:1, not_checked_in:99};
  members.sort((a,b)=>{
    if(a.employee_id===MY_ID) return -1;
    if(b.employee_id===MY_ID) return 1;
    return (order[a.status]??5) - (order[b.status]??5);
  });

  const cards = members.map(m => {
    const isMe = m.employee_id === MY_ID;
    const meta = STATUS_META[m.status] || {label:(m.status||'').replace(/_/g,' ').toUpperCase(), color:'var(--tx2)', bg:'var(--bg3)', icon:'circle-help'};
    const initials = m.employee_name.split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();
    const ts = m.timestamp ? `<div style="font-family:var(--mono);font-size:9px;color:var(--tx3);margin-top:2px">${fmt(m.timestamp)}</div>` : '';
    const splitLbl = m.split_label ? `<div style="display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:2px">${chipS(m.status||'',m.split_label)}${renderSplitLabel(m.split_label)}</div>` : '';
    const meBorder = isMe ? 'border:1.5px solid rgba(193,123,63,0.4)' : 'border:1px solid var(--b0)';
    return `<div style="display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;
            background:var(--bg2);${meBorder};transition:background 0.12s">
      <div style="width:32px;height:32px;border-radius:50%;background:${meta.bg};border:1px solid ${meta.color}33;
           flex-shrink:0;display:flex;align-items:center;justify-content:center;
           font-family:var(--mono);font-size:11px;font-weight:600;color:${meta.color}">${initials}</div>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:500;color:${isMe?'var(--acc)':'var(--tx)'}">${m.employee_name}${isMe?' <span style="font-family:var(--mono);font-size:9px;color:var(--acc)">(you)</span>':''}</div>
        ${splitLbl}${ts}
      </div>
      ${!m.split_label?`<div style="display:flex;align-items:center;gap:5px;padding:3px 9px;border-radius:20px;background:${meta.bg};border:1px solid ${meta.color}33;font-family:var(--mono);font-size:10px;color:${meta.color}">${svgI(meta.icon,11)} ${meta.label}</div>`:``}
    </div>`;
  }).join('');

  // Summary counts — split counts as WFO (compliance priority)
  const wfoCount = members.filter(m=>m.status==='wfo'||m.status==='split').length;
  const wfhCount = members.filter(m=>m.status==='wfh').length;
  const leaveCount = members.filter(m=>!['wfo','wfh','split','not_checked_in'].includes(m.status)).length;
  const pendingCount = members.filter(m=>m.status==='not_checked_in').length;

  el.innerHTML = `
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap">
      <span style="display:flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;background:var(--gbg);border:1px solid rgba(74,222,128,0.2);font-family:var(--mono);font-size:10px;color:var(--green)">
        ${svgI('building-2',11)} ${wfoCount} WFO
      </span>
      <span style="display:flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;background:var(--bbg);border:1px solid rgba(96,165,250,0.2);font-family:var(--mono);font-size:10px;color:var(--blue)">
        ${svgI('home',11)} ${wfhCount} WFH
      </span>
      ${leaveCount?`<span style="display:flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;background:var(--abg);border:1px solid rgba(251,191,36,0.2);font-family:var(--mono);font-size:10px;color:var(--amber)">
        ${svgI('plane',11)} ${leaveCount} Leave
      </span>`:''}
      ${pendingCount?`<span style="display:flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;background:var(--bg3);border:1px solid var(--b1);font-family:var(--mono);font-size:10px;color:var(--tx3)">
        ${svgI('clock',11)} ${pendingCount} pending
      </span>`:''}
    </div>
    <div style="display:flex;flex-direction:column;gap:6px">${cards}</div>`;
  lucide.createIcons();
}


async function loadTeamLeaveCalendar(){
  if(!MY_ID) return;
  const month = document.getElementById('team-leave-month')?.value || new Date().toISOString().slice(0,7);
  const el = document.getElementById('team-leave-content');
  if(!el) return;
  el.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;

  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';

  // Get my team via /api/me
  let myTeam = null;
  try {
    const meR = await fetch('/api/me', {headers:{'X-Employee-Id':MY_ID,'X-Device-Token':MY_TOKEN}});
    if(meR.ok){ const meData = await meR.json(); myTeam = meData?.team || null; }
  } catch(e) {}

  // Manager/admin sees all teams; employee sees own team only
  const teamParam = isMgr ? '' : (myTeam ? `&team=${encodeURIComponent(myTeam)}` : '');
  const titleEl = document.getElementById('team-leave-title');
  if(titleEl) titleEl.textContent = isMgr ? 'All Teams - Leave' : `${myTeam||'My Team'} - Leave`;

  const data = await get(`/api/team-leave?month=${month}${teamParam}`);
  const leaves = data?.leaves || [];

  if(!leaves.length){
    el.innerHTML = `<div style="color:var(--tx3);font-family:var(--mono);font-size:12px;padding:12px 0">No leave recorded${isMgr?'':` for ${myTeam||'your team'}`} this month.</div>`;
    return;
  }

  // Group by date
  const byDate = {};
  leaves.forEach(l => {
    if(!byDate[l.date]) byDate[l.date] = [];
    byDate[l.date].push(l);
  });

  const rows = Object.entries(byDate).sort(([a],[b])=>a.localeCompare(b)).map(([date, items])=>{
    const d = new Date(date+'T00:00:00');
    const dayName = d.toLocaleDateString('en-GB',{weekday:'short'});
    const dayNum  = d.toLocaleDateString('en-GB',{day:'2-digit',month:'short'});
    const pills = items.map(l => {
      const isMe = l.employee_id === MY_ID;
      const color = isMe ? 'var(--acc)' : 'var(--tx2)';
      const bg    = isMe ? 'var(--accg)' : 'var(--bg3)';
      const border= isMe ? 'rgba(193,123,63,0.3)' : 'var(--b1)';
      return `<span style="display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;
              background:${bg};border:1px solid ${border};font-family:var(--mono);font-size:11px;color:${color}">
        ${l.employee_name.split(' ')[0]}
        <span style="opacity:0.6;font-size:10px">${l.label}</span>
      </span>`;
    }).join('');
    return `<div style="display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--b0)">
      <div style="width:64px;flex-shrink:0;font-family:var(--mono);font-size:11px">
        <span style="color:var(--tx3)">${dayName}</span>
        <span style="color:var(--tx2);margin-left:4px">${dayNum}</span>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${pills}</div>
    </div>`;
  }).join('');

  el.innerHTML = `<div style="display:flex;flex-direction:column">${rows}</div>
    <div style="margin-top:10px;font-family:var(--mono);font-size:10px;color:var(--tx3)">
      ${leaves.length} leave record${leaves.length!==1?'s':''} - ${myTeam} only
      <span style="margin-left:8px;padding:2px 8px;border-radius:10px;background:var(--accg);color:var(--acc);border:1px solid rgba(193,123,63,0.2)">You</span> = highlighted
    </div>`;
  lucide.createIcons();
}

function buildLeaveApplyForm(){
  const el=document.getElementById('leave-apply-form');
  el.innerHTML=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div class="field"><label>From date</label><input type="date" id="my-leave-date-from" value="${TODAY}" onchange="updateMyLeaveDayPreview()"/></div><div class="field"><label>To date</label><input type="date" id="my-leave-date-to" value="${TODAY}" onchange="updateMyLeaveDayPreview()"/></div></div><div class="day-preview" id="my-day-preview">1 working day</div><div class="field"><label>Leave type</label><div class="leave-type-grid" id="my-leave-type-grid">${Object.entries(LEAVE_TYPES).map(([k,v])=>`<button class="leave-type-btn" id="ltype-${k}" onclick="selectLeaveType('${k}')" title="${v.label}"><i data-lucide="${k==='annual'?'plane':v.icon}"></i>${v.label}</button>`).join('')}</div></div><div class="field"><label id="my-leave-note-label">Note <span id="my-note-req" style="color:var(--red);display:none">*required for Other</span></label><textarea id="my-leave-note" placeholder="Optional note"></textarea></div><button class="btn btn-acc" style="width:100%" onclick="applySelfLeave()"><i data-lucide="calendar-plus"></i>Apply Leave</button><div id="my-leave-result" style="margin-top:10px"></div>`;
  reIcons();
}
let selectedLeaveType=null;
function selectLeaveType(type){
  selectedLeaveType=type;
  document.querySelectorAll('.leave-type-btn').forEach(b=>b.classList.remove('selected'));
  const btn=document.getElementById('ltype-'+type);if(btn)btn.classList.add('selected');
  const req=document.getElementById('my-note-req'),note=document.getElementById('my-leave-note');
  if(req)req.style.display=type==='other'?'inline':'none';
  if(note)note.placeholder=type==='other'?'Required: describe the reason':'Optional note';
}
async function applySelfLeave(){
  if(!selectedLeaveType){document.getElementById('my-leave-result').innerHTML=notice('warn','Select a leave type first.');reIcons();return;}
  const from=document.getElementById('my-leave-date-from')?.value;
  const to=document.getElementById('my-leave-date-to')?.value;
  const note=document.getElementById('my-leave-note')?.value?.trim()||null;
  if(selectedLeaveType==='other'&&!note){document.getElementById('my-leave-result').innerHTML=notice('warn','Note is required for "Other" leave type.');reIcons();return;}
  const days=getWorkingDays(from,to);
  if(!days.length){document.getElementById('my-leave-result').innerHTML=notice('warn','No working days in selected range.');reIcons();return;}
  let failed=0;
  for(const d of days){const res=await post('/api/leave',{employee_id:MY_ID,date:d,leave_type:selectedLeaveType,note:note||undefined,applied_by:MY_ID,source:'self'});if(!res.ok)failed++;}
  document.getElementById('my-leave-result').innerHTML=failed===0
    ?notice('ok',`Leave applied: ${LEAVE_TYPES[selectedLeaveType]?.label} for ${days.length} day${days.length!==1?'s':''} (${days[0]}${days.length>1?' -> '+days[days.length-1]:''})`)
    :notice('err',`${failed} day(s) failed.`);
  if(failed===0){selectedLeaveType=null;document.querySelectorAll('.leave-type-btn').forEach(b=>b.classList.remove('selected'));loadMyHistory();}
  reIcons();
}
async function loadMyHistory(){
  const month=document.getElementById('my-hist-month')?.value;
  if(!MY_ID||!month)return;
  const d=await get(`/api/history/${MY_ID}?month=${month}`);
  if(d)renderCalendar('my-cal-grid',d.records,month,d.public_holiday_dates||[]);
}

// -- HISTORY -------------------------------------------
async function initHistory(){
  // Preserve current selections before rebuilding dropdowns
  const sel     = document.getElementById('hist-emp');
  const monthSel= document.getElementById('hist-month');
  const prevEmp  = sel?.value || '';
  const prevMonth= monthSel?.value || '';

  const team=await get('/api/team');
  if(team?.team?.length){
    sel.innerHTML=team.team.map(m=>`<option value="${m.employee_id}">${m.employee_name} (${m.employee_id})</option>`).join('');
    // Restore previously selected employee — don't reset to first
    if(prevEmp && sel.querySelector(`option[value="${prevEmp}"]`)){
      sel.value = prevEmp;
    }
  } else {
    sel.innerHTML='<option value="">No employees available</option>';
  }
  monthOpts('hist-month');
  // Restore previously selected month
  if(prevMonth && monthSel.querySelector(`option[value="${prevMonth}"]`)){
    monthSel.value = prevMonth;
  }
  loadHistory();
}
async function loadHistory(){
  const empId=document.getElementById('hist-emp')?.value,month=document.getElementById('hist-month')?.value;
  if(!empId||!month)return;
  const d=await get(`/api/history/${empId}?month=${month}`);
  if(!d)return;
  renderCalendar('cal-grid',d.records,month,d.public_holiday_dates||[]);
  // Build sets for exclusion BEFORE counting
  const phDatesSet   = new Set(d.public_holiday_dates||[]);
  const leaveDtsSet  = new Set(d.personal_leave_dates||[]);

  // WFO: exclude public holidays and personal leave days from count
  // Exception: if employee comes in on a public holiday/leave, we still
  // count WFO (and alert manager separately) — handled in WFO-on-leave logic below
  const wfo=d.records.filter(r=>{
    // Never count WFO on a public holiday toward the WFO tally
    if(phDatesSet.has(r.date)) return false;
    if(r.status==='wfo') return true;
    if(r.display_status==='split'&&r.segments)
      return r.segments.some(s=>s.status==='wfo');
    return false;
  }).length;
  const wfh=d.records.filter(r=>{
    // Never count WFH on a public holiday or personal leave day
    if(phDatesSet.has(r.date)) return false;
    if(leaveDtsSet.has(r.date)) return false;
    if(r.display_status==='split'&&r.segments)
      return !r.segments.some(s=>s.status==='wfo')&&r.segments.some(s=>s.status==='wfh');
    return r.status==='wfh';
  }).length;
  const leave=d.records.filter(r=>{
    // Don't double-count: leave on a public holiday = holiday, not personal leave
    if(phDatesSet.has(r.date)) return false;
    return r.status&&LEAVE_TYPES[r.status];
  }).length;
  const working=wfo+wfh,rto=working?Math.round((wfo/working)*100):0;
  const[yr,mo]=month.split('-').map(Number),daysInMonth=new Date(yr,mo,0).getDate();
  const[ty2,tm2,td2]=TODAY.split('-').map(Number);
  // Public + personal leave dates to exclude from working days denominator
  let workDays=0, totalWorkDays=0, phCount=0;
  for(let day=1;day<=daysInMonth;day++){
    const dt=new Date(yr,mo-1,day);
    if(dt.getDay()===0||dt.getDay()===6)continue;
    const ds=`${month}-${String(day).padStart(2,'0')}`;
    // Exclude public holidays from working days for everyone
    if(phDatesSet.has(ds)){phCount++;continue;}
    totalWorkDays++;
    const isFuture=(yr===ty2&&mo===tm2&&day>td2)||(yr===ty2&&mo>tm2)||(yr>ty2);
    if(!isFuture)workDays++;
  }
  const phNote=phCount>0?` - ${phCount} public holiday${phCount>1?'s':''} excluded`:'';
  document.getElementById('hist-summary').innerHTML=`<div style="display:flex;flex-direction:column;gap:10px;margin-top:4px">
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--gbg);border-radius:7px"><span style="font-size:12px;color:var(--tx2)">${svgI('building')} Days in office</span><span style="font-family:var(--mono);font-size:20px;color:var(--green)">${wfo}</span></div>
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--bbg);border-radius:7px"><span style="font-size:12px;color:var(--tx2)">${svgI('home')} WFH days</span><span style="font-family:var(--mono);font-size:20px;color:var(--blue)">${wfh}</span></div>
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--pbg);border-radius:7px"><span style="font-size:12px;color:var(--tx2)">${svgI('calendar-days')} Leave days</span><span style="font-family:var(--mono);font-size:20px;color:var(--purple)">${leave}</span></div>
    ${phCount>0?`<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--abg);border-radius:7px;border:1px solid rgba(251,191,36,0.15)"><span style="font-size:12px;color:var(--tx2)">${svgI('sparkles')} Public holidays</span><span style="font-family:var(--mono);font-size:20px;color:var(--amber)">${phCount}</span></div>`:''}
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--bg2);border-radius:7px;border:1px solid var(--b0)"><span style="font-size:12px;color:var(--tx2)">${svgI('bar-chart-3')} RTO rate</span><div style="text-align:right"><span style="font-family:var(--mono);font-size:20px;color:var(--accl)">${rto}%</span><div style="font-family:var(--mono);font-size:9px;color:var(--tx3);margin-top:2px">${wfo+wfh+leave} of ${workDays} elapsed - ${totalWorkDays} total${phNote}</div></div></div>
  </div>`;
  reIcons();
}

// -- CALENDAR (Mon-Fri only) ---------------------------
function renderCalendar(gridId,records,month,phDates=[]){
  const calEl=document.getElementById(gridId);if(!calEl)return;
  const[yr,mo]=month.split('-').map(Number),daysInMonth=new Date(yr,mo,0).getDate();
  const statusMap={};records.forEach(r=>{
    statusMap[r.date]=r.display_status||r.status;
    // Store split label for tooltip
    if(r.split_label) statusMap[r.date+'_split']=r.split_label;
    // Store note for leave days (shown in tooltip)
    if(r.leaves&&r.leaves.length&&r.leaves[0].note)
      statusMap[r.date+'_note']=r.leaves[0].note;
  });
  const phSet=new Set(phDates);
  // Public holiday always wins — overrides any WFH/WFO check-in that fired
  // that day (agent may have run before the holiday was added, or at midnight).
  // Exception: if employee actually came in (wfo), keep wfo so it's visible.
  phSet.forEach(ds=>{
    const existing = statusMap[ds];
    if(!existing || (existing !== 'wfo' && existing !== 'split')){
      statusMap[ds] = 'public_holiday';
    }
  });
  calEl.style.gridTemplateColumns='repeat(5,1fr)';
  const cells=[];
  cells.push(...['Mon','Tue','Wed','Thu','Fri'].map(l=>`<div class="cal-day-lbl">${l}</div>`));
  const firstDow=new Date(yr,mo-1,1).getDay();
  const monOff=firstDow===0?4:firstDow===6?0:firstDow-1;
  for(let i=0;i<monOff;i++)cells.push(`<div class="cal-cell empty" style="opacity:0.2"></div>`);
  // Parse TODAY as local date components to avoid UTC timezone shift
  const [ty,tm,td]=TODAY.split('-').map(Number);
  for(let day=1;day<=daysInMonth;day++){
    const dt=new Date(yr,mo-1,day),dow=dt.getDay();
    if(dow===0||dow===6)continue;
    const ds=`${month}-${String(day).padStart(2,'0')}`;
    const s=statusMap[ds]||'';
    // Compare as local dates (yr/mo/day) not Date objects to avoid tz shift
    const isToday=(yr===ty&&mo===tm&&day===td);
    const isPast=(yr<ty)||(yr===ty&&mo<tm)||(yr===ty&&mo===tm&&day<td);
    const cls=['cal-cell',s||(isPast?'empty':'future'),isToday?'today':''].filter(Boolean).join(' ');
    const noteStr=statusMap[ds+'_note']?` - ${statusMap[ds+'_note']}`:'';
    const splitStr=statusMap[ds+'_split'] ? splitLabelForDisplay(statusMap[ds+'_split']) : '';
    const tooltipLabel=s==='split'&&splitStr?` - ${splitStr}`:s?`: ${s.toUpperCase()}`:'';
    cells.push(`<div class="${cls}" title="${ds}${tooltipLabel}${noteStr}">${day}</div>`);
  }
  calEl.innerHTML=cells.join('');
}

// -- COMPLIANCE ----------------------------------------
async function loadCompliance(){
  // Don't reset month dropdown if already populated — preserves selected value
  const monthSel = document.getElementById('compliance-month');
  if(!monthSel?.options.length) monthOpts('compliance-month');
  const month = monthSel?.value; if(!month) return;

  // Setup team filter for manager/admin
  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';
  const compFilter = document.getElementById('comp-team-filter');
  let selectedTeam = '';
  if(isMgr && compFilter){
    compFilter.style.display='block';
    const teams = MY_MANAGED_TEAMS || await _getAllTeams();
    // Rebuild if empty or team list has changed
    const prevSelected = compFilter.value;
    compFilter.innerHTML = teamFilterOpts(teams, prevSelected);
    selectedTeam = compFilter.value || '';
  } else if(MY_ROLE==='employee' && MY_TEAM){
    // Employees are scoped to their own team only
    selectedTeam = MY_TEAM;
  }
  // Build URL: team filter takes priority; server also enforces managed_teams via header
  const url = selectedTeam
    ? `/api/compliance?month=${month}&team=${encodeURIComponent(selectedTeam)}`
    : `/api/compliance?month=${month}`;
  const d = await get(url);
  const tbody=document.getElementById('compliance-tbody');
  if(!d?.team?.length){tbody.innerHTML='<tr><td colspan="8" style="text-align:center;padding:24px;color:var(--tx3);font-family:var(--mono);font-size:11px">No data.</td></tr>';return;}
  tbody.innerHTML=d.team.map(r=>{
    // Build weekly tooltip
    const weekTip=(r.weekly||[]).map(w=>{
      if(w.no_data && w.partial_week) return `WK ${w.week_start.slice(5)}: Partial week (grace period)`;
      if(w.no_data) return `WK ${w.week_start.slice(5)}: No data (grace period)`;
      return `WK ${w.week_start.slice(5)}: ${w.wfo}/${w.target} WFO ${w.passed?'Pass':'Miss'}${w.leave?` (${w.leave}d leave)`:''}`;
    }).join('&#10;');
    // For weeks display: only count weeks that had actual data
    const realWeeks=r.weekly?r.weekly.filter(w=>!w.no_data):[];
    const realPassed=realWeeks.filter(w=>w.passed).length;
    const ragColor=r.rag==='green'?'var(--green)':r.rag==='amber'?'var(--amber)':r.rag==='orange'?'#f97316':r.rag==='grey'?'var(--tx3)':'var(--red)';
    const wfoColor=r.rag==='grey'?'var(--tx3)':r.wfo>=12?'var(--green)':'var(--tx2)';
    return`<tr>
      <td><div class="rag ${r.rag}" title="${r.status||''}"></div></td>
      <td><div style="font-size:12px;font-weight:500">${r.employee_name}</div><div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${r.employee_id}</div></td>
      <td style="color:var(--tx2);font-size:12px">${r.team||'-'}</td>
      <td><span style="font-family:var(--mono);font-size:13px;font-weight:600;color:${wfoColor}">${r.wfo}</span><span style="font-family:var(--mono);font-size:10px;color:var(--tx3)">/12</span></td>
      <td style="color:var(--blue);font-family:var(--mono)">${r.wfh}</td>
      <td style="color:var(--purple);font-family:var(--mono)">${r.leave}</td>
      <td><span style="font-family:var(--mono);font-size:11px;color:${ragColor}">${r.status||'-'}</span></td>
      <td title="${weekTip}">${r.rag==='grey'?`<span style="font-family:var(--mono);font-size:12px;color:var(--tx3)">-</span>`:`<span style="font-family:var(--mono);font-size:12px;color:${realWeeks.length===0?'var(--tx3)':realPassed===realWeeks.length?'var(--green)':realPassed>0?'var(--amber)':'var(--red)'}">${realWeeks.length===0?'-':realPassed+'/'+realWeeks.length}</span><span style="font-family:var(--mono);font-size:9px;color:var(--tx3);margin-left:4px">${realWeeks.length===0?'no data yet':'weeks'}</span>`}</td>
    </tr>`;
  }).join('');
  reIcons();
}
async function exportCSV(){
  // Use compliance month picker if available, otherwise current month
  const month = document.getElementById('compliance-month')?.value
             || document.getElementById('my-hist-month')?.value
             || new Date().toISOString().slice(0,7);
  const r = await fetch(`${API}/api/export?month=${month}`, {
    headers:{'X-Employee-Id':MY_ID,'X-Device-Token':MY_TOKEN}
  });
  if(!r.ok) return;
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `rto-attendance-${month}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// -- LEAVE MANAGEMENT ----------------------------------
async function initLeaveManagement(){
  const sel=document.getElementById('lm-emp');
  const prevLmEmp = sel?.value || '';
  const team=await get('/api/team');
  if(sel&&team?.team?.length){
    sel.innerHTML=team.team.map(m=>`<option value="${m.employee_id}">${m.employee_name} (${m.employee_id})</option>`).join('');
    // Restore previously selected employee
    if(prevLmEmp && sel.querySelector(`option[value="${prevLmEmp}"]`)){
      sel.value = prevLmEmp;
    }
  }
  const grid=document.getElementById('lm-type-grid');
  if(grid)grid.innerHTML=Object.entries(LEAVE_TYPES).map(([k,v])=>`<button class="leave-type-btn" id="mgrltype-${k}" onclick="selectMgrLeaveType('${k}')" title="${v.label}"><i data-lucide="${v.icon}"></i>${v.label}</button>`).join('');
  monthOpts('lm-cal-month');
  const fromEl=document.getElementById('lm-date-from'),toEl=document.getElementById('lm-date-to');
  // Default to next Monday if today is weekend
  const defaultDate=(()=>{
    const[y,m,d]=TODAY.split('-').map(Number);
    const dow=new Date(y,m-1,d).getDay();
    if(dow===6){const nd=new Date(y,m-1,d+2);return `${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,'0')}-${String(nd.getDate()).padStart(2,'0')}`;}
    if(dow===0){const nd=new Date(y,m-1,d+1);return `${nd.getFullYear()}-${String(nd.getMonth()+1).padStart(2,'0')}-${String(nd.getDate()).padStart(2,'0')}`;}
    return TODAY;
  })();
  if(fromEl&&!fromEl.value)fromEl.value=defaultDate;
  if(toEl&&!toEl.value)toEl.value=defaultDate;
  // Populate team filter for calendar (managers/admins only)
  const calTeamSel = document.getElementById('lm-cal-team-filter');
  if(calTeamSel && (MY_ROLE==='manager'||MY_ROLE==='admin')){
    calTeamSel.style.display='inline-block';
    const teams = MY_MANAGED_TEAMS || await _getAllTeams();
    const prev = calTeamSel.value || '';
    calTeamSel.innerHTML='<option value="">All teams</option>'+teams.map(t=>`<option value="${t}"${t===prev?' selected':''}>${t}</option>`).join('');
  }
  updateLeaveDayPreview();
  loadTeamLeave();
  reIcons();
}
let selectedMgrLeaveType=null;
function selectMgrLeaveType(type){
  selectedMgrLeaveType=type;
  document.querySelectorAll('[id^="mgrltype-"]').forEach(b=>b.classList.remove('selected'));
  const btn=document.getElementById('mgrltype-'+type);if(btn)btn.classList.add('selected');
  const lbl=document.getElementById('lm-note-label'),note=document.getElementById('lm-note');
  if(lbl)lbl.innerHTML=type==='other'?'Note <span style="color:var(--red)">*required</span>':'Note (optional)';
  if(note)note.placeholder=type==='other'?'Required: describe the reason':'e.g. Sick leave - confirmed via message';
}
async function managerApplyLeave(){
  if(!selectedMgrLeaveType){document.getElementById('lm-result').innerHTML=notice('warn','Select a leave type first.');reIcons();return;}
  const empId=document.getElementById('lm-emp')?.value;
  const from=document.getElementById('lm-date-from')?.value,to=document.getElementById('lm-date-to')?.value;
  const note=document.getElementById('lm-note')?.value?.trim()||null;
  if(selectedMgrLeaveType==='other'&&!note){document.getElementById('lm-result').innerHTML=notice('warn','Note is required for "Other" leave type.');reIcons();return;}
  const days=getWorkingDays(from,to);
  if(!days.length){document.getElementById('lm-result').innerHTML=notice('warn','No working days in selected range.');reIcons();return;}
  let failed=0;
  for(const d of days){const res=await post('/api/leave',{employee_id:empId,date:d,leave_type:selectedMgrLeaveType,note:note||undefined,applied_by:MY_ID,source:'manager'});if(!res.ok)failed++;}
  const lbl=LEAVE_TYPES[selectedMgrLeaveType]?.label;
  document.getElementById('lm-result').innerHTML=failed===0
    ?notice('ok',`${lbl} applied for ${empId}: ${days.length} day${days.length!==1?'s':''} (${days[0]}${days.length>1?' -> '+days[days.length-1]:''})`)
    :notice('err',`${failed} day(s) failed.`);
  if(failed===0){selectedMgrLeaveType=null;document.querySelectorAll('[id^="mgrltype-"]').forEach(b=>b.classList.remove('selected'));loadTeamLeave();}
  reIcons();
}
async function loadTeamLeave(){
  const monthEl=document.getElementById('lm-cal-month')||document.getElementById('team-leave-month');
  const month=monthEl?.value;if(!month)return;
  let teamFilter='';
  if(MY_ROLE==='employee'&&MY_TEAM){
    // Employees scoped to own team only — no filter UI shown to them
    teamFilter=`&team=${encodeURIComponent(MY_TEAM)}`;
  } else {
    // Managers/admins: use the cal filter dropdown
    const calSel=document.getElementById('lm-cal-team-filter');
    const t=calSel?.value||'';
    if(t) teamFilter=`&team=${encodeURIComponent(t)}`;
  }
  const d=await get(`/api/team-leave?month=${month}${teamFilter}`);
  const el=document.getElementById('team-leave-list');
  if(!d?.leaves?.length){el.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:8px 0">No leave records this month.</div>';return;}
  const byDate={};d.leaves.forEach(l=>{if(!byDate[l.date])byDate[l.date]=[];byDate[l.date].push(l);});
  el.innerHTML=Object.entries(byDate).map(([date,leaves])=>`
    <div style="display:flex;gap:10px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--b0)">
      <div style="font-family:var(--mono);font-size:11px;color:var(--tx3);min-width:80px;padding-top:2px">${date}</div>
      <div style="display:flex;flex-direction:column;gap:4px;flex:1">
        ${leaves.map(l=>`
          <div style="display:flex;flex-direction:column;gap:2px">
            <div class="leave-tag" title="${l.note?l.note:l.employee_name}">
              ${svgI(LEAVE_TYPES[l.leave_type]?.icon||'calendar')}
              ${l.employee_name.split(' ')[0]} - ${l.label}${l.half_day_period?` (${l.half_day_period.toUpperCase()})`:''}</div>
            ${l.note?`<div style="font-family:var(--mono);font-size:10px;color:var(--tx3);padding-left:4px;display:flex;align-items:center;gap:4px">${svgI('message-square',10)} ${l.note}</div>`:''}
          </div>`).join('')}
      </div>
    </div>`).join('');
  reIcons();
}

// -- ANOMALIES -----------------------------------------
async function loadAnomalies(){
  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';
  const tp = isMgr ? getTeamParam(DASH_TEAM_FILTER) : getTeamParam('');
  const today=await get('/api/today'+tp.replace('&','?'));
  const flagged=today?.checkins?.filter(c=>c.flagged)||[];
  document.getElementById('flag-count').textContent=`${flagged.length} records`;
  document.getElementById('flagged-list').innerHTML=flagged.length
    // Anomalies is manager/admin-only (see buildNav — the nav item itself
    // only renders for isMgr), so unlike the general Today/history views,
    // this one deliberately shows the real, unredacted flag_reason — the
    // whole point of this panel is investigating exactly what was flagged.
    ?flagged.map(c=>`<div style="display:flex;align-items:center;gap:12px;padding:12px 14px;background:var(--bg2);border:1px solid var(--b0);border-left:3px solid var(--red);border-radius:7px;margin-bottom:8px"><div style="width:30px;height:30px;border-radius:50%;background:${colorFor(c.employee_id)};display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff">${iniOf(c.employee_name)}</div><div style="flex:1"><div style="font-size:12px;font-weight:500">${c.employee_name} <span style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${c.employee_id}</span></div><div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${c.flag_reason||'Flagged record'}</div></div><span style="font-family:var(--mono);font-size:11px;color:var(--accl);cursor:pointer;text-decoration:underline" onclick="nav('override')">Override →</span></div>`).join('')
    :'<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:8px 0">No flagged records today.</div>';

  // The real audit trail — everything anomaly-worthy the server has ever
  // logged (signal fabrication, unverified WFO claims, token enumeration,
  // WFO-while-on-leave, mismatched missed-day claims...), not just today's
  // flagged check-ins. This used to only exist in the raw admin table
  // browser; wiring it in here so managers/admins actually see it without
  // digging through /admin.
  const anomalies = await get('/api/anomalies'+tp.replace('&','?')) || {anomalies:[]};
  const list = anomalies.anomalies || [];
  document.getElementById('anomaly-count').textContent = `${list.length} active`;
  document.getElementById('anomaly-list').innerHTML = list.length
    ? list.map(a=>{
        const sevColor = a.severity==='high' ? 'var(--red)' : a.severity==='medium' ? 'var(--amber)' : 'var(--tx3)';
        const sevBg    = a.severity==='high' ? 'var(--rbg)' : a.severity==='medium' ? 'rgba(193,123,63,0.12)' : 'var(--bg2)';
        const typeLabel = (a.type||'').replace(/_/g,' ').replace(/\b\w/g, ch=>ch.toUpperCase());
        return `<div style="display:flex;align-items:flex-start;gap:12px;padding:12px 14px;background:var(--bg2);border:1px solid var(--b0);border-left:3px solid ${sevColor};border-radius:7px;margin-bottom:8px">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <span style="font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:10px;background:${sevBg};color:${sevColor}">${(a.severity||'').toUpperCase()}</span>
              <span style="font-size:12px;font-weight:500">${typeLabel}</span>
              ${a.employee_name?`<span style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${a.employee_name}${a.employee_id&&a.employee_id!=='unknown'?' · '+a.employee_id:''}</span>`:''}
            </div>
            <div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${a.description||''}</div>
            <div style="font-family:var(--mono);font-size:9px;color:var(--tx3);margin-top:4px">${fmt(a.detected_at)}</div>
          </div>
          <span style="font-family:var(--mono);font-size:11px;color:var(--accl);cursor:pointer;text-decoration:underline;white-space:nowrap" onclick="resolveAnomaly(${a.id})">Resolve</span>
        </div>`;
      }).join('')
    : '<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:8px 0">No active anomalies.</div>';

  reIcons();
}

async function resolveAnomaly(id){
  const r = await patch(`/api/anomalies/${id}/resolve`);
  if(r.ok) loadAnomalies();
}

// -- OVERRIDE ------------------------------------------
async function doOverride(){
  const body={employee_id:document.getElementById('ov-id').value.trim(),date:document.getElementById('ov-date').value.trim(),new_status:document.getElementById('ov-status').value,override_by:document.getElementById('ov-by').value.trim()||MY_ID,note:document.getElementById('ov-note').value.trim()||null};
  if(!body.employee_id||!body.date){document.getElementById('ov-result').innerHTML=notice('err','Fill in employee ID and date.');reIcons();return;}
  if(!body.note){document.getElementById('ov-result').innerHTML=notice('warn','Please provide a reason for the override.');reIcons();return;}
  const res=await post('/api/override',body);
  const msg=res.ok
    ?`Override applied: ${body.employee_id} on ${body.date} -> ${body.new_status.toUpperCase()}${body.note?' - Reason: '+body.note:''}`
    :JSON.stringify(res.data);
  document.getElementById('ov-result').innerHTML=res.ok?notice('ok',msg):notice('err',msg);
  reIcons();
}

// -- TEAM ----------------------------------------------
async function loadTeam(){
  const [d, teamsD] = await Promise.all([get('/api/team'), get('/api/teams')]);
  if(!d?.team?.length){
    document.getElementById('team-groups').innerHTML='<div class="notice info"><i data-lucide="info"></i><span>No devices registered yet.</span></div>';
    reIcons(); return;
  }

  // Populate filter dropdown
  const filterSel = document.getElementById('team-filter');
  const currentFilter = filterSel?.value || '';
  if(teamsD?.teams?.length && filterSel){
    const opts = '<option value="">All teams</option>' +
      teamsD.teams.map(t=>`<option value="${t.name}"${t.name===currentFilter?' selected':''}>${t.name}</option>`).join('');
    filterSel.innerHTML = opts;
  }

  // Filter members
  const members = currentFilter
    ? d.team.filter(m => m.team === currentFilter)
    : d.team;

  // Group by team
  const groups = {};
  members.forEach(m => {
    const t = m.team || 'Unassigned';
    if(!groups[t]) groups[t] = [];
    groups[t].push(m);
  });

  const isMgr = MY_ROLE === 'manager' || MY_ROLE === 'admin';

  document.getElementById('team-groups').innerHTML = Object.entries(groups).map(([team, members]) => `
    <div class="card" style="margin-bottom:0">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
        <div style="font-family:var(--serif);font-size:14px">${team}</div>
        <span style="font-family:var(--mono);font-size:10px;color:var(--tx3);background:var(--bg2);padding:2px 8px;border-radius:10px">${members.length} member${members.length!==1?'s':''}</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">
        ${members.map(m => `
          <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--bg2);border:1px solid var(--b0);border-radius:8px">
            <div style="width:32px;height:32px;border-radius:50%;background:${colorFor(m.employee_id)};display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:#fff;flex-shrink:0">${iniOf(m.employee_name)}</div>
            <div style="flex:1;min-width:0">
              <div style="font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${m.employee_name}</div>
              <div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${m.employee_id}</div>
              <div style="display:flex;gap:5px;margin-top:3px;align-items:center">
                <span class="role-pill ${m.role||'employee'}" style="font-size:9px">${(m.role||'employee').charAt(0).toUpperCase()+(m.role||'employee').slice(1)}</span>
                <span style="font-family:var(--mono);font-size:9px;color:var(--tx3)">${m.platform||''}</span>
              </div>
            </div>
          </div>`).join('')}
      </div>
    </div>`).join('<div style="height:12px"></div>');

  // Team management section (admin/manager)
  const mgmtEl = document.getElementById('team-mgmt-section');
  if(isMgr && mgmtEl){
    mgmtEl.innerHTML = `
      <div class="card">
        <div class="card-title">Manage teams <span class="card-sub">Add or remove team names</span></div>
        <div style="display:flex;gap:8px;margin-bottom:14px">
          <input type="text" id="new-team-name" placeholder="Enter team name" style="flex:1"/>
          <button class="btn btn-acc" onclick="addTeam()"><i data-lucide="plus"></i>Add team</button>
        </div>
        <div id="teams-config-list"></div>
      </div>`;
    loadTeamsConfig();
  } else if(mgmtEl){
    mgmtEl.innerHTML = '';
  }

  reIcons();
}

async function addTeam(){
  const inp = document.getElementById('new-team-name');
  const name = inp?.value?.trim();
  if(!name){ inp?.focus(); return; }
  const res = await post('/api/teams', {name, created_by: MY_ID});
  if(res.ok){
    inp.value = '';
    await loadTeam();
  } else {
    alert('Failed to add team');
  }
}

async function loadTeamsConfig(){
  const d = await get('/api/teams');
  const el = document.getElementById('teams-config-list');
  if(!el) return;
  if(!d?.teams?.length){
    el.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:6px 0">No teams yet, add one above.</div>';
    return;
  }
  el.innerHTML = d.teams.map(t=>`
    <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--bg2);border:1px solid var(--b0);border-radius:7px;margin-bottom:6px">
      <span style="font-size:12px">${t.name}</span>
      <button class="btn btn-danger btn-sm" onclick="deleteTeam(${t.id},'${t.name.replace(/'/g,'')}')">${svgI('x',11)}</button>
    </div>`).join('');
  reIcons();
}

async function deleteTeam(id, name){
  if(!confirm(`Remove team "${name||id}"?`)) return;
  await del('/api/teams/'+id);
  await loadTeam();
}

// -- ROLES ---------------------------------------------
async function loadRoles(){
  const[team,roles]=await Promise.all([get('/api/team'),get('/api/roles')]);
  const sel=document.getElementById('role-emp-sel');
  if(sel&&team?.team)sel.innerHTML=team.team.map(m=>`<option value="${m.employee_id}">${m.employee_name} (${m.employee_id})</option>`).join('');
  const el=document.getElementById('roles-list');
  if(!roles?.roles?.length){el.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px">No role assignments yet.</div>';return;}
  el.innerHTML=roles.roles.map(r=>`<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:var(--bg2);border:1px solid var(--b0);border-radius:7px;margin-bottom:6px"><div><div style="font-size:12px;font-weight:500">${(team?.team?.find(m=>m.employee_id===r.employee_id)?.employee_name)||r.employee_id}</div><div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${r.employee_id} - by ${r.assigned_by||'system'}</div></div><div style="display:flex;align-items:center;gap:8px"><span class="role-pill ${r.role}">${r.role.charAt(0).toUpperCase()+r.role.slice(1)}</span>${r.employee_id!==MY_ID?`<button class="btn btn-danger btn-sm" onclick="removeRole('${r.employee_id}')">${svgI('x',11)}</button>`:''}</div></div>`).join('');
  reIcons();
}
async function assignRole(){
  const empId=document.getElementById('role-emp-sel')?.value,role=document.getElementById('role-sel')?.value;
  const res=await post('/api/roles',{employee_id:empId,role,assigned_by:MY_ID});
  document.getElementById('role-result').innerHTML=res.ok?notice('ok',`Role updated: ${empId} \u2192 ${role}`):notice('err','Failed to assign role.');
  if(res.ok){loadRoles();loadSidebarUser();}reIcons();
}
async function removeRole(empId){await del(`/api/roles/${empId}`);loadRoles();}

// -- CONFIG + HOLIDAYS ---------------------------------
async function loadConfig(){
  const sel=document.getElementById('ph-year');
  if(sel&&!sel.options.length){const yr=new Date().getFullYear();sel.innerHTML=[yr,yr+1].map(y=>`<option value="${y}"${y===yr?' selected':''}>${y}</option>`).join('');}
  const dateEl=document.getElementById('ph-date');if(dateEl&&!dateEl.value)dateEl.value=TODAY;
  loadHolidays();
  const h=await get('/health');
  document.getElementById('server-info').innerHTML=h?`<div style="display:flex;flex-direction:column;gap:8px"><div style="display:flex;justify-content:space-between;padding:10px 12px;background:var(--gbg);border-radius:7px"><span style="font-size:12px;color:var(--tx2)">Status</span><span style="font-family:var(--mono);font-size:11px;color:var(--green)";display:inline-flex;align-items:center;gap:5px><span style="width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block"></span> Online</span></div><div style="display:flex;justify-content:space-between;padding:10px 12px;background:var(--bg2);border-radius:7px;border:1px solid var(--b0)"><span style="font-size:12px;color:var(--tx2)">Version</span><span style="font-family:var(--mono);font-size:11px;color:var(--accl)">${h.version||'2.0'}</span></div><div style="display:flex;justify-content:space-between;padding:10px 12px;background:var(--bg2);border-radius:7px;border:1px solid var(--b0)"><span style="font-size:12px;color:var(--tx2)">Port</span><span style="font-family:var(--mono);font-size:11px;color:var(--accl)">${h.port||9999}</span></div><div style="display:flex;justify-content:space-between;padding:10px 12px;background:var(--bg2);border-radius:7px;border:1px solid var(--b0)"><span style="font-size:12px;color:var(--tx2)">Timezone</span><span style="font-family:var(--mono);font-size:11px;color:var(--accl)">Asia/Kolkata (IST)</span></div></div>`:notice('err','Server unreachable');
  // Sync display-preference toggles to current server settings
  const s = await get('/api/settings');
  if(s){ APP_SETTINGS = { ...APP_SETTINGS, ...s }; }
  _syncSettingToggles();
  reIcons();
}
function _syncSettingToggles(){
  for(const key of Object.keys(APP_SETTINGS)){
    const btn = document.getElementById(`setting-${key}`);
    if(btn) btn.className = `toggle-pill${APP_SETTINGS[key] ? ' on' : ''}`;
  }
}
async function toggleAppSetting(key){
  const prev = APP_SETTINGS[key];
  APP_SETTINGS[key] = !prev;
  _syncSettingToggles();
  const res = await put('/api/settings', APP_SETTINGS);
  if(!res.ok){
    APP_SETTINGS[key] = prev;
    _syncSettingToggles();
  }
}
async function loadHolidays(){
  const year=document.getElementById('ph-year')?.value||new Date().getFullYear();
  const d=await get(`/api/holidays?year=${year}`);
  const el=document.getElementById('holidays-list');
  if(!d?.holidays?.length){el.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:8px 0">No holidays configured.</div>';return;}
  el.innerHTML=d.holidays.map(h=>`<div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:var(--bg2);border:1px solid var(--b0);border-radius:7px;margin-bottom:6px"><div><div style="font-size:12px;font-weight:500">${h.name}${h.optional?' <span style="font-family:var(--mono);font-size:9px;color:var(--amber);background:var(--abg);padding:1px 5px;border-radius:4px">Optional</span>':''}</div><div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${h.date} - ${h.country}</div></div><button class="btn btn-danger btn-sm" onclick="deleteHoliday('${h.date}')">${svgI('x',11)}</button></div>`).join('');
  reIcons();
}
async function addHoliday(){
  const d=document.getElementById('ph-date')?.value,name=document.getElementById('ph-name')?.value?.trim(),opt=document.getElementById('ph-optional')?.value==='true';
  if(!d||!name){document.getElementById('ph-result').innerHTML=notice('warn','Date and name required.');reIcons();return;}
  const res=await post('/api/holidays',{date:d,name,optional:opt});
  document.getElementById('ph-result').innerHTML=res.ok?notice('ok',`Added: ${name} on ${d}${!opt?' - applied to all employees':''}`)
    :notice('err','Failed to add holiday.');
  if(res.ok)loadHolidays();reIcons();
}
async function deleteHoliday(date){await del(`/api/holidays/${date}`);loadHolidays();}

// -- SERVER DOT ----------------------------------------
async function checkServer(){const h=await get('/health');document.getElementById('s-dot').className='s-dot '+(h?'ok':'err');}
function refreshAll(){
  // Warn if user has unsaved leave form input
  const hasInput=[...document.querySelectorAll(
    '.page.active textarea, .page.active input[type="text"]'
  )].some(el=>el.value.trim().length>0);
  if(hasInput&&!confirm('Refresh will clear your unsaved input. Continue?'))return;
  const a=document.querySelector('.page.active')?.id?.replace('page-','');
  if(a)nav(a);
}

// -- INIT ----------------------------------------------
const FALLBACK_ICONS={
  'bar-chart-3':'<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  'building':'<rect width="16" height="20" x="4" y="2" rx="2"/><path d="M9 22v-4h6v4"/><path d="M8 6h.01"/><path d="M16 6h.01"/><path d="M12 6h.01"/><path d="M12 10h.01"/><path d="M12 14h.01"/><path d="M16 10h.01"/><path d="M16 14h.01"/><path d="M8 10h.01"/><path d="M8 14h.01"/>',
  'building-2':'<path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18"/><path d="M6 12H4a2 2 0 0 0-2 2v8"/><path d="M18 9h2a2 2 0 0 1 2 2v11"/><path d="M10 6h4"/><path d="M10 10h4"/><path d="M10 14h4"/><path d="M10 18h4"/>',
  'calendar':'<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/>',
  'calendar-days':'<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/><path d="M8 14h.01"/><path d="M12 14h.01"/><path d="M16 14h.01"/><path d="M8 18h.01"/><path d="M12 18h.01"/>',
  'calendar-plus':'<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/><path d="M10 16h4"/><path d="M12 14v4"/>',
  'check-circle':'<path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="10"/>',
  'download':'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/>',
  'file-text':'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>',
  'fingerprint':'<path d="M2 12C2 6.5 6.5 2 12 2a10 10 0 0 1 10 10"/><path d="M5 19.5C5.7 18 6 16 6 12a6 6 0 0 1 12 0c0 1.6-.2 3.2-.7 4.8"/><path d="M8 22c1-2 1.5-5 1.5-10a2.5 2.5 0 0 1 5 0c0 5-.5 8-1.5 10"/><path d="M12 12c0 4-.4 7-1.2 9"/>',
  'git-branch':'<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
  'history':'<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  'home':'<path d="M3 10.5 12 3l9 7.5"/><path d="M5 10v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V10"/><path d="M9 21v-6h6v6"/>',
  'home':'<path d="M3 10.5 12 3l9 7.5"/><path d="M5 10v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V10"/><path d="M9 21v-6h6v6"/>',
  'info':'<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
  'layout-dashboard':'<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
  'monitor-check':'<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/><path d="m9 10 2 2 4-4"/>',
  'palm-tree':'<path d="M12 13v9"/><path d="M12 13c-2.2-1.7-5.4-1.6-8 1 1-4 4-6 8-4"/><path d="M12 13c2.2-1.7 5.4-1.6 8 1-1-4-4-6-8-4"/><path d="M12 10c-1-3-3.5-5-7-5 2.5-2 6-1.5 7 1"/><path d="M12 10c1-3 3.5-5 7-5-2.5-2-6-1.5-7 1"/>',
  'plus':'<path d="M5 12h14"/><path d="M12 5v14"/>',
  'refresh-cw':'<path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/>',
  'scan-search':'<path d="M7 3H5a2 2 0 0 0-2 2v2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M17 21h2a2 2 0 0 0 2-2v-2"/><circle cx="11" cy="11" r="3"/><path d="m16 16-2.2-2.2"/>',
  'settings-2':'<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
  'shield-check':'<path d="M20 13c0 5-3.5 7.5-8 9-4.5-1.5-8-4-8-9V5l8-3 8 3z"/><path d="m9 12 2 2 4-4"/>',
  'sparkles':'<path d="m12 3-1.8 5.2L5 10l5.2 1.8L12 17l1.8-5.2L19 10l-5.2-1.8z"/><path d="M5 3v4"/><path d="M3 5h4"/><path d="M19 17v4"/><path d="M17 19h4"/>',
  'square-pen':'<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.4 2.6a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z"/>',
  'stethoscope':'<path d="M4.8 2v4a4.2 4.2 0 0 0 8.4 0V2"/><path d="M8 15a6 6 0 0 0 12 0v-3"/><circle cx="20" cy="10" r="2"/>',
  'sunrise':'<path d="M12 2v8"/><path d="m4.9 10.9 1.4 1.4"/><path d="m17.7 12.3 1.4-1.4"/><path d="M2 18h20"/><path d="M6 22h12"/><path d="M8 18a4 4 0 0 1 8 0"/>',
  'sunset':'<path d="M12 10V2"/><path d="m4.9 10.9 1.4 1.4"/><path d="m17.7 12.3 1.4-1.4"/><path d="M2 18h20"/><path d="M6 22h12"/><path d="M8 18a4 4 0 0 1 8 0"/>',
  'triangle-alert':'<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  'user':'<path d="M19 21a7 7 0 0 0-14 0"/><circle cx="12" cy="7" r="4"/>',
  'users':'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>',
  'x':'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  'x-circle':'<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>'
};
function fallbackIconSvg(name,attrs=''){
  const body=FALLBACK_ICONS[name]||FALLBACK_ICONS.info;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" data-lucide="${name}" ${attrs}>${body}</svg>`;
}
function renderFallbackIcons(){
  document.querySelectorAll('[data-lucide]').forEach(el=>{
    const name=el.getAttribute('data-lucide');
    if(el.tagName.toLowerCase()==='svg'){
      if(!el.children.length)el.innerHTML=FALLBACK_ICONS[name]||FALLBACK_ICONS.info;
      return;
    }
    el.outerHTML=fallbackIconSvg(name);
  });
}
function reIcons(){
  try{
    if(typeof lucide!=='undefined')lucide.createIcons();
    else renderFallbackIcons();
  }catch(e){renderFallbackIcons();}
}

async function initApp(){
  document.getElementById('pg-date').textContent=' - '+new Date().toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'long',year:'numeric'});
  reIcons();
  checkServer();
  let ready = false;
  try {
    ready = await loadSidebarUser();
  } catch(e) {
    console.error('Sidebar load failed:', e);
    showUnregisteredState('Unable to verify this browser identity.');
  }
  // Fetch server-side display settings (affects all roles)
  try {
    const s = await get('/api/settings');
    if(s) APP_SETTINGS = { ...APP_SETTINGS, ...s };
  } catch(e) {}
  try {
    if(ready) loadDashboard();
  } catch(e) {
    console.error('Dashboard load failed:', e);
  }
  const ovBy=document.getElementById('ov-by'),ovDate=document.getElementById('ov-date');
  if(ovBy&&MY_ID)ovBy.value=MY_ID;
  if(ovDate)ovDate.value=TODAY;
  setInterval(()=>{
    try{
      // Skip auto-refresh if user is actively typing or has unsaved input
      const active = document.activeElement;
      const isTyping = active && (
        active.tagName==='INPUT' ||
        active.tagName==='TEXTAREA' ||
        active.tagName==='SELECT'
      );
      const hasInput = [...document.querySelectorAll(
        '.page.active input[type="text"], .page.active textarea'
      )].some(el=>el.value.trim().length>0);
      if(isTyping||hasInput) return;
      const a=document.querySelector('.page.active')?.id?.replace('page-','');
      if(!a) return;
      // Soft refresh: reload DATA only, never re-init dropdowns.
      // nav() would call initHistory() which rebuilds and resets the
      // employee dropdown — causing the "jumps back to first employee" bug.
      if(a==='dashboard')   loadDashboard();
      else if(a==='today')  loadToday();
      else if(a==='mystatus') loadMyStatus();
      else if(a==='history')  loadHistory();       // data only, dropdown untouched
      else if(a==='compliance') loadCompliance();  // loadCompliance already preserves selection
      else if(a==='leavemgmt')  loadTeamLeave();   // only refresh the calendar, not the form
      else if(a==='anomalies')  loadAnomalies();
      else if(a==='team')       loadTeam();
      else if(a==='insights') loadInsights();
      else if(a==='rhythm')   loadRhythm();
      else nav(a); // for other pages (config, roles etc) full nav is fine
    }catch(e){}
  },60000);
}

// -- TEAM ASSIGNMENT ----------------------------------
async function loadTeamAssign(){
  const isAdminView = MY_ROLE === 'admin';
  const teamsD = await get('/api/teams');
  const el = document.getElementById('team-assign-list');
  const allTeams = teamsD?.teams?.map(t=>t.name) || [];

  if(isAdminView){
    // Admin sees all managers and can edit anyone
    const rolesD = await get('/api/roles');
    const managers = (rolesD?.roles||[]).filter(r=>r.role==='manager'||r.role==='admin');
    if(!managers.length){
      el.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:12px 0">No managers or admins assigned yet.</div>';
      return;
    }
    el.innerHTML = managers.map(m=>_renderTeamAssignRow(m.employee_id, m.managed_teams, allTeams, true)).join('');
  } else {
    // Manager sees only themselves — fetch own managed teams
    const myData = await get(`/api/managed-teams/${MY_ID}`);
    if(!myData){
      el.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:12px 0">Could not load your team access.</div>';
      return;
    }
    el.innerHTML = `
      <div class="notice info" style="margin-bottom:14px"><i data-lucide="info"></i>
        <span>Select which teams you want to track and manage. Your attendance is always tracked under your registered team.</span>
      </div>
      ${_renderTeamAssignRow(MY_ID, myData.managed_teams, allTeams, false)}`;
  }
  reIcons();
}

function _renderTeamAssignRow(empId, managedTeams, allTeams, showId){
  const assigned = managedTeams || [];
  const isAll = !managedTeams || managedTeams === null;
  // Store data as JSON in hidden divs to avoid onclick escaping issues
  const dataId = `ta-data-${empId}`;
  return `<div style="padding:16px 0;border-bottom:1px solid var(--b0)">
    <script type="application/json" id="${dataId}">${JSON.stringify({assigned, allTeams, isAll})}<\/script>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <div>
        ${showId?`<div style="font-size:13px;font-weight:500">${empId}</div>`:''}
        <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px" id="ta-chips-${empId}">
          ${_teamsChipsHtml(assigned, isAll)}
        </div>
      </div>
      <div style="display:flex;gap:6px">
        <button class="btn btn-ghost btn-sm" onclick="editTeamAccess('${empId}')"><i data-lucide="pencil"></i>Edit</button>
        ${!isAll?`<button class="btn btn-ghost btn-sm" onclick="clearTeamAccess('${empId}')" title="Remove restrictions"><i data-lucide="globe"></i>All access</button>`:''}
      </div>
    </div>
    <div id="ta-edit-${empId}" style="display:none"></div>
  </div>`;
}

function _teamsChipsHtml(assigned, isAll){
  if(isAll) return '<span style="font-family:var(--mono);font-size:10px;color:var(--green)">All teams (unrestricted)</span>';
  if(!assigned.length) return '<span style="font-family:var(--mono);font-size:10px;color:var(--amber)">No teams assigned — click Edit to add</span>';
  return assigned.map(t=>`<span style="background:var(--bbg);color:var(--blue);border:1px solid rgba(96,165,250,0.2);border-radius:4px;padding:1px 8px;font-family:var(--mono);font-size:10px">${t}</span>`).join('');
}

function editTeamAccess(empId){
  const dataEl = document.getElementById(`ta-data-${empId}`);
  if(!dataEl){ console.error('No data for', empId); return; }
  const {assigned, allTeams, isAll} = JSON.parse(dataEl.textContent);
  const el = document.getElementById(`ta-edit-${empId}`);
  if(!el) return;
  el.style.display = 'block';
  el.innerHTML = `<div style="background:var(--bg2);border:1px solid var(--b0);border-radius:10px;padding:16px;margin-top:8px">
    <div style="font-size:11px;color:var(--tx3);margin-bottom:12px;font-family:var(--mono);letter-spacing:0.04em">SELECT TEAMS TO GRANT ACCESS</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px">
      ${allTeams.map(t=>`
        <label style="display:flex;align-items:center;gap:7px;padding:6px 12px;border-radius:7px;
          border:1px solid var(--b1);background:var(--bg1);cursor:pointer;font-size:11px;
          font-weight:500;color:var(--tx2);transition:all 0.12s;user-select:none"
          onmouseover="this.style.borderColor='var(--acc)';this.style.color='var(--tx)'"
          onmouseout="this.querySelector('input').checked?(this.style.borderColor='var(--acc)',this.style.background='var(--accg)',this.style.color='var(--accl)'):(this.style.borderColor='var(--b1)',this.style.background='var(--bg1)',this.style.color='var(--tx2)')">
          <input type="checkbox" value="${t}"
            ${(assigned.includes(t)||isAll)?'checked':''}
            onchange="this.closest('label').style.borderColor=this.checked?'var(--acc)':'var(--b1)';
                      this.closest('label').style.background=this.checked?'var(--accg)':'var(--bg1)';
                      this.closest('label').style.color=this.checked?'var(--accl)':'var(--tx2)'"
            style="display:none"> ${t}
        </label>`).join('')}
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-acc btn-sm" onclick="saveTeamAccess('${empId}')"><i data-lucide="save"></i>Save</button>
      <button class="btn btn-ghost btn-sm" onclick="document.getElementById('ta-edit-${empId}').style.display='none'"><i data-lucide="x"></i>Cancel</button>
    </div>
  </div>`;
  // Apply initial checked styles
  el.querySelectorAll('label').forEach(lbl=>{
    const cb = lbl.querySelector('input');
    if(cb?.checked){
      lbl.style.borderColor='var(--acc)';
      lbl.style.background='var(--accg)';
      lbl.style.color='var(--accl)';
    }
  });
  reIcons();
}

async function saveTeamAccess(empId){
  const el = document.getElementById(`ta-edit-${empId}`);
  const checked = [...el.querySelectorAll('input[type=checkbox]:checked')].map(c=>c.value);
  const managed = checked.length ? checked : null;
  const r = await fetch(`${API}/api/managed-teams/${empId}`,{
    method:'PUT',
    headers:{'Content-Type':'application/json','X-Employee-Id':MY_ID,'X-Device-Token':MY_TOKEN},
    body: JSON.stringify({managed_teams: managed})
  });
  if(r.ok){
    notice('ok', `Team access updated for ${empId}`);
    // Update the data store and chips inline without full reload
    const dataEl = document.getElementById(`ta-data-${empId}`);
    if(dataEl){
      const prev = JSON.parse(dataEl.textContent);
      dataEl.textContent = JSON.stringify({...prev, assigned: managed||[], isAll: !managed});
    }
    const chipsEl = document.getElementById(`ta-chips-${empId}`);
    if(chipsEl) chipsEl.innerHTML = _teamsChipsHtml(managed||[], !managed);
    el.style.display = 'none';
  } else {
    const e = await r.json().catch(()=>({}));
    notice('err', e.detail||'Save failed');
  }
}

async function clearTeamAccess(empId){
  const r=await fetch(`${API}/api/managed-teams/${empId}`,{
    method:'PUT',headers:{'Content-Type':'application/json','X-Employee-Id':MY_ID,'X-Device-Token':MY_TOKEN},
    body:JSON.stringify({managed_teams:null})
  });
  if(r.ok){notice('ok',`${empId} now has unrestricted access`);loadTeamAssign();}
  else notice('err','Failed to update');
}

// -- TEAM FILTER & SEARCH HELPERS ---------------------
let _allTeamsCache = null;
async function _getAllTeams(){
  if(_allTeamsCache) return _allTeamsCache;
  const d = await get('/api/teams');
  _allTeamsCache = d?.teams?.map(t=>t.name)||[];
  return _allTeamsCache;
}

function onDashTeamFilter(){
  DASH_TEAM_FILTER = document.getElementById('dash-team-filter')?.value||'';
  const badge = document.getElementById('dash-filter-badge');
  if(badge) badge.textContent = DASH_TEAM_FILTER ? `Showing: ${DASH_TEAM_FILTER}` : '';
  loadDashboard();
}

// -- DASHBOARD SEARCH ----------------------------------
let _allEmployeesCache = null;
async function _getAllEmployees(){
  if(_allEmployeesCache) return _allEmployeesCache;
  const d = await get('/api/team'); // returns all registered devices
  _allEmployeesCache = d?.team||[];
  return _allEmployeesCache;
}

function toggleDashSearch(){
  const panel = document.getElementById('dash-search-panel');
  if(!panel) return;
  const visible = panel.style.display==='block';
  panel.style.display = visible?'none':'block';
  if(!visible) setTimeout(()=>document.getElementById('dash-search-input')?.focus(),50);
}

function closeDashSearch(){
  const panel = document.getElementById('dash-search-panel');
  if(panel) panel.style.display='none';
}

// Close search on outside click
document.addEventListener('click', e=>{
  const wrap = document.getElementById('dash-search-wrap');
  if(wrap && !wrap.contains(e.target)) closeDashSearch();
});

async function onDashSearch(query){
  const resultsEl = document.getElementById('dash-search-results');
  if(!query.trim()){ resultsEl.innerHTML=''; return; }
  const q = query.toLowerCase();
  // Get all employees + today's check-ins
  const [employees, todayData] = await Promise.all([_getAllEmployees(), get('/api/today')]);
  const checkinMap = {};
  (todayData?.checkins||[]).forEach(c=>{ checkinMap[c.employee_id]=c; });
  // Filter employees by name or ID
  const matches = employees.filter(e=>
    e.employee_name.toLowerCase().includes(q)||
    e.employee_id.toLowerCase().includes(q)
  ).slice(0,8); // max 8 results
  if(!matches.length){
    resultsEl.innerHTML='<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;padding:8px">No matches found</div>';
    return;
  }
  resultsEl.innerHTML = matches.map(e=>{
    const c = checkinMap[e.employee_id];
    const status = c ? (c.display_status||c.status) : null;
    const statusHtml = c
      ? `<div style="display:flex;align-items:center;gap:4px;margin-top:3px">${chipS(status,c.split_label)}${c.split_label?renderSplitLabel(c.split_label):''}</div>`
      : '<div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:2px">Not checked in today</div>';
    const timeHtml = c ? `<div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${fmt(c.timestamp)}</div>` : '';
    return `<div style="display:flex;align-items:flex-start;gap:10px;padding:8px;border-radius:7px;cursor:pointer;transition:background 0.1s"
      onmouseover="this.style.background='var(--bg2)'" onmouseout="this.style.background='transparent'">
      <div style="width:28px;height:28px;border-radius:50%;background:${colorFor(e.employee_id)};display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff;flex-shrink:0">${iniOf(e.employee_name)}</div>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:500">${e.employee_name}</div>
        <div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${e.employee_id} · ${e.team||'-'}</div>
        ${statusHtml}
        ${timeHtml}
      </div>
    </div>`;
  }).join('');
  reIcons();
}

if(document.readyState==='loading'){
  document.addEventListener('DOMContentLoaded',initApp);
} else {
  initApp();
}
// ── INSIGHTS ──────────────────────────────────────────────────────────────
async function loadInsights(){
  // Initialise person to self on first load or if reset
  if(!INSIGHTS_EMP_ID) INSIGHTS_EMP_ID = MY_ID;

  // Build/refresh person picker (employees = own team, mgr/admin = managed scope)
  await _initInsightsPicker();

  const el = document.getElementById('insights-content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Loading insights...</div>';

  const empId = INSIGHTS_EMP_ID;
  const isSelf = empId === MY_ID;

  // Compliance fetch scoped by role
  const compMonth = TODAY.slice(0,7);
  const compUrl = (MY_ROLE==='employee' && MY_TEAM)
    ? `/api/compliance?month=${compMonth}&team=${encodeURIComponent(MY_TEAM)}`
    : `/api/compliance?month=${compMonth}`;

  const [d, compData] = await Promise.all([
    get(`/api/insights/${empId}`),
    get(compUrl),
  ]);
  if(!d){ el.innerHTML = '<div class="notice warn">Could not load insights.</div>'; return; }

  // Update team label in picker
  const teamSpan = document.getElementById('insights-person-team');
  if(teamSpan) teamSpan.textContent = d.employee_name ? (isSelf ? '' : `· ${d.employee_name.split(' ')[0]}'s insights`) : '';

  const ownerLabel = isSelf ? 'My' : `${d.employee_name?.split(' ')[0]}'s`;

  if(d.insufficient_data){
    el.innerHTML = `
      <div class="card" style="max-width:560px">
        <div class="card-title">${ownerLabel} Insights <span class="card-sub">Personal forecast</span></div>
        <div class="notice info" style="margin:0">
          <i data-lucide="info"></i>
          <span>${d.message}</span>
        </div>
      </div>`;
    reIcons(); return;
  }

  const m = d.monthly;
  const pct = Math.round((m.actual_wfo / m.target) * 100);
  const barW = Math.min(100, pct);
  const barColor = pct >= 100 ? 'var(--green)' : pct >= 60 ? 'var(--amber)' : 'var(--red)';
  const needed = m.needed;
  const remaining = m.remaining_days;
  const achievable = m.achievable;
  const projected = d.projected_month_total || m.actual_wfo;

  // ── Progress card ────────────────────────────────────────────────────
  const progressCard = `
    <div class="card">
      <div class="card-title">${ownerLabel} ${m.month} Progress <span class="card-sub">${d.confidence_label}</span></div>
      ${narrativeBlock(d.narratives?.progress)}
      <div style="margin:6px 0 14px">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
          <span style="font-family:var(--serif);font-size:28px;color:${barColor}">${m.actual_wfo}</span>
          <span style="font-family:var(--mono);font-size:11px;color:var(--tx3)">/ ${m.target} WFO days</span>
        </div>
        <div style="height:6px;background:var(--bg2);border-radius:4px;overflow:hidden">
          <div style="height:100%;width:${barW}%;background:${barColor};border-radius:4px;transition:width 0.6s ease"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:6px">
          <span style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${m.elapsed_days} days elapsed</span>
          <span style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${remaining} days remaining</span>
        </div>
      </div>
      ${needed > 0 ? `
      <div style="display:flex;flex-direction:column;gap:6px">
        <div style="font-family:var(--mono);font-size:11px;color:${achievable?'var(--amber)':'var(--red)'}">
          ${needed} more WFO day${needed!==1?'s':''} needed ${achievable?`— achievable in ${remaining} remaining days`:'— target may be difficult to reach'}
        </div>
        ${d.confidence !== 'insufficient' ? `
        <div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">
          Based on ${ownerLabel.toLowerCase()} pattern: predicted month-end total ~${Math.round(projected)} days
        </div>` : ''}
      </div>` : `
      <div style="font-family:var(--mono);font-size:11px;color:var(--green)">
        Monthly target met!
      </div>`}
    </div>`;

  // ── WFO Pattern card — uses stable rates ─────────────────────────────
  const dowNames = ['Mon','Tue','Wed','Thu','Fri'];
  const stableRates = d.dow_rates_stable || d.dow_rates || {};
  const dowBars = Object.entries(stableRates).map(([dow, rate]) => {
    const h = Math.max(3, Math.round(rate * 48));
    const c = rate >= 0.65 ? 'var(--green)' : rate >= 0.35 ? 'var(--amber)' : 'var(--red)';
    return `<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1">
      <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">${Math.round(rate*100)}%</div>
      <div style="width:100%;height:48px;display:flex;align-items:flex-end">
        <div style="width:100%;height:${h}px;background:${c};border-radius:3px 3px 0 0"></div>
      </div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--tx2)">${dowNames[dow]}</div>
    </div>`;
  }).join('');

  const patternCard = `
    <div class="card">
      <div class="card-title">${ownerLabel} WFO Pattern <span class="card-sub">Day-of-week baseline</span></div>
      ${narrativeBlock(d.narratives?.pattern)}
      <div style="display:flex;gap:8px;align-items:flex-end;height:80px">${dowBars}</div>
      ${d.narratives?.pattern ? `
      <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:8px">
        Based on ${d.active_weeks} week${d.active_weeks!==1?'s':''} of recorded days, not a prediction.
      </div>` : `
      <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:8px">
        Based on ${d.active_weeks} week${d.active_weeks!==1?'s':''} of recorded days — this is what has
        actually happened, not a prediction.
      </div>
      <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:4px;border-top:1px solid var(--b0);padding-top:6px">
        Stable 12-week average — single deviations don't move these bars.
        ${d.predictability !== undefined ? `<br>How well this predicts upcoming days: <span style="color:${
          d.confidence==='high'?'var(--green)':d.confidence==='medium'?'var(--amber)':'var(--tx2)'
        }">${d.confidence_label}</span>` : ''}
      </div>`}
    </div>`;

  // ── Compliance status card ────────────────────────────────────────────
  const myComp = (compData?.team||[]).find(r=>r.employee_id===empId);
  let compCard = '';
  if(myComp){
    const ragCol = myComp.rag==='green'?'var(--green)':myComp.rag==='amber'?'var(--amber)':myComp.rag==='orange'?'#f97316':'var(--red)';
    const ragLabel = myComp.rag==='green'?'Target met':myComp.rag==='amber'?'On track':myComp.rag==='orange'?'Weekly pattern uneven':'Monthly target missed';
    const weekTip = (myComp.weekly||[]).map(w=>{
      if(w.no_data && w.partial_week) return `WK ${w.week_start.slice(5)}: Partial week (grace)`;
      if(w.no_data) return `WK ${w.week_start.slice(5)}: No data (grace period)`;
      return `WK ${w.week_start.slice(5)}: ${w.wfo}/${w.target} WFO ${w.passed?'Pass':'Miss'}`;
    }).join('\n');
    const realWeeks=(myComp.weekly||[]).filter(w=>!w.no_data);
    const realPassed=realWeeks.filter(w=>w.passed).length;
    compCard = `
      <div class="card">
        <div class="card-title">${ownerLabel} Compliance Status <span class="card-sub">${TODAY.slice(0,7)}</span></div>
        <div style="display:flex;align-items:center;gap:14px;padding:10px 0">
          <div style="width:14px;height:14px;border-radius:50%;background:${ragCol};flex-shrink:0"></div>
          <div>
            <div style="font-size:13px;font-weight:500;color:${ragCol}">${ragLabel}</div>
            <div style="font-family:var(--mono);font-size:11px;color:var(--tx2);margin-top:2px">${myComp.status||'-'}</div>
          </div>
        </div>
        <div style="display:flex;gap:18px;padding:10px 0;border-top:1px solid var(--b0)">
          <div style="text-align:center">
            <div style="font-family:var(--serif);font-size:22px;color:var(--green)">${myComp.wfo}</div>
            <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">WFO days</div>
          </div>
          <div style="text-align:center">
            <div style="font-family:var(--serif);font-size:22px;color:var(--blue)">${myComp.wfh}</div>
            <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">WFH days</div>
          </div>
          <div style="text-align:center">
            <div style="font-family:var(--serif);font-size:22px;color:var(--amber)">${myComp.leave}</div>
            <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">Leave days</div>
          </div>
          <div style="flex:1"></div>
          <div title="${weekTip}" style="text-align:center;cursor:default">
            <div style="font-family:var(--serif);font-size:22px;color:${realWeeks.length===0?'var(--tx3)':realPassed===realWeeks.length?'var(--green)':realPassed>0?'var(--amber)':'var(--red)'}">${realWeeks.length===0?'-':realPassed+'/'+realWeeks.length}</div>
            <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">weeks passed</div>
          </div>
        </div>
      </div>`;
  }

  // ── Compliance Outlook card (replaces 14-day table) ───────────────────
  const compWeeks = d.compliance_weeks || [];
  const wfhBudget = d.wfh_budget ?? null;
  let outlookCard = '';

  if(compWeeks.length === 0 && m.needed === 0){
    outlookCard = `
      <div class="card">
        <div class="card-title">Compliance Outlook <span class="card-sub">${m.month}</span></div>
        <div style="font-family:var(--mono);font-size:11px;color:var(--green);padding:8px 0">
          Monthly target already met — no further action needed.
        </div>
      </div>`;
  } else if(compWeeks.length === 0){
    outlookCard = `
      <div class="card">
        <div class="card-title">Compliance Outlook <span class="card-sub">${m.month}</span></div>
        <div style="font-family:var(--mono);font-size:11px;color:var(--tx3);padding:8px 0">
          No remaining working days this month.
        </div>
      </div>`;
  } else {
    // Plain-language monthly budget line. This states, in one sentence, how
    // many WFH days are left before the monthly office target is at risk —
    // that's the number people actually come here to check.
    const budgetColor = wfhBudget === 0 ? 'var(--red)' : wfhBudget <= 3 ? 'var(--amber)' : 'var(--green)';
    const budgetHtml = wfhBudget !== null ? `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;
                  background:var(--bg2);border:1px solid var(--b0);border-radius:8px;margin-bottom:14px">
        <div>
          <div style="font-family:var(--mono);font-size:11px;color:${budgetColor}">
            ${wfhBudget === 0
              ? 'No WFH days left this month — every remaining working day needs to be in office'
              : `${wfhBudget} WFH day${wfhBudget!==1?'s':''} left this month before the target is at risk`}
          </div>
          <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:2px">
            Based on your pattern, you're on track for about ${Math.round(projected)} office days this month
          </div>
        </div>
        <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);text-align:right">
          ${m.needed} more needed<br>${m.remaining_days} working days left
        </div>
      </div>` : '';

    // Per-week rows
    const weekRows = compWeeks.map(wk => {
      const badge = wk.risk
        ? `<span style="font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:10px;background:var(--rbg);color:var(--red)">Needs more office days</span>`
        : `<span style="font-family:var(--mono);font-size:9px;padding:2px 7px;border-radius:10px;background:var(--gbg);color:var(--green)">On track</span>`;

      // Day mini-bars. Facts (an actual check-in, leave, or holiday already
      // on record) are drawn solid — they're not a guess. Forecast days are
      // drawn lighter/dashed on purpose, so it's visually obvious those are
      // predictions, not certainties, and their confidence label shows on
      // hover (and underneath, in words) instead of a fake-precise percent.
      const dayBars = wk.days.map(day => {
        let barBg, barH, opacity = '1', border = 'none';
        if(day.is_leave){
          barBg = 'var(--tx3)'; barH = 6; opacity = '0.6';
        } else if(day.is_fact){
          barBg = day.predicted_status==='wfo' ? 'var(--green)' : 'var(--blue)';
          barH = day.predicted_status==='wfo' ? 28 : 14;
        } else {
          // forecast — dashed outline + lower opacity signals "this is a guess"
          barBg = day.predicted_status==='wfo' ? 'var(--green)' : 'var(--blue)';
          barH = Math.max(6, Math.round(day.rate * 28));
          opacity = '0.45';
          border = '1px dashed ' + barBg;
        }
        const subLabel = day.is_leave ? 'Leave'
          : day.is_fact ? (day.predicted_status==='wfo' ? 'In office' : 'WFH')
          : (day.predicted_status==='wfo' ? 'Likely office' : 'Likely WFH');
        const title = day.is_leave ? `${day.dow_name}: leave`
          : day.is_fact ? `${day.dow_name}: ${subLabel} (recorded)`
          : `${day.dow_name}: ${subLabel} — ${day.confidence_label}`;
        return `<div title="${title}" style="display:flex;flex-direction:column;align-items:center;gap:3px;flex:1;cursor:default">
          <div style="width:100%;height:28px;display:flex;align-items:flex-end">
            <div style="width:100%;height:${barH}px;background:${barBg};opacity:${opacity};border:${border};
                        border-radius:2px 2px 0 0;box-sizing:border-box;
                        ${day.is_today?'outline:1px solid var(--acc);outline-offset:1px':''}"></div>
          </div>
          <div style="font-family:var(--mono);font-size:8px;color:${day.is_today?'var(--acc)':'var(--tx3)'}">${day.dow_name}</div>
          <div style="font-family:var(--mono);font-size:7px;color:var(--tx3);text-align:center;line-height:1.2">${subLabel}</div>
        </div>`;
      }).join('');

      // Week date range label
      const [ws_mo, ws_d] = wk.week_start.slice(5).split('-').map(Number);
      const [we_mo, we_d] = wk.week_end.slice(5).split('-').map(Number);
      const months = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
      const weekLabel = ws_mo === we_mo
        ? `${ws_d}–${we_d} ${months[ws_mo]}`
        : `${ws_d} ${months[ws_mo]}–${we_d} ${months[we_mo]}`;

      // How many days this week already point to "office" — counting both
      // recorded facts and forecast days — read straight off the day list
      // so this number can never disagree with what the bars show.
      const officeDayCount = wk.days.filter(d => !d.is_leave && d.predicted_status === 'wfo').length;

      // Chips built straight from the same per-day list the bars below use
      // — replaces a single plain-text sentence (which mixed "In office"
      // and "likely office" inline, easy to misread at a glance) with
      // visually distinct groups that use the exact same solid/dashed,
      // green/blue vocabulary as the bar chart directly underneath, so the
      // summary can never visually disagree with what the bars show.
      const chipGroups = [
        {key:'office_fact',   label:'In office',     match: d=>!d.is_leave && d.is_fact && d.predicted_status==='wfo', color:'var(--green)', dashed:false},
        {key:'office_forecast', label:'Likely office', match: d=>!d.is_leave && !d.is_fact && d.predicted_status==='wfo', color:'var(--green)', dashed:true},
        {key:'home_fact',     label:'WFH',           match: d=>!d.is_leave && d.is_fact && d.predicted_status==='wfh', color:'var(--blue)', dashed:false},
        {key:'home_forecast', label:'Likely WFH',    match: d=>!d.is_leave && !d.is_fact && d.predicted_status==='wfh', color:'var(--blue)', dashed:true},
      ].map(g => {
        const days = wk.days.filter(g.match).map(d=>d.dow_name);
        if(!days.length) return '';
        return `<span style="display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:10px;
                    padding:3px 8px;border-radius:12px;background:var(--bg3);color:var(--tx2)">
          <span style="width:8px;height:8px;border-radius:50%;background:${g.color};
                       ${g.dashed?`opacity:0.45;border:1px dashed ${g.color}`:''}"></span>
          ${g.label} <b style="color:var(--tx1);font-weight:500">${days.join(', ')}</b>
        </span>`;
      }).join('');

      return `
        <div style="background:var(--bg2);border:1px solid ${wk.risk?'rgba(248,113,113,0.2)':'var(--b0)'};
                    border-radius:8px;padding:12px 14px;${wk.is_current_week?'border-color:rgba(193,123,63,0.3)':''}">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-family:var(--mono);font-size:11px;color:${wk.is_current_week?'var(--acc)':'var(--tx2)'}">
                Week of ${weekLabel}
              </span>
              ${wk.leave_days>0?`<span style="font-family:var(--mono);font-size:9px;color:var(--tx3)">${wk.leave_days}d leave</span>`:''}
            </div>
            ${badge}
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">${chipGroups}</div>
          <div style="display:flex;gap:6px;align-items:flex-end;height:56px;margin-bottom:8px">${dayBars}</div>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="font-family:var(--mono);font-size:10px;color:${wk.risk?'var(--red)':'var(--green)'}">
              ${officeDayCount} of ${wk.week_target} office day${wk.week_target!==1?'s':''} needed this week
            </span>
            ${wk.week_wfh_budget>0?`<span style="font-family:var(--mono);font-size:10px;color:var(--tx3)">
              · ${wk.week_wfh_budget} WFH day${wk.week_wfh_budget!==1?'s':''} okay this week
            </span>`:''}
          </div>
        </div>`;
    }).join('');

    // Up-front legend, not a buried footnote — the single thing everyone
    // reading this card needs to know before the numbers make sense: solid
    // = already happened, dashed = a guess based on past pattern.
    const legendHtml = `
      <div style="display:flex;align-items:center;gap:16px;padding:8px 12px;margin-bottom:12px;
                  background:var(--bg3);border-radius:6px;flex-wrap:wrap">
        <div style="display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--tx2)">
          <span style="width:10px;height:10px;border-radius:2px;background:var(--tx2)"></span> Recorded fact
        </div>
        <div style="display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--tx2)">
          <span style="width:10px;height:10px;border-radius:2px;background:var(--tx2);opacity:0.45;border:1px dashed var(--tx2)"></span> Forecast — not a guarantee
        </div>
      </div>`;

    outlookCard = `
      <div class="card">
        <div class="card-title">Compliance Outlook
          <span class="card-sub">${m.month} — remaining weeks</span>
        </div>
        ${legendHtml}
        ${narrativeBlock(d.narratives?.compliance_outlook)}
        ${budgetHtml}
        <div style="display:flex;flex-direction:column;gap:10px">${weekRows}</div>
        <div style="font-family:var(--mono);font-size:10px;color:var(--tx3);margin-top:10px;padding-top:8px;border-top:1px solid var(--b0)">
          Forecast days are based on ${ownerLabel.toLowerCase()} usual pattern for that weekday — hover any
          day for its confidence level. No forecast is a guarantee, it just reflects past behaviour.
        </div>
      </div>`;
  }

  el.innerHTML = `
    <div class="two-col" style="margin-bottom:20px">${progressCard}${patternCard}</div>
    <div style="margin-bottom:20px">${compCard}</div>
    <div>${outlookCard}</div>`;
  reIcons();
}

async function _initInsightsPicker(){
  const picker = document.getElementById('insights-person-picker');
  const sel    = document.getElementById('insights-emp-sel');
  if(!picker || !sel) return;

  const data = await get('/api/insights-members');
  const members = data?.members || [];

  if(members.length <= 1){
    picker.style.display = 'none';
    if(members.length === 1) INSIGHTS_EMP_ID = members[0].employee_id;
    return;
  }

  picker.style.display = 'block';

  // Rebuild only if membership changed or empty
  const currentIds = [...sel.options].map(o=>o.value).join(',');
  const newIds = members.map(m=>m.employee_id).join(',');
  if(currentIds === newIds) return; // preserve current selection

  sel.innerHTML = members.map(m => {
    const label = m.employee_id === MY_ID
      ? `${m.employee_name} (me)`
      : `${m.employee_name}`;
    const teamSuffix = m.team ? ` · ${m.team}` : '';
    return `<option value="${m.employee_id}"${m.employee_id===INSIGHTS_EMP_ID?' selected':''}>${label}${teamSuffix}</option>`;
  }).join('');

  // Ensure INSIGHTS_EMP_ID is valid in current list; fall back to self
  if(!members.find(m=>m.employee_id===INSIGHTS_EMP_ID)){
    INSIGHTS_EMP_ID = MY_ID;
    sel.value = MY_ID;
  }
}

function onInsightsPersonChange(){
  const sel = document.getElementById('insights-emp-sel');
  if(sel) INSIGHTS_EMP_ID = sel.value;
  loadInsights();
}

// ── TEAM RHYTHM ────────────────────────────────────────────────────────────
async function loadRhythm(){
  const el = document.getElementById('rhythm-content');
  el.innerHTML = '<div class="loading"><div class="spinner"></div>Loading team rhythm...</div>';

  const isMgr = MY_ROLE==='manager'||MY_ROLE==='admin';

  // Determine team to load
  let team = MY_TEAM;
  if(isMgr){
    // Show team filter for managers/admins
    const filterWrap = document.getElementById('rhythm-team-filter');
    const filterSel  = document.getElementById('rhythm-team-sel');
    if(filterWrap) filterWrap.style.display = 'block';
    if(filterSel && filterSel.options.length <= 1){
      const teams = MY_MANAGED_TEAMS || await _getAllTeams();
      filterSel.innerHTML = '<option value="">Select team</option>' +
        teams.map(t => `<option value="${t}">${t}</option>`).join('');
    }
    team = filterSel?.value || '';
    if(!team){
      el.innerHTML = '<div style="color:var(--tx3);font-family:var(--mono);font-size:12px;padding:20px 0">Select a team above to view their rhythm.</div>';
      return;
    }
  }

  if(!team){
    el.innerHTML = '<div class="notice warn"><i data-lucide="triangle-alert"></i><span>No team assigned.</span></div>';
    reIcons(); return;
  }

  const d = await get(`/api/rhythm/${encodeURIComponent(team)}`);
  if(!d){ el.innerHTML = '<div class="notice warn">Could not load team rhythm.</div>'; reIcons(); return; }

  const dowNames = ['Monday','Tuesday','Wednesday','Thursday','Friday'];
  const dowShort = ['Mon','Tue','Wed','Thu','Fri'];

  // ── Best days card ──────────────────────────────────────────────────────
  const bestDays = (d.best_days || []).slice(0, 5);
  const bestBars = bestDays.map((bd, i) => {
    const h = Math.round(bd.probability * 56);
    const c = i === 0 ? 'var(--green)' : i === 1 ? 'var(--green)' : 'var(--amber)';
    const pct = Math.round(bd.probability * 100);
    return `<div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex:1">
      <div style="width:100%;height:56px;display:flex;align-items:flex-end">
        <div style="width:100%;height:${Math.max(h,3)}px;background:${c};border-radius:3px 3px 0 0;opacity:0.85"
             title="${bd.label}"></div>
      </div>
      <div style="font-family:var(--mono);font-size:10px;color:var(--tx2);font-weight:${i===0?'600':'400'}">${bd.dow_name.slice(0,3)}${i===0?' ★':''}</div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">${pct}%</div>
    </div>`;
  }).join('');

  const bestDay = bestDays[0]?.dow_name || '';
  const bestCard = `
    <div class="card">
      <div class="card-title">Best Meeting Days <span class="card-sub">${team}</span></div>
      <div style="display:flex;gap:8px;align-items:flex-end;height:56px;margin-bottom:8px">${bestBars}</div>
      <div style="font-family:var(--mono);font-size:10px;color:var(--tx3)">
        Based on last ${d.lookback_weeks} weeks · ${d.team_size} member${d.team_size!==1?'s':''}
        ${d.data_start ? ` · Data from ${d.data_start}` : ''}
        ${bestDay ? ` · <span style="color:var(--green)">Best day: ${bestDay}</span>` : ''}
      </div>
    </div>`;

  // ── Overlap matrix card ─────────────────────────────────────────────────
  const overlapRows = (d.overlap_matrix || []).map(pair => {
    const sc = pair.score;
    const c  = sc >= 0.60 ? 'var(--green)' : sc >= 0.35 ? 'var(--amber)' : 'var(--red)';
    const bar = Math.round(sc * 100);
    return `<tr>
      <td style="font-size:12px">${pair.a_name.split(' ')[0]} + ${pair.b_name.split(' ')[0]}</td>
      <td>
        <div style="display:flex;align-items:center;gap:8px">
          <div style="flex:1;height:5px;background:var(--bg2);border-radius:3px;overflow:hidden">
            <div style="height:100%;width:${bar}%;background:${c};border-radius:3px"></div>
          </div>
          <span style="font-family:var(--mono);font-size:10px;color:${c};min-width:32px">${bar}%</span>
        </div>
      </td>
      <td style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${pair.shared_days} shared days</td>
      <td style="font-family:var(--mono);font-size:10px;color:var(--tx3)">${pair.label}</td>
    </tr>`;
  }).join('');

  const overlapCard = `
    <div class="card">
      <div class="card-title">Team Overlap <span class="card-sub">Days in office together</span></div>
      ${overlapRows ? `<div class="tbl-wrap"><table>
        <thead><tr><th>Pair</th><th>Overlap score</th><th>Shared days</th><th>Assessment</th></tr></thead>
        <tbody>${overlapRows}</tbody>
      </table></div>` : `<div style="color:var(--tx3);font-family:var(--mono);font-size:11px;line-height:1.6">
          Not enough data yet — an overlap score only shows once two people have at least
          5 shared office days on record, so a couple of early check-ins can't produce a
          misleadingly confident number.
        </div>`}
    </div>`;

  // ── Heatmap card ────────────────────────────────────────────────────────
  const heatmap = d.heatmap || [];
  const weeks = heatmap[0]?.weeks || [];
  const weekLabels = weeks.map(w => {
    const d2 = new Date(w.week_start + 'T12:00:00');
    return d2.toLocaleDateString('en-GB',{month:'short',day:'numeric'});
  });

  const heatRows = heatmap.map(row => {
    const cells = row.weeks.map(w => {
      const r = w.rate;
      const op = w.total_days === 0 ? 0.15 : 0.2 + r * 0.8;
      const bg = w.total_days === 0 ? 'var(--bg3)' :
                 r >= 0.6 ? `rgba(74,222,128,${op})` :
                 r >= 0.3 ? `rgba(251,191,36,${op})` :
                 `rgba(248,113,113,${op})`;
      const title = w.total_days === 0 ? 'No data' :
        `${w.wfo_days}/${w.total_days} WFO days (${Math.round(r*100)}%)`;
      return `<td title="${title}" style="width:32px;height:26px;background:${bg};border-radius:3px;text-align:center;font-family:var(--mono);font-size:9px;color:var(--tx2);cursor:default">
        ${w.total_days > 0 ? w.wfo_days : ''}
      </td>`;
    }).join('');
    return `<tr>
      <td style="font-size:11px;padding-right:12px;white-space:nowrap">${row.employee_name.split(' ')[0]}</td>
      ${cells}
    </tr>`;
  }).join('');

  const heatHeader = weekLabels.map(l =>
    `<th style="font-family:var(--mono);font-size:9px;font-weight:400;color:var(--tx3);white-space:nowrap">${l}</th>`
  ).join('');

  const heatCard = `
    <div class="card">
      <div class="card-title">WFO Heatmap <span class="card-sub">Last ${d.lookback_weeks} weeks — darker = more WFO</span></div>
      <div style="overflow-x:auto">
        <table style="border-collapse:separate;border-spacing:3px">
          <thead><tr><th></th>${heatHeader}</tr></thead>
          <tbody>${heatRows}</tbody>
        </table>
      </div>
      <div style="display:flex;gap:12px;margin-top:10px;align-items:center">
        <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">Low</div>
        ${[0.1,0.3,0.5,0.7,0.9].map(r=>`<div style="width:18px;height:10px;border-radius:2px;background:rgba(74,222,128,${0.2+r*0.8})"></div>`).join('')}
        <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">High WFO</div>
      </div>
    </div>`;

  // ── Gaps / suggestions card ─────────────────────────────────────────────
  const gaps = d.gaps || [];
  const gapsHtml = gaps.length ? gaps.map(g => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;background:var(--bg2);border:1px solid rgba(251,191,36,0.2);border-radius:8px">
      <i data-lucide="users" style="width:13px;height:13px;color:var(--amber);margin-top:2px;flex-shrink:0"></i>
      <span style="font-size:12px;color:var(--tx2)">${g.message}</span>
    </div>`).join('') :
    '<div style="font-family:var(--mono);font-size:11px;color:var(--tx3)">No collaboration gaps detected.</div>';

  const gapsCard = `
    <div class="card">
      <div class="card-title">Collaboration Insights <span class="card-sub">Alignment suggestions</span></div>
      <div style="display:flex;flex-direction:column;gap:8px">${gapsHtml}</div>
    </div>`;

  // ── Individual patterns card ────────────────────────────────────────────
  const indivRows = (d.individual || []).map(p => {
    const rates = Object.entries(p.dow_rates || {});
    const bars = rates.map(([dow, rate]) => {
      const h = Math.round(rate * 32);
      const c = rate >= 0.65 ? 'var(--green)' : rate >= 0.35 ? 'var(--amber)' : 'var(--red)';
      const pct = Math.round(rate * 100);
      return `<div title="${dowNames[dow]}: ${pct}% WFO" style="display:flex;flex-direction:column;align-items:center;gap:1px;cursor:default">
        <div style="width:14px;height:32px;display:flex;align-items:flex-end">
          <div style="width:100%;height:${Math.max(h,2)}px;background:${c};border-radius:2px 2px 0 0"></div>
        </div>
        <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">${dowShort[dow]}</div>
      </div>`;
    }).join('');
    const streak = p.current_streak_wfo;
    const isMe = p.employee_id === MY_ID;
    return `<div style="display:flex;align-items:center;gap:14px;padding:10px 12px;background:var(--bg2);border:1px solid ${isMe?'rgba(193,123,63,0.3)':'var(--b0)'};border-radius:8px">
      <div style="width:32px;height:32px;border-radius:50%;background:${colorFor(p.employee_id)};display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#fff;flex-shrink:0">${iniOf(p.employee_name)}</div>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;font-weight:500">${p.employee_name}${isMe?' <span style="font-family:var(--mono);font-size:9px;color:var(--acc)">(you)</span>':''}</div>
        <div style="font-family:var(--mono);font-size:9px;color:var(--tx3)">${p.confidence} · ${p.active_weeks}w data${streak>0?` · ${streak} WFO streak`:''}</div>
      </div>
      <div style="display:flex;gap:3px;align-items:flex-end">${bars}</div>
    </div>`;
  }).join('');

  const indivCard = `
    <div class="card">
      <div class="card-title">Individual Patterns <span class="card-sub">Day-of-week WFO tendency</span></div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;font-family:var(--mono);font-size:9px;color:var(--tx3)">
        <span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:var(--green)"></span>High WFO</span>
        <span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:var(--amber)"></span>Medium</span>
        <span style="display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:2px;background:var(--red)"></span>Low — mostly WFH</span>
        <span>· hover a bar for the exact %</span>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px">${indivRows || '<div style="font-family:var(--mono);font-size:11px;color:var(--tx3)">No one on this team has enough history yet.</div>'}</div>
    </div>`;

  el.innerHTML = `
    ${narrativeBlock(d.narrative)}
    <div class="two-col" style="margin-bottom:20px">${bestCard}${overlapCard}</div>
    <div style="margin-bottom:20px">${heatCard}</div>
    <div class="two-col">${gapsCard}${indivCard}</div>`;
  reIcons();
}

