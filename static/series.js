let seriesList = [], seriesBoardData = null, seriesDraft = [], seriesEditing = null;
let seriesRequest = 0, seriesSearchRequest = 0, seriesSort = {key: 'gmv', direction: -1};

async function seriesAPI(path, payload) {
  const response = await fetch(path, payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : {});
  const result = await response.json();
  if (!result.ok) throw Error(result.error);
  return result.data;
}

function seriesError(error) {
  $('#series-coverage-wrap').classList.add('error');
  $('#series-coverage').textContent = error.message;
}

function clearSeriesResults() {
  seriesBoardData = null;
  for (const id of ['business-core', 'promotion-core', 'group-metrics']) {
    $(`#series-${id}`).innerHTML = '<div class="baby-empty">暂无数据</div>';
  }
  $('#series-trend').innerHTML = '<div class="empty">暂无趋势数据</div>';
  $('#series-rows').innerHTML = '<tr><td colspan="11" class="empty">暂无商品数据</td></tr>';
  $('#series-scope').hidden = true;
  $('#series-quality-note').hidden = true;
  $('#series-member-count').textContent = '0 个商品';
  $('#series-coverage-wrap').classList.remove('error', 'warning');
}

function selectedSeries() {
  return seriesList.find(item => String(item.id) === $('#series-select').value);
}

function renderSeriesVersions(preferred) {
  const item = selectedSeries();
  $('#series-edit').disabled = !item;
  $('#series-refresh').disabled = !item;
  $('#series-version').innerHTML = item ? item.versions.map(version =>
    `<option value="${version.version}">V${version.version}${version.version === item.current_version ? ' · 当前' : ' · 历史'} · ${version.product_ids.length} 个商品 · ${escapeHtml(version.created_at)}</option>`
  ).join('') : '';
  if (item?.versions.some(version => String(version.version) === String(preferred))) $('#series-version').value = String(preferred);
}

async function loadSeries(preferred) {
  const account = selectedAccountId, token = ++seriesRequest;
  if (!account) return;
  const prior = preferred ?? $('#series-select').value;
  const items = await seriesAPI(`/api/series?account_id=${encodeURIComponent(account)}`);
  if (account !== selectedAccountId || token !== seriesRequest) return;
  seriesList = items;
  $('#series-select').innerHTML = '<option value="">请选择系列</option>' + items.map(item =>
    `<option value="${item.id}">${escapeHtml(item.name)} · ${item.product_ids.length} 个商品</option>`).join('');
  if (items.some(item => String(item.id) === String(prior))) $('#series-select').value = String(prior);
  renderSeriesVersions();
  await loadDataGroups('series');
  if (account !== selectedAccountId || token !== seriesRequest) return;
  if (selectedSeries()) await loadSeriesBoard();
  else { clearSeriesResults(); $('#series-coverage').textContent = items.length ? '请选择系列' : '当前店铺尚未创建系列'; }
}

async function loadSeriesBoard() {
  const item = selectedSeries(), account = selectedAccountId, token = ++seriesRequest;
  clearSeriesResults();
  if (!item || !account) return;
  const params = new URLSearchParams({account_id: account, series_id: item.id, version: $('#series-version').value,
    start_date: $('#series-start-date').value, end_date: $('#series-end-date').value});
  $('#series-coverage').textContent = '正在读取系列数据...';
  $('#series-refresh').disabled = true;
  try {
    const data = await seriesAPI(`/api/series-board?${params}`);
    if (account !== selectedAccountId || token !== seriesRequest) return;
    seriesBoardData = data;
    renderSeriesBoard();
  } catch (error) {
    if (account === selectedAccountId && token === seriesRequest) seriesError(error);
  } finally {
    if (token === seriesRequest) $('#series-refresh').disabled = !selectedSeries();
  }
}

