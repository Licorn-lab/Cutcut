// One small transparent base per edit session; pointer input never starts a server render.
let geometryBaseImage=null,geometryBaseKey='',geometryPreviewTicket=0,geometryPaintKey='';
function perspectiveMatrix(w,h,g){
 const p=g.horizontal*.003,q=g.vertical*.003;
 const corners=[[-1,-1],[1,-1],[1,1],[-1,1]].map(([x,y])=>[x/(1+p*x+q*y),y/(1+p*x+q*y)]);
 const lo=[Math.min(...corners.map(c=>c[0])),Math.min(...corners.map(c=>c[1]))];
 const hi=[Math.max(...corners.map(c=>c[0])),Math.max(...corners.map(c=>c[1]))];
 const multiply=(a,b)=>a.map(row=>b[0].map((_,j)=>row.reduce((sum,v,k)=>sum+v*b[k][j],0)));
 const m=multiply(multiply([[w/(hi[0]-lo[0]),0,-lo[0]*w/(hi[0]-lo[0])],[0,h/(hi[1]-lo[1]),-lo[1]*h/(hi[1]-lo[1])],[0,0,1]],[[1,0,0],[0,1,0],[p,q,1]]),[[2/w,0,-1],[0,2/h,-1],[0,0,1]]);
 return [m[0][0],m[1][0],0,m[2][0],m[0][1],m[1][1],0,m[2][1],0,0,1,0,m[0][2],m[1][2],0,m[2][2]];
}
function syncGeometryPreview(){
 if(!geometryDraft){
  geometryPreviewTicket++;geometryBaseKey='';geometryBaseImage=null;geometryPaintKey='';
  cancelAnimationFrame(geometryTimer);$('geometryPreview').hidden=true;$('result').style.visibility='';return;
 }
 const key=imageURL(selected,'result',false,detail.row.revision)+'&geometry_base=1';
 if(key===geometryBaseKey)return;
 geometryBaseKey=key;geometryBaseImage=null;geometryPaintKey='';const ticket=++geometryPreviewTicket,im=new Image();
 $('geometryPreview').hidden=true;$('result').style.visibility='';
 im.onload=()=>{if(ticket!==geometryPreviewTicket||!geometryDraft)return;geometryBaseImage=im;renderLiveGeometry();updateGeometryControls();setEditStatus('拖动滑块即可实时查看右侧变化，确认调整后保存。','draft');};
 im.onerror=()=>{if(ticket!==geometryPreviewTicket||!geometryDraft)return;closeGeometry();toast('实时预览加载失败，请重试旋转与校正');};
 im.src=key;
}
function positionGeometryPreview(){
 if(!geometryDraft||!geometryBaseImage||$('geometryPreview').hidden)return;
 const viewport=$('resultView'),[w,h]=cropFrameDimensions(),fit=Math.min(viewport.clientWidth/w,viewport.clientHeight/h),dw=w*fit,dh=h*fit;
 Object.assign($('geometryPreview').style,{width:dw+'px',height:dh+'px',transform:`translate(${view.x}px,${view.y}px) scale(${view.scale})`,background:detail.row.background==='transparent'?'transparent':detail.row.background||'#ffffff'});
 $('geometrySurface').style.transform=`matrix3d(${perspectiveMatrix(dw,dh,geometryDraft).join(',')})`;
}
function renderLiveGeometry(){
 if(!geometryDraft||!geometryBaseImage)return;
 const g=geometryDraft,im=geometryBaseImage,canvas=$('geometrySurface'),key=[g.turns,g.flip,g.angle].join('/');
 if(key!==geometryPaintKey){
  const [w,h]=adjustedSize([im.naturalWidth,im.naturalHeight],g);canvas.width=w;canvas.height=h;
  const ctx=canvas.getContext('2d');ctx.translate(w/2,h/2);ctx.rotate((g.turns*90+g.angle)*Math.PI/180);ctx.scale(g.flip?-1:1,1);ctx.drawImage(im,-im.naturalWidth/2,-im.naturalHeight/2);geometryPaintKey=key;
 }
 $('geometryPreview').hidden=false;$('result').style.visibility='hidden';positionGeometryPreview();renderGeometryGrid();
}

