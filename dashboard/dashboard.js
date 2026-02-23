/* ═══════════════════════════════════════════════════════════════════
   Address Pipeline Dashboard — JavaScript
   ═══════════════════════════════════════════════════════════════════ */

// ── State ──────────────────────────────────────────────────────────

let allRows = [];        // full dataset
let filteredRows = [];   // after filters
let currentPage = 1;
let sortCol = null;
let sortAsc = true;
let charts = {};
let currentTheme = 'lloyds'; // default

// ── Theme Management ───────────────────────────────────────────────

const THEME_CHART_COLORS = {
  lloyds: {
    validated: '#006a4d', needs_review: '#d4790e', rejected: '#c6322a',
    libpostal: '#1a6fb5', geonames_scan: '#006a4d', llm: '#6a3d9a',
    accent: '#006a4d',
    labelColor: '#5a6472', gridColor: '#e0e3e8',
  },
  dark: {
    validated: '#22c55e', needs_review: '#f59e0b', rejected: '#ef4444',
    libpostal: '#3b82f6', geonames_scan: '#22c55e', llm: '#a855f7',
    accent: '#6366f1',
    labelColor: '#8b8fa3', gridColor: '#2a2e3e',
  },
};

function initTheme() {
  const saved = localStorage.getItem('dashboard-theme');
  currentTheme = saved && THEME_CHART_COLORS[saved] ? saved : 'lloyds';
  applyTheme(currentTheme);
}

function applyTheme(theme) {
  currentTheme = theme;
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('dashboard-theme', theme);
  const sel = document.getElementById('themeSelect');
  if (sel) sel.value = theme;
  // Re-render charts with new colours if data is loaded
  if (allRows.length) renderCharts();
}

document.getElementById('themeSelect').addEventListener('change', function () {
  applyTheme(this.value);
});

function themeColors() {
  return THEME_CHART_COLORS[currentTheme] || THEME_CHART_COLORS.lloyds;
}

initTheme();

const DISPLAY_COLS = [
  'address_1', 'address_2', 'address_3', 'country_code',
  'town', 'status', 'confidence_score', 'parser_source',
  'geonames_match', 'geonames_id',
  'warnings', 'review_reason',
  'mismatch_detected', 'suggested_country_code',
  'llm_calls', 'llm_prompt_tokens', 'llm_completion_tokens',
];

const COL_LABELS = {
  address_1: 'Address 1', address_2: 'Address 2', address_3: 'Address 3',
  country_code: 'CC', town: 'Town', status: 'Status',
  confidence_score: 'Confidence', parser_source: 'Parser',
  geonames_match: 'Geo Match', geonames_id: 'Geo ID',
  warnings: 'Warnings', review_reason: 'Review Reason',
  mismatch_detected: 'Mismatch', suggested_country_code: 'Sugg. CC',
  llm_calls: 'LLM Calls', llm_prompt_tokens: 'Prompt Tok', llm_completion_tokens: 'Comp. Tok',
  street: 'Street', building: 'Building', postal_code: 'Postal Code',
  normalized_town: 'Normalized Town',
};

// ── File Upload ────────────────────────────────────────────────────

const fileInput = document.getElementById('fileInput');
fileInput.addEventListener('change', handleFile);

function handleFile(e) {
  const file = e.target.files[0];
  if (!file) return;

  document.getElementById('fileName').textContent = file.name;
  const ext = file.name.split('.').pop().toLowerCase();

  if (ext === 'csv') {
    const reader = new FileReader();
    reader.onload = (ev) => {
      const text = ev.target.result;
      allRows = parseCSV(text);
      onDataLoaded();
    };
    reader.readAsText(file);
  } else {
    // Excel via SheetJS
    const reader = new FileReader();
    reader.onload = (ev) => {
      const wb = XLSX.read(ev.target.result, { type: 'array' });
      const ws = wb.Sheets[wb.SheetNames[0]];
      allRows = XLSX.utils.sheet_to_json(ws, { defval: '' });
      // Normalize keys
      allRows = allRows.map(r => {
        const o = {};
        for (const [k, v] of Object.entries(r)) o[k.trim().toLowerCase()] = String(v ?? '');
        return o;
      });
      onDataLoaded();
    };
    reader.readAsArrayBuffer(file);
  }
}

