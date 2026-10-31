let cropDraft=null,backgroundChoosing=false,brushHover=null;
for(const name of ['original','result']){const ring=node('div',undefined,'brush-cursor');ring.hidden=true;ring.setAttribute('aria-hidden','true');$(name+'View').append(ring);}
function renderBrushCursor(){
 for(const name of ['original','result']){
  const viewport=$(name+'View'),ring=viewport.querySelector('.brush-cursor'),im=$(name);
  const visible=brushHover?.el===viewport&&$('editShape').value==='brush'&&$('editMode').value!=='pan'&&!actionBusy&&!editBusy&&!selectedBlocked()&&!cropDraft&&!geometryDraft&&imagePoint(brushHover,viewport);
  ring.hidden=!visible;if(!visible)continue;
  const r=viewport.getBoundingClientRect(),ir=im.getBoundingClientRect(),[x1,y1,x2,y2]=previewBounds(im),radius=Number($('radius').value)||1;
  Object.assign(ring.style,{left:brushHover.clientX-r.left+'px',top:brushHover.clientY-r.top+'px',width:Math.max(2,radius*2/(x2-x1)*ir.width)+'px',height:Math.max(2,radius*2/(y2-y1)*ir.height)+'px'});
  ring.dataset.mode=$('editMode').value;
 }
}
for(const id of ['radius','radiusRange'])$(id).addEventListener('input',()=>{const value=Math.max(1,Math.min(1000,Math.round(Number($(id).value)||1)));if(id==='radiusRange')$('radius').value=value;else $('radiusRange').value=value;renderBrushCursor();});
$('radius').addEventListener('change',()=>{$('radius').value=$('radiusRange').value;renderBrushCursor();});

