/* Workspace conversation budgets and explicit, exportable handoffs. */
const budgetSettings=settings;
settings=function(){
  budgetSettings();const p=state.config.conversation_policy;
  if(!p)return;
  const ceiling=p.capacity||16384;
  $('#dialog-body').insertAdjacentHTML('beforeend',`<hr class="settings-divider"><h3>问答上下文预算</h3><p class="modal-note">用于文章问答和资料库问答。默认 16K，包含回答预留；到达预算会结束本段对话并保存交接记录。</p><label class="form-field">本段预算 <output id="budget-value">${p.budget.toLocaleString()} tokens</output><input id="conversation-budget" aria-label="问答上下文预算" type="range" min="8192" max="${ceiling}" step="1" value="${p.budget}"></label><p class="modal-note">${p.capacity?`${p.official?'官方模型容量':'手动标注容量'}：${p.capacity.toLocaleString()} tokens。`:'当前模型容量尚未确认，调整范围暂限于 8K–16K。'} 回答预留最多 4,096 tokens。${p.official?`<br>DeepSeek 官方规格 · 核对日期 ${esc(p.checked)}`:''}</p><div class="actions">${button('budget-default','恢复默认 16K')}${button('budget-save','保存预算','primary')}</div><p class="modal-note">发送前按文字字节与图片预留保守检查，可能提前触发；接口返回用量用于核对。预算不是累计计费额度。导图和浮窗词条追问不使用本段会话的封存流程。</p>`);
  $('#conversation-budget').oninput=e=>{$('#budget-value').textContent=Number(e.target.value).toLocaleString()+' tokens';};
};
contextInfo=function(){
  const r=state.session.context_usage,p=state.config.conversation_policy;
  const valid=r&&r.signature===state.config.signature;
  modal('上下文用量',`<p>圆环表示上次请求用量占当前问答预算的比例，不是累计费用。</p><p>${valid?`${r.kind==='reported'?'接口返回':'文字估算'}：输入 ${r.input_tokens.toLocaleString()} tokens${r.output_tokens!=null?'，输出 '+r.output_tokens.toLocaleString()+' tokens':''}。`:'尚无当前模型的会话用量记录。'}</p><p>问答预算：${(p?.budget||16384).toLocaleString()} tokens；模型容量：${p?.capacity?p.capacity.toLocaleString():'尚未确认'}。</p><p>文章和资料库问答保留本段历史；发送前检查预算并预留回答空间。达到预算会保存原对话、结束本段并生成交接记录，可导出后接续新对话。</p><p class="modal-note">发送前是保守估算，不是模型的精确分词结果；图片按预留值检查。原文、模型回答与交接摘录始终区分。</p>${button('settings','调整问答预算')}${button('handoff-list','查看交接记录')}`);
};
function handoffNotice(){
  const s=state.session;
  return s.closed?`<div class="handoff-notice" role="status"><strong>本段对话已结束，交接记录已保存</strong><p>${esc(s.end_reason)}</p><div class="actions">${button('handoff-view','查看交接记录')}${button('handoff-export','导出 Markdown')}${button('conversation-restart','接续新对话','primary')}</div></div>`:(s.archives?.length?`<div class="handoff-history">${button('handoff-list',`交接记录 · ${s.archives.length}`)}</div>`:'');
}
const budgetMessages=messagesHTML;
messagesHTML=function(){return budgetMessages()+handoffNotice();};
const budgetLibraryMessages=libraryMessages;
libraryMessages=function(){return budgetLibraryMessages()+handoffNotice();};
function currentConversationKey(){return state.page==='library'?library.key:state.article?sessionKey():null;}
let displayedHandoff='';
function showHandoff(text){displayedHandoff=text;modal('会话交接记录',`<p class="modal-note">根据已保存对话在本机整理；没有额外调用模型。这里的回答需要对照引用核实。</p><textarea class="handoff-document" readonly aria-label="交接记录正文">${esc(text)}</textarea><div class="actions">${button('handoff-export-current','导出 Markdown')}${button('close','关闭')}</div>`);}
const budgetDispatch=dispatch;
dispatch=async function(action){
  if(action==='budget-default'){$('#conversation-budget').value=Math.min(16384,Number($('#conversation-budget').max));$('#conversation-budget').dispatchEvent(new Event('input'));return;}
  if(action==='budget-save'){state.config=await api('conversation-budget',{budget:Number($('#conversation-budget').value)});settings();updateContextMeter();toast('问答预算已保存');return;}
  if(action==='handoff-view'){showHandoff(state.session.handoff||'尚无交接记录。');return;}
  if(action==='handoff-list'){
    const records=[...(state.session.archives||[]),...(state.session.handoff?[state.session]:[])];
    modal('交接记录',`<p class="modal-note">原对话与交接记录保存在本机，可随时导出。</p>${records.map((s,i)=>`<button class="handoff-item" data-handoff-index="${i}">第 ${i+1} 段 · ${esc(new Date(s.ended_at).toLocaleString())}</button>`).join('')||'<p>当前会话尚未生成交接记录。</p>'}`);
    document.querySelectorAll('[data-handoff-index]').forEach(el=>el.onclick=()=>showHandoff(records[Number(el.dataset.handoffIndex)].handoff));return;
  }
  if(action==='handoff-export'||action==='handoff-export-current'){const text=action==='handoff-export'?state.session.handoff:displayedHandoff;if(text)download('小探-会话交接记录.md',text,'text/markdown;charset=utf-8');return;}
  if(action==='conversation-restart'){
    if(state.job)throw Error('请先等待当前任务完成。');
    const key=currentConversationKey();if(!key)throw Error('请先打开对应对话。');
    const result=await api('conversation-restart',{key});drafts.delete(key);state.session=result;
    if(state.page==='library'){library.session=result;renderLibrary();}else renderWork();
    if($('#dialog').open)$('#dialog').close();toast('已开启新一段对话，旧记录可随时查看');return;
  }
  if((action==='send'||action==='library-send'||action==='explain')&&state.session.closed){showHandoff(state.session.handoff);return;}
  return budgetDispatch(action);
};
new MutationObserver(()=>{
  if(!['work','library'].includes(state.page))return;
  const el=document.querySelector(state.page==='library'?'#library-question':'#question');
  if(el&&state.session.closed&&!el.disabled)el.disabled=true;
}).observe($('#main'),{childList:true,subtree:true});
