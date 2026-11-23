const $=id=>document.getElementById(id);
const call=(action,parameters={})=>chrome.runtime.sendMessage({action,...parameters});
const labels={development:"PCAR 开发版",community:"PCAR 社区版"};
const states={
  waiting:"已记住对话。请在 DeepSeek 发起导出。",
  in_progress:"正在下载。完成后将交给 PCAR。",
  interrupted:"下载中断。请重新获取官方下载。",
  pending:"已交给 PCAR，请核对建卡预览。",
  imported:"这个文件已导入 PCAR。",
  cancelled:"该交接已取消，可重新选择对话。",
  invalid_target:"请先在当前标签页打开一段 DeepSeek 对话。",
  invalid_url:"请填写受支持的 DeepSeek 官方下载链接。",
  invalid_file:"该下载暂不可用，请在 PCAR 选择 ZIP。",
  setup_required:"已打开 PCAR。完成初始化后点击「继续上次交接」。",
  key_required:"已打开 PCAR。验证 Key 后点击「继续上次交接」。",
  not_installed:"未找到 PCAR 浏览器桥接，请在 PCAR 中连接浏览器扩展。",
  retry:"暂未完成，请重试。"
};
function show(result){$("status").textContent=states[result?.state]||"";}
async function run(action,parameters={}){
  try{show(await call(action,parameters));await refresh(false);}
  catch{show({state:"retry"});}
}
async function refresh(showJob=true){
  const {job,supported,edition,downloads}=await call("status");
  const select=$("edition");
  const previous=select.value||edition;
  select.replaceChildren();
  for(const item of supported)select.add(new Option(labels[item],item));
  if(supported.includes(previous))select.value=previous;
  $("start").disabled=!supported.length;
  $("download").disabled=!job||(job.download!==undefined&&job.state!=="interrupted");
  $("retry").disabled=!job||job.download===undefined;
  $("files").replaceChildren();
  for(const file of downloads){
    const control=document.createElement("button");
    control.textContent="使用已下载文件 "+file.name;
    control.onclick=()=>run("associate",{id:file.id});
    $("files").append(control);
  }
  if(showJob)show(job||(!supported.length?{state:"not_installed"}:null));
}
$("start").onclick=()=>run("start",{edition:$("edition").value});
$("download").onclick=()=>{
  const url=$("download-url").value.trim();$("download-url").value="";return run("download",{url});
};
$("retry").onclick=()=>run("retry");
refresh().catch(()=>show({state:"retry"}));
