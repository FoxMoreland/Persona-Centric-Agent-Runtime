/* Native workbench bindings. The standalone design preview has its own data. */
window.pcarLive = (() => {
  "use strict";
  const api = () => window.pywebview.api;
  let activation=null, keyPanel=null, seenIntake=null;
  const keyLocked=()=>activation && activation.state!=="verified";
  class FormError extends Error {}
  const hostMessages=new Set([
    "请选择 Markdown 文件。","人物已切换。","记忆政策版本已变化。","接收记忆资料的模型已变化。",
    "角色包格式无效。","模型选项无效。","提示词文件中有重复的字段。","提示词文件超过容量。",
    "提示词文件需为有效的 UTF-8 JSON。","提示词与审定发布版不符，请选择完整的发布版提示词文件。",
    "此实例使用独立的模板和授权。请在新实例导入发布版，或管理原实例的用途授权。",
    "请选择本地发布版提示词 JSON 文件。","请选择有效的发布版提示词文件。","无法读取提示词文件。",
    "发布版提示词文件无法读取。","发布版提取或成卡模板的指纹不符。"
  ]);
  const bufferErrors={
    manual_buffer_full:"本轮缓冲已满，草稿已保留。确认发送或取消本轮后可继续。",
    manual_buffer_pending:"请先确认发送或取消当前缓冲的消息。",
    manual_buffer_scope_changed:"会话或模型连接已更新，草稿已保留。恢复原连接或取消本轮后可继续。",
    manual_confirmation_required:"请点击「确认发送本轮」。",
    input_buffer_policy_changed:"输入缓冲方式已更新，草稿已保留。请核对后再发送。",
    pending_batch_resolution_required:"定时缓冲仍有消息，请等本轮发出后再更改模式。",
    session_has_pending_work:"当前对话还有未处理的消息，请处理后再新建对话。"
  };
  const run = fn => (...args) => Promise.resolve().then(() => fn(...args)).catch(e => {
    const detail=String(e.message||e);
    if(detail.includes("deepseek_activation_required")){keyPrompt();return;}
    if(detail.includes("roleplay_prompts_review_required")){
      toast("角色扮演提示词待审定。");return;
    }
    if(detail.includes("roleplay_official_deepseek_required")){
      toast("角色扮演模式需要官方 DeepSeek 连接。");return;
    }
    if(detail.includes("roleplay_desktop_story_only")){
      toast("请在文游会话中直接输入剧情和台词。");return;
    }
    const known={session_sealed:"这段对话已封存，可新建对话继续。",session_not_current:"这段对话已归档，可返回当前会话继续。",
      reference_unavailable:"引用的消息已失效，请重新选择。",reference_source_changed:"引用的消息或权限已变化，请重新选择。",
      reference_budget_exceeded:"引用原文过长，无法和本次输入一起发送。",
      multiple_quote_references:"请先发送或取消当前待发消息，再引用另一条。",
      session_has_pending_work:"请先处理当前会话中待发送或还在回复的消息。",
      quote_cannot_request_public_lookup:"请移除引用后，再发起公开查找。",version_conflict:"内容已更新，请重新打开核对。",
      credential_required_for_new_endpoint:"请填写新网关的 API Key。",session_privacy_recipients_changed:"接收方已变化，请重新打开授权设置。",
      main_reply_assembly_unavailable:"请先启用候选、录入密钥并确认本机正文装配。",
      main_reply_session_authorization_required:"请在主回复模型设置中确认当前会话的 OpenRouter 正文授权。",
      main_reply_private_route_required:"Hydrogen 适用于已登记私聊装配的桌面角色。",
      main_reply_retry_wait:"OpenRouter 正在限流，请在等待时间结束后重试。",
      main_reply_retry_unavailable:"此尝试已有回执或已达到重试上限，请核对当前记录。",
      native_public_task_invalid:"请选取可公开查找的片段，或跳过本次查找。",private_lookup_scope_changed:"本轮授权或原消息已变化，请重新核对。",
      private_lookup_confirmation_closed:"本轮查找范围已经处理。",
      invalid_model_connection:"请核对 HTTPS 接口与模型名。",session_privacy_operation_conflict:"授权操作已变化，请重新打开核对。",
      service_draining:"本机服务正在退出，已发送的消息会按原记录核对。",local_repair_active:"本机正在修复，稍后会自动返回聊天。",
      backup_target_must_be_new_and_independent:"请选择当前实例数据目录以外的位置。",
      backup_not_available:"没有可通过校验的备份。",backup_invalid:"所选备份未通过校验，请选择另一份。",
      backup_index_unverified:"备份登记无法核验，请保留原文件。",
      retrieved_notes_unavailable:"本轮资料或来源权限已变化，暂时无法打开。"};
    notify(bufferErrors[detail]||known[detail]||(e instanceof FormError||hostMessages.has(detail)?detail:"操作暂未完成，请稍后重试。"));
  });
  const action = (name, args={}) => api().import_action(name,args);
  const titles = {introduction:"人物介绍", core_traits:"核心特征", expression_style:"表达习惯",
    values_and_boundaries:"价值与边界", initial_background:"初始背景"};
  const jobs = {preview:"待确认归属", prepared:"已准备脱敏片段", ready:"已提炼", partial_ready:"已按部分分段预填", running:"正在提炼",
    unknown:"发送结果待核对", failed:"提炼失败", cancelled:"已取消", interrupted:"已中断", revoked:"已撤回"};
  let active = false, epoch = 0, polling = false, paintDecision = null, selectionToken = null;
  let generation=null, linkState="starting", nextCheck=null, timer=null, stopped=false, delay=750, composing=false;
  let drawnSession=null, readingTimer=null, locationWrites=Promise.resolve(), renderedTurns=new Map(), disconnectedReading=null;
  const statusLine=byId("chat-status");
  message.addEventListener("compositionstart",()=>{composing=true;});
  message.addEventListener("compositionend",()=>{composing=false;});
  function capture(){
    if(!active || !drawnSession || drawnSession!==currentSession())return null;
    if(!drawnSession.newer && !drawnSession.pastNewer && timeline.scrollHeight-timeline.clientHeight-timeline.scrollTop<=8)return {kind:"tail"};
    const top=timeline.getBoundingClientRect().top;
    const element=Array.from(timeline.querySelectorAll("[data-anchor]")).find(node=>node.getBoundingClientRect().bottom>top);
    return element?{kind:element.dataset.kind,id:element.dataset.anchor,position:Number(element.dataset.position),
      offset:element.getBoundingClientRect().top-top}:null;
  }
  function restoreReading(mark){
    if(!mark)return;
    if(mark.kind==="tail"){timeline.scrollTop=timeline.scrollHeight;return;}
    const candidates=Array.from(timeline.querySelectorAll("[data-anchor]")).filter(node=>node.dataset.kind===mark.kind);
    const exact=candidates.find(node=>node.dataset.anchor===mark.id);
    const node=exact||candidates.reduce((best,node)=>!best||Math.abs(Number(node.dataset.position)-mark.position)<
      Math.abs(Number(best.dataset.position)-mark.position)?node:best,null);
    if(node)timeline.scrollTop+=node.getBoundingClientRect().top-timeline.getBoundingClientRect().top-mark.offset;
  }
  function saveReading(){
    clearTimeout(readingTimer);
    const mark=capture(),p=currentPerson(),s=currentSession();
    if(!mark || !p || !s?.id || s.loading||linkState!=="online")return;
    locationWrites=locationWrites.then(()=>api().live_location(p.id,s.id,mark)).catch(()=>{});
  }
  timeline.addEventListener("scroll",()=>{
    clearTimeout(readingTimer);readingTimer=setTimeout(saveReading,160);
  },{passive:true});
  function stamp(node,kind,id,position){
    node.dataset.kind=kind;node.dataset.anchor=id;node.dataset.position=position;
    return node;
  }
  function setStatus(text="",action=false){
    statusLine.hidden=!text; statusLine.querySelector("span").textContent=text;
    const control=statusLine.querySelector("button");
    control.hidden=!action;
    control.textContent=action==="quota"?"查看等待任务":"更新模型连接";
    control.onclick=action==="quota"?()=>quotaPrompt(lastHealth?.quotaWaits||[],true):()=>connection();
    updateSendButton();
  }
  function pendingDraft(s){
    return s?.draftRevision && s.outbox?.some(item=>item.draft_revision===s.draftRevision &&
      (item.state==="unconfirmed" || item.state==="blocked"));
  }
  function acceptedSubmission(item){
    return ["accepted","waiting_capacity","stopped"].includes(item.state);
  }
  function newId() {
    const bytes=crypto.getRandomValues(new Uint8Array(16));
    bytes[6]=(bytes[6]&15)|64;
    bytes[8]=(bytes[8]&63)|128;
    const hex=Array.from(bytes,b=>b.toString(16).padStart(2,"0")).join("");
    return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join("-");
  }
  const presenter = newId();
  let draftWrites=Promise.resolve();
  function saveDraft(session, pid, previous){
    const update={previous,text:session.draft,revision:session.draftRevision,reference:session.reference||null};
    const write=draftWrites.then(()=>api().live_draft(pid,session.id,update));
    draftWrites=write.catch(()=>notify("草稿暂时无法保存，输入仍在当前窗口。"));
    return write;
  }
  function reviseDraft(session){
    const previous=session.draftRevision||null;
    session.draftRevision=newId();
    updateSendButton();
    return saveDraft(session,currentPerson().id,previous);
  }
  message.addEventListener("input", () => {
    const session = currentSession();
    if(active && session){
      reviseDraft(session);
    }
  });

  function el(tag, value, parent) {
    const item=document.createElement(tag);
    if(value!==null) item.textContent=value;
    if(parent) parent.append(item);
    return item;
  }
  function button(parent,label,fn) {
    const item=el("button",label,parent);
    item.type="button"; item.className="button";
    item.onclick=run(async () => {
      const owner=dialog.contains(item) ? dialogBody.firstElementChild : null;
      if(owner?.inert) return;
      const controls=owner ? [...owner.querySelectorAll("button:not(:disabled)"), ...byId("dialog-actions").querySelectorAll("button:not(:disabled)")] : [item];
      if(owner) owner.inert=true;
      for(const control of controls) control.disabled=true;
      try {await fn();}
      finally {
        if(owner) owner.inert=false;
        for(const control of controls) control.disabled=false;
      }
    });
    return item;
  }
  function panel(title) {
    const notice = byId("import-card-toast");
    if (notice) notice.hidden = true;
    present(title,"");
    const box=el("div",null,dialogBody);
    box.className="fields";
    return box;
  }
  function check(parent,label,checked=false) {
    const wrap=el("label",null,parent), input=el("input",null,wrap);
    input.type="checkbox"; input.checked=checked;
    wrap.append(document.createTextNode(" "+label)); return input;
  }
  function field(parent,label,value="",list=false,path=null) {
    const wrap=el("label",label,parent); wrap.className="field";
    const input=el("textarea",null,wrap); input.rows=2;
    if(path) input.dataset.cardPath=path;
    input.value=list?value.join("\n"):value; return input;
  }
  function readField(input,original,parse=value=>value.trim()) {
    const initial=input.value;
    return ()=>input.value===initial ? original : parse(input.value);
  }
  function lines(value) {
    return value.split("\n").map(line=>line.trim()).filter(Boolean);
  }
  function specForm(parent,spec,required=[]) {
    function control(key,title,list=false) {
      const mandatory=required.includes(key);
      const input=field(parent,title+(mandatory?"（必填）":""),spec[key],list,"spec."+key);
      if(mandatory) input.setAttribute("aria-required","true");
      return input;
    }
    const name=control("name","名称");
    const fields=Object.entries(titles).map(([key,title])=>{
      const list=["core_traits","values_and_boundaries"].includes(key);
      const input=control(key,title+(list?"（每行一项）":""),list);
      return [key,readField(input,spec[key],list?lines:undefined)];
    });
    const readName=readField(name,spec.name);
    return ()=>Object.fromEntries([["format_version",1],["name",readName()],
      ...(spec.chat_mode ? [["chat_mode",spec.chat_mode]] : []),
      ...fields.map(([key,read])=>[key,read()])]);
  }
  function inputField(box,label,type,value="") {
    const line=el("label",label,box); line.className="field";
    const control=el(type==="select"?"select":"input",null,line);
    if(type!=="select") {control.type=type;control.value=value;}
    return control;
  }
  function showRecipients(box,providers,roleplay=false) {
    const names={dialogue:"续聊主模型",jev:"场景判断（按角色设置）",memory:"记忆整理（按本次选择）"};
    if(roleplay){delete names.jev;names.memory="故事记忆整理（按本次选择）";}
    for(const [key,label] of Object.entries(names)) {
      const value=providers?.[key];
      el("p",label+" · "+(value?.name || "待配置")+(value?.base_url?" · "+value.base_url:""),box);
    }
  }
  async function importHistory() {
    const person=currentPerson();
    if(!person) return;
    const samePerson=()=>currentPerson()?.id===person.id;
    const options={personaId:person.id,targetName:person.profile.name};
    if(!api().commit_import_scope){
      const data=await api().pick_transcript();
      if(data && samePerson())setupHistory(data,options);
      return;
    }
    const scope=await api().pick_transcript(true);
    if(scope && samePerson()){
      const mode=person.profile.chat_mode || "private_chat";
      if(scope.chat_mode==="roleplay" && mode!=="roleplay")
        throw new FormError("这份 Markdown 标记为角色扮演，请选择同模式的人格接续。");
      chooseImportScope(scope, data=>{
        if(samePerson())setupHistory(data,options);
      },()=>dialog.close(),mode);
    }
  }
  function setupHistory(data,options,draft=null) {
    const boundSelf=options.targetRole ? (options.targetRole==="A"?"B":"A") : "";
    draft ??= {mapping:{...data.mapping},overrides:{...data.overrides},self:boundSelf,first:"",last:"",
               excluded:"",timezone:String(-new Date().getTimezoneOffset()),organize:false};
    const box=panel("历史聊天接续");
    el("strong",options.targetName ? "接续人物 · "+options.targetName : "从这份记录创建角色",box);
    el("p",data.format+" · "+data.preview.count+" 条记录",box);
    const names=Object.entries(data.counts);
    if(options.personaId && !options.sessionId){
      const mappingBox=groupBox(box,"说话人归属",names.length!==2);
      for(const [name,n] of names) {
        const select=inputField(mappingBox,name+" · "+n+" 条","select");
        for(const value of ["A","B","-"])select.add(new Option(value==="-"?"排除":value,value));
        select.value=draft.mapping[name];
        select.onchange=()=>{draft.mapping[name]=select.value;fillIdentity();};
      }
    }
    const self=inputField(box,"原记录中哪一方是我","select");
    self.disabled=Boolean(boundSelf);
    function fillIdentity(){
      self.replaceChildren(new Option("请选择本人",""));
      for(const role of roles){
        const labels=names.filter(([name])=>draft.mapping[name]===role).map(([name])=>name);
        self.add(new Option(role+(labels.length?" · "+labels.join("、"):""),role));
      }
      self.value=draft.self;
    }
    fillIdentity();
    const range=groupBox(box,"消息范围与时间");
    const first=inputField(range,"从消息编号","number",draft.first),last=inputField(range,"至消息编号","number",draft.last);
    first.min=last.min="1";first.max=last.max=String(data.preview.count);
    first.placeholder="1";last.placeholder=String(data.preview.count);
    const excluded=inputField(range,"排除的消息编号（用逗号或空格分隔）","text",draft.excluded);
    const tz=inputField(range,"原记录未标明时区时采用","select");
    for(let n=-720;n<=840;n+=15){
      const value=Math.abs(n), time=String(Math.floor(value/60)).padStart(2,"0")+":"+String(value%60).padStart(2,"0");
      tz.add(new Option("UTC"+(n<0?"−":"+")+time,String(n)));
    }
    tz.value=draft.timezone;
    const organize=check(box,(data.preview?.chat_mode==="roleplay"?"整理为故事记忆":"整理为长期记忆")+
      " · 按该角色的后台模型设置和额度处理",draft.organize);
    const error=el("p",null,box);error.className="error";error.setAttribute("role","alert");
    function remember(){
      Object.assign(draft,{first:first.value,last:last.value,excluded:excluded.value,timezone:tz.value,
        organize:organize.checked,self:self.value});
    }
    button(box,options.personaId && !options.sessionId?"逐条核对或修正":"查看原件与逐条归属",()=>{
      remember();return evidence(data,0,false,()=>setupHistory(data,options,draft),
        options.personaId && !options.sessionId?draft.overrides:null,true);
    });
    const previewButton=button(box,"预览接续范围",async()=>{
      remember();
      try {
        if(!draft.self) throw new FormError("请选择原记录中的本人。");
        if(options.creation && !options.creation.roles.includes(draft.self==="A"?"B":"A"))
          throw new FormError("历史要接到对方角色，请返回角色卡选择对方或同时创建两人。");
        const omit=draft.excluded.trim()?draft.excluded.trim().split(/[,，、\s]+/):[];
        if(omit.some(v=>!(/^[1-9]\d*$/.test(v)))) throw new FormError("排除项请填写有效的消息编号。");
        const preview=await action("history_preview",{ident:data.id,revision:data.revision,self_role:draft.self,
          mapping:draft.mapping,overrides:draft.overrides,first:draft.first?Number(draft.first):null,
          last:draft.last?Number(draft.last):null,excluded:omit.map(Number),timezone_offset:Number(draft.timezone),
          persona_id:options.personaId || null,organize:draft.organize,session_id:options.sessionId || null,creation:options.creation || null});
        if(box.isConnected) confirmHistory(data,options,draft,preview);
      } catch(e){error.textContent=String(e.message||e);}
    });
    self.onchange=()=>{draft.self=self.value;previewButton.disabled=!self.value;};
    previewButton.disabled=!self.value;
    if(options.back) button(box,"返回角色卡",options.back);
  }
  function confirmHistory(data,options,draft,preview){
    const box=panel("确认历史接续");
    const peer=draft.self==="A"?"B":"A";
    const labels=role=>[role,...(preview.names[role] || [])].join(" · ");
    el("p","本人："+labels(draft.self)+"　对方："+labels(peer),box);
    el("p","接续人物："+(options.targetName || options.cards?.[peer].spec.name || labels(peer)),box);
    el("p","所选范围 · "+preview.total+" 条",box);
    for(const row of preview.preview_rows || []) {
      el("small","#"+row.id+" · "+row.speaker+" · "+(row.date || "时间待核"),box);
      el("p",row.text,box).className="import-original";
    }
    const receivers=groupBox(box,"使用这段历史的模型",true);
    showRecipients(receivers,preview.providers,preview.chat_mode==="roleplay");
    el("p",draft.organize?"本次历史将按该角色的后台设置整理。":"本次历史用于续聊和查档，暂不做长期记忆整理。",box);
    if(options.personaId && !options.sessionId && !preview.reused)
      el("p","已有角色会开启独立会话，当前会话按正常流程封存。重复导入可打开原会话。",box);
    const consent=check(box,"确认本人、对方和消息范围，允许上述模型按所选用途使用原话");
    const operation=newId(), error=el("p",null,box);error.className="error";error.setAttribute("role","alert");
    const commit=button(box,"保存并打开接续会话",async()=>{
      if(!consent.checked)return;
      try {
        const result=await action("history_commit",{preview,confirmed:true,operation_id:operation,creation:options.creation || null});
        await reload(result.persona_id,true,result.session_id);
        if(options.targetRole) window.pcarMobile?.chat();
        if(result.reused) notify("已打开先前导入的会话。");
      } catch(e){error.textContent=String(e.message||e);}
    });
    commit.classList.add("primary");commit.disabled=true;
    consent.onchange=()=>{commit.disabled=!consent.checked;};
    button(box,"返回调整范围",()=>setupHistory(data,options,draft));
  }
  async function home() {
    const box=panel("新建角色");
    if(api().pick_deepseek_export)button(box,"从 DeepSeek 创建",deepseekHome);
    button(box,"从聊天记录生成",async()=>{
      if(!api().commit_import_scope){
        const data=await api().pick_transcript();
        if(data)await openImport(data);
        return;
      }
      const scope=await api().pick_transcript(true);
      if(scope)chooseImportScope(scope,openImport,home);
    });
    button(box,"导入已有角色包",async()=>{
      const pack=await api().live_package();if(!pack)return;
      const form=panel("核对并导入角色包"),read=specForm(form,pack.persona_spec),request=newId();
      button(form,"保存为新角色",async()=>{pack.persona_spec=read();const p=await api().live_save_package(pack,request);
        dialog.close();await reload(p.id);});
    });
    button(box,"空白创建",()=>{
      const form=panel("空白创建"), read=specForm(form,{name:"",introduction:"",core_traits:[],
        expression_style:"",values_and_boundaries:[],initial_background:""});
      button(form,"创建并选用",async()=>{const result=await api().live_create(read()); dialog.close();await reload(result.id);});
    });
    const templates=el("div",null,box); templates.className="import-template-row";
    const status=el("span",null,templates); status.setAttribute("role","status");
    function showTemplate(value) {
      const sourceNames={builtin:"内置",file:"已导入"};
      status.textContent=value.active ? value.label+" · "+sourceNames[value.source] : value.message;
      status.title=value.active ? [value.version,value.model,value.sha256].filter(Boolean).join(" · ") : value.message;
    }
    const error=el("small",null,box); error.className="error"; error.hidden=true; error.setAttribute("role","alert");
    button(templates,"导入提示词文件",async()=>{
      error.hidden=true;
      try {
        const result=await api().import_prompt_file();
        if(result && box.isConnected) showTemplate(result);
      } catch(e) {error.textContent=String(e.message || e);error.hidden=false;}
    });
    const [docs,template]=await Promise.all([action("list"),action("prompt_status")]);
    if(!box.isConnected)return;
    if(!status.textContent) showTemplate(template);
    for(const d of docs)button(box,d.format+" · "+d.created.slice(0,16)+" · "+(jobs[d.status]||d.status),
      async()=>openImport(await action("status",{ident:d.id})));
  }

  function keyPrompt(){
    const box=keyPanel=panel("请先填写并验证 DeepSeek API Key");
    button(box,"前往填写",keySetup);
    button(box,"退出 PCAR",()=>api().exit_workbench());
  }

  function keySetup(){
    const box=keyPanel=panel("验证 DeepSeek API Key");
    const label=el("label","官方 DeepSeek API Key",box);
    const input=el("input",null,label);
    input.type="password";input.autocomplete="off";input.setAttribute("data-autofocus","");
    const detail=el("p",null,box);detail.setAttribute("role","status");
    const reasons={invalid_key:"官方鉴权未通过，请核对 Key。",proxy_required:"请先开启已配置的 Windows 系统代理。",
      network_error:"暂时无法验证 Key，请检查连接后重试。",connection_changed:"本机连接已变化，请重新核对。",
      missing:"请填写 DeepSeek API Key。"};
    const save=button(box,"验证并保存",()=>verify(input.value.trim()));
    const existing=activation?.has_key?button(box,"验证已有 Key",()=>verify("")):null;
    button(box,"前往 DeepSeek 创建 Key",()=>api().open_deepseek_site(true));
    if(activation?.has_key)button(box,"清除 Key",()=>{
      const review=keyPanel=panel("清除 DeepSeek API Key");
      el("p","清除后，业务功能将锁定，直至验证新 Key。",review);
      button(review,"确认清除",async()=>{activation=await api().clear_deepseek_key();dialog.close();arm(0);});
      button(review,"返回",keySetup);
    });
    async function verify(value,confirm=null){
      if(!value&&!activation?.has_key){detail.textContent=reasons.missing;return;}
      save.disabled=true;if(existing)existing.disabled=true;
      detail.textContent="正在向 DeepSeek 验证。";
      try{
        const result=await api().verify_deepseek_key(value,confirm);
        if(result.state==="confirm"){
          const review=keyPanel=panel("确认更新 DeepSeek 连接");
          el("p",result.affected?"此变更影响 "+result.affected+" 个已有会话的接收方授权。":"此变更会更新基础模型的接收方。",review);
          button(review,"确认并验证",()=>verify(value,result.confirmation));
          button(review,"返回",keySetup);
          return;
        }
        if(result.state==="verified"){
          input.value="";activation=result;dialog.close();
          notify(result.balance_available?"DeepSeek 鉴权通过。":"DeepSeek 鉴权通过。当前账户余额不足。");
          arm(0);return;
        }
        if(!box.isConnected){
          const retry=keyPanel=panel("DeepSeek 鉴权");
          el("p",reasons[result.state]||reasons.network_error,retry);
          button(retry,"重新填写",keySetup);
        }else detail.textContent=reasons[result.state]||reasons.network_error;
      }catch(e){
        const retry=keyPanel=panel("DeepSeek 鉴权未完成");
        el("p","请检查本机服务与连接，再次验证。",retry);
        button(retry,"返回填写",keySetup);
      }finally{save.disabled=false;if(existing)existing.disabled=false;}
    }
    input.focus();
  }

  let exportWaitingPanel=null;
  async function manualDeepSeekZip(parent){
    try{
      const picked=await api().pick_deepseek_export();
      if(picked){
        if(api().stop_deepseek_export)await api().stop_deepseek_export();
        await chooseDeepSeek(picked);
      }
    }catch(e){el("p",String(e.message||e),parent);}
  }

  function deepseekGuide(parent){
    const guide=el("details",null,parent);
    el("summary","DeepSeek 导出图文教程",guide);
    const steps=el("div",null,guide);steps.className="deepseek-guide";
    function figure(title,description){
      const figure=el("figure",null,steps);
      const diagram=el("div",null,figure);diagram.className="deepseek-diagram";
      diagram.setAttribute("aria-hidden","true");
      const caption=el("figcaption",null,figure);
      el("strong",title,caption);
      el("p",description,caption);
      return diagram;
    }
    function token(diagram, label, style=""){
      const item=el("span",label,diagram);
      item.className="deepseek-guide-token"+(style?" "+style:"");
    }
    function arrow(diagram){el("span","›",diagram).className="deepseek-guide-arrow";}
    const settings=figure("在官网找到数据导出","在浏览器登录 DeepSeek。点侧栏的账号头像，进入「设置 → 数据管理」。");
    el("span","",settings).className="deepseek-guide-avatar";
    arrow(settings);token(settings,"设置");arrow(settings);token(settings,"数据管理","highlight");
    const download=figure("生成链接，再下载 ZIP","在数据导出中点「生成下载链接」。链接就绪后点官网下载，等浏览器显示下载完成。");
    token(download,"生成下载链接");arrow(download);token(download,"下载 ZIP","highlight");
    const select=figure("回 PCAR 选择一段对话","收到提示后选要导入的对话。也可点「选择已下载的 ZIP」，直接打开官网给你的压缩包。");
    token(select,"ZIP","archive");arrow(select);token(select,"PCAR · 选择对话","highlight");
    return guide;
  }

  function showDeepSeekWaiting(folder){
    const box=exportWaitingPanel=panel("等待 DeepSeek 导出");
    el("p","请在浏览器登录 DeepSeek，在设置里生成下载链接。链接就绪后点击官网下载。PCAR 会自动识别下载完成的 ZIP。",box);
    el("small",folder,box);
    el("p","等待期间可以关闭这个提示，下载完成后会从工作台提示。",box);
    deepseekGuide(box);
    button(box,"选择已下载的 ZIP",()=>manualDeepSeekZip(box));
    button(box,"停止等待",async()=>{await api().stop_deepseek_export();await deepseekHome();});
  }

  async function deepseekHome(){
    const box=panel("从 DeepSeek 创建");
    el("p","在系统浏览器完成登录和导出。PCAR 会识别下载好的官方 ZIP，再让你选择一段对话。",box);
    async function openBrowser(folder){
      try{
        const ready=await api().start_deepseek_export(folder);
        if(ready)showDeepSeekWaiting(ready);
      }catch(e){el("p",String(e.message||e),box);}
    }
    if(api().start_deepseek_export){
      button(box,"打开 DeepSeek 并等待下载",()=>openBrowser(false));
      button(box,"选择下载文件夹并打开官网",()=>openBrowser(true));
    }
    button(box,"选择已下载的 ZIP",()=>manualDeepSeekZip(box));
    deepseekGuide(box).open=true;
    const pending=await api().deepseek_pending();
    if(pending&&box.isConnected)button(box,"继续选择对话",()=>openDeepSeekDelivery(pending.id,true));
    else if(api().deepseek_export_waiting){
      const folder=await api().deepseek_export_waiting();
      if(folder&&box.isConnected)button(box,"查看下载等待进度",()=>showDeepSeekWaiting(folder));
    }
  }

  async function openDeepSeekDelivery(job,chooseNow=false){
    seenIntake=job;
    try{
      const data=await api().open_deepseek_pending(job);
      if(chooseNow)return chooseDeepSeek(data,job);
      const box=panel("已收到 DeepSeek 导出");
      el("p","已提取会话目录，共 "+data.conversations.length+" 段。可以现在选择，也可以稍后从「从 DeepSeek 创建」继续。",box);
      button(box,"选择一段对话",()=>chooseDeepSeek(data,job));
      button(box,"稍后",()=>dialog.close());
    }catch(e){
      const box=panel("浏览器导入待处理");
      el("p",String(e.message||e),box);
      button(box,"重新选择 ZIP",deepseekHome);
      button(box,"取消此次交接",async()=>{await api().discard_deepseek_pending(job);dialog.close();});
    }
  }

  async function chooseDeepSeek(data,job=null){
    const box=panel("选择一段 DeepSeek 对话");
    el("p","每次提取一段对话。选中后即可审阅其 Markdown 正文。",box);
    if(!data.conversations.length)el("p","这个导出包中没有会话。",box);
    const search=el("input",null,box);
    search.type="search";search.placeholder="搜索对话标题或编号";
    search.setAttribute("aria-label","搜索对话标题或编号");
    const list=el("div",null,box);
    let page=0;
    function show(){
      list.replaceChildren();
      const query=search.value.trim().toLowerCase();
      const matches=data.conversations.filter(item=>item.title.toLowerCase().includes(query)||item.id.toLowerCase().includes(query));
      el("p",matches.length+" 个会话",list);
      for(const item of matches.slice(page*50,page*50+50)){
        if(item.updated || item.count!==null)el("small",[item.updated,item.count===null?null:item.count+" 条"].filter(Boolean).join(" · "),list);
        button(list,item.title,()=>importDeepSeek(item.id,null,data,job));
      }
      if(page)button(list,"上一页",()=>{page--;show();});
      if((page+1)*50<matches.length)button(list,"下一页",()=>{page++;show();});
    }
    search.oninput=()=>{page=0;show();};show();
    if(job)button(box,"取消此次交接",async()=>{await api().discard_deepseek_pending(job);dialog.close();});
  }

  async function importDeepSeek(ident,branch,data,job){
    try{
      const result=await api().import_deepseek_session(ident,branch);
      if(result.scope)return chooseImportScope(result.scope,reviewDeepSeek,()=>chooseDeepSeek(data,job));
      if(result.document)return reviewDeepSeek(result.document);
      if(result.branches){
        const box=panel("选择可还原的分支");
        el("p",result.conversation.title,box);
        const choice=el("select",null,box);
        choice.setAttribute("aria-label","选择 DeepSeek 分支");
        for(const item of result.branches)choice.add(new Option([item.date,"分支 "+item.id].filter(Boolean).join(" · "),item.id));
        button(box,"导入所选分支",()=>importDeepSeek(ident,Number(choice.value),data,job));
        button(box,"返回会话",()=>chooseDeepSeek({...data,target:null},job));
        return;
      }
      return chooseDeepSeek({...data,target:ident,conversations:data.conversations.filter(item=>item.id!==ident)},job);
    }catch(e){
      const box=panel("DeepSeek 导入未完成");
      el("p",String(e.message||e),box);
      button(box,"返回会话",()=>chooseDeepSeek({...data,target:null},job));
    }
  }

  function chooseImportScope(source, accept, back, fixedMode=null){
    const box=panel("选择聊天模式和导入数量");
    box.classList.add("import-range");
    if(source.title)el("strong",source.title,box);
    el("p","原记录共 "+source.total.toLocaleString("zh-CN")+" 条消息。按原顺序导入最近所选条数。",box);
    const mode=inputField(box,"聊天模式","select");
    mode.add(new Option("私人聊天模式","private_chat"));
    mode.add(new Option("角色扮演模式","roleplay"));
    mode.value=fixedMode || source.chat_mode;
    mode.disabled=Boolean(fixedMode);
    const description=el("p",null,box);
    const range=inputField(box,"导入数量","select");
    for(const count of [50,100,200,500,1000]){
      const option=new Option("导入 "+count+" 条消息",String(count));
      option.disabled=count>source.total;
      range.add(option);
    }
    range.add(new Option("导入全部消息","all"));
    range.add(new Option("自定义导入数量","custom"));
    range.value=source.total>500 ? "500" : "all";
    const custom=inputField(box,"最近消息条数","number",String(Math.min(500,source.total)));
    custom.min="1";custom.max=String(source.total);custom.step="1";
    const preview=el("p",null,box);
    preview.setAttribute("aria-live","polite");
    if(source.title)el("small","建卡范围需要包含用户和 DeepSeek 双方的消息。",box);
    if(source.total>5000){
      const warning=el("p","原记录超过 5000 条。整理较长历史会增加建卡用时和模型费用。",box);
      warning.className="import-range-warning";warning.setAttribute("role","status");
    }
    const consent=check(box,"我接受本次超过 5000 条消息的建卡用时和费用");
    const consentLine=consent.parentElement;consentLine.className="import-range-warning";
    const apply=button(box,"确认导入",async()=>{
      const count=chosenCount();
      if(!valid(count) || (count>5000&&!consent.checked))return;
      apply.disabled=true;
      try{
        const data=await api().commit_import_scope(source.token,count,mode.value,consent.checked);
        if(box.isConnected)await accept(data);
      }finally{if(box.isConnected)refresh();}
    });
    button(box,"返回",back);
    function chosenCount(){
      if(range.value==="all")return source.total;
      if(range.value==="custom")return custom.valueAsNumber;
      return Number(range.value);
    }
    function valid(count){return Number.isInteger(count)&&count>=1&&count<=source.total;}
    function refresh(){
      const count=chosenCount();
      custom.parentElement.hidden=range.value!=="custom";
      consentLine.hidden=!(count>5000);
      const okay=valid(count);
      preview.textContent=okay ? "将导入 "+count.toLocaleString("zh-CN")+" 条消息。" : "请输入有效的导入条数。";
      description.textContent=mode.value==="roleplay" ?
        "用于 AI 文游。保留动作、旁白和角色心理，将剧情记在这个人格的故事世界里。" :
        "用于模拟即时通讯私聊。按真实交流整理人物与记忆。";
      apply.disabled=!okay || (count>5000&&!consent.checked);
    }
    mode.onchange=refresh;
    consent.onchange=refresh;
    range.onchange=custom.oninput=()=>{consent.checked=false;refresh();};
    refresh();
  }

  async function reviewDeepSeek(document,start=0,edits={}){
    const box=panel("审阅 DeepSeek Markdown");
    el("p",document.deepseek?.title||"DeepSeek 对话",box);
    el("p","可修改每条消息的 Markdown 正文。删除正文会排除该条。原始导出保留供核对。",box);
    const status=el("p",null,box);
    status.setAttribute("role","status");
    const data=await action("evidence",{ident:document.id,start});
    if(!box.isConnected)return;
    if(data.total)el("small","第 "+(start+1)+"–"+Math.min(start+10,data.total)+" 条，共 "+data.total+" 条",box);
    for(const row of data.rows.slice(0,10)){
      const label=el("label",[row.speaker,row.date].filter(Boolean).join(" · "),box);
      label.className="field";
      const input=el("textarea",null,label);
      input.rows=4;
      input.value=edits[row.id]??row.original;
      input.oninput=()=>{
        if(input.value===row.original)delete edits[row.id];
        else edits[row.id]=input.value;
      };
    }
    if(start>0)button(box,"上一页",()=>reviewDeepSeek(document,Math.max(start-10,0),edits));
    if(start+10<data.total)button(box,"下一页",()=>reviewDeepSeek(document,start+10,edits));
    if(data.total>10){
      const jump=el("input",null,box);
      jump.type="number";jump.min=1;jump.max=data.total;jump.placeholder="消息编号";
      jump.setAttribute("aria-label","跳到消息编号");
      button(box,"跳转",()=>{
        const position=Number(jump.value);
        if(Number.isInteger(position)&&position>=1&&position<=data.total)
          return reviewDeepSeek(document,Math.floor((position-1)/10)*10,edits);
      });
    }
    const commit=()=>Object.keys(edits).length?api().revise_deepseek_markdown(document.id,edits):document;
    button(box,"另存审阅版 Markdown",async()=>{
      try{const revised=await commit();if(await api().save_deepseek_markdown(revised.id))status.textContent="已保存审阅版。";}
      catch(e){status.textContent=String(e.message||e);}
    });
    button(box,"确认内容并继续建卡",async()=>{
      try{await openImport(await commit());}
      catch(e){status.textContent=String(e.message||e);}
    });
  }

  async function evidence(data,start,prepared,back,overrides=null,showSections=false) {
    const p=await action("evidence",{ident:data.id,start,prepared,...(showSections ? {sections:true} : {})});
    const box=panel(prepared?"将用于分析的脱敏片段":"文件原话与消息归属");
    button(box,"返回",back);
    if (!prepared && p.total > 50) {
      const jump = el("label","跳至消息编号",box);
      jump.className = "field";
      const number = el("input",null,jump);
      number.type = "number"; number.min = "1"; number.max = String(p.total); number.value = String(start+1);
      button(box,"定位消息",()=>{
        const id = number.valueAsNumber;
        if (!Number.isInteger(id) || id < 1 || id > p.total) throw new FormError("请输入文件中的消息编号。");
        return evidence(data,id-1,prepared,back,overrides,showSections);
      });
    }
    for(const r of p.rows){
      const label=r.speaker+" · #"+r.id+(r.date?" · "+r.date:"");
      el("strong",label,box);
      const body=el("p",prepared?r.text:r.original,box);body.className="import-original";
      if(showSections && r.sections?.length){
        const sectionNames={body:"说话人正文",quote:"引用",forward:"转发",system:"系统",attachment:"附件",code:"代码"};
        const parts=Array.from(r.original);
        body.replaceChildren();
        for(const part of r.sections){
          el("small",sectionNames[part.kind] || part.kind,body).className="import-section-label";
          body.append(document.createTextNode(parts.slice(part.start-r.start,part.end-r.start).join("")));
        }
      }
      if(overrides && Object.hasOwn(data.mapping,r.speaker)){
        const sel=el("select",null,box);
        for(const v of ["","A","B","-"])sel.add(new Option(v===""?"沿用说话人归属":v==="-"?"排除":v,v));
        sel.value=overrides[r.id]||"";
        sel.onchange=()=>{if(sel.value) overrides[r.id]=sel.value; else delete overrides[r.id];};
      }
    }
    if(start>0)button(box,"上一页",()=>evidence(data,Math.max(0,start-50),prepared,back,overrides,showSections));
    if(start+50<p.total)button(box,"下一页",()=>evidence(data,start+50,prepared,back,overrides,showSections));
  }
  function mapping(data, map={...data.mapping}, overrides={...data.overrides}) {
    const box=panel("确认两位说话人");
    const range=data.preview||{};
    if(range.chat_mode==="roleplay")el("p","角色扮演模式 · 剧情将随人格保存。",box);
    el("p",[data.format,range.count && range.count+" 条记录",range.from,range.to].filter(Boolean).join(" · "),box);
    if(data.format==="DeepSeek Markdown"){
      if(data.deepseek)el("p",[data.deepseek.title,"分支 "+data.deepseek.branch,data.deepseek.from,data.deepseek.to].filter(Boolean).join(" · "),box);
      button(box,"另存 Markdown",()=>api().save_deepseek_markdown(data.id));
    }
    const speakers = Object.entries(data.counts);
    if (speakers.length === 1)
      el("p","文件中只有一个昵称。同名的两位说话人可在逐条核对时指定归属，缺少一方时请补充记录。",box);
    for(const [speaker,count] of speakers){
      const label=el("label",speaker+"（"+count+" 条）",box), sel=el("select",null,label);
      for(const value of ["A","B","-"])sel.add(new Option(value==="-"?"排除":value,value));
      sel.value=map[speaker]; sel.onchange=()=>{map[speaker]=sel.value; checkMapping();};
    }
    button(box,"逐条核对或修正",()=>evidence(data,0,false,()=>mapping(data,map,overrides),overrides,true));
    const message = el("small",null,box);
    const generate = button(box,"确认说话人并继续",async()=>openImport(await action("plan",{
      ident:data.id,revision:data.revision,mapping:map,overrides})));
    function checkMapping() {
      const assigned = new Set([...Object.values(map),...Object.values(overrides)]);
      generate.disabled = !assigned.has("A") || !assigned.has("B");
      message.hidden = !generate.disabled;
      message.textContent = generate.disabled ? "需要给 A、B 各指定至少一条有效发言。" : "";
    }
    checkMapping();
  }

  async function openImport(data,role=data.format==="DeepSeek Markdown" && data.mapping.DeepSeek==="B"?"B":"A") {
    if(data.status==="preview"){mapping(data);return;}
    if(data.plan){return openPlanned(data,role);}
    const box=panel("聊天人设草稿");
    el("p",data.format+" · "+(jobs[data.status]||data.status),box);
    if(data.status==="revoked"){button(box,"返回",home);return;}
    if(data.status==="running"){
      button(box,"取消提炼",async()=>openImport(await action("cancel",{ident:data.id})));
      setTimeout(run(async()=>{if(box.isConnected)await openImport(await action("status",{ident:data.id}),role);}),900);
      return;
    }
    const again=async()=>openImport(await action("status",{ident:data.id}),role);
    button(box,"查看脱敏范围",()=>evidence(data,0,true,again));
    if(data.status!=="ready"){
      button(box,"用已收到的回执预填",async()=>openImport(await action("replay",{ident:data.id,revision:data.revision})));
      const consent=check(box,"允许官方 DeepSeek 分析此文件中预览的双方脱敏发言，调用可能产生费用");
      button(box,"分析两位人物",async()=>{if(!consent.checked)throw new FormError("请选择本次分析用途。");
        await openImport(await action("analyze",{ident:data.id,revision:data.revision,confirmed:true}));});
    }
    const draft=data.draft[role];
    if(!draft)return;
    const line=el("div",null,box);line.className="inline-actions";
    const other=role==="A"?"B":"A";
    el("h3",role+" · "+draft.spec.name,box);
    if(draft.coverage)el("p","已采用 "+draft.coverage.received+" / "+draft.coverage.eligible+" 个分段。",box);
    const read=specForm(box,draft.spec), relation=field(box,"关系说明",draft.relationship);
    const saveDraft=()=>{
      const spec=read(),prior=data.draft[role];
      const same=Object.keys(spec).every(k=>JSON.stringify(spec[k])===JSON.stringify(prior.spec[k]));
      return same && relation.value===prior.relationship?Promise.resolve(data):
        action("edit",{ident:data.id,revision:data.revision,role,spec,relationship:relation.value});
    };
    button(line,"暂存并查看 "+other,async()=>openImport(await saveDraft(),other));
    button(line,"暂存草稿",async()=>openImport(await saveDraft(),role));
    const proof=el("details",null,box);el("summary","提炼依据与表达样例",proof);
    const proofPage=el("div",null,proof);
    function showProof(start=0){
      proofPage.replaceChildren();
      const total=draft.claims.length+draft.samples.length;
      for(let i=start;i<Math.min(start+25,total);i++){
        if(i<draft.claims.length){
          const claim=draft.claims[i];
          el("p",(titles[claim.field]||claim.field)+(claim.needs_review?"（待核）":"")+" · "+claim.value,proofPage);
          for(const ref of claim.evidence)button(proofPage,"#"+ref.id+" "+ref.quote,()=>evidence(data,Number(ref.id)-1,false,again));
        }else{
          const sample=draft.samples[i-draft.claims.length];
          button(proofPage,"#"+sample.id+" "+sample.text,()=>evidence(data,Number(sample.id)-1,false,again));
        }
      }
      if(start>0)button(proofPage,"上一页",()=>showProof(start-25));
      if(start+25<total)button(proofPage,"下一页",()=>showProof(start+25));
    }
    showProof();
    button(box,"用现有通用模板补齐空项",async()=>{
      const spec=read(),defaults=await api().template_spec();
      for(const key of Object.keys(titles))if(!spec[key]?.length)spec[key]=defaults[key];
      await openImport(await action("edit",{ident:data.id,revision:data.revision,role,spec,relationship:relation.value}),role);
    });
    const both=check(box,"同时创建 A 和 B"),style=check(box,"允许把本角色的脱敏原话用于日常发言");
    button(box,"确认创建",async()=>{
      data=await saveDraft();
      const roles=both.checked?["A","B"]:[role];
      const operation=[data.id,data.revision,roles.join(""),Number(style.checked),0].join(".");
      const ids=await action("create",{ident:data.id,revision:data.revision,roles,
        enable_style:style.checked,continue_history:false,operation_id:operation});
      await reload(ids[both.checked && role==="B"?1:0]);
    });
    button(box,"使用新版分批提炼",async()=>mapping(await saveDraft()));
    button(box,"撤回该文件的提炼与风格使用",async()=>{if(confirm("停止该来源的提炼与表达取样？已确认还原的历史可在会话中查看。"))
      await openImport(await action("revoke",{ident:data.id}));});
  }

  const roles = ["A", "B"];
  const batchStates = {planned:"已计划", accepted:"已验收", partial:"部分观察待核", rejected:"已隔离", unknown:"发送结果待核对",
    unsent:"尚未派发", awaiting_approval:"等待请求审定", pending:"等待处理", sending:"正在派发", received:"已收到回执"};
  function batchState(state) { return batchStates[state] || state || "已计划"; }
  function nameOf(data, role) { return role + " · " + data.draft[role].spec.name; }

  function showCoverage(box, data) {
    const plan = data.plan;
    const sectionList = el("details",null,box);
    sectionList.open = data.status === "running" || data.status === "prepared";
    el("summary","分析覆盖 · " + roles.map(r=>r+" "+plan.roles[r].selected+"/"+plan.roles[r].eligible+" 批").join("、"),sectionList);
    for (const role of roles) {
      const info = plan.roles[role];
      const counts = new Map();
      for (const batch of info.batches) {
        const name = batchState(batch.state);
        counts.set(name, (counts.get(name) || 0) + 1);
      }
      const section = el("div", null, sectionList);
      el("strong", nameOf(data,role), section);
      el("p", "学习范围 " + info.messages + " 条 · 选中 " + info.selected + " / " + info.eligible
        + " 批" + (info.selected_messages == null ? "" : " · 本批范围共 " + info.selected_messages + " 条本人发言"), section);
      const dates = [info.first_date,info.last_date].filter(Boolean);
      if (dates.length) el("small", dates.join(" 至 "), section);
      if (counts.size) el("p", Array.from(counts, ([name,n]) => name + " " + n).join(" · "), section);
      if (info.merges?.length) el("p","汇总 · " + info.merges.map(m=>batchState(m.state)).join("、"),section);
      const reasons = {ref_outside_target_core:"引用超出本人的主片段",ref_quote:"引文与原话不符",
        ref_span:"引文区间无效",ref_source_span:"无法映射回原文件",foreign_observation:"引用其他人物或字段的观察",
        conflicting_sources:"支持与反例冲突",missing_support:"缺少支持证据",identity:"回执身份或批次不符",
        envelope:"回执结构无效",missing_template:"汇总配置待审定"};
      for (const [stage,batches] of [["提取",info.batches],["汇总",info.merges || []]]) {
        for (const batch of batches.filter(b=>b.rejected_count)) {
          const codes = batch.reasons.map(code=>reasons[code] || code).join("、");
          el("small",stage + "批次 " + batch.id.slice(0,8) + " · 隔离 " + batch.rejected_count + " 项 · " + codes,section)
            .className = "import-section-label";
        }
      }
    }
    const budget=Number.isInteger(plan.max_tokens) ? "每次最多输出 "+plan.max_tokens.toLocaleString("zh-CN")+" tokens。" : "";
    const notice=plan.prompt_release ? "跨段观察按需汇总，最后最多调用一次生成两张角色卡。" : "汇总与成卡按实例的用途授权执行，成卡最多一次。";
    el("p","计划分段提取 "+plan.requests+" 次。"+notice+budget+"官方 DeepSeek 按实际用量计费。",box);
  }

  async function batchEvidence(data, role, batch, back, start=0) {
    const page = await action("plan_evidence", {ident:data.id, plan_id:data.plan.id, role, batch_id:batch.id, start});
    const box = panel("本批待发的脱敏片段");
    button(box,"返回分析范围", back);
    for (const row of page.rows) {
      const isSupport = row.kind === "core" && row.speaker === role;
      el("strong", (isSupport ? "本人主片段" : "对话语境") + " · " + row.speaker + " · #" + row.id, box);
      el("small", "字符 [" + row.start + ", " + row.end + ")" + (row.date ? " · " + row.date : ""), box);
      el("p",row.text,box).className = "import-original";
    }
    const size = page.rows.length;
    if (start > 0) button(box,"上一页",()=>batchEvidence(data,role,batch,back,Math.max(0,start-50)));
    if (size && start + size < page.total) button(box,"下一页",()=>batchEvidence(data,role,batch,back,start+size));
  }

  function planScope(data, tab, back, selected, start=0) {
    const plan = data.plan, box = panel("核对分析范围");
    const choices = plan.choices;
    if (!selected) {
      selected = Object.fromEntries(roles.map(r => [r,new Set(choices[r].filter(b=>b.selected).map(b=>b.id))]));
    }
    button(box,"返回草稿",back);
    const nav = el("div",null,box); nav.className = "inline-actions";
    for (const r of roles) {
      const control = button(nav,nameOf(data,r),()=>planScope(data,r,back,selected));
      control.setAttribute("aria-pressed",String(r === tab));
    }
    const info = plan.roles[tab];
    el("p","共 " + info.messages + " 条本人学习材料 · " + choices[tab].length + " 个可选批次",box);
    const page = choices[tab].slice(start,start+20);
    const editable = data.status !== "running";
    for (const batch of page) {
      const cell = el("div",null,box); cell.className = "import-batch";
      const toggle = check(cell, "第 " + batch.ordinal + " 批 · " + batch.messages + " 条本人发言", selected[tab].has(batch.id));
      toggle.disabled = !editable;
      toggle.onchange = () => {
        if (toggle.checked && selected[tab].size >= 6) {
          toggle.checked = false;
          notify("每位人物每份计划最多选择六批。");
          return;
        }
        if (toggle.checked) selected[tab].add(batch.id); else selected[tab].delete(batch.id);
        changed();
      };
      const dates = [batch.first_date, batch.last_date].filter(Boolean);
      if (dates.length) el("small",dates.join(" 至 "),cell);
      if (batch.selected) button(cell,"查看第 " + batch.ordinal + " 批待发内容",
        ()=>batchEvidence(data,tab,batch,()=>planScope(data,tab,back,selected,start)));
    }
    const pager = el("div",null,box); pager.className = "inline-actions";
    if (start) button(pager,"上一页",()=>planScope(data,tab,back,selected,Math.max(0,start-20)));
    if (start + page.length < choices[tab].length) button(pager,"下一页",()=>planScope(data,tab,back,selected,start+page.length));
    const label = el("p",null,box);
    const apply = editable ? button(box,"按所选范围生成新计划",async()=>{
      const selection = Object.fromEntries(roles.map(r=>[r,choices[r].filter(b=>selected[r].has(b.id)).map(b=>b.id)]));
      const next = await action("plan",{ident:data.id,revision:data.revision,mapping:data.mapping,overrides:data.overrides,selection});
      await openImport(next,tab);
    }) : null;
    function changed() {
      label.textContent = "已勾选 A " + selected.A.size + " 批、B " + selected.B.size + " 批";
      if (apply) apply.disabled = !(selected.A.size + selected.B.size) ||
        !roles.some(r=>choices[r].some(b=>Boolean(b.selected) !== selected[r].has(b.id)));
    }
    changed();
  }

  async function claimSource(data, role, ref, back, backLabel="返回人设建议") {
    const pos = Number(ref.id);
    if (!Number.isSafeInteger(pos) || pos < 1) throw new FormError("原文位置无效。");
    const result = await action("evidence",{ident:data.id,start:pos-1,prepared:false,sections:true});
    const raw = result.rows.find(row=>String(row.id) === String(ref.id));
    if (!raw) throw new FormError("此项证据无法定位到原文件。");
    const box = panel("核对原话");
    button(box,backLabel,back);
    el("strong",(ref.speaker || role) + " · #" + ref.id + (raw.date ? " · " + raw.date : ""),box);
    if (ref.quote) {
      const hasRange = Number.isInteger(ref.start) && Number.isInteger(ref.end);
      el("small","已核验的脱敏引文" + (hasRange ? " · 字符 [" + ref.start + ", " + ref.end + ")" : ""),box);
      el("p",ref.quote,box).className = "import-original";
    }
    el("strong","文件原文 · " + raw.speaker,box);
    const body = el("p",null,box); body.className = "import-original";
    const chars = Array.from(raw.original), from = ref.raw_start-raw.start, to = ref.raw_end-raw.start;
    if (Number.isInteger(from) && Number.isInteger(to) && from >= 0 && from < to && to <= chars.length) {
      body.append(document.createTextNode(chars.slice(0,from).join("")));
      el("mark",chars.slice(from,to).join(""),body);
      body.append(document.createTextNode(chars.slice(to).join("")));
    } else body.textContent = raw.original;
  }

  function suggestions(box, data, role, save, readonly=false) {
    const claims = data.draft[role].claims;
    const wrap = el("div",null,box); wrap.className = "fields";
    const decisionNames = {accept:"已采纳", reject:"已舍弃"};
    function drawPage(start) {
      wrap.replaceChildren();
      el("h3","人设建议 · " + claims.length + " 项",wrap);
      for (const claim of claims.slice(start,start+12)) {
        const item = el("details",null,wrap);
        const pending = claim.needs_review ? (readonly ? "待核" : "冲突待核") : "待采纳";
        const status = decisionNames[claim.decision] || pending;
        el("summary",(titles[claim.field] || claim.field) + " · " + status + " · " + claim.value,item);
        if (claim.scope) el("p","适用情境 · " + claim.scope,item);
        const refs = el("div",null,item); refs.className = "fields";
        if (claim.support_groups) el("small","来自 " + claim.support_groups.length + " 个去重组",refs);
        for (const [label,evidence] of [["依据",claim.evidence],["反例",claim.counter_evidence || []]]) {
          for (const ref of evidence) {
            button(refs,label + " · " + (ref.speaker || role) + " · #" + ref.id + " · " + ref.quote,async()=>{
              const current = readonly ? data : await save();
              await claimSource(current,role,ref,()=>readonly ? archiveView(current,role) : openImport(current,role));
            }).classList.add("import-citation");
          }
        }
        if (readonly) continue;
        const actions = el("div",null,item); actions.className = "inline-actions";
        for (const [decision,label] of [["accept","采纳"],["reject","舍弃"]]) {
          if (claim.decision === decision || (decision === "accept" && claim.needs_review)) continue;
          button(actions,label,async()=>{
            const current = await save();
            const updated = await action("review",{ident:current.id,revision:current.revision,
              plan_id:current.plan.id,role,suggestion_id:claim.id,decision});
            await openImport(updated,role);
          });
        }
      }
      const nav = el("div",null,wrap); nav.className = "inline-actions";
      if (start) button(nav,"上一页建议",()=>drawPage(Math.max(0,start-12)));
      if (start+12 < claims.length) button(nav,"下一页建议",()=>drawPage(start+12));
    }
    drawPage(0);
  }

  function archiveView(data, role) {
    const box = panel("归档草稿 · " + nameOf(data,role));
    button(box,"返回当前草稿",async()=>openImport(await action("status",{ident:data.id}),role));
    const other = role === "A" ? "B" : "A";
    button(box,"查看归档 " + other,()=>archiveView(data,other));
    const draft = data.draft[role];
    for (const [key,label] of Object.entries(titles)) {
      el("strong",label,box);
      el("p",[].concat(draft.spec[key] || []).join("\n") || "待补",box).className = "import-original";
    }
    if (draft.relationship) el("p","关系说明 · " + draft.relationship,box);
    if (draft.claims?.length) suggestions(box,data,role,null,true);
  }

  const sceneLabels = {daily:"日常交流",relationship:"情绪与关系",help:"明确求助",closing:"收束",fallback:"兜底"};
  const cardStates = {none:"尚未整合", available:"可整合", awaiting_approval:"等待合成请求审定",
    sending:"正在生成两张角色卡", running:"正在生成两张角色卡", received:"已收到首次回执",
    unknown:"合成结果待核对", unsent:"上次请求未发送", ready:"角色卡已生成",
    stale:"合成来源已变化", not_ready:"等待两份画像就绪", capacity:"两份画像超过本次输入容量",
    unconfigured:"角色卡合成模板待配置", no_key:"请先配置官方 DeepSeek 连接",invalid_config:"角色卡配置需要核对",
    partial:"可用字段已预填", manual_only:"画像材料不足，可补填后保存所选人物", failed:"合成未完成", revoked:"来源已撤回"};
  const groups = {
    identity:["身份",{aliases:"别名",species:"种族",gender:"性别",pronouns:"代词"}],
    age:["年龄与生日",{fixed_age:"固定年龄",birth_date:"出生日期",birthday:"生日（月-日）"}],
    appearance:["外貌",{height_cm:"身高（厘米）",weight_kg:"体重（千克）",blood_type:"血型",
      hair_color:"发色",eye_color:"瞳色",description:"外貌描述"}],
    biography:["经历",{hometown:"家乡",occupation:"职业",education:"教育经历",family:"家庭",relationships:"人物关系"}],
    interests:["兴趣",{likes:"喜欢",dislikes:"不喜欢",hobbies:"爱好",notes:"兴趣补充"}],
    social:["社交表达",{public_image:"外在印象",humor:"幽默方式",context_rules:"不同情境的表达"}],
    world:["世界设定",{name:"世界名称",description:"世界描述"}],
  };
  function groupBox(parent,title,open=false) {
    const detail=el("details",null,parent); detail.className="import-editor-section"; detail.open=open;
    el("summary",title,detail);
    const body=el("div",null,detail); body.className="fields";
    return body;
  }
  function numberOrNull(value,integer=false) {
    if (!value) return null;
    const number=Number(value);
    if (!Number.isFinite(number) || (integer && !Number.isInteger(number))) throw new FormError("请填写有效数值。");
    return number;
  }
  function characterForm(box, character) {
    const readers=[];
    for(const [group,[title,fields]] of Object.entries(groups)){
      const section=groupBox(box,title);
      for(const [key,label] of Object.entries(fields)){
        const old=character[group][key], list=Array.isArray(old);
        const input=field(section,label+(list?"（每行一项）":""),old??"",list,"character."+group+"."+key);
        const initial=input.value;
        readers.push(result=>{
          if(input.value===initial) return;
          const text=input.value.trim();
          let value=text || null;
          if (list) value=text.split("\n").map(v=>v.trim()).filter(Boolean);
          if (["fixed_age","height_cm","weight_kg"].includes(key)) value=numberOrNull(text,key==="fixed_age");
          result[group][key]=value;
        });
      }
    }
    const extras=groupBox(box,"表达样例与补充资料");
    const examples=new Map(), entries=new Map();

    function addExample(example,open=false){
      const sub=groupBox(extras,"表达样例 "+(examples.size+1),open);
      const text=field(sub,"原话",example.text), scenario=field(sub,"情境",example.scenario);
      const keys=field(sub,"关键词（每行一项）",example.keywords,true);
      const enabled=check(sub,"启用样例",example.enabled);
      const readText=readField(text,example.text), readScenario=readField(scenario,example.scenario);
      const readKeys=readField(keys,example.keywords,lines);
      examples.set(example.id,()=>({...example,text:readText(),scenario:readScenario(),
                                    keywords:readKeys(),enabled:enabled.checked}));
      button(sub,"移除此样例",()=>{examples.delete(example.id);sub.parentElement.remove();});
    }
    function addEntry(entry,open=false){
      const sub=groupBox(extras,entry.title||"补充资料 "+(entries.size+1),open);
      const title=field(sub,"标题",entry.title), category=field(sub,"类别",entry.category), body=field(sub,"正文",entry.text);
      const keys=field(sub,"关键词（每行一项）",entry.keywords,true);
      const enabled=check(sub,"启用条目",entry.enabled);
      const visible=check(sub,"允许对话引用",entry.visibility==="conversation");
      const readTitle=readField(title,entry.title),readBody=readField(body,entry.text);
      const readCategory=readField(category,entry.category),readKeys=readField(keys,entry.keywords,lines);
      entries.set(entry.id,()=>({...entry,title:readTitle(),text:readBody(),category:readCategory(),
                                keywords:readKeys(),enabled:enabled.checked,
                                visibility:visible.checked?"conversation":"creator_only"}));
      button(sub,"移除此条目",()=>{entries.delete(entry.id);sub.parentElement.remove();});
    }
    for(const item of character.examples) addExample(item);
    for(const item of character.lorebook) addEntry(item);
    const additions=el("div",null,extras);additions.className="inline-actions";
    button(additions,"添加表达样例",()=>addExample({id:newId(),text:"",scenario:"",keywords:[],enabled:false},true));
    button(additions,"添加补充资料",()=>addEntry({id:newId(),title:"",text:"",category:"",priority:0,keywords:[],
                                                dependencies:[],enabled:false,visibility:"creator_only"},true));
    return ()=>{
      const result=structuredClone(character);
      for(const reader of readers) reader(result);
      if (result.age.fixed_age!==null && result.age.birth_date) throw new FormError("固定年龄和出生日期请选择一种年龄依据。");
      if(result.age.fixed_age!==character.age.fixed_age || result.age.birth_date!==character.age.birth_date)
        result.age.policy=result.age.fixed_age!==null?"fixed":result.age.birth_date?"calendar":"unspecified";
      result.examples=Array.from(examples.values(),read=>read());
      result.lorebook=Array.from(entries.values(),read=>read());
      return result;
    };
  }
  function pathLabel(path) {
    if (path.startsWith("spec.")) return titles[path.slice(5)] || "名称";
    if (path.startsWith("scenes.")) return sceneLabels[path.slice(7)];
    if (path.startsWith("jev.criteria.")) return "Jev · "+sceneLabels[path.slice(13)];
    if (path.startsWith("character.")) {
      const [,group,key]=path.split(".");
      return groups[group]?.[1]?.[key] || ({examples:"表达样例",lorebook:"补充资料"})[group] || path;
    }
    return ({relationship:"关系说明",common:"共通人设提示","jev.instruction":"Jev 总决策说明"})[path] || path;
  }
  function cardPath(card, path) {
    if (typeof path !== "string") return null;
    const parts=path.split(".");
    const [section,key,subkey]=parts;
    const sceneSlot=parts.length===2 && section==="scenes" && Object.hasOwn(sceneLabels,key);
    const criterion=parts.length===3 && section==="jev" && key==="criteria" && Object.hasOwn(sceneLabels,subkey);
    const persona=parts.length===2 && section==="spec" && Object.hasOwn(titles,key);
    const fact=section==="character" && ((parts.length===2 && ["examples","lorebook"].includes(key)) ||
      (parts.length===3 && Object.hasOwn(groups,key) && Object.hasOwn(groups[key][1],subkey)));
    if(!["relationship","common","jev.instruction"].includes(path) && !sceneSlot && !criterion && !persona && !fact)
      return null;
    let parent=card;
    for(const part of parts.slice(0,-1)) {
      if(!Object.hasOwn(parent,part)) return null;
      parent=parent[part];
    }
    return {parent,key:parts.at(-1)};
  }
  function pendingProposals(box,card,onApply,scenesOnly=false,onApplyText=null) {
    const proposals=card.issues.filter(i=>i.severity!=="conflict" && Object.hasOwn(i,"proposal") && cardPath(card,i.field) &&
      (!scenesOnly || i.field==="common" || i.field.startsWith("scenes.") || i.field.startsWith("jev.")));
    if(!proposals.length) return;
    const pending=proposals.filter(item=>{
      const slot=cardPath(card,item.field);
      return JSON.stringify(slot.parent[slot.key])!==JSON.stringify(item.proposal);
    }).length;
    const pane=groupBox(box,pending?"可选补充 · "+pending:"补充填写记录");
    const prose=proposals.filter(i=>!i.field.startsWith("character."));
    if(onApplyText && prose.some(i=>{
      const slot=cardPath(card,i.field);
      return JSON.stringify(slot.parent[slot.key])!==JSON.stringify(i.proposal);
    }))button(pane,"核对并填入本角色文案",()=>onApplyText(prose));
    for(const item of proposals) {
      const title=pathLabel(item.field), section=groupBox(pane,title);
      el("p",item.reason,section);
      const value=item.proposal;
      el("pre",typeof value==="string" ? value : JSON.stringify(value,null,2),section).className="import-original";
      const {parent,key}=cardPath(card,item.field);
      if(JSON.stringify(parent[key])===JSON.stringify(value)) el("small","已填入当前草稿",section);
      else button(section,"核对后填入「"+title+"」",()=>onApply(item));
    }
  }
  function fillTextProposals(card,issues) {
    const updated=structuredClone(card), inserts=[],replacements=[];
    for(const issue of issues){
      const slot=cardPath(updated,issue.field);
      if(!slot || issue.field.startsWith("character.")) continue;
      const before=slot.parent[slot.key];
      if(JSON.stringify(before)===JSON.stringify(issue.proposal)) continue;
      const filled=before!==null && before!=="" && (!Array.isArray(before) || before.length>0);
      (filled?replacements:inserts).push(pathLabel(issue.field));
      slot.parent[slot.key]=structuredClone(issue.proposal);
    }
    if(!inserts.length && !replacements.length) return null;
    const summary=["将候选文案作为手工填写保存到「"+card.spec.name+"」的草稿。"];
    if(inserts.length) summary.push("填入空项："+inserts.join("、"));
    if(replacements.length) summary.push("覆盖已有内容："+replacements.join("、"));
    return confirm(summary.join("\n")) ? updated : null;
  }
  function fillProposal(card,issue) {
    const updated=structuredClone(card), slot=cardPath(updated,issue.field);
    if(!slot) throw new FormError("该字段需要在表单内填写。");
    const before=slot.parent[slot.key];
    const occupied=before!==null && before!=="" && (!Array.isArray(before) || before.length>0);
    if(occupied && JSON.stringify(before)!==JSON.stringify(issue.proposal) &&
       !confirm("覆盖草稿中已有的「"+pathLabel(issue.field)+"」？")) return null;
    slot.parent[slot.key]=structuredClone(issue.proposal);
    if(issue.field==="character.age.fixed_age" || issue.field==="character.age.birth_date"){
      const age=updated.character.age;
      if(age.fixed_age!==null && age.birth_date) throw new FormError("请先在人物资料中选择固定年龄或出生日期。");
      age.policy=age.fixed_age!==null?"fixed":age.birth_date?"calendar":"unspecified";
    }
    return updated;
  }
  function cardProof(parent, data, role, card, save) {
    const sources=Object.entries(card.sources);
    const issues=card.issues.filter(i=>i.severity!=="conflict" && (!Object.hasOwn(i,"proposal")||!cardPath(card,i.field)));
    if(!sources.length && !issues.length && !card.generated_fields?.length) return;
    const box=groupBox(parent,"字段出处与待补项");
    if(card.generated_fields?.length)
      el("p","模型整理的文案 · "+card.generated_fields.map(pathLabel).join("、"),box);
    for (const issue of issues) el("p",pathLabel(issue.field)+" · "+issue.reason,box);
    for (const [path,observations] of sources) {
      const part=groupBox(box,pathLabel(path));
      for(const obs of observations) {
        el("strong",obs.value,part);
        if(obs.scope) el("p",obs.scope,part);
        for (const ref of obs.evidence || []) {
          button(part,role+" · #"+ref.id+" · "+ref.quote,async()=>{
            await save();
            await claimSource(data,role,ref,async()=>openImport(await action("status",{ident:data.id}),role),"返回角色卡");
          }).classList.add("import-citation");
        }
      }
    }
  }
  function sceneMaterial(box,card) {
    const common=field(box,"角色共通提示",card.common,false,"common");
    const readCommon=readField(common,card.common), inputs={};
    for(const [key,label] of Object.entries(sceneLabels)) {
      const group=groupBox(box,label);
      const segment=field(group,"场景正文",card.scenes[key],false,"scenes."+key);
      const preview=groupBox(group,"完整场景提示");
      const body=el("pre",null,preview); body.className="import-original";
      const readSegment=readField(segment,card.scenes[key]);
      const update=()=>{body.textContent=readCommon() && readSegment() ? readCommon()+"\n\n"+readSegment() : "待补";};
      common.addEventListener("input",update); segment.addEventListener("input",update); update();
      inputs[key]=readSegment;
    }
    const routing=groupBox(box,"Jev 场景判断");
    const instruction=field(routing,"总决策说明",card.jev.instruction,false,"jev.instruction");
    const readInstruction=readField(instruction,card.jev.instruction), criteria={};
    for(const [key,label] of Object.entries(sceneLabels)){
      const input=field(routing,label+"判别依据",card.jev.criteria[key],false,"jev.criteria."+key);
      criteria[key]=readField(input,card.jev.criteria[key]);
    }
    const values=readers=>Object.fromEntries(Object.entries(readers).map(([key,read])=>[key,read()]));
    return ()=>({common:readCommon(),scenes:values(inputs),jev:{instruction:readInstruction(),criteria:values(criteria)}});
  }
  function cardConflicts(cards, selected=roles) {
    const items=[];
    for (const role of selected) {
      const card=cards[role];
      const seen=new Set();
      for(const issue of card?.issues || []) {
        if(issue.severity!=="conflict" || seen.has(issue.field)) continue;
        seen.add(issue.field);
        items.push({role,field:issue.field,reason:issue.reason,alternatives:issue.alternatives || [],
          observations:issue.observations || card.sources[issue.field] || []});
      }
    }
    return items;
  }

  function conflictWarning(items, cards, locate) {
    if (!items.length || !dialog.open) return;
    const popup=el("dialog",null,document.body);
    popup.className="import-conflict-dialog"; popup.setAttribute("role","alertdialog");
    popup.setAttribute("aria-labelledby","card-conflict-title");popup.setAttribute("aria-describedby","card-conflict-message");
    const header=el("div",null,popup);header.className="dialog-head";
    el("h2","核对角色卡冲突",header).id="card-conflict-title";
    const body=el("div",null,popup);body.className="dialog-body";
    el("p","请核对以下字段的依据，修改内容或确认保留当前值。",body).id="card-conflict-message";
    for(const issue of items){
      const group=el("section",null,body);group.className="import-conflict-row";
      el("strong",issue.role+" · "+cards[issue.role].spec.name+" · "+pathLabel(issue.field),group);
      el("p",issue.reason,group);
      button(group,"处理 "+issue.role+" 的「"+pathLabel(issue.field)+"」",async()=>{
        popup.close();await locate(issue.role,issue.field);
      });
    }
    const foot=el("div",null,popup);foot.className="dialog-foot";
    button(foot,"返回角色卡",()=>popup.close());
    const closeOnExit=()=>{if(popup.open) popup.close();};
    dialog.addEventListener("close",closeOnExit);
    popup.addEventListener("close",()=>{
      dialog.removeEventListener("close",closeOnExit);
      popup.remove();
    },{once:true});
    popup.showModal();
  }

  function focusCardField(box,path) {
    const control=[...box.querySelectorAll("[data-card-path]")].find(el=>el.dataset.cardPath===path);
    if(!control) return;
    for(let parent=control.parentElement; parent && parent!==box; parent=parent.parentElement)
      if(parent.tagName==="DETAILS") parent.open=true;
    const focus=()=>{if(control.isConnected){control.scrollIntoView({block:"center"});control.focus();}};
    focus();
    setTimeout(focus,0);
  }

  let cardToastTimer, openingCardChat=false;
  function cardNotice(message) {
    let notice=byId("import-card-toast");
    if(!notice) {
      notice=el("div",null,dialog);
      notice.id="import-card-toast";
      notice.className="import-card-toast";
      notice.setAttribute("role","status");
    }
    clearTimeout(cardToastTimer);
    notice.textContent=message;
    notice.hidden=false;
    cardToastTimer=setTimeout(()=>{notice.hidden=true;},2600);
  }

  function markRequiredCardFields(box,missing) {
    const paths=missing.map(key=>"spec."+key);
    const controls=[...box.querySelectorAll("textarea[data-card-path]")];
    cardNotice("请填写"+missing.map(key=>"「"+(titles[key]||"名称")+"」").join("和"));
    for(const input of controls) {
      input.classList.remove("card-field-shake");
      input.removeAttribute("aria-invalid");
      if(paths.includes(input.dataset.cardPath)) input.setAttribute("aria-invalid","true");
    }
    setTimeout(()=>{
      if(!box.isConnected||!dialog.open) return;
      for(const input of controls.filter(node=>node.getAttribute("aria-invalid")==="true")){
        input.getBoundingClientRect();
        input.classList.add("card-field-shake");
      }
      focusCardField(box,paths[0]);
      const first=controls.find(input=>input.dataset.cardPath===paths[0]);
      if(first) first.setSelectionRange(first.value.length,first.value.length);
    },0);
  }

  function addConflictResolution(box, issue, save, finish, openSource) {
    const control=[...box.querySelectorAll("[data-card-path]")].find(input=>input.dataset.cardPath===issue.field);
    const pane=el("div",null,box);pane.className="import-conflict-row";
    const parent=control?.closest("label.field") || control;
    if(parent) parent.after(pane);
    else {pane.dataset.cardPath=issue.field; pane.tabIndex=-1;}
    el("strong","需核对 · "+pathLabel(issue.field),pane);
    el("p",issue.reason,pane);
    if(issue.alternatives.length) {
      el("strong","分析返回的候选值",pane);
      const list=el("ul",null,pane);
      for(const variant of issue.alternatives) {
        const value=Array.isArray(variant.value)?variant.value.join("、"):String(variant.value);
        el("li",pathLabel(variant.field)+" · "+value,list);
      }
    }
    if(issue.observations.length) {
      const proof=groupBox(pane,"相关原话");
      for(const obs of issue.observations) {
        el("p",obs.value,proof);
        for(const [name,refs] of [["依据",obs.evidence],["反例",obs.counter_evidence]])
          for(const ref of refs||[])button(proof,name+" · #"+ref.id+" · "+ref.quote,()=>openSource(ref));
      }
    }
    button(pane,"确认「"+pathLabel(issue.field)+"」当前填写",async()=>{
      const result=await save([issue.field]);finish(result);
    });
  }

  function openCard(data, role, job, creation={}) {
    const card=job.cards[role],other=role==="A"?"B":"A";
    const box=panel("核对角色卡 · "+role+" · "+card.spec.name);
    el("p",cardStates[job.state] || job.state,box);
    const blockers=cardConflicts(job.cards);
    async function locate(target,path){
      if(target===role){focusCardField(box,path);return;}
      openCard(data,target,await save(),{...editorOptions(target),focusField:path});
    }
    if(blockers.length) button(box,"核对冲突（"+blockers.length+"）",()=>conflictWarning(blockers,job.cards,locate));
    if(job.stale) {
      el("p","当前显示最新草稿。",box);
      const previous=job.stale_candidate?.[role];
      if(previous){
        const compare=groupBox(box,"旧版本候选");
        for(const [key,title] of Object.entries(titles)){
          el("strong",title,compare);el("p",[].concat(previous.spec[key]||[]).join("\n") || "待补",compare).className="import-original";
        }
        for(const [title,body] of [["共通提示",previous.common],["Jev 总决策说明",previous.jev.instruction],
                                   ...Object.entries(sceneLabels).map(([key,label])=>[label,previous.scenes[key]])]){
          el("strong",title,compare);el("p",body || "待补",compare).className="import-original";
        }
      }
    }
    if(job.error) el("p",job.error==="invalid_receipt"?"成卡回执未通过格式校验，请手工补齐。":String(job.error),box);
    const nav=el("div",null,box); nav.className="tablist";
    for(const target of roles){
      const tab=button(nav,"角色 "+target,async()=>{
        if(target!==role)openCard(data,target,await save(),editorOptions(target));
      });
      tab.setAttribute("aria-pressed",String(target===role));
    }
    button(nav,"查看原画像",async()=>{
      await save(); await openPlanned(await action("status",{ident:data.id}),role,{source:true});
    });
    async function saveProposal(issues,fill){
      const current=await save();
      const proposal=fill(current.cards[role],issues);
      if(!proposal) return;
      const next=await action("card_edit",{ident:data.id,revision:data.revision,plan_id:data.plan.id,
                              card_revision:current.revision,role,card:proposal});
      openCard(data,role,next,editorOptions());
    }
    const base=groupBox(box,"基础人设与语言风格",true);
    const readSpec=specForm(base,card.spec,job.required);
    const relation=readField(field(base,"关系说明",card.relationship,false,"relationship"),card.relationship);
    base.addEventListener("input",event=>{
      const input=event.target;
      if(input.matches("textarea[data-card-path]") && input.value.trim()){
        input.removeAttribute("aria-invalid");
        input.classList.remove("card-field-shake");
      }
    });
    pendingProposals(box,card,issue=>saveProposal(issue,fillProposal),false,
                     items=>saveProposal(items,fillTextProposals));
    const factBox=groupBox(box,"人物资料");
    const readCharacter=characterForm(factBox,card.character);
    const roleplay=data.preview?.chat_mode==="roleplay";
    const canChooseScene=!roleplay && !window.pcarMobile;
    const materialBox=groupBox(box,state.community?"场景提示与 Flash 判别":"场景提示与 Jev");
    const readScenes=sceneMaterial(materialBox,card);
    const scene=canChooseScene ? check(materialBox,"核准并启用当前角色的场景提示和判别内容",Boolean(creation.scene)) : null;
    materialBox.parentElement.hidden=!canChooseScene;
    if(roleplay) el("p","角色扮演模式 · 剧情以原有叙述方式呈现。",box);
    cardProof(box,data,role,card,save);
    async function save(resolvedFields=[]){
      const changed={...card,spec:readSpec(),relationship:relation(),
                     character:readCharacter(),...readScenes()};
      if (!resolvedFields.length && JSON.stringify(changed) === JSON.stringify(job.cards[role])) return job;
      job=await action("card_edit",{ident:data.id,revision:data.revision,plan_id:data.plan.id,
        card_revision:job.revision,role,card:changed,...(resolvedFields.length?{resolved_fields:resolvedFields}:{})});
      return job;
    }
    button(box,"暂存角色卡",async()=>{
      openCard(data,role,await save(),editorOptions());
      cardNotice("草稿已暂存");
    });
    const finalActions=el("div",null,byId("dialog-actions"));
    finalActions.className="import-create-actions";
    const mode=inputField(box,"开聊方式","select");
    mode.add(new Option("从空白开始","empty"));
    mode.add(new Option("带历史开始","history"));
    mode.value=creation.mode||"empty";
    function editorOptions(target=role){return {mode:mode.value,scene:target===role && Boolean(scene?.checked)};}
    async function openSavedChat(pid){
      openingCardChat=true;
      try{
        await reload(pid,false);
        if(stopped || currentPerson()?.id!==pid) throw new Error("card_chat_selection_incomplete");
      }catch{
        if(!dialog.open) return;
        const saved=panel("角色已保存");
        button(saved,"打开角色 "+role+" 的聊天",()=>openSavedChat(pid)).classList.add("primary");
        return;
      }finally{
        openingCardChat=false;
      }
      dialog.close();
      window.pcarMobile?.chat();
    }
    async function previewPublication(start){
      const result=await save(), options=editorOptions();
      const identity={ident:data.id,revision:data.revision,plan_id:data.plan.id,card_revision:result.revision};
      const preview=await action("card_review",identity);
      if(!box.isConnected||!dialog.open) return;
      const current=preview.roles[role], peer=preview.roles[other];
      if(current.unavailable){cardNotice("请先从人物中心恢复这个角色");return;}
      if(current.missing.length){markRequiredCardFields(box,current.missing);return;}
      if(!start && current.saved){cardNotice("角色 "+role+" 已保存");return;}
      const selected=[role];
      if(start && !peer.unavailable && !peer.saved && !peer.missing.length) selected.push(other);
      const pending=selected.filter(target=>!preview.roles[target].saved);
      const scenes=options.scene && !current.saved && Object.keys(current.scenes).length ? current.scenes : null;
      const next={plan_id:identity.plan_id,card_revision:identity.card_revision,
                  roles:pending,enable_scene:Boolean(scenes),omit_unresolved:true,
                  ...(scenes?{scene_role:role}:{})};
      const back=()=>openCard(data,role,result,options);
      async function confirm(){
        if(start && options.mode==="history" && !current.saved){
          setupHistory(data,{cards:result.cards,creation:next,targetRole:role,back});
          return;
        }
        const operation=["card",data.id,data.revision,result.revision,pending.join(""),next.scene_role||""].join(".");
        const people=pending.length?await action("card_create",{...identity,...next,operation_id:operation,confirmed:true}):[];
        if(!start){back();cardNotice("角色 "+role+" 已保存");return;}
        const pid=current.saved||people[pending.indexOf(role)];
        if(options.mode==="history"){
          setupHistory(data,{personaId:pid,targetName:current.name,targetRole:role,back});
        } else {
          await openSavedChat(pid);
        }
      }
      if(!pending.length){await confirm();return;}
      const reviewBox=panel("预览人设提示词");
      const choosing=el("div",null,reviewBox);
      choosing.className="tablist";
      let viewedRole=role;
      const promptMode=scenes?inputField(reviewBox,"提示词类型","select"):null;
      if(promptMode){
        promptMode.add(new Option("普通聊天",""));
        for(const key of Object.keys(scenes))promptMode.add(new Option(sceneLabels[key],key));
        promptMode.onchange=()=>show(viewedRole);
      }
      const prompt=field(reviewBox,"人设提示词","",false);
      prompt.readOnly=true;
      prompt.className="import-card-prompt";
      const details=el("p",null,reviewBox);
      details.className="hint";
      function show(target){
        viewedRole=target;
        const item=preview.roles[target];
        if(promptMode) promptMode.closest("label").hidden=target!==role;
        prompt.value=(target===role && scenes?.[promptMode?.value]) || item.prompt;
        prompt.scrollTop=0;
        details.textContent=[item.template_ready?"":"角色扮演模板待审定，框内显示本次保存的人设。",
          item.omitted.length?"已跳过 "+item.omitted.length+" 个待核对的字段。":""].filter(Boolean).join(" ");
        details.hidden=!details.textContent;
        for(const tab of choosing.querySelectorAll("button"))
          tab.setAttribute("aria-pressed",String(tab.dataset.role===target));
      }
      if(selected.length>1) for(const target of selected){
        const tab=button(choosing,"角色 "+target,()=>show(target));
        tab.dataset.role=target;
      }
      show(role);
      if(options.scene && !current.saved){
        el("p",scenes?"保存时核对场景连接与用途授权。":"场景材料待核对。本次保存普通人设。",reviewBox).className="hint";
      }
      if(start){
        const text=selected.length>1?"同时保存角色 "+other+"，进入角色 "+role+" 的聊天。":
          "进入角色 "+role+" 的聊天。"+(!peer.saved && !peer.unavailable?"角色 "+other+" 保留为草稿。":"");
        el("p",text,reviewBox);
      }
      const actions=byId("dialog-actions");
      button(actions,"返回编辑",back);
      button(actions,start?(options.mode==="history"?"确认并选择历史":"确认并开始聊天"):"确认保存",confirm).classList.add("primary");
    }
    button(finalActions,"保存角色 "+role,()=>previewPublication(false));
    button(finalActions,"开始聊天",()=>previewPublication(true)).classList.add("primary");
    for(const issue of blockers.filter(i=>i.role===role)) {
      addConflictResolution(box,issue,save,next=>openCard(data,role,next,editorOptions()),
        async ref=>{
          const current=await save();
          await claimSource(data,role,ref,()=>openCard(data,role,current,{...editorOptions(),focusField:issue.field}),"返回冲突字段");
        });
    }
    if(creation.focusField) focusCardField(box,creation.focusField);
  }

  async function openPlanned(data, role, creation={}) {
    const box = panel("聊天人设草稿");
    const plan = data.plan;
    let read=null, relation=null;
    el("p",data.format + " · " + (data.status==="partial_ready" ? "部分结果可审阅" : jobs[data.status] || data.status),box);
    if (data.status === "revoked") {button(box,"返回",home); return;}
    showCoverage(box,data);
    if (data.status === "running") {
      button(box,"查看本次分析范围",()=>planScope(data,role,()=>openImport(data,role)));
      button(box,"取消提炼",async()=>openImport(await action("cancel",{ident:data.id}),role));
      setTimeout(run(async()=>{
        if (!box.isConnected || !dialog.open) return;
        const latest = await action("status",{ident:data.id});
        if (box.isConnected && dialog.open) await openImport(latest,role);
      }),900);
      return;
    }
    const job = await action("card_status",{ident:data.id});
    if(!box.isConnected) return;
    if(!creation.source && job.cards?.[role] &&
       (job.revision > 0 || creation.editCard || job.state==="manual_only") && !["sending","running"].includes(job.state)) {
      return openCard(data,role,job,creation);
    }
    const waiting=["sending","running"].includes(job.state);
    const hasBoth=roles.every(r=>data.draft[r]?.claims.some(c=>c.decision!=="reject"));
    const draft = data.draft[role];
    const refreshed = next => openImport(next,role);
    if(waiting) {
      el("p",cardStates[job.state],box);
      button(box,"取消本次成卡",async()=>refreshed(await action("cancel",{ident:data.id})));
      setTimeout(run(async()=>{if(box.isConnected && dialog.open)await refreshed(await action("status",{ident:data.id}));}),850);
      return;
    }
    if(job.cards?.[role]) button(box,job.revision>0 ? "打开角色卡" : "手工编辑角色卡",async()=>{
      const current=await save();
      const latest=await action("card_status",{ident:current.id});
      if(!latest.cards?.[role]) throw new FormError("最新画像尚未就绪。");
      openCard(current,role,latest,creation);
    });
    if(hasBoth && job.state==="received"){
      button(box,"校验已收成卡回执",async()=>{
        const current=await save();
        await action("card_compose",{ident:current.id,revision:current.revision,plan_id:plan.id,confirmed:true});
        await refreshed(await action("status",{ident:current.id}));
      });
    } else if(hasBoth && ["available","unsent"].includes(job.state) && !job.stale) {
      const ready=check(box,"允许官方 DeepSeek 用这两份画像生成角色卡，最多一次额外请求");
      const make=button(box,"整合两张角色卡",async()=>{
        if(!ready.checked) throw new FormError("请先确认本次合成用途。");
        const current=await save();
        const result=await action("card_compose",{ident:current.id,revision:current.revision,plan_id:plan.id,confirmed:true});
        if(result.state==="awaiting_approval") notify(cardStates.awaiting_approval);
        await refreshed(await action("status",{ident:data.id}));
      });
      make.disabled=true;
      ready.onchange=()=>{make.disabled=!ready.checked||!job.configured;};
      if(!job.configured) el("small","合成模板待配置。",box);
    } else if(["unknown","failed","capacity","no_key","invalid_config","unconfigured","awaiting_approval","stale"].includes(job.state)) {
      el("p",cardStates[job.state],box);
      if(job.state==="no_key") button(box,"配置模型连接",async()=>{await save();connection();});
      if(["awaiting_approval","unconfigured","invalid_config","unknown"].includes(job.state))
        button(box,"重新检查成卡状态",async()=>refreshed(await save()));
    }
    button(box,"查看或调整本次分析范围",async()=>{
      const current = await save();
      planScope(current,role,()=>openImport(current,role));
    });
    if (!plan.configured) {
      el("p",plan.chat_mode==="roleplay"?"角色扮演提示词待审定。可先核对范围和编辑草稿。":
        "提炼配置待审定。可先核对范围和编辑草稿。",box);
      button(box,"按当前配置重新计划",async()=>{
        const current = await save();
        await refreshed(await action("plan",{ident:data.id,revision:current.revision,mapping:current.mapping,
          overrides:current.overrides}));
      });
    }
    if (data.status !== "ready") {
      if(!state.connected) button(box,"配置本次模型连接",async()=>{
        await save(); connection(async()=>openImport(await action("status",{ident:data.id}),role));
      });
      const consent = check(box,"允许官方 DeepSeek 按上述范围提取、按需汇总并生成两张角色卡");
      const analyze = button(box,"分析并自动填写两张角色卡",async()=>{
        if (!consent.checked) throw new FormError("请先确认本次分析用途。");
        const current = await save();
        await refreshed(await action("analyze",{ident:data.id,revision:current.revision,plan_id:plan.id,confirmed:true,compose_card:true}));
      });
      analyze.disabled = true;
      consent.onchange = () => {analyze.disabled = !consent.checked || !plan.configured || !state.connected;};
      button(box,"从现有回执恢复",async()=>{
        const current = await save();
        await refreshed(await action("replay",{ident:data.id,revision:current.revision,plan_id:plan.id}));
      });
    }
    if (!hasBoth && !roles.some(r=>data.draft[r]?.claims.length)) {
      button(box,"重新核对说话人",()=>mapping(data));
      return;
    }
    const other = role === "A" ? "B" : "A";
    el("h3",nameOf(data,role),box);
    const stats = plan.roles[role].stats;
    if (stats) {
      const detail = el("details",null,box); el("summary","本地表达统计",detail);
      el("p",stats.messages + " 条完整的本人发言 · 平均 " + stats.mean_chars + " 字 · 连发 " + stats.burst_messages + " 条",detail);
      const marks = Object.entries(stats.ending_punctuation).map(([mark,count])=>mark + " " + count + " 次");
      if (marks.length) el("p","句末标点 · " + marks.join("、"),detail);
      for (const repeated of stats.common_messages)
        button(detail,"重复表达 #" + repeated.id + " · " + repeated.count + " 次",async()=>{
          const current = await save();
          await claimSource(current,role,repeated,()=>openImport(current,role));
        });
    }
    button(box,"暂存并查看 " + other,async()=>openImport(await save(),other));
    if (draft.claims.some(claim => claim.decision === "pending" && !claim.needs_review &&
        claim.field !== "values_and_boundaries")) {
      button(box,"采纳本人物可用建议",async()=>{
        const current = await save();
        await refreshed(await action("review_all",{ident:data.id,revision:current.revision,plan_id:plan.id,role}));
      });
    }
    suggestions(box,data,role,save);
    read = specForm(box,draft.spec);
    relation = field(box,"关系说明",draft.relationship);
    async function save() {
      if(!read) return data;
      const spec = read(), previous = data.draft[role];
      if (Object.keys(spec).every(k=>JSON.stringify(spec[k])===JSON.stringify(previous.spec[k])) &&
          relation.value === previous.relationship) return data;
      data = await action("edit",{ident:data.id,revision:data.revision,role,spec,relationship:relation.value});
      return data;
    }
    button(box,"暂存草稿",async()=>refreshed(await save()));
    if (plan.archived?.length) {
      const list = el("details",null,box); el("summary","查看旧计划草稿",list);
      for (const old of plan.archived) button(list,old.created || old.id.slice(0,12),async()=>{
        const current = await save();
        const entry = await action("plan_archive",{ident:current.id,plan_id:old.id});
        archiveView({...entry,id:current.id},role);
      });
    }
    button(box,"重新核对归属",async()=>mapping(await save()));
    button(box,"撤回该文件的提炼与风格使用",async()=>{
      if (confirm("停止该来源的提炼与风格使用？已还原的历史继续保存在会话中。"))
        await refreshed(await action("revoke",{ident:data.id}));
    });
  }

  async function reload(prefer=null,closeDialog=true,viewSession=null,reconnect=false) {
    const token=++epoch;
    const result=await api().live_people();
    if(token!==epoch||stopped)return;
    state.connected=result.connected;state.model=result.model;state.community=!!result.community;
    if(state.community)recipientLabels.jev="DeepSeek Flash 决策";
    if(!reconnect)saveReading();
    state.people=result.people.filter(p=>!p.deleted).map(p=>({id:p.id,profile:{name:p.name},sessions:[]}));
    const chosen=state.people.find(p=>p.id===prefer) || state.people.find(p=>result.people.find(q=>q.id===p.id)?.selected) || state.people[0];
    if(chosen)await select(chosen,false,closeDialog,viewSession,reconnect);
    else {state.current=null;state.guide=true;render();}
  }
  async function select(person,fresh=false,closeDialog=true,viewSession=null,reconnect=false){
    if(!reconnect)saveReading();
    const id=++epoch;
    selectionToken=id;
    message.disabled=true;updateSendButton();
    try{
      await loadSelection(person,fresh,closeDialog,viewSession,reconnect,id);
    }finally{
      if(selectionToken===id){
        selectionToken=null;message.disabled=!ready()||Boolean(currentSession()?.readOnly);updateSendButton();
      }
    }
  }
  async function loadSelection(person,fresh,closeDialog,viewSession,reconnect,id){
    const result=viewSession ? await api().live_select(person.id,false,viewSession) : await api().live_select(person.id,fresh);
    if(id!==epoch||stopped)return;
    person.sessions=result.sessions.map(s=>({id:s.id,label:s.created_at.slice(0,16).replace("T"," "),lifecycle:s.state,
      draft:"",messages:[],past:[],version:null,historyReady:false,turns:[],readOnly:s.id!==result.active}));
    if(!person.sessions.length)person.sessions.push({id:"",label:"新对话",draft:"",turns:[],past:[],version:null});
    const chosen=person.sessions.find(s=>s.id===viewSession)||person.sessions.find(s=>s.id===result.active)||person.sessions[0];
    person.profile=result.person.spec; person.writableSession=result.active;
    person.activeSession=chosen.id;state.current=person.id;
    const target=currentSession();
    if(target){
      target.inputBuffer=result.input_buffer;
      target.loading=true;
      await Promise.all([draftWrites,locationWrites]);
      if(id!==epoch||stopped)return;
      const [saved,location]=await Promise.all([api().live_draft(person.id,target.id),
        api().live_location(person.id,target.id)]);
      if(id!==epoch||stopped)return;
      target.draft=saved.text;target.draftRevision=saved.revision;target.reference=saved.reference;
      const parked=disconnectedReading;
      target.restore=(reconnect && parked?.pid===person.id && parked?.sid===target.id && parked.mark)||location||{kind:"tail"};
    }
    state.guide=false;closeDrawer();if(closeDialog && dialog.open)dialog.close();render();
    await refresh();
    if(fresh && id===epoch && target===currentSession() && !dialog.open)await privacyPanel();
  }
  function appendTurns(s,cards){
    const rows=new Map(s.turns.map(t=>[t.id,t]));
    for(const card of cards){
      if(s.older && card.position<s.turns[0]?.position)continue;
      if(s.newer && card.position>s.turns.at(-1)?.position)continue;
      rows.set(card.id,card);
    }
    s.turns=Array.from(rows.values()).sort((a,b)=>a.position-b.position);
    if(s.turns.length>120){
      s.turns=s.turns.slice(-120);s.older=s.turns[0].position;
    }
  }
  async function nativePage(s,back){
    const p=currentPerson();
    if(!p||s!==currentSession()||s.paging)return;
    const token=++epoch;
    s.paging=true;
    try{
      const result=await api().live_page(p.id,s.id,null,back?s.older:null,back?null:s.newer);
      if(token!==epoch||s!==currentSession())return;
      s.turns=result.turns;s.older=result.older;s.newer=result.newer;s.version=result.version;s.latest=result.latest;
      s.inputBuffer=result.input_buffer;
      render();
      const natives=timeline.querySelectorAll('[data-kind="native"]');
      const edge=back?natives[natives.length-1]:natives[0];
      if(edge)edge.scrollIntoView({block:back?"end":"start"});
      saveReading();
    }finally{s.paging=false;}
  }
  async function refresh(){
    if(polling||!active)return;
    const p=currentPerson(),s=currentSession(),id=epoch;
    if(!p||!s||!s.id){if(s){s.loading=false;s.restore=null;}return;}
    if(s.paging||s.sending||s.bufferWorking)return;
    const bufferRevision=s.bufferRevision||0;
    const current=()=>id===epoch && s===currentSession() && !stopped &&
      bufferRevision===(s.bufferRevision||0) && !s.sending && !s.bufferWorking;
    polling=true;
    try{
      if(paintDecision){
        await api().presentation_decide(presenter,paintDecision.id,paintDecision.state,paintDecision.reason??null);
        paintDecision=null;
      }
      if(await api().presentation_reconcile())s.version=null;
      let result=await api().live_page(p.id,s.id,s.version,null,null,s.version===null&&s.restore?.kind==="native"?s.restore.position:null);
      if(!current())return;
      const sourceVersion=JSON.stringify(result.source_version);
      const rightsChanged=s.sourceVersion!==undefined && s.sourceVersion!==sourceVersion;
      if(rightsChanged || (result.reset && s.version!==null && s.version!==undefined)){
        const reading=capture();
        s.restore=reading||s.restore;
        if(rightsChanged || reading?.kind==="native"){
          result=await api().live_page(p.id,s.id,null,null,null,reading?.kind==="native"?reading.position:null);
          if(!current())return;
        }
      }
      if(rightsChanged){
        s.historyReady=false;s.past=[];s.before=null;
        const saved=await api().live_draft(p.id,s.id);
        if(id!==epoch||s!==currentSession())return;
        if(saved.revision===s.draftRevision)s.reference=saved.reference;
      }
      s.sourceVersion=sourceVersion;
      s.latest=result.latest;
      if(!s.historyReady){
        const revision=s.historyRevision;
        const mark=s.restore;
        let h=await action("history",{persona_id:p.id,session_id:s.id,
          ...(mark?.kind==="import"?{before:mark.position+40}:{}),limit:80});
        if(mark?.kind==="import" && !h.rows.length)h=await action("history",{persona_id:p.id,session_id:s.id,after:Math.max(1,mark.position-1),limit:80});
        if(id!==epoch||s!==currentSession()||revision!==s.historyRevision)return;
        s.past=h.rows;s.before=h.before;s.pastNewer=mark?.kind==="import" && h.rows.length?h.rows.at(-1).seq:null;s.historyReady=true;
      }
      const submissions=await api().live_submissions(p.id,s.id);
      if(!current())return;
      const scopeChanged=s.readOnly!==Boolean(result.sealed||s.id!==p.writableSession);
      s.readOnly=Boolean(result.sealed||s.id!==p.writableSession);
      s.branch=result.branch||null;
      const bufferChanged=JSON.stringify(s.inputBuffer)!==JSON.stringify(result.input_buffer);
      s.inputBuffer=result.input_buffer;
      if(result.reset){s.turns=result.turns;s.older=result.older;s.newer=result.newer;}
      else if(result.turns)appendTurns(s,result.turns);
      const known=new Set(s.turns.map(t=>t.id));
      const held=new Set(s.inputBuffer?.batch?.members.map(item=>item.request_id)||[]);
      const outbox=submissions.filter(item=>item.state!=="rejected"&&!known.has(item.turn_id)&&!held.has(item.id));
      const rejected=submissions.find(item=>item.state==="rejected");
      if(rejected)notify(bufferErrors[rejected.reason]);
      const changed=JSON.stringify(outbox)!==JSON.stringify(s.outbox||[]);
      s.outbox=outbox;
      const settled=submissions.filter(item=>(known.has(item.turn_id)||item.state==="stopped")&&!item.legacy).map(item=>item.id);
      const accepted=submissions.find(item=>acceptedSubmission(item) && item.draft_revision && item.draft_revision===s.draftRevision);
      if(accepted){
        s.draft="";s.reference=null;s.draftRevision=newId();
        await saveDraft(s,p.id,accepted.draft_revision);
      }
      if(settled.length)await api().live_ack_submissions(p.id,s.id,settled);
      if(!current())return;
      if(result.turns!==null){
        const atEnd=!s.restore && !s.newer && timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<24;
        const position=capture();
        if(!result.reset && !rightsChanged && !scopeChanged && !changed && !accepted && !bufferChanged && updateTurnNodes(s,result.turns)){
          if(atEnd)timeline.scrollTop=timeline.scrollHeight;else restoreReading(position);
        }else render({scrollToEnd:atEnd});
      }else if(changed || accepted || rightsChanged || scopeChanged || bufferChanged) render();
      s.version=result.version;
      s.longReplyAvailable=result.long_reply_available;
      updateSendButton();
      if(s.restore){restoreReading(s.restore);s.restore=null;disconnectedReading=null;}
      s.loading=false;
      if(!result.presentable||document.hidden||dialog.open||quotaDialog.open||repairDialog.open||!await api().presentation_visible()||s!==currentSession()||id!==epoch)return;
      const recovered=s.turns.find(t=>t.inline_reply?.confirmable && !(t.desktop_publications||[]).some(
        plan=>plan.background_lookup && ["pending","partial"].includes(plan.state)));
      if(recovered && !s.readOnly && await confirmInlineMain(p,s,id,recovered)){s.version=null;return;}
      const pending=result.pending_notices?.[0];
      if(pending){
        const args={...pending,persona_id:p.id,session_id:s.id};
        await presentMessage(s,id,"notice",args);
        s.version=null;return;
      }
      const next=plan=>plan.items.find(item=>!["presented","not_presented","cancelled"].includes(item.state));
      const speech=s.turns.flatMap(t=>t.desktop_publications||[]).find(x=>["pending","partial"].includes(x.state)&&next(x)?.state==="pending");
      if(!speech)return;
      const item=next(speech);
      const last=speech.visible.at(-1);
      if(last && Date.now()-Date.parse(last.presented_at)<item.delay_ms)return;
      const owner=speech.kind==="legacy"?{turn_id:speech.turn_id}:{utterance_id:speech.utterance_id};
      await presentMessage(s,id,speech.kind,{...owner,persona_id:p.id,session_id:s.id,publication_id:speech.publication_id,
        revision:speech.revision,ordinal:item.ordinal},speech.source_kind==="maibot_private_reply" && speech.phase==="main");
      s.version=null;
    }finally{polling=false;}
  }
  async function presentMessage(s,id,kind,args,timelineMain=false){
    if(!timelineMain && !await checkPaintSlot(s,id))return;
    const claim=newId();
    paintDecision={id:claim,state:"not_presented",reason:"bridge_error"};
    const offer=await api().presentation_begin(presenter,claim,kind,args);
    paintDecision=null;
    if(!offer)return;
    let decision={state:"unknown",reason:"bridge_error"};
    try{decision=await paint(s,id,offer,timelineMain);}
    finally{
      paintDecision={id:offer.id,...decision};
      await api().presentation_decide(presenter,offer.id,decision.state,decision.reason);
      paintDecision=null;
      await api().presentation_reconcile();
    }
  }
  function paintPoint(s,id,bubble){
    if(s!==currentSession()||id!==epoch)return {reason:"session_changed"};
    if(document.hidden)return {reason:"hidden"};
    if(dialog.open||quotaDialog.open||repairDialog.open)return {reason:"dialog_open"};
    if(!bubble.isConnected)return {reason:"detached"};
    const rect=bubble.getBoundingClientRect(),area=timeline.getBoundingClientRect();
    const left=Math.max(0,rect.left,area.left),right=Math.min(innerWidth,rect.right,area.right);
    const top=Math.max(0,rect.top,area.top),bottom=Math.min(innerHeight,rect.bottom,area.bottom);
    if(right<=left||bottom<=top)return {reason:"outside"};
    const x=(left+right)/2,y=(top+bottom)/2;
    return bubble.contains(document.elementFromPoint(x,y)) ? {x,y} : {reason:"covered"};
  }
  async function visibility(s,id,bubble){
    const hit=paintPoint(s,id,bubble);
    if(hit.reason)return hit.reason;
    const visible=await api().presentation_visible(hit.x*devicePixelRatio,hit.y*devicePixelRatio);
    return paintPoint(s,id,bubble).reason || (visible?null:"native_covered");
  }
  function paintSlot(){
    const bubble=el("p","",timeline.querySelector(".conversation-inner")||timeline);
    bubble.className="bubble reply";bubble.style.minHeight="1em";bubble.style.minWidth="2em";
    bubble.style.opacity="0";
    bubble.scrollIntoView({block:"center"});
    return bubble;
  }
  async function checkPaintSlot(s,id){
    const previous=timeline.scrollTop,bubble=paintSlot();
    try{
      return !await visibility(s,id,bubble);
    }finally{bubble.remove();if(s===currentSession()&&id===epoch)timeline.scrollTop=previous;}
  }
  function paintFrames(){
    return new Promise(resolve=>{
      let first,second,timer;
      function finish(value){
        clearTimeout(timer);cancelAnimationFrame(first);cancelAnimationFrame(second);
        document.removeEventListener("visibilitychange",hidden);resolve(value);
      }
      const hidden=()=>{if(document.hidden)finish("hidden");};
      document.addEventListener("visibilitychange",hidden);
      timer=setTimeout(()=>finish("frame_timeout"),1500);
      first=requestAnimationFrame(()=>{second=requestAnimationFrame(()=>finish(null));});
    });
  }
  function timelineReady(s,id,node){
    if(s!==currentSession()||id!==epoch)return "session_changed";
    if(document.hidden)return "hidden";
    if(!node?.isConnected||!timeline.contains(node))return "detached";
    return node.getClientRects().length?null:"outside";
  }
  async function confirmInlineMain(person,s,id,turn){
    const reply=turn.inline_reply;
    const group=renderedTurns.get(turn.id);
    if(!group)return false;
    const nodes=Array.from(group.querySelectorAll("[data-original-fragment]"));
    function mounted(){
      return nodes.length===reply.items.length && nodes.every((node,i)=>
        !timelineReady(s,id,node) && node.dataset.originalFragment===reply.items[i].id &&
        node.textContent===reply.items[i].text);
    }
    if(!mounted()||await paintFrames()||!mounted())return false;
    const result=await api().live_confirm_inline_main(person.id,s.id,turn.id,reply.publication,
      reply.revision,reply.items.map(item=>[item.id,item.body_hash]));
    return result.confirmed;
  }
  async function paint(s,id,offer,timelineMain=false){
    const bubble=paintSlot();
    bubble.dataset.fragment=offer.fragment;
    let written=false,confirmed=false;
    try{
      const before=timelineMain?timelineReady(s,id,bubble):await visibility(s,id,bubble);
      if(before)return {state:"not_presented",reason:before};
      bubble.textContent=offer.text;
      written=true;
      bubble.style.opacity="";
      bubble.scrollIntoView({block:"nearest"});
      const frame=await paintFrames();
      const reason=frame || (timelineMain?timelineReady(s,id,bubble):await visibility(s,id,bubble));
      confirmed=!reason;
      return {state:confirmed?"presented":"unknown",reason:reason||"confirmed"};
    }catch(error){
      return {state:written?"unknown":"not_presented",reason:"bridge_error"};
    }finally{
      if(!confirmed)bubble.remove();
    }
  }
  const memoryStateNames={off:"未开启",waiting_configuration:"等待角色记忆或模型配置",pending:"待整理",
    processing:"正在整理",waiting_quota:"等待后台额度",processed:"已处理",quarantined:"冲突待核",
    unknown:"结果待核对",failed:"部分整理任务失败，可查看详情",cancelled:"整理已取消",stale:"来源已变更，等待重新核对",
    no_memory:"已核对，未形成长期记忆",partly_withdrawn:"部分来源已撤回，其余已处理",blocked_input:"个别原话超过整理容量",withdrawn:"已撤回"};
  async function historySettings(s){
    const pid=currentPerson()?.id;
    const info=await action("history_status",{persona_id:pid,session_id:s.id});
    if(s!==currentSession())return;
    const box=panel("历史来源与用途");
    if(!info){el("p","本会话没有导入记录。",box);return;}
    el("p","续聊用途 · "+({active:"已确认",legacy:"待核对本人和原件",provider_changed:"当前模型待确认",
      withdrawn:"已撤回"}[info.usage_state] || "待确认"),box);
    el("p","长期记忆 · "+(memoryStateNames[info.memory_state] || "待核对"),box);
    showRecipients(box,info.current_providers,currentPerson()?.profile.chat_mode==="roleplay");
    if(info.usage_state==="legacy") {
      button(box,"选择原件并核对本人",async()=>{
        const file=await api().pick_transcript();
        if(!file)return;
        if(file.id!==info.document) throw new FormError("请选这段历史原先使用的聊天文件。");
        setupHistory(file,{personaId:pid,sessionId:s.id,targetName:currentPerson().profile.name});
      });
      return;
    }
    if(info.withdrawn)return;
    async function change(actionName) {
      await action("history_update",{persona_id:pid,session_id:s.id,expected_revision:info.revision,
        action:actionName,confirmed:true,expected_providers:info.current_providers});
      if(actionName==="withdraw") {
        for(const row of s.past) row.referenceable=false;
        s.reference=null;
        await reviseDraft(s);
      }
      s.historyRevision=(s.historyRevision||0)+1;
      s.historyReady=false;s.version=null;render();
      await historySettings(s);await refresh();
    }
    function providerChanged(key){
      const old=info.providers?.[key],current=info.current_providers?.[key];
      return old?.name!==current?.name || old?.base_url!==current?.base_url;
    }
    if(info.usage_state==="provider_changed" || providerChanged("jev"))button(box,"确认当前模型用于续聊",()=>change("authorize"));
    if(info.organize && providerChanged("memory")) button(box,"确认当前记忆整理模型",()=>change("organize"));
    if(info.organize)button(box,"暂停本次整理",()=>change("pause"));
    else button(box,"允许当前模型整理本次历史",()=>change("organize"));
    button(box,"撤回历史使用",async()=>{
      const confirmBox=panel("撤回历史使用");
      el("p","撤回后仍可在本机翻看。续聊、召回和派生记忆会停止使用这段来源。",confirmBox);
      button(confirmBox,"确认撤回历史使用",()=>change("withdraw"));
      button(confirmBox,"返回历史用途",()=>historySettings(s));
    });
  }
  function historyDetails(s,event,back=null){
    const box=panel("历史消息");
    const source=event.source || {};
    el("p",[source.speaker,source.number && "#"+source.number,
      (event.timestamp?.precision==="minute" && source.raw_time?source.raw_time:event.created_at) || "时间待核",source.line && "文件第 "+source.line+" 行"].filter(Boolean).join(" · "),box);
    const body=el("p",event.text,box); body.className="import-original";
    if(source.sections?.length>1 || source.sections?.length===1 && source.sections[0].kind!=="body"){
      const kinds={body:"正文",quote:"引用",forward:"转发",attachment:"附件",code:"代码",system:"系统"};
      const chars=Array.from(event.text);
      body.replaceChildren();
      for(const part of source.sections) {
        el("small",kinds[part.kind] || part.kind,body).className="import-section-label";
        body.append(document.createTextNode(chars.slice(part.start-source.start,part.end-source.start).join("")));
      }
    }
    if(!s.readOnly && event.role!=="note" && event.referenceable)button(box,"引用这条消息",()=>
      quoteMessage(s,event.message_target||messageTarget(event.id),event.text));
    if(back)button(box,"返回查找结果",back);
  }
  function historySearch(s,query="",rows=[],before=null,loaded=false){
    const box=panel("查找历史消息");
    const input=inputField(box,"消息正文","search",query);
    async function lookup(next=false) {
      const value=input.value.trim();if(!value)throw new FormError("请输入要查找的文字。");
      const cursor=next && value===query?before:null;
      const result=await action("history",{persona_id:currentPerson().id,session_id:s.id,query:value,before:cursor,limit:80});
      if(s===currentSession() && box.isConnected) historySearch(s,value,result.rows,result.before,true);
    }
    button(box,"查找",()=>lookup());
    input.onkeydown=e=>{if(e.key==="Enter" && !e.isComposing){e.preventDefault();run(lookup)();}};
    if(loaded){
      if(!rows.length)el("p",before?"当前范围没有匹配，可继续向前查找。":"此范围没有匹配消息。",box);
      for(const row of rows){
        const label=(row.source?.speaker || (row.role==="user"?"本人":"对方"))+" · "+row.text.slice(0,100);
        button(box,label,()=>historyDetails(s,row,()=>historySearch(s,query,rows,before,true)));
      }
      if(before)button(box,"继续向前查找",()=>lookup(true));
    }
    input.focus();
  }
  function messageTarget(id,index=0){return {kind:"desktop_message",id,index};}
  function quoteMessage(s,target,text){
    if(s!==currentSession()||s.readOnly)return;
    s.reference={...target,text};reviseDraft(s);dialog.close();render();message.focus();
  }
  async function reopenSource(s){
    const person=currentPerson(), branch=s.branch;
    const parent=person.sessions.find(item=>item.id===branch.parent_session_id);
    if(parent?.lifecycle==="active" && parent.id!==person.writableSession){
      s.parentOperation||=newId();
      await api().live_activate(person.id,parent.id,s.parentOperation);
    }
    if(s!==currentSession())return;
    let mark=null;
    try{mark=await api().live_message_position(person.id,branch.parent_session_id,branch.anchor);}
    catch{notify("原消息已不可引用，已打开原会话。");}
    if(s!==currentSession())return;
    await select(person,false,true,branch.parent_session_id);
    const now=currentSession();
    if(mark && now?.id===branch.parent_session_id){
      now.restore=mark;now.version=null;now.historyReady=false;await refresh();
    }
  }
  function messageActions(node,target,text){
    const parent=node.parentNode,s=currentSession(),person=currentPerson();
    if(!active||!s?.id||!person||!target.id)return;
    const row=document.createElement("div");
    row.className="message-with-menu"+(node.classList.contains("own")?" own-message":"");
    parent.replaceChild(row,node);row.append(node);
    let pressed=false,timer=null,start=null;
    function cancelPress(){clearTimeout(timer);timer=null;start=null;}
    function open(){
      if(currentSession()!==s||currentPerson()?.id!==person.id)return;
      const box=panel("消息操作");
      if(!s.readOnly)button(box,"引用这条消息",()=>quoteMessage(s,target,text));
      const operation=newId();
      button(box,"从这里开启分支",async()=>{
        if(s!==currentSession())return;
        const result=await api().live_branch(person.id,s.id,target,operation);
        if(s!==currentSession())return;
        await select(person,false,true,result.session_id);
      });
    }
    const menu=button(row,"⋯",open);
    menu.className="message-menu";
    menu.setAttribute("aria-label","消息操作");menu.title="消息操作";
    menu.dataset.messageMenu="";
    node.addEventListener("contextmenu",e=>{e.preventDefault();cancelPress();if(!pressed)open();});
    node.addEventListener("pointerdown",e=>{
      if(e.pointerType!=="touch" && e.pointerType!=="pen")return;
      pressed=false;start={x:e.clientX,y:e.clientY};
      timer=setTimeout(()=>{cancelPress();pressed=true;open();},500);
    });
    node.addEventListener("pointermove",e=>{if(start&&Math.hypot(e.clientX-start.x,e.clientY-start.y)>12)cancelPress();});
    for(const name of ["pointerup","pointercancel","pointerleave"])node.addEventListener(name,cancelPress);
    node.addEventListener("click",e=>{if(pressed){pressed=false;e.preventDefault();e.stopImmediatePropagation();}},true);
  }
  function turnNode(t){
    const group=document.createElement("div");group.style.display="contents";
    const show=(role,text,id,time,index=0,menu=true)=>{
      const node=stamp(makeBubble(group,role,text,time),"native",index?id+"/"+index:id,t.position);
      if(t.status==="completed" && menu)messageActions(node,messageTarget(id,index),text);
      return node;
    };
    for(const m of t.input_members||[])if(m.text)show("user",m.text,m.id,m.timestamp);
    const plans=t.desktop_publications||[];
    const notices=plans.filter(v=>v.phase==="search_notice");
    if(t.search_notice&&!notices.some(v=>v.kind==="speech"))show("persona",t.search_notice.text,t.search_notice.event_id,t.search_notice.timestamp);
    else for(const plan of notices)for(const m of plan.visible)show("persona",m.text,m.id,m.receipt_id?{recorded_at:m.presented_at,source:"presented"}:null);
    const main=plans.find(v=>v.phase==="main");
    const awaitingNotice=notices.some(plan=>plan.background_lookup && ["pending","partial"].includes(plan.state));
    if(t.inline_reply && !awaitingNotice){
      const seen=new Map((main?.visible||[]).map(item=>[item.id,item]));
      for(const item of t.inline_reply.items){
        const prior=seen.get(item.id);
        const time=prior?.receipt_id?{recorded_at:prior.presented_at,source:"presented"}:
          {recorded_at:t.inline_reply.received_at,source:"reply_received"};
        show("persona",item.text,item.id,time,0,Boolean(prior?.receipt_id)).dataset.originalFragment=item.id;
      }
    }else if(main){for(const m of main.visible)show("persona",m.text,m.id,m.receipt_id?{recorded_at:m.presented_at,source:"presented"}:null);}
    else if(t.status==="completed" && t.reply){
      const parts=t.display_bubbles||[t.display_reply||t.reply];
      for(const [index,part] of parts.entries())show("persona",part,t.reply_event_id,
        parts.length===1?{recorded_at:t.reply_recorded_at,source:"event_recorded"}:null,index);
    }
    if(!state.community && t.feedback_available)button(group,"这条不像",()=>feedback(currentPerson().id,t.id));
    if(t.lookup_confirmation)button(group,"确认本轮查找范围",async()=>{
      const pid=currentPerson().id,s=currentSession(),box=panel("本轮查找范围");
      const proposed=t.lookup_target_status==="needs_public_scope";
      const invalid=t.lookup_target_status==="invalid";
      const clarify = proposed || invalid || t.lookup_target_status==="ambiguous" || t.lookup_target_status==="needs_consent";
      el("p",proposed?"这是按语境找到的查找目标。确认或编辑后，仅将此次确认的内容用于本轮公开查找。":
        invalid?"这次没能定位要查的资料。可以填入对象和想核对的内容，或跳过查找继续聊天。":
        clarify?"你指的是哪个公开对象？可以在这里补充查找目标。":"选取本轮消息中允许发给搜索服务的原文，也可以跳过本次查找。",box);
      const field=el("textarea",null,el("label",clarify?"公开查找目标":"查找片段",box));
      if(proposed)field.value=t.lookup_proposal||"";
      if(!clarify)for(const m of t.input_members||[])button(box,"选用 · "+m.text.slice(0,36),()=>{field.value=m.text;field.focus();});
      async function resume(text){
        if(currentPerson()?.id!==pid||currentSession()!==s)return;
        await api().live_lookup_confirm(pid,s.id,t.id,t.lookup_confirmation,newId(),text);
        dialog.close();s.version=null;arm(0);
      }
      button(box,"确认并继续本轮",()=>resume(field.value.trim()));
      button(box,"跳过本次查找",()=>resume(null));
    });
    if(t.role_long_detail)turnDetailButton(group,t,"long","查看角色长文");
    if(!state.community)turnResearchActions(group,t);
    if(["queued","running"].includes(t.status))button(group,"停止本轮",()=>api().live_cancel(t.id));
    if(t.error_code && !(t.inline_reply && t.error_code==="desktop_presentation_unknown")){
      const labels={upstream_error:"本轮模型请求失败。",upstream_no_reply:"模型超过 180 秒未回复。",desktop_network_wait:"网络暂时不可用，消息已保存，连接恢复后会自动继续。",service_restarted:"本轮已中断，原结果待核对，可以继续聊天。",
        desktop_storage_wait:"消息已保存，正在等待本机恢复写入。",
        local_knowledge_store_unavailable:"本机知识资料暂时无法读取，本轮消息已保留。",
        local_knowledge_embedding_unavailable:"本机语义检索模型暂时不可用，本轮消息已保留。",
        knowledge_study_failed:state.community?"资料已查回，整理请求失败。":"资料已查回，整理请求失败，可查看本轮资料。",
        knowledge_study_unknown:"资料已查回，整理请求的回执待核对。",
        knowledge_study_input_too_large:state.community?"资料已查回，整理模型本次可接收的空间不足。":"资料已查回，整理模型本次可接收的空间不足。可查看本轮资料。",
        desktop_connection_changed:"等待期间模型连接已更换，本轮已停止，原消息仍可查看。",
        desktop_reply_superseded:"已接收新消息，上一轮发言已结束。",
        knowledge_repeated_notice:state.community?"资料已查回，本轮回复重复了查前提示。":"资料已查回，本轮回复重复了查前提示。可以打开检索资料。",
        reply_bubble_limit:"本轮发言因气泡超限已停止。",
        reply_bubble_detail_required:"本轮含有无法在气泡内完整显示的内容。",
        reply_decision_required:"本轮尚未取得可用的接话决策。",
        reply_review_pending:"本轮已生成草稿，等待独立复核。",
        private_lookup_confirmation_required:"请确认这条消息的查找范围。",
        lookup_target_invalid:"检索对象未能对应到本会话的来源，本轮查找已停止。",
        lookup_target_input_budget_exceeded:"本轮语境超过检索对象模型的输入容量。",
        private_lookup_scope_changed:"本轮资料授权已变化。",
        search_notice_not_fully_presented:"查前发言尚未确认显示。",
        private_reply_review_rejected:"本轮修复后仍未通过复核，可以继续聊天。",
        lookup_reply_format_invalid:"正文已生成，格式未能处理。",
        private_reply_model_changed:"本轮正文因模型连接更换而中断。",
        private_chat_review_version_changed:"本轮绑定的回复装配已更换，处理已停止。",
        lookup_reply_citation_invalid:"已收到回复，正文引用无法与资料对上。",
        lookup_reply_unbound:"已收到回复，其中的查证说法缺少本轮依据。",
        private_reply_reference_unavailable:"引用资料已变化，本轮已停止。",
        private_reply_source_changed:"本轮资料已变化，请根据当前对话继续。",
        private_reply_transport_unavailable:"候选回复服务待接入，本轮已保存。",
        private_reply_candidate_retired:"本实例已停用候选回复服务，本轮消息已保存。",
        reply_dispatch_unknown:"本轮发送结果待核对，原请求已保留。",
        scene_superseded:"已接收新消息，上一轮发言已结束。",
        search_notice_superseded:"已接收新消息，上一轮查找已停止。",
        desktop_presentation_unknown:"这段发言的显示结果待核对，可以继续聊天。",
        scene_presentation_unknown:"这段发言的显示结果待核对，可以继续聊天。",
        desktop_presentation_unavailable:"暂时无法确认这段发言，可以继续聊天。",
        desktop_settlement_pending:"回复已收到，正在等待本机保存。",
        desktop_receipt_requires_review:"回复已收到，来源待核对。",
        desktop_source_withdrawn:"相关资料已撤回，本轮已停止。",
        desktop_instance_changed:"原消息属于其他实例，本轮已停止。"};
      const auth=["provider_http_401","provider_http_403","model_not_configured"].includes(t.error_code);
      const limited=t.error_code==="provider_http_429";
      const result=t.research_progress;
      const noScope=result?.tool==="not_dispatched" && result.coverage!=="complete" &&
        ["ambiguous","invalid","input_budget"].includes(result.target_status);
      const stoppedAfterSearch=result?.tool==="acquired" &&
        ["private_reply_model_changed","private_chat_review_version_changed"].includes(t.error_code);
      const interruptedAfterSearch=t.error_code==="private_reply_model_changed"?
        "本轮已查回资料，正文因模型连接切换而中断。":"本轮已查回资料，正文因回复版本更换而中断。";
      el("small",stoppedAfterSearch?interruptedAfterSearch:
        t.lookup_scope_unresolved||noScope?"这轮没能确定查找范围，搜索没有发起。":
        auth?"本轮的模型连接未通过验证。":limited?"模型服务暂时限流，本轮已停止。":
        labels[t.error_code]||(/unknown|unconfirmed/.test(t.error_code)?"本轮结果待核对，可以继续聊天。":"本轮未完成，原消息已保留。"),group);
      if(t.stage_retry){
        const pid=currentPerson().id, session=currentSession(), version=t.stage_retry.version, op=newId();
        button(group,"重试",async()=>{
          if(currentPerson()?.id!==pid || currentSession()!==session)return;
          const result=await api().live_stage_retry(pid,session.id,t.id,version,op);
          if(result.state==="awaiting_receipt")notify("正在核对原请求的结果。");
          session.version=null;arm(0);
        });
      }
    }else if(["queued","running"].includes(t.status)){
      const studyPhase={background_search_notice_saved:"查前消息已保存，正在处理查询",research_assessing:"正在核对已有资料能否回答本轮问题",
        research_preparing_notice:"已有资料还不够，正在准备补查",
        knowledge_selection:"工具已返回，正在核对资料",knowledge_no_matches:"本轮未取得可回答的新资料",knowledge_studying:"正在整理查到的资料",knowledge_study_received:"正在核对资料出处",knowledge_study_committed:"资料已保存，正在重新检索",
        knowledge_study_insufficient:"已查回资料，正在准备回复",knowledge_study_read:"已完成本地检索，正在准备回复"};
      const renew=t.research_quota?.next_epoch*1000;
      const quotaLabel=Number.isFinite(renew)&&renew>Date.now()?"额度预计 "+new Date(renew).toLocaleTimeString("zh-CN",{hour:"2-digit",minute:"2-digit"})+" 恢复":"正在等待模型调用额度";
      const stage=state.community?"":studyPhase[t.phase]||
        (t.phase==="knowledge_search_notice"?"查前消息显示后会自动开始搜索":"");
      el("small",t.research_quota?quotaLabel:stage||(t.phase==="knowledge_waiting_quota"?"正在等待可用额度":
        t.status==="queued"?"已受理，正在等待回复":"正在回复"),group);
    }
    return group;
  }
  function turnDetailButton(group,t,kind,title){
    button(group,title,async()=>{
      const pid=currentPerson().id,s=currentSession();
      const data=await api().live_reply_detail(pid,s.id,t.id,kind);
      const citations=kind==="research"&&t.used_sources?await api().live_reply_detail(pid,s.id,t.id,"citations"):[];
      if(currentPerson()?.id!==pid||currentSession()!==s)return;
      const box=panel(title);
      if(citations.length){
        const used=el("details",null,box);
        el("summary","这条回复引用的原文",used);
        for(const item of citations){
          el("p",item.claim,used);el("blockquote",item.quote,used);
          el("small",[item.title,item.url].filter(Boolean).join(" · "),used);
        }
      }
      if(kind==="research" && t.public_lookup){
        const names={native:"原生检索",search:"公开搜索",fetch:"读取网页"};
        const states={received:"已返回",sending:"等待回执",unsent:"确认未发送",unknown:"结果待核对",failed:"失败"};
        for(const call of t.public_lookup.calls)el("small",names[call.step]+" · "+states[call.state]+
          (call.results!=null?" · "+call.results+" 条来源":""),box);
        if(data.question)el("p","公开查询 · "+data.question,box);
        if(t.public_lookup.outcome==="partial_evidence")el("p","本轮资料只覆盖了问题的一部分。",box);
        if(data.updated_at)el("p","资料核验于 "+new Date(data.updated_at).toLocaleString(),box);
      }
      if(data.queries){
        const coverage={complete:"可答",partial:"部分可答",missing:"缺资料",unconfirmed:"判断未完成",clarify:"所指待明确"};
        const tools={not_dispatched:"仅查本地",acquired:"补查已取得资料",empty:"补查未取得资料",failed:"补查失败"};
        for(const query of data.queries){
          el("p",query.text,box);
          el("small",[coverage[query.result],tools[query.tool],query.sources.length?"来源 "+query.sources.join("、"):""].filter(Boolean).join(" · "),box);
        }
      }else{
        el("p",kind==="research"?data.notes:data.body,box).style.whiteSpace="pre-wrap";
      }
      for(const [index,source] of (data.sources||[]).entries()){
        const item=el("details",null,box);el("summary",data.queries?(index+1)+" · "+source.title:source.title,item);
        el("small",[source.date,source.url].filter(Boolean).join(" · "),item);
        el("p",source.content||source.summary||"",item).style.whiteSpace="pre-wrap";
        if(source.qualification)el("small",source.qualification,item);
      }
      if(data.original){
        const old=el("details",null,box);
        el("summary","查看本轮当时的修订",old);
        el("p",data.original.question+" · "+new Date(data.original.at).toLocaleString(),old);
        for(const source of data.original.sources){
          el("small",[source.title,source.date,source.url].filter(Boolean).join(" · "),old);
          el("p",source.text,old);
          if(source.qualification)el("small",source.qualification,old);
        }
      }
    });
  }
  function turnResearchActions(group,t){
    if(t.retained_lookup)el("small","本轮沿用了上轮查回的资料。",group);
    if(t.research_detail)turnDetailButton(group,t,"research","查看本轮查找资料");
    if(t.has_retrieved_notes)button(group,"查看本轮检索资料",async()=>{
      const pid=currentPerson().id,sid=currentSession().id;
      const material=await api().live_retrieved_notes(pid,sid,t.id);
      if(currentPerson()?.id!==pid||currentSession()?.id!==sid)return;
      const box=panel("本轮检索资料");
      const notes=el("p",material.notes,box);notes.style.whiteSpace="pre-wrap";
      const sources=el("details",null,box);el("summary","资料来源（"+material.sources.length+"）",sources);
      for(const source of material.sources)el("p",[source.title,source.url].filter(Boolean).join(" · "),sources);
    });
  }
  const clocks = new Map();
  function markTime(node, time) {
    const at=time?.recorded_at;
    const date=at?new Date(at):null;
    if(!date || !Number.isFinite(date.getTime())){
      node.dataset.clock="时间待核";
      node.title="时间待核";
      return;
    }
    const precision=time.precision || "second";
    const source={received:"受理",presented:"显示确认",reply_received:"回复生成",imported:"来源",event_recorded:"事件记录"}[time.source];
    if(!clocks.has(precision)){
      const options=precision==="day"?{dateStyle:"short"}:
        {hour:"2-digit",minute:"2-digit",hourCycle:"h23",...(precision==="minute"?{}:{second:"2-digit"})};
      clocks.set(precision,new Intl.DateTimeFormat("zh-CN",options));
    }
    const local=clocks.get(precision).format(date);
    node.dataset.clock=(time.source==="event_recorded"?"记录 ":"")+local;
    const original=precision==="minute"?at.replace(/(:\d\d):00(?=Z|[+-]\d\d:\d\d$|$)/,"$1"):at;
    node.title=source+"时间 · "+date.toLocaleDateString("zh-CN")+" "+local+" · "+
      clocks.get(precision).resolvedOptions().timeZone+" · "+original;
    node.setAttribute("aria-description",node.title);
  }
  function makeBubble(parent,role,text,time){
    const node=time?.recorded_at?button(parent,text,()=>{
      const box=panel("消息时间");
      el("p",node.title,box);
    }):el("p",text,parent);
    node.className="bubble "+(role==="user"?"own":"reply");
    if(time!==undefined)markTime(node,time);
    return node;
  }
  function updateTurnNodes(s,cards){
    const thread=timeline.querySelector(".conversation-inner");
    if(!thread || drawnSession!==s || !s.turns.length)return false;
    if(renderedTurns.size!==s.turns.length || cards.some(t=>!renderedTurns.has(t.id)))return false;
    for(const t of cards){
      const next=turnNode(t);
      renderedTurns.get(t.id).replaceWith(next);renderedTurns.set(t.id,next);
    }
    return true;
  }
  function draw(s){
    drawnSession=s;
    renderedTurns.clear();
    timeline.classList.remove("first");timeline.replaceChildren();
    const thread=el("div",null,timeline);thread.className="conversation-inner";
    const reference=byId("history-reference");
    reference.hidden=!s.reference || s.readOnly;
    reference.querySelector("span").textContent=s.reference?(s.reference.unavailable?"引用已失效，请移除或重新选择。":
      "引用 · "+(s.reference.text||"").slice(0,180)):"";
    if(s.branch)button(thread,"返回原消息",()=>reopenSource(s)).className="inline-actions history-actions";
    if(s.readOnly && s.lifecycle==="active")button(thread,"在此会话继续",async()=>{
      const person=currentPerson();s.activateOperation||=newId();
      await api().live_activate(person.id,s.id,s.activateOperation);
      if(s===currentSession())await select(person,false,true,s.id);
    }).className="inline-actions history-actions";
    if(s.past.length){
      const actions=el("div",null,thread);actions.className="inline-actions history-actions";
      button(actions,"查找历史消息",()=>historySearch(s));
      if(!s.branch)button(actions,"历史来源与用途",()=>historySettings(s));
    }
    if(s.before)button(thread,"查看更早的消息",async()=>{
      const before=s.before,revision=s.historyRevision;
      const result=await action("history",{persona_id:currentPerson().id,session_id:s.id,before,limit:80});
      if(s!==currentSession()||s.before!==before||s.historyRevision!==revision)return;
      const mark=capture(),merged=[...result.rows,...s.past];
      s.past=merged.slice(0,160);s.before=result.before;
      if(merged.length>160)s.pastNewer=s.past.at(-1).seq;
      render();if(mark)restoreReading(mark);
    });
    if(s.pastNewer)button(thread,"查看较新的导入消息",async()=>{
      const after=s.pastNewer,revision=s.historyRevision;
      const result=await action("history",{persona_id:currentPerson().id,session_id:s.id,after,limit:80});
      if(s!==currentSession()||s.pastNewer!==after||s.historyRevision!==revision)return;
      const merged=[...s.past,...result.rows];s.past=merged.slice(-160);s.pastNewer=result.after;
      if(merged.length>160)s.before=s.past[0].seq;
      render();
    });
    let day=null;
    for(const event of s.past){
      const sourceDate=event.created_at?new Date(event.created_at):null;
      const nextDay=sourceDate && Number.isFinite(sourceDate.getTime())?sourceDate.toLocaleDateString("zh-CN"):"日期待核";
      if(nextDay!==day){el("div",nextDay+" · 导入历史",thread).className="date-divider";day=nextDay;}
      const node=button(thread,event.text,()=>historyDetails(s,event));
      stamp(node,"import",event.id,event.seq);
      node.className=event.role==="note"?"button ghost history-actions":"bubble "+(event.role==="user"?"own":"reply");
      markTime(node,event.timestamp);
      node.title="查看来源与引用 · "+node.title;
      if(event.role!=="note" && event.referenceable)messageActions(node,event.message_target||messageTarget(event.id),event.text);
    }
    if(s.older)button(thread,"查看更早的对话",()=>nativePage(s,true));
    for(const t of s.turns){
      const node=turnNode(t);renderedTurns.set(t.id,node);thread.append(node);
    }
    if(s.newer)button(thread,"查看较新的对话",()=>nativePage(s,false));
    const held=s.inputBuffer?.batch?.members||[];
    const heldIds=new Set(held.map(item=>item.request_id));
    for(const item of held)makeBubble(thread,"user",item.text,{recorded_at:item.received_at,source:"received"}).dataset.awaitingConfirmation="";
    for(const item of s.outbox||[]){
      if(heldIds.has(item.id))continue;
      const own=makeBubble(thread,"user",item.text,item.received_at?{recorded_at:item.received_at,source:"received"}:undefined);
      own.dataset.submissionState=item.state;
      const labels={accepted:"已受理，正在等待回复",waiting_capacity:"已受理，正在等待空闲位置。",blocked:"本条消息待核对，可以继续聊天。",
        stopped:"已保留原消息，本轮已停止。",unconfirmed:"正在核对发送结果"};
      if(item.awaiting_confirmation)own.dataset.awaitingConfirmation="";
      else el("small",labels[item.state],thread);
    }
    if(!thread.childElementCount)el("p","可以发送第一条消息。",thread);
  }
  const bufferPanel=byId("input-buffer"),bufferStatus=byId("input-buffer-status");
  const privacyButton=byId("session-privacy");
  const recipientLabels={main:"主回复",organization:"后台整理",selector:"选材",jev:"Jev 决策",lookup_target:"检索对象解析",native_web:"原生联网模型",search_fetch:"联网检索与抓取"};
  async function privacyPanel(){
    const person=currentPerson(),session=currentSession();
    if(!person || !session)return;
    const sameSession=()=>person.id===currentPerson()?.id && session.id===currentSession()?.id;
    const state=await api().live_privacy(person.id,session.id);
    if(!sameSession())return;
    const box=panel("本会话的隐私授权");
    const mode=state.enabled?(state.candidate?"已开启全流程授权":"已登记授权，将用于新版回复链"):
      state.registered?"授权绑定已变化，当前使用标准模式":"当前使用标准模式";
    el("p",mode,box);
    const singleRetrieval=Number.isInteger(state.retrieval_history_turns);
    el("p",singleRetrieval
      ? `开启后，原双人私聊链按原授权使用人物和会话资料。检索取材由官方 DeepSeek Flash 完成，读取本轮原话、获准引用和本会话最近最多 ${state.retrieval_history_turns} 个可读回合。判断材料时接收本地原文。公开补查另由下方授权控制。`
      : "此授权供新版双人私聊链使用。开启后，本会话已有和新增的原话、人物资料、引用、导入记录、记忆及文件内容均可交给以下接收方。联网查询可以包含私人信息。你可以随时关闭。",box);
    for(const [role,url] of Object.entries(state.recipients))el("p",[recipientLabels[role],state.models[role],url].filter(Boolean).join(" · "),box);
    const context=state.public_lookup;
    if(state.enabled && state.candidate && (context?.available || context?.enabled)){
      const automatic=context.protocol==="desktop-public-lookup-scope-v3";
      const automaticText=singleRetrieval
        ? "先按译问查本地。开启后，只把有公开依据、仍缺资料的查询交给联网接收方，取得资料后回到本地库再判断。"
        : "开启后，直接发送消息即可。模型判断是否需要查证，并用当前消息中选出的公开对象、待查属性和时效片段组成查询。补查也可沿用本会话中已核对的公开主题。查询片段会发给上方的联网接收方。";
      if(singleRetrieval && context.semantic_review)el("p","涉及别名、改述或私聊线索时，官方 DeepSeek Flash 可用本轮相关原话和必要依据核对哪些译问可以外发。",box);
      el("p",automatic?automaticText:"语境补查使用本会话已核对的公开主题，核准的公开对象、待查属性和时效会发给上方的联网接收方。",box);
      async function saveContext(scope,enabled){
        if(!sameSession())return;
        await api().live_privacy(person.id,state.session_id,{enabled,operation_id:newId(),
          revision:scope.revision,protocol:scope.protocol,recipient_hash:scope.recipient_hash});
        if(sameSession() && dialog.contains(box)){dialog.close();arm(0);}
      }
      const label=automatic?(context.enabled?"关闭自动联网查证":"同意本会话自动联网查证"):
        (context.enabled?"关闭语境补查授权":"同意本会话按语境补查公开资料");
      button(box,label,()=>saveContext(context,!context.enabled));
      if(context.upgrade){
        el("p",automaticText,box);
        button(box,"同意本会话自动联网查证",()=>saveContext(context.upgrade,true));
      }
    }
    async function save(enabled){
      if(!sameSession())return;
      await api().live_privacy(person.id,state.session_id,{enabled,operation_id:newId(),revision:state.revision,
        protocol:state.protocol,recipient_hash:state.recipient_hash});
      if(!sameSession() || !dialog.contains(box))return;
      dialog.close();
      if(!session.id)await reload(person.id,false,state.session_id);
      else arm(0);
    }
    button(box,state.enabled||state.registered?"关闭并使用标准模式":"使用标准模式",()=>save(false));
    if(!state.enabled && Object.keys(state.recipients).length)button(box,"同意并开启本会话全流程授权",()=>save(true));
  }
  privacyButton.onclick=run(privacyPanel);
  const bufferSettings=byId("input-buffer-settings"),bufferConfirm=byId("input-buffer-confirm"),bufferCancel=byId("input-buffer-cancel");
  const bufferModes={legacy_per_message:"逐条发送",natural_batch:"定时缓冲",manual:"无限缓冲"};
  const modeOf=settings=>settings.manual?"manual":settings.mode;
  const manualReady=()=>Boolean(active && currentSession()?.inputBuffer?.manual &&
    currentSession()?.inputBuffer?.batch?.members.length && !bufferPanel.hidden &&
    !bufferConfirm.hidden && !bufferConfirm.disabled && !message.disabled);
  function updateManualHint(){
    const s=currentSession();
    const hint=manualReady() && document.activeElement===message && !composing && !message.value.trim();
    message.placeholder=hint?"再次按下回车即可发送":s?.readOnly?"已封存的会话可供查阅":
      ready()?"输入消息":"准备完成后即可输入消息";
  }
  message.addEventListener("focus",updateManualHint);
  message.addEventListener("blur",updateManualHint);
  message.addEventListener("compositionstart",updateManualHint);
  message.addEventListener("compositionend",updateManualHint);
  function renderInputBuffer(){
    const s=currentSession(), settings=s?.inputBuffer, batch=settings?.batch;
    privacyButton.hidden=!active||!ready()||!s;
    bufferPanel.hidden=!active||!ready()||!settings||s.readOnly;
    if(bufferPanel.hidden){updateManualHint();return;}
    const mode=modeOf(settings),count=batch?.members.length||0;
    const label=count?count+" 条待确认":mode==="natural_batch"?`定时缓冲 · ${settings.quiet_ms/1000} 秒`:bufferModes[mode];
    if(bufferStatus.textContent!==label)bufferStatus.textContent=label;
    bufferConfirm.hidden=bufferCancel.hidden=!batch;
    const busy=stopped||linkState!=="online"||selectionToken!==null||s.sending||s.bufferWorking;
    const unchecked=s.outbox?.some(item=>item.state==="unconfirmed"||item.awaiting_confirmation);
    bufferSettings.disabled=busy||unchecked;
    bufferConfirm.disabled=busy||unchecked||!state.connected;
    bufferCancel.disabled=busy||unchecked;
    updateManualHint();
  }
  function manualEnter(repeat){
    if(!active || !currentSession()?.inputBuffer?.manual)return false;
    if(repeat)return true;
    if(message.value.trim())return false;
    if(!composing && document.activeElement===message && manualReady())run(()=>finishBuffer("confirm"))();
    return true;
  }
  async function editInputBuffer(){
    const person=currentPerson(),s=currentSession(),settings=s?.inputBuffer;
    if(!settings||bufferSettings.disabled)return;
    const box=panel("输入缓冲");
    const modeField=el("label","发送方式",box);modeField.className="field";
    const mode=el("select",null,modeField);
    for(const [value,label] of Object.entries(bufferModes))mode.add(new Option(label,value));
    mode.value=modeOf(settings);
    const duration=el("label","等待秒数",box);duration.className="field";
    const seconds=el("input",null,duration);
    seconds.type="number";seconds.min="1";seconds.max="3600";seconds.step="1";seconds.value=settings.quiet_ms/1000;
    const help=el("p",null,box);
    const explain=()=>{
      duration.hidden=mode.value!=="natural_batch";
      help.textContent={
        legacy_per_message:"每次发送都会提交一条消息。",
        natural_batch:"从最后一条消息起计时，时间到了自动一起发送。",
        manual:"点击「加入本轮」暂存消息。点击「确认发送本轮」后，才会一起发送。"
      }[mode.value];
    };
    mode.onchange=explain;explain();
    if(settings.batch)el("p","请先确认发送或取消当前缓冲的消息，再更改方式。",box);
    const opByChoice=new Map();
    const save=button(box,"保存",async()=>{
      if(s!==currentSession())return;
      if(mode.value==="natural_batch" && (!seconds.value||!seconds.validity.valid))
        throw new FormError("请输入 1 到 3600 之间的整数秒数。");
      const manual=mode.value==="manual";
      const selected=manual?"natural_batch":mode.value;
      const quiet=mode.value==="natural_batch"?Number(seconds.value)*1000:settings.quiet_ms;
      const key=JSON.stringify([selected,quiet,manual]);
      if(!opByChoice.has(key))opByChoice.set(key,newId());
      s.bufferRevision=(s.bufferRevision||0)+1;
      s.bufferWorking=true;updateSendButton();
      try{
        const policy=await api().live_set_buffer(person.id,selected,quiet,manual,settings.version,opByChoice.get(key));
        s.inputBuffer={version:policy.id,mode:policy.mode,quiet_ms:policy.buffer_policy.quiet_ms,
          manual:policy.mode==="natural_batch"&&Boolean(policy.buffer_policy.manual_confirmation),batch:null};
        dialog.close();
      }finally{s.bufferWorking=false;updateSendButton();arm(0);}
    });
    save.classList.add("primary");save.disabled=Boolean(settings.batch);
  }
  async function finishBuffer(action){
    const person=currentPerson(),s=currentSession(),batch=s?.inputBuffer?.batch;
    const control=action==="confirm"?bufferConfirm:bufferCancel;
    if(!batch||control.disabled)return;
    if(action==="cancel"&&!confirm("取消本轮？暂存的消息将从聊天窗口移除。"))return;
    const key=JSON.stringify([batch.id,batch.revision,action]);
    if(s.bufferIntent?.key!==key)s.bufferIntent={key,id:newId()};
    s.bufferRevision=(s.bufferRevision||0)+1;
    s.bufferWorking=true;updateSendButton();
    try{
      const result=await api().live_batch_action(person.id,s.id,batch.id,batch.revision,s.bufferIntent.id,action);
      s.inputBuffer={...s.inputBuffer,batch:null};
      s.outbox=(s.outbox||[]).filter(item=>!item.awaiting_confirmation);
      if(action==="confirm"&&!s.turns.some(turn=>turn.id===result.turn_id)){
        const status=result.status==="sealed_waiting_capacity"?"waiting_capacity":"accepted";
        s.outbox.push(...batch.members.map(item=>({id:item.request_id,text:item.text,state:status,turn_id:result.turn_id})));
      }
      if(s===currentSession())render();
    }finally{s.bufferWorking=false;updateSendButton();arm(0);}
  }
  bufferSettings.onclick=run(editInputBuffer);
  bufferConfirm.onclick=run(()=>finishBuffer("confirm"));
  bufferCancel.onclick=run(()=>finishBuffer("cancel"));
  byId("long-reply-send").addEventListener("click",()=>run(()=>send(true))());
  async function send(longRequested=false){
    const p=currentPerson(),s=currentSession(),value=message.value;
    if(stopped||!p||!s||s.readOnly||!value.trim()||s.sending||s.bufferWorking||selectionToken!==null||pendingDraft(s)||linkState!=="online")return;
    if(!state.connected){connection();return;}
    if(s.reference?.unavailable)throw new FormError("引用已失效，请移除或重新选择。");
    s.bufferRevision=(s.bufferRevision||0)+1;
    s.sending=true;updateSendButton();
    const ref=s.reference?.id || null;
    const request=newId(), revision=s.draftRevision ||= newId();
    try{
      const policy=s.inputBuffer?{version:s.inputBuffer.version}:null;
      const choice=ref?(s.reference.kind==="desktop_message"?messageTarget(ref,s.reference.index):{kind:"event",id:ref}):null;
      const args=[p.id,s.id,value,request,choice,revision,false,policy];
      if(longRequested)args.push(true);
      const accepted=await api().live_submit(...args);
      if(accepted.state==="rejected")throw new FormError(bufferErrors[accepted.reason]);
      if(!s.outbox?.some(item=>item.id===accepted.id))s.outbox=[...(s.outbox||[]),accepted];
      if(accepted.input_buffer && accepted.session_id===s.id){
        s.inputBuffer=accepted.input_buffer;
        const held=new Set(s.inputBuffer.batch.members.map(item=>item.request_id));
        s.outbox=s.outbox.filter(item=>!held.has(item.id));
      }
      let clearing=null;
      if(acceptedSubmission(accepted) && s.draftRevision===revision && (s.reference?.id||null)===ref){
        s.draft="";s.draftRevision=newId();s.reference=null;
        clearing=saveDraft(s,p.id,revision);
      }
      s.version=null;
      if(s===currentSession())render({scrollToEnd:true});
      if(clearing)await clearing;
      if(!s.id && accepted.session_id && s===currentSession()){
        await reload(p.id,false,accepted.session_id);
        return;
      }
    }finally{s.sending=false;updateSendButton();arm(0);}
  }
  async function feedback(pid,tid){
    const samples=await action("feedback",{persona_id:pid,turn_id:tid});
    const box=panel("本轮发言参考");
    for(const sample of samples){
      el("p",sample.text,box);
      for(const [mode,label] of [["demote","同类语境降低排序"],["disable","停用这条样例"]])
        button(box,label,async()=>{await action("feedback",{persona_id:pid,turn_id:tid,mode,sample:sample.id});dialog.close();});
    }
    button(box,"记录本轮待复核",async()=>{await action("feedback",{persona_id:pid,turn_id:tid,mode:"review"});dialog.close();});
  }
  async function editPublishedScenes(scene) {
    const data=await action("status",{ident:scene.document});
    const job=await action("card_status",{ident:data.id});
    if(data.status==="revoked" || !job.cards?.[scene.role] || job.plan_id!==scene.plan_id ||
       job.source_revision!==scene.source_revision || data.revision!==scene.source_revision)
      throw new FormError("当前来源已变化，请重新核对角色资料。");
    const card=job.cards[scene.role],box=panel("编辑来源场景 · "+card.spec.name);
    const read=sceneMaterial(box,card);
    async function write(material){
      return action("card_edit",{ident:data.id,revision:data.revision,card_revision:job.revision,
        plan_id:data.plan.id,role:scene.role,card:material});
    }
    async function saveProposal(issues,fill){
      const proposal=fill({...card,...read()},issues);
      if(proposal){await write(proposal); await editPublishedScenes(scene);}
    }
    pendingProposals(box,card,issue=>saveProposal(issue,fillProposal),true,
                     items=>saveProposal(items,fillTextProposals));
    button(box,"暂存并返回人物资料",async()=>{await write({...card,...read()});await profile();});
  }
  const personaImages=new Map();
  const imageKey=(pid,kind)=>JSON.stringify([pid,kind]);
  function getPersonaImage(pid,kind){
    const key=imageKey(pid,kind);
    if(!personaImages.has(key) && active && api()?.live_persona_image){
      const entry={url:null};
      personaImages.set(key,entry);
      entry.loading=api().live_persona_image(pid,kind).then(url=>{
        if(personaImages.get(key)!==entry)return;
        entry.url=url;
        repaintPersonaImage(pid,kind);
      }).catch(()=>{
        if(personaImages.get(key)===entry)personaImages.delete(key);
      });
    }
    return personaImages.get(key);
  }
  function avatar(node,pid){
    node.dataset.personaAvatar=pid||"";
    const url=pid?getPersonaImage(pid,"avatar")?.url:null;
    if(node.dataset.personaImage===(url||""))return;
    node.dataset.personaImage=url||"";
    if(!url){node.innerHTML=icon("person");return;}
    const image=document.createElement("img");
    image.alt="";
    image.onerror=()=>{if(image.isConnected)node.innerHTML=icon("person");};
    image.src=url;
    node.replaceChildren(image);
  }
  function appearance(pid){
    avatar(byId("chat-avatar"),pid);
    const url=pid && ready()?getPersonaImage(pid,"background")?.url:null;
    timeline.classList.toggle("has-wallpaper",Boolean(url));
    if(url)timeline.style.setProperty("--chat-wallpaper","url("+JSON.stringify(url)+")");
    else timeline.style.removeProperty("--chat-wallpaper");
  }
  function repaintPersonaImage(pid,kind){
    if(kind==="avatar"){
      for(const node of document.querySelectorAll("[data-persona-avatar]")){
        if(node.dataset.personaAvatar===pid)avatar(node,pid);
      }
    }else if(currentPerson()?.id===pid)appearance(pid);
  }
  function personaAppearance(box,pid){
    const pane=groupBox(box,"聊天外观");
    for(const [kind,label] of [["avatar","头像"],["background","聊天背景"]]){
      const slot=el("div",null,pane);
      el("strong",label,slot);
      const preview=el("img",null,slot);
      preview.className="persona-image-preview";
      preview.dataset.kind=kind;
      preview.alt=label+"预览";
      function updatePreview(){
        const url=personaImages.get(imageKey(pid,kind))?.url;
        preview.hidden=!url;
        if(url)preview.src=url;
        else preview.removeAttribute("src");
      }
      updatePreview();
      getPersonaImage(pid,kind)?.loading?.then(updatePreview);
      async function change(method){
        const result=await api()[method](pid,kind);
        if(result.error)throw new FormError(result.error);
        if(!result.changed)return;
        personaImages.set(imageKey(pid,kind),{url:result.url});
        updatePreview();
        repaintPersonaImage(pid,kind);
      }
      button(slot,"选择"+label,()=>change("live_choose_persona_image"));
      button(slot,"恢复默认"+label,()=>change("live_reset_persona_image"));
    }
  }
  async function profile(){
    const person=currentPerson(),pid=person.id,box=panel(person.profile.name);
    if(api()?.live_choose_persona_image){
      const portrait=el("span",null,box);
      portrait.className="avatar";
      avatar(portrait,pid);
      personaAppearance(box,pid);
    }
    for(const [field,label] of Object.entries(titles))el("p",label+" · "+[].concat(person.profile[field]||[]).join("、"),box);
    const memory=await api().live_memory(pid);
    button(box,memory.enabled?"暂停自动记忆":"开启自动记忆并整理历史",async()=>{
      const recipient=memory.provider || {};
      if(!memory.enabled && !confirm("记忆整理模型 · "+(recipient.name || "待配置")+" · "+(recipient.base_url || "待配置")
        +"\n允许该角色使用上述模型整理获准的历史和新对话？后台提取与复核可能产生费用。")) return;
      await api().live_memory(pid,!memory.enabled,memory.version,memory.provider);await profile();
    });
    const scene=await action("card_scene_status",{persona_id:pid});
    if(!box.isConnected) return;
    if(scene){
      const pane=groupBox(box,"已保存的角色卡与场景");
      el("p", scene.enabled ? "场景发言已启用" : scene.reason || "当前使用普通发言",pane);
      if(scene.main && scene.jev_model) el("small","主回复 · "+scene.main+"　场景判断 · "+scene.jev_model,pane);
      if(scene.revoked && scene.affected_fields?.length)
        el("p","来源已撤回，需复核字段 · "+scene.affected_fields.map(pathLabel).join("、"),pane);
      if (scene.common) {el("strong","共通提示",pane); el("p",scene.common,pane).className="import-original";}
      for(const [key,title] of Object.entries(sceneLabels)){
        const slot=groupBox(pane,title);
        el("p",scene.common && scene.scenes[key] ? scene.common+"\n\n"+scene.scenes[key] : "待补",slot).className="import-original";
        if(scene.jev.criteria[key]){
          el("strong","Jev 判别依据",slot);el("p",scene.jev.criteria[key],slot).className="import-original";
        }
      }
      if(scene.jev.instruction) {
        const selection=groupBox(pane,"Jev 总决策说明");
        el("p",scene.jev.instruction,selection).className="import-original";
      }
      if(scene.updated) el("p","来源草稿中的场景已修改，请核对当前版本。",pane);
      if(scene.can_edit_source) button(pane,"编辑来源场景",()=>editPublishedScenes(scene));
      if(scene.enabled || scene.available)button(pane,scene.enabled?"暂停场景发言":"确认并启用场景发言",async()=>{
        await action("card_scene_enable",{persona_id:pid,card_revision:scene.revision,version:scene.version,
          enabled:!scene.enabled,confirmed:true});
        await profile();
      });
    }
    const binding=await action("style",{persona_id:pid});
    if(!box.isConnected||!binding)return;
    el("p",binding.revoked?"风格来源已撤回":binding.enabled?"风格取样已开启":"风格取样已暂停",box);
    const update=async(extra)=>{await action("update",{persona_id:pid,expected_revision:binding.revision,...extra});await profile();};
    if(binding.affected_fields?.length)el("p","需复核来源的字段 · "+binding.affected_fields.map(k=>titles[k]||k).join("、"),box);
    if(!binding.revoked)button(box,binding.enabled?"暂停风格取样":"启用风格取样",()=>update({enabled:!binding.enabled}));
    for(const version of binding.versions.filter(v=>v!==binding.version))
      button(box,"恢复版本 "+version.slice(0,8),()=>update({enabled:true,version}));
    button(box,"采用另一份草稿的风格",async()=>{
      const choices=panel("选择已核对的人物草稿");
      for(const doc of await action("list")){
        if(!["ready","partial_ready"].includes(doc.status))continue;
        const d=await action("status",{ident:doc.id});
        for(const [role,entry] of Object.entries(d.draft))button(choices,entry.spec.name+" · "+doc.created.slice(0,10),
          ()=>update({enabled:true,ident:doc.id,revision:d.revision,role}));
      }
    });
    el("p","待复核及已处理反馈 "+binding.feedback.length+" 条",box);
    button(box,"查看字段与原话依据",async()=>openImport(await action("status",{ident:binding.document}),binding.role));
  }
  function connection(afterSave=null){
    if(state.community||keyLocked())return keySetup();
    const box=panel(state.community?"DeepSeek 官方连接":"模型连接");
    const model=state.community?null:el("select",null,box);
    if(model){
      for(const name of ["deepseek-v4-pro","deepseek-flash"])model.add(new Option(name,name));
      model.value=state.model;
    }
    const key=el("input",null,box);key.type="password";key.placeholder="官方 DeepSeek API Key";
    key.autocomplete="off";
    async function saveConnection(role,address,name,secret){
      const preview=await api().live_connection_change(role,address,name,secret);
      async function commit(){
        await api().live_connection_change(role,address,name,secret,preview.confirmation);
        key.value="";
        if(typeof afterSave==="function") {
          const latest=await api().live_people(); state.connected=latest.connected; state.model=latest.model; await afterSave();
        } else {setStatus();await reload(state.current,true,currentSession()?.id);}
      }
      if(!preview.affected.length){await commit();return;}
      const review=panel("更新模型连接与会话授权");
      el("p","本次保存 · "+name+(preview.account_changed?" · 更新连接凭据":""),review);
      for(const [purpose,url] of Object.entries(preview.recipients))el("p",recipientLabels[purpose]+" · "+url,review);
      el("strong","受影响的会话",review);
      for(const s of preview.sessions)el("p",s.name+" · "+new Date(s.created_at).toLocaleString(),review);
      button(review,"保存连接并更新授权",commit);
    }
    button(box,"保存连接",()=>saveConnection("main","https://api.deepseek.com",
      model?.value||"deepseek-v4-pro",key.value.trim()));
    if(state.community)return;
    button(box,"主回复模型",mainReplySettings);
    button(box,"配置其他接收方",()=>{
      const pane=panel("模型接收方"),role=el("select",null,pane);
      role.setAttribute("aria-label","模型用途");
      for(const purpose of ["jev","organization","selector","lookup_target"])role.add(new Option(recipientLabels[purpose],purpose));
      const url=el("input",null,el("label","HTTPS 接口",pane)),name=el("input",null,el("label","模型名",pane));
      const secret=el("input",null,el("label","API Key",pane));
      url.type="url";secret.type="password";
      button(pane,"保存连接",()=>saveConnection(role.value,url.value.trim(),name.value.trim(),secret.value));
    });
  }
  async function mainReplySettings(){
    const status=await api().live_main_reply(),box=panel("主回复模型");
    const labels={ds_pro:"DeepSeek V4 Pro",openrouter_hydrogen:"Gemini 4 Hydrogen"};
    el("p","当前正文 · "+labels[status.active],box);
    const secret=el("input",null,el("label","OpenRouter API Key",box));
    secret.type="password";secret.autocomplete="off";
    el("p",status.has_key?"已录入密钥":"待录入密钥",box);
    el("p","候选接收方 · "+status.endpoint,box);
    el("p",status.ready?"Hydrogen 正文装配已就绪":"Hydrogen 正文装配待确认",box);
    button(box,"保存密钥并启用候选",async()=>{
      if(!secret.value.trim()&&!status.has_key)return;
      const change={enabled:true};
      if(secret.value.trim())change.api_key=secret.value.trim();
      await api().live_main_reply(change);secret.value="";
      await mainReplySettings();
    });
    const selected=el("select",null,el("label","新回合正文",box));
    for(const [id,label] of Object.entries(labels))selected.add(new Option(label,id));
    selected.value=status.active;selected.options[1].disabled=!status.ready;
    button(box,"保存正文选择",async()=>{
      await api().live_main_reply({active:selected.value});
      await mainReplySettings();
    });
    const sid=currentSession()?.id,pid=state.current;
    if(status.active==="openrouter_hydrogen" && sid && pid){
      const privacy=await api().live_privacy(pid,sid);
      const writer=privacy.main_reply_writer;
      if(writer){
        el("p",privacy.enabled?"当前会话已开启全流程授权":"请先在当前会话开启全流程授权",box);
        button(box,writer.enabled?"撤回当前会话的 OpenRouter 正文授权":"授权当前会话给 OpenRouter 生成正文",async()=>{
          if(!writer.enabled && !confirm("允许 OpenRouter 使用当前隔离会话及已审角色的必要可见材料生成 Gemini 4 Hydrogen 正文？\n装配 SHA-256："+status.bundle))return;
          await api().live_main_reply_consent(pid,sid,!writer.enabled,writer.revision,writer.recipient_hash);
          await mainReplySettings();
        });
      }
    }
  }
  let checking=false,attached=false,lastHealth=null;
  const repairDialog=byId("repair-dialog");
  let repairVersion=0;
  repairDialog.addEventListener("cancel",event=>event.preventDefault());
  byId("repair-exit").onclick=()=>api().exit_workbench();
  function updateRepair(health){
    if(health.state!=="repairing"){
      if(repairDialog.open)repairDialog.close();
      return false;
    }
    const record=health.repair;
    if(linkState==="online"){
      const mark=capture();
      if(mark && !currentSession()?.loading)disconnectedReading={pid:state.current,sid:currentSession()?.id,mark};
      saveReading();epoch++;
    }
    linkState="repairing";delay=500;updateSendButton();
    byId("repair-title").textContent=record.phase==="read_only"?"工作台暂时只读":"修复中";
    byId("repair-description").textContent=record.phase==="read_only"?
      record.reason==="verify_unavailable"?"本机操作结果仍在核对。PCAR 会自动复查，也可以安全退出。":
      "本机存储或修复记录暂时无法核验。PCAR 会自动复查，也可以安全退出。":
      record.phase==="verifying"?"正在核对修复结果，完成后自动返回原会话。":
      record.action==="restart_service"?"正在恢复本机聊天服务。原消息和草稿会保留。":"正在检查本机数据目录能否恢复写入。";
    if(!repairDialog.open)repairDialog.showModal();
    return true;
  }
  function repairStatus(record){
    if(stopped)return;
    repairVersion++;
    if(record && ["executing","verifying","read_only"].includes(record.phase))updateRepair({state:"repairing",repair:record});
    else {updateRepair({state:"online"});arm(0);}
  }
  function arm(ms){
    if(stopped)return;
    clearTimeout(timer);nextCheck=Date.now()+ms;timer=setTimeout(checkService,ms);
  }
  async function checkService(){
    if(stopped||checking)return;
    checking=true;
    try{
      if(!attached){await api().presentation_attach(presenter);attached=true;}
      const observedRepair=repairVersion;
      const health=await api().live_health();
      if(stopped)return;
      if(observedRepair!==repairVersion){delay=100;return;}
      if(updateRepair(health))return;
      if(health.state!=="online")throw Error(health.state==="wrong_instance"?
        (health.repair?.reason==="external_owner"?"desktop_port_occupied":"desktop_instance_changed"):"service_disconnected");
      if(health.draining)throw Error("service_draining");
      const recovering=linkState!=="online"||generation!==health.generation;
      const configChanged=state.connected!==health.configured||state.model!==health.model||lastHealth?.configVersion!==health.configVersion;
      lastHealth=health;generation=health.generation;linkState="online";
      activation=health.activation||null;
      timeline.style.visibility=keyLocked()?"hidden":"";
      if(keyLocked()){
        if(dialog.open&&!keyPanel?.isConnected)dialog.close();
        state.connected=false;setStatus("请先填写并验证 DeepSeek API Key。",true);
        delay=750;return;
      }
      state.connected=health.configured;state.model=health.model;
      if(recovering||configChanged){
        if(!openingCardChat){
          const saved=currentPerson()?{persona_id:state.current,session_id:currentSession()?.id}:await api().live_location();
          if(stopped||openingCardChat)return;
          await reload(saved?.persona_id,false,saved?.session_id,recovering);
        }
      }else await refresh();
      if((!dialog.open||exportWaitingPanel?.isConnected)&&api().deepseek_pending){
        const pending=await api().deepseek_pending();
        if(pending && pending.id!==seenIntake)await openDeepSeekDelivery(pending.id);
      }
      if(stopped)return;
      if(observedRepair!==repairVersion){delay=100;return;}
      delay=currentSession()?.outbox?.length?180:750;
      quotaPrompt(health.quotaWaits||[]);
      const session=currentSession();
      if(health.quotaWaits?.length){
        setStatus("有任务正在等待本机调用额度恢复。","quota");
      }else if(session?.latest?.needs_connection){
        setStatus("这条消息的模型连接未通过验证，可在这里更新。",true);
      }else if(!health.configured && session){setStatus("首次聊天前，请连接模型。",true);}
      else setStatus();
    }catch(error){
      if(stopped)return;
      if(linkState==="online"){
        const mark=capture();
        if(mark && !currentSession()?.loading)disconnectedReading={pid:state.current,sid:currentSession()?.id,mark};
        saveReading();epoch++;
      }
      const reason=String(error.message||error);
      if(reason.includes("local_repair_active")){
        if(!repairDialog.open)updateRepair({state:"repairing",repair:{phase:"verifying"}});
        delay=300;return;
      }
      linkState=reason.includes("desktop_instance_changed")||reason.includes("desktop_port_occupied")?"wrong_instance":reason.includes("service_draining")?"draining":"disconnected";
      delay=Math.min(8000,Math.max(1000,delay*2));
      setStatus(reason.includes("desktop_port_occupied")?"本机服务端口由其他程序使用，PCAR 正在等待端口释放。":
                linkState==="wrong_instance"?"正在等待原工作台实例。":linkState==="draining"?"本机服务正在结束本次运行。":
                "正在连接本机服务，已发送的消息会自动核对。");
    }finally{checking=false;arm(delay);}
  }
  function addLogExport(box){
    if(!api().export_support_logs)return;
    const saved=el("p",null,box);
    saved.className="import-original";
    saved.setAttribute("role","status");
    saved.hidden=true;
    const control=button(box,"导出日志",async()=>{
      saved.hidden=false;
      saved.textContent="正在整理本机排障日志。";
      try{
        const result=await api().export_support_logs(linkState,activation?.state ?? null);
        saved.textContent="日志已保存，可将此 ZIP 发给排障人员。\n"+result.path;
      }catch{
        saved.textContent="保存日志失败。请检查 Windows 下载和 PCAR 数据目录是否有可用空间及写入权限。";
      }
    });
    control.dataset.supportLogs="";
  }
  function diagnostics(){
    const box=panel("运行与诊断");
    box.dataset.supportPanel="";
    addLogExport(box);
    button(box,"模型调用限额",quotaSettings);
    button(box,"本机备份与恢复",backupPanel);
    if(lastHealth?.quotaWaits?.length)button(box,"查看等待额度的任务",()=>quotaPrompt(lastHealth.quotaWaits,true));
    const labels={starting:"正在启动",online:"已连接",disconnected:"等待本机服务恢复",wrong_instance:"等待原实例",draining:"正在退出"};
    el("p","本机服务 · "+(labels[linkState]||"正在连接"),box);
    el("p","模型连接 · "+(lastHealth?.configured?"已配置":"待配置"),box);
    if(lastHealth?.repair){
      const repair=lastHealth.repair;
      el("p","本机修复 · "+(repair.action==="check_storage"?"存储检查":"聊天服务恢复")+" · "+
         (repair.phase==="resolved"?"已通过复验":"等待下一次核对"),box);
    }
    const names={foreground:"对话查找",background:"后台队列",knowledge:"知识维护",memory:"记忆整理",index:"索引",character_index:"人物索引",network_routes:"模型连接检查",
      daily:"每日扫描",incremental:"增量扫描",proactive:"主动消息",model_changes:"模型配置检查"};
    for(const [name,item] of Object.entries(lastHealth?.maintenance||{})){
      el("p",(names[name]||"后台任务")+"将在 "+new Date(item.next_epoch*1000).toLocaleTimeString()+" 自动重试。",box);
    }
    const session=currentSession();
    const latest=session?.latest;
    if(latest){
      el("p","当前会话最近一轮 · "+latest.id,box);
      if(latest.error_code)el("p","诊断代码 · "+latest.error_code,box);
    }
    if(state.community){
      const turns=session?.turns?.filter(t=>t.research_detail||t.has_retrieved_notes||t.retained_lookup||t.feedback_available)||[];
      if(turns.length){
        const details=groupBox(box,"会话资料与反馈");
        for(const t of turns.slice().reverse()){
          const label=t.input_members?.[0]?.text || t.id;
          const round=groupBox(details,label.length>40?label.slice(0,40)+"…":label);
          turnResearchActions(round,t);
          if(t.feedback_available)button(round,"这条不像",()=>feedback(currentPerson().id,t.id));
        }
      }
    }
    if(linkState!=="online" && nextCheck)el("p","下次自动检查 · "+new Date(nextCheck).toLocaleTimeString(),box);
  }
  async function backupPanel(){
    const data=await api().live_backups(),box=panel("本机备份与恢复");
    const format=at=>new Date(at*1000).toLocaleString();
    if(!data.entries.length){el("p","尚无已登记的自动备份。",box);return;}
    const hours=Math.max(0,Math.floor((Date.now()/1000-data.entries[0].at)/3600));
    el("p","最近登记的备份 · "+format(data.entries[0].at)+"（"+(hours?hours+" 小时前":"最近一小时内")+"）",box);
    if(data.index_fallback)el("p","当前索引未通过核验，已使用上一份经核验的登记记录。",box);
    if(data.deferred_retirement)el("p","有 "+data.deferred_retirement+" 份旧备份仍在使用或需要核对，原文件已保留。",box);
    if(data.unverified_legacy_count)el("p","旧清单中有 "+data.unverified_legacy_count+" 项无法识别，已保留原始清单。",box);
    el("p","恢复时会核验数据库和依赖清单，在所选位置新建独立的核对目录。当前聊天可继续使用。",box);
    const choice=el("select",null,box);choice.setAttribute("aria-label","备份版本");
    choice.add(new Option("最近一份校验通过的备份",""));
    for(const entry of data.entries)choice.add(new Option(format(entry.at),entry.file));
    button(box,"选择恢复位置",async()=>{
      const copy=await api().restore_backup_copy(choice.value||null);
      if(copy){
        el("p","已恢复 "+format(copy.at)+" 的数据库，共 "+copy.counts.personas+" 个角色、"+copy.counts.sessions+" 个会话和 "+copy.counts.events+" 条事件。保存位置为 "+copy.directory,box);
        if(copy.live_counts)el("p","当前实例有 "+copy.live_counts.events+" 条事件和 "+copy.live_counts.memories+" 条记忆。恢复副本有 "+copy.counts.memories+" 条记忆。",box);
      }
    });
  }
  const quotaDialog=byId("quota-dialog");
  let presentedWaits=[],quotaSaving=false;
  function quotaPrompt(waits,manual=false){
    if(quotaSaving)return;
    if(!waits.length){
      if(quotaDialog.open)quotaDialog.close();
      presentedWaits=[];return;
    }
    if(!quotaDialog.open && !manual && !waits.some(row=>row.state==="waiting"))return;
    presentedWaits=waits.map(row=>row.id);
    const reasons={hourly:"每小时总额",daily:"24 小时总额",lookup_target_hourly:"目标解析每小时额度",selector_project:"自动调用总额",selector:"选择与理解额度",
      background:"后台处理额度",learning:"表达学习额度"};
    const reached=[...new Set(waits.map(row=>reasons[row.reason]||"本机限额"))].join("、");
    byId("quota-description").textContent="已达到"+reached+"，当前有 "+waits.length+" 项工作暂停。临时放行后，PCAR 会继续本轮消息和这些任务的后续步骤，无需重发。新任务仍按原限额执行。";
    if(!quotaDialog.open){byId("quota-result").textContent="";quotaDialog.showModal();byId("quota-wait").focus();}
  }
  async function decideQuota(allow){
    if(quotaSaving)return;
    quotaSaving=true;byId("quota-allow").disabled=byId("quota-wait").disabled=true;
    try{
      await api().live_quota_decide(presentedWaits.slice(),allow);
      quotaDialog.close();arm(0);
    }catch{
      byId("quota-result").textContent="本机服务暂时未确认，请稍后再试。";
    }finally{quotaSaving=false;byId("quota-allow").disabled=byId("quota-wait").disabled=false;}
  }
  byId("quota-allow").onclick=()=>decideQuota(true);
  byId("quota-wait").onclick=()=>decideQuota(false);
  quotaDialog.addEventListener("cancel",event=>{event.preventDefault();decideQuota(false);});
  async function quotaSettings(){
    const settings=await api().live_quota_settings(),box=panel("模型调用限额");
    const enabled=check(box,"启用调用限额",settings.enabled);
    if(settings.temporary_unlimited)el("p","当前开发或验收期间临时不限额，结束后使用保存的额度。",box);
    const inputs={};
    function number(parent,key,label,value,max){
      const wrap=el("label",label,parent);wrap.className="field";
      const input=el("input",null,wrap);input.type="number";input.min="0";input.max=String(max);
      input.step="1";input.value=String(value);inputs[key]=input;
    }
    el("p","按过去 1 小时或 24 小时统计。0 表示该项不限额。",box);
    number(box,"hourly","全部模型调用 · 每小时",settings.hourly,1000000);
    number(box,"daily","全部模型调用 · 每 24 小时",settings.daily,1000000);
    const more=el("details",null,box);el("summary","单独设置自动调用额度",more);
    const labels={project_hourly:"自动调用总额 · 每小时",background_hourly:"后台整理 · 每小时",
      background_daily:"后台整理 · 每 24 小时",selector_hourly:"选择与理解 · 每小时",selector_daily:"选择与理解 · 每 24 小时"};
    for(const [key,label] of Object.entries(labels))number(more,key,label,settings.automatic[key],10000);
    const sync=()=>{for(const input of Object.values(inputs))input.disabled=!enabled.checked;};
    enabled.onchange=sync;sync();
    button(box,"保存限额",async()=>{
      const saved={...settings.automatic,hourly:settings.hourly,daily:settings.daily};
      const values=Object.fromEntries(Object.entries(inputs).map(([k,input])=>[k,input.value===""?0:input.valueAsNumber]));
      const valid=k=>Number.isSafeInteger(values[k]) && values[k]>=0 && values[k]<=Number(inputs[k].max);
      for(const key of Object.keys(inputs)){
        if(!valid(key)){
          if(enabled.checked)throw new FormError("请填写有效的调用次数。");
          values[key]=saved[key];
        }
      }
      if(enabled.checked && !Object.values(values).some(v=>v>0))throw new FormError("启用时至少设置一项正数。");
      const {hourly,daily,...automatic}=values;
      await api().live_quota_settings({enabled:enabled.checked,hourly,daily,automatic});
      dialog.close();notify(enabled.checked?"已保存模型调用限额。":"已关闭模型调用限额。");arm(0);
    });
  }
  function stop(){if(!stopped)saveReading();stopped=true;linkState="draining";epoch++;clearTimeout(timer);updateSendButton();}
  function exitStatus(phase){
    stop();
    const steps={stop_admission:"正在保存本机状态。",draining:"正在等待已受理的工作完成。",logout:"正在关闭所属连接。",
      checkpoint:"正在核对已收到的结果和在途请求。",settled:"正在结束本机服务。",releasing:"正在释放所属组件。",
      component_ownership_or_release_unconfirmed:"有组件的归属或退出状态尚未确认，PCAR 将自动复查。"};
    byId("repair-title").textContent="正在退出";
    byId("repair-description").textContent=steps[phase]||"正在核对退出结果。";
    byId("repair-exit").disabled=true;
    if(!repairDialog.open)repairDialog.showModal();
  }
  function resume(){if(stopped){stopped=false;linkState="starting";arm(0);}}
  function wake(){
    if(!active||stopped)return;
    if(linkState==="online")api().live_network_wake().catch(()=>{});
    arm(0);
  }
  document.addEventListener("click",event=>{
    if(!active||!keyLocked()||!(event.target instanceof Element))return;
    if(keyPanel?.isConnected && (keyPanel.contains(event.target)||event.target.closest("#dialog-close")))return;
    const control=event.target.closest("button,a,input,select,textarea,summary,[role=button]");
    if(!control||control.closest("#repair-dialog")||control.matches(
      '#menu-toggle,[data-menu="exit"],[data-menu="advanced"],[data-support-logs]'))return;
    if(control.matches("#dialog-close")&&dialogBody.querySelector("[data-support-panel]"))return;
    event.preventDefault();event.stopImmediatePropagation();keyPrompt();
  },true);
  document.addEventListener("beforeinput",event=>{
    if(active&&keyLocked()&&keyPanel?.contains(event.target)!==true){event.preventDefault();keyPrompt();}
  },true);
  window.addEventListener("online",wake);
  window.addEventListener("focus",()=>{
    if(active && !stopped && !checking && !document.hidden)arm(0);
  });
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)wake();});
  function connect(){
    if(active)return;
    active=true;byId("sample-mode").hidden=true;
    document.querySelector('[data-menu="reset"]')?.remove();
    setStatus("正在连接本机服务。");arm(0);
  }
  window.addEventListener("pagehide",stop);
  window.addEventListener("pywebviewready",run(connect),{once:true});
  if(window.pywebview?.api)run(connect)();
  return {get active(){return active;},get offline(){return linkState!=="online";},get composing(){return composing;},
    get selecting(){return selectionToken!==null;},get pendingDraft(){return Boolean(pendingDraft(currentSession()));},
    home:run(home),select:run(select),draw,capture,restore:restoreReading,reviseDraft,renderInputBuffer,manualEnter,
    avatar,appearance,send:run(send),profile:run(profile),importHistory:run(importHistory),
    diagnostics,connection,stop,resume,wake,repairStatus,exitStatus};
})();
