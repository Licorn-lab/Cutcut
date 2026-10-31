let geometryDraft=null,geometryTimer=null;
const $=id=>document.getElementById(id);
let lastExport=null,failedEdit=null;
let wasBusy=false,actionBusy=false,editBusy=false,editPoints=[],drawing=false,editHistory={undo:false,redo:false};
let state=null,selected=null,detail=null,offset=0,dirty=false,loading=false,view={scale:1,x:0,y:0},serial=0,editorNav=null;
const labels={new:'未处理',pending:'待审核',editing:'编辑中',approved:'已通过',redo:'待返工',failed:'处理失败',processing:'正在处理'};
function toast(text){$('toast').textContent=text;$('toast').style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').style.display='none',4200)}
async function api(path,data){let r;try{r=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});}catch(e){throw Error('本地服务连接失败。请确认启动窗口仍在运行，再重试；重新导出前请先检查文件是否已生成。')}let v;try{v=await r.json();}catch(e){throw Error('服务返回异常，请刷新后重试。')}if(!r.ok)throw Error(v.error||'操作失败');return v}
function setEditStatus(text,kind='info'){$('editStatus').textContent=text;$('editStatus').dataset.kind=kind;}
function syncSavedStatus(){
 if(dirty)return setEditStatus('名称或返工备注尚未导出；确认通过或标记返工后生效。','draft');
 const row=detail?.row;
 if(!row)return setEditStatus('尚未生成候选，请先开始本地抠图。');
 if(row.status==='failed')return setEditStatus('处理失败 · 可换模重试；原图保留。','error');
 if(row.status==='processing')return setEditStatus('本张正在处理，请稍候。','saving');
 if(detail.export)return setEditStatus(row.status==='approved'?'本张已通过并导出。':'本张已导出至待重制，备注仅写在文件名中。','exported');
 setEditStatus('已保存到本机 · 尚未导出；确认通过后生成文件。','saved');
}
function showExportFeedback(){
 const info=detail?.export||lastExport,box=$('exportFeedback');box.hidden=!info;if(!info)return;
 $('exportFeedbackLabel').textContent=detail?.export?'本张已导出':'上张已导出';
 $('exportPath').textContent=info.path;$('exportPath').title=info.path;
 $('revealExport').onclick=async()=>{const button=$('revealExport');button.disabled=true;try{await api('/api/reveal-export',{id:info.id,relative:info.relative});}catch(e){toast(e.message)}finally{button.disabled=false}};
}
function savedChange(text){detail.export=null;if(lastExport?.id===selected)lastExport=null;setEditStatus(text+' · 已保存，尚未导出。','saved');showExportFeedback();}
function saveFailure(error,instruction){setEditStatus(error.message+' '+instruction,'error');toast(error.message);}

