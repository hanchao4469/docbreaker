"use strict";
const $=id=>document.getElementById(id);
const state={key:"",busy:false,offset:0,limit:25,total:0,metrics:null,history:[],tasksVersion:0,controlBusy:false};
const labels={uploading:"上传中",queued:"排队中",running:"处理中",completed:"已完成",failed:"失败"};
const stages={inspect:"检查文档",extract:"提取文本",llm:"模型解析",local:"本地解析",completed:"已完成",assemble:"合并结果"};
const esc=value=>String(value??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const number=value=>value==null?"—":Number(value).toLocaleString("zh-CN",{maximumFractionDigits:1});
const percent=value=>value==null?"采样中":`${number(value)}%`;
const duration=value=>value==null?"—":value<1000?`${Math.round(value)} ms`:value<60000?`${number(value/1000)} 秒`:`${number(value/60000)} 分钟`;
const bytes=value=>value==null?"不可用":value>=1024**3?`${number(value/1024**3)} GiB`:`${number(value/1024**2)} MiB`;
const clock=value=>value?new Date(value*1000).toLocaleString("zh-CN",{hour12:false}):"—";
const text=(id,value)=>{$(id).textContent=value;};
const tenantName=value=>value==null?"未指定租户":String(value);
function badge(status){return `<span class="tag status-${esc(status)}">${esc(labels[status]||status)}</span>`;}
async function request(path,method="GET"){
 const response=await fetch(path,{method,headers:{Authorization:`Bearer ${state.key}`},cache:"no-store",signal:AbortSignal.timeout(12000)});
 if(response.status===401){const error=new Error("API Key 无效或已失效，请重新输入。");error.auth=true;throw error;}
 if(!response.ok)throw new Error(`读取失败：HTTP ${response.status}`);
 return response.json();
}
function connection(label,kind){text("connection",label);$("connection").className=`badge ${kind}`;}
function disconnect(message=""){
 state.key="";state.tasksVersion++;state.metrics=null;state.history=[];state.offset=0;$("api-key").value="";$("app").hidden=true;$("login").hidden=false;$("refresh").hidden=true;$("logout").hidden=true;connection("待连接","neutral");text("login-error",message);
}
function meter(id,used,limit){$(id).max=limit||1;$(id).value=used||0;}
function histogram(points){
 const width=700,height=165,left=28,bottom=136,top=10,step=(width-left-8)/Math.max(1,points.length);
 const max=Math.max(1,...points.map(p=>p.completed+p.failed));
 let out=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="近一小时处理文档数量">`;
 for(let i=0;i<3;i++){const y=top+(bottom-top)*i/2;out+=`<line class="grid" x1="${left}" y1="${y}" x2="${width}" y2="${y}"/><text x="0" y="${y+4}">${Math.round(max*(1-i/2))}</text>`;}
 points.forEach((p,i)=>{const x=left+i*step+step*.25,w=step*.5,h=p.completed/max*(bottom-top),f=p.failed/max*(bottom-top);out+=`<rect class="completed-bar" x="${x}" y="${bottom-h}" width="${w}" height="${h}" rx="2"><title>${esc(clock(p.at))} · 完成 ${p.completed} · 失败 ${p.failed}</title></rect><rect class="failed-bar" x="${x}" y="${bottom-h-f}" width="${w}" height="${f}" rx="2"/>`;if(i%3===0||i===points.length-1)out+=`<text x="${x+w/2}" y="158" text-anchor="middle">${new Date(p.at*1000).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit",hour12:false})}</text>`;});
 $("throughput-chart").innerHTML=out+"</svg>";
}
function resourceChart(){
 const h=state.history,w=1000,height=90,top=8,bottom=66;
 if(h.length<2){$("resource-chart").innerHTML='<div class="empty">等待下一次资源采样…</div>';return;}
 const path=key=>h.map((p,i)=>p[key]==null?"":`${i===0||h[i-1][key]==null?"M":"L"}${i/(h.length-1)*w},${bottom-Math.min(100,Math.max(0,p[key]))/100*(bottom-top)}`).join(" ");
 $("resource-chart").innerHTML=`<svg viewBox="0 0 ${w} ${height}" preserveAspectRatio="none" role="img" aria-label="CPU 容量使用率与内存参考使用率"><line class="grid" x1="0" y1="${bottom}" x2="${w}" y2="${bottom}"/><path class="cpu-line" d="${path("cpu")}"/><path class="mem-line" d="${path("memory")}"/><text x="0" y="85">CPU 容量占用 · 绿色 / 内存参考占用 · 蓝色</text><text x="${w}" y="85" text-anchor="end">本次页面 · ${Math.round((h.at(-1).at-h[0].at)/60)} 分钟</text></svg>`;
}
function tenantTable(){
 const rows=(state.metrics?.tenants||[]).filter(t=>tenantName(t.tenant_id).includes($("tenant-search").value.trim()));
 $("tenant-body").innerHTML=rows.length?rows.map(t=>`<tr><td><button class="tenant-link" data-tenant="${esc(t.tenant_id??"unassigned")}">${esc(tenantName(t.tenant_id))}</button></td>${[t.total,t.uploading,t.queued,t.running,t.completed,t.failed,t.processed_pages,t.model_calls,t.total_tokens].map(v=>`<td>${number(v)}</td>`).join("")}</tr>`).join(""):'<tr><td colspan="10" class="empty">没有匹配的租户任务</td></tr>';
}
function render(m){
 state.metrics=m;const s=m.service,t=m.totals,p=m.performance,r=m.system,model=m.model,usage=model.usage||{};
 connection(!s.healthy?"服务异常":!s.accepting?"排空中":"服务正常",!s.healthy?"bad":!s.accepting?"warn":"good");
 text("admission-note",s.accepting?`正在接收新任务 · 当前已受理 ${s.outstanding} 份`:`已暂停新任务 · 尚余 ${s.outstanding} 份已受理任务（上传 ${s.uploading} / 排队 ${s.queued} / 运行 ${s.running}）`);
 text("deploy-readiness",s.accepting?"接收中":s.safe_to_deploy?"已排空，可安全发布":"排空中，请等待");$("deploy-readiness").className=`tag ${s.accepting?"neutral":s.safe_to_deploy?"good":"warn"}`;text("admission-toggle",s.accepting?"暂停接收新任务":"恢复接收新任务");$("admission-toggle").disabled=state.controlBusy;
 text("updated",`更新于 ${new Date(m.sampled_at*1000).toLocaleTimeString("zh-CN",{hour12:false})} · 5 秒刷新`);
 text("build",`BUILD ${(s.build||"unknown").slice(0,12)}`);text("uptime",`本次运行 ${duration(s.uptime_seconds*1000)}`);
 text("scope",`保留任务窗口 ${number(m.scope.retention_seconds/3600)} 小时 · ${number(t.total)} 份任务 · ${number(m.tenants.length)} 个租户分组`);
 const warnings=[];if(!s.healthy)warnings.push("服务工作线程异常，请检查服务日志。");if(!s.model_configured)warnings.push("模型未配置：需要视觉解析的页面无法完成。");if(m.scope.invalid_state_files)warnings.push(`${m.scope.invalid_state_files} 个任务状态文件异常，未计入统计。`);if(!s.accepting)warnings.push("服务正在排空，暂不接受新任务。");$("alert").hidden=!warnings.length;text("alert",warnings.join(" "));
 text("running",number(s.running));text("running-note",`${s.running} / ${s.document_limit} 槽位 · 上传中 ${s.uploading}`);text("queued",number(s.queued));text("queue-note",`最长等待 ${duration(p.oldest_queue_seconds*1000)}`);text("completed",number(t.completed));text("completed-note",`共 ${number(t.pages)} 页已识别 / ${number(t.processed_pages)} 页已处理`);text("failed",number(t.failed));text("failed-note",`终态成功率 ${t.success_rate==null?"—":percent(t.success_rate*100)}`);text("speed",number(p.pages_per_minute));text("speed-note",`页 / 分钟 · ${number(p.docs_per_minute)} 文档 / 分钟 · 近 5 分钟`);text("tokens",number(usage.total_tokens||0));text("tokens-note",`输入 ${number(usage.prompt_tokens||0)} · 输出 ${number(usage.completion_tokens||0)}`);
 text("avg-duration",duration(p.average_processing_ms));text("p95-duration",duration(p.p95_processing_ms));text("avg-queue",duration(p.average_queue_ms));text("processed-pages",number(t.processed_pages));histogram(m.timeline);
 text("model-name",`${s.model} · 模型槽位占用表示当前占用的并发额度`);text("model-health",s.model_configured?"模型已配置":"未配置");text("doc-slots",`${s.running} / ${s.document_limit}`);text("model-slots",`${s.model_in_flight} / ${s.model_limit} · ${percent(s.model_in_flight/s.model_limit*100)}`);text("page-slots",`${s.pages_in_flight} / ${s.page_limit}`);meter("doc-progress",s.running,s.document_limit);meter("model-progress",s.model_in_flight,s.model_limit);meter("page-progress",s.pages_in_flight,s.page_limit);
 text("model-waiting",number(s.model_waiting));text("model-calls",number(model.calls||0));text("model-retries",number(model.retries||0));text("model-failed",number(model.failed||0));text("model-detail",`本次进程模型峰值 ${s.model_peak} / ${s.model_limit}。重试及失败为新增观测明细，历史任务不补算。模型耗时含调用内重试等待。`);
 text("host-cpu",percent(r.host_cpu_percent));text("container-cpu",percent(r.container_cpu_percent));text("cpu-capacity",`CPU 可用容量 ${number(r.cpu_cores)} 核`);text("host-memory",`主机可用内存 ${bytes(r.host_memory_available_bytes)} / ${bytes(r.host_memory_total_bytes)}`);text("memory",bytes(r.container_memory_bytes));text("memory-limit",r.container_memory_limit_bytes?`容器硬上限 ${bytes(r.container_memory_limit_bytes)}`:`未设硬上限 · 主机参考 ${bytes(r.host_memory_total_bytes)}`);text("rss",bytes(r.process_rss_bytes));text("disk",bytes(r.disk.free_bytes));text("disk-note",`文件系统总量 ${bytes(r.disk.total_bytes)}，非任务目录配额`);
 if(!state.history.length||state.history.at(-1).at!==m.sampled_at){state.history.push({at:m.sampled_at,cpu:r.container_cpu_capacity_percent,memory:r.container_memory_capacity_bytes&&r.container_memory_bytes!=null?r.container_memory_bytes/r.container_memory_capacity_bytes*100:null});if(state.history.length>180)state.history.shift();}resourceChart();
 const selected=$("tenant-filter").value;$("tenant-filter").innerHTML='<option value="">全部租户</option>'+m.tenants.map(t=>`<option value="${esc(t.tenant_id??"unassigned")}">${esc(tenantName(t.tenant_id))}</option>`).join("");if([...$("tenant-filter").options].some(o=>o.value===selected))$("tenant-filter").value=selected;tenantTable();
 $("errors").innerHTML=m.recent_errors.length?m.recent_errors.map(e=>`<div class="error-item"><div><span class="filename" title="${esc(e.filename)}">${esc(e.filename)}</span><span class="sub">租户 ${esc(tenantName(e.tenant_id))} · ${esc(clock(e.completed_at))}</span><span class="sub code">${esc(e.task_id)}</span></div><b>${esc(e.code)}</b></div>`).join(""):'<div class="empty">保留任务中暂无失败文档</div>';
 text("local-pages",number(t.local_pages));text("visual-pages",number(t.model_pages));text("blank-pages",number(t.blank_pages));text("input-tokens",number(usage.prompt_tokens||0));text("output-tokens",number(usage.completion_tokens||0));text("task-retries",number(t.task_retries));text("observed-attempts",number(model.observed_attempts||0));text("model-latency",duration(model.average_attempt_ms));text("model-errors",Object.entries(model.errors||{}).length?`模型异常分类：${Object.entries(model.errors).map(([k,v])=>`${k} × ${v}`).join("；")}`:"尚无新增模型异常记录。");
}
async function loadTasks(){
 const version=++state.tasksVersion;const query=new URLSearchParams({offset:state.offset,limit:state.limit});if($("tenant-filter").value)query.set("tenant",$("tenant-filter").value);if($("status-filter").value)query.set("status",$("status-filter").value);
 const data=await request(`/v1/dashboard/tasks?${query}`);if(version!==state.tasksVersion||!state.key)return;state.total=data.total;
 $("task-body").innerHTML=data.items.length?data.items.map(t=>`<tr><td><span class="filename" title="${esc(t.filename)}">${esc(t.filename)}</span><span class="sub code">${esc(t.task_id)}</span></td><td>${esc(tenantName(t.tenant_id))}</td><td>${badge(t.status)}<span class="sub">${esc(t.status==="completed"?"已完成":stages[t.stage]||t.stage||"—")}</span></td><td>${number(t.processed_pages??0)} / ${number(t.page_count)}</td><td>${duration(t.status==="running"&&t.started_at?(Date.now()/1000-t.started_at)*1000:t.duration_ms)}</td><td>${number(t.model_calls??0)} 次<span class="sub">${number(t.total_tokens)} Token</span></td><td>${esc(clock(t.created_at))}</td><td><span class="code">${esc(t.error_code||"—")}</span></td></tr>`).join(""):'<tr><td colspan="8" class="empty">暂无匹配任务</td></tr>';
 text("page-info",`共 ${number(data.total)} 份 · 第 ${Math.floor(state.offset/state.limit)+1} 页`);text("task-summary",`按提交时间倒序 · ${number(data.total)} 份匹配任务`);$("prev").disabled=state.offset===0;$("next").disabled=state.offset+state.limit>=data.total;
}
async function refresh(){
 if(!state.key||state.busy)return;state.busy=true;$("refresh").disabled=true;
 try{const data=await request("/v1/dashboard/metrics");if(!state.key)return;render(data);await loadTasks();$("login").hidden=true;$("app").hidden=false;$("refresh").hidden=false;$("logout").hidden=false;text("login-error","");}
 catch(error){if(error.auth)disconnect(error.message);else{connection("连接中断","bad");text("login-error",error.message);$("alert").hidden=false;text("alert",`数据更新失败，当前展示上次成功采样。${error.message}`);}}
 finally{state.busy=false;$("refresh").disabled=false;$("connect").disabled=false;}
}
$("login-form").addEventListener("submit",event=>{event.preventDefault();state.key=$("api-key").value.trim();if(!state.key)return;$("connect").disabled=true;text("login-error","");refresh();});
$("logout").addEventListener("click",()=>disconnect());$("refresh").addEventListener("click",refresh);$("tenant-search").addEventListener("input",tenantTable);
function filterTasks(){state.offset=0;loadTasks().catch(error=>{if(error.auth)disconnect(error.message);else{connection("读取失败","bad");$("alert").hidden=false;text("alert",error.message);}});}
$("tenant-filter").addEventListener("change",filterTasks);$("status-filter").addEventListener("change",filterTasks);
$("tenant-body").addEventListener("click",event=>{const link=event.target.closest("[data-tenant]");if(link){$("tenant-filter").value=link.dataset.tenant;filterTasks();$("task-summary").scrollIntoView({behavior:"smooth",block:"center"});}});
$("prev").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-state.limit);loadTasks().catch(handleTaskError);});$("next").addEventListener("click",()=>{state.offset+=state.limit;loadTasks().catch(handleTaskError);});
function handleTaskError(error){if(error.auth)disconnect(error.message);else{$("alert").hidden=false;text("alert",error.message);}}
setInterval(()=>{if(!document.hidden)refresh();},5000);document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});

$("admission-toggle").addEventListener("click",async()=>{
 if(state.controlBusy||!state.metrics)return;
 const pause=state.metrics.service.accepting;
 if(!window.confirm(pause?"暂停接收新任务？已受理的文档会继续处理，全部完成后页面显示可安全发布。integrating application 开启旧解析器兜底时，新文档将转由旧服务处理。":"恢复接收新任务？"))return;
 state.controlBusy=true;$("admission-toggle").disabled=true;
 try{await request(`/v1/admin/${pause?"drain":"resume"}`,"POST");await refresh();}
 catch(error){handleTaskError(error);}
 finally{state.controlBusy=false;$("admission-toggle").disabled=false;}
});
