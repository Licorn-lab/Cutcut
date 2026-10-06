"""Compare candidate masks with explicit heuristic scores; never approve an image."""
from dataclasses import replace
import numpy as np
from PIL import Image


def evaluate_mask(image,cutout):
    import cv2
    if cutout.size!=image.size:return {'score':-1000.,'bad':True,'reason':'蒙版尺寸不一致'}
    size=(min(256,image.width),min(256,image.height))
    alpha=np.asarray(cutout.getchannel('A').resize(size,Image.Resampling.BILINEAR),np.float32)/255
    if not np.isfinite(alpha).all():return {'score':-1000.,'bad':True,'reason':'蒙版包含无效值'}
    hard=alpha>.5;area=float(hard.mean())
    if area<.001:return {'score':-1000.,'bad':True,'reason':'未识别到有效主体'}
    rgb=np.asarray(image.convert('RGB').resize(size,Image.Resampling.BILINEAR),np.float32)/255
    border=np.concatenate([rgb[0],rgb[-1],rgb[:,0],rgb[:,-1]])
    background=np.median(border,axis=0)
    distance=np.linalg.norm(rgb-background,axis=2)
    # Background-based term is used only when the border is largely uniform.
    plain=float(np.mean(np.linalg.norm(border-background,axis=1)<.12))>.85
    outline=(cv2.dilate(hard.astype(np.uint8),np.ones((3,3),np.uint8))-cv2.erode(hard.astype(np.uint8),np.ones((3,3),np.uint8))).astype(bool)
    uncertain=float(np.mean((alpha>.1)&(alpha<.9)))
    edge_contact=float(np.mean(np.concatenate([hard[0],hard[-1],hard[:,0],hard[:,-1]])))
    gray=cv2.cvtColor((rgb*255).astype(np.uint8),cv2.COLOR_RGB2GRAY)
    edges=cv2.Canny(gray,50,120)>0
    nearby=cv2.dilate(edges.astype(np.uint8),np.ones((3,3),np.uint8)).astype(bool)
    alignment=float(nearby[outline].mean()) if outline.any() else 0
    score=40+15*alignment-15*uncertain-15*edge_contact
    recall=1.;residue=0.
    terms=['边缘与原图结构、透明过渡和边界占用比较']
    if plain:
        signal=distance>.18;quiet=distance<.08
        recall=float(alpha[signal].mean()) if signal.any() else 0
        residue=float(alpha[quiet].mean()) if quiet.any() else 0
        score+=35*recall-40*residue
        terms.append('均匀背景下的主体保留与背景残留比较')
    bad=area>.985 or (plain and area<.005)
    if bad:score-=60
    triggers=[]
    if bad:triggers.append('主体面积异常')
    if uncertain>.12:triggers.append('透明过渡区域较大')
    if alignment<.35:triggers.append('边缘与原图结构对齐较弱')
    if plain and recall<.85:triggers.append('可能遗漏主体')
    if plain and residue>.08:triggers.append('可能残留背景')
    return {'score':round(score,3),'bad':bool(bad),'suspicious':bool(triggers),'triggers':triggers,'reason':'；'.join(terms)}


class AutoSession:
    def __init__(self,factory,remover):
        self.factory=factory;self.remover=remover;self.sessions={}
    def predict(self,image,options):
        candidates=[];errors=[]
        for model in ['isnet-general-use','birefnet-general','u2net']:
            if model=='u2net' and any(not item[2]['bad'] for item in candidates):break
            print('自动选模：正在比较 '+model,flush=True)
            try:
                if model not in self.sessions:self.sessions[model]=self.factory(model)
                cutout=self.remover(image,self.sessions[model],replace(options,model=model))
                metrics=evaluate_mask(image,cutout)
                if model=='isnet-general-use' and not metrics['bad']:
                    from core import render_white,inspect_cutout
                    screening=replace(options,model=model,layout='original')
                    white,_=render_white(cutout,screening)
                    warnings=inspect_cutout(image,cutout,white,screening)
                    if warnings:
                        metrics['suspicious']=True
                        metrics.setdefault('triggers',[]).extend(warnings)
                candidates.append((model,cutout,metrics))
                if model=='isnet-general-use' and not metrics.get('suspicious',metrics['bad']):break
            except Exception as error:
                errors.append(model+'：'+str(error))
        usable=[item for item in candidates if item[2]['score']>-1000]
        if not usable:raise RuntimeError('自动选模没有有效候选；'+'；'.join(errors))
        # A small tie preference is stable and does not merge contradictory masks.
        preferred=[item for item in usable if not item[2]['bad']] or usable
        winner=max(preferred,key=lambda item:(item[2]['score'],item[0]=='birefnet-general'))
        model,cutout,metrics=winner
        result=cutout.copy()
        result.info['routing']={'selected':model,'reason':('默认模型检查未触发换模；' if len(candidates)==1 and not errors else '默认结果可疑或失败，已比较其他模型；')+metrics['reason']+'（启发式选择，仍需人工审核）',
          'scores':{item[0]:item[2]['score'] for item in candidates},'errors':errors,
          'fallback_triggered':len(candidates)>1 or bool(errors),
          'trigger_reasons':candidates[0][2].get('triggers',[]) if candidates else [],
          'uncertain':bool(metrics['bad'] or errors)}
        result.info['auto_warnings']=[]
        if metrics['bad']:result.info['auto_warnings'].append('自动选模候选仍有明显异常，请重点检查主体和背景。')
        if errors:result.info['auto_warnings'].append('部分候选模型未完成：'+'；'.join(errors))
        print('自动选模：选用 '+model,flush=True)
        return result
