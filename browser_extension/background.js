const origins=new Set(["https://chat.deepseek.com","https://cdn.deepseek.com","https://fe-static.deepseek.com"]);
const hosts={
  development:"com.pcar.deepseek.development",
  community:"com.pcar.deepseek.community"
};
const get=async key=>(await chrome.storage.local.get(key))[key];
const save=(key,value)=>chrome.storage.local.set({[key]:value});

function official(raw){
  try{return origins.has(new URL(raw).origin);}catch{return false;}
}
function selected(raw){
  try{
    const url=new URL(raw);
    return url.origin==="https://chat.deepseek.com"?
      /^\/a\/chat\/s\/([a-zA-Z0-9_-]{1,128})\/?$/.exec(url.pathname)?.[1]:null;
  }catch{return null;}
}
async function native(edition,request){
  if(!hosts[edition])return {state:"not_installed"};
  try{return await chrome.runtime.sendNativeMessage(hosts[edition],{version:1,...request});}
  catch{return {state:"not_installed"};}
}
async function candidate(item,job){
  if(!item||item.incognito||!official(item.url)||!official(item.finalUrl||item.url))return false;
  if(item.id===job.download)return true;
  return Date.parse(item.startTime)>=job.started&&String(item.filename||"").toLowerCase().endsWith(".zip");
}
const deliveries=new Set();
async function settle(job){
  if(job?.download===undefined || deliveries.has(job.id))return job;
  deliveries.add(job.id);
  try{
    const [item]=await chrome.downloads.search({id:job.download});
    if(!await candidate(item,job))return job;
    if(item.state!=="complete"||item.exists===false)return { ...job,state:item.state };
    const reply=await native(job.edition,{action:"deliver",id:job.id,target:job.target,path:item.filename});
    if((await get("job"))?.id!==job.id)return job;
    const updated={...job,state:reply.state};
    await save("job",updated);
    return updated;
  }finally{deliveries.delete(job.id);}
}
chrome.downloads.onChanged.addListener(async changed=>{
  const job=await get("job");
  if(!job||["pending","imported","cancelled"].includes(job.state))return;
  if(job.download===changed.id){await settle(job);return;}
  if(job.download!==undefined||changed.state?.current!=="complete")return;
  const [item]=await chrome.downloads.search({id:changed.id});
  if(await candidate(item,job)&&selected(item.referrer)===job.target&&(await get("job"))?.id===job.id){
    const next={...job,download:item.id};
    await save("job",next);
    await settle(next);
  }
});
chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(sender.id!==chrome.runtime.id||sender.tab)return;
  handle(message).then(respond,()=>respond({state:"retry"}));
  return true;
});

async function handle(message){
  const job=await get("job");
  switch(message.action){
    case "status":{
      const supported=(await Promise.all(Object.keys(hosts).map(async edition=>
        (await native(edition,{action:"probe"})).state==="ready"?edition:null))).filter(Boolean);
      const available=job&&!["pending","imported","cancelled"].includes(job.state)?
        await chrome.downloads.search({startedAfter:new Date(job.started).toISOString(),limit:30}):[];
      const downloads=[];
      for(const item of available){
        if(item.state==="complete"&&await candidate(item,job)){
          downloads.push({id:item.id,name:String(item.filename).split(/[\\\/]/).pop()});
        }
      }
      return {job,downloads,supported,edition:await get("edition")};
    }
    case "start":{
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      const target=selected(tab?.url);
      if(!target||!hosts[message.edition])return {state:"invalid_target"};
      const next={id:crypto.randomUUID().replaceAll("-",""),target,edition:message.edition,started:Date.now(),state:"waiting"};
      await save("edition",next.edition);
      await save("job",next);
      await native(next.edition,{action:"remember",id:next.id,target:next.target});
      return next;
    }
    case "download":{
      if(!job||!official(message.url))return {state:"invalid_url"};
      if(job.download!==undefined && job.state!=="interrupted")return {state:"invalid_url"};
      const download=await chrome.downloads.download({url:message.url,filename:"DeepSeek-PCAR-"+job.id+".zip",conflictAction:"uniquify"});
      const next={...job,download,state:"in_progress"};
      await save("job",next);
      return settle(next);
    }
    case "associate":{
      if(!job)return {state:"invalid_target"};
      const [item]=await chrome.downloads.search({id:message.id});
      if(!await candidate(item,job))return {state:"invalid_file"};
      const next={...job,download:item.id};
      await save("job",next);
      return settle(next);
    }
    case "retry":
      return job?settle(job):{state:"invalid_target"};
    default:return {state:"unsupported"};
  }
}