// Geometry is a per-image output instruction; masks and the original stay untouched.
function geometryValue(){return {turns:0,flip:false,angle:0,horizontal:0,vertical:0,...(detail?.row?.output_crop?{}:detail?.row?.geometry)}}
function hasGeometry(value){return value&&(value.turns||value.flip||value.angle||value.horizontal||value.vertical)}
function rawEditingView(){return !cropDraft&&$('editMode').value!=='pan'}
function adjustedSize(size,g){let [w,h]=size;if((g?.turns||0)%2)[w,h]=[h,w];if(g?.angle){const a=g.angle*Math.PI/180,c=Math.abs(Math.cos(a)),s=Math.abs(Math.sin(a));[w,h]=[Math.ceil(w*c+h*s),Math.ceil(w*s+h*c)]}return [w,h]}
function updateGeometryControls(){
 const busy=actionBusy||editBusy||selectedBlocked();
 $('startGeometry').disabled=busy||!detail?.row?.mask||!!cropDraft||drawing||!!editPoints.length;
 $('geometryPanel').hidden=!geometryDraft;
 $('startGeometry').textContent=geometryDraft?'正在调整图片':'旋转与校正';
 for(const el of $('geometryPanel').querySelectorAll('button,input'))el.disabled=busy||(!geometryBaseImage&&!['cancelGeometry','resetComposition'].includes(el.id));
 if(!geometryDraft)return;
 for(const key of ['angle','horizontal','vertical']){
  $(key+'Range').value=geometryDraft[key];if(document.activeElement!==$(key+'Number'))$(key+'Number').value=geometryDraft[key];
 }
 $('flipImage').setAttribute('aria-pressed',String(geometryDraft.flip));
 $('geometrySummary').textContent=`顺时针 ${geometryDraft.turns*90}°${geometryDraft.flip?' · 已水平翻转':''} · 只调整右侧结果`;
}
function renderGeometryGrid(){
 const grid=$('geometryGrid');grid.hidden=!geometryDraft;
 if(!geometryDraft)return;
 const r=(!$('geometryPreview').hidden?$('geometryPreview'):$('result')).getBoundingClientRect(),v=$('resultView').getBoundingClientRect();
 Object.assign(grid.style,{left:r.left-v.left+'px',top:r.top-v.top+'px',width:r.width+'px',height:r.height+'px'});
}
function scheduleGeometryPreview(){
 cancelAnimationFrame(geometryTimer);
 updateGeometryControls();renderExtras();
 setEditStatus('实时预览 · 点击“确认调整”保存，或取消还原。','draft');
 geometryTimer=requestAnimationFrame(renderLiveGeometry);
}
function closeGeometry(){cancelAnimationFrame(geometryTimer);geometryDraft=null;updateGeometryControls();refreshPreviewImages(true);updateActionControls()}
$('startGeometry').onclick=()=>{
 if(geometryDraft)return;
 if(cropDraft||editPoints.length)return toast('请先完成或取消当前裁剪和选区');
 if(!detail?.row?.mask)return toast('请先生成候选');
 $('editMode').value='pan';geometryDraft=geometryValue();
 updateGeometryControls();updateActionControls();refreshPreviewImages(true);
 setEditStatus('正在准备实时预览… 已有裁剪保留，左侧原图不变。','draft');
};
for(const key of ['angle','horizontal','vertical']){
 const change=e=>{
  if(!geometryDraft)return;
  const value=Number(e.target.value),limit=key==='angle'?45:100;
  if(!Number.isFinite(value)||e.target.value==='')return;
  geometryDraft[key]=Math.round(Math.max(-limit,Math.min(limit,value))*10)/10;
  scheduleGeometryPreview();
 };
 $(key+'Range').oninput=change;$(key+'Number').oninput=change;
}
$('rotateImage').onclick=()=>{geometryDraft.turns=(geometryDraft.turns+1)%4;scheduleGeometryPreview()};
$('flipImage').onclick=()=>{geometryDraft.flip=!geometryDraft.flip;scheduleGeometryPreview()};
$('resetGeometry').onclick=()=>{geometryDraft={turns:0,flip:false,angle:0,horizontal:0,vertical:0};scheduleGeometryPreview()};
$('resetComposition').onclick=async()=>{
 if(editBusy||!confirm('清除本张所有旋转、透视校正和裁剪，恢复初始构图？画笔修整与背景颜色会保留。'))return;
 cancelAnimationFrame(geometryTimer);editBusy=true;setEditStatus('正在保存图片调整…','saving');updateActionControls();
 try{const value=await api('/api/geometry',{id:selected,revision:detail.row.revision,reset_composition:true});detail.row=value.row;geometryDraft=null;refreshPreviewImages(true);savedChange('已恢复初始构图，画笔修整与背景保留');toast('已恢复初始构图，画笔修整与背景保留');}
 catch(e){saveFailure(e,'调整未确认保存，请重试或取消本次调整。')}finally{editBusy=false;updateActionControls();await refresh()}
};
$('cancelGeometry').onclick=()=>{closeGeometry();syncSavedStatus()};
$('saveGeometry').onclick=async()=>{
 if(!geometryDraft||editBusy)return;
 cancelAnimationFrame(geometryTimer);editBusy=true;setEditStatus('正在保存图片调整…','saving');updateActionControls();
 try{
  const value=await api('/api/geometry',{id:selected,revision:detail.row.revision,geometry:geometryDraft});
  detail.row=value.row;geometryDraft=null;updateGeometryControls();refreshPreviewImages(true);
  savedChange('旋转与校正完成');toast('已保存调整并更新右侧预览');
 }catch(e){saveFailure(e,'调整未确认保存，请重试或取消本次调整。')}finally{editBusy=false;updateActionControls();await refresh()}
};
$('result').addEventListener('load',renderGeometryGrid);
updateGeometryControls();
