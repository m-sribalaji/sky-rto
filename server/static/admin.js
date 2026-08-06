// Powers the raw-table admin view — loads a database table, lets you sort/search/edit
// cells inline, export to CSV, and delete rows. Used to be an inline <script> tag inside
// admin.html; pulled out here so it's diffable on its own. Section dividers below
// ("// --") mark state, init, sidebar, load/render table, sort, search, export, inline
// edit, delete, toast, keyboard shortcuts.

// -- STATE -------------------------------------------------
// Migrate old key names to new ones (one-time)
(function(){
  const oldEid = localStorage.getItem('rto_my_employee_id');
  const oldTok = localStorage.getItem('rto_device_token');
  if(oldEid && !localStorage.getItem('_sk_ei')){
    localStorage.setItem('_sk_ei', oldEid);
    localStorage.removeItem('rto_my_employee_id');
  }
  if(oldTok && !localStorage.getItem('_sk_dt')){
    localStorage.setItem('_sk_dt', oldTok);
    localStorage.removeItem('rto_device_token');
  }
})();
const MY_ID    = localStorage.getItem('_sk_ei') || '';
const MY_TOKEN = localStorage.getItem('_sk_dt') || '';
let currentTable = null;
let currentPage  = 0;
let currentSearch = '';
let sortCol = null, sortAsc = true;
let allRows = [];
let pendingDelete = null;
let pendingEdit = null;
let tableSchema = {};
let liveTeams = [];
const LIMIT = 50;

const READONLY_FIELDS = {
  devices:        new Set(['hostname','registered_at','platform','employee_id']),
  checkins:       new Set(['id','timestamp','public_ip','hostname','employee_name','employee_id']),
  day_segments:   new Set(['id','started_at','ended_at','duration_minutes','hostname',
                            'employee_name','segment_number','public_ip','lan_ip',
                            'vpn_tunnel_ip','dns_servers','dns_domains','platform','employee_id']),
  leave_requests: new Set(['id','applied_at','employee_name','employee_id']),
  public_holidays:new Set(['id']),
  roles:          new Set(['id','assigned_at','employee_id','assigned_by']),
  anomalies:      new Set(['id','detected_at','employee_name','employee_id']),
  team_configs:   new Set(['id','created_at']),
};


// -- INIT --------------------------------------------------
async function init() {
  lucide.createIcons();
  if (!MY_ID || !MY_TOKEN) { showDenied('No saved device token found - register/open the main dashboard first.'); return; }
  document.getElementById('topbar-emp').textContent = MY_ID;

  // Check admin role
  try {
    const r = await fetch('/api/admin/tables', {
      headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN }
    });
    if (r.status === 403) { showDenied('You do not have admin access.'); return; }
    if (!r.ok) throw new Error('Server error');
    const counts = await r.json();
    document.getElementById('app').style.display = 'flex';
    document.getElementById('app').style.flexDirection = 'column';
    renderSidebar(counts);
  } catch(e) {
    showDenied('Could not connect to server.');
  }
}

function showDenied(msg) {
  document.getElementById('denied').style.display = 'flex';
  document.getElementById('denied').querySelector('p').textContent = msg;
  lucide.createIcons();
}

// -- SIDEBAR -----------------------------------------------
const TABLE_ICONS = {
  devices:'monitor', checkins:'clock', day_segments:'layers',
  leave_requests:'plane', public_holidays:'sparkles',
  roles:'shield', anomalies:'alert-triangle', team_configs:'users'
};

function renderSidebar(counts) {
  const list = document.getElementById('table-list');
  list.innerHTML = Object.entries(counts).map(([t,c]) => `
    <button class="table-btn" id="btn-${t}" onclick="loadTable('${t}')">
      <span style="display:flex;align-items:center;gap:7px">
        <i data-lucide="${TABLE_ICONS[t]||'table'}"></i>
        ${t.replace(/_/g,' ')}
      </span>
      <span class="count">${c}</span>
    </button>
  `).join('');
  lucide.createIcons();
}

// -- LOAD TABLE --------------------------------------------
async function loadTable(table, page=0) {
  currentTable = table;
  currentPage  = page;
  document.querySelectorAll('.table-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-'+table)?.classList.add('active');

  const main = document.getElementById('main-content');
  main.innerHTML = '<div class="loading"><span>Loading...</span></div>';

  const params = new URLSearchParams({
    page, limit: LIMIT, search: currentSearch
  });
  // Load schema + live teams
  const [sr, tr] = await Promise.all([
    fetch(`/api/admin/schema/${table}`, { headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN } }),
    fetch('/api/teams', { headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN } })
  ]);
  if (sr.ok) tableSchema = await sr.json();
  if (tr.ok) { const td2 = await tr.json(); liveTeams = (td2.teams||[]).map(t=>t.name); }

  const r = await fetch(`/api/admin/table/${table}?${params}`, {
    headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN }
  });
  if (!r.ok) { main.innerHTML = '<div class="empty"><p>Failed to load table.</p></div>'; return; }
  const data = await r.json();
  allRows = data.rows;
  renderTable(data);
}