function seriesMetricValue(code, value) {
  const metric = dataGroupState.series?.metrics.find(item => item.metric_code === code);
  if (value == null) return '暂无';
  return metric?.unit === 'amount' ? money(value) : metric?.unit === 'percent' ? pct(value) : metric?.unit === 'ratio' ? Number(value).toFixed(2) : num(value);
}

function seriesMetricCard(code) {
  const definition = dataGroupState.series?.metrics.find(item => item.metric_code === code);
  if (!definition) return '';
  const people = ['visitors', 'paid_buyers', 'cart_people', 'conversion_rate'].includes(code);
  const note = people ? '商品日合计，非系列/周期去重' : definition.formula;
  return babyCard(escapeHtml(definition.label), seriesMetricValue(code, seriesBoardData.metrics[code]), escapeHtml(note), code);
}

function renderSeriesGroup() {
  const state = dataGroupState.series;
  if (!state || !seriesBoardData) return;
  const group = state.groups.find(item => String(item.id) === $('#series-data-group').value);
  $('#series-group-metrics').innerHTML = group?.items.map(item => item.available ? seriesMetricCard(item.metric_code) :
    babyCard(escapeHtml(item.label), '不可用', '当前看板不支持该指标', item.metric_code)).join('') || '<div class="baby-empty">暂无指标</div>';
}

function renderSeriesBoard() {
  const data = seriesBoardData, quality = data.quality;
  $('#series-business-core').innerHTML = quality.has_business ? ['gmv', 'gsv', 'paid_units', 'successful_refund_amount', 'refund_rate', 'conversion_rate'].map(seriesMetricCard).join('') : '<div class="baby-empty">所选成员在该日期范围无经营记录</div>';
  $('#series-promotion-core').innerHTML = quality.has_promotion ? ['spend', 'clicks', 'ppc', 'click_rate', 'attributed_deal_amount', 'roi'].map(seriesMetricCard).join('') : '<div class="baby-empty">无推广记录</div>';
  $('#series-scope').hidden = false;
  $('#series-scope').textContent = `整个周期按 V${data.member_version}${data.member_version === data.series.current_version ? ' 当前' : ' 历史'}成员集合计算：${data.product_ids.join('、')}。成员版本不冻结来源数据，重新导入生效后数值可能变化。`;
  const hasGap = quality.business_product_days < quality.expected_product_days || quality.promotion_product_days < quality.expected_product_days;
  $('#series-coverage-wrap').classList.toggle('warning', hasGap || (quality.has_promotion && !quality.attribution_windows_compatible));
  $('#series-coverage').textContent = `${data.start_date} 至 ${data.end_date} · ${data.product_ids.length} 个成员 · 经营覆盖 ${quality.business_product_days}/${quality.expected_product_days} 商品日 · 推广覆盖 ${quality.promotion_product_days}/${quality.expected_product_days} 商品日`;
  const notes = [quality.people_note, quality.contribution_note, '生意参谋实际支付与无界归因成交分开展示；重叠系列不可相加作为店铺总计。'];
  if (hasGap) notes.push('覆盖不足：未记录的商品日无法判断为零；现有汇总不是完整周期经营证明。');
  if (quality.has_promotion && !quality.attribution_windows_compatible) notes.push('归因窗口不兼容或未知且跨多个批次，合并归因成交及 ROI 暂不计算。');
  $('#series-quality-note').hidden = false;
  $('#series-quality-note').textContent = notes.join(' ');
  const oldMetric = $('#series-trend-metric').value;
  const metrics = dataGroupState.series?.metrics.filter(item => item.available) || [];
  $('#series-trend-metric').innerHTML = metrics.map(item => `<option value="${item.metric_code}">${escapeHtml(item.label)}</option>`).join('');
  if (metrics.some(item => item.metric_code === oldMetric)) $('#series-trend-metric').value = oldMetric;
  renderSeriesGroup(); renderSeriesTrend(); renderSeriesRows();
}