// Strokes use original-image coordinates; previews never replace source pixels.
function updateEditControls(){const blocked=actionBusy||editBusy||selectedBlocked()||!!cropDraft||!!geometryDraft;for(const id of ['editMode','editShape','radius','radiusRange','finishPolygon','cancelSelection','resetMask'])$(id).disabled=!!blocked;$('undoEdit').disabled=!!blocked||!editHistory.undo||!!editPoints.length;$('redoEdit').disabled=!!blocked||!editHistory.redo||!!editPoints.length;for(const el of document.querySelectorAll('.viewport'))el.classList.toggle('editing',$('editMode').value!=='pan');renderBrushCursor();}
function cancelEditSelection(){editPoints=[];drawing=false;failedEdit=null;$('retrySave').hidden=true;drawSelection();updateEditControls()}
function drawSelection(){renderBrushCursor();for(const name of ['original','result']){const canvas=$(name+'Overlay'),viewport=canvas.parentElement,rect=viewport.getBoundingClientRect(),im=$(name),r=im.getBoundingClientRect();canvas.width=Math.round(rect.width);canvas.height=Math.round(rect.height);const ctx=canvas.getContext('2d');if(!editPoints.length||!detail?.dimensions||!im.complete||!im.naturalWidth)continue;const [ox,oy,ex,ey]=previewBounds(im),w=ex-ox,h=ey-oy,xy=p=>[r.left-rect.left+(p[0]-ox)/w*r.width,r.top-rect.top+(p[1]-oy)/h*r.height],points=editPoints.map(xy),shape=$('editShape').value;ctx.strokeStyle=$('editMode').value==='restore'?'#148558':'#e56932';ctx.fillStyle=$('editMode').value==='restore'?'#14855844':'#e5693244';ctx.lineWidth=2;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();if(shape==='rectangle'&&points.length>1){const a=points[0],b=points.at(-1);ctx.rect(a[0],a[1],b[0]-a[0],b[1]-a[1]);ctx.fill();ctx.stroke()}else{points.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));if(shape==='brush'){ctx.lineWidth=Math.max(1,Number($('radius').value)*2/w*r.width);if(points.length===1){ctx.lineTo(points[0][0]+.01,points[0][1])}ctx.globalAlpha=.5;ctx.stroke()}else{if(shape==='lasso')ctx.closePath();ctx.fill();ctx.stroke()}}}}
function imagePoint(e,el,clamp=false){if(!detail?.dimensions)return null;const im=el.querySelector('img'),r=im.getBoundingClientRect();if(!r.width||!im.complete||!im.naturalWidth)return null;let x=(e.clientX-r.left)/r.width,y=(e.clientY-r.top)/r.height;if(!clamp&&(x<0||x>1||y<0||y>1))return null;const [ox,oy,ex,ey]=previewBounds(im);return [ox+Math.max(0,Math.min(1,x))*(ex-ox),oy+Math.max(0,Math.min(1,y))*(ey-oy)]}
async function applyEdit(action='draw',retryPayload=null){if(editBusy||selectedBlocked()||!detail?.row)return;if(action==='draw'&&!editPoints.length&&!retryPayload)return;const id=selected,revision=detail.row.revision;const payload=retryPayload||{id,revision,action,shape:$('editShape').value,mode:$('editMode').value,radius:Number($('radius').value),points:editPoints.map(p=>p.map(v=>Math.round(v*100)/100))};editBusy=true;drawing=false;updateEditControls();setEditStatus('正在保存原图尺寸的修整…','saving');$('retrySave').hidden=true;try{const value=await api('/api/edit',payload);detail.row=value.row;editHistory={undo:value.undo,redo:value.redo};editPoints=[];failedEdit=null;refreshPreviewImages();if(value.changed)savedChange('修整完成');else syncSavedStatus();drawSelection();}catch(e){failedEdit=['draw','reset'].includes(action)?payload:null;saveFailure(e,failedEdit?'修整未确认保存，可点击“重试保存修整”；若提示候选已更新，请重新选择图片。':'操作未确认保存，请重新选择图片检查结果。');}finally{editBusy=false;$('retrySave').hidden=!failedEdit;updateEditControls();await refresh()}}
$('retrySave').onclick=()=>{if(failedEdit)applyEdit(failedEdit.action,failedEdit)};
for(const el of document.querySelectorAll('.viewport')){let last=null;el.addEventListener('wheel',e=>{e.preventDefault();if(!drawing)zoom(Math.exp(-e.deltaY*.002))},{passive:false});el.onpointerdown=e=>{if(e.button!==0||actionBusy||editBusy||selectedBlocked()||cropDraft||geometryDraft)return;if($('editMode').value==='pan'){last={x:e.clientX,y:e.clientY};el.setPointerCapture(e.pointerId);return}if(!detail?.dimensions){toast('请先生成候选');return}const pt=imagePoint(e,el);if(!pt)return;if(failedEdit){toast('请先重试保存，或取消未保存的修整');return}if($('editShape').value==='polygon'){if(editPoints.length>=1800){toast('选区点过多，请应用后继续');return}editPoints.push(pt);drawSelection();updateEditControls();return}failedEdit=null;$('retrySave').hidden=true;editPoints=[pt];drawing=true;el.setPointerCapture(e.pointerId);drawSelection()};el.onpointerleave=()=>{if(brushHover?.el===el)brushHover=null;renderBrushCursor()};el.onpointermove=e=>{brushHover={el,clientX:e.clientX,clientY:e.clientY};renderBrushCursor();if(last){view.x+=e.clientX-last.x;view.y+=e.clientY-last.y;last={x:e.clientX,y:e.clientY};transform();return}if(!drawing)return;const pt=imagePoint(e,el,true);if(!pt)return;if($('editShape').value==='rectangle')editPoints=[editPoints[0],pt];else if(editPoints.length<1799)editPoints.push(pt);drawSelection()};el.onpointerup=()=>{last=null;if(drawing){drawing=false;const shape=$('editShape').value;if(shape==='lasso'&&editPoints.length<3){cancelEditSelection();return}applyEdit()}};el.onpointercancel=()=>{last=null;cancelEditSelection()};}
$('finishPolygon').onclick=()=>{if($('editShape').value!=='polygon'||editPoints.length<3){toast('多边形至少选择三个点');return}applyEdit()};$('cancelSelection').onclick=()=>{cancelEditSelection();syncSavedStatus()};$('undoEdit').onclick=()=>applyEdit('undo');$('redoEdit').onclick=()=>applyEdit('redo');$('resetMask').onclick=()=>{if(confirm('恢复本次模型的初始蒙版并清空网页撤销历史？')){cancelEditSelection();applyEdit('reset')}};
for(const id of ['editMode','editShape'])$(id).onchange=()=>{cancelEditSelection();refreshPreviewImages(true);updateExtras()};
for(const id of ['original','result'])$(id).addEventListener('load',transform);
window.addEventListener('resize',transform);updateEditControls();