function parseCSV(text) {
  const lines = text.trim().split('\n');
  if (lines.length < 2) return [];
  const headers = parseCSVLine(lines[0]);
  return lines.slice(1).map(line => {
    const vals = parseCSVLine(line);
    const obj = {};
    headers.forEach((h, i) => obj[h.trim().toLowerCase()] = (vals[i] ?? '').trim());
    return obj;
  });
}

function parseCSVLine(line) {
  const result = [];
  let current = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"' && line[i + 1] === '"') { current += '"'; i++; }
      else if (ch === '"') inQuotes = false;
      else current += ch;
    } else {
      if (ch === '"') inQuotes = true;
      else if (ch === ',') { result.push(current); current = ''; }
      else current += ch;
    }
  }
  result.push(current);
  return result;
}

// ── Data Loaded ────────────────────────────────────────────────────

function onDataLoaded() {
  document.body.classList.add('data-loaded');
  populateCountryFilter();
  applyFilters();
  renderCharts();
}

// ── KPIs ───────────────────────────────────────────────────────────

function renderKPIs(rows) {
  const total = rows.length;
  const validated = rows.filter(r => r.status === 'validated').length;
  const review = rows.filter(r => r.status === 'needs_review').length;
  const rejected = rows.filter(r => r.status === 'rejected').length;
  const det = rows.filter(r => r.parser_source && r.parser_source !== 'llm').length;
  const llm = rows.filter(r => r.parser_source === 'llm').length;
  const tokens = rows.reduce((s, r) => s + num(r.llm_prompt_tokens) + num(r.llm_completion_tokens), 0);

  setText('kpiTotal', fmt(total));
  setText('kpiValidated', fmt(validated));
  setText('kpiValidatedPct', pct(validated, total));
  setText('kpiReview', fmt(review));
  setText('kpiReviewPct', pct(review, total));
  setText('kpiRejected', fmt(rejected));
  setText('kpiRejectedPct', pct(rejected, total));
  setText('kpiDeterministic', fmt(det));
  setText('kpiDeterministicPct', pct(det, total));
  setText('kpiLLM', fmt(llm));
  setText('kpiLLMPct', pct(llm, total));
  setText('kpiTokens', fmt(tokens));
}

// ── Charts ─────────────────────────────────────────────────────────

function renderCharts() {
  renderStatusChart();
  renderParserChart();
  renderConfidenceChart();
  renderCountryChart();
}

function destroyChart(key) {
  if (charts[key]) { charts[key].destroy(); delete charts[key]; }
}

function renderStatusChart() {
  destroyChart('status');
  const tc = themeColors();
  const counts = countBy(filteredRows, 'status');
  const labels = Object.keys(counts);
  charts.status = new Chart(document.getElementById('chartStatus'), {
    type: 'doughnut',
    data: {
      labels: labels.map(l => l.replace('_', ' ')),
      datasets: [{
        data: labels.map(l => counts[l]),
        backgroundColor: labels.map(l => tc[l] || tc.accent),
        borderWidth: 0,
      }],
    },
    options: chartOpts(),
  });
}

function renderParserChart() {
  destroyChart('parser');
  const tc = themeColors();
  const counts = countBy(filteredRows, 'parser_source');
  const labels = Object.keys(counts);
  charts.parser = new Chart(document.getElementById('chartParser'), {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: labels.map(l => counts[l]),
        backgroundColor: labels.map(l => tc[l] || tc.accent),
        borderWidth: 0,
      }],
    },
    options: chartOpts(),
  });
}

function renderConfidenceChart() {
  destroyChart('confidence');
  const tc = themeColors();
  const buckets = { '0.00': 0, '0.40': 0, '0.70': 0, '0.75': 0, '0.80': 0, '0.95': 0, '1.00': 0 };
  filteredRows.forEach(r => {
    const v = parseFloat(r.confidence_score) || 0;
    if (v >= 1.0)       buckets['1.00']++;
    else if (v >= 0.95) buckets['0.95']++;
    else if (v >= 0.80) buckets['0.80']++;
    else if (v >= 0.75) buckets['0.75']++;
    else if (v >= 0.70) buckets['0.70']++;
    else if (v >= 0.40) buckets['0.40']++;
    else                buckets['0.00']++;
  });
  const labels = Object.keys(buckets);
  const confColors = currentTheme === 'lloyds'
    ? ['#c6322a', '#d4790e', '#b8960a', '#5a9a30', '#006a4d', '#008b5c', '#004b35']
    : ['#ef4444', '#f59e0b', '#eab308', '#84cc16', '#22c55e', '#10b981', '#059669'];
  charts.confidence = new Chart(document.getElementById('chartConfidence'), {
    type: 'bar',
    data: {
      labels: labels.map(l => `≥${l}`),
      datasets: [{
        data: labels.map(l => buckets[l]),
        backgroundColor: confColors,
        borderRadius: 4,
      }],
    },
    options: {
      ...chartOpts(),
      plugins: { ...chartOpts().plugins, legend: { display: false } },
      scales: {
        x: { ticks: { color: tc.labelColor, font: { size: 11 } }, grid: { display: false } },
        y: { ticks: { color: tc.labelColor, font: { size: 11 } }, grid: { color: tc.gridColor } },
      },
    },
  });
}