function renderSeriesTrend() {
  const rows = seriesBoardData?.trend || [], code = $('#series-trend-metric').value;
  const values = rows.map(row => row.metrics[code]).filter(value => value != null);
  if (!values.length) { $('#series-trend').innerHTML = '<div class="empty">该指标暂无趋势数据</div>'; return; }
  const width = 1000, height = 240, left = 76, right = 20, top = 20, bottom = 34;
  const max = Math.max(1, ...values), min = Math.min(0, ...values);
  const x = index => left + (rows.length === 1 ? (width-left-right)/2 : index*(width-left-right)/(rows.length-1));
  const y = value => top + (max-value)*(height-top-bottom)/(max-min);
  let connected = false;
  const path = rows.map((row, index) => {
    const value = row.metrics[code];
    if (value == null) { connected = false; return ''; }
    const command = connected ? 'L' : 'M'; connected = true;
    return `${command}${x(index)},${y(value)}`;
  }).join(' ');
  $('#series-trend').innerHTML = `<svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="系列趋势图">${[0,.25,.5,.75,1].map(t => {
    const value = min + (max-min)*t;
    return `<line class="chart-grid" x1="${left}" x2="${width-right}" y1="${y(value)}" y2="${y(value)}"/><text class="chart-axis" x="${left-8}" y="${y(value)+3}" text-anchor="end">${seriesMetricValue(code,value)}</text>`;
  }).join('')}<path class="plan-line" d="${path}"/>${rows.map((row,index) => row.metrics[code] == null ? '' : `<circle class="plan-point" cx="${x(index)}" cy="${y(row.metrics[code])}" r="3"><title>${row.date} · ${seriesMetricValue(code,row.metrics[code])}</title></circle>`).join('')}${rows.map((row,index) => index === 0 || index === rows.length-1 || rows.length <= 10 ? `<text class="chart-axis" x="${x(index)}" y="${height-10}" text-anchor="middle">${row.date.slice(5)}</text>` : '').join('')}</svg>`;
}

function renderSeriesRows() {
  if (!seriesBoardData) return;
  const rows = [...seriesBoardData.rows].sort((a,b) => a[seriesSort.key] == null ? (b[seriesSort.key] == null ? 0 : 1) : b[seriesSort.key] == null ? -1 : (a[seriesSort.key]-b[seriesSort.key])*seriesSort.direction);
  $('#series-rows').innerHTML = rows.map(row => `<tr><td><button type="button" class="series-product-link" data-product-id="${escapeHtml(row.product_id)}">${escapeHtml(row.product_name || '未命名商品')}<br><span class="muted">${escapeHtml(row.product_id)}</span></button></td><td>${row.has_business ? '有经营记录' : '无经营记录'}<br>${row.has_promotion ? '有推广记录' : '无推广记录'}</td><td>${money(row.gmv)}</td><td>${pct(row.sales_share)}</td><td>${num(row.paid_units)}</td><td>${pct(row.conversion_rate)}</td><td>${money(row.spend)}</td><td>${pct(row.spend_share)}</td><td>${num(row.clicks)}</td><td>${money(row.attributed_deal_amount)}</td><td>${seriesMetricValue('roi',row.roi)}</td></tr>`).join('');
  $('#series-member-count').textContent = `${rows.length} 个商品`;
  document.querySelectorAll('#series-page .sortable').forEach(button => {
    button.classList.toggle('active', button.dataset.sort === seriesSort.key);
    button.setAttribute('aria-label', `${button.textContent}排序`);
  });
}

function renderSeriesDraft() {
  $('#series-draft-count').textContent = `${seriesDraft.length} 个商品`;
  $('#series-draft-members').innerHTML = seriesDraft.map(item => `<div class="series-member"><span>${escapeHtml(item.product_name || '商品')}<br><small>${escapeHtml(item.product_id)}</small></span><button type="button" class="secondary" data-remove="${escapeHtml(item.product_id)}" aria-label="移除 ${escapeHtml(item.product_id)}">移除</button></div>`).join('') || '<div class="empty">暂无成员</div>';
}