// -- RENDER TABLE ------------------------------------------
function renderTable(data) {
  const main = document.getElementById('main-content');
  if (!data.rows.length && !currentSearch) {
    main.innerHTML = `
      <div class="tbl-header">
        <span class="tbl-name">${currentTable}</span>
        <span class="tbl-total">0 rows</span>
      </div>
      <div class="empty"><i data-lucide="inbox"></i><p>No rows in this table</p></div>`;
    lucide.createIcons(); return;
  }

  const cols = data.columns;
  const from = data.page * data.limit + 1;
  const to   = Math.min(from + data.rows.length - 1, data.total);

  main.innerHTML = `
    <div class="tbl-header">
      <span class="tbl-name">${currentTable}</span>
      <span class="tbl-total">${data.total} rows</span>
      <div class="search-wrap">
        <i data-lucide="search"></i>
        <input type="text" placeholder="Search all columns..."
               value="${currentSearch}"
               oninput="onSearch(this.value)"/>
      </div>
      <button class="export-btn" onclick="exportCSV()">
        <i data-lucide="download"></i> CSV
      </button>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr>
            ${cols.map(c => `
              <th onclick="sortBy('${c}')" class="${sortCol===c?'sorted':''}">
                ${c} <i class="sort-icon" data-lucide="${sortCol===c?(sortAsc?'arrow-up':'arrow-down'):'arrow-up-down'}"></i>
              </th>`).join('')}
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${getSortedRows(data.rows).map(row => `
            <tr>
              ${cols.map(c => renderCell(c, row[c], row[cols[0]], currentTable)).join('')}
              <td><button class="del-btn" onclick="askDelete('${currentTable}','${row[cols[0]]}','${row[cols[1]]||''}')">
                <i data-lucide="trash-2"></i>
              </button></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
    <div class="pagination">
      <button class="pg-btn" onclick="loadTable(currentTable,${data.page-1})"
              ${data.page===0?'disabled':''} style="display:flex;align-items:center;gap:5px"><svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg> Prev</button>
      <span class="pg-info">${from}-${to} of ${data.total}</span>
      <button class="pg-btn" onclick="loadTable(currentTable,${data.page+1})"
              ${to>=data.total?'disabled':''} style="display:flex;align-items:center;gap:5px">Next <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg></button>
    </div>`;
  lucide.createIcons();
}

function renderCell(col, val, rowPk, table) {
  const roSet = READONLY_FIELDS[table] || new Set();
  const isReadonly = roSet.has(col);
  const schema  = tableSchema[col] || {};
  const isPk    = schema.pk;
  const colType = schema.type || 'String';

  // Read-only: show as plain non-clickable cell
  if (isPk || isReadonly) {
    const display = (val===null||val===undefined||val==='') ? 'null' : String(val);
    const cls = isPk ? 'pk' : 'readonly';
    return `<td class="${cls}" title="${display}">${display.length>60?display.slice(0,60)+'...':display}</td>`;
  }

  // PK - never editable
  if (isPk) {
    return `<td class="pk">${val??'null'}</td>`;
  }

  const editAttr = `onclick="startEdit(this,'${col}','${rowPk}','${colType}')"`;
  const nullCls  = (val===null||val===undefined||val==='') ? ' null' : '';

  if (val === null || val === undefined || val === '')
    return `<td class="editable${nullCls}" ${editAttr}>null</td>`;

  if (colType === 'Boolean')
    return `<td class="editable ${val?'bool-true':'bool-false'}" ${editAttr}>${val}</td>`;

  // Status colouring
  const statusCols = ['status','final_status','auto_status','display_status'];
  if (statusCols.includes(col)) {
    const cls = val==='wfo'?'status-wfo':val==='wfh'?'status-wfh':
                val.includes('leave')||val==='leave'?'status-leave':'';
    return `<td class="editable ${cls}" title="${val}" ${editAttr}>${val}</td>`;
  }
  if (col==='flagged')
    return `<td class="editable ${val?'status-flagged':''}" ${editAttr}>${val}</td>`;

  const str = String(val);
  return `<td class="editable" title="${str.replace(/"/g,'&quot;')}" ${editAttr}>${str.length>60?str.slice(0,60)+'...':str}</td>`;
}

