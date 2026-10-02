import {dateRange, sortedRows, escapeHtml as e} from './controls.mjs';

const $ = id => document.getElementById(id);
const names = {openai:'OpenAI', xai:'xAI', byteplus:'BytePlus', elevenlabs:'ElevenLabs', google:'Google Cloud', minimax:'MiniMax'};
const icons = {openai:'◎', xai:'𝕏', byteplus:'B', elevenlabs:'Ⅱ', google:'G', minimax:'M'};
const colors = {openai:'#40a88b', xai:'#526078', byteplus:'#7891ed', elevenlabs:'#ad89ce', google:'#eab967', minimax:'#ee795b'};
const unavailable = '<span class="unavailable" title="Not available from this reporting source" aria-label="Unavailable">×</span>';
const number = value => value == null ? null : Number(value);
const count = value => value == null ? unavailable : new Intl.NumberFormat('en', {maximumFractionDigits:2}).format(Number(value));
const label = value => value.replaceAll('_', ' ');
let data, view = 'provider', sort = {key:'cost', direction:'desc'}, expanded = null, requestId = 0;
const charts = {};

function money(value, currency='USD') {
  if (value == null) return unavailable;
  const n = Number(value);
  const text = Math.abs(n) > 0 && Math.abs(n) < .01 ? `${n < 0 ? '−' : ''}<${new Intl.NumberFormat('en', {style:'currency', currency}).format(.01)}` : new Intl.NumberFormat('en', {style:'currency', currency}).format(n);
  return `<span class="money" title="${e(value)} ${e(currency)}">${e(text)}</span>`;
}
function amounts(values = {}) {
  const currencies = Object.keys(values).sort((a,b) => (a === 'USD' ? -1 : b === 'USD' ? 1 : a.localeCompare(b)));
  return currencies.length ? currencies.map((c,i) => `${i ? '<span class="subvalue">' : ''}${money(values[c],c)}${i ? '</span>' : ''}`).join('') : unavailable;
}
function hasEstimate(item) { return Object.keys(item.estimated_cost).length > 0; }
function spending(item) {
  const estimated = hasEstimate(item);
  const mixed = Object.keys(item.cost).length > 0;
  const caption = estimated ? (mixed ? 'Includes estimates' : 'Estimated') : '';
  return amounts(item.spending) + (caption ? `<span class="subvalue estimate-label">${caption}${item.estimate_partial || item.unpriced_tasks ? ' · partial' : ''}</span>` : '');
}
function estimateDetail(item) {
  if (!hasEstimate(item)) return '';
  return `${Object.keys(item.cost).length ? amounts(item.cost) + ' reported + ' : ''}${amounts(item.estimated_cost)} estimated${item.estimate_partial || item.unpriced_tasks ? ' · some charges unavailable' : ''}`;
}
function stamp(value) {
  return value ? new Date(value).toLocaleString('en', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : 'Not yet reported';
}
function stateBadge(status) {
  const captions = {updated:'Updated',partial:'Partial',stale:'Stale',error:'Unavailable',not_configured:'Not connected'};
  return `<span class="status ${e(status.state)}" title="${e(status.message || stamp(status.last_success))}">${captions[status.state] || e(status.state)}</span>`;
}
function providerName(provider, expand=false) {
  const content = `<span class="provider-logo ${provider}">${icons[provider]}</span>${e(names[provider])}`;
  return `<span class="provider-name">${expand ? `<button data-expand="${provider}" aria-expanded="${expanded === provider}">${content}<span class="chevron">${expanded === provider ? '⌄' : '›'}</span></button>` : content}</span>`;
}
function headings(columns) {
  return `<thead><tr>${columns.map(([key,title,numeric]) => `<th class="${numeric ? 'numeric' : ''}" aria-sort="${sort.key === key ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}"><button class="sort-button ${sort.key === key ? 'active' : ''}" data-sort="${key}">${e(title)}<span class="arrow">${sort.key === key ? (sort.direction === 'asc' ? '↑' : '↓') : '↕'}</span></button></th>`).join('')}</tr></thead>`;
}
function metric(item) {
  const unit = $('usage-unit').value;
  if (!unit) return unavailable;
  return `${count(item.metrics[unit])}<span class="subvalue">${e(label(unit))}</span>`;
}
function value(item,key) {
  if (key === 'name') return item.name || item.provider_name;
  if (key === 'model') return item.model;
  if (key === 'cost') return number(item.spending.USD);
  if (key === 'today') return number(item.today?.spending.USD);
  if (key === 'payments') return number(item.payments?.USD);
  if (key === 'balance') return item.balance?.unit === 'USD' ? number(item.balance.amount) : null;
  if (key === 'usage') return number(item.metrics[$('usage-unit').value]);
  if (key === 'status') return item.status?.state;
  return number(item.metrics[key]);
}
function sorted(items) { return sortedRows(items,sort.key,sort.direction,value); }
function modelFilter(items) {
  const term = $('model-search').value.trim().toLowerCase();
  return items.filter(r => `${r.model} ${r.service || ''} ${(r.aliases || []).join(' ')}`.toLowerCase().includes(term));
}
function modelTable(items) {
  const columns = [['name','Provider'],['model','Model / service'],['cost','Usage cost',true],['requests','Requests',true],['generations','Generations',true],['usage','Native usage',true]];
  return `<table>${headings(columns)}<tbody>${sorted(modelFilter(items)).map(m => `<tr><td>${providerName(m.provider)}</td><td class="model-name">${e(m.model)}${m.aliases?.length ? `<span class="model-provider">Gateway: ${e(m.aliases.join(", "))}</span>` : ""}${m.service ? `<span class="model-provider">${e(m.service)}</span>` : ''}</td><td class="numeric">${spending(m)}</td><td class="numeric">${count(m.metrics.requests)}</td><td class="numeric">${count(m.metrics.generations)}</td><td class="numeric">${metric(m)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">No reported models match this selection.</td></tr>'}</tbody></table>`;
}
function renderTable() {
  if (!data) return;
  if (view === 'model') { $('breakdown-table').innerHTML = modelTable(data.models); return; }
  const columns = [['name','Provider'],['today','Today',true],['cost','Period cost',true],['requests','Requests',true],['generations','Generations',true],['usage','Native usage',true],['balance','Latest balance',true],['payments','Payments / top-ups',true],['status','Status']];
  $('breakdown-table').innerHTML = `<table>${headings(columns)}<tbody>${sorted(data.providers).map(p => {
    const balance = !p.balance ? unavailable : `${p.balance.unit === 'USD' ? money(p.balance.amount) : count(p.balance.amount)}<span class="subvalue">${e(p.balance.unit === 'USD' ? 'USD · prepaid' : p.balance.unit)} · ${e(stamp(p.balance.as_of))}</span>`;
    const sub = hasEstimate(p) ? `<span class="subvalue">${p.coverage.estimated_cost} ${p.coverage.estimated_cost === 1 ? "day" : "days"} with estimates</span>` : p.coverage.cost < p.days_requested ? `<span class="subvalue">${p.coverage.cost} / ${p.days_requested} days reported</span>` : '';
    return `<tr><td>${providerName(p.id,true)}</td><td class="numeric">${spending(p.today)}</td><td class="numeric">${spending(p)}${sub}</td><td class="numeric">${count(p.metrics.requests)}</td><td class="numeric">${count(p.metrics.generations)}</td><td class="numeric">${metric(p)}</td><td class="numeric">${balance}</td><td class="numeric">${amounts(p.payments)}</td><td>${stateBadge(p.status)}</td></tr>${expanded === p.id ? `<tr class="detail-row"><td colspan="9"><div class="detail-title">${e(p.name)} · ${e(p.time_zones.join(', ') || 'Timezone unavailable')} · ${e(p.basis.join('; ') || 'Provider-native usage')}<br>${p.unpriced_tasks ? `<div>${p.unpriced_tasks} tasks could not be priced; available usage is retained.</div>` : ""}${p.notes.map(n => `<div>${e(n)}</div>`).join('')}${adjustments(p.adjustments)}${subscription(p.balance)}${p.id === "google" ? `<div>Service subdivisions of this total: ${p.services.map(s => `${e(s.name)} ${amounts(s.cost)}`).join(" · ")}</div>` : ""}</div>${modelTable(data.models.filter(m => m.provider === p.id))}</td></tr>` : ''}`;
  }).join('')}</tbody></table>`;
}
function subscription(balance) {
  if (!balance || balance.unit !== 'credits') return '';
  const overage = balance.overage;
  return `<div>Allowance: ${count(balance.used)} / ${count(balance.limit)} credits used · resets ${e(balance.reset_at ? stamp(Number(balance.reset_at)*1000) : '×')}${overage?.amount != null && overage?.currency ? ` · reported overage ${money(overage.amount,overage.currency.toUpperCase())}` : ''}</div>`;
}
function adjustments(values) {
  const entries = Object.entries(values || {});
  return entries.length ? 'Reported adjustments: ' + entries.map(([key,v]) => `${e(label(key))} ${typeof v === 'object' ? amounts(v) : count(v)}`).join(' · ') : '';
}
function renderComparisons() {
  $('comparison-table').innerHTML = `<table><thead><tr><th>Provider</th><th class="numeric">Provider reported</th><th class="numeric">Gateway recorded</th><th class="numeric">LiteLLM logged</th><th class="numeric">Magic Lens cost</th><th>Cost differences (left − right)</th></tr></thead><tbody>${data.comparisons.map(c => `<tr><td>${providerName(c.provider)}</td>${['provider','gateway','litellm','magiclens'].map(s => `<td class="numeric">${money(c.sources[s].cost.USD)}${c.sources[s].pending ? `<span class="subvalue">${c.sources[s].pending} pending</span>` : ''}<span class="subvalue">${e(c.scopes[s].join(', ') || 'No reported coverage')}</span></td>`).join('')}<td class="difference">${Object.entries(c.differences).map(([pair,d]) => `<div>${e(pair === 'provider_gateway' ? 'Provider − Gateway' : pair === 'gateway_litellm' ? 'Gateway − LiteLLM' : 'Gateway − Magic Lens')}: ${money(d.absolute)}${d.percent != null ? ` <small>${Number(d.percent).toFixed(1)}% · ${e(d.reason)}</small>` : `<small>${e(d.reason)}</small>`}</div>`).join('')}</td></tr>`).join('')}</tbody></table>`;
}
function renderFunding() {
  $('payments-table').innerHTML = `<table><thead><tr><th>Date</th><th>Provider</th><th>Type</th><th>Description</th><th class="numeric">Amount</th></tr></thead><tbody>${data.payments.map(p => `<tr><td>${e(p.day)}</td><td>${providerName(p.provider)}</td><td>${e(p.kind === 'topup' ? 'Top-up' : label(p.kind))}</td><td>${e(p.description || '')}</td><td class="numeric">${money(p.amount,p.currency)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">× &nbsp; No payment history reported for this selection. Usage charges are shown separately above.</td></tr>'}</tbody></table>`;
}
function chart(id,type,labels,datasets,extra={}) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), {type,data:{labels,datasets},options:{responsive:true,maintainAspectRatio:false,animation:false,interaction:{mode:'index',intersect:false},plugins:{legend:{position:'bottom',labels:{boxWidth:8,boxHeight:8,usePointStyle:true,font:{size:10},padding:20}},tooltip:{callbacks:{label:ctx => `${ctx.dataset.label}: ${ctx.parsed.y ?? ctx.parsed.x}`}}},scales:{x:{grid:{display:false},border:{display:false},ticks:{font:{size:10},color:'#94a1b5',maxTicksLimit:8}},y:{beginAtZero:true,border:{display:false},grid:{color:'#f0f2f7'},ticks:{font:{size:10},color:'#94a1b5',maxTicksLimit:5}}},...extra}});
}
function renderCharts() {
  const labels = data.daily.map(d => new Date(d.day+'T12:00:00Z').toLocaleDateString('en',{month:'short',day:'numeric',timeZone:'UTC'}));
  chart('cost-chart','bar',labels,data.providers.map(p => ({label:names[p.id] + (hasEstimate(p) ? " · estimated" : ""),data:data.daily.map(d => number(d.spending_providers[p.id])),backgroundColor:colors[p.id],borderRadius:3,maxBarThickness:18})));
  $('cost-empty').textContent = data.summary.spending.USD == null ? 'No USD cost available for this period.' : 'Reported charges plus labeled estimates. Gaps indicate unavailable daily data; today may be incomplete.';
  const kind = $('volume-kind').value;
  chart('volume-chart','line',labels,[{label:label(kind),data:data.daily.map(d => number(d.metrics[kind])),borderColor:'#6586ee',backgroundColor:'#eef3ff',fill:true,borderWidth:2,pointRadius:2,tension:.2,spanGaps:false}]);
  $('volume-empty').textContent = data.summary.metrics[kind] == null ? `No ${kind} count reported for this period.` : 'Counts include only what the selected providers report.';
  renderModelChart();
}
function renderModelChart() {
  const provider = $('provider').value || expanded;
  $('model-contribution').hidden = !provider;
  if (!provider || !data) return;
  const models = data.models.filter(m => m.provider === provider && m.model !== 'Unassigned' && m.spending.USD != null).sort((a,b) => Number(b.spending.USD)-Number(a.spending.USD)).slice(0,10);
  $('model-contribution-title').textContent = `${names[provider]} · model contribution`;
  const palette = Object.values(colors);
  chart('model-chart','line',data.daily.map(d => d.day),models.map((m,i) => ({label:m.model + (hasEstimate(m) ? " · estimated" : ""),data:data.daily.map(d => number(d.spending_models[provider]?.[m.model])),borderColor:palette[i % palette.length],borderWidth:2,pointRadius:1,spanGaps:false})));
  $('model-empty').textContent = models.length ? 'Reported model costs and labeled posted-rate estimates; unassigned charges remain in the table.' : '× Model-level monetary costs are not reported by this source. Available native usage is shown in the model table.';
}
function renderSources() {
  const selected = new Set(data.providers.map(p => p.id));
  $('source-table').innerHTML = `<table><thead><tr><th>Source</th><th>Status</th><th>Last success</th><th>Retained coverage</th><th>Details</th></tr></thead><tbody>${Object.entries(data.source_status).filter(([key]) => selected.has(key.split(':')[1])).map(([key,s]) => `<tr><td>${e(key.replaceAll(':',' · '))}</td><td>${stateBadge(s)}</td><td>${e(stamp(s.last_success))}</td><td>${e(s.earliest || '×')} — ${e(s.latest || '×')}</td><td>${e(s.message || s.basis || '')}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No sources have been collected yet.</td></tr>'}</tbody></table>`;
}
function render() {
  $('today-cost').innerHTML = spending(data.today_summary);
  $('today-estimate').innerHTML = estimateDetail(data.today_summary);
  $('period-cost').innerHTML = spending(data.summary);
  $('payments-cost').innerHTML = amounts(data.payments_summary);
  $('payment-coverage').textContent = `Reported funds · ${data.providers.filter(p => Object.keys(p.payments).length).length} of ${data.providers.length} providers supplied payment amounts`;
  $('today-usage').innerHTML = `${count(data.today_summary.metrics.requests)} requests &nbsp; · &nbsp; ${count(data.today_summary.metrics.generations)} generations`;
  const complete = data.providers.filter(p => p.coverage.cost === p.days_requested).length;
  $('coverage').innerHTML = `${estimateDetail(data.summary)}<div>${complete} of ${data.providers.length} providers report costs for every selected day</div>`;
  const successes = data.providers.map(p => p.status.last_success).filter(Boolean).sort();
  if (!$('refresh').disabled) $('last-refresh').textContent = successes.length ? `Last refresh ${stamp(successes[0])}` : 'No successful refresh yet';
  const units = [...new Set(data.models.flatMap(m => Object.keys(m.metrics)))].filter(k => !['requests','generations'].includes(k)).sort();
  const previous = $('usage-unit').value;
  $('usage-unit').innerHTML = '<option value="">Choose a unit</option>' + units.map(u => `<option value="${e(u)}">${e(label(u))}</option>`).join('');
  $('usage-unit').value = units.includes(previous) ? previous : units.includes('input_tokens') ? 'input_tokens' : units[0] || '';
  $('warnings').hidden = !data.warnings.length;
  $('warnings').innerHTML = data.warnings.map(w => `<div class="notice">${e(w.message)}</div>`).join('');
  $('range-label').textContent = `${data.start} — ${data.end}`;
  $('period-label').textContent = $('period').selectedOptions[0].text.toLowerCase();
  renderTable(); renderComparisons(); renderFunding(); renderCharts(); renderSources();
  $('announcement').textContent = 'Report updated.';
}
async function load() {
  const id = ++requestId;
  const [start,end] = $('period').value === 'custom' ? [$('start').value,$('end').value] : dateRange($('period').value);
  if (!start || !end) return;
  const params = new URLSearchParams({start,end});
  if ($('provider').value) params.set('providers',$('provider').value);
  $('error').hidden = true;
  try {
    const response = await fetch('/api/report?'+params);
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || 'Unable to retrieve the report.');
    if (id !== requestId) return;
    data = body; render();
  } catch (error) {
    $('error').textContent = error.message;
    $('error').hidden = false;
  }
}
async function refresh() {
  $('refresh').disabled = true;
  const requestedAt = Date.now();
  try {
    const response = await fetch('/api/refresh',{method:'POST',headers:{'X-Dashboard-Request':'1'}});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Could not start refresh.');
    if (result.state === 'demo') { $('announcement').textContent = result.message; await load(); return; }
    $('last-refresh').textContent = 'Refresh queued…';
    let sawRunning = false;
    for (let attempt=0; attempt<240; attempt++) {
      await new Promise(resolve => setTimeout(resolve,5000));
      const statusResponse = await fetch('/api/status');
      if (!statusResponse.ok) throw new Error('Unable to read refresh status.');
      const status = await statusResponse.json();
      sawRunning ||= status.collection.state === 'running';
      if (status.collection.state === 'running') $('last-refresh').textContent = 'Refreshing reports in the background…';
      if (status.collection.state !== 'running' && (sawRunning || new Date(status.collection.finished_at).getTime() >= requestedAt)) break;
      if (attempt % 6 === 0) await load();
    }
    await load();
  } catch (error) { $('error').textContent = error.message; $('error').hidden = false; }
  finally { $('refresh').disabled = false; if (data) render(); }
}
$('period').addEventListener('change',() => { $('custom-dates').hidden = $('period').value !== 'custom'; if ($('period').value !== 'custom') load(); });
$('provider').addEventListener('change',() => {expanded = null; load();});
$('apply').addEventListener('click',load);
$('refresh').addEventListener('click',refresh);
$('model-search').addEventListener('input',renderTable);
$('usage-unit').addEventListener('change',renderTable);
$('volume-kind').addEventListener('change',renderCharts);
for (const tab of ['provider','model']) $(''+tab+'-tab').addEventListener('click',() => {
  view = tab; sort = {key:'cost',direction:'desc'};
  for (const name of ['provider','model']) $(name+'-tab').setAttribute('aria-selected',String(name === tab));
  renderTable();
});
$('breakdown-table').addEventListener('click',event => {
  const sorter = event.target.closest('[data-sort]');
  if (sorter) { const key = sorter.dataset.sort; sort = {key,direction:sort.key === key && sort.direction === 'desc' ? 'asc' : sort.key === key ? 'desc' : ['name','model','status'].includes(key) ? 'asc' : 'desc'}; renderTable(); }
  const expander = event.target.closest('[data-expand]');
  if (expander) { expanded = expanded === expander.dataset.expand ? null : expander.dataset.expand; renderTable(); renderModelChart(); }
});
const [initialStart,initialEnd] = dateRange('7');
$('start').value = initialStart; $('end').value = initialEnd;
$('start').max = initialEnd; $('end').max = initialEnd;
load();