function editorNote(message) { $('#series-editor-note').hidden = !message; $('#series-editor-note').textContent = message; }

async function searchSeriesProducts() {
  const account = selectedAccountId, token = ++seriesSearchRequest;
  const keyword = $('#series-product-search').value.trim();
  const response = await fetch(`/api/baby-products?account_id=${encodeURIComponent(account)}&keyword=${encodeURIComponent(keyword)}`);
  const result = await response.json();
  if (token !== seriesSearchRequest || account !== selectedAccountId || !$('#series-editor').open) return;
  if (!result.ok) throw Error(result.error);
  $('#series-product-results').innerHTML = result.products.map(item => `<button type="button" class="product-option" data-add="${escapeHtml(item.product_id)}" data-name="${escapeHtml(item.product_name || '')}"><b>${escapeHtml(item.product_name || '未命名商品')}</b><small>${escapeHtml(item.product_id)}</small></button>`).join('') || '<div class="empty">当前店铺未找到商品</div>';
}

function addSeriesMember(item) {
  if (seriesDraft.some(member => member.product_id === item.product_id)) { editorNote('该商品已经在系列内'); return; }
  if (seriesDraft.length >= 500) { editorNote('一个系列最多 500 个商品'); return; }
  seriesDraft.push(item); editorNote(''); renderSeriesDraft();
}

function openSeriesEditor(item) {
  seriesEditing = item ? {...item, account_id: selectedAccountId} : {account_id: selectedAccountId};
  seriesDraft = item ? item.product_ids.map(product_id => ({product_id, product_name: seriesBoardData?.rows.find(row => row.product_id === product_id)?.product_name})) : [];
  $('#series-name').value = item?.name || '';
  $('#series-editor-title').textContent = item ? '管理系列' : '创建系列';
  $('#series-delete').hidden = !item;
  $('#series-product-search').value = '';
  $('#series-product-results').innerHTML = '';
  editorNote(''); renderSeriesDraft(); $('#series-editor').showModal();
  searchSeriesProducts().catch(error => editorNote(error.message));
}

