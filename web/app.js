const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const clone = value => JSON.parse(JSON.stringify(value));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
const number = (value, precision = 0) => finite(value) ? Number(value).toLocaleString('zh-CN', {minimumFractionDigits:precision,maximumFractionDigits:precision}) : '—';
const amount = value => finite(value) ? Number(value).toLocaleString('zh-CN', {minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
const percent = value => finite(value) ? `${number(Number(value) * 100,2)}%` : '—';
const integerOrDecimal = value => finite(value) ? Number(value).toLocaleString('zh-CN', {maximumFractionDigits:2}) : '—';
const nonemptyNumber = value => value === '' || value === null || value === undefined ? null : Number(value);
const basisLabels = {orders_amount:'报单金额',orders_quantity:'报单数量（小单位）',outbound_amount:'出库金额',outbound_quantity:'出库数量（小单位）',sales_quantity:'实际销量（小单位）'};
const metricLabels = {name:'商品名称',warehouse:'仓库',basePriceLarge:'到岸价（大单位）',basePrice:'到岸价（小单位）',salesQuantity:'累计销量',plan:'月计划',completed:'累计完成',stockLarge:'现货大单位',stockSmall:'现货小单位',stockAmount:'现货金额',transitSmall:'在途小单位',transitAmount:'在途金额',outboundQuantity:'出库数量',outboundAmount:'出库到岸金额',supplyAmount:'出库供价金额',discountRate:'折扣比例',markdownRate:'折价率',lossAmount:'货损金额',lossRate:'货损率',averageSales:'日均销量',yesterdaySales:'昨日销量',coverageDays:'现货可售天数',code:'商品编码',pack:'箱规'};
const storageKey = 'operator-console-profiles-v1';
let profiles = [];
try { profiles = JSON.parse(localStorage.getItem(storageKey) || '[]'); if (!Array.isArray(profiles)) profiles=[]; } catch { profiles=[]; }
const state = {inspection:null,workbookId:null,config:null,result:null,pendingImport:null,historySnapshot:null,historyDeleted:false,historyItems:[],historyOffset:0,historyTotal:0,historyHasMore:false,historyRequest:0,activeTab:'dashboard',tableMode:'business',search:'',statusFilter:'all',issueFilter:'all',sort:'target',selectedSheet:null,preview:null,previewOffset:0,previewTarget:null,selectedProduct:null,busy:false,sampleAvailable:true,defaultDirectory:'',directoryPath:'',directoryFiles:[],importTab:'file',configTab:'rules',configTarget:'current',configDraft:null,request:0,previewRequest:0};

async function api(url, body, method) {
  const options={method:method||(body===undefined?'GET':'POST')};
  if(body!==undefined){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}
  const response = await fetch(url, options);
  let data;
  try {data=await response.json();} catch {throw new Error('服务返回了无法识别的内容，请确认本地工具正常运行。');}
  if (!response.ok || data.error) { const error=new Error(typeof data.error==='string'?data.error:data.message||'处理失败，请检查文件与配置。'); error.status=response.status; error.data=data; throw error; }
  return data;
}
function toast(message, error=false) {
  const element=document.createElement('div');element.className=`toast${error?' error':''}`;
  element.innerHTML=`${icon(error?'alert':'check')}<span>${esc(message)}</span>`;$('#toast-root').append(element);
  setTimeout(()=>element.remove(),error?8000:4000);
}
function busy(on,label='正在分析工作簿') {state.busy=on;$('#busy-overlay').hidden=!on;$('#busy-label').textContent=label;document.body.setAttribute('aria-busy',String(on));}
function isAmountBasis(){return (state.config?.planBasis||'orders_amount').endsWith('amount');}
function basis(){return basisLabels[state.config?.planBasis]||'报单金额';}
function planNumber(value){return isAmountBasis()?amount(value):integerOrDecimal(value);}
function measure(value, money=true){return finite(value)?`${money?'¥ ':''}${money?amount(value):integerOrDecimal(value)}`:'—';}
function visibleIssueCount(product){return (state.result?.issues||[]).filter(issue=>String(issue.product??'')===String(product.id)).length || (product.issues||[]).length;}
function productName(product){return product.name&&String(product.name)!==String(product.id)?String(product.name):`商品 ${product.id}`;}
function manualCount(){return Object.keys(state.config?.overrides||{}).filter(key=>Object.keys(state.config.overrides[key]||{}).length).length;}
function percentWidth(value){return finite(value)?Math.max(0,Math.min(100,Number(value)*100)):0;}
function periodLabel(){const [year,month]=(state.config?.period||'').split('-');return year&&month?`${year} 年 ${Number(month)} 月`:'当月';}
function tag(text, cls=''){return `<span class="tag ${cls}">${esc(text)}</span>`;}
function readOnlySnapshot(){return Boolean(state.historySnapshot&&!state.historySnapshot.sourceAvailable);}
function configInspection(){return state.configTarget==='pending'?state.pendingImport?.inspection:state.inspection;}
function dateTime(value){if(!value)return '生成时间未记录';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false});}
function requireSource(){if(!readOnlySnapshot())return true;toast(state.historySnapshot.sourceMessage||'此历史记录的原始文件不可用，当前只读；可重新导入文件继续分析。',true);return false;}

function pageHeading(loaded=false){return `<div class="page-heading"><div><div class="eyebrow">BUSINESS OVERVIEW</div><h1>电商看盘</h1><p>${loaded?'库存、销量与计划进度，在这里一起看清。':'选择经营文件，确认范围后生成经营视图。'}</p></div><div class="heading-actions"><button class="btn" data-action="open-history" aria-label="查看历史记录">${icon('clock')}历史记录</button>${loaded?`<button class="btn" data-action="open-config" ${readOnlySnapshot()?'disabled title="原始文件不可用，此历史记录只读"':''}>${icon('settings')}分析配置</button><button class="btn" data-action="export">${icon('download')}导出看盘表</button>`:''}<button class="btn btn-primary" data-action="open-import">${icon('upload')}${loaded?'导入 Excel':'导入数据'}</button></div></div>`;}

function render(){
  $('#app').innerHTML=state.result?renderDashboard():renderEmpty();
  if(state.pendingImport){const pending=state.pendingImport;$('.page-heading')?.insertAdjacentHTML('afterend',`<div class="pending-import-banner"><span class="issue-symbol">${icon('settings')}</span><div><strong>所选文件尚未生成看盘表 · ${esc(pending.inspection.filename)}</strong><p>${esc(pending.error||'请确认工作表和列映射后再分析。')}</p><small>${state.result?'下方保留上一份看盘结果，两份文件分别处理。':'已保留文件识别信息，可在配置页指定工作表和列。'}</small></div><button class="btn btn-subtle" data-action="configure-pending">配置所选文件 ${icon('arrow')}</button><button class="icon-btn" data-action="dismiss-pending" aria-label="关闭待配置文件提示">${icon('close')}</button></div>`);}
  if(state.historySnapshot){const history=state.historySnapshot;$('.source-strip')?.insertAdjacentHTML('beforebegin',`<div class="history-snapshot-banner">${icon('clock')}<div><strong>历史快照${history.deleted?' · 已删除记录':''}</strong><span>生成于 ${esc(dateTime(history.generatedAt))}</span>${readOnlySnapshot()?`<small>${esc(history.sourceMessage||'原始文件不可用，当前记录只读。')} 已保存的指标和问题仍可查看。</small>`:'<small>这里展示保存时的结果；重新分析将生成一条新记录。</small>'}</div><span class="tag ${readOnlySnapshot()?'':'purple'}">${readOnlySnapshot()?'只读记录':'原始文件可用'}</span></div>`);}
  if(state.activeTab==='sources'&&state.result&&!readOnlySnapshot()) fetchPreview();
}
function renderEmpty(){return `${pageHeading()}<section class="welcome-card card"><div class="welcome-copy"><div class="welcome-kicker">${icon('bolt')} 你的第一个运营工具</div><h2>连起经营数据，<br>开始今天的<span>经营看盘。</span></h2><p>自动整理工作表，关联商品与经营数据。<br>把报单进度、现货、在途和货损放在同一个视图。</p><div class="welcome-features"><span>${icon('check')} 灵活配置口径</span><span>${icon('check')} 支持手动修正</span><span>${icon('check')} 可追溯到原表</span></div></div><div class="sample-visual" aria-hidden="true"><div class="sample-visual-top"><span>经营概览</span><span>自定义工作簿</span></div><div class="sample-mini-numbers"><div><span>月计划达成</span><strong>— %</strong></div><div><span>当前现货</span><strong>¥ —</strong></div><div><span>在途库存</span><strong>¥ —</strong></div></div><div class="sample-bars">${[30,47,38,62,50,75,65,88,76,100].map(h=>`<i style="height:${h}%"></i>`).join('')}</div><div class="sample-visual-bottom"><span>每个数字，都有来源</span><span>示意视图</span></div></div></section><section class="drop-zone card" id="main-drop-zone"><div class="upload-disc">${icon('upload')}</div><h3>拖入 Excel，确认后生成看盘表</h3><p>支持多文件、多工作表 · 保留原工作簿 · 先核对范围与格式</p><div class="upload-actions"><button class="btn btn-primary" data-action="choose-file">${icon('file')}选择 Excel 文件</button><button class="btn" data-action="load-sample" ${!state.sampleAvailable?'disabled':''}>体验现有样例 ${icon('arrow')}</button><button class="btn btn-text" data-action="open-directory">从目录读取</button></div></section><div class="empty-cards"><article class="empty-feature card">${icon('table')}<div><strong>把散落的数据连起来</strong><p>关联计划、库存、销量与出库。表名或列名不同，也能调整对应关系。</p></div></article><article class="empty-feature card">${icon('alert')}<div><strong>先发现问题，再做判断</strong><p>识别日期、编码和手填金额差异。缺失数据会明确提示。</p></div></article><article class="empty-feature card">${icon('settings')}<div><strong>让工具适应你的口径</strong><p>调整目标比例、观察天数与金额来源，保存为可复用配置。</p></div></article></div><div class="empty-state-sub">${icon('info')} 文件在本机解析，源数据与手动修正分别保留。</div>`;}

