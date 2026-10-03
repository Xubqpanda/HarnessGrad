const $=s=>document.querySelector(s);
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=v=>(v==null?'—':Number(v).toFixed(3));
const j=async u=>(await fetch(u)).json();
let OPTS=null,POLL=null,CUR=null,RUN=null,SEL=0,SETTINGS=null,PROVIDERS=null;
let RUNS_CACHE=null;
// 当前这次运行跑的是哪一侧。train 与 eval 各有一个开始按钮,运行结束后要知道该把
// 哪一个按钮放回可用状态 —— 否则一个面板跑完,另一个面板的按钮会一直灰着。
let RUN_SIDE='train';

const PAGES = {exp:['跑实验','配置与启动'], run:['运行详情',''],
               task:['任务详情',''],
               hist:['实验','每一次测量的记录'],
               graph:['版本图','每个圆是一轮;虚线是 eval 测的那个 train 轮次'],
               set:['设置','模型与服务']};
function view(id,btn){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('on'));
  document.querySelectorAll('#nav button').forEach(b=>b.classList.remove('on'));
  $('#v-'+id).classList.add('on');
  // 拿不到按钮就只是不点亮它,不能让导航本身失败:调用方常见的写法是
  // `view('exp', ...); showRun(id)`,这里抛异常会把 showRun 一起吃掉。
  if(btn) btn.classList.add('on');
  $('#nav button[data-v="'+id+'"]')?.classList.add('on');
  const [t,sub] = PAGES[id]||['',''];
  $('#crumb-leaf').textContent = t; $('#page-sub').textContent = sub;
  // 换视图要回到顶部。不是小事:长页面来回切换时,滚动位置会留在原处,于是
  // "点一道题进去"看到的是**上一屏的下半部分** —— 实测截图里任务页的标题被顶到
  // 视口上方 908px,看起来像那一页没有标题。
  ['#wrap','.main','#v-'+id].forEach(sel=>{
    const el=document.querySelector(sel); if(el) el.scrollTop=0;
  });
  window.scrollTo(0,0);
  if(id==='run') drawChart(RUN&&RUN.points||[]);
  if(id==='task') drawChart(window.TASK_PTS||[], '#task-chart');
  if(id==='hist') loadHist();
  if(id==='graph') loadGraph();
  if(id==='set') loadSettings();
}
function toggleSide(){
  const f = $('#frame'); f.classList.toggle('collapsed');
  $('.collapse').textContent = f.classList.contains('collapsed') ? '» 展开' : '« 收起侧栏';
}
// 侧栏底部的两个状态点:一眼看出模型服务在不在、有多少实验记录
async function sideStatus(){
  try{
    const s = await j('/api/model-service?action=status&port=8001');
    const d = $('#s-model');
    d.className = 'dot ' + (s.ready?'ok':'off');
    d.nextSibling.textContent = s.ready ? '模型服务 就绪' : '模型服务 未启动';
  }catch(e){}
  try{
    const r = await j('/api/runs');
    const d = $('#s-runs'); d.className = 'dot ok';
    d.nextSibling.textContent = `实验记录 ${r.length} 个`;
  }catch(e){}
}

// ---------- 把某一轮存进 base_harness ----------
//
// 用户要的是:跑完一段进化之后,把某一轮的 harness 固定成一个新的起点,后面的实验
// 从它继续。所以这里写的是 `base_harness/<名字>` —— 新运行启动时暂存并提交的那个
// 目录 —— 而不是记录里的一个指针。
//
// 失败必须说出来。`promote` 会拒绝重名(除非确认覆盖)、拒绝没有留下状态的轮次,
// 把它的 error 直接显示在按钮旁边,不吞掉。
async function promote(round){
  const el=$('#prom-status'); if(!el) return;
  if(!RUN){ el.innerHTML='<span class="bad">先选一次运行</span>'; return; }
  // 默认名字带上来源,便于以后在 base_harness/ 里一眼看出它是从哪来的
  const as=window.prompt(
    `把 ${RUN.run_id} 第 ${round} 轮的 harness 存成 base_harness/ 下的哪个名字?`,
    `${RUN.run_id}-r${round}`);
  if(!as) return;
  el.textContent='正在复制…';
  const q=new URLSearchParams({run_id:RUN.run_id,round:String(round),as:as});
  let r; try{ r=await j('/api/promote?'+q); }
  catch(e){ el.innerHTML='<span class="bad">✗ '+esc(String(e))+'</span>'; return; }
  if(r.error){
    // 重名是可恢复的:问一次是否覆盖,而不是让用户自己去找那个开关
    if(r.exists && window.confirm(r.error+'\n\n覆盖它吗?')){
      q.set('overwrite','1');
      el.textContent='正在覆盖…';
      try{ r=await j('/api/promote?'+q); }
      catch(e){ el.innerHTML='<span class="bad">✗ '+esc(String(e))+'</span>'; return; }
    }
  }
  el.innerHTML = r.error
    ? '<span class="bad">✗ '+esc(r.error)+'</span>'
    : '<span class="ok">✓ 已存为 base_harness/'+esc(as)+'</span>'
      + ' <span class="dim">新实验从它开始</span>';
  if(!r.error) loadHarnessNames();
}

// 存完之后「基础 HARNESS」下拉要能看到新名字,否则用户以为没成功
async function loadHarnessNames(){
  try{ OPTS=await j('/api/options'); }catch(e){ return; }
  const was=$('#f-harness').value;
  renderHarnessOptions();
  renderSplit();
  if([...$('#f-harness').options].some(o=>o.value===was)) $('#f-harness').value=was;
}