// -- SORT --------------------------------------------------
function sortBy(col) {
  if (sortCol === col) sortAsc = !sortAsc;
  else { sortCol = col; sortAsc = true; }
  const data = {
    rows: allRows, columns: allRows.length ? Object.keys(allRows[0]) : [],
    total: allRows.length, page: currentPage, limit: LIMIT
  };
  renderTable(data);
}

function getSortedRows(rows) {
  if (!sortCol) return rows;
  return [...rows].sort((a,b) => {
    const va = a[sortCol] ?? '';
    const vb = b[sortCol] ?? '';
    return sortAsc ? (va>vb?1:-1) : (va<vb?1:-1);
  });
}

// -- SEARCH ------------------------------------------------
let searchTimer;
function onSearch(val) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    currentSearch = val;
    loadTable(currentTable, 0);
  }, 350);
}

// -- EXPORT CSV --------------------------------------------
async function exportCSV() {
  // Fetch all rows
  const r = await fetch(`/api/admin/table/${currentTable}?page=0&limit=10000&search=${currentSearch}`, {
    headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN }
  });
  const data = await r.json();
  const cols = data.columns;
  const lines = [
    cols.join(','),
    ...data.rows.map(row => cols.map(c => {
      const v = row[c] ?? '';
      return typeof v === 'string' && v.includes(',') ? `"${v}"` : v;
    }).join(','))
  ];
  const blob = new Blob([lines.join('\n')], {type:'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${currentTable}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  toast(`Exported ${data.rows.length} rows`, 'ok');
}


// -- INLINE EDIT -------------------------------------------
const BOOL_COLS   = new Set(['vpn_active','is_ethernet','flagged','overridden','user_declared','resolved','optional','force_update']);
const STATUS_OPTS = ['wfo','wfh','annual','casual','sick','public_holiday','optional_holiday','half_day_am','half_day_pm','leave','manual','other'];
const ROLE_OPTS   = ['admin','manager','employee'];
const CONF_OPTS   = ['high','medium','low'];

function startEdit(td, col, rowPk, colType) {
  if (td.classList.contains('editing')) return;
  const orig = td.textContent === 'null' ? '' : td.textContent;
  td.classList.add('editing');
  td.removeAttribute('onclick');

  let input;
  if (BOOL_COLS.has(col) || colType === 'Boolean') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'cell-input';
    input.checked = orig === 'true' || orig === '1';
  } else if (col === 'role') {
    input = document.createElement('select');
    input.className = 'cell-input';
    ROLE_OPTS.forEach(o => {
      const opt = document.createElement('option');
      opt.value = o; opt.textContent = o;
      if (o === orig) opt.selected = true;
      input.appendChild(opt);
    });
  } else if (col === 'team') {
    // Live teams from DB
    input = document.createElement('select');
    input.className = 'cell-input';
    liveTeams.forEach(o => {
      const opt = document.createElement('option');
      opt.value = o; opt.textContent = o;
      if (o === orig) opt.selected = true;
      input.appendChild(opt);
    });
    // Also allow current value if not in list
    if (orig && !liveTeams.includes(orig)) {
      const opt = document.createElement('option');
      opt.value = orig; opt.textContent = orig; opt.selected = true;
      input.insertBefore(opt, input.firstChild);
    }
  } else if (['status','final_status','auto_status','leave_type'].includes(col)) {
    input = document.createElement('select');
    input.className = 'cell-input';
    STATUS_OPTS.forEach(o => {
      const opt = document.createElement('option');
      opt.value = o; opt.textContent = o;
      if (o === orig) opt.selected = true;
      input.appendChild(opt);
    });
  } else if (col === 'confidence') {
    input = document.createElement('select');
    input.className = 'cell-input';
    CONF_OPTS.forEach(o => {
      const opt = document.createElement('option');
      opt.value = o; opt.textContent = o;
      if (o === orig) opt.selected = true;
      input.appendChild(opt);
    });
  } else if (colType === 'DateTime') {
    input = document.createElement('input');
    input.type = 'datetime-local';
    input.className = 'cell-input';
    if (orig) { try { input.value = orig.slice(0,16); } catch(e) { input.value = orig; } }
  } else if (colType === 'Integer') {
    input = document.createElement('input');
    input.type = 'number'; input.step = '1';
    input.className = 'cell-input'; input.value = orig;
  } else if (colType === 'Float') {
    input = document.createElement('input');
    input.type = 'number'; input.step = 'any';
    input.className = 'cell-input'; input.value = orig;
  } else {
    input = document.createElement('input');
    input.type = 'text';
    input.className = 'cell-input'; input.value = orig;
  }

  // -- Build wrapper: input + Save + Cancel buttons ------
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;align-items:center;gap:4px;padding:3px 6px;';

  const saveBtn = document.createElement('button');
  saveBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  saveBtn.style.cssText = 'padding:2px 7px;border-radius:4px;border:1px solid rgba(74,222,128,0.4);background:rgba(74,222,128,0.1);color:var(--green);cursor:pointer;font-size:11px;flex-shrink:0;';

  const cancelBtn = document.createElement('button');
  cancelBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
  cancelBtn.style.cssText = 'padding:2px 7px;border-radius:4px;border:1px solid rgba(248,113,113,0.3);background:rgba(248,113,113,0.08);color:var(--red);cursor:pointer;font-size:11px;flex-shrink:0;';

  input.style.flex = '1';
  wrap.appendChild(input);
  wrap.appendChild(saveBtn);
  wrap.appendChild(cancelBtn);
  td.innerHTML = '';
  td.appendChild(wrap);
  if (input.type !== 'checkbox') { input.focus(); }

  const doSave = (e) => {
    e && e.stopPropagation();
    let newVal = input.type === 'checkbox' ? input.checked : (input.value === '' ? null : input.value);
    saveEdit(td, col, rowPk, newVal, colType, orig);
  };
  const doCancel = (e) => {
    e && e.stopPropagation();
    loadTable(currentTable, currentPage);
  };

  saveBtn.addEventListener('mousedown', e => e.preventDefault());
  cancelBtn.addEventListener('mousedown', e => e.preventDefault());
  saveBtn.addEventListener('click', doSave);
  cancelBtn.addEventListener('click', doCancel);

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); doSave(); }
    if (e.key === 'Escape') { e.preventDefault(); doCancel(); }
  });
  // NO blur auto-save
}

