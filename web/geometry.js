// Geometry is a per-image output instruction; masks and the original stay untouched.
function geometryValue(){return {turns:0,flip:false,angle:0,horizontal:0,vertical:0,...detail?.row?.geometry}}
function hasGeometry(value){return value&&(value.turns||value.flip||value.angle||value.horizontal||value.vertical)}
function rawEditingView(){return !cropDraft&&$('editMode').value!=='pan'}
function adjustedSize(size,g){let [w,h]=size;if((g?.turns||0)%2)[w,h]=[h,w];if(g?.angle){const a=g.angle*Math.PI/180,c=Math.abs(Math.cos(a)),s=Math.abs(Math.sin(a));[w,h]=[Math.ceil(w*c+h*s),Math.ceil(w*s+h*c)]}return [w,h]}
function updateGeometryControls(){
 const busy=actionBusy||editBusy||selectedBlocked();
 $('startGeometry').disabled=busy||!detail?.row?.mask||!!cropDraft||drawing||!!editPoints.length;
 $('geometryPanel').hidden=!geometryDraft;
 $('startGeometry').textContent=geometryDraft?'正在调整图片':'旋转与校正';
 for(const el of $('geometryPanel').querySelectorAll('button,input'))el.disabled=busy;
 if(!geometryDraft)return;
 for(const key of ['angle','horizontal','vertical']){
  $(key+'Range').value=geometryDraft[key];$(key+'Number').value=geometryDraft[key];
 }
 $('flipImage').setAttribute('aria-pressed',String(geometryDraft.flip));
 $('geometrySummary').textContent=`顺时针 ${geometryDraft.turns*90}°${geometryDraft.flip?' · 已水平翻转':''} · 只调整右侧结果`;
}
function renderGeometryGrid(){
 const grid=$('geometryGrid');grid.hidden=!geometryDraft;
 if(!geometryDraft)return;
 const r=$('result').getBoundingClientRect(),v=$('resultView').getBoundingClientRect();
 Object.assign(grid.style,{left:r.left-v.left+'px',top:r.top-v.top+'px',width:r.width+'px',height:r.height+'px'});
}
function scheduleGeometryPreview(){
 clearTimeout(geometryTimer);
 updateGeometryControls();renderExtras();
 $('editStatus').textContent='调整预览中，点击“确认调整”保存；左侧原图保持不变。';
 geometryTimer=setTimeout(()=>{if(geometryDraft)refreshPreviewImages(true)},180);
}
function closeGeometry(){clearTimeout(geometryTimer);geometryDraft=null;updateGeometryControls();refreshPreviewImages(true);updateActionControls()}
$('startGeometry').onclick=()=>{
 if(geometryDraft)return;
 if(cropDraft||editPoints.length)return toast('请先完成或取消当前裁剪和选区');
 if(!detail?.row?.mask)return toast('请先生成候选');
 $('editMode').value='pan';geometryDraft=geometryValue();
 updateGeometryControls();updateActionControls();refreshPreviewImages(true);
 $('editStatus').textContent='拖动滑块或输入数值；更改校正后需重新裁剪，左侧原图不变。';
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
$('cancelGeometry').onclick=()=>{closeGeometry();$('editStatus').textContent='已取消本次调整，保留此前保存的结果。'};
$('saveGeometry').onclick=async()=>{
 if(!geometryDraft||editBusy)return;
 clearTimeout(geometryTimer);editBusy=true;updateActionControls();
 try{
  const value=await api('/api/geometry',{id:selected,revision:detail.row.revision,geometry:geometryDraft});
  detail.row=value.row;geometryDraft=null;updateGeometryControls();refreshPreviewImages(true);
  $('editStatus').textContent='图片调整已保存；人工确认通过后导出。';toast('已保存调整并更新右侧预览');
 }catch(e){toast(e.message)}finally{editBusy=false;updateActionControls();await refresh()}
};
$('result').addEventListener('load',renderGeometryGrid);
updateGeometryControls();