function imageURL(id,kind='original',thumb=false,revision=0){return `/api/image?id=${encodeURIComponent(id)}&kind=${kind}&revision=${revision}${thumb?'&thumb=1':''}`}
// Saved crops are previewed at their own aspect ratio; edits still use source coordinates.
function previewBounds(im){const crop=new URL(im.src,location.href).searchParams.get('crop')==='1'?detail?.row?.crop:null;return crop||[0,0,...(detail?.dimensions||[1,1])];}
function refreshPreviewImages(reset=false){
 if(!selected||!detail)return;
 const revision=detail.row?.revision||0;
 const raw=typeof rawEditingView==='function'&&rawEditingView();

 for(const name of ['original','result']){
  if(name==='result'&&(!detail.row?.mask||geometryDraft))continue;
  const url=imageURL(selected,name,false,revision)+(name==='result'&&!raw?'&crop=1'+(cropDraft||geometryDraft?'&frame=1':''):'');
  if($(name).getAttribute('src')!==url)$(name).src=url;
 }
 $('originalLabel').textContent='原图';
 const background=detail.row?.background||'#ffffff';
 const resultName=background==='transparent'?'透明底结果':background==='#ffffff'?'白底结果':'彩色背景结果';
 $('resultLabel').textContent=cropDraft?'裁剪预览（保留已有调整）':geometryDraft?'图片调整预览':raw&&(hasGeometry(detail.row?.geometry)||detail.row?.image_steps?.length)?'蒙版修整视图（原始方向）':resultName;
 $('resultView').classList.toggle('transparent-preview',background==='transparent');
 if(reset)view={scale:1,x:0,y:0};
 if(typeof syncGeometryPreview==='function')syncGeometryPreview();
 transform();
}
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n}
function updateNavigation(){
 $('editorPosition').textContent=editorNav?.position?`${editorNav.position} / ${state.filtered} · ${labels[detail?.row?.status]||'未处理'}`:labels[detail?.row?.status]||'未处理';
 const blocked=actionBusy||editBusy||drawing;
 $('previousImage').disabled=!!blocked||!editorNav?.prev;
 $('nextImage').disabled=!!blocked||!editorNav?.next;
}
function selectedBlocked(){return !detail?.row?.mask||detail.row.status==='processing'||(state?.selected===selected?state.selected_reviewable===false:detail?.reviewable===false)}
function updateActionControls(){
 const local=editBusy||actionBusy||drawing||editPoints.length>0;
 const batchBlocked=!!state?.job.busy||local;
 for(const id of ['open','start','restart','restartScope','retry','desktop'])$(id).disabled=batchBlocked||(['retry'].includes(id)&&!selected)||(['start','restart','restartScope','desktop'].includes(id)&&!state?.count);
 $('approve').disabled=local||selectedBlocked()||!!cropDraft||!!geometryDraft;
 // Failed candidates can still be exported for rework once the batch is idle.
 const failedRework=detail?.row?.status==='failed'&&!state?.job.busy;
 $('redo').disabled=local||(!failedRework&&selectedBlocked())||!!cropDraft||!!geometryDraft;
 document.querySelectorAll('[data-pick]').forEach(b=>b.disabled=batchBlocked);
 $('filter').disabled=actionBusy;
 $('retrySave').hidden=!failedEdit;$('retrySave').disabled=actionBusy||editBusy||drawing||selectedBlocked();updateNavigation();updateEditControls();updateExtras();$('titleName').disabled=!selected||actionBusy;
}
async function alignEditorQueue(){
 const value=await api(`/api/state?filter=${$('filter').value}&selected=${encodeURIComponent(selected||'')}`);
 editorNav=value.navigation;
 if(editorNav?.position)offset=Math.floor((editorNav.position-1)/40)*40;
}
for(const [id,direction] of [['previousImage','prev'],['nextImage','next']])$(id).onclick=async()=>{
 if(actionBusy||editBusy||drawing)return;
 const target=editorNav?.[direction];if(target)await openEditor(target);
};
async function refresh(){if(loading)return;loading=true;try{
 state=await api(`/api/state?offset=${offset}&filter=${$('filter').value}&selected=${encodeURIComponent(selected||'')}`);
 if(!$('source').value)$('source').value=state.settings.source||'';if(!$('destination').value)$('destination').value=state.settings.destination||'';
 const setup=document.querySelector('.setup');if(state.count&&!setup.dataset.loaded){setup.open=false;setup.dataset.loaded='1'}
 $('job').textContent=state.job.message;$('count').textContent=`共 ${state.count} 张 · 已通过 ${state.counts.approved||0} · 失败 ${state.counts.failed||0}`;$('error').textContent=state.job.error;
 $('progress').max=state.job.total||1;$('progress').value=state.job.progress||0;
 $('batchHint').textContent=state.restart?`有 ${state.restart.pending.length} 张整批重做任务未完成，点击“继续”按已记录模型处理。`:'失败与有程序提示的图片优先展示；每张图片均需人工确认';
 const items=$('items'),sig=JSON.stringify([selected,state.items]);if(items.dataset.signature!==sig){items.dataset.signature=sig;items.replaceChildren();for(const item of state.items){const b=node('button',undefined,'item'+(selected===item.id?' active':''));b.dataset.status=item.status;b.dataset.id=item.id;const im=node('img');im.loading='lazy';im.src=imageURL(item.id,'original',true);im.alt='';const text=node('div',undefined,'name');text.append(node('span',item.name),node('span',labels[item.status]+(item.warning?' · 程序提示':''),'status'));b.append(im,text);b.onclick=()=>openEditor(item.id,b);items.append(b)}if(!state.items.length)items.append(node('div',state.count?'当前筛选下暂无图片':'请先在批次设置中打开图片文件夹','empty-list'))}
 $('page').textContent=state.filtered?`${offset+1}–${Math.min(offset+40,state.filtered)} / ${state.filtered}`:'暂无图片';$('prev').disabled=offset===0;$('next').disabled=offset+40>=state.filtered;
 $('stop').disabled=!state.job.busy;
 editorNav=state.navigation;updateActionControls();
 if(!selected&&state.items.length&&!actionBusy){await select(state.items[0].id);updateActionControls()}
 if(selected&&!dirty&&!failedEdit&&!actionBusy&&!editBusy&&!drawing&&!editPoints.length&&!cropDraft&&!geometryDraft){const current=state.items.find(i=>i.id===selected);if((wasBusy&&!state.job.busy)||(current&&detail&&(current.revision!==(detail.row?.revision||0)||current.status!==(detail.row?.status||'new'))))await select(selected,true)}
 wasBusy=state.job.busy;
 }catch(e){toast(e.message)}finally{loading=false}}
