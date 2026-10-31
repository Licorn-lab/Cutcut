"""Task context with pretrained visual classification drafts and human confirmation."""
from pathlib import Path
import hashlib
import json
from copy import deepcopy
from core import atomic_json, digest, collect_images

MODELS = {'随批次设置': '', '快速 · U²-Net': 'u2net', '备用 · IS-Net': 'isnet-general-use', '质量 · BiRefNet General': 'birefnet-general'}
CATEGORIES = ['待确认', '包袋', '围巾披肩', '平面织物', '服饰', '其他物品']
FEATURES = ['流苏与细带', '浅色主体', '复杂背景', '常规完整主体', '镂空与间隙', '多件物品', '主体触边', '旧物磨损边缘']
DEFAULT_RULES = '白色背景；保留主体、流苏、细带和原有磨损边缘；不裁切主体，不覆盖原图。逐图例外以本张要求为准。'
DATA = Path(__file__).resolve().parent / 'data' / 'task-cards'

def context_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

class TaskCards:
    def __init__(self, source, directory=None):
        self.source = Path(source).resolve()
        self.path = Path(directory or DATA) / (context_hash(str(self.source)) + '.json')
        self.data = {'schema': 'whiteflow-task-cards/v1', 'source': str(self.source), 'rules': DEFAULT_RULES, 'items': {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            if loaded.get('schema') != self.data['schema'] or loaded.get('source') != str(self.source):
                raise ValueError('任务卡格式或原图目录不匹配')
            self.data = loaded
        seed = json.loads((Path(__file__).parent / 'review_seed.json').read_text())
        self.seeds = {}
        for sample in seed['samples']:
            for checksum in [sample['sha256'], sample.get('normalized_copy_sha256')]:
                if checksum:self.seeds[checksum]=(sample['id'], seed['records'].get(sample['id'], {}))

    def item(self, path):
        path = Path(path).resolve()
        key = path.relative_to(self.source).as_posix()
        checksum = digest(path)
        saved = self.data['items'].get(key)
        if saved and saved['sha256'] == checksum:
            value=deepcopy(saved)
            value.setdefault('category','待确认');value.setdefault('category_confirmed',False)
            return value
        sid, record = self.seeds.get(checksum, ('', {}))
        requirements = {'02': '去掉挂钩；保留主体。', '11': '允许去掉吊带，只保留包身。', '15': '去掉色卡；保留织物。', '16': '去掉色卡；保留主体。'}.get(sid, '')
        return {'category':'待确认', 'category_confirmed':False, 'sha256': checksum, 'features': record.get('features', []), 'features_confirmed': bool(record.get('featuresConfirmed', False)), 'requirements': requirements, 'model': '', 'historical_review': deepcopy(record), 'historical_id': sid}

    def save_item(self, path, value):
        path = Path(path).resolve()
        key = path.relative_to(self.source).as_posix()
        if value.get('sha256') != digest(path):
            raise ValueError('原图已改变，请重新打开任务卡')
        if value.get('model', '') not in MODELS.values():
            raise ValueError('请选择当前保留的模型')
        if value.get('category','待确认') not in CATEGORIES:
            raise ValueError('请选择有效的物品类别')
        if value.get('category_confirmed') and value.get('category','待确认')=='待确认':
            raise ValueError('请先选择类别，再确认')
        self.data['items'][key] = deepcopy(value)
        atomic_json(self.path, self.data)

    def save_classification(self, path, result):
        value = self.item(path)
        if result.get('sha256') != value['sha256']:
            raise ValueError('分类对应的原图已改变')
        value['classification'] = deepcopy(result)
        if not value.get('category_confirmed'):
            candidates=result.get('category_candidates',[])
            value['category']=candidates[0]['label'] if candidates and not result.get('category_uncertain',True) else '待确认'
        # A pretrained guess never replaces confirmed human labels or requirements.
        if not value['features_confirmed']:
            value['features'] = list(result['suggested_features'])
        self.save_item(path, value)
        return value

    def snapshot(self, paths):
        result = {}
        for path in paths:
            value = self.item(path)
            if value.get('model', '') not in MODELS.values():raise ValueError('任务卡含未启用的模型')
            result[Path(path).resolve().relative_to(self.source).as_posix()] = {
                'rules': self.data['rules'], 'category':value.get('category','待确认'),
                'category_confirmed':value.get('category_confirmed',False), 'features': value['features'],
                'features_confirmed': value['features_confirmed'], 'requirements': value['requirements'],
                'model': value['model'], 'sha256': value['sha256']}
        return result

    def export_drafts(self, paths, target):
        cards=[]
        for path in paths:
            item=self.item(path)
            cards.append({'source':Path(path).resolve().relative_to(self.source).as_posix(),
                          'sha256':item['sha256'], 'category':item.get('category','待确认'),
                          'category_confirmed':item.get('category_confirmed',False),
                          'features':item['features'], 'features_confirmed':item['features_confirmed'],
                          'batch_rules':self.data['rules'], 'image_requirements':item['requirements'],
                          'model':item['model'], 'classification':item.get('classification'),
                          'status':'待人工复核'})
        result={'schema':'whiteflow-task-card-export/v1','source':str(self.source),'cards':cards}
        atomic_json(Path(target),result)
        return result

class TaskCardWindow:
    def __init__(self, root, source, destination, recursive=True):
        import tkinter as tk
        from tkinter import ttk, messagebox
        from PIL import ImageTk
        from core import load_image
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.load_image, self.ImageTk = load_image, ImageTk
        self.store = TaskCards(source)
        self.paths = collect_images(Path(source).resolve(), Path(destination).resolve(), recursive)
        if not self.paths:raise ValueError('没有可分类的图片')
        self.window = tk.Toplevel(root); self.window.title('CutCut · 预训练模型分类与任务卡')
        self.window.geometry('1100x880');self.window.minsize(960,780)
        self.busy=False;self.stop_requested=False
        self.index = 0
        frame=ttk.Frame(self.window,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='批次处理要求（本机保存）').pack(anchor='w')
        self.rules=tk.Text(frame,height=3,wrap='word');self.rules.pack(fill='x',pady=6)
        self.rules.insert('1.0',self.store.data['rules'])
        body=ttk.Frame(frame);body.pack(fill='both',expand=True)
        left=ttk.Frame(body);left.pack(side='left',fill='both',expand=True,padx=(0,18))
        self.title=ttk.Label(left,wraplength=470);self.title.pack(anchor='w')
        self.preview=ttk.Label(left);self.preview.pack(expand=True)
        right=ttk.Frame(body);right.pack(side='right',fill='y')
        ttk.Label(right,text='物品类别').pack(anchor='w')
        self.category=tk.StringVar()
        ttk.Combobox(right,textvariable=self.category,values=CATEGORIES,state='readonly',width=34).pack(fill='x',pady=4)
        self.category_confirmed=tk.BooleanVar()
        ttk.Checkbutton(right,text='已核对物品类别',variable=self.category_confirmed).pack(anchor='w')
        ttk.Label(right,text='图片特征（AI草稿，确认后保留人工结论）').pack(anchor='w')
        self.features={name:tk.BooleanVar() for name in FEATURES}
        feature_grid=ttk.Frame(right);feature_grid.pack(anchor='w')
        for i,(name,var) in enumerate(self.features.items()):
            ttk.Checkbutton(feature_grid,text=name,variable=var).grid(row=i//2,column=i%2,sticky='w',padx=(0,8),pady=2)
        self.confirmed=tk.BooleanVar();ttk.Checkbutton(right,text='已检查本张特征',variable=self.confirmed).pack(anchor='w',pady=6)
        ttk.Label(right,text='本张处理要求 / 允许删除的部件').pack(anchor='w',pady=(10,0))
        self.requirements=tk.Text(right,height=3,width=39,wrap='word');self.requirements.pack(fill='x',pady=6)
        ttk.Label(right,text='本张模型（随批次默认使用快速模型）').pack(anchor='w')
        self.model=tk.StringVar();ttk.Combobox(right,textvariable=self.model,values=list(MODELS),state='readonly',width=34).pack(fill='x',pady=6)
        self.ai_info=ttk.Label(right,wraplength=340);self.ai_info.pack(anchor='w',pady=6)
        self.history=ttk.Label(left,wraplength=470);self.history.pack(anchor='w',pady=6)
        ttk.Label(right,text='分类使用预训练视觉模型，在本机CPU逐张运行。\n分类建议不代表处理要求，也不会自动通过审核。',wraplength=320).pack(anchor='w',pady=8)
        controls=ttk.Frame(frame);controls.pack(fill='x',pady=(14,0))
        ttk.Button(controls,text='← 上一张',command=lambda:self.move(-1)).pack(side='left')
        ttk.Button(controls,text='保存当前任务卡',command=self.save).pack(side='left',padx=10)
        ttk.Button(controls,text='下一张 →',command=lambda:self.move(1)).pack(side='left')
        ai_controls=ttk.Frame(frame);ai_controls.pack(fill='x',pady=4)
        ttk.Button(ai_controls,text='预训练分类本张（试验）',command=lambda:self.classify_images(False)).pack(side='left')
        ttk.Button(ai_controls,text='预训练分类本批（试验）',command=lambda:self.classify_images(True)).pack(side='left',padx=10)
        ttk.Button(ai_controls,text='停止分类（当前图完成后）',command=self.stop_classification).pack(side='left')
        ttk.Button(ai_controls,text='导出本批任务卡',command=self.export_cards).pack(side='left',padx=10)
        self.status=ttk.Label(frame,text='');self.status.pack(anchor='w',pady=6)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.show()

    def show(self):
        path=self.paths[self.index];self.current=self.store.item(path)
        self.title.configure(text=f'{self.index+1}/{len(self.paths)} · {path.name}')
        image=self.load_image(path);image.thumbnail((490,430));self.photo=self.ImageTk.PhotoImage(image)
        self.preview.configure(image=self.photo)
        for name,var in self.features.items():var.set(name in self.current['features'])
        self.confirmed.set(self.current['features_confirmed'])
        self.category.set(self.current.get('category','待确认'));self.category_confirmed.set(self.current.get('category_confirmed',False))
        self.requirements.delete('1.0','end');self.requirements.insert('1.0',self.current['requirements'])
        self.model.set(next(k for k,v in MODELS.items() if v==self.current['model']))
        self.show_classification()
        historical=self.current.get('historical_review',{});preferred=historical.get('preferred','')
        self.history.configure(text=('已匹配历史审核编号 '+self.current['historical_id']+'\n历史首选：'+(preferred or '无直接合格候选')+'\n历史结论仅供参考，本次生成结果仍须审核。') if historical else '此图暂无历史审核记录。')

    def save(self):
        if self.busy:
            self.status.configure(text='正在分类，请先停止并等待当前图片完成。');return False
        try:
            self.store.data['rules']=self.rules.get('1.0','end-1c').strip()
            value=deepcopy(self.current)
            # Preserve any older tags that are not in this version of the UI.
            value.update(category=self.category.get(),category_confirmed=self.category_confirmed.get(),features=[n for n in value['features'] if n not in self.features]+[n for n,v in self.features.items() if v.get()], features_confirmed=self.confirmed.get(),requirements=self.requirements.get('1.0','end-1c').strip(),model=MODELS[self.model.get()])
            self.store.save_item(self.paths[self.index],value);self.current=value
            self.status.configure(text='已保存到本机；尚未执行抠图或入库。')
            return True
        except Exception as exc:self.messagebox.showerror('保存未完成',str(exc),parent=self.window);return False

    def export_cards(self):
        if self.busy or not self.save():return
        from tkinter import filedialog
        target=filedialog.asksaveasfilename(parent=self.window,title='导出本批任务卡',initialfile='CutCut-任务卡.json',defaultextension='.json',filetypes=[('JSON','*.json')])
        if not target:return
        try:
            self.store.export_drafts(self.paths,target)
            self.status.configure(text='本批任务卡已导出；类别和特征确认状态一并保留。')
        except Exception as exc:self.messagebox.showerror('导出失败',str(exc),parent=self.window)

    def move(self,delta):
        if self.busy:return
        if not self.save():return
        self.index=max(0,min(len(self.paths)-1,self.index+delta))
        try:self.show()
        except Exception as exc:self.messagebox.showerror('图片无法打开',str(exc),parent=self.window)

    def show_classification(self):
        result=self.current.get('classification')
        if not result:
            self.ai_info.configure(text='尚未运行预训练分类。已确认的历史标签会保留。');return
        candidates=result['category_candidates']
        category=' / '.join(v['label'] for v in candidates[:2]) if result['category_uncertain'] else candidates[0]['label']
        uncertain=[k for k,v in result['feature_scores'].items() if v['suggested'] is None]
        self.ai_info.configure(text='AI类别建议：'+category+'\nAI特征建议：'+('、'.join(result['suggested_features']) or '暂无明显特征')+'\n待确认：'+('、'.join(uncertain) or '请核对以上特征')+'\nMobileCLIP-S0 · CPU · '+str(result['seconds'])+'秒\n相似度未经概率校准；人工确认前均为草稿。')

    def stop_classification(self):
        self.stop_requested=True
        if self.busy:self.status.configure(text='将在当前图片分类完成后停止。')

    def classify_images(self, batch):
        if self.busy:return
        if not self.save():return
        from pretrained_classifier import classify, ready
        if not ready():
            self.messagebox.showerror('分类不可用','预训练模型文件尚未准备完成。',parent=self.window);return
        import queue, threading
        self.busy=True;self.stop_requested=False;self.ai_events=queue.Queue()
        self.disabled=[]
        def freeze(widget):
            for child in widget.winfo_children():
                if isinstance(child,(self.ttk.Entry,self.ttk.Combobox,self.ttk.Checkbutton,self.tk.Text)):
                    state=child.cget('state');self.disabled.append((child,state));child.configure(state='disabled')
                freeze(child)
        freeze(self.window)
        paths=list(self.paths) if batch else [self.paths[self.index]]
        def work():
            try:
                for i,path in enumerate(paths):
                    if self.stop_requested:break
                    self.ai_events.put(('progress',(i+1,len(paths),path.name)))
                    self.ai_events.put(('result',(path,classify(path))))
            except Exception as exc:self.ai_events.put(('error',str(exc)))
            finally:self.ai_events.put(('done',None))
        threading.Thread(target=work,daemon=True).start()
        self.window.after(100,self.poll_classification)

    def poll_classification(self):
        import queue
        while True:
            try:event,value=self.ai_events.get_nowait()
            except queue.Empty:break
            if event=='progress':self.status.configure(text=f'预训练模型分类 {value[0]}/{value[1]} · {value[2]} · CPU逐张运行')
            elif event=='result':
                try:
                    path,result=value;self.store.save_classification(path,result)
                    if path==self.paths[self.index]:
                        self.current=self.store.item(path)
                        for name,var in self.features.items():var.set(name in self.current['features'])
                        self.category.set(self.current.get('category','待确认'))
                        self.category_confirmed.set(self.current.get('category_confirmed',False))
                        self.show_classification()
                except Exception as exc:self.stop_requested=True;self.messagebox.showerror('分类保存失败',str(exc),parent=self.window)
            elif event=='error':self.messagebox.showerror('分类未完成',value,parent=self.window)
            elif event=='done':
                self.busy=False
                for widget,state in self.disabled:widget.configure(state=state)
                self.status.configure(text='分类已停止。' if self.stop_requested else '分类草稿已保存；已确认人工标签和处理要求保持不变。')
                return
        self.window.after(100,self.poll_classification)

    def close(self):
        if self.busy:self.stop_classification();return
        if self.save():self.window.destroy()