function renderCountryChart() {
  destroyChart('country');
  const tc = themeColors();
  const counts = countBy(filteredRows, 'country_code');
  const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 10);
  const labels = sorted.map(s => s[0] || '(empty)');
  const data = sorted.map(s => s[1]);
  charts.country = new Chart(document.getElementById('chartCountry'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: tc.accent,
        borderRadius: 4,
      }],
    },
    options: {
      ...chartOpts(),
      indexAxis: 'y',
      plugins: { ...chartOpts().plugins, legend: { display: false } },
      scales: {
        x: { ticks: { color: tc.labelColor, font: { size: 11 } }, grid: { color: tc.gridColor } },
        y: { ticks: { color: tc.labelColor, font: { size: 11 } }, grid: { display: false } },
      },
    },
  });
}

function chartOpts() {
  const tc = themeColors();
  return {
    responsive: true,
    maintainAspectRatio: true,
    plugins: {
      legend: {
        position: 'bottom',
        labels: { color: tc.labelColor, font: { size: 11 }, padding: 12, usePointStyle: true },
      },
    },
  };
}

// ── Filters ────────────────────────────────────────────────────────

function populateCountryFilter() {
  const sel = document.getElementById('filterCountry');
  const countries = [...new Set(allRows.map(r => r.country_code).filter(Boolean))].sort();
  sel.innerHTML = countries.map(c => {
    const safe = escapeHtml(c);
    return `<option value="${safe}" selected>${safe}</option>`;
  }).join('');
}

function getSelectedValues(id) {
  return [...document.getElementById(id).selectedOptions].map(o => o.value);
}