function hasProductAssessment(){return state.result?.summary?.assessmentScope==='product'&&state.result?.summary?.assessmentVersion===1;}
function assessmentState(product){return hasProductAssessment()?(['met','below','unknown'].includes(product.assessmentStatus)?product.assessmentStatus:'unknown'):'legacy';}
function assessmentLabel(product){return {met:'已达标',below:'未达标',unknown:'待核对',legacy:'历史未保存'}[assessmentState(product)];}
function assessmentBadge(product){const status=assessmentState(product);return `<span class="assessment-pill ${status}">${esc(assessmentLabel(product))}</span>`;}
function dailyTaskText(item,status){
 if(finite(item.dailyRequired))return measure(item.dailyRequired,isAmountBasis());
 if(status==='below'&&Number(item.remainingDays)===0)return '已到期，仍有缺口';
 return hasProductAssessment()?'待核对':'未记录';
}
function renderAssessmentOverview(){
 const s=state.result.summary||{};
 if(!hasProductAssessment())return `<section class="assessment-overview card legacy-assessment" aria-label="历史考核口径"><div><h2 class="section-title">历史考核口径</h2><p>这条快照未保存逐款考核状态，保留原有结果。历史差额可能按总额计算，不能用来判断每款是否达标。</p><small>重新分析会按当前单品口径生成新记录，历史记录保持原样。</small></div><button class="btn btn-subtle" data-action="reanalyze" ${readOnlySnapshot()?'disabled title="请重新导入文件后分析"':''}>重新分析 ${icon('refresh')}</button></section>`;
 const incomplete=Number(s.unknownTargetCount)>0;
 const dailyStatus=Number(s.belowTargetCount)>0?'below':'met';
 const remaining=Number(s.remainingDays);
 const dailyText=incomplete?(remaining===0?'已到期，考核待核对':'待补齐数据'):dailyTaskText(s,dailyStatus);
 return `<section class="assessment-overview card" aria-labelledby="assessment-title"><div class="assessment-heading"><div><h2 class="section-title" id="assessment-title">单品目标考核<span class="section-subtitle">逐款达到 ${percent(s.targetRate??state.config.targetRate)}</span></h2><p>每款商品独立考核，超额完成不抵扣其他商品的缺口。</p></div><span class="assessment-period">${finite(s.remainingDays)?remaining>0?`剩余 ${number(remaining)} 个自然日`:'考核期间已结束':'剩余日期待核对'}</span></div><div class="assessment-stats">${[['met','已达标',s.metCount],['unmet','未达标',s.belowTargetCount],['unknown','待核对',s.unknownTargetCount]].map(([key,label,value])=>`<button class="assessment-stat ${key}" data-assessment-filter="${key}" aria-pressed="${state.statusFilter===key}" aria-label="筛选${label}商品"><span>${label}</span><strong>${number(value)}<small>款</small></strong><span class="assessment-stat-link">查看商品 ${icon('chevron')}</span></button>`).join('')}</div><div class="assessment-task-grid"><div><span>逐款未完成缺口合计</span><strong>${finite(s.targetGap)?measure(s.targetGap,isAmountBasis()):'待补齐数据'}</strong><small>${incomplete?`${finite(s.knownTargetGap)?`已知商品缺口 ${measure(s.knownTargetGap,isAmountBasis())}`:'尚无可计算的商品缺口'}，另有 ${number(s.unknownTargetCount)} 款待核对。`:'各商品正差额相加，已达标商品计为 0。'}</small></div><div><span>剩余每日需完成 · ${esc(basis())}</span><strong class="${dailyText.includes('已到期')?'assessment-expired':''}">${esc(dailyText)}</strong><small>${incomplete?(remaining===0?'本期已无剩余日期，请先核对未达标及待核对商品。':finite(s.knownDailyRequired)?`已知商品每日需完成 ${measure(s.knownDailyRequired,isAmountBasis())}，尚不是完整任务。`:'补齐计划或完成额后，才能计算完整每日任务。'):remaining>0?'逐款缺口合计 ÷ 截止日之后至月末的剩余自然日。':Number(s.belowTargetCount)>0?'本期已无剩余日期，请查看未达标商品。':'所有可评价商品均已达标，剩余任务为 0。'}</small></div></div><div class="assessment-footnote">${icon('info')}<span>考核状态与数据校验分别展示。${incomplete?'待核对商品未计作达标，完整缺口和每日任务保留未计算。':'可通过下方筛选查看未达标商品，默认按缺口优先。'}</span><button class="btn btn-text btn-small" data-assessment-filter="all">查看全部商品</button></div></section>`;
}
function renderOverallPlan(){
 const s=state.result.summary||{},current=hasProductAssessment();
 return `<section class="plan-card card"><div class="section-top"><h2 class="section-title">整体计划进度<span class="section-subtitle">合计展示，${current?'不替代单品考核':'按历史保存口径'}</span></h2><span class="period-label">${esc(periodLabel())}</span></div><div class="plan-numbers"><div><span>累计${esc(basis())}</span><strong>${measure(s.completed,isAmountBasis())}</strong></div><div class="plan-total"><span>原月计划合计</span><strong>${measure(s.plan,isAmountBasis())}</strong></div></div><div class="goal-bar"><span class="goal-bar-fill" style="width:${percentWidth(s.achievement)}%"></span></div><div class="goal-labels"><span>0%</span><span>合计达成率 ${percent(s.achievement)}</span><span>100%</span></div><div class="goal-note">${icon('info')}<span>${current?'总额达标也可能有单品未达标，请以上方逐款考核为准。':`历史保存差额（原口径） <strong>${measure(s.targetGap,isAmountBasis())}</strong>，不按新版规则重算。`}</span></div></section>`;
}
function renderDashboard(){
 const r=state.result,s=r.summary||{},count=(r.products||[]).length,issues=r.issues||[];
 const partialLabels=(s.partialMetrics||[]).map(key=>metricLabels[key]||key).join('、');
 const partialBanner=partialLabels?`<div class="info-banner warning">${icon('info')}<span>${esc(partialLabels)}存在缺失数据，相关金额汇总仅包含已提供的记录；缺少必要数据的比率保留为空，请结合数据校验查看。</span></div>`:'';
 return `${pageHeading(true)}${partialBanner}<section class="source-strip card"><div class="excel-mark">${icon('file')}</div><div><div class="source-info"><strong>${esc(r.filename||state.inspection?.filename||'工作簿')}</strong>${tag(state.historySnapshot?'历史快照':'已生成看盘','purple')}${manualCount()?tag(`${manualCount()} 款已修正`):''}</div><div class="source-caption">${r.importManifest?`${r.importManifest.inputFiles.length} 份文件 · ${r.importManifest.selectionManifest.filter(item=>item.role!=='ignore').length} 张纳入 / ${r.importManifest.selectionManifest.length} 张原表`:`${state.inspection?.sheets?.length||0} 个工作表`} · ${count} 款商品 · ${esc(basis())}口径</div></div><div class="source-time">${icon('clock')}截至 ${esc(r.asOf||state.config.asOf)}</div><button class="btn btn-small" data-action="reanalyze" ${readOnlySnapshot()?'disabled title="原始文件不可用，此历史记录只读"':''}>${icon('refresh')}${state.historySnapshot?'生成新记录':'重新分析'}</button></section><div class="kpi-grid"><article class="kpi card"><div class="kpi-header"><span>合计计划达成率</span><span class="kpi-icon">${icon('chart')}</span></div><div class="kpi-value">${finite(s.achievement)?`${number(Number(s.achievement)*100,2)}<span class="unit">%</span>`:'—'}</div><div class="kpi-footer"><span>合计展示，非单品结论</span><strong>${measure(s.completed,isAmountBasis())}</strong></div><div class="kpi-microbar"><span style="width:${percentWidth(s.achievement)}%"></span></div></article><article class="kpi card"><div class="kpi-header"><span>现货库存金额</span><span class="kpi-icon green">${icon('box')}</span></div><div class="kpi-value">${finite(s.stockAmount)?`<span class="currency">¥</span>${number(s.stockAmount)}`:'—'}</div><div class="kpi-footer"><span>按到岸价口径</span><strong>现货独立统计</strong></div><div class="kpi-microbar" style="background:var(--green-soft)"><span style="width:100%;background:var(--green)"></span></div></article><article class="kpi card"><div class="kpi-header"><span>在途库存金额</span><span class="kpi-icon blue">${icon('truck')}</span></div><div class="kpi-value">${finite(s.transitAmount)?`<span class="currency">¥</span>${number(s.transitAmount)}`:'—'}</div><div class="kpi-footer"><span>未确认到货</span><strong>不并入当前现货</strong></div><div class="kpi-microbar" style="background:var(--surface-subtle)"><span style="width:100%;background:var(--purple)"></span></div></article><article class="kpi card"><div class="kpi-header"><span>货损率</span><span class="kpi-icon amber">${icon('alert')}</span></div><div class="kpi-value">${finite(s.lossRate)?`${number(Number(s.lossRate)*100,2)}<span class="unit">%</span>`:'—'}</div><div class="kpi-footer"><span>货损金额</span><strong>${measure(s.lossAmount)}</strong></div><div class="kpi-microbar" style="background:var(--amber-soft)"><span style="width:100%;background:var(--line-strong)"></span></div></article></div>${renderAssessmentOverview()}<div class="overview-grid">${renderOverallPlan()}<section class="attention-card card"><div class="section-top"><h2 class="section-title">需要关注 <span class="count-badge">${issues.length}</span></h2><button class="btn btn-text btn-small" data-action="show-issues">查看全部 ${icon('chevron')}</button></div><div class="attention-list">${issues.length?issues.slice().sort((a,b)=>severityRank(a.severity)-severityRank(b.severity)).slice(0,3).map(issue=>`<button class="attention-item" data-action="show-issues"><span class="severity-dot ${esc(issue.severity)}"></span><span class="attention-copy"><strong>${esc(issue.title)}</strong><small>${esc(issue.sheet||issue.product||'分析口径')}${issue.cell?` · ${esc(issue.cell)}`:''}</small></span>${icon('chevron')}</button>`).join(''):`<div class="empty-attention">${icon('check')}本次未发现数据问题</div>`}</div></section></div><div class="tabbar" role="tablist" aria-label="看盘内容"><button class="tab ${state.activeTab==='dashboard'?'active':''}" data-tab="dashboard" role="tab" aria-selected="${state.activeTab==='dashboard'}">${icon('grid')}商品看盘</button><button class="tab ${state.activeTab==='issues'?'active':''}" data-tab="issues" role="tab" aria-selected="${state.activeTab==='issues'}">${icon('alert')}数据校验 <span class="tab-count">${issues.length}</span></button><button class="tab ${state.activeTab==='sources'?'active':''}" data-tab="sources" role="tab" aria-selected="${state.activeTab==='sources'}">${icon('table')}数据来源</button></div><div id="active-panel">${state.activeTab==='issues'?renderIssues():state.activeTab==='sources'?renderSources():renderReport()}</div>`;
}
function severityRank(severity){return {error:0,warning:1,info:2}[severity]??3;}
function parsePreviewLocation(value){
 const text=String(value||'').trim();
 const cell=text.match(/(?:^|[!\s,，:：])\$?([A-Z]{1,3})\$?([1-9]\d*)(?=$|[\s,，:：;；])/i)||text.match(/^\$?([A-Z]{1,3})\$?([1-9]\d*)/i);
 if(cell)return {column:cell[1].toUpperCase(),row:Number(cell[2]),cell:`${cell[1].toUpperCase()}${Number(cell[2])}`,label:`${cell[1].toUpperCase()}${Number(cell[2])}`};
 const row=text.match(/第?\s*([1-9]\d*)\s*行/)||text.match(/^([1-9]\d*)$/);
 return row?{column:null,row:Number(row[1]),cell:null,label:`第 ${Number(row[1])} 行`}:null;
}
function locatePreview(sheet,location=''){
 if(!requireSource())return;
 const target=parsePreviewLocation(location);state.selectedSheet=sheet;state.previewTarget=target?{...target,sheet}:null;
 state.previewOffset=target?Math.floor((target.row-1)/30)*30:0;state.activeTab='sources';render();
 if(!target)$('#active-panel')?.scrollIntoView({behavior:'smooth',block:'start'});
}
function renderReport(){
 const current=hasProductAssessment(),shown=filteredProducts().length;
 return `<section class="report-card card"><div class="table-toolbar"><div class="table-toolbar-left"><span class="table-title">商品明细<span class="table-count" id="product-visible-count">${shown} / ${state.result.products.length} 款商品</span></span><div class="segmented"><button class="${state.tableMode==='business'?'active':''}" data-table-mode="business">经营进度</button><button class="${state.tableMode==='inventory'?'active':''}" data-table-mode="inventory">库存与销量</button></div></div><div class="toolbar-tools"><label class="search-field">${icon('search')}<input id="product-search" type="search" placeholder="搜索商品 / 编码" value="${esc(state.search)}" aria-label="搜索商品或编码"></label><select class="small-select" id="status-filter" aria-label="筛选商品">${[['all','全部商品'],['unmet','考核未达标'],['met','考核已达标'],['unknown','考核待核对'],['attention','有数据提醒'],['manual','手动修正']].map(([key,label])=>`<option value="${key}" ${state.statusFilter===key?'selected':''} ${!current&&['unmet','met','unknown'].includes(key)?'disabled':''}>${label}</option>`).join('')}</select><select class="small-select" id="sort-select" aria-label="排序商品"><option value="target" ${state.sort==='target'?'selected':''}>${current?'未达标与缺口优先':'历史记录顺序'}</option><option value="attention" ${state.sort==='attention'?'selected':''}>数据提醒优先</option><option value="achievement" ${state.sort==='achievement'?'selected':''}>达成率升序</option><option value="stock" ${state.sort==='stock'?'selected':''}>库存金额降序</option><option value="name" ${state.sort==='name'?'selected':''}>商品名称</option></select></div></div>${state.tableMode==='inventory'?`<div class="info-banner ${state.config.inventoryComparable?'':'warning'}" style="margin:0 18px 12px">${icon('info')}<span>${state.config.inventoryComparable?'可售天数按当前现货小单位 ÷ 近期日均销量计算，在途单独展示。':'请先在分析配置中确认库存与销量属于同一库存池，随后计算现货可售天数。'}</span></div>`:''}<div class="table-wrap" id="product-table-wrap">${renderProductTable()}</div><div class="table-foot"><span>${icon('info')}点击商品查看目标完成额、每日任务、来源与数据提醒</span><span>${esc(state.config.asOf)} · ${state.config.amountMode==='calculated'?'按明细重算金额':'优先沿用源表金额'}</span></div></section>`;
}
function filteredProducts(){
 const search=state.search.toLowerCase().trim();
 const result=state.result.products.filter(product=>{
  if(search&&![product.id,product.name,product.code,product.warehouse].some(value=>String(value??'').toLowerCase().includes(search)))return false;
  if(state.statusFilter==='attention'&&!visibleIssueCount(product)&&!['error','warning'].includes(product.status))return false;
  if(state.statusFilter==='manual'&&!state.config.overrides?.[product.id])return false;
  if(state.statusFilter==='unmet'&&assessmentState(product)!=='below')return false;
  if(state.statusFilter==='met'&&assessmentState(product)!=='met')return false;
  if(state.statusFilter==='unknown'&&assessmentState(product)!=='unknown')return false;
  return true;
 });
 const attention=(a,b)=>severityRank(a.status)-severityRank(b.status)||visibleIssueCount(b)-visibleIssueCount(a);
 const assessmentRank=product=>({below:0,unknown:1,met:2}[assessmentState(product)]??3);
 const sorters={target:(a,b)=>hasProductAssessment()?(assessmentRank(a)-assessmentRank(b)||(b.targetGap??-Infinity)-(a.targetGap??-Infinity)||attention(a,b)):attention(a,b),attention,achievement:(a,b)=>(a.achievement??Infinity)-(b.achievement??Infinity),stock:(a,b)=>(b.stockAmount??-Infinity)-(a.stockAmount??-Infinity),name:(a,b)=>productName(a).localeCompare(productName(b),'zh-CN')};
 return result.sort(sorters[state.sort]||sorters.target);
}
function renderProductTable(){
 const products=filteredProducts(),inventory=state.tableMode==='inventory',current=hasProductAssessment();
 const headers=inventory?['商品 / 编码','现货小单位','现货金额','在途小单位','昨日销量',`${state.config.averageDays||7} 日均销量`,'现货可售天数','数据状态']:['商品 / 编码',`月计划 / 目标${isAmountBasis()?'金额':'数量'}`,`累计${basis()}`,'达成率','单品考核',current?'未完成缺口':'历史差额（原口径）','剩余每日任务','折价率','货损率','数据状态'];
 return `<table class="data-table ${inventory?'':'assessment-table'}"><thead><tr>${headers.map(header=>`<th scope="col">${esc(header)}</th>`).join('')}</tr></thead><tbody>${products.length?products.map((product,index)=>`<tr tabindex="0" data-product="${esc(product.id)}" data-assessment-status="${assessmentState(product)}" aria-label="查看${esc(productName(product))}"><td><div class="product-name"><span class="product-avatar tone-${index%5}">${esc(String(product.id).slice(0,2))}</span><span class="product-copy"><strong>${esc(productName(product))}${state.config.overrides?.[product.id]?'<span class="manual-tag">手动</span>':''}</strong><small>${esc(product.code||`简称 ${product.id}`)}</small></span></div></td>${inventory?`<td><span class="table-number">${integerOrDecimal(product.stockSmall)}</span><span class="sub-number">${integerOrDecimal(product.stockLarge)} 大单位</span></td><td>${amount(product.stockAmount)}</td><td>${integerOrDecimal(product.transitSmall)}</td><td>${integerOrDecimal(product.yesterdaySales)}</td><td>${number(product.averageSales,2)}</td><td><span class="${finite(product.coverageDays)&&product.coverageDays<3?'purple-text':''}">${finite(product.coverageDays)?`${number(product.coverageDays,1)} 天`:'—'}</span></td>`:`<td><span class="table-number">${planNumber(product.plan)}</span><span class="sub-number">${current?`目标 ${planNumber(product.targetAmount)}`:'历史目标未记录'}</span></td><td><span class="table-number">${planNumber(product.completed)}</span></td><td><span class="purple-text">${percent(product.achievement)}</span><div class="row-progress"><span style="width:${percentWidth(product.achievement)}%"></span></div></td><td>${assessmentBadge(product)}${current&&assessmentState(product)==='unknown'?`<span class="sub-number assessment-reason">${esc(product.assessmentReason||'缺少考核数据')}</span>`:''}</td><td>${planNumber(product.targetGap)}</td><td><span class="${current&&assessmentState(product)==='below'&&Number(product.remainingDays)===0?'assessment-expired':''}">${esc(dailyTaskText(product,assessmentState(product)))}</span></td><td>${percent(product.markdownRate)}</td><td>${percent(product.lossRate)}</td>`}<td>${productStatus(product)}</td></tr>`).join(''):`<tr><td class="empty-row" colspan="${headers.length}">没有符合条件的商品，请调整搜索或筛选。</td></tr>`}</tbody></table>`;
}
function productStatus(product){const count=visibleIssueCount(product);return `<span class="status-pill ${product.status==='error'?'error':count||product.status==='warning'?'':'good'}"><i></i>${count?`${count} 项提醒`:product.status==='warning'?'待核对':product.status==='error'?'数据异常':'无单品提醒'}</span>`;}
function updateProductTable(){const target=$('#product-table-wrap');if(target)target.innerHTML=renderProductTable();const count=$('#product-visible-count');if(count)count.textContent=`${filteredProducts().length} / ${state.result.products.length} 款商品`;$$('[data-assessment-filter]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.assessmentFilter===state.statusFilter)));}
function renderProductAssessment(product){
 const current=hasProductAssessment();
 return `<section class="product-assessment"><div class="product-assessment-heading"><h3>单品考核</h3>${assessmentBadge(product)}</div><p>${current?esc(product.assessmentReason||'按当前商品的计划与完成额独立判断。'):'该历史快照没有逐款考核状态，以下差额保留保存时的原口径。'}</p><div class="drawer-metrics"><div class="drawer-metric"><span>月计划 · ${esc(basis())}</span><strong>${planNumber(product.plan)}</strong></div><div class="drawer-metric"><span>${isAmountBasis()?'目标完成额':'目标完成量'} · ${percent(state.result.summary?.targetRate??state.config.targetRate)}</span><strong>${current?planNumber(product.targetAmount):'历史未记录'}</strong></div><div class="drawer-metric"><span>累计完成</span><strong>${planNumber(product.completed)}</strong></div><div class="drawer-metric"><span>计划达成率</span><strong>${percent(product.achievement)}</strong></div><div class="drawer-metric"><span>${current?'未完成缺口':'历史差额（原口径）'}</span><strong>${planNumber(product.targetGap)}</strong></div><div class="drawer-metric"><span>${current?'剩余每日需完成':'历史每日任务'}</span><strong class="assessment-task-value">${esc(dailyTaskText(product,assessmentState(product)))}</strong></div></div>${current?`<small>${finite(product.remainingDays)?Number(product.remainingDays)>0?`截止日后至月末剩余 ${number(product.remainingDays)} 个自然日；每天任务按单品缺口均摊。`:'当前考核期间已结束。':'剩余日期未记录。'}</small>`:''}</section>`;
}
function renderIssues(){
 const all=state.result.issues||[],filtered=all.filter(issue=>state.issueFilter==='all'||issue.severity===state.issueFilter);
 return `<div class="info-banner">${icon('info')}<span>这里显示影响数据解读的具体问题。可修改分析配置、核对原表，或在商品详情中记录本次手动修正。</span></div><div class="issues-summary">${[['all','全部'],['error','异常'],['warning','需核对'],['info','口径提示']].map(([key,label])=>`<button class="${state.issueFilter===key?'active':''}" data-issue-filter="${key}">${label} ${key==='all'?all.length:all.filter(issue=>issue.severity===key).length}</button>`).join('')}</div><div class="issues-list">${filtered.length?filtered.slice().sort((a,b)=>severityRank(a.severity)-severityRank(b.severity)).map(issue=>`<article class="issue-card card"><span class="issue-symbol ${esc(issue.severity)}">${icon(issue.severity==='info'?'info':'alert')}</span><div class="issue-content"><h3>${esc(issue.title)}</h3><p>${esc(issue.detail)}</p><div class="issue-meta">${tag({error:'异常',warning:'需核对',info:'口径提示'}[issue.severity]||'提醒')}${issue.sheet?`<button data-action="preview-issue" data-sheet="${esc(issue.sheet)}" data-cell="${esc(issue.cell||'')}">${esc(issue.sheet)}${issue.cell?`!${esc(issue.cell)}`:''}</button>`:''}${issue.product?`<span>商品 ${esc(issue.product)}</span>`:''}</div></div>${issue.product&&state.result.products.some(product=>String(product.id)===String(issue.product))?`<button class="icon-btn" data-product="${esc(issue.product)}" aria-label="查看商品">${icon('chevron')}</button>`:''}</article>`).join(''):`<div class="card empty-attention" style="padding:45px;justify-content:center">${icon('check')}当前筛选下没有问题</div>`}</div>`;
}
function renderImportManifest(){
 const manifest=state.result?.importManifest;if(!manifest)return '';
 const labels={summary:'月计划',products:'商品规格',conversion:'商品对照',inventory:'现货库存',sales:'平台销量',outbound:'仓库出库',orders:'报单记录',transit:'在途记录',loss:'货损记录',ignore:'已忽略'};
 return `<section class="card" style="padding:20px;margin-bottom:18px"><h2 class="section-title">本次导入范围</h2><p class="section-subtitle" style="margin:8px 0 14px">${(manifest.inputFiles||[]).length} 份文件 · 以下为确认时的范围；忽略的工作表未参与计算。</p><div class="table-wrap"><table class="data-table"><thead><tr><th>源文件</th><th>工作表</th><th>用途</th><th>记录数</th><th>业务日期</th></tr></thead><tbody>${(manifest.selectionManifest||[]).map(source=>`<tr><td>${esc(source.filename)}</td><td>${esc(source.sheet)}</td><td>${esc(labels[source.role]||source.role)}</td><td>${source.records??'—'}</td><td>${esc(source.resolvedSnapshotDate||source.snapshotDate||(source.role==='ignore'?'—':source.columns?.date?'读取日期列':'不适用／日期表头'))}</td></tr>`).join('')}</tbody></table></div></section>`;
}
function renderSources(){
 const sources=state.result.sources||[];
 if(readOnlySnapshot())return `${renderImportManifest()}<div class="info-banner warning">${icon('info')}<span>${esc(state.historySnapshot.sourceMessage||'原始文件不可用')}。以下保留历史分析记录中的来源信息；原表预览需要重新导入工作簿。</span></div><div class="sources-grid">${sources.filter(source=>source.sheet).map(source=>`<div class="source-card">${icon('table')}<div><strong>${esc(source.label||source.role)}<span class="section-subtitle">${esc(source.sheet)}</span></strong><small>${source.records??'—'} 条记录${source.startDate?` · ${esc(source.startDate)}${source.endDate&&source.endDate!==source.startDate?` 至 ${esc(source.endDate)}`:''}`:''}</small></div></div>`).join('')}</div>`;
 if(!state.selectedSheet)state.selectedSheet=sources.find(source=>source.sheet)?.sheet||state.inspection.sheets[0]?.name;
 return `${renderImportManifest()}<div class="info-banner">${icon('info')}<span>${state.result.importManifest?'当前是合并后的分析明细；来源文件、工作表和原始行在末列。下拉可切换保留的原表值（不执行公式）。更改范围或字段请重新合并导入。':'点击数据来源可预览原始单元格。紫色数值带有原表公式，悬停可查看。'}映射关系可以在分析配置中修改。</span></div><div class="sources-grid">${sources.filter(source=>source.sheet).map(source=>`<button class="source-card ${source.sheet===state.selectedSheet?'active':''}" data-preview-sheet="${esc(source.sheet)}">${icon('table')}<div><strong>${esc(source.label||source.role)}<span class="section-subtitle">${esc(source.sheet)}</span></strong><small>${source.records??'—'} 条记录${source.startDate?` · ${esc(source.startDate)}${source.endDate&&source.endDate!==source.startDate?` 至 ${esc(source.endDate)}`:''}`:''}</small></div></button>`).join('')}</div><section class="source-preview card"><div class="preview-heading"><p>原始数据预览 <small id="preview-subtitle">保留源表内容</small></p><select class="small-select" id="preview-sheet" aria-label="预览工作表" style="max-width:220px">${state.inspection.sheets.map(sheet=>`<option value="${esc(sheet.name)}" ${sheet.name===state.selectedSheet?'selected':''}>${esc(sheet.name)}</option>`).join('')}</select></div><div id="preview-location" class="preview-location" hidden></div><div id="preview-table"><div class="loading-inline"><div class="spinner"></div>正在读取工作表</div></div><div class="table-foot"><span id="preview-range">单次预览 30 行</span><div style="display:flex;gap:10px"><button data-action="preview-prev">上一页</button><button data-action="preview-next">下一页</button></div></div></section>`;
}
async function fetchPreview(){
 if(!requireSource())return;
 const sheet=state.selectedSheet;if(!sheet||!state.workbookId)return;
 const request=++state.previewRequest;
 try{
  const data=await api(`/api/preview?workbookId=${encodeURIComponent(state.workbookId)}&sheet=${encodeURIComponent(sheet)}&offset=${state.previewOffset}&limit=30`);
  if(request!==state.previewRequest||state.activeTab!=='sources')return;
  state.preview=data;renderPreviewTable(data);
 }catch(error){if(request===state.previewRequest&&$('#preview-table'))$('#preview-table').innerHTML=`<div class="loading-inline">${esc(error.message)}</div>`;}
}
function renderPreviewTable(data){
 const container=$('#preview-table');if(!container)return;
 const columns=data.columns||[],rows=data.rows||[];
 if(!rows.length){container.innerHTML='<div class="loading-inline">该工作表没有可预览的数据。</div>';return;}
 const cellEntries= row => Array.isArray(row.cells)?row.cells:row.cells&&typeof row.cells==='object'?Object.entries(row.cells).map(([column,value])=>({column,value})):Array.isArray(row)?row.map((value,index)=>({column:columns[index]?.column||String(index+1),value})):[];
 const columnKeys=columns.length?columns.map(column=>typeof column==='string'?column:column.column):[...new Set(rows.flatMap(row=>cellEntries(row).map(cell=>cell.column)))];
 const target=state.previewTarget?.sheet===state.selectedSheet?state.previewTarget:null;
 container.innerHTML=`<div class="table-wrap"><table class="data-table preview-table"><thead><tr><th>#</th>${columnKeys.map(column=>`<th class="${target?.column===column?'preview-target-column':''}">${esc(column)}</th>`).join('')}</tr></thead><tbody>${rows.map((row,index)=>{const cells=cellEntries(row),rowNumber=row.row??state.previewOffset+index+1,selected=target?.row===rowNumber;return `<tr data-preview-row="${rowNumber}" class="${selected?'preview-target-row':''}"><td>${rowNumber}</td>${columnKeys.map(column=>{const cell=cells.find(item=>item.column===column),value=cell?.value,coordinate=`${column}${rowNumber}`,highlight=selected&&target?.column===column;return `<td data-preview-cell="${esc(coordinate)}" class="${cell?.formula?'formula-cell ':''}${highlight?'preview-target-cell':''}" ${highlight?'tabindex="-1"':''} title="${esc(cell?.formula||value||'')}">${esc(value===null||value===undefined?'':typeof value==='object'?JSON.stringify(value):value)}</td>`;}).join('')}</tr>`;}).join('')}</tbody></table></div>`;
 const total=data.totalRows??state.inspection.sheets.find(sheet=>sheet.name===state.selectedSheet)?.rows??rows.length;
 if($('#preview-range'))$('#preview-range').textContent=`第 ${state.previewOffset+1}—${state.previewOffset+rows.length} 行 · 共 ${total} 行`;
 if($('#preview-subtitle'))$('#preview-subtitle').textContent=`${state.selectedSheet} · ${columnKeys.length} 列`;
 const location=$('#preview-location');
 if(location){location.hidden=!target;if(target){const hasRow=rows.some((row,index)=>(row.row??state.previewOffset+index+1)===target.row);location.innerHTML=`${icon('info')}<span>${hasRow?'已定位':'未找到目标行'}：<strong>${esc(state.selectedSheet)}!${esc(target.label)}</strong>${target.column&&!columnKeys.includes(target.column)?' · 目标列超出当前预览范围，已标记所在行':''}</span>`;}}
 if(target)requestAnimationFrame(()=>{const element=target.cell?$(`[data-preview-cell="${target.cell}"]`,container):null;const row=$(`[data-preview-row="${target.row}"]`,container);(element||row)?.scrollIntoView({behavior:'smooth',block:'center',inline:'center'});element?.focus({preventScroll:true});});
}

async function acceptInspection(inspection,configured){
 if(!inspection.workbookId)throw new Error('文件已解析，但未获得文件标识，请重新导入。');
 const previous=Object.fromEntries(['inspection','workbookId','config','result','historySnapshot','pendingImport','selectedSheet','previewOffset','previewTarget','preview','activeTab','selectedProduct','search','statusFilter','issueFilter'].map(key=>[key,state[key]]));
 state.inspection=inspection;state.workbookId=inspection.workbookId;state.config=clone(configured||inspection.config||{});state.config.overrides={};state.historySnapshot=null;state.pendingImport=null;state.selectedSheet=null;state.previewTarget=null;state.previewOffset=0;state.preview=null;state.activeTab='dashboard';state.selectedProduct=null;state.search='';state.statusFilter='all';state.issueFilter='all';
 try{await analyze();}catch(error){Object.assign(state,previous);state.pendingImport={inspection,config:clone(configured||inspection.config||{}),error:error.message};render();throw error;}
}
async function analyze(message='正在生成电商看盘表'){
 if(!state.workbookId)return;
 if(!requireSource())return;
 const request=++state.request;busy(true,message);
 try{const result=await api('/api/analyze',{workbookId:state.workbookId,config:state.config});if(request!==state.request)return;state.result=result;state.config=clone(result.config||state.config);state.historySnapshot=null;render();}
 finally{if(request===state.request)busy(false);}
}
async function loadSample(){closeModal();busy(true,'正在读取现有样例');try{const inspection=await api('/api/sample');await window.RetailLinkDataset.inspect(inspection);toast('请确认工作表范围后继续。');}catch(error){toast(error.message,true);}finally{busy(false);}}
async function loadFile(file){
 if(!file)return;
 closeModal();
 try{await window.RetailLinkDataset.openFiles([file]);}catch(error){toast(error.message,true);}
 finally{$('#file-input').value='';}
}
async function openPath(path){
 closeModal();try{await window.RetailLinkDataset.openPaths([path]);}catch(error){toast(error.message,true);}
}

function openHistory(deleted=false,offset=0){
 closeDrawer();state.historyDeleted=deleted;state.historyOffset=offset;
 showModal(`<section class="modal history-modal" role="dialog" aria-modal="true" aria-labelledby="history-title"><div class="modal-header"><div><h2 id="history-title">分析历史</h2><p>保留每次分析的结果、配置与问题，随时回看。</p></div><button class="icon-btn" data-action="close-modal" aria-label="关闭历史记录">${icon('close')}</button></div><div class="modal-tabs"><button data-history-view="active" class="${deleted?'':'active'}">分析记录</button><button data-history-view="deleted" class="${deleted?'active':''}">已删除</button></div><div class="modal-body"><div class="info-banner ${deleted?'warning':''}">${icon('info')}<span>${deleted?'已删除的分析记录可以恢复。原始 Excel 文件始终保留。':'打开的是保存时的结果快照。删除记录会移入“已删除”，不删除原始 Excel。'}</span></div><div id="history-list"><div class="loading-inline"><div class="spinner"></div>正在读取历史记录</div></div></div><div class="modal-footer"><small id="history-count">每页最多 50 条记录</small><div><button class="btn btn-small" data-action="history-prev" ${offset===0?'disabled':''}>上一页</button><button class="btn btn-small" id="history-next" data-action="history-next" disabled>下一页</button><button class="btn" data-action="close-modal">关闭</button></div></div></section>`);
 fetchHistory();
}
async function fetchHistory(){
 const request=++state.historyRequest;
 try{
  const data=await api(`/api/history?deleted=${state.historyDeleted?1:0}&offset=${state.historyOffset}&limit=50`);
  if(request!==state.historyRequest||!$('#history-list'))return;
  state.historyItems=data.items||[];state.historyTotal=data.total??state.historyItems.length;state.historyHasMore=Boolean(data.hasMore);
  $('#history-list').innerHTML=renderHistoryItems();
  $('#history-count').textContent=`共 ${state.historyTotal} 条${state.historyDeleted?'已删除记录':'分析记录'}${state.historyItems.length?` · 第 ${state.historyOffset+1}—${state.historyOffset+state.historyItems.length} 条`:''}`;
  $('#history-next').disabled=!state.historyHasMore;
 }catch(error){if(request===state.historyRequest&&$('#history-list'))$('#history-list').innerHTML=`<div class="history-empty">${icon('alert')}<strong>暂时无法读取历史</strong><p>${esc(error.message)}</p><button class="btn btn-small" data-action="history-retry">重新读取</button></div>`;}
}
function renderHistoryItems(){
 if(!state.historyItems.length)return `<div class="history-empty">${icon('clock')}<strong>${state.historyDeleted?'还没有已删除的记录':'还没有分析记录'}</strong><p>${state.historyDeleted?'删除的记录会保留在这里，可随时恢复。':'导入一份 Excel 生成看盘表后，分析记录会自动保存。'}</p>${!state.historyDeleted?'<button class="btn btn-subtle" data-action="open-import">导入 Excel</button>':''}</div>`;
 return `<div class="history-list">${state.historyItems.map(item=>`<article class="history-item"><span class="history-item-icon">${icon('file')}</span><div class="history-item-copy"><strong>${esc(item.filename||'未命名工作簿')}</strong><p>生成于 ${esc(dateTime(item.generatedAt))}</p><div class="history-item-meta"><span>截至 ${esc(item.asOf||'—')}</span><span>${item.summary?.productCount??'—'} 款商品</span><span>计划达成 ${percent(item.summary?.achievement)}</span>${item.appVersion?`<span>v${esc(String(item.appVersion).replace(/^v/,''))}</span>`:''}</div></div><div class="history-item-actions">${state.historyDeleted?`<button class="btn btn-small btn-subtle" data-history-restore="${esc(item.id)}">${icon('refresh')}恢复</button>`:`<button class="btn btn-small" data-history-open="${esc(item.id)}">打开快照 ${icon('arrow')}</button><button class="icon-btn" data-history-delete="${esc(item.id)}" aria-label="删除${esc(item.filename||'此分析记录')}" title="移入已删除">${icon('close')}</button>`}</div></article>`).join('')}</div>`;
}
async function openHistoryRecord(id){
 busy(true,'正在打开历史快照');
 try{
  const data=await api(`/api/history/${encodeURIComponent(id)}`),result=data.result;
  if(!result||!Array.isArray(result.products))throw new Error('此历史记录缺少完整看盘结果。');
  const item=state.historyItems.find(item=>String(item.id)===String(id));
  state.result=result;state.config=clone(result.config||{});
  state.inspection=data.inspection||{filename:result.filename,sheets:[],mappingSchema:[],config:clone(state.config),workbookId:null};
  const available=Boolean(data.sourceAvailable&&data.inspection?.workbookId);
  state.workbookId=available?data.inspection.workbookId:null;
  state.historySnapshot={id,generatedAt:result.generatedAt||data.generatedAt||item?.generatedAt,sourceAvailable:available,sourceMessage:data.sourceMessage||(available?'原始文件可用':'原始文件不可用，此记录只读。'),deleted:state.historyDeleted};
  state.activeTab='dashboard';state.search='';state.statusFilter='all';state.issueFilter='all';state.selectedSheet=null;state.previewTarget=null;state.preview=null;state.previewOffset=0;state.configTarget='current';state.selectedProduct=null;
  closeModal();render();window.scrollTo({top:0,behavior:'smooth'});toast(available?'已打开历史快照。':'已打开历史快照，原始文件不可用，当前只读。');
 }catch(error){toast(error.message,true);}finally{busy(false);}
}
async function deleteHistoryRecord(id){
 try{await api(`/api/history/${encodeURIComponent(id)}`,undefined,'DELETE');if(String(state.historySnapshot?.id)===String(id)){state.historySnapshot.deleted=true;render();}openHistory(true,0);toast('分析记录已移入“已删除”，原始 Excel 保留。');}
 catch(error){toast(error.message,true);}
}
async function restoreHistoryRecord(id){
 try{await api(`/api/history/${encodeURIComponent(id)}/restore`,{});if(String(state.historySnapshot?.id)===String(id)){state.historySnapshot.deleted=false;render();}openHistory(false,0);toast('分析记录已恢复。');}
 catch(error){toast(error.message,true);}
}

function closeModal(){const modal=$('#modal-root');modal.innerHTML='';document.body.style.overflow=$('#drawer-root').innerHTML?'hidden':'';}
function closeDrawer(){state.selectedProduct=null;$('#drawer-root').innerHTML='';document.body.style.overflow=$('#modal-root').innerHTML?'hidden':'';}
function showModal(html){$('#modal-root').innerHTML=`<div class="modal-backdrop" data-close-modal="true">${html}</div>`;document.body.style.overflow='hidden';setTimeout(()=>$('#modal-root button, #modal-root input')?.focus(),20);}
function openImport(){closeModal();window.RetailLinkDataset.open();}
function renderDirectoryFiles(){return state.directoryFiles.length?state.directoryFiles.map((file,index)=>`<div class="directory-item">${icon('file')}<div><strong>${esc(file.name)}</strong><small>${finite(file.size)?`${number(file.size/1024,1)} KB`:''}${file.modified?` · ${esc(String(file.modified).slice(0,19).replace('T',' '))}`:''}</small></div><button class="btn btn-small" data-open-directory-file="${index}">分析 ${icon('arrow')}</button></div>`).join(''):'<div class="directory-empty">输入目录后，点击“读取目录”查看工作簿。</div>';}
async function scanDirectory(){
 const path=$('#directory-path')?.value.trim();if(!path){toast('请填写目录路径。',true);return;}state.directoryPath=path;
 const target=$('#directory-list');if(target)target.innerHTML='<div class="loading-inline"><div class="spinner"></div>正在读取目录</div>';
 try{const data=await api('/api/directory',{path});state.directoryFiles=data.files||[];state.directoryPath=data.path||path;if($('#directory-list'))$('#directory-list').innerHTML=state.directoryFiles.length?renderDirectoryFiles():'<div class="directory-empty">该目录中没有 .xlsx 或 .xlsm 文件。</div>';}
 catch(error){if($('#directory-list'))$('#directory-list').innerHTML=`<div class="directory-empty">${esc(error.message)}</div>`;}
}

function openConfig(){if(!state.config){if(state.pendingImport){openPendingConfig();return;}toast('先导入一份 Excel，即可根据识别结果配置工作表和指标。');openImport();return;}if(!requireSource())return;state.configTarget='current';state.configDraft=clone(state.config);state.configTab='rules';renderConfig();}
function openPendingConfig(){if(!state.pendingImport)return;state.configTarget='pending';state.configDraft=clone(state.pendingImport.config||state.pendingImport.inspection.config||{});state.configTab='mappings';renderConfig();}
function renderConfig(){
 const c=state.configDraft;
 showModal(`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="config-title"><div class="modal-header"><div><h2 id="config-title">分析配置</h2><p>${state.configTarget==='pending'?`正在配置：${esc(state.pendingImport.inspection.filename)} · 完成映射后重新分析`:'让工作表、计算口径和经营目标对应你的业务。'}</p></div><button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-tabs"><button data-config-tab="rules" class="${state.configTab==='rules'?'active':''}">指标与口径</button><button data-config-tab="mappings" ${state.result?.importManifest?'disabled title="请通过合并导入重新确认工作表与字段"':''} class="${state.configTab==='mappings'?'active':''}">工作表与列映射</button></div><div class="modal-body"><div class="config-profiles"><label for="config-profile-select">已保存配置</label><select id="config-profile-select"><option value="">选择一份配置…</option>${profiles.map((profile,index)=>`<option value="${index}">${esc(profile.name)}</option>`).join('')}</select><button class="btn btn-small" data-action="load-profile">应用</button><button class="btn btn-text btn-small" data-action="reset-config">恢复自动识别</button></div>${state.configTab==='rules'?renderRuleFields(c):renderMappingFields(c)}<div class="form-section">${icon('settings')} 保存为可复用配置</div><div style="display:flex;gap:8px"><label class="field" style="flex:1"><input id="profile-name" placeholder="例如：南京月度看盘口径" aria-label="配置名称"></label><button class="btn" data-action="save-profile">保存配置</button></div><p style="font-size:10px;color:var(--muted);margin:9px 0 0">保存口径与映射，不携带当前商品的手动修正；导入新文件后可在这里应用。</p></div><div class="modal-footer"><small>当前默认口径来自样例公式，可根据实际业务调整。</small><div><button class="btn" data-action="close-modal">取消</button><button class="btn btn-primary" data-action="apply-config">应用并重新分析 ${icon('arrow')}</button></div></div></section>`);
}
function renderRuleFields(c){return `<div class="form-section">${icon('chart')} 统计期间与目标</div><div class="form-grid"><label class="field">统计月份<input id="cfg-period" type="month" value="${esc(c.period||'')}"><small>计划、报单、出库与货损按所选月份统计。</small></label><label class="field">数据截止日期<input id="cfg-asof" type="date" value="${esc(c.asOf||'')}"><small>排除截止日期之后的数据，库存按导入快照展示。</small></label><label class="field">计划达成口径<select id="cfg-plan-basis">${Object.entries(basisLabels).map(([key,label])=>`<option value="${key}" ${c.planBasis===key?'selected':''}>${label}</option>`).join('')}</select><small>原计划值不会自动换算单位；切换前请核对计划是金额还是数量。</small></label><label class="field">目标达成门槛（%）<input id="cfg-target" type="number" min="1" max="200" step="0.1" value="${numberInput((c.targetRate??.85)*100)}"><small>每款商品独立判断；总缺口合计逐款正差额，超额不抵其他款。</small></label><label class="field">日均销量观察天数<input id="cfg-average-days" type="number" min="1" max="90" step="1" value="${numberInput(c.averageDays||7)}"><small>观察窗口内有完整数据时计算，真实零销量保留。</small></label><label class="field">在途超期提醒（自然日）<input id="cfg-transit-days" type="number" min="1" max="90" step="1" value="${numberInput(c.transitDays||5)}"><small>超期仅标记待确认，不自动计为到货或删除。</small></label><label class="field" style="grid-column:1/-1">金额取值方式<select id="cfg-amount-mode"><option value="source" ${c.amountMode!=='calculated'?'selected':''}>优先沿用源表金额，并提示与明细的差异</option><option value="calculated" ${c.amountMode==='calculated'?'selected':''}>按当前期间的数量与单价重新计算</option></select><small>源表金额仅在明细覆盖所选期间时使用；跨期或截止日改变时按有效明细处理。</small></label></div><div class="form-section">${icon('info')} 需要人为明确的对应关系</div><label class="switch-row"><input id="cfg-align-year" type="checkbox" ${c.alignSalesYear?'checked':''}><span><strong>将销量表年份对齐到统计年份</strong><small>仅适用于确认沿用旧模板日期的情况。启用后保留日期偏移提醒，源文件日期不变。</small></span></label><label class="switch-row"><input id="cfg-comparable" type="checkbox" ${c.inventoryComparable?'checked':''}><span><strong>确认现货与销量属于同一库存池</strong><small>启用后按现货小单位与日均销量计算可售天数；自有仓和平台仓库存应分别核对。</small></span></label>`;}
function numberInput(value){return finite(value)?String(Number(value)):'';}
function renderMappingFields(c){const inspection=configInspection();return `<div class="info-banner">${icon('info')}<span>选择各类数据所在的工作表。列映射使用 Excel 列字母；空映射表示该字段未提供，缺少工作表可跳过。</span></div><div class="mapping-list">${(inspection.mappingSchema||[]).map(role=>{
 const mapping=c.mappings?.[role.role]||{sheet:'',headerRow:1,columns:{}};
 const sheet=inspection.sheets.find(sheet=>sheet.name===mapping.sheet);
 const headerOptions=(sheet?.headers||[]).map(header=>({column:header.column,label:header.label}));
 return `<section class="mapping-section" data-role="${esc(role.role)}"><div class="mapping-top"><strong>${esc(role.label)}</strong><select data-mapping-sheet="${esc(role.role)}" aria-label="${esc(role.label)}工作表"><option value="">未提供 / 跳过</option>${inspection.sheets.map(sheet=>`<option value="${esc(sheet.name)}" ${mapping.sheet===sheet.name?'selected':''}>${esc(sheet.name)}</option>`).join('')}</select><label>表头行<input data-mapping-header="${esc(role.role)}" type="number" min="1" max="100" value="${mapping.headerRow||1}" aria-label="${esc(role.label)}表头行"></label></div><div class="mapping-fields">${(role.fields||[]).map(field=>`<label>${esc(field.label)}<select data-mapping-column="${esc(field.key)}" data-mapping-role="${esc(role.role)}" aria-label="${esc(role.label)}${esc(field.label)}"><option value="">未映射</option>${allColumnOptions(headerOptions,mapping.columns?.[field.key]).map(header=>`<option value="${esc(header.column)}" ${mapping.columns?.[field.key]===header.column?'selected':''}>${esc(header.column)}${header.label?` · ${esc(String(header.label).slice(0,24))}`:''}</option>`).join('')}</select></label>`).join('')}</div></section>`;
 }).join('')}</div>`;}
function allColumnOptions(headers,current){
 const map=new Map(headers.filter(item=>item.column).map(item=>[item.column,item]));
 for(let i=1;i<=52;i++){const col=String.fromCharCode(65+(i-1)%26);const letter=i>26?'A'+col:col;if(!map.has(letter))map.set(letter,{column:letter,label:''});}
 if(current&&!map.has(current))map.set(current,{column:current,label:''});
 return [...map.values()].sort((a,b)=>a.column.length-b.column.length||a.column.localeCompare(b.column));
}
function captureConfig(){
 const c=state.configDraft;if(!c)return;
 if($('#cfg-period')){
  c.period=$('#cfg-period').value;c.asOf=$('#cfg-asof').value;c.planBasis=$('#cfg-plan-basis').value;c.targetRate=Number($('#cfg-target').value)/100;c.averageDays=Number($('#cfg-average-days').value);c.transitDays=Number($('#cfg-transit-days').value);c.amountMode=$('#cfg-amount-mode').value;c.alignSalesYear=$('#cfg-align-year').checked;c.inventoryComparable=$('#cfg-comparable').checked;
 }
 $$('[data-mapping-sheet]').forEach(element=>{const role=element.dataset.mappingSheet;c.mappings??={};c.mappings[role]??={columns:{}};c.mappings[role].sheet=element.value;c.mappings[role].headerRow=Number($(`[data-mapping-header="${role}"]`).value)||1;});
 $$('[data-mapping-column]').forEach(element=>{const role=element.dataset.mappingRole;c.mappings[role].columns??={};c.mappings[role].columns[element.dataset.mappingColumn]=element.value;});
}
function validateConfig(c){
 if(!/^\d{4}-\d{2}$/.test(c.period||''))throw new Error('请选择有效的统计月份。');
 if(!/^\d{4}-\d{2}-\d{2}$/.test(c.asOf||''))throw new Error('请选择有效的截止日期。');
 if(!finite(c.targetRate)||c.targetRate<=0||c.targetRate>2)throw new Error('目标门槛应在 0% 至 200% 之间。');
 if(!Number.isInteger(c.averageDays)||c.averageDays<1||c.averageDays>90)throw new Error('日均销量观察天数应为 1—90 天的整数。');
 if(!Number.isInteger(c.transitDays)||c.transitDays<1||c.transitDays>90)throw new Error('在途提醒天数应为 1—90 天的整数。');
}
async function applyConfig(){
 try{
  captureConfig();validateConfig(state.configDraft);
  if(state.configTarget==='pending'){
   if(!state.pendingImport)throw new Error('待配置文件已关闭，请重新导入。');
   const configured=clone(state.configDraft),inspection=state.pendingImport.inspection;
   await acceptInspection(inspection,configured);state.configTarget='current';closeModal();toast('已按人工映射生成看盘表。');return;
  }
  if(!requireSource())return;
  const previous=state.config;state.config=clone(state.configDraft);try{await analyze('正在按新口径重新分析');closeModal();toast('分析配置已应用。');}catch(error){state.config=previous;throw error;}
 }
 catch(error){toast(error.message,true);}
}
function saveProfile(){
 try{captureConfig();validateConfig(state.configDraft);const name=$('#profile-name')?.value.trim();if(!name){toast('请填写配置名称。',true);$('#profile-name')?.focus();return;}const config=clone(state.configDraft);delete config.overrides;const existing=profiles.findIndex(profile=>profile.name===name);const item={name,config,savedAt:new Date().toISOString()};if(existing>=0)profiles[existing]=item;else profiles.unshift(item);profiles=profiles.slice(0,12);localStorage.setItem(storageKey,JSON.stringify(profiles));queueDesktopPreferences();renderConfig();toast(`已保存配置“${name}”。`);}
 catch(error){toast(error.message||'配置保存失败，请检查浏览器存储。',true);}
}
function loadProfile(){
 captureConfig();const value=$('#config-profile-select')?.value;if(value===''){toast('先选择一份已保存配置。');return;}const profile=profiles[Number(value)];if(!profile)return;
 const overrides=clone(state.configDraft.overrides||{});state.configDraft={...clone(configInspection().config),...clone(profile.config),overrides};const names=new Set(configInspection().sheets.map(sheet=>sheet.name));
 Object.values(state.configDraft.mappings||{}).forEach(mapping=>{if(mapping.sheet&&!names.has(mapping.sheet))mapping.sheet='';});
 renderConfig();toast('已载入配置，请检查月份与工作表后应用。');
}

function openProduct(id){
 const product=state.result?.products.find(product=>String(product.id)===String(id));if(!product)return;
 state.selectedProduct=product.id;const overrides=state.config.overrides?.[product.id]||{};
 const issues=(state.result.issues||[]).filter(issue=>String(issue.product??'')===String(product.id));
 const sourceEntries=Object.entries(product.sources||{}).filter(([,source])=>source!==null&&source!==undefined&&source!=='');
 $('#drawer-root').innerHTML=`<div class="drawer-backdrop" data-close-drawer="true"><section class="drawer" role="dialog" aria-modal="true" aria-labelledby="product-title"><div class="modal-header"><div class="drawer-product"><span class="product-avatar">${esc(String(product.id).slice(0,2))}</span><div><h2 id="product-title">${esc(productName(product))}</h2><small>${esc(product.code||product.id)} · ${esc(product.warehouse||'库存归属待核对')}</small></div></div><button class="icon-btn" data-action="close-drawer" aria-label="关闭商品详情">${icon('close')}</button></div><div class="drawer-body">${renderProductAssessment(product)}<div class="form-section">${icon('box')} 库存与销量</div><div class="drawer-metrics"><div class="drawer-metric"><span>现货小单位 / 在途小单位</span><strong>${integerOrDecimal(product.stockSmall)} <span style="color:var(--muted)">/</span> ${integerOrDecimal(product.transitSmall)}</strong></div><div class="drawer-metric"><span>现货可售天数</span><strong>${finite(product.coverageDays)?`${number(product.coverageDays,1)} 天`:'—'}</strong></div></div>${issues.length?`<div class="form-section">${icon('alert')} 本商品需要核对</div><div class="drawer-issues">${issues.map(issue=>`<p><strong style="font-weight:500">${esc(issue.title)}</strong><br>${esc(issue.detail)}</p>`).join('')}</div>`:''}<div class="form-section">${icon('edit')} 本次分析的手动修正 ${Object.keys(overrides).length?tag('已修正','purple'):''}</div><div class="manual-form"><div class="manual-caption">填入数值以覆盖本次结果，留空沿用原始数据。修正与源文件分别保存；导入新文件时不自动沿用。</div><div class="form-grid">${[{key:'plan',label:`月计划${isAmountBasis()?'金额':'数量'}`,value:product.plan},{key:'stockSmall',label:'现货小单位数量',value:product.stockSmall},{key:'stockAmount',label:'现货金额',value:product.stockAmount},{key:'supplyPrice',label:'出库供价（小单位）',value:finite(product.outboundQuantity)&&product.outboundQuantity!==0&&finite(product.supplyAmount)?product.supplyAmount/product.outboundQuantity:null},{key:'lossAmount',label:'货损金额',value:product.lossAmount}].map(field=>`<label class="field">${esc(field.label)}<input type="number" step="any" data-manual-field="${field.key}" value="${numberInput(overrides[field.key])}" placeholder="当前 ${integerOrDecimal(field.value)}"></label>`).join('')}</div></div><div class="form-section">${icon('table')} 数据来源 <span class="section-subtitle">点击可查看原表</span></div><div class="drawer-source-list">${sourceEntries.length?sourceEntries.map(([key,source])=>{const value=typeof source==='string'?source:JSON.stringify(source);const match=value.match(/^'?([^!']+)'?!/);return `<div><span>${esc(metricLabels[key]||key)}</span>${match?`<button data-source-link="${esc(match[1])}" data-source-cell="${esc(value.split('!').slice(1).join('!'))}">${esc(value)}</button>`:`<code>${esc(value)}</code>`}</div>`;}).join(''):'<div>当前商品暂无单元格定位信息</div>'}</div></div><div class="drawer-footer"><button class="btn btn-text btn-small" data-action="reset-product">还原此商品</button><div><button class="btn" data-action="close-drawer">关闭</button><button class="btn btn-primary" data-action="save-product">应用修正 ${icon('check')}</button></div></div></section></div>`;
 if(readOnlySnapshot()){
  $$('.manual-form input').forEach(input=>{input.disabled=true;});
  $('[data-action="save-product"]')?.setAttribute('disabled','');$('[data-action="reset-product"]')?.setAttribute('disabled','');
  $('.manual-caption').textContent='此处展示历史保存的修正。原始文件不可用，当前快照只读，无法新增或还原修正。';
  $$('[data-source-link]',$('#drawer-root')).forEach(button=>{button.disabled=true;button.title='原始文件不可用，无法预览单元格。';});
 }
 document.body.style.overflow='hidden';setTimeout(()=>$('#drawer-root .icon-btn')?.focus(),20);
}
async function saveProduct(reset=false){
 if(!requireSource())return;
 const id=state.selectedProduct;if(id===null)return;const original=clone(state.config.overrides||{});const overrides=state.config.overrides??={};
 try{
  if(reset)delete overrides[id];else{const values={};for(const input of $$('[data-manual-field]')){const raw=input.value.trim();if(raw==='')continue;const value=Number(raw);if(!Number.isFinite(value))throw new Error('手动修正必须填写有效数字。');if(['plan','stockAmount','supplyPrice','lossAmount'].includes(input.dataset.manualField)&&value<0)throw new Error('计划、价格和金额应填写非负数。');values[input.dataset.manualField]=value;}if(Object.keys(values).length)overrides[id]=values;else delete overrides[id];}
  await analyze(reset?'正在还原商品数据':'正在应用手动修正');openProduct(id);toast(reset?'此商品已恢复源数据计算。':'手动修正已应用到当前分析。');
 }catch(error){state.config.overrides=original;toast(error.message,true);}
}
function csvCell(value){
 let text=value===null||value===undefined?'':String(value);if(typeof value==='string'&&/^[\s]*[=+\-@\t\r]/.test(text))text="'"+text;return `"${text.replace(/"/g,'""')}"`;
}
function exportReport(){
 if(!state.result)return;const r=state.result;
 const fields=[['id','商品简称'],['name','商品名称'],['code','商品编码'],['warehouse','仓库'],['plan',`月计划（${basis()}）`],['completed',`累计完成（${basis()}）`],['targetAmount',isAmountBasis()?'目标完成额':'目标完成量'],['achievement','达成率'],['assessmentStatus','单品考核状态'],['assessmentReason','考核说明'],['targetGap',hasProductAssessment()?'单品未完成缺口':'历史差额（原口径）'],['dailyRequired','剩余每日需完成'],['remainingDays','剩余自然日'],['stockLarge','现货大单位'],['stockSmall','现货小单位'],['stockAmount','现货金额'],['transitSmall','在途小单位'],['transitAmount','在途金额'],['yesterdaySales','昨日销量'],['averageSales',`${state.config.averageDays}日均销量`],['coverageDays','现货可售天数'],['outboundAmount','出库到岸金额'],['supplyAmount','出库供价金额'],['markdownRate','折价率'],['lossAmount','货损金额'],['lossRate','货损率']];
 const rows=[['统计月份',r.period,'截止日期',r.asOf,'计划口径',basis()],['考核口径',hasProductAssessment()?'按单品独立考核；超额不抵其他款':'历史保存口径，未重算'],['源文件',r.filename,'目标门槛',percent(state.config.targetRate),'金额模式',state.config.amountMode==='calculated'?'明细重算':'优先源表'],[],fields.map(([,label])=>label),...filteredProducts().map(product=>fields.map(([key])=>key==='assessmentStatus'?assessmentLabel(product):key==='assessmentReason'&&!hasProductAssessment()?'历史未保存逐款考核说明':['achievement','markdownRate','lossRate'].includes(key)?percent(product[key])==='—'?'':percent(product[key]):product[key]))];
 const blob=new Blob(['\ufeff'+rows.map(row=>row.map(csvCell).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=`电商看盘表_${r.period}_${r.asOf}.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(link.href),5000);toast('已导出当前筛选下的看盘结果。');
}
let exportPreview=null;
function exportXlsxModal(){
 if(!state.result){toast('先生成一份看盘结果。',true);return;}
 exportPreview=null;
 const defaultName=`零售通看盘-${state.result.asOf.replaceAll('-','')}.xlsx`;
 showModal(`<section class="modal small" role="dialog" aria-modal="true" aria-labelledby="export-title"><div class="modal-header"><div><h2 id="export-title">生成 Excel 看盘表</h2><p>先预览保存位置，再生成文件。原始工作簿保持只读。</p></div><button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><div id="export-message" class="info-banner">${icon('info')}<span>请选择本机目录。遇到同名文件时，可改名另存或确认覆盖并保留备份。</span></div><div class="form-grid"><label class="field">保存目录<input id="export-target-dir" value="${esc(state.defaultExportDirectory||'')}" placeholder="本机现有目录的完整路径"><small>可填入其他现有目录；该目录将直接接收文件。</small></label><label class="field">文件名<input id="export-filename" value="${esc(defaultName)}"></label><label class="field">导出商品范围<select id="export-scope"><option value="all">全部商品</option><option value="filtered">当前筛选下的商品</option></select><small>筛选导出时，总览仍标明完整快照汇总口径。</small></label></div><div class="export-sheets-note">包含：看盘总览、商品明细、库存与销量、数据问题、数据来源。保存已计算结果，缺失数据留空。</div><div id="export-receipt"></div></div><div class="modal-footer"><small>重名覆盖前备份 · 写后校验</small><div><button class="btn" data-action="export-csv-from-modal">CSV 下载</button><button class="btn btn-primary" data-action="preview-xlsx">预览保存</button><button class="btn btn-primary" id="export-commit" data-action="export-xlsx" hidden>确认生成</button></div></div></section>`);
}
async function previewXlsx(){
 const targetDir=$('#export-target-dir')?.value.trim(),filename=$('#export-filename')?.value.trim();
 if(!targetDir||!filename){toast('请填写保存目录和文件名。',true);return;}
 try{
  const productIds=$('#export-scope').value==='filtered'?filteredProducts().map(p=>String(p.id)):null;
  const preview=await api('/api/export/preview',{snapshotId:state.result.id,targetDir,filename,productIds});
  exportPreview=preview;
  $('#export-message').innerHTML=`${icon(preview.exists?'alert':'info')}<span><strong>${preview.exists?'已有同名文件':'将生成新文件'}</strong><br>${esc(preview.path)}<br>${preview.productCount} 款商品 · ${esc(preview.scope)}${preview.exists?'<br>确认后会先备份旧文件，再替换。也可改名后重新预览。':''}</span>`;
  const button=$('#export-commit');button.hidden=false;button.textContent=preview.exists?'确认覆盖并备份':'确认生成';
 }catch(error){exportPreview=null;toast(error.message,true);}
}
async function exportXlsx(){
 if(!exportPreview){toast('请先预览保存位置。',true);return;}
 const button=$('#export-commit');if(button)button.disabled=true;busy(true,'正在写入并校验 Excel');
 try{
  const result=await api('/api/export/xlsx',{token:exportPreview.token,overwrite:exportPreview.exists});
  exportPreview=null;
  $('#export-receipt').innerHTML=`<div class="export-receipt"><strong>已保存并通过校验</strong><p>${esc(result.path)}</p><small>${number(result.bytes/1024,1)} KB · ${esc(dateTime(result.createdAt))}<br>SHA-256：${esc(result.sha256)}</small><div class="export-receipt-actions"><a class="btn" href="/api/export/download/${encodeURIComponent(result.operationId)}" download>下载同一份 Excel</a>${result.backupPath?`<button class="btn" data-action="rollback-export" data-operation-id="${esc(result.operationId)}">恢复覆盖前版本</button>`:''}</div></div>`;
  if(button)button.hidden=true;toast('Excel 看盘表已保存。');
 }catch(error){exportPreview=null;if(button)button.hidden=true;toast(error.message,true);}
 finally{if(button)button.disabled=false;busy(false);}
}
async function rollbackExport(operationId){
 try{const result=await api('/api/export/rollback',{operationId,confirmed:true});toast('已恢复覆盖前版本。');if($('#export-receipt'))$('#export-receipt').innerHTML=`<div class="export-receipt"><strong>已恢复旧版本</strong><p>${esc(result.path)}</p></div>`;if($('#data-logs'))loadOperationLogs();}catch(error){toast(error.message,true);}
}
function collectPreferences(){
 const read=(key,fallback)=>{try{return JSON.parse(localStorage.getItem(key)||'null')??fallback;}catch{return fallback;}};
 return {version:1,profiles:clone(profiles),appearance:read('operator-console-appearance-v1',{}),dailyMapping:read('operator-daily-mapping-v1',{})};
}
function applyImportedPreferences(preferences){
 if(!preferences)return;
 profiles=preferences.profiles||[];localStorage.setItem(storageKey,JSON.stringify(profiles));
 localStorage.setItem('operator-daily-mapping-v1',JSON.stringify(preferences.dailyMapping||{}));
 window.OperatorAppearance?.applyPreferences(preferences.appearance||{});
}
let desktopPreferencesReady=false,desktopSaveTimer=null;
function queueDesktopPreferences(){
 if(!desktopPreferencesReady)return;
 clearTimeout(desktopSaveTimer);
 desktopSaveTimer=setTimeout(()=>api('/api/preferences',{preferences:collectPreferences()}).catch(error=>toast('设置尚未保存：'+error.message,true)),180);
}
window.addEventListener('operator-appearance-change',queueDesktopPreferences);
window.addEventListener('retail-link-preferences-change',queueDesktopPreferences);
let migrationPreview=null;
function openDataTools(){
 migrationPreview=null;
 const defaultPath=state.defaultExportDirectory||'';
 showModal(`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="data-title"><div class="modal-header"><div><h2 id="data-title">本地数据与操作记录</h2><p>迁移历史、每日记录和配置，不包含用户原始 Excel 和已生成的看盘文件。</p></div><button class="icon-btn" data-action="close-modal" aria-label="关闭">${icon('close')}</button></div><div class="modal-body"><div class="form-section">迁移包</div><div class="form-grid"><label class="field">导出 ZIP 的完整路径<input id="bundle-output" value="${esc(defaultPath+'/零售通数据-'+new Date().toLocaleDateString('sv-SE')+'.zip')}"><small>默认不覆盖同名文件；可修改文件名另存。</small></label><label class="field">导入 ZIP 的完整路径<input id="bundle-input" placeholder="选择已有迁移包，填写本机完整路径"></label></div><div class="data-tool-actions"><button class="btn" data-action="bundle-export">导出本地数据与当前配置</button><button class="btn" data-action="bundle-preview">预览导入包</button></div><div id="migration-message" class="info-banner">${icon('info')}<span>先预览再导入。默认合并不覆盖；完全替换会先备份现有数据。</span></div><div id="migration-options" hidden><label class="field">冲突处理<select id="bundle-mode"><option value="merge">合并：保留已有历史；每日数据库冲突时停止</option><option value="replace">替换：替换历史、每日数据和配置，保留迁移前备份</option></select></label><button class="btn btn-primary" data-action="bundle-import">确认导入</button></div><div class="form-section">最近操作记录</div><div class="data-tool-actions"><button class="btn btn-small" data-action="refresh-operation-logs">刷新记录</button><button class="btn btn-small" data-action="save-operation-logs">保存日志到上述目录</button></div><div id="data-logs">正在读取…</div></div><div class="modal-footer"><small>JSON / SQLite / JSONL · 无后台轮询</small><div><button class="btn" data-action="close-modal">关闭</button></div></div></section>`);
 loadOperationLogs();
}
async function loadOperationLogs(){
 try{const data=await api('/api/export/logs?limit=100');const restored=new Set(data.items.filter(x=>x.operation==='rollback'&&x.result==='rolled_back').map(x=>x.sourceOperationId));if($('#data-logs'))$('#data-logs').innerHTML=data.items.length?data.items.slice().reverse().map(item=>`<div class="operation-row"><strong>${esc(({read:'读取',analyze:'分析',export:'生成Excel',rollback:'恢复版本',import:'导入迁移包',data_export:'导出迁移包',data_import:'导入迁移包',preferences_save:'保存配置',import_review:'确认导入范围',request:'请求失败',history_restore:'恢复历史',history_delete:'删除历史',daily_save:'保存每日记录',daily_delete:'删除每日记录',daily_restore:'恢复每日记录'})[item.operation]||item.operation)}</strong><span>${esc(dateTime(item.time))}</span><span>${esc(({success:'成功',started:'开始',failed:'失败',rolled_back:'已恢复',partial:'部分完成'})[item.result]||item.result||'')}</span>${((item.operation==='export'&&item.result==='success')||item.operation==='rollback'&&item.result==='rolled_back')&&item.backupPath&&!restored.has(item.operationId)?`<button class="btn btn-small" data-action="rollback-export" data-operation-id="${esc(item.operationId)}">${item.operation==='rollback'?'撤销上次恢复':'恢复覆盖前版本'}</button>`:''}</div>`).join(''):'<p>暂无操作记录。</p>';}catch(error){if($('#data-logs'))$('#data-logs').textContent=error.message;}
}
async function exportBundle(){
 busy(true,'正在打包本地数据');
 try{await api('/api/preferences',{preferences:collectPreferences()});const result=await api('/api/data/bundle',{path:$('#bundle-output').value.trim()});$('#migration-message').innerHTML=`${icon('check')}<span>迁移包已生成：${esc(result.path)}<br>${number(result.bytes/1024,1)} KB · SHA-256 ${esc(result.sha256)}</span>`;await loadOperationLogs();}catch(error){toast(error.message,true);}finally{busy(false);}
}
async function previewBundle(){
 try{migrationPreview=await api('/api/data/preview',{path:$('#bundle-input').value.trim()});$('#migration-message').innerHTML=`${icon('info')}<span>导入包检查通过。<br>${esc(Object.entries(migrationPreview.counts).map(([key,value])=>(({history:'历史快照',historyDeleted:'已删除快照',dailyRecords:'每日记录',dailyRevisions:'日记录旧版',productCatalog:'商品关联',preferences:'配置',importTemplates:'导入格式模板',logFiles:'日志文件'})[key]||key)+' '+value).join('，'))}<br>${esc((migrationPreview.conflicts||[]).join('；')||'未发现已有数据冲突')}<br>请核对冲突处理方式，确认后开始导入。</span>`;$('#migration-options').hidden=false;}catch(error){migrationPreview=null;$('#migration-options').hidden=true;toast(error.message,true);}
}
async function importBundle(){
 if(!migrationPreview){toast('请重新预览导入包。',true);return;}
 busy(true,'正在导入并校验本地数据');
 try{const result=await api('/api/data/import',{path:$('#bundle-input').value.trim(),token:migrationPreview.token,replace:$('#bundle-mode').value==='replace'});migrationPreview=null;applyImportedPreferences(result.preferences);$('#migration-options').hidden=true;$('#migration-message').innerHTML=`${icon('check')}<span>本地数据已导入。${result.backupPath?'<br>迁移前备份：'+esc(result.backupPath):''}<br>打开历史可查看导入结果；当前页面继续显示原来的分析快照。</span>`;await loadOperationLogs();}catch(error){toast(error.message,true);}finally{busy(false);}
}
async function saveOperationLogs(){
 try{const full=$('#bundle-output')?.value.trim()||'';const directory=full.replace(/[\\/][^\\/]*$/, '');const path=directory+'/零售通操作日志-'+Date.now()+'.jsonl';const result=await api('/api/export/logs',{path});toast('操作日志已保存至 '+result.path);}catch(error){toast(error.message,true);}
}

document.addEventListener('click',async event=>{
 const target=event.target.closest('button,a,[data-product]');
 if(event.target.classList.contains('modal-backdrop')){closeModal();return;}
 if(event.target.classList.contains('drawer-backdrop')){closeDrawer();return;}
 if(!target)return;
 if(target.dataset.historyView){openHistory(target.dataset.historyView==='deleted',0);return;}
 if(target.dataset.historyOpen){await openHistoryRecord(target.dataset.historyOpen);return;}
 if(target.dataset.historyDelete){await deleteHistoryRecord(target.dataset.historyDelete);return;}
 if(target.dataset.historyRestore){await restoreHistoryRecord(target.dataset.historyRestore);return;}
 if(target.dataset.tab){state.activeTab=target.dataset.tab;render();return;}
 if(target.dataset.tableMode){state.tableMode=target.dataset.tableMode;$('#active-panel').innerHTML=renderReport();return;}
 if(target.dataset.issueFilter){state.issueFilter=target.dataset.issueFilter;$('#active-panel').innerHTML=renderIssues();return;}
 if(target.dataset.previewSheet){state.selectedSheet=target.dataset.previewSheet;state.previewTarget=null;state.previewOffset=0;$('#active-panel').innerHTML=renderSources();fetchPreview();return;}
 if(target.dataset.sourceLink){closeDrawer();locatePreview(target.dataset.sourceLink,target.dataset.sourceCell);return;}
 if(target.dataset.importTab){state.directoryPath=$('#directory-path')?.value||state.directoryPath;openImport(target.dataset.importTab);return;}
 if(target.dataset.configTab){captureConfig();state.configTab=target.dataset.configTab;renderConfig();return;}
 if(target.hasAttribute('data-open-directory-file')){const file=state.directoryFiles[Number(target.dataset.openDirectoryFile)];if(file)openPath(file.path);return;}
 if(target.dataset.assessmentFilter){state.statusFilter=state.statusFilter===target.dataset.assessmentFilter?'all':target.dataset.assessmentFilter;state.sort='target';state.activeTab='dashboard';render();$('#active-panel')?.scrollIntoView({behavior:'smooth',block:'start'});return;}
 if(target.dataset.product){openProduct(target.dataset.product);return;}
 if(target.dataset.nav){state.activeTab='dashboard';if(state.result)render();return;}
 const action=target.dataset.action;if(!action)return;
 const handlers={
  'open-history':()=>openHistory(false,0),'history-retry':fetchHistory,
  'history-prev':()=>openHistory(state.historyDeleted,Math.max(0,state.historyOffset-50)),
  'history-next':()=>{if(state.historyHasMore)openHistory(state.historyDeleted,state.historyOffset+50);},
  'configure-pending':openPendingConfig,
  'dismiss-pending':()=>{state.pendingImport=null;render();},
  'choose-file':()=>$('#file-input').click(),'open-import':()=>openImport(),'open-directory':()=>openImport('directory'),'close-modal':closeModal,'load-sample':loadSample,'open-config':openConfig,'close-drawer':closeDrawer,'save-product':()=>saveProduct(),'reset-product':()=>saveProduct(true),'apply-config':applyConfig,'save-profile':saveProfile,'load-profile':loadProfile,'scan-directory':scanDirectory,'export':exportXlsxModal,'preview-xlsx':previewXlsx,'open-data-tools':openDataTools,'refresh-operation-logs':loadOperationLogs,'save-operation-logs':saveOperationLogs,'bundle-export':exportBundle,'bundle-preview':previewBundle,'bundle-import':importBundle,'export-csv-from-modal':()=>{closeModal();exportReport();},'export-xlsx':exportXlsx,'rollback-export':()=>rollbackExport(target.dataset.operationId),
  'reanalyze':async()=>{try{await analyze();toast('已按当前配置重新分析。');}catch(error){toast(error.message,true);}},
  'show-issues':()=>{state.activeTab='issues';render();$('#active-panel')?.scrollIntoView({behavior:'smooth',block:'start'});},
  'preview-issue':()=>locatePreview(target.dataset.sheet,target.dataset.cell),
  'preview-prev':()=>{if(state.previewOffset>0){state.previewTarget=null;state.previewOffset=Math.max(0,state.previewOffset-30);fetchPreview();}},
  'preview-next':()=>{if(state.previewOffset+30<(state.preview?.totalRows||0)){state.previewTarget=null;state.previewOffset+=30;fetchPreview();}},
  'reset-config':()=>{const overrides=clone(state.configDraft.overrides||{});state.configDraft={...clone(configInspection().config),overrides};renderConfig();toast('已恢复本文件自动识别的配置，应用后生效。');}
 };
 if(handlers[action]){try{await handlers[action]();}catch(error){toast(error.message,true);}}
});
document.addEventListener('input',event=>{if(event.target.matches('#export-target-dir,#export-filename')){exportPreview=null;if($('#export-commit'))$('#export-commit').hidden=true;}if(event.target.id==='product-search'){state.search=event.target.value;updateProductTable();}});
document.addEventListener('change',event=>{if(event.target.id==='export-scope'){exportPreview=null;if($('#export-commit'))$('#export-commit').hidden=true;}
 if(event.target.id==='file-input'){closeModal();window.RetailLinkDataset.openFiles([...event.target.files]).catch(error=>toast(error.message,true));event.target.value='';return;}
 if(event.target.id==='status-filter'){state.statusFilter=event.target.value;updateProductTable();return;}
 if(event.target.id==='sort-select'){state.sort=event.target.value;updateProductTable();return;}
 if(event.target.id==='preview-sheet'){state.selectedSheet=event.target.value;state.previewTarget=null;state.previewOffset=0;$('#active-panel').innerHTML=renderSources();fetchPreview();return;}
 if(event.target.hasAttribute('data-mapping-sheet')){captureConfig();renderConfig();}
});
document.addEventListener('keydown',event=>{
 if(state.busy&&event.key==='Escape'){event.preventDefault();return;}
 if(event.key==='Escape'){if($('#modal-root').innerHTML)closeModal();else closeDrawer();}
 if((event.key==='Enter'||event.key===' ')&&event.target.matches('tr[data-product]')){event.preventDefault();openProduct(event.target.dataset.product);}
 if(event.key==='Enter'&&event.target.id==='directory-path')scanDirectory();
 if(event.key==='Tab'){
  const dialog=$('#modal-root [role="dialog"]')||$('#drawer-root [role="dialog"]');if(!dialog)return;
  const focusable=$$('button:not([disabled]),input:not([disabled]),select:not([disabled]),a[href]',dialog).filter(element=>element.offsetParent!==null);if(!focusable.length)return;
  const first=focusable[0],last=focusable.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
 }
});
let dragDepth=0;
document.addEventListener('dragenter',event=>{if(event.dataTransfer?.types.includes('Files')){event.preventDefault();dragDepth++;$('#drag-overlay').hidden=false;}});
document.addEventListener('dragover',event=>{if(event.dataTransfer?.types.includes('Files')){event.preventDefault();event.dataTransfer.dropEffect='copy';}});
document.addEventListener('dragleave',event=>{event.preventDefault();dragDepth--;if(dragDepth<=0){dragDepth=0;$('#drag-overlay').hidden=true;}});
document.addEventListener('drop',event=>{event.preventDefault();dragDepth=0;$('#drag-overlay').hidden=true;if(state.busy)return;const files=[...(event.dataTransfer?.files||[])];const file=files.find(file=>/\.(xlsx|xlsm)$/i.test(file.name));if(file){closeModal();window.RetailLinkDataset.openFiles(files.filter(file=>/\.(xlsx|xlsm)$/i.test(file.name))).catch(error=>toast(error.message,true));}else if(files.length)toast('请拖入 .xlsx 或 .xlsm 工作簿。',true);});

render();
api('/api/health').then(async data=>{
 state.sampleAvailable=data.sampleAvailable!==false;state.defaultDirectory=data.defaultDirectory||'';state.defaultExportDirectory=data.defaultExportDirectory||'';
 const versionLabel=document.querySelector('#app-version');if(versionLabel)versionLabel.textContent='v'+data.version;
 if(data.desktop){const stored=await api('/api/preferences');applyImportedPreferences(stored.preferences);desktopPreferencesReady=true;}
 if(!state.result)render();
}).catch(()=>toast('暂时无法连接本地服务，请重新启动零售通。',true));


window.RetailLinkImport = {
 acceptInspection,
};