// ---------- 版本图 ----------
//
// 每条泳道是 (harness · base model)。**不同的 base model 是不同的仪器**,所以不画在
// 一起 —— 和 plot_curve 拒绝把两条曲线混在一张图上是同一条规则。
//
// 横向是轮次,纵向是一次运行。只画记录里真实存在的两种边:同一运行里 round N→N+1,
// 以及 eval 点指向它测的那个 train 轮次(evaluated_from)。没有任何一条边是从
// "这两个运行名字一样"推出来的 —— 那种边看起来像证据,其实是猜的。
const GW=104, GH=66, GPADL=186, GPADT=30, GR=7;
async function loadGraph(){
  const box=$('#graph'); if(!box) return;
  let g; try{ g=await j('/api/graph'); }
  catch(e){ box.innerHTML='<div class="bad">读不到版本图:'+esc(String(e))+'</div>'; return; }
  const lanes=g.lanes||[];
  if(!lanes.length){
    box.innerHTML='<div class="hint">还没有任何一轮记录 —— 先跑一次训练。</div>'; return; }
  box.innerHTML=lanes.map((lane,i)=>{
    const runs=lane.runs||[];
    const rounds=runs.flatMap(r=>r.nodes.map(n=>n.round||0));
    const maxR=Math.max(0,...rounds);
    const edges=runs.flatMap(r=>r.edges||[]);
    // 位置表按泳道建。指向别的泳道的边画不出来 —— 那种边单独说明,不画成一条
    // 跨泳道的线,因为跨泳道本身就意味着换了仪器。
    const pos={};
    runs.forEach((r,ri)=>r.nodes.forEach(n=>{ pos[n.id]={x:GPADL+(n.round||0)*GW, y:GPADT+ri*GH, n:n}; }));
    const drawn=[], outside=[];
    edges.forEach(e=>{ (pos[e.from]&&pos[e.to]) ? drawn.push(e) : outside.push(e); });
    return `<div class="g-lane">
      <div class="g-lane-head"><b>${esc(lane.lane)}</b>
        <span class="dim">${runs.length} 次运行 · 第 0–${maxR} 轮</span></div>
      <div class="chartwrap">${laneSVG(runs,pos,drawn,maxR)}</div>
      ${outside.length?`<div class="hint">${outside.length} 条测量边指向别的泳道 ——
        换了 base model 就不是同一台仪器,不跨泳道连线。</div>`:''}
    </div>`;
  }).join('');
}
function laneSVG(runs,pos,edges,maxR){
  const W=GPADL+(maxR+1)*GW, H=GPADT+runs.length*GH;
  const cx=r=>GPADL+r*GW, cy=i=>GPADT+i*GH;
  let e='';
  edges.forEach((ed,k)=>{
    const a=pos[ed.from], b=pos[ed.to];
    e += ed.kind==='measured'
      ? `<path d="M${a.x+GR} ${a.y} C${a.x+52} ${a.y} ${b.x-52} ${b.y} ${b.x-GR} ${b.y}"
           stroke="#12b76a" stroke-width="1.8" stroke-dasharray="5 4" fill="none" opacity=".85"/>`
      : `<path d="M${a.x+GR} ${a.y} H${b.x-GR}" stroke="#9aa4b2" stroke-width="1.8" fill="none"/>`;
  });
  let n='';
  runs.forEach((r,i)=>{
    n += `<text x="${GPADL-GR-14}" y="${cy(i)-3}" text-anchor="end" font-size="12.5"
            font-weight="600" fill="#101828">${esc(r.run_id)}</text>`
      +  `<text x="${GPADL-GR-14}" y="${cy(i)+14}" text-anchor="end" font-size="11"
            fill="#98a2b3">${esc(r.side)}${r.model?' · '+esc(r.model):''}</text>`;
    r.nodes.forEach(nd=>{
      const c=nd.kind==='exam'?'#12b76a':'#4f46e5';
      n += `<circle cx="${cx(nd.round||0)}" cy="${cy(i)}" r="${GR}" fill="${c}"
              stroke="#fff" stroke-width="2"/>`
        +  `<text x="${cx(nd.round||0)}" y="${cy(i)+GR+15}" text-anchor="middle"
              font-size="11" fill="#667085" stroke="#fff" stroke-width="3"
              paint-order="stroke">${nd.score==null?'—':Number(nd.score).toFixed(3)}</text>`;
    });
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
    style="min-width:${W}px">${e}${n}</svg>`;
}

// ---------- 配置 ----------
async function initForm(){
  OPTS=await j('/api/options');
  $('#f-dataset').innerHTML=OPTS.datasets.map(x=>
    `<option value="${esc(x.name)}">${esc(x.name)}(${x.count}题)</option>`).join('');
  $('#f-dataset').value='probe_set';
  // 「改进方法」里只放**已发表的 RSI 方法**。
  //
  // 以前这里是把 methods/ 下的目录名平铺,于是 `echo_base`(平台的契约自检)和
  // `llm_improver`(所有方法共用的编辑器)和真正的方法混在一列,看不出哪个能被拿来
  // 比较。基线单独放在组外:它是包络面的地板,不是一个方法。
  const published=OPTS.methods.filter(m=>m.kind==='published');
  $('#f-method').innerHTML=
    `<optgroup label="基线">`
    + `<option value="noop">不改进 —— 只测地板</option></optgroup>`
    + (published.length
        ? `<optgroup label="已发表 RSI 方法">`+published.map(m=>
            `<option value="${esc(m.name)}">${esc(m.title)}</option>`).join('')
          + `</optgroup>`
        : `<optgroup label="已发表 RSI 方法">`
          + `<option disabled>methods/ 下没有 kind=published 的方法</option>`
          + `</optgroup>`);
  $('#f-method').value = (OPTS.methods||[]).some(m=>m.name==='codex') ? 'codex' : 'noop';
  // 改进器下拉:名字 + **本机解析到的版本**。解析失败的那一条不隐藏 —— 它是"这台机器
  // 上它现在不能跑"这个事实,藏起来只会让人以为配置没生效。
  const imps=OPTS.improvers||{};
  $('#f-improver').innerHTML=Object.entries(imps).map(([k,v])=>{
    // 选项里只放名字:336px 的窄面板容不下"名字 + 版本 +(默认)",会被截成"codex 0.160.0(默"。
    // 版本和可用性由下面那行提示负责,那里有整行宽度。
    const label=v.name + (v.default?'(默认)':'') + (v.ok?'':' ⚠');
    return `<option value="${esc(k)}"${v.default?' selected':''}>${esc(label)}</option>`;
  }).join('') || '<option value="">(没有配置改进器)</option>';
  $('#f-model').innerHTML=Object.keys(OPTS.models).map(k=>`<option>${esc(k)}</option>`).join('');
  renderFilters();
  renderHarnessOptions();
  $('#f-hcompat').onchange=renderHarnessOptions;
  $('#f-dataset').onchange=()=>{renderHarnessOptions();loadTasks();};
  ['f-harness','f-model','f-method','f-improver','f-imodel'].forEach(id=>{
    const el=$('#'+id); if(el) el.onchange=()=>{cfgHint();renderSplit();};
  });
  $('#f-fromrun').onchange=cfgHint;
  await loadTasks();
  await fillTrainRuns();
  await loadPrepare();
  renderSplit();
}

// 筛选做成**按钮组**,不是下拉。
//
// 下拉把它变成"一个字段",于是它和 harness、任务集、模型并列,而它其实不是配置,
// 是**视图**。三个下拉堆在一起还把栅格撑成三行,和旁边一行的字段对不齐。
// 按钮组一眼看出有哪几个值、当前选的是哪个、还有几个可选。
const FH={role:'',env:''};
function renderFilters(){
  const mk=(group,key,opts)=>{
    $('#'+group).innerHTML=opts.map(([v,label])=>
      `<button type="button" class="seg${FH[key]===v?' on':''}" `
      +`onclick="setFilter('${key}','${v}')">${esc(label)}</button>`).join('');
  };
  mk('g-hrole','role',[['','全部'],['harness','被测量'],['control','对照']]);
  mk('g-henv','env',[['','全部环境'],['files','files'],['exec','exec']]);
}
function setFilter(key,value){ FH[key]=value; renderFilters(); renderHarnessOptions(); }

function renderHarnessOptions(){
  const compatOnly=$('#f-hcompat').checked;
  const ds=OPTS.datasets.find(x=>x.name===$('#f-dataset').value)||{};
  const need=ds.env_kinds||[];
  const keep=x=>
    (!FH.role || (x.role||'harness')===FH.role)
    && (!FH.env || (x.env_kinds||[]).includes(FH.env))
    && (!compatOnly || need.every(k=>(x.env_kinds||[]).includes(k)));
  const shown=OPTS.harnesses.filter(keep);
  const was=$('#f-harness').value;

  const group=role=>shown.filter(x=>(x.role||'harness')===role).map(x=>
    `<option value="${esc(x.dir)}">${esc(x.name)} ${esc(x.version||'')}</option>`).join('');
  let html='';
  const real=group('harness');
  if(real) html+=`<optgroup label="被测量">${real}</optgroup>`;
  const ctrl=group('control');
  if(ctrl) html+=`<optgroup label="对照(不是 harness,不要引用它的曲线)">${ctrl}</optgroup>`;
  $('#f-harness').innerHTML = html || `<option value="">(没有符合筛选的 harness)</option>`;
  if(shown.some(x=>x.dir===was)) $('#f-harness').value=was;
  $('#f-count').textContent=`${shown.length}/${OPTS.harnesses.length}`;
  cfgHint();
}

let TASKS={tasks:[]};
async function loadTasks(){
  // 选一整个数据集,再从中挑具体任务。harness 的进化非常慢,**一个一个看**比成批看
  // 有价值,而成批是 offline 的事(CLI 直接跑整个数据集)。所以默认全选可跑的。
  const ds=$('#f-dataset').value;
  try{ TASKS=await j('/api/tasks?dataset='+encodeURIComponent(ds)); }
  catch(e){ TASKS={tasks:[],error:String(e)}; }
  renderTasks();
  cfgHint();
}

// 两个面板各自一份清单,并排。以前是"跑哪一侧"下拉 + 一份随侧别切换的清单 ——
// 那让 train 和 eval 看起来是一次运行的两个选项,而它们是两次运行。
const PICKED={train:new Set(),eval:new Set()};
function renderTasks(){
  ['train','eval'].forEach(side=>{
    const list=(TASKS.tasks||[]).filter(t=>t.side===side);
    const bad=list.filter(t=>t.available===false).length;
    const host=$('#'+side+'-list');
    if(!host) return;
    // 首次渲染或换了数据集:默认勾上**可跑的**。不可跑的默认不勾并说明原因 ——
    // 让用户勾一个点下去必然被拒的东西,只是把"发现问题"推到点击之后。
    if(!PICKED.side0 || PICKED.side0!==$('#f-dataset').value){
      PICKED[side]=new Set(list.filter(t=>t.available!==false).map(t=>t.task_id));
    }
    host.innerHTML=list.map(t=>{
      const ok=t.available!==false;
      const on=ok&&PICKED[side].has(t.task_id);
      return `<label class="tk-row${ok?'':' off'}" title="${esc(t.goal)}">`
        +`<input type="checkbox" class="tk" data-side="${side}" value="${esc(t.task_id)}" `
        +`${on?'checked':''} ${ok?'':'disabled'} onchange="tick('${side}','${esc(t.task_id)}',this.checked)"> `
        +`<code>${esc(t.task_id)}</code> `
        +`<span class="dim">${esc(t.goal.slice(0,52))}…</span>`
        +(ok?'':`<span class="why">${esc(t.why||'不可运行')}</span>`)+`</label>`;
    }).join('') || `<span class="dim">(本侧没有任务)</span>`;
    const total=list.length;
    $('#'+side+'-count').innerHTML= total
      ? `${PICKED[side].size}/${total} 已选` + (bad?` · <span style="color:var(--warn-fg)">${bad} 题不可跑</span>`:'')
      : '';
  });
  PICKED.side0=$('#f-dataset').value;
  cfgHint();
}
function tick(side,tid,on){ on?PICKED[side].add(tid):PICKED[side].delete(tid); renderTasks(); }
function pickTasks(side,what){
  const list=(TASKS.tasks||[]).filter(t=>t.side===side && t.available!==false);
  PICKED[side] = what==='none' ? new Set() : new Set(list.map(t=>t.task_id));
  renderTasks();
}
function chosenTasks(side){ return [...PICKED[side]]; }

// 「这次运行由什么组成」:被改的 harness / 改它的改进器 / 决定改什么的方法。
//
// 数据来自两处,而且正是它们各自的事实来源:harness 与 method 来自 /api/options(磁盘),
// 改进器的**解析结果**也来自那里,但它是这台机器上的实况(可能有多个 codex,坏的那个
// 就在 PATH 上)。所以这里既显示"选了什么",也显示"实际会跑起来的是什么" —— 一个
// `latest` 请求解析成 0.156.1,和解析失败,长得必须不一样。
function renderSplit(){
  const box=$('#split-flow'); if(!box) return;
  // select 的 value 是**目录名**(`dir`),不是展示用的 `name` —— 拿 name 去比会匹配不到,
  // 于是卡片上"被改进的 harness"一直是"—"。它只在截图里看得出来。
  const h=(OPTS.harnesses||[]).find(x=>x.dir===($('#f-harness').value));
  const m=(OPTS.methods||[]).find(x=>x.name===($('#f-method').value));
  const pick=$('#f-improver');
  const wanted=(pick&&pick.value)||(m&&m.improver)||'';
  const imp=(OPTS.improvers||{})[wanted] || null;
  const override=($('#f-imodel')&&$('#f-imodel').value||'').trim();
  const model=$('#f-model').value || '—';
  const node=(k,v,sub,cls)=>
    `<div class="node ${cls||''}"><div class="k">${k}</div><div class="v">${esc(v)}</div>`
    + (sub?`<div class="s">${esc(sub)}</div>`:'') + `</div>`;
  const parts=[];
  parts.push(node('被改进的 harness', h?`${h.name} ${h.version}`:'—',
    h?`${(h.env_kinds||[]).join('/')} · ${h.lines??'?'} 行 · ${h.role}`:''));
  parts.push('<div class="arrow">←</div>');
  parts.push(node('改进器 improver', imp?(imp.name+(imp.version?` ${imp.version}`:'')):'—',
    imp
      ? (imp.ok? `${override||imp.model||'—'} · ${imp.version_requested==='latest'?'latest':(imp.version_requested||'默认')}`
               : ('本机不可用: '+(imp.reason||'')))
      : '没有可用的改进器',
    imp && !imp.ok ? 'bad' : ''));
  parts.push('<div class="arrow">←</div>');
  parts.push(node('方法 method', m?m.name:'—',
    m ? ((m.kind||'') + (m.skill?' · 自带 skill':' · 用平台默认 skill')) : ''));
  box.innerHTML=parts.join('');

  const id=wanted||'codex';
  const hint=$('#split-hint');
  const ih=$('#imp-hint');
  if(ih) ih.innerHTML = imp
    ? (imp.ok
        ? `<span class="ok">●</span> ${esc(imp.name)} ${esc(imp.version||'')} 已在本机验证`
          + (imp.model?` · 默认模型 <code>${esc(imp.model)}</code>`:'')
          + (override?` · 本次覆盖为 <code>${esc(override)}</code>`:'')
        : `<span class="bad">●</span> ${esc(imp.name)}:${esc(imp.reason||'本机不可用')}`
          + ' —— 方法自带的改进器仍可运行,但这个选择会失败')
    : '还没配置任何改进器:见 <code>improvers/improvers.json</code>';
  const bits=[];
  if(m && m.improver_note) bits.push(esc(m.improver_note));
  if(m && !m.skill) bits.push('这个方法没有自带 skill,用的是 <code>improvers/skill.md</code>(平台默认)。');
  if(m && m.skill) bits.push('自带 skill:<code>'+esc(m.skill.split('/').slice(-2).join('/'))+'</code>。');
  if(imp && !imp.ok) bits.push('<span class="bad">这台机器上解析不到改进器 —— 方法自带的改进器还能跑,但这个组合会失败。</span>');
  if(imp && imp.modified) bits.push('<span class="bad">这个改进器被声明为"改造过" —— 用它跑出来的两条曲线不可直接比较。</span>');
  hint.innerHTML = bits.join(' ');
  const st=$('#split-state');
  if(st) st.innerHTML = imp
    ? (imp.ok ? `<span class="ok">●</span> ${esc(imp.name)} ${esc(imp.version||'')}`
              : `<span class="bad">●</span> 改进器不可用`)
    : '';
}

// ---------- 准备环境 ----------
//
// 只读的部分(缺什么)和可执行的部分(补上)分开:面板先报,按钮才动手。两件事的性质
// 差得远 —— 拉镜像可能几十分钟并可能中途失败,建 overlay 十二秒 —— 所以它们是两个
// 按钮,不是一个"准备好了"。
async function loadPrepare(){
  const box=$('#prep'); if(!box) return;
  const ds=$('#f-dataset').value, h=$('#f-harness').value;
  if(!ds){ box.style.display='none'; return; }
  box.style.display='block';
  let r;
  try{ r=await j('/api/prepare?dataset='+encodeURIComponent(ds)+
                 '&harness='+encodeURIComponent(h)); }
  catch(e){ $('#prep-body').innerHTML='<span class="dim">读不到环境状态:'+esc(String(e))+'</span>'; return; }
  if(r.error){ $('#prep-body').innerHTML='<span class="dim">'+esc(r.error)+'</span>'; return; }

  const im=r.images, dep=r.dependencies;
  const okImg = im.missing.length===0;
  // 一个没有容器任务的数据集(files 侧)不是"全都齐了",是"不需要" —— 说成前者会让
  // 读者以为 89 个镜像已经在本地了。
  const imgLine = im.total===0
    ? '<span class="dim">这个数据集不需要容器环境(全部在宿主上跑)</span>'
    : (okImg ? '<span class="ok">全部 '+im.total+' 个都在本地</span>'
             : '<b>'+im.local+'/'+im.total+'</b> 在本地,<span class="bad">缺 '+im.missing.length+'</span>'
               + '<span class="dim"> · 整套约 104 GB,最大的一个 21.6 GB</span>');
  const depLine = dep.declared
    ? `声明了 <code>${esc(dep.declared)}</code>,已配 overlay:${
        dep.configured.length? '<code>'+dep.configured.map(esc).join('</code> <code>')+'</code>'
                             : '<span class="bad">无</span>'}`
    : `<span class="dim">没有声明 <code>install</code>,不需要配置</span>`;
  const miss = im.missing.length
    ? `<details><summary class="dim">缺的是哪些</summary>${
        im.missing.map(m=>`<div class="tk-row off"><code>${esc(m.task_id)}</code>`+
          `<span class="dim">${esc(m.image)}</span></div>`).join('')}</details>`
    : '';
  $('#prep-body').innerHTML=
    `<div class="tk-row" style="cursor:default">
       <span style="flex:0 0 130px">任务镜像</span>
       <span>${imgLine}</span>
     </div>
     <div class="tk-row" style="cursor:default">
       <span style="flex:0 0 130px">harness 依赖</span>
       <span>${depLine}</span>
     </div>
     ${miss}
     <div class="row" style="margin-top:12px">
       <button class="go" id="prep-dep" ${dep.declared?'':'disabled'}
               onclick="runPrepare('overlay')">配置 harness 依赖</button>
       <button class="gh" id="prep-img" ${okImg?'disabled':''}
               onclick="runPrepare('images')">拉取缺失的 ${im.missing.length} 个镜像</button>
       <span class="hint">§2.5.5:平台不在运行中途拉取任何东西,所以这一步由你触发</span>
     </div>`;
  $('#prep-state').textContent = ((im.total===0 || okImg) &&
                                   (!dep.declared || dep.configured.length))
    ? '就绪' : '有缺项';
}

async function runPrepare(what){
  const cfg={what:what,dataset:$('#f-dataset').value,harness:$('#f-harness').value};
  ['prep-dep','prep-img'].forEach(id=>{const b=$('#'+id); if(b) b.disabled=true;});
  const log=$('#prep-log'); log.style.display='block'; log.textContent='启动中…';
  const r=await j('/api/prepare/run?cfg='+encodeURIComponent(JSON.stringify(cfg)));
  if(r.error){ log.innerHTML='<span class="bad">✗ '+esc(r.error)+'</span>';
    $('#prep-state').textContent='失败'; return; }
  const poll=async()=>{
    let s; try{ s=await j('/api/prepare/job/'+r.started); }catch(e){ return; }
    log.textContent=s.log||'';
    log.scrollTop=log.scrollHeight;
    if(s.running){ $('#prep-state').textContent='进行中…'; setTimeout(poll,1500); }
    else { $('#prep-state').textContent='已结束 —— 重新检查中'; await loadPrepare(); }
  };
  poll();
}

function cfgHint(){
  const h=OPTS.harnesses.find(x=>x.dir===$('#f-harness').value);
  const d=OPTS.datasets.find(x=>x.name===$('#f-dataset').value);
  const need=(d&&d.env_kinds)||[], have=(h&&h.env_kinds)||[];
  const missing=need.filter(k=>!have.includes(k));
  const blocked=!!(h&&d&&missing.length);
  // 能力不匹配是两个面板共同的阻塞:换成 eval 也一样跑不了
  ['btn-train','btn-eval'].forEach(id=>{ const b=$('#'+id); if(b) b.disabled=blocked; });
  const nTrain=PICKED.train.size, nEval=PICKED.eval.size;
  let extra='';
  if(!blocked && d){
    if(!nTrain) extra='　· <span class="dim">训练面板没选任务</span>';
    else if(!nEval) extra='　· <span class="dim">考试面板没选任务(训练仍可单独跑)</span>';
  }
  $('#cfg-hint').innerHTML=
    (blocked?`<span class="bad"><b>跑不了</b> · 任务集 <code>${esc(d.name)}</code> 需要 `
       +`<code>${esc(missing.join('/'))}</code>,而 harness <code>${esc(h.name)}</code> `
       +`只声明了 <code>${esc(have.join('/')||'files')}</code>。</span>`:'')
    + (h?`harness <code>${esc(h.name)} ${esc(h.version)}</code>(${esc(have.join('/'))})`:'')
    + (d?` · 任务集 <code>${esc(d.name)}</code>(${d.count} 题) · 本次 训练 ${nTrain} / 考试 ${nEval}`:'')
    + extra;
  // 环境状态跟着选择走。节流,因为它每个任务镜像都要问一次 docker。
  clearTimeout(loadPrepare._t);
  loadPrepare._t=setTimeout(loadPrepare, 400);
}

async function fillTrainRuns(){
  // 只列**留下了 harness 状态**的运行,因为 eval 要测的就是那个状态。列一个没有状态的
  // 运行只会换来一条拒绝。
  let runs=[];
  try{ runs=await j('/api/runs'); }catch(e){ runs=[]; }
  RUNS_CACHE=runs;
  const usable=runs.filter(r=>!r.refused && r.state_rounds && r.state_rounds.length);
  $('#f-fromrun').innerHTML = usable.length
    ? usable.map(r=>`<option value="${esc(r.run_id)}">${esc(r.run_id)} · 状态轮次 ${esc(r.state_rounds.join(','))}</option>`).join('')
    : `<option value="">(没有留下状态的运行 —— 先跑一次 train)</option>`;
}
async function go(side){
  const btn=$('#'+(side==='eval'?'btn-eval':'btn-train'));
  btn.disabled=true;
  const cfg={harness:$('#f-harness').value,dataset:$('#f-dataset').value,
    method:$('#f-method').value,rounds:side==='eval'?0:$('#f-rounds').value,
    improver:$('#f-improver').value||'', improver_model:($('#f-imodel').value||'').trim(),
    model:$('#f-model').value,overwrite:true,side:side,
    from_run:side==='eval'?$('#f-fromrun').value:'',
    run_id:(side==='eval'?$('#f-evalid'):$('#f-runid')).value,
    tasks:chosenTasks(side)};
  if(side==='eval' && !cfg.from_run){
    $('#cfg-hint').innerHTML='<span class="bad">先选一个训练状态 —— 考试测的就是它</span>';
    btn.disabled=false; return;
  }
  const r=await j('/api/run?cfg='+encodeURIComponent(JSON.stringify(cfg)));
  if(r.error){ $('#cfg-hint').innerHTML='<span class="bad">✗ '+esc(r.error)+'</span>';
    btn.disabled=false; return; }
  CUR=r.started; (side==='eval'?$('#f-evalid'):$('#f-runid')).value=CUR;
  RUN_SIDE=side;
  $('#prog').style.display='block'; $('#btn-stop').style.display='inline-block';
  $('#curve-card').style.display='none';
  // 按下开始就把设置收起来,把屏幕让给进度和曲线(摘要由 toggleSetup 填)。
  toggleSetup(false);
  $('#run-name').textContent=CUR; $('#run-badge').textContent='● 运行中';
  $('#run-badge').className='badge'; $('#run-banner').innerHTML='';
  $('#run-facts').innerHTML=''; $('#run-life').innerHTML='';
  view('run'); poll();
}
// 主题:深色是默认(工作台看的是曲线和日志,深色不刺眼),浅色留给截图和打印。
function toggleTheme(){
  const cur=document.documentElement.dataset.theme==='light'?'dark':'light';
  document.documentElement.dataset.theme=cur;
  try{ localStorage.setItem('hg-theme',cur); }catch(e){}
  // 图表是画在 SVG 里的,颜色取自常量而不只是 CSS 变量,所以换主题要重画。
  if(RUN&&RUN.points&&RUN.points.length) drawChart(RUN.points);
}

// 实验设置:默认展开。跑起来之后自动收起(结果才是要一直看的东西),
// 点标题栏的「展开」可以再打开。收/展状态只在本页有效,不写 localStorage ——
// 它是"跟着运行状态走"的,不是一个人的偏好。
function toggleSetup(open){
  const box=$('#setup'); if(!box) return;
  const want = (open===undefined) ? box.classList.contains('collapsed') : !!open;
  if(!want){ const sum=$('#setup-sum'); if(sum) sum.textContent=setupSummary(); }
  box.classList.toggle('collapsed', !want);
  if(want) setTimeout(()=>{const f=$('#f-method'); f&&f.focus();}, 180);
  setTimeout(redrawCharts, 220);
}

// 收起时那一行摘要要说清"这次跑什么",否则收起就等于把配置藏了。
function setupSummary(){
  const h=$('#f-harness'), d=$('#f-dataset'), m=$('#f-method'),
        im=$('#f-improver'), r=$('#f-rounds');
  const opt=sel=>sel&&sel.selectedOptions&&sel.selectedOptions[0]
    ? sel.selectedOptions[0].textContent.trim() : '';
  const bits=[opt(h), opt(d), opt(m)].filter(Boolean);
  const imp=opt(im); if(imp) bits.push('改进器 '+imp);
  const ro=r&&r.value?r.value:''; if(ro) bits.push(ro+' 步');
  const n=(typeof PICKED!=='undefined'&&PICKED.train)?PICKED.train.size:0;
  if(n) bits.push(n+' 题');
  return bits.join(' · ');
}

// 首页:最近一次实验的曲线 + 最近的实验列表。没有运行时给一句空状态,不画空图。
// 图跟着容器宽度重画。抽屉开合会改变主区宽度,而 SVG 用 `preserveAspectRatio="none"`
// 拉伸 —— 不重画就会把曲线横向压扁(viewBox 宽度还是旧的)。
//
// 这里刻意**不用 ResizeObserver**:这个页面是一个大内联脚本,观察器的回调在时序和
// 作用域上都不好推理,而且实测没能触发。显式重画更容易验证:抽屉开合调一次、
// 窗口 resize 调一次。代价是这两个时机之外的宽度变化不会自动跟随,目前没有那种情况。
function redrawCharts(){
  if(window.HOME_PTS&&window.HOME_PTS.length) drawChart(window.HOME_PTS,'#home-chart');
  if(window.RUN_PTS&&window.RUN_PTS.length) drawChart(window.RUN_PTS,'#chart');
}
window.addEventListener('resize',()=>redrawCharts());

async function loadHome(){
  let runs=[];
  try{ runs=await j('/api/runs'); }catch(e){ runs=[]; }
  RUNS_CACHE=runs;
  const withCurve=runs.filter(r=>(r.rounds||0)>0);
  const newest=withCurve[0];
  const body=$('#home-body'), title=$('#home-title'), meta=$('#home-meta');

  if(!newest){
    if(title) title.textContent='最近的实验';
    if(meta) meta.textContent='';
    if(body) body.innerHTML='还没有跑过实验。右上角 <b>⚙ 配置</b> 里选好 harness、方法与改进器,然后开始训练。';
  } else {
    if(title) title.textContent='最近的实验';
    if(meta) meta.innerHTML=`<a href="#" onclick="showRun('${esc(newest.run_id)}');return false">${esc(newest.run_id)} →</a>`;
    if(body){
      body.innerHTML=`<div id="home-facts" class="row" style="gap:8px;flex-wrap:wrap;margin-bottom:10px"></div>
        <div class="chartwrap" style="min-height:260px"><svg id="home-chart" viewBox="0 0 900 300"
          preserveAspectRatio="none" style="height:260px;display:block;width:100%"></svg></div>`;
      try{
        const d=await j('/api/run/'+newest.run_id);
        RUN=d;                       // drawChart 读全局 RUN
        SEL=(d.points||[]).length-1;
        window.HOME_PTS=d.points||[];
        drawChart(window.HOME_PTS, '#home-chart');
        const last=d.points[d.points.length-1], idn=last.identity||{};
        $('#home-facts').innerHTML=[
          `<span class="badge">${(d.points||[]).length} 轮</span>`,
          `<span class="badge">得分 <b>${fmt(last.score)}</b></span>`,
          idn.improver&&idn.improver.name?`<span class="badge">${esc(idn.improver.name)} ${esc(idn.improver.version||'')}</span>`:'',
          idn.agent_model?`<span class="badge">${esc(idn.agent_model)}</span>`:'',
        ].filter(Boolean).join('');
      }catch(e){ body.innerHTML='<span class="bad">读不到这次运行的细节。</span>'; }
    }
  }

  const rows=$('#runs-home tbody');
  if(rows){
    rows.innerHTML=runs.slice(0,8).map(r=>`<tr class="click" onclick="showRun('${esc(r.run_id)}')">
      <td><code>${esc(r.run_id)}</code></td>
      <td>${r.method?`<span class="pill accent">${esc(r.method)}</span>`:'<span class="dim">—</span>'}</td>
      <td><b>${(r.rounds||0)?fmt(r.last_score):'—'}</b></td>
      <td>${r.rounds||0}</td>
      <td class="dim">${esc(r.improver||'—')}</td></tr>`).join('')
      || '<tr><td colspan="5" class="hint">还没有记录。</td></tr>';
  }
}

function stopRun(){ if(CUR) fetch('/api/stop/'+CUR); }

// 「重跑这次配置」:把一次**已记录**的运行翻译回表单。
//
// 存在的理由很具体:换了改进器(或换了它的模型/effort)之后最想做的事就是拿同一个
// 方法重跑一遍,而照着记忆把表单填回原样正是人会填错的地方 —— 而填错的代价是又一轮
// 几十分钟的测量,读的人还分不清两条曲线为什么不同。
//
// 能确定的就填,不能确定的就不填,并在提示里说清楚哪一项没对上(诚实优先于假装)。
async function replay(runId){
  const run=(RUNS_CACHE||[]).find(r=>r.run_id===runId);
  const notes=[];
  if(!run){ $('#cfg-hint').innerHTML='<span class="bad">找不到这次运行的记录</span>'; return; }
  if(run.dataset){
    if([...$('#f-dataset').options].some(o=>o.value===run.dataset)) $('#f-dataset').value=run.dataset;
    else notes.push(`任务集 ${run.dataset}(下拉里没有)`);
  }
  if(run.harness){
    const h=(OPTS.harnesses||[]).find(x=>x.name===run.harness);
    if(h && [...$('#f-harness').options].some(o=>o.value===h.dir)) $('#f-harness').value=h.dir;
    else notes.push(`harness ${run.harness}(下拉里没有,可能已被筛掉)`);
  }
  if(run.method && [...$('#f-method').options].some(o=>o.value===run.method)){
    $('#f-method').value=run.method;
  } else if(run.method){
    notes.push(`方法 ${run.method}(下拉里没有)`);
  }
  $('#f-rounds').value=Math.max(1,(run.rounds||3)-1);   // 减掉 round 0 的基线
  await loadTasks();
  if(run.side==='train'){
    PICKED.train=new Set(run.tasks||[]);
    renderTasks();
  }
  renderSplit();
  view('exp', document.querySelector('button[data-v="exp"]'));
  $('#cfg-hint').innerHTML = `<span class="ok">已按 ${esc(runId)} 填好`
    + (notes.length?`;这几项没对上:${esc(notes.join('、'))}`:';改完改进器就能直接开始')
    + '</span>';
}

// 日志面板按 driver 写出的事件排版,不按终端文本排版。
// 文本是给人扫的;事件带着 driver 已经判定好的语义(哪一轮、哪个分数、是警告还是
// 错误)。让面板用正则去文本里反推这些,等于让显示逻辑猜运行逻辑 —— 而这两者一旦
// 不一致,面板就会把一次失败的运行画成一次正常的运行。
function evNum(v){ return (v===null||v===undefined||isNaN(v))?'—':Number(v).toFixed(3); }

// 上一次渲染的指纹。没有新东西时面板不重画 —— 每 1.5 秒换一次 innerHTML 会销毁并
// 重建 <details>,于是"原始输出"一展开就被关掉;顺带也会打断用户正在做的文本选择和
// 滚动位置。这两个症状同源,所以在这里一起修。
let EVKEY=null;
// 事件到达面板的时间 `seq -> 本地毫秒`。事件流里没有"此刻",而进度条上要显示一个
// 会自己走的秒数,否则两条心跳之间的 10 秒会看起来像卡住。
let EVAT={};
// 正在跑的时候每 5 秒强制重画一次,只为让那个秒数往前走。
let EVTICK=0;
setInterval(()=>{ if(CUR && RUNNING){ EVTICK++; renderEvents(LASTEV, LASTLOG, true); } },5000);
let LASTEV=null, LASTLOG='', RUNNING=false;

function renderEvents(ev, log, running){
  const box=$('#prog-log');
  LASTEV=ev; LASTLOG=log; RUNNING=!!running;
  if(ev && ev.length){
    const now=Date.now();
    for(const e of ev){
      const s=e.seq;
      if(s!==undefined && EVAT[s]===undefined) EVAT[s]=now;
      if(s!==undefined) e.at=EVAT[s];
    }
  }
  // 指纹带上最后一个事件的 seq:先前只看长度,而 beat 是**同一条**记录的重复追加,
  // 长度会变所以够用;seq 更稳。EVTICK 只在运行中前进,于是"跑着的时候每 5 秒重画
  // 一次、停下就完全不动"这两件事由同一个键决定。
  const last = ev && ev.length ? (ev[ev.length-1].seq ?? ev.length) : 0;
  const key = (CUR||'')+'|'+last+'|'+EVTICK+'|'+(log?log.length:0)+'|'+(running?'r':'s');
  if(key===EVKEY) return;
  EVKEY=key;

  // innerHTML 替换一定会丢掉展开状态,所以先记下来。这条不是保险:有新事件时面板
  // 确实必须重画,而重画就会重建那个 details。
  const wasOpen = !!box.querySelector('.ev-raw details[open]');
  const atBottom = box.scrollHeight-box.scrollTop-box.clientHeight < 24;

  const finish = (html) => {
    box.innerHTML = html;
    if(wasOpen){
      const d = box.querySelector('.ev-raw details');
      if(d) d.open = true;
    }
    if(running && atBottom) box.scrollTop = box.scrollHeight;
  };

  const raw = log?`<div class="ev-raw"><details><summary>原始输出</summary><pre>${esc(log)}</pre></details></div>`:'';
  if(!ev || !ev.length){
    return finish(`<div class="ev-empty">${running
      ? '已启动,等待 driver 写出第一个事件…'
      : '这次运行没有事件流 —— 它是加事件之前跑的,只剩终端文本。'}</div>` + raw);
  }
  const head=ev.find(e=>e.kind==='header'), sp=head&&head.split;
  let html='';
  if(head){
    const pairs=[
      ['harness', esc(head.harness)+' '+esc(head.version)],
      ['mode', esc(head.mode)+' · '+(head.mode==='A'?'平台驱动循环':'方法自驱循环')],
      ['dataset', esc(head.dataset)+' · '+head.n_tasks+' 题'
        + (sp?` · 训练 ${sp.train.length} / 评测 ${sp.eval.length}`:' · 未划分')],
      ['model', esc(head.model||'—')+' · '+esc(head.backend)],
      ['sandbox', esc(head.sandbox||'—')],
    ];
    html+='<div class="ev-head">'
        + pairs.map(p=>`<b>${p[0]}</b><span>${p[1]}</span>`).join('')+'</div>';
  }
  html+=evNow(ev, running, head && head.phase_labels);
  html+='<div class="ev-body">';
  for(const e of ev){
    let cls='', k='', m='', nums='';
    if(e.kind==='round'){
      cls = e.level==='error'?'err':(e.level==='warn'?'warn':'');
      k = 'round '+e.round;
      m = esc(e.label||'');
      if(e.note) m += (m?' — ':'')+esc(e.note);
      // 这行以前**永远**写 eval,连训练 run 也是。`score_kind` 就是为这个错误存在的:
      // 一个读曲线的人不该需要先知道这是哪一侧的 run,才能读懂这一格。
      const kind = e.score_kind==='training' ? 'train'
                 : (e.score_kind==='exam' ? 'eval' : 'eval');
      nums = e.measured===false
        ? '<span class="ev-n">未测量</span>'
        : `<span class="ev-n">${kind} ${evNum(e.eval)}</span>`
          + (e.train===null||e.train===undefined
             ? '' : `<span class="ev-n">train ${evNum(e.train)}</span>`);
    } else if(e.kind==='note'){
      cls = e.level==='error'?'err':(e.level==='warn'?'warn':'');
      k = e.level==='error'?'错误':(e.level==='warn'?'注意':'说明');
      m = esc(e.message||'');
    } else if(e.kind==='artifact'){
      k = esc(e.label); m = `<code>${esc(e.path)}</code>`;
    } else if(e.kind==='log'){
      // harness 打印的东西 / 判分脚本的输出。以前这些在平台里被读进内存就丢掉,
      // 只在 harness 失败时印一行 —— 于是"为什么 0 分"只能靠手工考古 trace。
      k = e.source==='check' ? '判分' : (e.source==='method' ? '方法' : 'harness');
      m = `${esc(e.task||'')}`;
      if(e.detail) m += ` — ${esc(String(e.detail).slice(0,220))}`;
      const body = e.body ? String(e.body) : '';
      if(body) m += `<details class="ev-out"><summary>输出 ${body.length} 字节</summary>`
                  + `<pre>${esc(body.slice(-4000))}</pre></details>`;
      nums = e.exit_code===null||e.exit_code===undefined
        ? '' : `<span class="ev-n">exit ${e.exit_code}</span>`;
    } else if(e.kind==='task' && e.status && e.status!=='start'){
      cls = e.status==='failed'?'err':'';
      k = '题目';
      m = `${esc(e.task||'')} — ${e.status==='failed'?'失败':'完成'}`;
      nums = `<span class="ev-n">${secs(e.elapsed_s)}`
           + (e.score===null||e.score===undefined ? '' : ` · ${evNum(e.score)}`)
           + `</span>`;
    } else if(e.kind==='phase' && e.status && e.status!=='start'){
      if(e.status==='failed'){
        cls='err'; k='阶段'; m=`${esc(e.phase||'')} 失败 — ${esc(e.detail||'')}`;
        nums=`<span class="ev-n">${secs(e.elapsed_s)}</span>`;
      }
      // 成功的阶段边界不进列表:每道题四个,一轮二十行,只会把 round 行挤下去。
      else continue;
    } else continue;
    html+=`<div class="ev-row ${cls}"><span class="ev-k">${k}</span>`
        + `<span class="ev-m">${m}</span>${nums}</div>`;
  }
  finish(html+'</div>'+raw);
}

// 「现在在哪」。以前没有这一块,于是面板在一轮 100 分钟里显示同一屏 —— driver 只在
// 轮次结束时写事件,轮内一道题都不写。它读的是同一份事件流,不是另一条通道,所以
// 它说的和记录里的不会漂移。阶段名从 header 的 `phase_labels` 来(那是 driver 发的),
// 不在 JS 里另存一份,免得 `PHASES` 里加了新阶段而这里只显示个裸标识符。
function phaseLabel(labels, name){
  if(!name) return '';
  return (labels && labels[name]) || name;
}

function secs(v){
  if(v===null||v===undefined||isNaN(v)) return '—';
  v=Number(v);
  if(v<90) return Math.round(v)+'s';
  const m=Math.floor(v/60), sec=Math.round(v%60);
  return (sec===60? (m+1)+'m00s' : m+'m'+String(sec).padStart(2,'0')+'s');
}

function evNow(ev, running, labels){
  // 只看最后一次 round 之后的事件:它描述轮内进度,而上一轮的任务行已经列表化过了。
  let ri=-1;
  for(let i=ev.length-1;i>=0;i--){ if(ev[i].kind==='round'){ ri=i; break; } }
  const cur=ev.slice(ri+1);
  const tasks=cur.filter(e=>e.kind==='task');
  const starts=tasks.filter(e=>e.status==='start');
  const done=tasks.filter(e=>e.status&&e.status!=='start');
  const beats=cur.filter(e=>e.kind==='beat');
  const phases=cur.filter(e=>e.kind==='phase'&&e.status==='start');
  const lastBeat=beats[beats.length-1];
  const lastPhase=phases[phases.length-1];
  const over=cur.filter(e=>e.kind==='note'&&e.phase);
  if(!tasks.length && !lastBeat) return '';

  // 阶段名和题目名分开取:心跳只带阶段(它属于当前 task 上下文),而 index/total
  // 只出现在 task 事件上 —— 心跳每 10 秒一条,不能为了这两个数再去查表。
  let task=(lastBeat&&lastBeat.task)||(starts.length?starts[starts.length-1].task:'');
  let phase=(lastBeat&&lastBeat.phase)||(lastPhase?lastPhase.phase:'');
  const ref=starts.length?starts[starts.length-1]:tasks[tasks.length-1];
  const total = ref && ref.total ? ref.total : 0;
  // 已完成 + 正在跑的这半道。没有 task 事件(纯方法阶段)时就退回 0。
  const doneN = done.length;
  const active = Boolean(lastBeat) || (starts.length>done.length);
  const index = Math.min(total||1, doneN + (active?1:0));
  const pct = total ? Math.round(100*(doneN + (active?0.5:0))/total) : 0;

  if(!running && !active) return '';   // 结束了的 run 不再占一块地方

  let label;
  if(active && phase) label = phaseLabel(labels, phase);
  else if(!active) label = '轮间隙(方法在改 harness / 平台在建下一轮)';
  else label = '阶段切换中';

  const beatAge = lastBeat && Number.isFinite(lastBeat.elapsed_s)
    ? secs(lastBeat.elapsed_s) : null;
  let tail;
  if(!running) tail = '已结束';
  else if(active && beatAge) tail = `阶段已跑 ${beatAge}`;
  else tail = '等这个阶段结束';
  // 没有新事件时也把秒数往前推:一条心跳之间的 10 秒里,进度条上那个数字如果冻住,
  // 看的人分不清是"平台没在数"还是"平台没在动" —— 而这一块的存在就是为了回答这个。
  const since = lastBeat ? (Date.now() - (lastBeat.at || Date.now())) : 0;
  if(running && active && lastBeat && Number.isFinite(lastBeat.elapsed_s))
    tail = `阶段已跑 ${secs(Number(lastBeat.elapsed_s) + since/1000)}`;

  const cls = over.length ? ' warn' : '';
  return `<div class="ev-now${cls}">`
    + `<div class="ev-now-l">`
    + `<span class="dot"></span>`
    + `<b>${esc(task||'—')}</b>`
    + (total?`<span class="num">第 ${index}/${total} 题 · 已完成 ${doneN}</span>`:'')
    + `<span class="ph">${esc(label)}</span>`
    + `<span class="sp"></span><span class="ev-last">${esc(tail)}</span>`
    + `</div>`
    + `<div class="ev-bar"><i style="width:${pct}%"></i></div>`
    + `</div>`;
}

async function poll(){
  if(!CUR) return;
  const s=await j('/api/job/'+CUR);
  $('#prog-title').textContent='实验 '+CUR;
  $('#prog-state').textContent=s.running?'运行中…':'已结束';
  renderEvents(s.events, s.log, s.running);
  if(s.running){ POLL=setTimeout(poll,1500); }
  else { $('#btn-stop').style.display='none';
    const b=$('#'+(RUN_SIDE==='eval'?'btn-eval':'btn-train')); if(b) b.disabled=false;
    loadHome(); showRun(CUR); }
}

// ---------- 曲线 + 每步 ----------
async function showRun(id){
  RUN=await j('/api/run/'+id);
  // 这个 run 还在跑吗?是的话接管成"正在看的那一次":`poll()` 本来就按
  // `/api/job/<CUR>` 渲染事件流,复用它,不要再写第二套渲染。
  let live=null;
  try{ live=await j('/api/job/'+id); }catch(e){ live=null; }
  if(live&&live.running){
    RUN=RUN||{}; RUN.running=true;
    $('#prog').style.display='block';
    $('#prog-title').textContent='进度 — '+id;
    $('#prog-state').textContent='运行中…';
    $('#btn-stop').style.display='inline-block';
    RUN_SIDE=RUN.side||'train';
    // 手上已经有 `/api/job/<id>` 的事件和日志了,直接画 —— 只挂轮询、不先画一次的话,
    // 第一屏会停在"等待输出…"直到下一次轮询(实测就是这样)。
    const box=$('#prog-log');
    if(box) renderEvents(live.events||[], live.log||'', true);
    if(POLL) clearTimeout(POLL);
    poll();                      // 接管轮询:曲线一出现就会自己画出来
  }
  view('run');
  CUR=id;
  const head=$('#run-name'); if(head) head.textContent=id;
  const badge=$('#run-badge'), side=$('#run-side'), facts=$('#run-facts'),
        banner=$('#run-banner'), life=$('#run-life'), notes=$('#run-notes'),
        logtail=$('#run-log');
  const m=RUN.meta||{};

  // 没曲线:区分"还在跑"和"被拒/结束但没有曲线"。以前两种情况共用一句话,而且
  // **只清了 facts、没清 notes/log 尾巴** —— 于是先点开一个跑完的 run、再点开一个
  // 正在跑的 run,标题换成了新的,下面却还挂着上一个 run 的文字(实测复现:
  // run-name=18481 而 notes 里是 41133 的方法自述)。跨 run 残留是这类页面最坏的一种
  // 错误:读者会把两个实验的证据读成同一个。
  if(!RUN || !RUN.points || !RUN.points.length){
    const running=!!(RUN && RUN.running);
    if(badge){
      badge.textContent=running?'● 运行中':'没有曲线';
      badge.className='badge';
    }
    if(banner) banner.innerHTML=`<div class="runbar ${running?'run':'err'}">`
      + (running
          ? `这次运行还在跑 —— 曲线要等第一轮测完才出现。进度在下面的<b>进度</b>卡片里。`
          : `这次运行没有曲线:它在门口被拒,或中途结束了。原因在 `
            + `<code>runs/${esc(id)}/console.log</code>。`)
      + `</div>`;
    $('#curve-card').style.display='none';
    // **每一个显示上一个 run 的槽位都要清干净。** 少清一个就会串实验。
    if(life) life.innerHTML='';
    if(facts) facts.innerHTML='';
    if(notes) notes.textContent='—';
    if(logtail) logtail.textContent='—';
    ['#run-rounds tbody','#run-tasks tbody'].forEach(sel=>{
      const tb=document.querySelector(sel); if(tb) tb.innerHTML='';
    });
    const rt=$('#run-tasks'), rr=$('#run-rounds');
    if(rt) rt.closest('.card').style.display='none';
    if(rr) rr.closest('.card').style.display='none';
    return;
  }
  // 有曲线了:把跑动时藏起来的两张表放回来。
  const rtc=$('#run-tasks'), rrc=$('#run-rounds');
  if(rtc) rtc.closest('.card').style.display='';
  if(rrc) rrc.closest('.card').style.display='';
  const pts=RUN.points, last=pts[pts.length-1], first=pts[0], idn=last.identity||{};
  const ran=(RUN.state_rounds||[]).length>0;

  // 顶部:一条状态 + 一排"事实"。参考图的信息密度就在这里 —— 每个事实一个小胶囊,
  // 没有一句解释性的话。
  if(badge){
    badge.textContent=(last.score>0? '● ':'● ')+(RUN.side==='eval'?'考试':'训练');
    badge.className='badge';
  }
  if(side) side.innerHTML=`<code>${esc(idn.harness_name||'')}</code> ${esc(idn.harness_version||'')}`;
  if(facts){
    const f=[];
    f.push(`<span class="badge" title="这一侧的测量点个数">${pts.length} 轮</span>`);
    f.push(`<span class="badge" title="最后一轮的得分(训练侧 = 被拟合抬高过)">得分 <b>${fmt(last.score)}</b></span>`);
    f.push(`<span class="badge" title="平台测量的任务数">${(last.task_ids||[]).length||Object.keys(last.per_task||{}).length} 题</span>`);
    if(idn.improver&&idn.improver.name)
      f.push(`<span class="badge" title="改进器:${esc(idn.improver.path||'')}">${esc(idn.improver.name)} ${esc(idn.improver.version||'')}</span>`);
    if(idn.improver&&idn.improver.model) f.push(`<span class="badge">${esc(idn.improver.model)}</span>`);
    if(idn.agent_model) f.push(`<span class="badge" title="做题的模型">${esc(idn.agent_model)}</span>`);
    if(idn.harness_sha) f.push(`<span class="badge" title="harness 内容地址(tree hash)"><code>${esc(String(idn.harness_sha).slice(0,10))}</code></span>`);
    facts.innerHTML=f.join('');
  }
  if(banner){
    const failed=(last.per_task?Object.values(last.per_task).filter(v=>v<1).length:0);
    banner.innerHTML=`<div class="runbar${failed?'':' '}">`
      + (failed? `${failed} 道题未通过 — 最后一轮的判分理由在下面。`
                : `全部通过。`) + `</div>`;
  }

  // 生命周期:和参考图一样,一列步骤 + 右侧耗时。这里的步骤是平台真实写出的 phase
  // 事件(每道题:创建/准备/做题/快照/判分/清理),按轮次聚合耗时。
  if(life){
    const ev=[];
    const PT={setup:'准备题目文件',create:'创建容器',prepare:'拷入 SETUP',
              agent:'harness 做题',snapshot:'停机+快照',verify:'题目检查判分',
              collect:'取回产物',teardown:'清理容器'};
    const byRound={};
    (RUN.events||[]).forEach(e=>{
      if(e.kind==='phase'&&e.status==='done'&&e.phase!=='agent'||e.kind==='phase'&&e.status==='done')
        byRound[e.round]=(byRound[e.round]||0)+(e.elapsed_s||0);
    });
    let rows='';
    rows+=`<div class="step done"><span class="dot ok"></span><span class="what">构建 workspace 并固定镜像</span><span class="dur">—</span></div>`;
    pts.forEach(p=>{
      const t=byRound[p.round];
      rows+=`<div class="step ${p.score>0?'ok':'done'}"><span class="dot ${p.score>0?'ok':''}"></span>`
        + `<span class="what">第 ${p.round} 轮 · ${esc(String(p.label||'').slice(0,54))}</span>`
        + `<span class="dur">${t?secs(t):''}</span></div>`;
    });
    rows+=`<div class="step ${ran?'ok':'err'}"><span class="dot ${ran?'ok':'err'}"></span>`
      + `<span class="what">${ran?'保留每轮 harness 状态':'没有留下状态'}</span><span class="dur"></span></div>`;
    life.innerHTML=rows;
  }
  if(notes){
    const h=last.method_hypothesis;
    notes.textContent=h?String(h):'方法这次没有写理由。';
  }
  if(logtail){
    const lines=(RUN.events||[]).filter(e=>e.kind==='log'||e.kind==='note').slice(-14)
      .map(e=>`${e.kind==='log'?('['+(e.source||'')+'] '):''}${e.detail||e.message||''}`)
      .filter(Boolean);
    logtail.textContent=lines.join('\n')||'—';
  }

  // 每一轮一行:得分、通过数、改动、以及那个 harness 的内容地址。
  const rr=$('#run-rounds tbody');
  if(rr){
    rr.innerHTML=pts.map(p=>{
      const per=p.per_task||{}, n=Object.keys(per).length;
      const passed=Object.values(per).filter(v=>v>=1).length;
      const files=(p.edits_applied||[]).length;
      return `<tr class="click" onclick="pick(${pts.indexOf(p)})">
        <td>${p.round}</td><td><b>${fmt(p.score)}</b></td>
        <td>${passed}/${n}</td>
        <td>${files?files+' 个文件':'<span class="dim">'+(p.method_reported&&p.method_reported.error?'失败':'无改动')+'</span>'}</td>
        <td><code>${esc(String(idn.harness_sha||'').slice(0,10))}</code></td></tr>`;
    }).join('');
  }
  // 每道题一行:最后一轮的得分与判分类型,点进去看生命周期。
  const rt=$('#run-tasks tbody');
  if(rt){
    const per=last.per_task||{}, vf=last.verifiers||{};
    rt.innerHTML=Object.keys(per).sort().map(t=>{
      const sc=per[t];
      return `<tr class="click" onclick="showTask('${esc(id)}','${esc(t)}')">
        <td><code>${esc(t)}</code></td>
        <td><b>${fmt(sc)}</b></td>
        <td>${sc>=1?'<span class="pill ok">通过</span>':'<span class="pill err">未通过</span>'}</td>
        <td class="dim">—</td></tr>`;
    }).join('') || '<tr><td colspan="4" class="hint">这一轮没有可判分的题目。</td></tr>';
  }

  $('#curve-card').style.display='block';
  $('#curve-title').textContent='得分曲线';
  $('#curve-meta').innerHTML=`${esc(idn.harness_name||'')} · ${esc(idn.agent_model||'')}`
    + ` · ${fmt(first.score)} → <b>${fmt(last.score)}</b>`;
  window.RUN_PTS=pts;
  drawChart(pts, '#chart');
  SEL=pts.length-1;
  $('#step-btns').innerHTML=pts.map((p,i)=>
    `<button class="${i===SEL?'on':''}" onclick="pick(${i})">step ${p.round} · ${fmt(p.score)}</button>`).join('');
  showStep(SEL);
}
function pick(i){ SEL=i; document.querySelectorAll('#step-btns button')
  .forEach((b,k)=>b.classList.toggle('on',k===i)); showStep(i); }

function drawChart(pts, target){
  // 颜色从 CSS 变量里取,不是写死的十六进制:否则切到深色主题时网格和坐标轴还是
  // 浅灰,而曲线会在浅背景的假设下继续描白底描边 —— 图是整个页面最显眼的东西,
  // 它不跟着主题走,主题就等于没换。
  const cs=getComputedStyle(document.documentElement);
  const V=k=>cs.getPropertyValue(k).trim()||'#888';
  const axis=V('--line-2'), grid=V('--grid'), faint=V('--fg-4'), text=V('--fg-2');
  const line=V('--brand'), card=V('--bg-2');
  // 目标 svg 的实际宽度决定 viewBox:抽屉打开时主区窄了,如果还用 900 的坐标,
  // 横向会被压扁(实测:曲线变成一条被挤扁的波浪,step 标签互相重叠)。
  const host0=$(target||'#chart');
  const rectW=(host0&&host0.getBoundingClientRect().width)||900;
  const W=Math.max(420, Math.round(rectW)), H=300, PL=56, PR=18, PT=18, PB=38;
  const n=pts.length;
  const X=i=> n===1? PL+(W-PL-PR)/2 : PL+i*(W-PL-PR)/(n-1);
  const Y=v=> PT+(1-v)*(H-PT-PB);
  let g='';
  for(let k=0;k<=5;k++){
    const v=k/5, y=Y(v);
    g+=`<line x1="${PL}" y1="${y}" x2="${W-PR}" y2="${y}" stroke="${grid}"/>`;
    g+=`<text x="${PL-10}" y="${y+4}" text-anchor="end" font-size="11" fill="${faint}">${v.toFixed(1)}</text>`;
  }
  if(n>1){
    const d=pts.map((p,i)=>`${X(i)},${Y(p.score)}`).join(' ');
    // 线下淡淡的填充,让"曲线"在深色底上也有分量。
    g+=`<polyline points="${d}" fill="none" stroke="${line}" stroke-width="2.5"
         stroke-linejoin="round" stroke-linecap="round"/>`;
  }
  pts.forEach((p,i)=>{
    const x=X(i), y=Y(p.score), on=i===SEL;
    g+=`<g class="pt" onclick="pick(${i})" style="cursor:pointer">
      <circle cx="${x}" cy="${y}" r="${on?7:5}" fill="${card}" stroke="${line}"
        stroke-width="${on?3:2.5}"/>
      <text x="${x}" y="${y-15}" text-anchor="middle" font-size="12"
        font-weight="620" fill="${on?text:faint}">${p.score.toFixed(3)}</text>
      <text x="${x}" y="${H-PB+20}" text-anchor="middle" font-size="11"
        fill="${on?line:faint}" font-weight="${on?620:400}">step ${p.round}</text>
    </g>`;
  });
  g+=`<line x1="${PL}" y1="${PT}" x2="${PL}" y2="${H-PB}" stroke="${axis}"/>`;
  g+=`<line x1="${PL}" y1="${H-PB}" x2="${W-PR}" y2="${H-PB}" stroke="${axis}"/>`;
  g+=`<text x="${(PL+W-PR)/2}" y="${H-6}" text-anchor="middle" font-size="11.5"
        fill="${faint}">step</text>`;
  // **viewBox 要跟着容器宽度走。** 忘了写这一行的话,几何按 W 算、而 viewBox 还是
  // HTML 里写死的 900 —— 于是 SVG 把它横向拉伸,线是歪的、step 标签会挤在一起。
  if(host0){
    host0.setAttribute('viewBox', `0 0 ${W} ${H}`);
    host0.innerHTML=g;
  }
}

// 一道题的解剖页。生命周期时间线放在这里才是对的层:它的每一步都属于**这一道题**。
async function showTask(runId, taskId){
  const d=await j(`/api/run/${runId}/task/${taskId}`);
  view('task');
  $('#task-name').textContent=taskId;
  $('#task-side').textContent=(d.found && d.side==='eval')?'考试':'训练';
  if(!d.found){ $('#task-body').innerHTML='<div class="hint">没有这道题的记录。</div>'; return; }

  const PT=d.phase_names||{};
  const last=d.rounds[d.rounds.length-1]||{};
  const scores=d.rounds.map(r=>r.score);
  window.TASK_PTS=d.rounds.map(r=>({round:r.round, score:r.score}));

  // 事实条:得分轨迹、命令数、解析错误、token。
  const tr=d.trace||{};
  const facts=[];
  facts.push(`<span class="badge">${d.rounds.length} 轮</span>`);
  facts.push(`<span class="badge">得分 ${scores.map(fmt).join(' → ')}</span>`);
  if(tr.usage) facts.push(`<span class="badge">${tr.usage.calls||0} 次模型调用 · ${(tr.usage.input_tokens||0)+(tr.usage.output_tokens||0)} tokens</span>`);
  facts.push(`<span class="badge">${tr.n_commands||0} 条命令</span>`);
  if(tr.n_parse_errors) facts.push(`<span class="badge" style="color:var(--err-fg)">${tr.n_parse_errors} 条解析失败</span>`);
  if(tr.extra_data) facts.push(`<span class="badge" title="一个回复里出现了多余的 JSON 对象 —— 旧解析器会因此结束整个任务">${tr.extra_data} 条多余数据</span>`);
  $('#task-facts').innerHTML=facts.join('');

  // 曲线小图 + 生命周期(按轮) + 判分理由 + 轨迹
  let html=`<div class="card">
    <h3>得分轨迹</h3>
    <div class="chartwrap" style="min-height:220px">
      <svg id="task-chart" viewBox="0 0 900 300" preserveAspectRatio="none"
        style="height:220px;display:block;width:100%"></svg></div>
  </div>`;

  const rounds=Object.keys(d.lifecycle||{}).map(Number).sort((a,b)=>a-b);
  if(rounds.length){
    html+=`<div class="card"><h3>生命周期 · 这道题的每个阶段</h3>`;
    rounds.forEach(r=>{
      const ph=d.lifecycle[String(r)]||[];
      const total=ph.reduce((a,x)=>a+(x.seconds||0),0);
      const top=[...ph].sort((a,b)=>(b.seconds||0)-(a.seconds||0))[0]||{};
      html+=`<div class="row" style="justify-content:space-between;margin:12px 0 2px">
        <b>第 ${r} 轮</b>
        <span class="hint">共 ${secs(total)} · 最贵的一步:${esc(PT[top.phase]||top.phase||'—')} ${secs(top.seconds)}</span>
      </div><div class="life">`;
      ph.forEach(x=>{
        html+=`<div class="step done"><span class="dot"></span>`
          + `<span class="what">${esc(PT[x.phase]||x.phase)}</span>`
          + `<span class="dur">${secs(x.seconds)}</span></div>`;
      });
      html+=`</div>`;
    });
    html+=`</div>`;
  }

  // 判分:每轮的理由。这是"为什么 0 分"唯一该被回答的地方。
  html+=`<div class="card"><h3>判分说了什么</h3>`;
  d.rounds.forEach(r=>{
    html+=`<div class="row" style="justify-content:space-between;margin:10px 0 4px">
      <b>第 ${r.round} 轮 · ${fmt(r.score)}</b>
      <span class="hint">${esc(r.kind||'')}${r.passed===true?' · 通过':''}</span></div>`;
    if(r.harness && r.harness.exit_code!==null && r.harness.exit_code!==undefined)
      html+=`<div class="hint">harness 退出码 ${r.harness.exit_code}</div>`;
    html+=`<div class="logtail">${esc(String(r.detail||'(没有判分输出)'))}</div>`;
    if(r.harness && r.harness.stderr)
      html+=`<details style="margin-top:6px"><summary class="hint">harness 输出 ${String(r.harness.stderr).length} 字节</summary>
        <div class="logtail" style="margin-top:6px">${esc(String(r.harness.stderr)).slice(0,6000)}</div></details>`;
  });
  html+=`</div>`;

  // 轨迹:harness 每一步做了什么。命令 + 退出码 + 输出前几行。
  if(tr.found && (tr.commands||[]).length){
    html+=`<div class="card"><h3>harness 每一步 <span class="hint" style="font-weight:400">最近一轮</span></h3>`;
    (tr.commands||[]).forEach(c=>{
      html+=`<div style="margin:8px 0">
        <div class="row" style="gap:8px;align-items:baseline">
          <span class="badge">step ${c.step}</span>
          <code style="overflow-wrap:anywhere">${esc(String(c.command).slice(0,180))}</code>
          <span class="grow" style="flex:1"></span>
          <span class="hint">exit ${c.exit===null||c.exit===undefined?'?':c.exit}</span>
        </div>`;
      if(c.output) html+=`<div class="logtail" style="margin-top:4px;max-height:120px">${esc(String(c.output).slice(0,900))}</div>`;
      html+=`</div>`;
    });
    html+=`</div>`;
  } else if(tr.found){
    html+=`<div class="card"><h3>harness 每一步</h3>
      <div class="hint">这一轮 harness 没有执行任何命令${tr.n_parse_errors?' —— 它收到了解析不了的回复就结束了。':''}</div></div>`;
  }
  $('#task-body').innerHTML=html;
  drawChart(window.TASK_PTS, '#task-chart');
}

async function showStep(i){
  const p=RUN.points[i];
  const d=await j(`/api/run/${RUN.run_id}/diff/${p.round}`);
  const per=p.per_task||{};
  const fails=Object.keys(per).filter(t=>per[t]<1);
  let html=`<div class="row" style="justify-content:space-between;margin-bottom:6px">
    <b>step ${p.round}</b>
    <span class="hint">得分 ${fmt(p.score)} · 区间 [${(p.score_ci95||[]).map(x=>Number(x).toFixed(2)).join(', ')}]
    · 通过 ${Object.keys(per).length-fails.length}/${Object.keys(per).length}</span></div>`;
  if(fails.length) html+=`<div class="hint">未通过:${fails.map(esc).join(', ')}</div>`;
  html+=`<div class="row" style="margin:8px 0">
    <button class="gh" onclick="explain(${p.round})">🤖 让模型解释这次改动</button>
    <button class="gh" onclick="replay(${JSON.stringify(RUN.run_id)})"
      title="把这次运行的配置填回表单,换改进器之后直接重跑">↻ 重跑这次配置</button>
    <span class="hint" id="expl-status"></span></div>`;
  // 把这一轮的状态存成 base_harness,下次的进化从这里继续。
  //
  // 只有**留下过状态**的轮次才能存:`state_after_round<N>` 是运行结束时刻的 harness,
  // 而它只在这一轮真的跑过、且保存成功时才存在。
  const hasState=(RUN.state_rounds||[]).includes(p.round);
  html+=`<div class="row" style="margin:8px 0">
    <button class="gh" ${hasState?'':'disabled'} onclick="promote(${p.round})">
      存为 base harness${hasState?'':' (这一轮没有留下状态)'}</button>
    <span class="hint" id="prom-status"></span></div>`;
  if(p.round===0){
    html+=`<div class="hint">step 0 是基线,没有改动。</div>`;
  } else if(!d.found){
    html+=`<div class="hint">这一步的 diff 没有记录。</div>`;
  } else {
    const files=(d.meta?.files||[]).map(f=>
      `<code>${esc(f.path)} ${f.added!=null?'+'+f.added:''} ${f.removed!=null?'-'+f.removed:''}</code>`).join('');
    html+=`<div class="files">${files||'<span class="hint">无文件改动</span>'}</div>
      <pre>${colorDiff(d.patch)}</pre>`;
  }
  // 这一轮的**身份**:三样东西各是谁。解耦之后它们是三条独立的事实,所以分开写:
  // 被改进的 harness 的树哈希(内容地址)、改进器的版本+哈希、方法自己的标签与假设。
  // 以前这里只有 harness 名和两个模型 —— 于是一次"改进器换了"看起来像"方法变强了"。
  const id=p.identity||{};
  const imp=id.improver;
  const cells=[];
  cells.push(['harness',
    `${esc(id.harness_name)} ${esc(id.harness_version||'')}`,
    `tree <code>${esc((id.harness_sha||'').slice(0,12))}</code>`]);
  if(imp && imp.name){
    cells.push(['improver',
      `${esc(imp.name)} ${esc(imp.version||'')}`,
      `${esc(imp.model||'—')} · sha <code>${esc((imp.sha256||'').slice(0,10))}</code>`
      + (imp.modified?' · <span class="bad">改造过</span>':'')]);
  } else {
    cells.push(['improver','未记录','这个方法自带改进器,或本机解析不到']);
  }
  cells.push(['method', p.label?esc(String(p.label).slice(0,60)):'—',
    esc(id.method_model||'—')]);
  html+=`<div class="split" style="margin:12px 0">`
    + cells.map(([k,v,s])=>`<div class="cell"><h4>${k}</h4><div class="v">${v}</div>`
        + `<div class="s">${s}</div></div>`).join('')
    + `</div>`;
  if(p.method_hypothesis){
    html+=`<div class="hint" style="margin-bottom:8px"><b>方法自己说的理由</b>:`
        + esc(String(p.method_hypothesis).slice(0,400)) + `</div>`;
  }
  html+=`<div id="expl-box"></div>`;
  $('#step-body').innerHTML=html;
}
function colorDiff(t){
  return esc(t).split('\n').map(l=>{
    if(l.startsWith('+')) return `<span class="add">${l}</span>`;
    if(l.startsWith('-')) return `<span class="del">${l}</span>`;
    if(l.startsWith('@@')) return `<span class="ctx">${l}</span>`;
    return l;
  }).join('\n');
}
async function explain(round){
  $('#expl-status').textContent='模型正在阅读…';
  const r=await j(`/api/explain?run=${encodeURIComponent(RUN.run_id)}&round=${round}`);
  if(r.error){ $('#expl-status').innerHTML='<span class="bad">'+esc(r.error)+'</span>'; return; }
  $('#expl-status').innerHTML='<span class="ok">由 '+esc(r.model)+' 生成</span>';
  $('#expl-box').innerHTML=`<div class="expl">${esc(r.explanation)}</div>`;
}

// ---------- 历史 ----------
async function loadHist(){
  const [runs,exps]=await Promise.all([j('/api/runs'),j('/api/experiments')]);
  const by=Object.fromEntries(runs.map(r=>[r.run_id,r]));
  $('#compare').innerHTML=exps.map(e=>{
    const rows=e.rows.map(row=>{const r=by[row.run];const s=r?r.last_score:0;
      return `<div class="row" style="gap:10px;margin:5px 0">
        <span style="flex:0 0 290px">${esc(row.label)}</span>
        <span style="flex:1;height:16px;background:#eef0f3;border-radius:4px;overflow:hidden">
          <span style="display:block;height:100%;width:${(s*100).toFixed(1)}%;background:var(--brand)"></span></span>
        <b style="flex:0 0 52px;text-align:right">${fmt(s)}</b></div>`;}).join('');
    return `<div class="card"><b>${esc(e.title)}</b>
      <div class="hint">${esc(e.question)}</div>${rows}
      <div style="margin-top:9px">${esc(e.finding)}</div>
      <div class="hint" style="margin-top:5px">${esc(e.why)}</div></div>`;
  }).join('');
  // 方法列现在是解耦的一半,值得单独一列:一次运行是"方法 + 改进器",而改进器在
  // 每个曲线点的 identity 里,不在这一行里 —— 所以这里只写方法,点进去看改进器。
  $('#runs tbody').innerHTML=runs.map(r=>`<tr class="click" onclick="showRun('${esc(r.run_id)}')">
    <td><code>${esc(r.run_id)}</code></td><td>${esc(r.harness)||'—'}</td>
    <td>${r.method?`<span class="pill accent">${esc(r.method)}</span>`:'<span class="dim">—</span>'}</td>
    <td class="dim">${esc(r.agent_model)||'—'}</td><td class="dim">${esc(r.method_model)||'—'}</td>
    <td>${r.rounds}</td><td><b>${fmt(r.last_score)}</b></td><td>${r.task_count}</td></tr>`).join('');

  // 仪表带:四个从记录里数出来的数。刻意不写"平均分"之类 —— 混着 train 与 exam 的
  // 平均值是一个没有意义的量,而一个摆在顶部的无意义数字会被当成结论引用。
  const st=$('#hist-stats');
  if(st){
    const withCurve=runs.filter(r=>r.rounds>0);
    const pts=withCurve.reduce((a,r)=>a+(r.rounds||0),0);
    const imp=(OPTS&&OPTS.improvers)||{};
    const def=Object.values(imp).find(x=>x.default)||Object.values(imp)[0]||null;
    st.innerHTML=[
      ['实验记录', String(runs.length), `${withCurve.length} 条有曲线`],
      ['测量点', String(pts), '每条曲线一轮一个点'],
      ['方法', String(new Set(withCurve.map(r=>r.method).filter(Boolean)).size),
       '记录里出现过的方法'],
      ['改进器', def?`${esc(def.name)} ${esc(def.version||'')}`:'未配置',
       def?(def.ok?'本机解析成功':'本机解析失败 —— 见设置'):'improvers/improvers.json'],
    ].map(([k,v,s])=>`<div class="cell"><h4>${k}</h4><div class="v">${v}</div><div class="s">${s}</div></div>`).join('');
  }
}

// ---------- 设置 ----------
const AGENT_FIELDS = [["HG_AGENT_MODEL","模型"],["HG_AGENT_BASE_URL","Base URL"],
                      ["HG_AGENT_API_KEY","API Key"]];
const METHOD_FIELDS = [["HG_METHOD_MODEL","模型"],["HG_METHOD_BASE_URL","Base URL"],
                       ["HG_METHOD_API_KEY","API Key"]];

async function loadSettings(){
  const [cfg, prov] = await Promise.all([j('/api/settings'), j('/api/providers')]);
  SETTINGS = cfg; PROVIDERS = prov;

  // 本地候选模型填进服务目录
  $('#svc-dir').innerHTML = (prov._local_model_dirs||[]).map(d =>
    `<option value="${esc(d)}">${esc(d.split('/').pop())} — ${esc(d)}</option>`).join('')
    || '<option value="">(没找到本地模型权重)</option>';
  svcStatus();
  renderCfg('agent', cfg);
  renderCfg('method', cfg);
}

function cfgValue(cfg, key){
  const e = cfg[key] || {};
  return e.value != null ? e.value : '';
}
function renderCfg(which, cfg){
  const fields = which==='agent'?AGENT_FIELDS:METHOD_FIELDS;
  const p = which==='agent'?'HG_AGENT':'HG_METHOD';
  const title = which==='agent'
    ? 'Harness 的模型 <span class="hint">— 执行任务,它的表现就是分数</span>'
    : '方法的模型 <span class="hint">— 改进 harness,独立预算</span>';

  const provOptions = Object.keys(PROVIDERS).filter(k=>!k.startsWith('_'))
    .map(k=>`<option>${esc(k)}</option>`).join('');
  const keyState = cfg[`${p}_API_KEY`] || {};
  const isLocal = (cfgValue(cfg,`${p}_BASE_URL`)||'').includes('127.0.0.1')
               || (cfgValue(cfg,`${p}_BASE_URL`)||'').includes('localhost');

  let html = `<div class="row" style="justify-content:space-between">
    <b>${title}</b>
    <span class="pill ${keyState.set||isLocal?'y':'n'}">${
      isLocal?'本地服务,无需密钥':(keyState.set?'API key 已配置':'API key 缺失')}</span></div>`;
  html += `<div class="config" style="margin-top:10px">
    <div class="field"><label>提供方</label>
      <select onchange="applyProvider('${which}', this.value)">
        <option value="">— 选择以自动填写 —</option>${provOptions}</select></div>`;
  for (const [k,label] of fields){
    if (k.endsWith('API_KEY')){
      html += `<div class="field" style="min-width:280px"><label>${label}</label>
        <input type="password" id="f-${k}"
          placeholder="${keyState.set?'已配置 — 输入新值以替换':'输入密钥;本地服务可留空'}"
          autocomplete="new-password"></div>`;
    } else if (k.endsWith('MODEL')){
      html += `<div class="field" style="min-width:220px"><label>${label}</label>
        <input type="text" id="f-${k}" value="${esc(cfgValue(cfg,k))}"
          list="models-${which}" placeholder="模型名">
        <datalist id="models-${which}"></datalist></div>`;
    } else {
      html += `<div class="field" style="flex:1;min-width:280px"><label>${label}</label>
        <input type="text" id="f-${k}" value="${esc(cfgValue(cfg,k))}"
          placeholder="服务地址,例如 https://api.deepseek.com"></div>`;
    }
  }
  html += `<div class="field"><button class="gh" onclick="testConn('${which}')">测试连接</button></div>
    </div><div class="hint" id="test-${which}" style="margin-top:8px"></div>`;
  $('#cfg-'+(which==='agent'?'agent':'method')).innerHTML = html;
}

function applyProvider(which, name){
  if (!name) return;
  const p = PROVIDERS[name]; if (!p) return;
  const pfx = which==='agent'?'HG_AGENT':'HG_METHOD';
  if (p.base_url) $('#f-'+pfx+'_BASE_URL').value = p.base_url;
  const dl = document.getElementById('models-'+which);
  if (dl) dl.innerHTML = (p.models||[]).map(m=>`<option value="${esc(m)}">`).join('');
  if ((p.models||[]).length && !$('#f-'+pfx+'_MODEL').value)
    $('#f-'+pfx+'_MODEL').value = p.models[0];
  const hint = $('#test-'+which);
  if (hint) hint.innerHTML = esc(p.note||'') +
    ((p.models||[]).length?` · 可用模型:${p.models.slice(0,4).join(', ')}`:'');
}

async function testConn(which){
  const el = $('#test-'+which);
  el.textContent = '正在调用模型…';
  // 先保存再测:测的必须是即将用于实验的那份配置
  await saveSettings(true);
  const r = await j('/api/test-connection?which='+which);
  el.innerHTML = r.ok ? `<span class="ok">✓ ${esc(r.detail)}</span>`
                      : `<span class="bad">✗ ${esc(r.detail)}</span>`;
}

async function saveSettings(silent){
  const cfg = {};
  for (const [k] of AGENT_FIELDS.concat(METHOD_FIELDS)){
    const el = $('#f-'+k);
    if (el && el.value) cfg[k] = el.value;   // 空 = 不改(不抹掉已存的 key)
  }
  const r = await j('/api/settings/save?cfg='+encodeURIComponent(JSON.stringify(cfg)));
  if (!silent){
    $('#save-msg').innerHTML = r.error
      ? `<span class="bad">✗ ${esc(r.error)}</span>`
      : `<span class="ok">✓ 已写入 .env:${(r.applied||[]).join(', ')||'(无改动)'}</span>`;
  }
  return r;
}

async function svcStatus(){
  const s = await j('/api/model-service?action=status&port='+($('#svc-port').value||8001));
  $('#svc-state').innerHTML = s.ready
    ? `<span class="ok">● 就绪</span> <span class="dim">(pid ${esc(s.pid)}, 端口 ${s.port})</span>`
    : (s.running ? `<span class="bad">● 端口被占但不应答</span>`
                 : `<span class="dim">○ 未运行(端口 ${s.port})</span>`);
  return s;
}
async function svc(action){
  const dir = $('#svc-dir').value, port = $('#svc-port').value;
  const r = await j(`/api/model-service?action=${action}&model_dir=${encodeURIComponent(dir)}&port=${port}`);
  $('#svc-state').innerHTML = r.error ? `<span class="bad">${esc(r.error)}</span>`
    : `<span class="dim">${esc(r.note||action+' 已发出')}</span>`;
  // 启动是异步的(加载要两分钟),所以轮询而不是等
  let n = 0;
  const timer = setInterval(async ()=>{ const s = await svcStatus();
    if (++n > 40 || (action==='start' && s.ready) || (action==='stop' && !s.running))
      clearInterval(timer); }, 6000);
}

initForm().then(()=>{
  // 默认展开:配置是"按一次"的东西,但进来第一件事就是选它。
  toggleSetup(true);
  loadHome();
});
sideStatus(); setInterval(sideStatus, 20000);
