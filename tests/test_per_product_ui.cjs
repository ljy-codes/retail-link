/* Isolated presentation checks: no browser, live data or user preferences are changed. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),dev=fs.existsSync(path.join(root,'开发空间/web/app.js'))?path.join(root,'开发空间'):root,file=path.join(dev,'web/app.js'),source=fs.readFileSync(file,'utf8');
const boundary=source.indexOf("document.addEventListener('click',");assert.ok(boundary>0);
const nodes=new Map();const links=[];let capturedBlob;
function node(){return {innerHTML:'',textContent:'',style:{},hidden:false,append(){},setAttribute(){},scrollIntoView(){},focus(){}};}
const document={body:node(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);},querySelectorAll(){return [];},createElement(tag){const n=node();if(tag==='a'){n.click=()=>links.push({filename:n.download,href:n.href});}return n;}};
const sandbox=vm.createContext({document,window:{addEventListener(){}},localStorage:{getItem(){return null;},setItem(){}},setTimeout(){return 1;},clearTimeout(){},Blob,URL:{createObjectURL(blob){capturedBlob=blob;return 'blob:isolated-test';},revokeObjectURL(){}},console,CustomEvent:class{}});
vm.runInContext(source.slice(0,boundary),sandbox,{filename:file});
const evaluate=code=>vm.runInContext(code,sandbox);
const product=(id,status,gap,extra={})=>({id,name:'虚构商品'+id,code:'CODE-'+id,plan:1000,completed:status==='met'?1600:500,targetAmount:850,achievement:status==='met'?1.6:.5,targetGap:gap,dailyRequired:gap===null?null:gap/10,remainingDays:10,assessmentStatus:status,assessmentReason:status==='unknown'?'缺少完成额':status==='met'?'本商品已达到目标':'本商品尚未达到目标',status:'normal',issues:[],sources:{},...extra});
function load(products=[product('A','met',0),product('B','below',350)],summary={}){
 const s={plan:2000,completed:2100,achievement:1.05,targetRate:.85,targetGap:350,knownTargetGap:350,dailyRequired:35,knownDailyRequired:35,remainingDays:10,metCount:1,belowTargetCount:1,unknownTargetCount:0,assessmentScope:'product',assessmentVersion:1,partialMetrics:[],...summary};
 evaluate(`Object.assign(state,${JSON.stringify({result:{filename:'虚构.xlsx',period:'2026-11',asOf:'2026-11-20',products,summary:s,issues:[],sources:[]},config:{asOf:'2026-11-20',period:'2026-11',planBasis:'orders_amount',targetRate:.85,overrides:{},averageDays:7,amountMode:'calculated'},inspection:{sheets:[]},historySnapshot:null,statusFilter:'all',sort:'target',search:'',tableMode:'business'})})`);
}
let passed=0;
function check(name,fn){fn();passed++;console.log('PASS '+name);}
load();
check('总额105%时仍显示逐款350缺口与35每日任务',()=>{const h=evaluate('renderDashboard()');assert.match(h,/105\.00%/);assert.match(h,/逐款未完成缺口合计/);assert.match(h,/¥ 350\.00/);assert.match(h,/¥ 35\.00/);assert.match(h,/总额达标也可能有单品未达标/);});
check('三类单品状态可独立筛选',()=>{const h=evaluate('renderAssessmentOverview()');for(const key of ['met','unmet','unknown'])assert.ok(h.includes(`data-assessment-filter="${key}"`));});
check('未达标筛选不依赖数据错误',()=>{evaluate("state.statusFilter='unmet'");assert.equal(evaluate('filteredProducts().map(p=>p.id).join()'),'B');});
check('已达标筛选仅保留已达标',()=>{evaluate("state.statusFilter='met'");assert.equal(evaluate('filteredProducts().map(p=>p.id).join()'),'A');});
check('数据提醒筛选与考核状态分开',()=>{evaluate("state.statusFilter='attention'");assert.equal(evaluate('filteredProducts().length'),0);});
check('缺口优先排序在同类未达标商品间稳定生效',()=>{load([product('A','met',0,{status:'error'}),product('B','below',100),product('C','unknown',null),product('D','below',500)]);assert.equal(evaluate('filteredProducts().map(p=>p.id).join()'),'D,B,C,A');});
check('数据提醒排序仍可单独使用',()=>{evaluate("state.sort='attention'");assert.equal(evaluate('filteredProducts()[0].id'),'A');});
check('未知考核不被计算成零任务或已达标',()=>{evaluate("state.statusFilter='unknown'");assert.equal(evaluate('filteredProducts().map(p=>p.id).join()'),'C');assert.match(evaluate('renderProductTable()'),/待核对/);});
check('搜索与考核筛选同时保留',()=>{evaluate("state.statusFilter='unmet';state.search='code-d'");assert.equal(evaluate('filteredProducts().map(p=>p.id).join()'),'D');});
check('空筛选给出可恢复提示',()=>{evaluate("state.search='not-found'");assert.match(evaluate('renderProductTable()'),/没有符合条件的商品/);});
check('部分缺数据时仅标已知缺口，不假装完整',()=>{load([product('A','met',0),product('B','below',350),product('C','unknown',null)],{targetGap:null,dailyRequired:null,unknownTargetCount:1});const h=evaluate('renderAssessmentOverview()');assert.match(h,/待补齐数据/);assert.match(h,/已知商品缺口 ¥ 350\.00/);assert.match(h,/尚不是完整任务/);});
check('全未知时不把已知缺口显示为0',()=>{load([product('C','unknown',null)],{targetGap:null,knownTargetGap:null,dailyRequired:null,knownDailyRequired:null,metCount:0,belowTargetCount:0,unknownTargetCount:1});assert.match(evaluate('renderAssessmentOverview()'),/尚无可计算的商品缺口/);});
check('月末未达标明确显示到期',()=>{load([product('B','below',350,{remainingDays:0,dailyRequired:null})],{remainingDays:0,dailyRequired:null,knownDailyRequired:null,metCount:0});assert.match(evaluate('renderAssessmentOverview()'),/已到期，仍有缺口/);assert.match(evaluate('renderProductTable()'),/已到期，仍有缺口/);});
check('月末已达标每日任务保留真实0',()=>{load([product('A','met',0,{remainingDays:0,dailyRequired:0})],{remainingDays:0,targetGap:0,knownTargetGap:0,dailyRequired:0,knownDailyRequired:0,belowTargetCount:0});const h=evaluate('renderAssessmentOverview()');assert.match(h,/¥ 0\.00/);assert.doesNotMatch(h,/已到期，仍有缺口/);});
check('月末未知不会承诺补数据后还能均摊日期',()=>{load([product('C','unknown',null,{remainingDays:0})],{remainingDays:0,targetGap:null,dailyRequired:null,unknownTargetCount:1});const h=evaluate('renderAssessmentOverview()');assert.match(h,/已到期，考核待核对/);assert.match(h,/本期已无剩余日期，请先核对/);});
check('单品详情包含目标、完成额、缺口和每日任务',()=>{load();evaluate("openProduct('B')");const h=nodes.get('#drawer-root').innerHTML;for(const label of ['目标完成额','累计完成','未完成缺口','剩余每日需完成','未达标'])assert.ok(h.includes(label));assert.ok(h.includes('850.00'));});
check('数量口径使用目标完成量标签并不添加货币符号',()=>{load();evaluate("state.config.planBasis='orders_quantity'");const h=evaluate('renderProductAssessment(state.result.products[1])');assert.match(h,/目标完成量/);assert.doesNotMatch(h,/¥/);});
check('渲染新结果不会改写保存数据',()=>{load();const before=evaluate('JSON.stringify(state.result)');evaluate('renderDashboard();renderProductAssessment(state.result.products[1])');assert.equal(evaluate('JSON.stringify(state.result)'),before);});
check('旧历史不静默重算或套新版逐款合计',()=>{load();evaluate('delete state.result.summary.assessmentScope;delete state.result.summary.assessmentVersion;state.result.summary.targetGap=0;state.result.products.forEach(p=>{delete p.assessmentStatus;delete p.targetAmount;delete p.remainingDays;});');const before=evaluate('JSON.stringify(state.result)'),h=evaluate('renderDashboard()');assert.match(h,/历史考核口径/);assert.match(h,/历史保存差额（原口径）/);assert.doesNotMatch(h,/逐款未完成缺口合计/);assert.match(h,/<option value="unmet"[^>]*disabled/);assert.equal(evaluate('JSON.stringify(state.result)'),before);});
check('旧历史只读时禁用重新分析入口',()=>{evaluate("state.historySnapshot={sourceAvailable:false}");assert.match(evaluate('renderAssessmentOverview()'),/disabled title="请重新导入文件后分析"/);});
check('商品文本保持HTML转义',()=>{load([product('<img src=x>','below',350)]);const h=evaluate('renderProductTable()');assert.ok(h.includes('&lt;img src=x&gt;'));assert.ok(!h.includes('data-product="<img'));});
(async()=>{
 load();evaluate('exportReport()');const csv=await capturedBlob.text();assert.match(csv,/单品考核状态/);assert.match(csv,/目标完成额/);assert.match(csv,/按单品独立考核/);assert.match(csv,/未达标/);passed++;console.log('PASS CSV导出包含保存的单品考核与目标');
 evaluate('delete state.result.summary.assessmentScope;delete state.result.summary.assessmentVersion;exportReport()');const legacy=await capturedBlob.text();assert.match(legacy,/历史保存口径，未重算/);assert.match(legacy,/历史未保存/);passed++;console.log('PASS 旧版CSV明确历史口径');
 console.log(`\n${passed} tests, ${passed} passed`);
})().catch(error=>{console.error(error);process.exitCode=1;});