function applyFilters() {
  const statuses = getSelectedValues('filterStatus');
  const parsers = getSelectedValues('filterParser');
  const countries = getSelectedValues('filterCountry');
  const geoMatch = document.getElementById('filterGeoMatch').value;
  const mismatch = document.getElementById('filterMismatch').value;
  const warnings = document.getElementById('filterWarnings').value;
  const confMin = parseInt(document.getElementById('filterConfMin').value) / 100;
  const search = document.getElementById('filterSearch').value.toLowerCase().trim();

  filteredRows = allRows.filter(r => {
    if (statuses.length && !statuses.includes(r.status)) return false;
    if (parsers.length && !parsers.includes(r.parser_source)) return false;
    if (countries.length && !countries.includes(r.country_code)) return false;
    if (geoMatch && r.geonames_match !== geoMatch) return false;
    if (mismatch && r.mismatch_detected !== mismatch) return false;
    if (warnings === 'yes' && !r.warnings) return false;
    if (warnings === 'no' && r.warnings) return false;
    if (confMin > 0 && (parseFloat(r.confidence_score) || 0) < confMin) return false;
    if (search) {
      const hay = [r.address_1, r.address_2, r.address_3, r.town, r.country_code, r.warnings, r.review_reason]
        .join(' ').toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  });

  currentPage = 1;
  renderKPIs(filteredRows);
  renderCharts();
  renderTable();
  setText('filterCount', `${fmt(filteredRows.length)} of ${fmt(allRows.length)} rows`);
}

document.getElementById('btnApply').addEventListener('click', applyFilters);
document.getElementById('btnReset').addEventListener('click', () => {
  document.querySelectorAll('.filter-group select[multiple] option').forEach(o => o.selected = true);
  document.getElementById('filterGeoMatch').value = '';
  document.getElementById('filterMismatch').value = '';
  document.getElementById('filterWarnings').value = '';
  document.getElementById('filterConfMin').value = 0;
  document.getElementById('filterConfMinVal').textContent = '0.00';
  document.getElementById('filterSearch').value = '';
  applyFilters();
});

// Confidence range label
document.getElementById('filterConfMin').addEventListener('input', function () {
  document.getElementById('filterConfMinVal').textContent = (this.value / 100).toFixed(2);
});

// Live search
document.getElementById('filterSearch').addEventListener('input', debounce(applyFilters, 300));

// Page size change
document.getElementById('pageSize').addEventListener('change', () => { currentPage = 1; renderTable(); });

// ── Table Rendering ────────────────────────────────────────────────

function renderTable() {
  const pageSize = parseInt(document.getElementById('pageSize').value);
  const rows = getSortedRows();
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  if (currentPage > totalPages) currentPage = totalPages;

  const start = (currentPage - 1) * pageSize;
  const pageRows = rows.slice(start, start + pageSize);

  // Determine which columns to show (only cols present in data)
  const cols = DISPLAY_COLS.filter(c => allRows.length === 0 || allRows[0].hasOwnProperty(c));

  // Header
  const thead = document.getElementById('tableHead');
  thead.innerHTML = '<th class="row-num">#</th>' + cols.map(c => {
    const arrow = sortCol === c ? (sortAsc ? ' ▲' : ' ▼') : '';
    return `<th data-col="${c}">${COL_LABELS[c] || c}<span class="sort-arrow">${arrow}</span></th>`;
  }).join('');

  // Body
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = pageRows.map((r, i) => {
    const idx = start + i + 1;
    return `<tr data-idx="${start + i}">
      <td class="row-num">${idx}</td>
      ${cols.map(c => `<td>${renderCell(c, r[c], r)}</td>`).join('')}
    </tr>`;
  }).join('');

  // Count
  setText('tableCount', `(${fmt(rows.length)} rows)`);

  // Pagination
  renderPagination(totalPages);

  // Sort handlers
  thead.querySelectorAll('th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortCol === col) sortAsc = !sortAsc;
      else { sortCol = col; sortAsc = true; }
      renderTable();
    });
  });

  // Row click → modal
  tbody.querySelectorAll('tr').forEach(tr => {
    tr.addEventListener('click', () => openModal(filteredRows[parseInt(tr.dataset.idx)]));
  });
}

function renderCell(col, val, row) {
  if (val === undefined || val === null || val === '') return '<span style="color:#555">—</span>';

  if (col === 'status') {
    const safe = escapeHtml(val);
    const cls = sanitizeCssClass(val);
    return `<span class="badge badge-${cls}">${safe.replace('_', ' ')}</span>`;
  }
  if (col === 'parser_source') {
    const safe = escapeHtml(val);
    const cls = sanitizeCssClass(val);
    return `<span class="badge badge-${cls}">${safe}</span>`;
  }
  if (col === 'confidence_score') {
    const v = parseFloat(val) || 0;
    const tc = themeColors();
    const color = v >= 0.75 ? tc.validated : v >= 0.40 ? tc.needs_review : tc.rejected;
    const w = Math.min(100, Math.max(0, Math.round(v * 100)));
    return `<span class="conf-bar">
      <span class="conf-bar-bg"><span class="conf-bar-fill" style="width:${w}%;background:${color}"></span></span>
      ${v.toFixed(2)}
    </span>`;
  }
  if (col === 'geonames_match' || col === 'mismatch_detected') {
    return val === 'True' ? '✅' : val === 'False' ? '—' : escapeHtml(val);
  }
  if (col === 'llm_calls' || col === 'llm_prompt_tokens' || col === 'llm_completion_tokens') {
    const n = parseInt(val) || 0;
    return n === 0 ? '<span style="color:#555">0</span>' : fmt(n);
  }

  return escapeHtml(val);
}

function getSortedRows() {
  if (!sortCol) return [...filteredRows];
  return [...filteredRows].sort((a, b) => {
    let va = a[sortCol] ?? '';
    let vb = b[sortCol] ?? '';
    // Try numeric
    const na = parseFloat(va), nb = parseFloat(vb);
    if (!isNaN(na) && !isNaN(nb)) return sortAsc ? na - nb : nb - na;
    // String
    va = va.toString().toLowerCase();
    vb = vb.toString().toLowerCase();
    return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
  });
}

// ── Pagination ─────────────────────────────────────────────────────