function canLeave(){if(editBusy||drawing){toast('请等待当前修整保存完成');return false}if((failedEdit||cropDraft||geometryDraft||editPoints.length||dirty)&&!confirm('放弃尚未确认的图片调整、裁剪、选区或未导出的返工备注或名称？'))return false;cropDraft=null;geometryDraft=null;cancelAnimationFrame(geometryTimer);cancelEditSelection();return true}
async function openEditor(id){if(actionBusy)return;if(await select(id)){await alignEditorQueue();await refresh();requestAnimationFrame(transform)}}
async function select(id,force=false){if(editBusy||(actionBusy&&!force))return false;if(!force&&selected!==id&&!canLeave())return false;const ticket=++serial;try{const d=await api('/api/item?id='+encodeURIComponent(id));if(ticket!==serial)return false;if(selected!==id||detail?.row?.revision!==d.row?.revision)editHistory={undo:false,redo:false};selected=id;detail=d;failedEdit=null;$('retrySave').hidden=true;dirty=false;backgroundChoosing=false;syncSavedStatus();showExportFeedback();updateExtras();const has=!!d.row?.mask;$('result').style.display=has?'':'none';$('noresult').style.display=has?'none':'';if(!has)$('result').removeAttribute('src');refreshPreviewImages(true);$('warnings').textContent=[d.row?.error,(d.row?.automatic||'未发现程序规则提示。').replace(/程序提示仅供筛查[，。]?/g,'').trim().replace(/[，；]+$/,'。'),'程序筛查结果，仍需人工确认。'].filter(Boolean).join(' ');$('reason').value=d.rework_note||'';$('naming').value=d.row?.export_naming==='custom'?'custom':'original';$('customName').value=d.row?.export_naming==='custom'?(d.row.export_name||'').replace(/\.(?:jpe?g|png)$/i,''):'';updateFilenamePreview();$('retryModel').value=d.row?.options.model||$('model').value;updateEditControls();updateNavigation();return true}catch(e){toast(e.message);return false}}
async function action(work){if(actionBusy)return;if(geometryDraft){toast('请先确认或取消图片调整');return}if(cropDraft){toast('请先确认或取消裁剪');return}if(editBusy||drawing||editPoints.length||failedEdit){toast('请先完成、重试保存或取消修整选区');return}actionBusy=true;updateActionControls();try{await work()}catch(e){saveFailure(e,'请检查当前状态后重试。')}finally{actionBusy=false;await refresh();updateActionControls()}}
for(const id of ['reason','customName','naming'])$(id).addEventListener('input',()=>{dirty=true;setEditStatus('名称或返工备注已修改 · 尚未导出，仅写入导出文件名。','draft');updateFilenamePreview()});
// The header and footer edit one export-name draft; originals are never renamed.
let titleNameSnapshot=null;
$('titleName').onfocus=()=>{titleNameSnapshot={naming:$('naming').value,name:$('customName').value,dirty};};
$('titleName').oninput=()=>{if(!selected||actionBusy)return;$('naming').value='custom';$('customName').value=$('titleName').value;dirty=true;setEditStatus('导出名称已修改 · 尚未导出，原图名称保留。','draft');updateFilenamePreview();};
$('titleName').onblur=()=>{titleNameSnapshot=null;updateFilenamePreview();};
$('titleName').onkeydown=e=>{
 if(e.isComposing)return;
 if(e.key==='Enter'){e.preventDefault();$('titleName').blur();}
 if(e.key==='Escape'){e.preventDefault();e.stopPropagation();if(titleNameSnapshot){$('naming').value=titleNameSnapshot.naming;$('customName').value=titleNameSnapshot.name;dirty=titleNameSnapshot.dirty;}$('titleName').blur();syncSavedStatus();}
};
function updateFilenamePreview(){const custom=$('naming').value==='custom';$('customName').hidden=!custom;const name=custom?$('customName').value.trim().replace(/\.(?:jpe?g|png)$/i,''):(detail?.name||'').split('/').pop().replace(/\.[^.]+$/,'');const note=$('reason').value.replace(/[\\/:*?"<>|\x00-\x1f\x7f]+/g,'_').replace(/\s+/g,' ').replace(/^[ ._]+|[ ._]+$/g,'');const extension=detail?.row?.background==='transparent'?'.png':'.jpg';
 if(document.activeElement!==$('titleName'))$('titleName').value=name;
 $('titleName').disabled=!selected||actionBusy;
 const measure=updateFilenamePreview.measure||(updateFilenamePreview.measure=document.createElement('canvas').getContext('2d'));measure.font=getComputedStyle($('titleName')).font;$('titleName').style.width=Math.ceil(measure.measureText($('titleName').value||$('titleName').placeholder).width+10)+'px';
 $('titleExtension').textContent=selected?extension:'';
 $('filenamePreview').textContent=name?`通过：${name}${detail?.row?.background==='transparent'?'.png':'.jpg'}${note?' · 返工：'+name+'_返工-'+note+'.jpg':''}（重名自动加序号）`:'';}
document.querySelectorAll('[data-pick]').forEach(b=>b.onclick=()=>action(async()=>{const v=await api('/api/pick',{});if(v.path)$(b.dataset.pick).value=v.path}));
$('open').onclick=()=>action(async()=>{if(dirty&&!confirm('放弃未导出的返工备注或名称并打开批次？'))return;await api('/api/open',{source:$('source').value,destination:$('destination').value});selected=null;detail=null;lastExport=null;failedEdit=null;showExportFeedback();dirty=false;editorNav=null;offset=0;cropDraft=null;editHistory={undo:false,redo:false};for(const id of ['original','result'])$(id).removeAttribute('src');$('result').style.display='none';$('noresult').style.display='';$('customName').value='';updateFilenamePreview();$('reason').value='';updateExtras();updateEditControls()});
function batchOptions(mode){return {model:$('model').value,layout:$('layout').value,size:Number($('size').value),occupancy:Number($('occupancy').value)/100,mode,confirmed:mode==='restart_all'||mode==='restart_unapproved'}}
$('start').onclick=()=>action(async()=>{await api('/api/start',batchOptions('resume'))});
$('restart').onclick=()=>action(async()=>{
 const all=$('restartScope').value==='all',count=all?state.count:state.count-(state.counts.approved||0),model=$('model').selectedOptions[0].textContent;
 if(!count)return toast('没有需要重做的未通过图片');
 if(!confirm(`将${all?'全部':'未通过的'} ${count} 张图片用 ${model} 重新处理。${all?'':'已通过的图片和导出文件会保留。'}\n重做图片的旧修整、裁剪和审核状态会清除，对应旧导出会移除。停止后可继续。`))return;
 await api('/api/start',batchOptions(all?'restart_all':'restart_unapproved'));
 lastExport=null;showExportFeedback();
});
$('stop').onclick=async()=>{try{await api('/api/stop',{})}catch(e){toast(e.message)}};
$('retry').onclick=()=>action(async()=>{if(!detail.row)throw Error('请先生成候选');if(!confirm('本张重新抠图将清除画笔、裁剪和审核状态，并移除本工具生成的对应旧导出。继续？'))return;await api('/api/retry',{id:selected,revision:detail.row.revision,model:$('retryModel').value});if(lastExport?.id===selected)lastExport=null});
for(const [id,decision] of [['approve','approved'],['redo','redo']])$(id).onclick=()=>action(async()=>{
 if(!detail.row)throw Error('请先生成候选');
 const current=selected;
 const before=decision==='approved'?await api(`/api/state?filter=${$('filter').value}&selected=${encodeURIComponent(current)}`):null;
 setEditStatus('正在确认并导出，请稍候…','saving');
 const result=await api('/api/decide',{id:current,revision:detail.row.revision,decision,reason:decision==='redo'?$('reason').value:'',naming:$('naming').value,custom_name:$('customName').value});
 lastExport=result.export;dirty=false;
 const next=before?.navigation?.next_review;
 if(!await select(next||current,true)){detail.export=result.export;showExportFeedback();setEditStatus('图片已导出，但下一张加载失败，请重新选择图片。','error');return;}
 showExportFeedback();
 await alignEditorQueue();
 toast(decision==='approved'?(next?'已确认并导出，已切换到下一张待审核图片':'已确认并导出，当前筛选下没有其他待审核图片'):'返工图片已导出，备注已写入文件名');
});
$('desktop').onclick=()=>action(()=>api('/api/desktop',{}));$('filter').onchange=()=>{offset=0;refresh()};$('prev').onclick=()=>{offset=Math.max(0,offset-40);refresh()};$('next').onclick=()=>{offset+=40;refresh()};
function transform(){for(const id of ['original','result']){const im=$(id),viewport=im.parentElement;if(im.complete&&im.naturalWidth){const ratio=Math.min(viewport.clientWidth/im.naturalWidth,viewport.clientHeight/im.naturalHeight);im.style.width=(im.naturalWidth*ratio)+'px';im.style.height=(im.naturalHeight*ratio)+'px';}im.style.transform=`translate(${view.x}px,${view.y}px) scale(${view.scale})`;}if(typeof drawSelection==='function')drawSelection();if(typeof renderExtras==='function')renderExtras();if(typeof positionGeometryPreview==='function')positionGeometryPreview();if(typeof renderGeometryGrid==='function')renderGeometryGrid()}
function zoom(f){view.scale=Math.max(.25,Math.min(12,view.scale*f));transform()}
$('zoomin').onclick=()=>zoom(1.25);$('zoomout').onclick=()=>zoom(.8);$('fit').onclick=()=>{view={scale:1,x:0,y:0};transform()};
window.addEventListener('beforeunload',e=>{if(dirty||editBusy||failedEdit||editPoints.length||cropDraft||geometryDraft){e.preventDefault();e.returnValue=''}});
window.addEventListener('keydown',e=>{if(e.key!=='Escape')return;if(geometryDraft){$('cancelGeometry').click();return}if(cropDraft){$('cancelCrop').click();return}if(editPoints.length||failedEdit){$('cancelSelection').click();return}if(document.querySelector('.workspace').classList.contains('focus'))$('focusMode').click()});
function updateSquareOptions(){const square=$('layout').value==='square';for(const id of ['squareSizeOption','squareOccupancyOption','squareHint'])$(id).hidden=!square;}
$('layout').onchange=updateSquareOptions;updateSquareOptions();
refresh();setInterval(refresh,2000);
