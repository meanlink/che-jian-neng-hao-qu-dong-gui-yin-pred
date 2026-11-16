import {reviews,datasets} from '../api/entities.js';
import {download} from '../api/client.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {PeriodStatus,BranchStatus} from '../constants/enums.js';

let filter='all',selectedPlan=null;

const FILTERS=[['all','全部'],...Object.entries(PeriodStatus)];
const WEEKDAYS=['周一','周二','周三','周四','周五','周六','周日'];

function planCard(plan,counts){
 const active=plan.id===selectedPlan;
 return '<article class="plan-card '+(active?'selected':'')+'"><button class="plan-pick" data-plan="'+plan.id+'"><strong>'+esc(plan.name)+'</strong><span>'+esc(plan.destination)+'</span></button><div class="plan-meta"><span>应跑 '+plan.weekdays.map(w=>WEEKDAYS[w]).join('、')+' · '+esc(plan.run_time)+'</span><span>下一期 '+esc(plan.next_due||'—')+'</span><span>'+(plan.enabled?'已启用':'已停用')+' · '+plan.lines.length+' 条线体</span></div><div class="plan-actions">'+(session.user.role==='admin'?'<button class="button small" data-generate="'+plan.id+'" '+(plan.enabled?'':'disabled')+'>按日历补建期次 ↻</button><button class="button small" data-plan-toggle="'+plan.id+'" data-enabled="'+(plan.enabled?'0':'1')+'">'+(plan.enabled?'停用':'启用')+'</button>':'')+'</div>'+(plan.holidays.length?'<small class="hint">节假日 '+esc(plan.holidays.join('、'))+'</small>':'')+'</article>';
}

function lineRows(plan){
 return plan.lines.map(line=>'<tr><td><strong>'+esc(line.name)+'</strong><small class="cell-note">'+esc(line.code)+'</small></td><td>'+(line.dataset_id?'<span class="status completed">资料已绑定</span> <small class="cell-note">'+esc((workspace.datasets.find(d=>d.id===line.dataset_id)||{}).name||'')+'</small>':'<span class="status waiting_data">待资料</span>')+'</td><td><small class="cell-note">'+esc(JSON.stringify(line.params))+'</small></td><td>'+(line.enabled?'在用':'已停用')+'</td><td>'+(session.user.role==='admin'?'<button class="text-button" data-line-off="'+plan.id+'" data-line="'+line.id+'">移除</button>':'')+'</td></tr>').join('');
}

function periodCard(period){
 const done=period.lines.filter(l=>l.status==='completed').length;
 const missing=period.lines.filter(l=>l.status==='waiting_data'),failed=period.lines.filter(l=>l.status==='failed');
 const blockers=missing.length?missing.map(l=>esc(l.line_name)).join('、')+' 待资料':(failed.length?failed.map(l=>esc(l.line_name)).join('、')+' 计算失败':'');
 return '<article class="period-card"><div class="period-head"><div><p class="eyebrow">'+esc(period.plan_name)+'</p><h3>'+esc(period.due_date)+' 期次</h3><span class="status '+esc(period.status)+'">'+esc(period.status_label)+'</span>'+(blockers?' <small class="cell-note">'+blockers+'</small>':'')+'</div><div class="period-progress"><strong>'+done+' / '+period.lines.length+'</strong><span>线体已完成归因</span></div></div><p class="period-summary">'+esc(period.briefing?.summary||'尚未生成简报，等待各线归因完成。')+'</p><div class="period-actions"><a class="button small" href="#/review/'+period.id+'">查看各线依据 ↗</a>'+(session.user.role==='admin'&&period.status==='pending_approval'?'<button class="button small primary" data-approve="'+period.id+'">批准整期</button>':'')+(session.user.role==='admin'&&period.status==='approved'?'<button class="button small primary" data-distribute="'+period.id+'">分发简报</button>':'')+'</div></article>';
}