function cropFrameDimensions(){
 if(!detail?.dimensions)return [1,1];
 const row=detail.row||{};let size=[...detail.dimensions];
 for(const step of row.image_steps||[])size=step.crop?[step.crop[2]-step.crop[0],step.crop[3]-step.crop[1]]:adjustedSize(size,step.geometry);
 if(row.crop)size=[row.crop[2]-row.crop[0],row.crop[3]-row.crop[1]];
 size=adjustedSize(size,row.geometry);
 if(geometryDraft){
  if(row.output_crop)size=[row.output_crop[2]-row.output_crop[0],row.output_crop[3]-row.output_crop[1]];
  else{size=[...detail.dimensions];for(const step of row.image_steps||[])size=step.crop?[step.crop[2]-step.crop[0],step.crop[3]-step.crop[1]]:adjustedSize(size,step.geometry);if(row.crop)size=[row.crop[2]-row.crop[0],row.crop[3]-row.crop[1]];}
  size=adjustedSize(size,geometryDraft);
 }
 return size;
}
function updateExtras(){
 const ready=!selectedBlocked()&&!actionBusy&&!editBusy&&!drawing&&!editPoints.length&&!geometryDraft;
 $('startCrop').disabled=!ready||!!cropDraft;
 for(const id of ['resetCrop','saveCrop','cancelCrop']){$(id).disabled=!cropDraft||editBusy;$(id).hidden=!cropDraft;}
 $('clearCrop').disabled=!ready||!!cropDraft;
 const saved=!!(detail?.row?.crop||detail?.row?.output_crop);
 $('clearCrop').hidden=!saved||!!cropDraft;$('cropDimensions').hidden=true;
 $('cropToolbar').hidden=!cropDraft;
 $('cropSizeInputs').hidden=!cropDraft;$('cropRatioLabel').hidden=!cropDraft;$('cropRatio').disabled=editBusy;updateBackgroundControls();
 for(const id of ['cropWidth','cropHeight'])$(id).disabled=editBusy;
 if(typeof updateGeometryControls==='function')updateGeometryControls();
 drawSelection();renderExtras();
}
function renderExtras(){
 const box=$('cropBox'),size=detail?.dimensions,frame=cropFrameDimensions();
 const crop=(cropDraft||(!geometryDraft&&detail?.row?.output_crop)||null)?.map(Math.round);
 const dims=crop?[crop[2]-crop[0],crop[3]-crop[1]]:frame;
 const saved=detail?.row?.crop||detail?.row?.output_crop;
 $('cropDimensions').textContent=(crop||saved)?`裁剪宽 ${dims[0]} × 高 ${dims[1]} px`:'尚未裁剪';
 if(cropDraft){for(const [id,value,max] of [['cropWidth',dims[0],frame[0]],['cropHeight',dims[1],frame[1]]]){
  const el=$(id);el.placeholder=String(value);el.max=max;
  if(document.activeElement!==el)el.value=value;
 }}
 const hasPriorCrop=detail?.row?.image_steps?.some(step=>step.crop);
 const exported=!crop&&!saved&&!hasPriorCrop&&detail?.row?.options?.layout==='square'?[detail.row.options.size,detail.row.options.size]:dims;
 $('imageDimensions').textContent=size?`原图 ${size[0]} × ${size[1]} px${cropDraft?'':` · 导出 ${exported[0]} × ${exported[1]} px`}`:'';
 if(!cropDraft||!detail?.row?.mask){box.hidden=true;return}
 const r=$('result').getBoundingClientRect(),v=$('resultView').getBoundingClientRect(),[w,h]=frame,[x1,y1,x2,y2]=cropDraft;
 if(!$('result').complete||!r.width||!r.height){box.hidden=true;return}box.hidden=false;
 Object.assign(box.style,{left:(r.left-v.left+x1/w*r.width)+'px',top:(r.top-v.top+y1/h*r.height)+'px',width:((x2-x1)/w*r.width)+'px',height:((y2-y1)/h*r.height)+'px'});
}
$('focusMode').onclick=()=>{const focused=document.querySelector('.workspace').classList.toggle('focus');document.body.classList.toggle('canvas-focused',focused);$('focusMode').textContent=focused?'退出放大':'放大画布';requestAnimationFrame(transform)};
$('viewMode').onclick=()=>{if(cropDraft){toast('请先确认或取消裁剪');return}const single=document.querySelector('.compare').classList.toggle('result-only');$('viewMode').textContent=single?'双图对照':'仅看结果';requestAnimationFrame(transform)};
$('startCrop').onclick=async()=>{if(!detail?.row)return toast('请先生成候选');if(editPoints.length)return toast('请先完成或取消选区');try{const id=selected,revision=detail.row.revision;const value=await api(`/api/crop-suggest?id=${encodeURIComponent(id)}&revision=${revision}`);if(id!==selected||revision!==detail?.row?.revision)return;$('editMode').value='pan';cropDraft=detail.row.output_crop?[...detail.row.output_crop]:value.box;$('cropRatio').value='free';$('cropWidth').value='';$('cropHeight').value='';document.querySelector('.compare').classList.remove('original-only');refreshPreviewImages(true);updateExtras();updateEditControls();toast('可拖动裁剪框，或选填宽、高（像素）；裁剪基于校正后的右侧结果')}catch(e){toast(e.message)}};
$('resetCrop').onclick=async()=>{try{const v=await api(`/api/crop-suggest?id=${encodeURIComponent(selected)}&revision=${detail.row.revision}`);cropDraft=fitCropRatio(v.box);$('cropWidth').value='';$('cropHeight').value='';renderExtras()}catch(e){toast(e.message)}};
$('cancelCrop').onclick=()=>{cropDraft=null;refreshPreviewImages(true);updateExtras();updateEditControls();syncSavedStatus()};
async function persistCrop(box){if(!detail?.row)return toast('请先生成候选');editBusy=true;setEditStatus('正在保存裁剪…','saving');updateExtras();try{const v=await api('/api/crop',{id:selected,revision:detail.row.revision,box,space:'adjusted'});detail.row=v.row;cropDraft=null;refreshPreviewImages(true);savedChange(box?'裁剪完成':'已清除裁剪');toast(box?`裁剪已保存：${box[2]-box[0]} × ${box[3]-box[1]} 像素，已放大预览`:'已清除裁剪范围');}catch(e){saveFailure(e,'裁剪未确认保存，保留当前裁剪框，请重试。')}finally{editBusy=false;updateExtras();updateEditControls();await refresh()}}
$('clearCrop').onclick=()=>persistCrop(null);
let cropDrag=null;
$('cropBox').onpointerdown=e=>{e.stopPropagation();e.preventDefault();cropDrag={edge:e.target.dataset.edge||'move',point:[e.clientX,e.clientY],box:[...cropDraft]};e.currentTarget.setPointerCapture(e.pointerId)};
$('cropBox').onpointermove=e=>{if(!cropDrag)return;const r=$('result').getBoundingClientRect(),[w,h]=cropFrameDimensions(),dx=(e.clientX-cropDrag.point[0])*w/r.width,dy=(e.clientY-cropDrag.point[1])*h/r.height,[x1,y1,x2,y2]=cropDrag.box,edge=cropDrag.edge;if(edge==='move'){const x=Math.max(-x1,Math.min(w-x2,dx)),y=Math.max(-y1,Math.min(h-y2,dy));cropDraft=[x1+x,y1+y,x2+x,y2+y]}else{cropDraft=[edge.includes('w')?Math.max(0,Math.min(x2-1,x1+dx)):x1,edge.includes('n')?Math.max(0,Math.min(y2-1,y1+dy)):y1,edge.includes('e')?Math.min(w,Math.max(x1+1,x2+dx)):x2,edge.includes('s')?Math.min(h,Math.max(y1+1,y2+dy)):y2]}if(edge!=='move')cropDraft=fitCropRatio(cropDraft,edge==='n'||edge==='s'?'height':'width',false);renderExtras()};
$('cropBox').onpointerup=()=>{cropDrag=null};$('cropBox').onpointercancel=()=>{cropDrag=null};
for(const name of ['original','result'])$(name).addEventListener('load',renderExtras);