function renderPagination(totalPages) {
  const container = document.getElementById('pagination');
  if (totalPages <= 1) { container.innerHTML = ''; return; }

  let html = '';
  html += `<button class="page-btn" ${currentPage === 1 ? 'disabled' : ''} data-page="${currentPage - 1}">‹ Prev</button>`;

  const pages = getPaginationRange(currentPage, totalPages);
  for (const p of pages) {
    if (p === '...') {
      html += `<span class="page-info">…</span>`;
    } else {
      html += `<button class="page-btn ${p === currentPage ? 'active' : ''}" data-page="${p}">${p}</button>`;
    }
  }

  html += `<button class="page-btn" ${currentPage === totalPages ? 'disabled' : ''} data-page="${currentPage + 1}">Next ›</button>`;
  html += `<span class="page-info">Page ${currentPage} of ${totalPages}</span>`;

  container.innerHTML = html;
  container.querySelectorAll('.page-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = parseInt(btn.dataset.page);
      if (p >= 1 && p <= totalPages) { currentPage = p; renderTable(); }
    });
  });
}

function getPaginationRange(current, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages = [];
  pages.push(1);
  if (current > 3) pages.push('...');
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i);
  if (current < total - 2) pages.push('...');
  pages.push(total);
  return pages;
}

// ── Row Detail Modal ───────────────────────────────────────────────

function openModal(row) {
  if (!row) return;
  const body = document.getElementById('modalBody');
  const allCols = Object.keys(row);

  let html = '<div class="detail-grid">';
  for (const col of allCols) {
    const label = COL_LABELS[col] || col;
    let val = row[col];
    // Render badges in modal too — always sanitise data-derived values
    if (col === 'status') {
      const cls = sanitizeCssClass(val);
      val = `<span class="badge badge-${cls}">${escapeHtml((val || '').replace('_', ' '))}</span>`;
    }
    else if (col === 'parser_source' && val) {
      const cls = sanitizeCssClass(val);
      val = `<span class="badge badge-${cls}">${escapeHtml(val)}</span>`;
    }
    else if (col === 'confidence_score') {
      const v = parseFloat(val) || 0;
      const tc = themeColors();
      const color = v >= 0.75 ? tc.validated : v >= 0.40 ? tc.needs_review : tc.rejected;
      val = `<span style="color:${color};font-weight:600">${v.toFixed(2)}</span>`;
    }
    else val = escapeHtml(val || '—');

    html += `<div class="detail-label">${escapeHtml(label)}</div><div class="detail-value">${val}</div>`;
  }
  html += '</div>';
  body.innerHTML = html;
  document.getElementById('modalOverlay').classList.add('open');
}

document.getElementById('modalClose').addEventListener('click', () => {
  document.getElementById('modalOverlay').classList.remove('open');
});
document.getElementById('modalOverlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) document.getElementById('modalOverlay').classList.remove('open');
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.getElementById('modalOverlay').classList.remove('open');
});

// ── Export Filtered CSV ────────────────────────────────────────────

document.getElementById('btnExport').addEventListener('click', () => {
  if (!filteredRows.length) return;
  const cols = Object.keys(filteredRows[0]);
  const lines = [cols.join(',')];
  for (const r of filteredRows) {
    lines.push(cols.map(c => {
      let v = r[c] ?? '';
      if (v.includes(',') || v.includes('"') || v.includes('\n')) {
        v = '"' + v.replace(/"/g, '""') + '"';
      }
      return v;
    }).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  const url = URL.createObjectURL(blob);
  a.href = url;
  a.download = 'filtered_results.csv';
  a.click();
  // Release blob memory after download triggers
  setTimeout(() => URL.revokeObjectURL(url), 5000);
});

// ── Utilities ──────────────────────────────────────────────────────

function setText(id, text) { document.getElementById(id).textContent = text; }
function num(v) { return parseInt(v) || 0; }
function fmt(n) { return n.toLocaleString(); }
function pct(n, total) { return total ? `${(100 * n / total).toFixed(1)}%` : ''; }
function countBy(rows, key) {
  const m = {};
  rows.forEach(r => { const v = r[key] || '(empty)'; m[v] = (m[v] || 0) + 1; });
  return m;
}
function escapeHtml(s) {
  if (!s) return '';
  return s.toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
/** Strip everything except [a-zA-Z0-9_-] to prevent CSS class injection */
function sanitizeCssClass(s) {
  if (!s) return '';
  return s.toString().replace(/[^a-zA-Z0-9_-]/g, '');
}
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