async function saveEdit(td, col, rowPk, newVal, colType, orig) {
  if (String(newVal) === String(orig) || (newVal===null && orig==='null')) {
    loadTable(currentTable, currentPage); return;
  }
  td.classList.add('saving');
  try {
    const r = await fetch(`/api/admin/table/${currentTable}/${rowPk}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN },
      body: JSON.stringify({ [col]: newVal })
    });
    if (r.ok) {
      toast(`${col} updated`, 'ok');
      loadTable(currentTable, currentPage);
    } else {
      const err = await r.json();
      toast(err.detail || 'Update failed', 'err');
      loadTable(currentTable, currentPage);
    }
  } catch(e) {
    toast('Network error', 'err');
    loadTable(currentTable, currentPage);
  }
}

// -- DELETE ------------------------------------------------
function askDelete(table, id, name) {
  pendingDelete = {table, id};
  const cascade = table === 'devices'
    ? ' This will also delete all check-ins, segments, leave records, and roles for this employee.'
    : '';
  document.getElementById('modal-msg').textContent =
    `Delete row ID ${id}${name?' ('+name+')':''}? This cannot be undone.${cascade}`;
  document.getElementById('modal').classList.add('open');
}
function closeModal() {
  document.getElementById('modal').classList.remove('open');
  pendingDelete = null;
}
async function confirmDelete() {
  if (!pendingDelete) return;
  const table = pendingDelete.table;
  const id    = pendingDelete.id;
  closeModal();
  pendingDelete = null;
  toast('Deleting...', 'ok');
  try {
    const r = await fetch(
      `/api/admin/table/${table}/${encodeURIComponent(id)}`,
      { method: 'DELETE', headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN } }
    );
    if (r.ok) {
      const data = await r.json().catch(() => ({}));
      toast(data.cascade ? 'Device and all related records deleted' : 'Row deleted', 'ok');
      loadTable(currentTable, currentPage);
      // Refresh sidebar counts
      fetch('/api/admin/tables', { headers: { 'X-Employee-Id': MY_ID, 'X-Device-Token': MY_TOKEN } })
        .then(cr => cr.ok ? cr.json() : null)
        .then(counts => { if (counts) renderSidebar(counts); });
    } else {
      const err = await r.json().catch(() => ({}));
      toast(err.detail || `Delete failed (${r.status})`, 'err');
    }
  } catch(e) {
    console.error('Delete error:', e);
    toast(`Delete error: ${e.message}`, 'err');
  }
}

// -- TOAST -------------------------------------------------
function toast(msg, type='ok') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${type}`;
  setTimeout(() => t.classList.remove('show'), 2500);
}

// -- KEYBOARD ----------------------------------------------
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeModal();
});

init();