$('cropBox').onkeydown=e=>{
 if(!cropDraft||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key))return;
 e.preventDefault();e.stopPropagation();const [w,h]=cropFrameDimensions(),dx=e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:0,dy=e.key==='ArrowUp'?-1:e.key==='ArrowDown'?1:0;
 let [x1,y1,x2,y2]=cropDraft.map(Math.round);
 if(e.shiftKey){x2=Math.max(x1+1,Math.min(w,x2+dx));y2=Math.max(y1+1,Math.min(h,y2+dy))}
 else{const x=Math.max(-x1,Math.min(w-x2,dx)),y=Math.max(-y1,Math.min(h-y2,dy));x1+=x;x2+=x;y1+=y;y2+=y}
 cropDraft=e.shiftKey?fitCropRatio([x1,y1,x2,y2],dy?'height':'width',false):[x1,y1,x2,y2];renderExtras();
};

function applyCropDimensions(changed){
 if(!cropDraft||editBusy)return false;
 const [maxW,maxH]=cropFrameDimensions(),[x1,y1,x2,y2]=cropDraft;
 let w=$('cropWidth').value===''?Math.round(x2-x1):Number($('cropWidth').value);
 let h=$('cropHeight').value===''?Math.round(y2-y1):Number($('cropHeight').value);
 const ratio=cropRatio();if(ratio){if(changed==='cropHeight')w=Math.max(1,Math.round(h*ratio));else h=Math.max(1,Math.round(w/ratio));}
 if(!Number.isInteger(w)||!Number.isInteger(h)||w<1||h<1||w>maxW||h>maxH){toast(`请输入范围内的整数像素：宽 1–${maxW}，高 1–${maxH}`);return false;}
 const x=Math.max(0,Math.min(maxW-w,Math.round((x1+x2-w)/2))),y=Math.max(0,Math.min(maxH-h,Math.round((y1+y2-h)/2)));
 cropDraft=[x,y,x+w,y+h];renderExtras();return true;
}
for(const id of ['cropWidth','cropHeight']){
 $(id).onchange=()=>applyCropDimensions(id);
 $(id).onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();applyCropDimensions(id);}};
}
$('saveCrop').onclick=()=>{if(applyCropDimensions())persistCrop(cropDraft.map(Math.round));};


