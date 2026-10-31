"""MobileCLIP-S0 zero-shot classification: one CPU process, no agent or network.
Scores are cosine similarities, never calibrated probabilities.
"""
from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
import time
from PIL import Image, ImageOps
from core import digest

ROOT = Path(__file__).resolve().parent / 'classification'
MODEL = 'MobileCLIP-S0/CoreML-CPU'
# Fixed before sample testing. These descriptions define comparisons, not training data.
CATEGORIES = {
    '包袋': ['a photo of an embroidered fabric handbag', 'a photo of a woven shoulder bag'],
    '围巾披肩': ['a photo of a woven scarf with tassels', 'a photo of a textile shawl'],
    '平面织物': ['a photo of a rectangular piece of patterned woven fabric', 'a photo of a traditional embroidered textile panel'],
    '服饰': ['a photo of traditional embroidered clothing', 'a photo of a traditional textile garment'],
    '其他物品': ['a photo of a textile accessory', 'a photo of a handmade decorative object'],
}
FEATURE_PROMPTS = {
    '流苏与细带': (['a textile with many hanging fringe threads and tassels', 'a fabric bag with thin straps or cords'], ['a textile with a plain solid edge and no fringe', 'a fabric object with no straps or cords']),
    '浅色主体': (['a white or pale cream colored fabric object', 'a light colored textile product'], ['a dark colored fabric object', 'a brightly colored textile product']),
    '复杂背景': (['a textile product against a cluttered indoor background', 'a fabric object on a busy patterned background'], ['a textile product on a plain white background', 'a fabric object on a simple uniform background']),
    '常规完整主体': (['a complete fabric product fully visible in the photograph', 'a whole textile object with simple solid edges'], ['a close up photograph of a partly cropped textile object', 'a textile with complex frayed edges']),
    '镂空与间隙': (['a textile with visible open holes and gaps between threads', 'a fabric object with openwork lace'], ['a textile with dense solid fabric and no holes', 'a fabric object with a solid closed surface']),
    '多件物品': (['several separate textile products in one photograph', 'multiple fabric objects displayed together'], ['one single textile product in the photograph', 'a single fabric object displayed alone']),
    '主体触边': (['a textile product cut off by the border of the photograph', 'a fabric object extending beyond the edge of the image'], ['a complete textile product surrounded by empty background', 'a fabric object centered with space around it']),
    '旧物磨损边缘': (['an old worn textile with frayed damaged edges', 'an antique fabric with torn edges'], ['a new textile with neat finished edges', 'a fabric object with clean intact edges']),
}

def prompt_spec():
    return {'categories': CATEGORIES, 'features': FEATURE_PROMPTS, 'preprocess': 'full-square-white-padding-and-center-crop-256/v1', 'model': MODEL}

def prompts():
    return [p for values in CATEGORIES.values() for p in values] + [p for pair in FEATURE_PROMPTS.values() for group in pair for p in group]

def ready():
    return all(p.exists() for p in [ROOT/'encoder', ROOT/'text_vectors.json', ROOT/'models/mobileclip_s0_image.mlmodelc'])

def _normalize(v):
    import numpy as np
    v = np.asarray(v, dtype=float)
    return v / max(float(np.linalg.norm(v)), 1e-12)

def interpret(vectors, text_vectors):
    import numpy as np
    image = _normalize(np.mean([_normalize(v) for v in vectors], axis=0))
    sims = [_normalize(v).dot(image).item() for v in text_vectors]
    offset=0; categories=[]; features={}
    for label, values in CATEGORIES.items():
        score=sum(sims[offset:offset+len(values)])/len(values); offset+=len(values)
        categories.append({'label':label,'similarity':round(score,5)})
    categories.sort(key=lambda x:x['similarity'], reverse=True)
    for label, (yes,no) in FEATURE_PROMPTS.items():
        positive=sum(sims[offset:offset+len(yes)])/len(yes);offset+=len(yes)
        negative=sum(sims[offset:offset+len(no)])/len(no);offset+=len(no)
        margin=positive-negative
        features[label]={'positive_similarity':round(positive,5),'negative_similarity':round(negative,5),'margin':round(margin,5),'suggested':True if margin >= .025 else False if margin <= -.025 else None}
    return {'category_candidates':categories[:3], 'category_uncertain':categories[0]['similarity']-categories[1]['similarity'] < .025, 'feature_scores':features,'suggested_features':[k for k,v in features.items() if v['suggested'] is True]}

def classify(path):
    if not ready():raise RuntimeError('预训练分类模型尚未准备好，请先完成模型安装测试。')
    started=time.monotonic();path=Path(path)
    before=digest(path)
    cache=json.loads((ROOT/'text_vectors.json').read_text())
    if cache['spec'] != json.loads(json.dumps(prompt_spec())):raise RuntimeError('分类描述已改变，需要重新生成文字向量。')
    # Only two tiny views; originals stay on this machine and are never modified.
    with tempfile.TemporaryDirectory(prefix='whiteflow-classify-') as directory:
        with Image.open(path) as opened:
            image=ImageOps.exif_transpose(opened).convert('RGB')
            full=ImageOps.pad(image,(256,256),method=Image.Resampling.BICUBIC,color='white')
            crop=ImageOps.fit(image,(256,256),method=Image.Resampling.BICUBIC)
            image.close()
        files=[]
        for name,view in [('full',full),('crop',crop)]:
            file=Path(directory)/(name+'.png');view.save(file);files.append(str(file))
        result=subprocess.run([str(ROOT/'encoder'),'image',str(ROOT/'models/mobileclip_s0_image.mlmodelc'),*files],capture_output=True,text=True,timeout=45)
        if result.returncode:raise RuntimeError('分类模型运行失败：'+result.stderr[-1000:])
        rows=[json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if digest(path)!=before:raise RuntimeError('原图已改变，丢弃本次分类建议。')
    output=interpret([r['embedding'] for r in rows],cache['vectors'])
    output.update(model=MODEL,sha256=before,seconds=round(time.monotonic()-started,3),status='draft',spec_hash=hashlib.sha256(json.dumps(cache['spec'],sort_keys=True).encode()).hexdigest(),note='零样本相似度建议，未校准为概率；所有特征需人工确认。')
    return output

if __name__=='__main__':
    import sys
    if sys.argv[1:] == ['--prepare-text']:
        target=ROOT/'prompts.json';target.write_text(json.dumps(prompts()))
        result=subprocess.run([str(ROOT/'encoder'),'text',str(ROOT/'models/mobileclip_s0_text.mlmodelc'),str(ROOT/'vendor'),str(target)],check=True,capture_output=True,text=True,timeout=120)
        (ROOT/'text_vectors.json').write_text(json.dumps({'spec':prompt_spec(),'vectors':json.loads(result.stdout)},ensure_ascii=False))
        print('Cached text embeddings:',len(prompts()))
    else:
        for arg in sys.argv[1:]:print(json.dumps({'path':arg,**classify(arg)},ensure_ascii=False),flush=True)
