let cloudToken=null,cloudImageId=null,cloudInFlight=false;
async function cloudStatus(){
 const value=await api('/api/cloud/config');
 $('cloudBucket').value=value.bucket||'';$('cloudRegion').value=value.region||'';
 $('cloudSecretId').value='';$('cloudSecretKey').value='';
 $('cloudConfigStatus').textContent=(value.configured?'已保存配置；修改配置时请重新填写密钥。':'尚未配置。')+(value.pending_cleanup?` 有 ${value.pending_cleanup} 条临时文件待核实或清理记录，不代表文件一定已上传。`:'');
}
$('cloudSettings').onclick=async()=>{try{await cloudStatus();$('cloudSettingsDialog').showModal()}catch(e){toast(e.message)}};
$('cloudSettingsDialog').addEventListener('close',()=>{$('cloudSecretId').value='';$('cloudSecretKey').value=''});
$('cloudSave').onclick=async()=>{try{
 await api('/api/cloud/config',{bucket:$('cloudBucket').value,region:$('cloudRegion').value,secret_id:$('cloudSecretId').value,secret_key:$('cloudSecretKey').value});
 $('cloudSecretId').value='';$('cloudSecretKey').value='';await cloudStatus();toast('腾讯云配置已保存至钥匙串');
}catch(e){toast(e.message)}};
$('cloudCleanup').onclick=async()=>{try{const v=await api('/api/cloud/cleanup',{});await cloudStatus();toast(v.remaining?`仍有 ${v.remaining} 个临时文件待清理，请检查当前账号及网络`:'当前配置的临时文件已清理')}catch(e){toast(e.message)}};
$('cloudRetry').onclick=async()=>{
 if(cloudInFlight)return toast('已有腾讯云操作正在进行');
 if(actionBusy||editBusy||drawing||editPoints.length||failedEdit||geometryDraft||cropDraft)return toast('请先完成当前编辑');
 if(selectedBlocked())return toast('请先生成本张本地候选');
 cloudInFlight=true;
 let failed=false;
 try{
 const settings=await api('/api/cloud/config');
 if(!settings.configured){await cloudStatus();$('cloudSettingsDialog').showModal();return}
 if(!confirm(`将本张原图上传到腾讯云存储桶 ${settings.bucket} 并调用通用抠图，可能产生费用。处理后会尝试删除临时文件。继续？`))return;
 const id=selected,revision=detail.row.revision;setEditStatus('正在上传并生成腾讯云候选，请稍候…','saving');
 const value=await api('/api/cloud/run',{id,revision,confirmed:true});
 if(id!==selected||detail?.row?.revision!==revision){await api('/api/cloud/discard',{});toast('图片已切换或修改，云端候选未应用');return}
 cloudToken=value.token;cloudImageId=id;
 $('cloudCurrent').src=imageURL(id,'result',false,revision)+'&crop=1';
 $('cloudCandidate').src='/api/cloud/preview?token='+encodeURIComponent(cloudToken);
 $('cloudCleanupWarning').textContent=value.remaining?`有 ${value.remaining} 个临时文件待清理，请到腾讯云设置重试。`:'';
 $('cloudCandidateDialog').showModal();
 }catch(e){failed=true;saveFailure(e,'请检查当前状态后重试。')}finally{cloudInFlight=false;if(!failed)syncSavedStatus();updateActionControls()}
};
$('cloudDiscard').onclick=async()=>{try{await api('/api/cloud/discard',{});cloudToken=null;$('cloudCandidateDialog').close();syncSavedStatus()}catch(e){toast(e.message)}};
$('cloudCandidateDialog').addEventListener('cancel',e=>{e.preventDefault();$('cloudDiscard').click()});
$('cloudAccept').onclick=async()=>{
 if(!cloudToken||cloudImageId!==selected)return toast('图片已切换，请重新生成');
 if(!confirm('使用云端结果会替换当前蒙版，清除本张画笔修整、裁剪、旋转与校正，并移除本工具生成的旧导出。确认后需要重新审核。继续？'))return;
 try{await api('/api/cloud/accept',{id:selected,token:cloudToken});cloudToken=null;$('cloudCandidateDialog').close();if(lastExport?.id===selected)lastExport=null;await select(selected,true);await refresh();toast('已采用云端结果，请重新审核')}catch(e){toast(e.message)}
};
