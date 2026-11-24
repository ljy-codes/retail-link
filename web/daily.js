/* Dated price and sales ledger. Kept independent from report snapshot state. */
(() => {
  'use strict';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const format = value => value === null || value === undefined || value === '' ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:2});
  const today = () => {const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;};
  const stamp = value => value ? new Date(value).toLocaleString('zh-CN',{hour12:false}) : '—';
  const fields = {date:'业务日期',product:'商品标识',name:'商品名称',shop:'店铺／仓库',price:'当日售价',salesQuantity:'当日销量',specification:'规格',sourceSku:'来源SKU',sourceSystem:'来源系统',warehouseId:'仓库ID'};
  const state = {tab:'records',items:[],total:0,offset:0,limit:200,hasMore:false,start:'',end:'',product:'',shop:'',deleted:false,trendKey:'',busy:false,notice:'',error:false,editing:null,inspection:null,importData:null,preview:null,previewSettings:null,previewKey:null,previewOffset:0,previewFilter:'all',importEpoch:0,recordsEpoch:0,actionBusy:false,catalogItems:[],catalogBusy:false,catalogEditing:null,catalogFile:null,catalogSheet:'',catalogConflict:'skip',catalogPreview:null,catalogSettings:null,catalogKey:null,catalogOffset:0,catalogFilter:'all',catalogEpoch:0};
  const seriesKey = item => [item.product,item.specification||'',item.source?.catalog?'':item.sourceSku||'',item.source?.catalog?'':item.sourceSystem||'',item.warehouseId||item.shop].join('\u0000');
  let root, previousOverflow='';
  const $ = selector => root.querySelector(selector);
  async function request(url, body, method) {
    const response = await fetch(url, {method:method || (body===undefined?'GET':'POST'),headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    const data=await response.json();if(!response.ok||data.error)throw new Error(data.error||'操作失败，请重试。');return data;
  }
  function notify(message, error=false){state.notice=message;state.error=error;const area=$('#dr-notice');if(area){area.className=`dr-notice ${error?'error':''}`;area.textContent=message;area.hidden=!message;}}
  function sourceText(source){
    if(typeof source==='string')return source;
    let original=source;for(let depth=0;depth<4&&original?.original;depth++)original=original.original;
    if(typeof original==='string')return original;
    if(original?.filename)return `${original.filename}${original.sheet?' · '+original.sheet:''}${original.row?' 第'+original.row+'行':''}${original.priceCell?' · 售价 '+original.priceCell:''}${original.salesCell?'／销量 '+original.salesCell:''}${source?.catalog?'（已应用商品关联）':''}`;
    return original?.type||'手动记录';
  }
  function close(){root.hidden=true;document.body.style.overflow=previousOverflow;}
  function open(){previousOverflow=document.body.style.overflow;root.hidden=false;document.body.style.overflow='hidden';state.tab='records';render();loadRecords();}
  function frame(body){return `<section class="dr-panel" role="dialog" aria-modal="true" aria-labelledby="dr-title"><header class="dr-header"><div><span class="dr-eyebrow">DAILY OBSERVATIONS</span><h2 id="dr-title">每日价格与销量</h2><p>按日期保存每款商品的售价与销量，明天的价格不会覆盖昨天。</p></div><button class="dr-close" data-dr="close" aria-label="关闭每日价格与销量">×</button></header><nav class="dr-tabs" aria-label="日记录功能">${[['records','日记录'],['import','导入价格表'],['catalog','商品关联'],['manual',state.editing?'修改记录':'手动记录']].map(([tab,label])=>`<button data-dr-tab="${tab}" class="${state.tab===tab?'active':''}">${label}</button>`).join('')}</nav><div id="dr-notice" class="dr-notice ${state.error?'error':''}" role="status" ${state.notice?'':'hidden'}>${escape(state.notice)}</div><div class="dr-body">${body}</div><footer class="dr-footer"><span>本机持久保存 · 原始文件不改写 · 空白代表未知</span><button class="dr-btn" data-dr="close">关闭</button></footer></section>`;}
  function render(){root.innerHTML=frame(state.tab==='records'?recordsView():state.tab==='import'?importView():state.tab==='catalog'?catalogView():manualView());refreshBusy();}
  function refreshBusy(){root.setAttribute('aria-busy',String(state.actionBusy));root.querySelectorAll('[data-dr-write],button[type="submit"]').forEach(button=>{button.disabled=state.actionBusy||(button.dataset.dr==='commit'&&!state.preview?.validCount)||(button.dataset.dr==='commit-catalog'&&!state.catalogPreview?.validCount);});}
  async function exclusive(operation){if(state.actionBusy)return;state.actionBusy=true;refreshBusy();try{return await operation();}finally{state.actionBusy=false;refreshBusy();}}
  function recordsView(){return `<div class="dr-toolbar"><label>开始日期<input id="dr-start" type="date" value="${escape(state.start)}"></label><label>结束日期<input id="dr-end" type="date" value="${escape(state.end)}"></label><label>商品<input id="dr-product" placeholder="商品标识或名称" value="${escape(state.product)}"></label><label>店铺／仓库<input id="dr-shop" placeholder="全部" value="${escape(state.shop)}"></label><button class="dr-btn primary" data-dr="filter">筛选</button><label class="dr-checkbox"><input id="dr-deleted" type="checkbox" ${state.deleted?'checked':''}>已删除记录</label></div><div class="dr-export-actions"><span>导出当前筛选的全部记录，包含其他分页。</span><div><button class="dr-btn small" data-dr="export-xlsx" data-dr-write>导出 Excel</button><button class="dr-btn small" data-dr="export-csv" data-dr-write>导出 CSV</button></div></div><div class="dr-note">记录的是指定日期的售价与当日销量；售价不代表真实成交均价，不能把最新售价套用到过去销量。</div><div id="dr-records-content">${recordsContent()}</div>`;}
  function recordsContent(){
    if(state.busy)return '<div class="dr-empty">正在读取本地日记录…</div>';
    if(!state.items.length)return `<div class="dr-empty"><span class="dr-empty-symbol">▤</span><h3>${state.deleted?'没有已删除的记录':'先留下一天的数据'}</h3><p>${state.deleted?'删除记录可在这里恢复，原始 Excel 始终保留。':'导入每天另存的价格表，或手动填写一款商品的售价和当日销量。'}</p>${state.deleted?'':'<button class="dr-btn primary" data-dr-tab="import">导入价格表</button><button class="dr-btn" data-dr="new">手动记录</button>'}</div>`;
    const keys=[...new Map(state.items.map(item=>[seriesKey(item),{product:item.product,name:item.name,shop:item.shop,warehouseId:item.warehouseId,specification:item.specification}])).entries()];
    const selected=keys.find(([key])=>key===state.trendKey)|| (keys.length===1?keys[0]:null);
    const options=keys.map(([key,item],index)=>`<option value="${index}" ${selected?.[0]===key?'selected':''}>${escape(item.name||item.product)} · ${escape(item.shop||'未指定店铺')}${item.warehouseId?' · ID '+escape(item.warehouseId):' · 仓库ID未知'}${item.specification?' · '+escape(item.specification):''}</option>`).join('');
    return `<div class="dr-section-heading"><div><h3>${state.deleted?'已删除日记录':'日期记录'}</h3><small>共 ${state.total} 条 · 当前第 ${state.offset+1}—${state.offset+state.items.length} 条</small></div><div><button class="dr-btn" data-dr="new">＋ 手动记录</button><button class="dr-btn" data-dr-tab="import">导入价格表</button></div></div>${!state.deleted?`<div class="dr-trend-selector"><label>选择一个商品与店铺查看趋势<select id="dr-trend"><option value="">选择商品…</option>${options}</select></label><span>只连接同一商品、同一店铺的日记录；缺失值不填 0。</span></div><div id="dr-trends">${selected?trends(state.items.filter(item=>seriesKey(item)===selected[0])):'<div class="dr-note">选择商品后显示售价与销量趋势，不跨商品混算价格。</div>'}</div>`:''}<div class="dr-table-scroll"><table class="dr-table"><thead><tr><th>业务日期</th><th>商品</th><th>店铺／仓库</th><th class="numeric">当日售价</th><th class="numeric">当日销量</th><th>来源</th><th>版本</th><th>操作</th></tr></thead><tbody>${state.items.map(item=>`<tr><td>${escape(item.date)}</td><td><strong>${escape(item.name)}</strong><small>${escape(item.product)}</small></td><td>${escape(item.shop||'未指定')}<small>${escape(item.warehouseId?'仓库ID '+item.warehouseId:'仓库ID未知')}${item.specification?' · '+escape(item.specification):''}</small></td><td class="numeric">${format(item.price)}</td><td class="numeric">${format(item.salesQuantity)}</td><td class="dr-source" title="${escape(sourceText(item.source))}">${escape(sourceText(item.source))}<small>${escape(stamp(item.updatedAt))}</small></td><td><button class="dr-link" data-dr-revisions="${escape(item.id)}">第 ${item.revision} 版</button></td><td class="dr-actions">${state.deleted?`<button class="dr-btn small" data-dr-restore="${escape(item.id)}">恢复</button>`:`<button class="dr-btn small" data-dr-edit="${escape(item.id)}">修改</button><button class="dr-link danger" data-dr-delete="${escape(item.id)}">删除</button>`}</td></tr>`).join('')}</tbody></table></div><div class="dr-pagination"><small>${state.hasMore?'还有更多记录，可翻页或缩小筛选范围。':'已显示当前筛选范围的这一页记录。'}</small><div><button class="dr-btn small" data-dr="previous" ${state.offset===0?'disabled':''}>上一页</button><button class="dr-btn small" data-dr="next" ${state.hasMore?'':'disabled'}>下一页</button></div></div>`;
  }
  function sparkline(items, key, title, color){
    const sorted=items.slice().sort((a,b)=>a.date.localeCompare(b.date));const valid=sorted.filter(item=>item[key]!==null&&item[key]!==undefined);
    if(!valid.length)return `<div class="dr-chart"><h4>${title}</h4><p class="dr-muted">此范围没有可用数据。</p></div>`;
    const max=Math.max(...valid.map(item=>Number(item[key])),1);const last=valid[valid.length-1];let paths=[],segment=[];
    sorted.forEach((item,i)=>{if(item[key]===null||item[key]===undefined){if(segment.length)paths.push(segment.join(' '));segment=[];return;}segment.push(`${32+(i/Math.max(1,sorted.length-1))*420},${125-(Number(item[key])/max)*90}`);});if(segment.length)paths.push(segment.join(' '));
    const points=sorted.filter(item=>item[key]!==null&&item[key]!==undefined).map(item=>{const i=sorted.indexOf(item);return `<circle cx="${32+(i/Math.max(1,sorted.length-1))*420}" cy="${125-(Number(item[key])/max)*90}" r="3" fill="${color}"><title>${escape(item.date)}：${format(item[key])}</title></circle>`;}).join('');
    return `<div class="dr-chart"><div><h4>${title}</h4><strong>${format(last[key])}</strong><small>最近有值日期 ${escape(last.date)}</small></div><svg viewBox="0 0 480 165" role="img" aria-label="${title}趋势"><line x1="32" y1="125" x2="452" y2="125" stroke="var(--line)"/><text x="2" y="34">${format(max)}</text><text x="12" y="128">0</text>${paths.map(path=>`<polyline points="${path}" fill="none" stroke="${color}" stroke-width="2.5"/>`).join('')}${points}<text x="32" y="151">${escape(sorted[0].date)}</text><text x="452" y="151" text-anchor="end">${escape(sorted[sorted.length-1].date)}</text></svg></div>`;
  }
  function trends(items){return `<div class="dr-charts">${sparkline(items,'price','每日售价','var(--purple)')}${sparkline(items,'salesQuantity','当日销量','var(--green)')}</div>`;}
  async function loadRecords(){
    const epoch=++state.recordsEpoch;state.busy=true;if(state.tab==='records')render();
    try{const q=new URLSearchParams({start:state.start,end:state.end,product:state.product,shop:state.shop,deleted:state.deleted?'1':'0',offset:state.offset,limit:state.limit});const result=await request('/api/daily?'+q);if(epoch!==state.recordsEpoch)return;state.items=result.items||[];state.total=result.total||0;state.hasMore=Boolean(result.hasMore);state.busy=false;if(state.tab==='records')render();}
    catch(error){if(epoch!==state.recordsEpoch)return;state.busy=false;if(state.tab==='records')render();notify(error.message,true);}
  }
  function manualView(){const item=state.editing||{};const locked=Boolean(item.id);return `<div class="dr-form-intro"><h3>${locked?'修改这一天的记录':'记录一个商品的一天'}</h3><p>${locked?'日期、商品和店铺保持不变。保存新数值后，之前版本仍可查看。':'同一天、同一商品、同一店铺只有一条当前记录。昨天与今天分别保存。'}</p></div><form id="dr-manual-form"><div class="dr-form-grid"><label>业务日期<input type="date" id="dr-form-date" value="${escape(item.date||today())}" required ${locked?'disabled':''}></label><label>商品标识<input id="dr-form-product" value="${escape(item.product||'')}" placeholder="稳定的 SKU 或商品编码" required ${locked?'disabled':''}></label><label>商品名称<input id="dr-form-name" value="${escape(item.name||'')}" placeholder="可留空，沿用商品标识"></label><label>店铺／仓库<input id="dr-form-shop" value="${escape(item.shop||'')}" placeholder="可留空" ${locked?'disabled':''}></label>${[['specification','规格'],['sourceSku','来源SKU'],['sourceSystem','来源系统'],['warehouseId','仓库ID']].map(([key,label])=>`<label>${label}<input id="dr-form-${key}" value="${escape(item[key]||'')}" placeholder="未知可留空" ${locked?'disabled':''}></label>`).join('')}<label>当日售价<input id="dr-form-price" type="number" min="0" step="any" value="${item.price===null||item.price===undefined?'':escape(item.price)}" placeholder="不知道则留空"></label><label>当日销量<input id="dr-form-sales" type="number" min="0" step="any" value="${item.salesQuantity===null||item.salesQuantity===undefined?'':escape(item.salesQuantity)}" placeholder="真实无销量填 0，未知留空"></label><label class="wide">记录说明<input id="dr-form-note" placeholder="例如：周末活动价；人工核对销量"></label>${locked?'':`<label class="wide">遇到同日同商品同店铺<select id="dr-form-conflict"><option value="skip">保留已有记录，不覆盖</option><option value="replace">更新该日记录，保留修改前版本</option></select></label>`}</div><div class="dr-note">售价和销量至少填写一项。售价是当日记录价，不是已确认成交均价；不会自动推算真实销售收入。</div><div class="dr-form-actions"><button type="button" class="dr-btn" data-dr-tab="records">返回日记录</button><button type="submit" class="dr-btn primary">${locked?'保存修改并保留旧版':'保存日记录'}</button></div></form>`;}
  async function saveManual(){
    const nullable=id=>$(id).value.trim()===''?null:Number($(id).value);
    const record={date:$('#dr-form-date').value,product:$('#dr-form-product').value.trim(),name:$('#dr-form-name').value.trim(),shop:$('#dr-form-shop').value.trim(),price:nullable('#dr-form-price'),salesQuantity:nullable('#dr-form-sales'),source:{type:state.editing?'手动修正':'手工录入',note:$('#dr-form-note').value.trim()}};
    if(state.editing?.source?.catalog)record.source.catalog=state.editing.source.catalog;
    for(const key of ['specification','sourceSku','sourceSystem','warehouseId'])record[key]=$('#dr-form-'+key)?.value?.trim()??state.editing?.[key]??'';
    try{const result=await request('/api/daily',{record,onConflict:state.editing?'replace':$('#dr-form-conflict').value});invalidatePreview();state.editing=null;state.tab='records';state.offset=0;await loadRecords();notify(result.status==='skipped'?result.reason||'已有记录，保留原值。':result.status==='updated'?'修改已保存，旧版仍可查看。':'日记录已保存，下次启动仍可查看。');}
    catch(error){notify(error.message,true);}
  }
  function columnOptions(info, map) {
    const sheet=info.sheets.find(item=>item.name===map.sheet);
    const cols=new Map((sheet?.headers||[]).map(item=>[item.column,String(item.label??'')]));
    for(let i=1;i<=52;i++){const letter=(i>26?'A':'')+String.fromCharCode(65+(i-1)%26);if(!cols.has(letter))cols.set(letter,'');}
    Object.values(map.columns||{}).filter(Boolean).forEach(column=>{if(!cols.has(column))cols.set(column,'');});
    return [...cols].sort((a,b)=>a[0].length-b[0].length||a[0].localeCompare(b[0]));
  }
  function importView(){
    if(!state.inspection)return `<div class="dr-import-drop" id="dr-drop"><span class="dr-empty-symbol">⇧</span><h3>导入价格与销量原表</h3><p>支持每日一行的长表，也支持上层日期、下层销量／售价的横表。<br>先预览展开结果，再确认保存。</p><button class="dr-btn primary" data-dr="choose-file">选择价格 Excel</button><small>支持 .xlsx / .xlsm，最大 30 MB；也可拖到此区域。</small></div><div class="dr-note">保留源表的完整年月日；未知不填0。没有日期的单日快照需指定日期。</div>`;
    const info=state.importData,map=info.mapping,paired=map.layout==='paired_dates',options=columnOptions(info,map);
    const keys=paired?{product:fields.product,name:fields.name,shop:fields.shop,specification:fields.specification,sourceSku:fields.sourceSku,sourceSystem:fields.sourceSystem,warehouseId:fields.warehouseId}:fields;
    const columns=Object.entries(keys).map(([key,label])=>`<label>${label}列<select data-dr-map="${key}" aria-label="${label}列"><option value="">${key==='date'?'无日期列，使用指定日期':'不提供'}</option>${options.map(([column,text])=>`<option value="${escape(column)}" ${map.columns?.[key]===column?'selected':''}>${escape(column)}${text?' · '+escape(text.slice(0,36)):''}</option>`).join('')}</select></label>`).join('');
    const layout=paired?`<label>日期表头行<input type="number" min="1" max="1000" id="dr-date-header" value="${map.dateHeaderRow||1}"></label><label>销量／售价表头行<input type="number" min="1" max="1000" id="dr-value-header" value="${map.valueHeaderRow||2}"></label><label>数据起始行<input type="number" min="1" max="1001" id="dr-data-start" value="${map.dataStartRow||3}"></label>`:`<label>表头行<input type="number" min="1" max="1000" id="dr-import-header" value="${map.headerRow||1}"></label>`;
    const pairs=paired?`<div class="dr-note"><strong>按日期展开 ${map.datePairs?.length||0} 组价量列</strong><p>${(map.datePairs||[]).map(pair=>`${escape(pair.date)}：销量 ${escape(pair.salesQuantity||'未提供')}／售价 ${escape(pair.price||'未提供')}`).join('；')}</p>仓位只在实际合并区域内填充。日期与列对应以导入预览为准，不自动改年份。</div>`:'';
    return `<div class="dr-section-heading"><div><h3>${escape(info.filename)}</h3><small>原表只读；已保存的商品关联将用于本次导入。</small></div><button class="dr-btn small" data-dr="choose-file">重新选文件</button></div><div class="dr-form-grid dr-import-settings"><label>工作表<select id="dr-import-sheet">${info.sheets.map(item=>`<option value="${escape(item.name)}" ${map.sheet===item.name?'selected':''}>${escape(item.name)}</option>`).join('')}</select></label><label>表格布局<select id="dr-import-layout"><option value="long" ${paired?'':'selected'}>按行记录日期／单日快照</option><option value="paired_dates" ${paired?'selected':''}>双层日期价量横表</option></select></label>${layout}${columns}${paired?'':`<label>无日期列时的业务日期<input type="date" id="dr-snapshot-date" value="${escape(info.snapshotDate||'')}"><small>售价和销量必须属于这个日期。</small></label>`}<label>遇到已有的同日记录<select id="dr-import-conflict"><option value="skip" ${info.onConflict==='skip'?'selected':''}>保留已有记录，跳过重复</option><option value="merge" ${info.onConflict==='merge'?'selected':''}>仅补齐空白，冲突保留待核对</option><option value="replace" ${info.onConflict==='replace'?'selected':''}>明确更新该日记录，保留旧版本</option></select></label></div>${pairs}<label class="dr-checkbox dr-daily-confirm"><input id="dr-quantity-daily" type="checkbox" ${info.quantityIsDaily?'checked':''}>所选销量已经确认是当日销量（累计／月合计不能作为当日销量）</label><div class="dr-form-actions"><button class="dr-btn" data-dr="load-mapping">载入上次映射</button><button class="dr-btn" data-dr="save-mapping">保存这份映射</button><button class="dr-btn primary" data-dr="preview" data-dr-write>预览导入结果</button></div>${state.preview?previewView():`<div class="dr-note">先预览新增、未变化和冲突记录。修改映射、销量确认或重复处理方式后，需要重新预览。</div>`}`;
  }
  function captureImport(){
    if(!state.importData||!$('#dr-import-sheet'))return;
    const info=state.importData,map=info.mapping;map.sheet=$('#dr-import-sheet').value;map.layout=$('#dr-import-layout').value;
    if($('#dr-import-header'))map.headerRow=Number($('#dr-import-header').value);
    if($('#dr-date-header'))map.dateHeaderRow=Number($('#dr-date-header').value);
    if($('#dr-value-header'))map.valueHeaderRow=Number($('#dr-value-header').value);
    if($('#dr-data-start'))map.dataStartRow=Number($('#dr-data-start').value);
    map.columns={};root.querySelectorAll('[data-dr-map]').forEach(input=>map.columns[input.dataset.drMap]=input.value);
    if($('#dr-snapshot-date'))info.snapshotDate=$('#dr-snapshot-date').value;
    info.onConflict=$('#dr-import-conflict').value;info.quantityIsDaily=$('#dr-quantity-daily').checked;
  }
  function importSettings(previewOnly){const info=state.importData;return {workbookId:state.inspection.workbookId,mapping:info.mapping,snapshotDate:info.snapshotDate,quantityIsDaily:info.quantityIsDaily,onConflict:info.onConflict,previewOnly};}
  function invalidatePreview(catalog=false){
    if(catalog){state.catalogPreview=null;state.catalogSettings=null;state.catalogKey=null;state.catalogEpoch++;}
    else {state.preview=null;state.previewSettings=null;state.previewKey=null;state.importEpoch++;}
    root.querySelector(catalog?'.dr-catalog-preview':'.dr-daily-preview')?.remove();
  }
  const reviewLabels={new:'新增',unchanged:'未变化',conflict:'冲突',fill:'补齐空白',deleted:'已删除待恢复'};
  function issueView(preview){return preview.issues?.length?`<div class="dr-import-issues"><strong>需要核对的数据</strong>${preview.issues.slice(0,30).map(issue=>`<p>${issue.row?'第 '+escape(issue.row)+' 行':''}${issue.product?' · '+escape(issue.product):''}：${escape(issue.message)}</p>`).join('')}${preview.issues.length>30?`<small>共 ${preview.issues.length} 项，当前展示前30项。请修正原表或缩小导入范围后重新预览。</small>`:''}</div>`:'';}
  function differenceView(row){
    if(!row.differences?.length)return '<span class="dr-muted">—</span>';
    return row.differences.map(diff=>`<div class="dr-difference"><span>${escape(diff.label||fields[diff.field]||diff.field)}</span><del>${escape(diff.old===null||diff.old===undefined?'未知':diff.old)}</del><span>→</span><strong>${escape(diff.new===null||diff.new===undefined?'未知':diff.new)}</strong></div>`).join('');
  }
  function reviewTable(preview,catalog=false){
    const filter=catalog?state.catalogFilter:state.previewFilter,offset=catalog?state.catalogOffset:state.previewOffset;
    const rows=preview.review||(preview.records||[]).map(incoming=>({status:'new',incoming}));
    const selected=filter==='all'?rows:rows.filter(row=>row.status===filter),page=selected.slice(offset,offset+30),kind=catalog?'catalog':'daily';
    const statusOptions=[['all','全部状态'],...Object.entries(reviewLabels)].map(([key,label])=>`<option value="${key}" ${filter===key?'selected':''}>${label}</option>`).join('');
    return `<div class="dr-review-toolbar"><label>预览状态<select data-dr-review-filter="${kind}">${statusOptions}</select></label><span>${selected.length?offset+1:0}—${Math.min(offset+30,selected.length)} / ${selected.length} 条</span></div><div class="dr-table-scroll"><table class="dr-table dr-review-table"><thead><tr><th>状态</th>${catalog?'<th>来源商品／仓位</th><th>标准商品／规格</th><th>目标店铺／仓库</th>':'<th>业务日期</th><th>商品／店铺</th><th>本次售价／销量</th>'}<th>已有值 → 本次值</th></tr></thead><tbody>${page.map(row=>{const item=row.incoming||row.record||{};return `<tr><td><span class="dr-review-status ${escape(row.status)}">${escape(reviewLabels[row.status]||row.status)}</span></td>${catalog?`<td>${escape(item.sourceProduct)}<small>${escape(item.sourceShop||'未指定仓位')}</small></td><td>${escape(item.product)}<small>${escape(item.name)}${item.specification?' · '+escape(item.specification):''}</small></td><td>${escape(item.shop||'未指定')}</td>`:`<td>${escape(item.date)}</td><td>${escape(item.name||item.product)}<small>${escape(item.product)} · ${escape(item.shop||'未指定')}</small></td><td>售价 ${format(item.price)}<small>销量 ${format(item.salesQuantity)}</small></td>`}<td>${differenceView(row)}</td></tr>`;}).join('')||'<tr><td colspan="5">没有符合这个状态的记录。</td></tr>'}</tbody></table></div><div class="dr-pagination"><small>逐页核对全部有效记录；确认按所选重复处理方式保存。</small><div><button class="dr-btn small" data-dr-review-page="${kind}" data-step="-1" ${offset===0?'disabled':''}>上一页</button><button class="dr-btn small" data-dr-review-page="${kind}" data-step="1" ${offset+30>=selected.length?'disabled':''}>下一页</button></div></div>`;
  }
  function reviewSummary(preview){const counts=preview.reviewCounts||{};return Object.entries(reviewLabels).map(([key,label])=>`${label} ${counts[key]||0}`).join(' · ');}
  function previewView(){
    const preview=state.preview;
    return `<section class="dr-preview dr-daily-preview"><div class="dr-section-heading"><div><h3>导入预览</h3><small>${reviewSummary(preview)} · 需处理 ${preview.invalidCount||0} · 空白价量 ${preview.emptyCount||0} 组已略过</small></div><button class="dr-btn primary" data-dr="commit" data-dr-write ${preview.validCount?'':'disabled'}>确认保存有效记录</button></div>${preview.validCount?'':'<div class="dr-import-issues"><strong>没有可保存的价量记录</strong><p>已识别日期和商品；售价与销量均为空时不生成记录，也不补成0。请补齐原表后重新选择，或改用其他有实际数值的文件。</p></div>'}<div class="dr-note">${state.importData.onConflict==='replace'?'本次已选择明确更新；冲突记录更新后保留旧版本。':state.importData.onConflict==='merge'?'仅补齐空白；已有值不同的冲突记录不会静默覆盖。':'默认保留已有记录，冲突不会覆盖。可修改重复处理方式，再次预览后确认。'} 未变化记录不会重复累计；预览后数据或关联变化时须重新预览。</div>${issueView(preview)}${reviewTable(preview)}</section>`;
  }
  async function readWorkbook(file){
    if(!/\.(xlsx|xlsm)$/i.test(file.name))throw new Error('请选择 .xlsx 或 .xlsm 文件。');
    if(file.size>30*1024*1024)throw new Error('文件超过30 MB，请拆分后导入。');
    const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('文件无法读取。'));reader.readAsDataURL(file);});
    return request('/api/inspect',{filename:file.name,content});
  }
  async function chooseFile(file,catalog=false){
    if(!file||state.actionBusy)return;
    return exclusive(async()=>{
      invalidatePreview(catalog);const epoch=catalog?state.catalogEpoch:state.importEpoch;
      notify(catalog?'正在读取商品对照表…':'正在读取价格表…');
      try{
        const inspection=await readWorkbook(file);
        if(catalog){if(epoch!==state.catalogEpoch)return;state.catalogFile={...inspection,filename:file.name};state.catalogSheet=inspection.sheets?.[0]?.name||'';state.catalogConflict='skip';state.tab='catalog';render();notify('已读取订货表，请预览商品关联后保存。');}
        else {const info=await request('/api/daily/inspect?workbookId='+encodeURIComponent(inspection.workbookId));if(epoch!==state.importEpoch)return;state.inspection=inspection;state.importData={...info,snapshotDate:info.suggestedDate||'',onConflict:'skip',quantityIsDaily:false};state.tab='import';render();notify('请核对日期、价量列与商品关联，再生成预览。');}
      }catch(error){notify(error.message,true);}
    });
  }
  async function previewImport(){
    if(state.actionBusy)return;captureImport();invalidatePreview();
    const map=state.importData.mapping,hasQuantity=Boolean(map.columns?.salesQuantity)||(map.layout==='paired_dates'&&(map.datePairs||[]).some(pair=>pair.salesQuantity));
    if(hasQuantity&&!state.importData.quantityIsDaily){notify('请先确认所选销量是当日销量；累计或月合计不能直接导入。',true);return;}
    const settings=JSON.parse(JSON.stringify(importSettings(false))),key=JSON.stringify(settings),epoch=state.importEpoch;
    return exclusive(async()=>{try{const preview=await request('/api/daily/import',{...settings,previewOnly:true});if(epoch!==state.importEpoch)return;state.preview=preview;state.previewSettings={...settings,previewFingerprint:preview.previewFingerprint};state.previewKey=key;state.previewOffset=0;state.previewFilter='all';render();notify('请核对新增、未变化与冲突，再确认保存。');}catch(error){notify(error.message,true);}});
  }
  async function commitImport(){
    if(state.actionBusy||!state.preview?.validCount||!state.previewSettings)return;captureImport();
    if(JSON.stringify(importSettings(false))!==state.previewKey){invalidatePreview();notify('导入设置已变化，请重新预览。',true);return;}
    return exclusive(async()=>{try{const result=await request('/api/daily/import',state.previewSettings);invalidatePreview();state.tab='records';state.offset=0;state.deleted=false;await loadRecords();notify(`已新增 ${result.saved||0} 条，更新 ${result.replaced||0} 条，跳过 ${result.skipped||0} 条；需核对 ${result.invalidCount||0} 行。`);}catch(error){invalidatePreview();render();notify(error.message,true);}});
  }
  function captureFilters(){if(!$('#dr-start'))return;state.start=$('#dr-start').value;state.end=$('#dr-end').value;state.product=$('#dr-product').value.trim();state.shop=$('#dr-shop').value.trim();state.deleted=$('#dr-deleted').checked;}
  async function exportRecords(formatName){
    if(state.actionBusy)return;captureFilters();
    return exclusive(async()=>{try{const q=new URLSearchParams({format:formatName,start:state.start,end:state.end,product:state.product,shop:state.shop,deleted:state.deleted?'1':'0'});const result=await request('/api/daily/export?'+q);const binary=atob(result.dataBase64),bytes=Uint8Array.from(binary,c=>c.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:result.contentType}));const anchor=document.createElement('a');anchor.href=url;anchor.download=result.filename;document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);notify('已生成当前筛选范围的全部日记录，保存位置由下载窗口选择。');}catch(error){notify(error.message,true);}});
  }
  function catalogView(){
    if(state.catalogBusy)return '<div class="dr-note" role="status">正在读取已保存的商品关联…</div>';
    const item=state.catalogEditing||{},locked=Boolean(state.catalogEditing);
    const editor=[['sourceProduct','来源商品ID','原表中的商品ID',true],['sourceSystem','来源系统','不同平台使用不同来源名',false],['sourceSku','来源SKU','无SKU可用来源规格区分',false],['sourceSpecification','来源规格','与来源表一致，如1件/1组',false],['sourceWarehouseId','来源仓库ID','有ID时不按同名仓合并',false],['sourceShop','来源仓位','无ID时按名称匹配',false],['product','标准商品标识','同一标准SKU使用同一标识',true],['name','商品名称','可留空',false],['specification','标准规格','如：500毫升×12瓶',false],['warehouseId','标准仓库ID','未知留空',false],['shop','目标店铺／仓库','可留空',false]].map(([key,label,placeholder,required])=>`<label>${label}<input id="dr-catalog-${key}" value="${escape(item[key]||'')}" placeholder="${placeholder}" ${['sourceProduct','sourceSystem','sourceSku','sourceSpecification','sourceWarehouseId','sourceShop'].includes(key)&&locked?'disabled':''} ${required&&key!=='sourceShop'?'required':''}></label>`).join('');
    const file=state.catalogFile;
    return `<div class="dr-form-intro"><h3>商品与仓位关联</h3><p>按来源系统、商品ID、SKU／规格与仓库ID关联；缺ID时按仓名匹配，同名不同ID不会自动合并。日记录与主看盘共用关联，已保存日记录不自动搬移。</p></div><div class="dr-section-heading"><div><h3>已保存关联</h3><small>${state.catalogItems.length} 条</small></div><button class="dr-btn small" data-dr="choose-catalog-file">从订货 Excel 导入</button></div>${state.catalogBusy?'<div class="dr-note">正在读取关联…</div>':`<div class="dr-table-scroll"><table class="dr-table"><thead><tr><th>来源商品ID</th><th>来源仓位</th><th>标准商品</th><th>规格</th><th>目标店铺／仓库</th><th>操作</th></tr></thead><tbody>${state.catalogItems.map((entry,index)=>`<tr><td>${escape(entry.sourceProduct)}<small>${escape(entry.sourceSystem||'未指定来源')} · ${escape(entry.sourceSku||entry.sourceSpecification||'未提供SKU／规格')}</small></td><td>${escape(entry.sourceShop||'未指定')}<small>${escape(entry.sourceWarehouseId?'ID '+entry.sourceWarehouseId:'仓库ID未知')}</small></td><td>${escape(entry.product)}<small>${escape(entry.name)}</small></td><td>${escape(entry.specification||'未填写')}</td><td>${escape(entry.shop||'未指定')}</td><td><button class="dr-btn small" data-dr-catalog-edit="${index}">修改</button></td></tr>`).join('')||'<tr><td colspan="6">还没有商品关联。可以导入订货表，也可以在下方手动建立。</td></tr>'}</tbody></table></div>`}<section class="dr-catalog-editor"><h3>${locked?'修改关联':'建立关联'}</h3><form id="dr-catalog-form"><div class="dr-form-grid">${editor}</div><div class="dr-form-actions">${locked?'<button type="button" class="dr-btn" data-dr="cancel-catalog-edit">取消修改</button>':''}<button type="submit" class="dr-btn primary">${locked?'保存关联修改':'保存商品关联'}</button></div></form></section>${file?`<section class="dr-catalog-import"><div class="dr-section-heading"><h3>${escape(file.filename||file.workbookId)}</h3><button class="dr-btn small" data-dr="choose-catalog-file">重新选文件</button></div><div class="dr-form-grid"><label>订货工作表<select id="dr-catalog-sheet">${(file.sheets||[]).map(sheet=>`<option value="${escape(sheet.name)}" ${state.catalogSheet===sheet.name?'selected':''}>${escape(sheet.name)}</option>`).join('')}</select></label><label>已有来源关联<select id="dr-catalog-conflict"><option value="skip" ${state.catalogConflict==='skip'?'selected':''}>保留已有，跳过冲突</option><option value="replace" ${state.catalogConflict==='replace'?'selected':''}>明确更新关联</option></select></label></div><div class="dr-form-actions"><button class="dr-btn primary" data-dr="preview-catalog" data-dr-write>预览商品关联</button></div>${state.catalogPreview?catalogPreviewView():''}</section>`:''}`;
  }
  async function loadCatalog(){state.catalogBusy=true;if(state.tab==='catalog')render();try{const result=await request('/api/daily/catalog');state.catalogItems=result.items||[];state.catalogBusy=false;if(state.tab==='catalog')render();}catch(error){state.catalogBusy=false;if(state.tab==='catalog')render();notify(error.message,true);}}
  async function saveCatalog(){
    if(state.actionBusy)return;const record={};for(const key of ['sourceProduct','sourceShop','product','name','specification','shop','sourceSystem','sourceSku','sourceSpecification','sourceWarehouseId','warehouseId'])record[key]=$('#dr-catalog-'+key)?.value?.trim()??state.catalogEditing?.[key]??'';
    const expectedRevision=state.catalogEditing?.revision;
    return exclusive(async()=>{try{await request('/api/daily/catalog',{record,...(expectedRevision===undefined?{}:{expectedRevision})});state.catalogEditing=null;invalidatePreview();invalidatePreview(true);await loadCatalog();notify('关联已保存。之后的导入使用新关联，旧日记录保持原值。');}catch(error){notify(error.message,true);}});
  }
  function captureCatalogImport(){if($('#dr-catalog-sheet'))state.catalogSheet=$('#dr-catalog-sheet').value;if($('#dr-catalog-conflict'))state.catalogConflict=$('#dr-catalog-conflict').value;}
  function catalogImportSettings(previewOnly){return {workbookId:state.catalogFile.workbookId,sheet:state.catalogSheet,onConflict:state.catalogConflict,previewOnly};}
  async function previewCatalog(){
    if(state.actionBusy||!state.catalogFile)return;captureCatalogImport();invalidatePreview(true);const settings=catalogImportSettings(false),key=JSON.stringify(settings),epoch=state.catalogEpoch;
    return exclusive(async()=>{try{const preview=await request('/api/daily/catalog/import',{...settings,previewOnly:true});if(epoch!==state.catalogEpoch)return;state.catalogPreview=preview;state.catalogSettings={...settings,previewFingerprint:preview.previewFingerprint};state.catalogKey=key;state.catalogOffset=0;state.catalogFilter='all';render();notify('请核对来源商品、规格和仓位，再保存关联。');}catch(error){notify(error.message,true);}});
  }
  function catalogPreviewView(){const preview=state.catalogPreview;return `<section class="dr-preview dr-catalog-preview"><div class="dr-section-heading"><div><h3>商品关联预览</h3><small>${reviewSummary(preview)} · 需处理 ${preview.invalidCount||0}</small></div><button class="dr-btn primary" data-dr="commit-catalog" data-dr-write ${preview.validCount?'':'disabled'}>确认保存关联</button></div><div class="dr-note">${state.catalogConflict==='replace'?'本次已明确选择更新已有来源关联。':'默认保留已有来源关联；规格或目标不同将列为冲突。'} 不按名称合并，不更改原始工作簿或旧日记录。</div>${issueView(preview)}${reviewTable(preview,true)}</section>`;}
  async function commitCatalog(){
    if(state.actionBusy||!state.catalogPreview?.validCount||!state.catalogSettings)return;captureCatalogImport();if(JSON.stringify(catalogImportSettings(false))!==state.catalogKey){invalidatePreview(true);notify('关联导入设置已变化，请重新预览。',true);return;}
    return exclusive(async()=>{try{const result=await request('/api/daily/catalog/import',state.catalogSettings);invalidatePreview(true);invalidatePreview();await loadCatalog();notify(`关联已保存：新增 ${result.saved||0} 条，更新 ${result.replaced||0} 条，跳过 ${result.skipped||0} 条。`);}catch(error){invalidatePreview(true);render();notify(error.message,true);}});
  }
  async function revisionView(id){try{const current=state.items.find(item=>item.id===id);const data=await request('/api/daily/'+encodeURIComponent(id)+'/revisions');const box=document.createElement('div');box.className='dr-revision-overlay';box.innerHTML=`<section role="dialog" aria-modal="true" aria-label="日记录旧版本"><div class="dr-section-heading"><h3>${escape(current?.name||'商品')} · 修改前版本</h3><button class="dr-btn small" data-dr="close-revisions">关闭</button></div><p class="dr-muted">历史版本只供查看，当前值保存在日记录表中。</p>${data.items.length?`<div class="dr-table-scroll"><table class="dr-table"><thead><tr><th>保存时点</th><th>操作</th><th>业务日期</th><th>售价</th><th>销量</th><th>原来源</th></tr></thead><tbody>${data.items.map(item=>`<tr><td>${escape(stamp(item.savedAt))}</td><td>${escape({replace:'更新前',delete:'删除前',restore:'恢复前'}[item.action]||item.action)}</td><td>${escape(item.record.date)}</td><td>${format(item.record.price)}</td><td>${format(item.record.salesQuantity)}</td><td>${escape(sourceText(item.record.source))}</td></tr>`).join('')}</tbody></table></div>`:'<div class="dr-empty">这条记录尚未修改，没有旧版本。</div>'}</section>`;root.append(box);}catch(error){notify(error.message,true);}}
  function boot(){
    root=document.createElement('div');root.id='daily-root';root.className='dr-overlay';root.hidden=true;document.body.append(root);
    const nav=document.querySelector('nav[aria-label="主导航"]');if(nav){const button=document.createElement('button');button.className='nav-item';button.type='button';button.setAttribute('aria-label','每日价格与销量');button.innerHTML='<svg class="icon"><use href="#i-chart"/></svg><span>每日价格与销量</span>';button.addEventListener('click',event=>{event.stopPropagation();open();});nav.append(button);}
    const fileInput=document.createElement('input');fileInput.type='file';fileInput.accept='.xlsx,.xlsm';fileInput.hidden=true;fileInput.id='dr-file-input';document.body.append(fileInput);let filePurpose='daily';
    fileInput.addEventListener('change',()=>{chooseFile(fileInput.files[0],filePurpose==='catalog');fileInput.value='';});
    root.addEventListener('click',async event=>{
      event.stopPropagation();if(event.target===root){close();return;}const target=event.target.closest('button');if(!target||target.disabled)return;
      const action=target.dataset.dr;
      if(action==='close'){close();return;}if(action==='close-revisions'){$('.dr-revision-overlay')?.remove();return;}
      if(state.actionBusy)return;
      try{
        if(target.dataset.drTab){state.tab=target.dataset.drTab;state.notice='';if(state.tab==='manual')state.editing=null;render();if(state.tab==='records')loadRecords();else if(state.tab==='catalog')loadCatalog();return;}
        if(target.dataset.drEdit){state.editing=state.items.find(item=>item.id===target.dataset.drEdit);state.tab='manual';render();return;}
        if(target.dataset.drCatalogEdit!==undefined){state.catalogEditing=state.catalogItems[Number(target.dataset.drCatalogEdit)];render();return;}
        if(target.dataset.drDelete){await exclusive(async()=>{await request('/api/daily/'+encodeURIComponent(target.dataset.drDelete),undefined,'DELETE');invalidatePreview();await loadRecords();notify('日记录已移入已删除，可勾选“已删除记录”恢复。');});return;}
        if(target.dataset.drRestore){await exclusive(async()=>{await request('/api/daily/'+encodeURIComponent(target.dataset.drRestore)+'/restore',{});invalidatePreview();await loadRecords();notify('日记录已恢复。');});return;}
        if(target.dataset.drRevisions){await revisionView(target.dataset.drRevisions);return;}
        if(target.dataset.drReviewPage){const key=target.dataset.drReviewPage==='catalog'?'catalogOffset':'previewOffset';state[key]=Math.max(0,state[key]+Number(target.dataset.step)*30);render();return;}
        if(action==='new'){state.editing=null;state.tab='manual';render();}
        else if(action==='filter'){captureFilters();state.offset=0;await loadRecords();}
        else if(action==='previous'){state.offset=Math.max(0,state.offset-state.limit);await loadRecords();}
        else if(action==='next'){state.offset+=state.limit;await loadRecords();}
        else if(action==='choose-file'){filePurpose='daily';fileInput.click();}
        else if(action==='choose-catalog-file'){filePurpose='catalog';fileInput.click();}
        else if(action==='preview')await previewImport();
        else if(action==='commit')await commitImport();
        else if(action==='preview-catalog')await previewCatalog();
        else if(action==='commit-catalog')await commitCatalog();
        else if(action==='cancel-catalog-edit'){state.catalogEditing=null;render();}
        else if(action==='export-xlsx'||action==='export-csv')await exportRecords(action==='export-xlsx'?'xlsx':'csv');
        else if(action==='save-mapping'){captureImport();localStorage.setItem('operator-daily-mapping-v1',JSON.stringify(state.importData.mapping));window.dispatchEvent(new CustomEvent('retail-link-preferences-change'));notify('价格表映射已保存；其他文件可手动载入。');}
        else if(action==='load-mapping'){
          const saved=JSON.parse(localStorage.getItem('operator-daily-mapping-v1')||'null');if(!saved)throw new Error('还没有保存价格表映射。');captureImport();
          if(!state.importData.sheets.some(sheet=>sheet.name===saved.sheet))saved.sheet=state.importData.mapping.sheet;
          const detected=state.importData.sheets.find(sheet=>sheet.name===saved.sheet)?.mapping;
          if(saved.layout==='paired_dates'&&detected?.layout==='paired_dates')saved.datePairs=JSON.parse(JSON.stringify(detected.datePairs));
          state.importData.mapping=saved;invalidatePreview();render();notify('已载入映射，请核对工作表、原始日期与价量列后预览。');
        }
      }catch(error){notify(error.message,true);}
    });
    root.addEventListener('submit',event=>{event.preventDefault();event.stopPropagation();if(state.actionBusy)return;if(event.target.id==='dr-manual-form')exclusive(saveManual);else if(event.target.id==='dr-catalog-form')saveCatalog();});
    root.addEventListener('change',event=>{
      event.stopPropagation();const target=event.target;
      if(target.id==='dr-deleted'){captureFilters();state.offset=0;loadRecords();}
      else if(target.id==='dr-trend'){const keys=[...new Map(state.items.map(item=>[seriesKey(item),item])).keys()];state.trendKey=target.value===''?'':keys[Number(target.value)];const selected=state.items.filter(item=>seriesKey(item)===state.trendKey);$('#dr-trends').innerHTML=selected.length?trends(selected):'<div class="dr-note">请选择一个商品和店铺。</div>';}
      else if(target.dataset.drReviewFilter){const catalog=target.dataset.drReviewFilter==='catalog';state[catalog?'catalogFilter':'previewFilter']=target.value;state[catalog?'catalogOffset':'previewOffset']=0;render();}
      else if(target.id==='dr-import-sheet'){
        const name=target.value;captureImport();const sheet=state.importData.sheets.find(item=>item.name===name);state.importData.mapping=JSON.parse(JSON.stringify(sheet?.mapping||{layout:'long',sheet:name,headerRow:1,columns:{}}));state.importData.mapping.sheet=name;state.importData.quantityIsDaily=false;invalidatePreview();render();
      }
      else if(target.id==='dr-import-layout'){
        const layout=target.value;captureImport();const current=state.importData.mapping,sheet=state.importData.sheets.find(item=>item.name===current.sheet);
        state.importData.mapping=sheet?.mapping?.layout===layout?JSON.parse(JSON.stringify(sheet.mapping)):{...current,layout};invalidatePreview();render();
      }
      else if(target.id==='dr-catalog-sheet'||target.id==='dr-catalog-conflict'){captureCatalogImport();invalidatePreview(true);}
      else if(target.closest('.dr-import-settings')||target.id==='dr-quantity-daily'){captureImport();invalidatePreview();}
    });
    root.addEventListener('input',event=>{if(event.target.closest('.dr-import-settings'))invalidatePreview();else if(event.target.id==='dr-catalog-sheet'||event.target.id==='dr-catalog-conflict')invalidatePreview(true);});
    root.addEventListener('dragover',event=>{event.preventDefault();event.stopPropagation();});
    root.addEventListener('drop',event=>{event.preventDefault();event.stopPropagation();if(state.tab==='import'||state.tab==='catalog')chooseFile(event.dataTransfer.files[0],state.tab==='catalog');});
    document.addEventListener('keydown',event=>{if(!root.hidden&&event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();const revision=$('.dr-revision-overlay');if(revision)revision.remove();else close();}},true);
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