function periodDetail(period){
 if(!period)return '';
 const lines=period.lines.map(line=>{
  const metrics=(line.metrics||[]).map(m=>'<span>'+esc(m.label)+' <strong>'+esc(m.value)+'</strong>'+(m.unit?' '+esc(m.unit):'')+'</span>').join('');
  const dataSelect=workspace.datasets.map(d=>'<option value="'+d.id+'" '+(d.id===line.dataset_id?'selected':'')+'>'+esc(d.name)+'</option>').join('');
  const action=line.status==='waiting_data'&&session.user.role!=='viewer'?'<button class="button small" data-fill="'+line.id+'">补齐资料并计算</button>':(line.status==='failed'&&session.user.role!=='viewer'?'<button class="button small" data-retry="'+line.id+'">单独重试</button>':'');
  const fallback=line.status==='waiting_data'?'<label class="inline-data">选择数据批次<select id="fill-dataset-'+line.id+'">'+dataSelect+'</select></label>':'';
  const note=line.status==='waiting_data'?'该线体尚未绑定样本批次，补齐后才会入队计算。':(line.summary||'');
  return '<article class="branch-card '+esc(line.status)+'"><div class="branch-head"><div><strong>'+esc(line.line_name)+'</strong><small>'+esc(line.line_code)+' · 尝试 '+line.attempts+' 次</small></div><span class="status '+esc(line.status)+'">'+esc(BranchStatus[line.status]||line.status)+'</span></div><p>'+esc(note)+'</p><div class="metrics-line">'+metrics+'</div>'+(line.dataset_name?'<small class="cell-note">数据批次 '+esc(line.dataset_name)+' · 参数 '+esc(JSON.stringify(line.params))+'</small>':'')+(line.findings?.length?'<ol class="questions compact-list">'+line.findings.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ol>':'')+(line.warnings?.length?line.warnings.map(v=>'<p class="warning">'+esc(v)+'</p>').join(''):'')+(line.error?'<p class="warning">计算失败：'+esc(line.error)+'</p>':'')+(line.status!=='completed'?'<div class="branch-actions">'+fallback+action+'</div>':'')+'</article>';
 }).join('');
 return '<section class="panel period-detail"><div class="section-title"><h2>'+esc(period.due_date)+' 期次 · 各线依据</h2><span>'+esc(period.status_label)+'</span></div><div class="period-detail-meta"><span>应跑日 '+esc(period.due_date)+'</span><span>线体 '+period.lines.length+' 条</span><span>数据与参数来自建期快照</span>'+(period.approved_by?'<span>批准人 '+esc(period.approved_by)+'</span>':'')+(period.distributed_at?'<span>分发于 '+date(period.distributed_at)+'</span>':'')+'</div>'+lines+'</section>';
}

function planEditor(plan){
 if(session.user.role!=='admin')return '';
 const holidays=(plan?.holidays||[]).join(',');
 return '<section class="panel"><div class="section-title"><h2>'+(plan?'调整业务日历':'新建评审计划')+'</h2><span>只影响未建期</span></div><form id="plan-form"><label>计划名称<input name="name" required minlength="2" value="'+esc(plan?.name||'周度能耗评审')+'"></label><label>接收组<input name="destination" value="'+esc(plan?.destination||'业务复核组 · 本地收件箱')+'"></label><div class="weekday-grid">'+WEEKDAYS.map((label,i)=>'<label class="checkbox"><input type="checkbox" name="weekday" value="'+i+'" '+(plan?(plan.weekdays.includes(i)?' checked':''):(i<5?' checked':''))+'> '+label+'</label>').join('')+'</div><div class="parameter-grid"><label>应跑时刻<input name="run_time" value="'+esc(plan?.run_time||'08:00')+'" required></label><label>起始日期<input name="start_date" type="date" value="'+esc(plan?.start_date||'2026-11-02')+'" required '+(plan?'disabled':'')+'></label></div><label>节假日（逗号分隔的 YYYY-MM-DD）<input name="holidays" value="'+esc(holidays)+'" placeholder="2026-12-25,2027-01-01"></label><button type="submit" class="primary">'+(plan?'保存日历':'创建评审计划')+'</button><p class="hint">已有期次不随日历或线体变更改写；停用后不再建新期，已建期可继续收尾。</p></form></section>';
}

function lineEditor(plan){
 if(session.user.role!=='admin'||!plan)return '';
 return '<footer class="line-editor"><form id="line-form"><label>线体编码<input name="code" required placeholder="P04"></label><label>线体名称<input name="name" required placeholder="冲压四线"></label><label>数据批次<select name="dataset_id"><option value="">待资料（先不绑定）</option>'+workspace.datasets.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></label><label>最多解释变量<input name="max_features" type="number" min="2" max="4" value="4"></label><label>情景负荷变化 %<input name="scenario_pct" type="number" min="-30" max="30" value="10"></label><button type="submit" class="button">保存线体</button></form></footer>';
}

export async function reviewPage(id){
 const [plans,allPeriods,deliveries,allDatasets]=await Promise.all([reviews.list(),reviews.periods(),reviews.deliveries(),datasets.list()]);
 workspace.reviewPlans=plans;workspace.reviewPeriods=allPeriods;workspace.datasets=allDatasets;workspace.reviewDeliveries=deliveries;
 if(selectedPlan===null||!plans.some(p=>p.id===selectedPlan))selectedPlan=plans[0]?.id??null;
 const plan=plans.find(p=>p.id===selectedPlan);
 const planPeriods=allPeriods.filter(p=>!plan||p.plan_id===plan.id);
 const counts={all:planPeriods.length};
 Object.keys(PeriodStatus).forEach(key=>counts[key]=planPeriods.filter(p=>p.status===key).length);
 const periods=planPeriods.filter(p=>filter==='all'||p.status===filter);
 const m=workspace.meta;
 const filtered=id?allPeriods.find(p=>p.id===Number(id)):null;
 return heading('05 / REVIEW CYCLE',m.review_title||'周期评审','按业务日历与线体清单自动建期，逐线归因后汇成整期简报；资料齐全才可批准与分发。','<button class="button" id="refresh-review">刷新期次 ↻</button>')+'<div class="review-grid"><section><div class="section-title"><h2>评审计划</h2><span>'+plans.filter(p=>p.enabled).length+' 个启用</span></div><div class="plan-list">'+plans.map(p=>planCard(p,counts)).join('')+(!plans.length?'<p class="empty">尚无评审计划。由负责人按业务日历创建第一个计划。</p>':'')+'</div>'+planEditor(plan)+'</section><section><div class="section-title"><h2>期次台账</h2><span>'+allPeriods.length+' 期</span></div><div class="filter-row">'+FILTERS.map(([key,label])=>'<button class="filter-chip '+(filter===key?'active':'')+'" data-filter="'+key+'">'+esc(label)+' <span>'+counts[key]+'</span></button>').join('')+'</div><div id="review-period-list">'+periods.map(periodCard).join('')+(!periods.length?'<p class="empty">该状态下暂无期次。</p>':'')+'</div>'+(plan?'<div class="section-title"><h2>线体清单</h2><span>变更只影响未建期</span></div><div class="table-wrap"><table class="line-table"><thead><tr><th>线体</th><th>样本批次</th><th>固定参数</th><th>状态</th><th></th></tr></thead><tbody>'+lineRows(plan)+'</tbody></table></div>'+lineEditor(plan):'')+'</section></div>'+(filtered?periodDetail(filtered):'<p class="hint">点击“查看各线依据”展开期次快照与每条线体的归因依据。</p>')+'<section class="panel"><div class="section-title"><h2>简报归档</h2><span>'+deliveries.length+' 份</span></div>'+deliveries.map(d=>'<div class="delivery"><div><strong>'+esc(d.filename)+'</strong><p>期次 '+d.period_id+' · '+esc(d.destination)+' · '+date(d.created_at)+'</p><small>SHA256 '+esc(d.checksum.slice(0,24))+'…</small></div><div><button class="text-button" data-review-download="'+d.id+'" data-format="csv">CSV ↓</button><button class="text-button" data-review-download="'+d.id+'" data-format="json">JSON ↓</button></div></div>').join('')+(!deliveries.length?'<p class="empty">整期批准后由后台生成简报归档。</p>':'')+'</section>';
}

export function bindReview(refresh){
 document.querySelector('#refresh-review').onclick=refresh;
 document.querySelectorAll('[data-plan]').forEach(b=>b.onclick=()=>{if(Number(b.dataset.plan)===selectedPlan)return;selectedPlan=Number(b.dataset.plan);location.hash='#/review';refresh();});
 document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.filter;refresh();});
 document.querySelectorAll('[data-generate]').forEach(b=>b.onclick=async()=>{try{const r=await reviews.generate(Number(b.dataset.generate));notify('按日历补建 '+r.created.length+' 期，入队 '+r.queued.length+' 条线体');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-plan-toggle]').forEach(b=>b.onclick=async()=>{try{await reviews.toggle(Number(b.dataset.planToggle),b.dataset.enabled==='1');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=async()=>{try{await reviews.approve(Number(b.dataset.approve));notify('整期已批准，后台将生成简报归档');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-distribute]').forEach(b=>b.onclick=async()=>{try{await reviews.distribute(Number(b.dataset.distribute));notify('简报已分发到本地收件箱');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-retry]').forEach(b=>b.onclick=async()=>{try{await reviews.retry(Number(b.dataset.retry));notify('该线体已重新入队');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-fill]').forEach(b=>b.onclick=async()=>{try{const select=document.querySelector('#fill-dataset-'+b.dataset.fill);if(!select?.value)throw new Error('请选择数据批次');await reviews.fillData(Number(b.dataset.fill),Number(select.value));notify('资料已补齐，该线体已入队');await refresh();}catch(e){notify(e.message,true);}});
 document.querySelectorAll('[data-review-download]').forEach(b=>b.onclick=()=>download(Number(b.dataset.reviewDownload),b.dataset.format,'/review/deliveries/').catch(e=>notify(e.message,true)));
 const planForm=document.querySelector('#plan-form');
 if(planForm)submit(planForm,async f=>{
  const weekdays=[...planForm.querySelectorAll('[name=weekday]:checked')].map(x=>Number(x.value));
  const holidays=f.get('holidays').split(',').map(v=>v.trim()).filter(Boolean);
  const plan=workspace.reviewPlans.find(p=>p.id===selectedPlan);
  if(!weekdays.length)throw new Error('至少选择一个应跑日');
  if(plan)await reviews.update(plan.id,{destination:f.get('destination'),weekdays,run_time:f.get('run_time'),holidays});
  else await reviews.create({name:f.get('name'),destination:f.get('destination'),weekdays,run_time:f.get('run_time'),start_date:f.get('start_date'),holidays});
  notify(plan?'业务日历已保存，仅影响未建期':'评审计划已创建');await refresh();
 });
 const lineForm=document.querySelector('#line-form');
 if(lineForm)submit(lineForm,async f=>{await reviews.saveLine(selectedPlan,{code:f.get('code'),name:f.get('name'),dataset_id:f.get('dataset_id')?Number(f.get('dataset_id')):null,params:{max_features:Number(f.get('max_features')),scenario_pct:Number(f.get('scenario_pct'))},enabled:true});notify('线体已保存，仅影响未建期');await refresh();});
 document.querySelectorAll('[data-line-off]').forEach(b=>b.onclick=async()=>{try{await reviews.disableLine(Number(b.dataset.lineOff),Number(b.dataset.line));await refresh();}catch(e){notify(e.message,true);}});
}