$('#series-new').onclick = () => { if (selectedAccountId) openSeriesEditor(); else seriesError(Error('请先选择店铺')); };
$('#series-edit').onclick = () => openSeriesEditor(selectedSeries());
for (const id of ['close','cancel']) $(`#series-${id}`).onclick = () => $('#series-editor').close();
$('#series-select').onchange = () => { renderSeriesVersions(); loadSeriesBoard(); };
$('#series-version').onchange = loadSeriesBoard;
$('#series-refresh').onclick = loadSeriesBoard;
$('#series-start-date').onchange = loadSeriesBoard;
$('#series-end-date').onchange = loadSeriesBoard;
$('#series-trend-metric').onchange = renderSeriesTrend;
$('#series-product-search').oninput = () => searchSeriesProducts().catch(error => editorNote(error.message));
$('#series-product-results').onclick = event => {
  const button = event.target.closest('[data-add]');
  if (button) addSeriesMember({product_id: button.dataset.add, product_name: button.dataset.name});
};
$('#series-add-id').onclick = async () => {
  const product_id = $('#series-product-search').value.trim(), account = selectedAccountId;
  if (!/^[0-9]+$/.test(product_id)) { editorNote('请输入完整商品 ID，或从搜索结果选择商品'); return; }
  try {
    const response = await fetch(`/api/baby-products?account_id=${encodeURIComponent(account)}&keyword=${encodeURIComponent(product_id)}`);
    const result = await response.json();
    if (account !== selectedAccountId || !$('#series-editor').open) return;
    if (!result.ok) throw Error(result.error);
    const item = result.products.find(product => product.product_id === product_id);
    if (!item) throw Error('当前店铺未找到该商品 ID');
    addSeriesMember(item);
  } catch (error) { editorNote(error.message); }
};
$('#series-draft-members').onclick = event => {
  const button = event.target.closest('[data-remove]');
  if (!button) return;
  if (seriesDraft.length === 1) { editorNote('系列需至少保留一个商品；不再使用请删除系列'); return; }
  seriesDraft = seriesDraft.filter(item => item.product_id !== button.dataset.remove); renderSeriesDraft();
};
$('#series-form').onsubmit = async event => {
  event.preventDefault();
  const editing = seriesEditing, account = selectedAccountId;
  if (!seriesDraft.length) { editorNote('请至少添加一个商品'); return; }
  const ids = seriesDraft.map(item => item.product_id);
  const changed = editing.id && (ids.length !== editing.product_ids.length || ids.some(id => !editing.product_ids.includes(id)));
  if (changed && !confirm('修改成员将生成新版本，历史日期默认按新成员重新计算。旧版本可复查。确认保存？')) return;
  $('#series-save').disabled = true;
  try {
    const saved = await seriesAPI('/api/series', {account_id: editing.account_id, series_id: editing.id, revision: editing.revision, name: $('#series-name').value.trim(), product_ids: ids});
    if (account !== selectedAccountId || seriesEditing !== editing) return;
    $('#series-editor').close(); clearSeriesResults(); await loadSeries(saved.id);
  } catch (error) { if (seriesEditing === editing) editorNote(error.message); }
  finally { $('#series-save').disabled = false; }
};
$('#series-delete').onclick = async () => {
  const editing = seriesEditing, account = selectedAccountId;
  if (!editing.id || !confirm(`删除系列“${editing.name}”？商品与原始数据不受影响。`)) return;
  $('#series-delete').disabled = true;
  try {
    await seriesAPI('/api/series/delete', {account_id: editing.account_id, series_id: editing.id, revision: editing.revision});
    if (account !== selectedAccountId || seriesEditing !== editing) return;
    $('#series-editor').close(); clearSeriesResults(); await loadSeries('');
  } catch (error) { if (seriesEditing === editing) editorNote(error.message); }
  finally { $('#series-delete').disabled = false; }
};
$('#series-page thead').onclick = event => {
  const button = event.target.closest('[data-sort]');
  if (!button) return;
  seriesSort = {key: button.dataset.sort, direction: seriesSort.key === button.dataset.sort ? -seriesSort.direction : -1};
  renderSeriesRows();
};
$('#series-rows').onclick = async event => {
  const button = event.target.closest('[data-product-id]');
  if (!button || !seriesBoardData) return;
  const item = seriesBoardData.rows.find(row => row.product_id === button.dataset.productId);
  selectedBabyProduct = null;
  setBabyPeriod('custom');
  $('#baby-start-date').value = seriesBoardData.start_date;
  $('#baby-end-date').value = seriesBoardData.end_date;
  activatePage('baby-page');
  await loadDataGroups('baby');
  selectBabyProduct(item).catch(() => {});
};

const baseSelectAccount = selectAccount;
selectAccount = function(accountId) {
  const oldAccount = selectedAccountId;
  const result = baseSelectAccount(accountId);
  if (!result) return result;
  if (oldAccount !== selectedAccountId) {
    seriesRequest++; seriesSearchRequest++; seriesList = []; seriesEditing = null;
    $('#series-editor').close(); clearSeriesResults(); dataGroupState.series = null;
    $('#series-select').innerHTML = '<option value="">请选择系列</option>';
    $('#series-data-group').innerHTML = ''; $('#series-trend-metric').innerHTML = '';
    renderSeriesVersions(); $('#series-coverage').textContent = '店铺已切换，请选择系列';
  }
  if ($('#series-page').classList.contains('active')) loadSeries().catch(seriesError);
  return result;
};
document.querySelector('.nav-series').onclick = () => { activatePage('series-page'); loadSeries().catch(seriesError); };
clearSeriesResults();