function cropRatio(){
 const value=$('cropRatio').value;
 if(value==='original')return detail.dimensions[0]/detail.dimensions[1];
 return value==='free'?null:Number(value);
}
function fitCropRatio(box,changed='width',expand=true){
 const ratio=cropRatio();if(!ratio)return box;
 const [maxW,maxH]=cropFrameDimensions();let [x1,y1,x2,y2]=box,w=x2-x1,h=y2-y1;
 if(expand){if(w/h<ratio)w=h*ratio;else h=w/ratio;}
 else if(changed==='height')w=h*ratio;else h=w/ratio;
 const scale=Math.min(1,maxW/w,maxH/h);w=Math.max(1,Math.round(w*scale));h=Math.max(1,Math.round(h*scale));
 const x=Math.max(0,Math.min(maxW-w,Math.round((x1+x2-w)/2))),y=Math.max(0,Math.min(maxH-h,Math.round((y1+y2-h)/2)));
 return [x,y,x+w,y+h];
}
$('cropRatio').onchange=()=>{if(cropDraft){cropDraft=fitCropRatio(cropDraft);renderExtras();}};
function updateBackgroundControls(){
 const value=detail?.row?.background||'#ffffff',custom=!['#ffffff','transparent','#438edb','#d84148'].includes(value);
 $('backgroundMode').value=custom||backgroundChoosing?'custom':value;
 if(!backgroundChoosing){$('backgroundColor').value=value==='transparent'?'#ffffff':value;$('backgroundHex').value=$('backgroundColor').value.toUpperCase();}
 $('customColorControls').hidden=$('backgroundMode').value!=='custom';
 const disabled=actionBusy||editBusy||selectedBlocked()||!!cropDraft||!!geometryDraft||drawing||!!editPoints.length;
 $('backgroundMode').disabled=disabled;$('backgroundColor').disabled=disabled;$('backgroundHex').disabled=disabled;
 $('backgroundHint').textContent=value==='transparent'?'透明背景 · 导出 PNG':'纯色背景 · 导出 JPG';
}
async function saveBackground(value){
 if(!detail?.row||editBusy||actionBusy)return;
 editBusy=true;setEditStatus('正在保存背景…','saving');updateActionControls();
 try{const v=await api('/api/background',{id:selected,revision:detail.row.revision,background:value});detail.row=v.row;backgroundChoosing=false;refreshPreviewImages();updateFilenamePreview();savedChange('背景设置完成');toast('背景已保存，确认通过后导出');}
 catch(e){saveFailure(e,'背景未确认保存，请重新选择颜色重试。')}finally{backgroundChoosing=false;editBusy=false;updateActionControls();await refresh();}
}
$('backgroundMode').onchange=()=>{
 const value=$('backgroundMode').value;
 if(value==='custom'){backgroundChoosing=true;updateBackgroundControls();$('backgroundHex').focus();$('backgroundHex').select();return;}
 backgroundChoosing=false;saveBackground(value);
};
$('backgroundColor').oninput=()=>{backgroundChoosing=true;$('backgroundHex').value=$('backgroundColor').value.toUpperCase();};
$('backgroundColor').onchange=()=>saveBackground($('backgroundColor').value);
$('backgroundHex').oninput=()=>{backgroundChoosing=true;$('backgroundHex').setCustomValidity('');};
$('backgroundHex').onchange=()=>{let value=$('backgroundHex').value.trim();if(!value.startsWith('#'))value='#'+value;if(!/^#[0-9a-f]{6}$/i.test(value)){$('backgroundHex').setCustomValidity('请输入六位颜色值，例如 #F3E8DC');$('backgroundHex').reportValidity();return;}$('backgroundHex').setCustomValidity('');saveBackground(value.toLowerCase());};
$('backgroundHex').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('backgroundHex').onchange();}};
