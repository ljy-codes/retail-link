/* Synthetic presentation checks: no real ledger or user settings are read. */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const file=path.resolve(__dirname,'../web/app.js'),source=fs.readFileSync(file,'utf8');
const boundary=source.indexOf("document.addEventListener('click',");
assert.ok(boundary>0);
const nodes=new Map();
const node=()=>({innerHTML:'',style:{},append(){},setAttribute(){},scrollIntoView(){},focus(){}});
const document={body:node(),querySelector(selector){if(!nodes.has(selector))nodes.set(selector,node());return nodes.get(selector);},querySelectorAll(){return [];},createElement(){return node();}};
const sandbox=vm.createContext({document,window:{addEventListener(){}},localStorage:{getItem(){return null;}},setTimeout(){},clearTimeout(){},console,Blob,URL:{},CustomEvent:class{}});
vm.runInContext(source.slice(0,boundary),sandbox,{filename:file});
const evaluate=code=>vm.runInContext(code,sandbox);
function load(trend=[],products=[]){evaluate(`Object.assign(state,${JSON.stringify({result:{filename:'虚构验收.xlsx',asOf:'2026-11-26',products,trend,issues:[],sources:[],summary:{assessmentScope:'product',assessmentVersion:1,partialMetrics:[]}},config:{asOf:'2026-11-26',period:'2026-11',planBasis:'orders_amount',overrides:{}},inspection:{sheets:[]},health:null})})`);}
let passed=0;
function check(name,fn){fn();passed++;console.log('PASS '+name);}
check('日销量补全日期轴但保留缺失和真实零',()=>{const input=[{date:'2026-11-21',sales:10},{date:'2026-11-23',sales:0}],before=JSON.stringify(input);const days=JSON.parse(evaluate(`JSON.stringify(workbenchTrendDays(${JSON.stringify(input)}))`));assert.deepEqual(days,[{date:'2026-11-21',sales:10},{date:'2026-11-22',sales:null},{date:'2026-11-23',sales:0}]);assert.equal(JSON.stringify(input),before);});
check('近期图限制14个自然日且不改写旧快照',()=>{load(Array.from({length:26},(_,i)=>({date:`2026-11-${String(i+1).padStart(2,'0')}`,sales:i})));const before=evaluate('JSON.stringify(state.result)'),days=JSON.parse(evaluate('JSON.stringify(workbenchTrendDays(state.result.trend))'));assert.equal(days.length,14);assert.equal(days[0].date,'2026-11-13');assert.equal(days.at(-1).date,'2026-11-26');evaluate('renderWorkbenchTrend()');assert.equal(evaluate('JSON.stringify(state.result)'),before);});
check('缺失日期在折线中真实断开，0销量仍有可读点',()=>{load([{date:'2026-11-21',sales:10},{date:'2026-11-23',sales:0}]);const html=evaluate('renderWorkbenchTrend()');assert.equal((html.match(/<polyline /g)||[]).length,2);assert.match(html,/2026-11-23：0 小单位/);assert.match(html,/已记录 2 \/ 3 天/);assert.doesNotMatch(html,/NaN|Infinity/);});
check('单日和全零数据不会形成无效坐标',()=>{for(const trend of [[{date:'2026-11-26',sales:0}],[{date:'2026-11-25',sales:0},{date:'2026-11-26',sales:0}]]){load(trend);const html=evaluate('renderWorkbenchTrend()');assert.match(html,/wb-trend-dot/);assert.doesNotMatch(html,/NaN|Infinity/);}});
check('无日明细时不把汇总或占位图伪装成销量',()=>{load();const html=evaluate('renderWorkbenchTrend()');assert.match(html,/没有可绘制的日销量/);assert.doesNotMatch(html,/<polyline |<polygon /);});
check('目标环图来自同一快照逐款状态且保留待核对',()=>{load([],[{id:'1',assessmentStatus:'met'},{id:'2',assessmentStatus:'below'},{id:'3',assessmentStatus:'below'},{id:'4',assessmentStatus:'unknown'}]);const html=evaluate('renderWorkbenchDistribution()');assert.match(html,/共4款商品，已达标1款，未达标2款，待核对1款/);assert.match(html,/50\.00%/);assert.equal((html.match(/25\.00%/g)||[]).length,2);for(const filter of ['met','unmet','unknown'])assert.match(html,new RegExp('data-assessment-filter="'+filter+'"'));});
check('旧快照不凭商品状态猜测历史目标分布',()=>{load([],[{id:'1',assessmentStatus:'met'}]);evaluate('delete state.result.summary.assessmentVersion');const html=evaluate('renderWorkbenchDistribution()');assert.match(html,/历史快照未保存逐款考核状态/);assert.doesNotMatch(html,/100\.00%/);assert.match(html,/data-assessment-filter="met" disabled/);});
check('空工作台无虚构业务数值且保留导入及历史入口',()=>{load();const html=evaluate('renderEmpty()');assert.match(html,/data-action="choose-file"/);assert.match(html,/data-action="open-history"/);assert.match(html,/等待经营数据/);assert.match(html,/不填入演示业务数值/);assert.doesNotMatch(html,/<polyline |<polygon /);});
console.log(`\n${passed} tests, ${passed} passed`);
