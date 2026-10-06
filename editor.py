"""Mask editor and mandatory human review. Tk imports occur only when the window opens."""
from __future__ import annotations
import math
import sys
import queue
from concurrent.futures import ThreadPoolExecutor
import tkinter as tk
from tkinter import messagebox, ttk
from PIL import Image, ImageChops, ImageDraw, ImageTk
from workflow import BatchStore, STATUSES, safe_path
from core import load_image, MODEL_LABELS


def viewport_image(image,size,zoom,center):
    width,height=size
    canvas=Image.new('RGB',size,'#edf0f3')
    left=center[0]-width/(2*zoom);top=center[1]-height/(2*zoom)
    box=(max(0,math.floor(left)),max(0,math.floor(top)),min(image.width,math.ceil(left+width/zoom)),min(image.height,math.ceil(top+height/zoom)))
    if box[2]<=box[0] or box[3]<=box[1]:return canvas
    part=image.crop(box).convert('RGBA');target=(max(1,round(part.width*zoom)),max(1,round(part.height*zoom)))
    if part.size!=target:part=part.resize(target,Image.Resampling.LANCZOS)
    canvas.paste(part,(round((box[0]-left)*zoom),round((box[1]-top)*zoom)),part)
    return canvas


def scroll_distance(delta,platform):
    units=float(delta)*2 if platform=='darwin' else float(delta)/120*35
    return max(-48,min(48,units))


def wheel_zoom_factor(units):
    # Coalesced 50 ms input is limited to 3% per frame, including large deltas.
    return math.exp(max(-math.log(1.03),min(math.log(1.03),float(units)*.006)))


def text_input(widget):
    return widget.winfo_class() in {'Entry','TEntry','Spinbox','TSpinbox','TCombobox','Text','Listbox'}


class MaskEditor:
    """Undo stores changed regions rather than copies of every full-resolution photo."""
    def __init__(self,mask,max_bytes=64*1024*1024):
        self.mask=mask.convert('L').copy();self.undo=[];self.redo=[];self.max_bytes=max_bytes

    def apply(self,shape,points,restore=True,radius=10):
        if not points:return False
        if shape not in {'brush','rectangle','polygon','lasso'}:raise ValueError('选区方式无效')
        radius=max(1,min(1000,int(radius)))
        pad=radius+2 if shape=='brush' else 2
        xs,ys=zip(*points)
        box=(max(0,math.floor(min(xs)-pad)),max(0,math.floor(min(ys)-pad)),
             min(self.mask.width,math.ceil(max(xs)+pad+1)),min(self.mask.height,math.ceil(max(ys)+pad+1)))
        if box[2]<=box[0] or box[3]<=box[1]:return False
        before=self.mask.crop(box);after=before.copy();draw=ImageDraw.Draw(after)
        local=[(round(x-box[0]),round(y-box[1])) for x,y in points];value=255 if restore else 0
        if shape=='brush':
            if len(local)>1:draw.line(local,fill=value,width=2*radius,joint='curve')
            for x,y in local:draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=value)
        elif shape=='rectangle':
            if len(local)<2:return False
            x1,y1=local[0];x2,y2=local[-1];draw.rectangle((min(x1,x2),min(y1,y2),max(x1,x2),max(y1,y2)),fill=value)
        else:
            if len(local)<3:return False
            draw.polygon(local,fill=value)
        if not ImageChops.difference(before,after).getbbox():return False
        self.mask.paste(after,box)
        self.undo.append((box,before,after));self.redo.clear()
        while len(self.undo)>1 and sum(p[1].width*p[1].height*2 for p in self.undo)>self.max_bytes:self.undo.pop(0)
        return True

    def undo_one(self):
        if not self.undo:return False
        item=self.undo.pop();self.mask.paste(item[1],item[0]);self.redo.append(item);return True

    def redo_one(self):
        if not self.redo:return False
        item=self.redo.pop();self.mask.paste(item[2],item[0]);self.undo.append(item);return True


class ReviewWindow:
    def __init__(self,parent,destination):
        self.store=BatchStore(destination)
        self.window=tk.Toplevel(parent);self.window.title('全部图片人工审核与修整')
        self.window.geometry('1280x900');self.window.minsize(1000,740)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.jobs=ThreadPoolExecutor(max_workers=1);self.future=None;self.queue=queue.Queue();self.generation=0
        self.row=None;self.images={};self.photos=[];self.engine=None;self.busy=False;self.dirty=False
        self.zoom=1;self.center=(0,0);self.points=[];self.render_job=None;self.wheel_job=None;self.wheel_amount=0;self.wheel_anchor=None;self.space_pan=False;self.dragging_pan=False
        self.filter=tk.StringVar(value='待审核/编辑中/失败');self.tool=tk.StringVar(value='移动')
        self.shape=tk.StringVar(value='笔刷');self.radius=tk.StringVar(value='10');self.note=tk.StringVar()
        self.detail=tk.StringVar(value='生成候选图后，所有图片都需要人工审核。')
        self.state=tk.StringVar();self.position=tk.StringVar()
        outer=ttk.Frame(self.window,padding=12);outer.pack(fill='both',expand=True)
        navigation=ttk.Frame(outer);navigation.pack(fill='x')
        combo=ttk.Combobox(navigation,textvariable=self.filter,values=['待审核/编辑中/失败','全部','待重制','已通过'],state='readonly',width=20)
        combo.pack(side='left');combo.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        for label,command in [('刷新',self.refresh),('上一张',lambda:self.navigate(-1)),('下一张',lambda:self.navigate(1)),('适应窗口',self.fit),('100%',lambda:self.set_zoom(1)),('－',lambda:self.set_zoom(self.zoom/1.1)),('＋',lambda:self.set_zoom(self.zoom*1.1))]:
            ttk.Button(navigation,text=label,command=command).pack(side='left',padx=3)
        ttk.Label(navigation,textvariable=self.position).pack(side='right')
        editbar=ttk.Frame(outer);editbar.pack(fill='x',pady=8)
        for label in ['移动','恢复主体','去除背景']:
            ttk.Radiobutton(editbar,text=label,variable=self.tool,value=label,command=self.tool_changed).pack(side='left',padx=4)
        shape=ttk.Combobox(editbar,textvariable=self.shape,values=['笔刷','矩形','多边形','套索'],state='readonly',width=8)
        shape.pack(side='left',padx=5);shape.bind('<<ComboboxSelected>>',lambda e:self.cancel_selection())
        ttk.Label(editbar,text='笔刷半径（原图像素）').pack(side='left')
        ttk.Spinbox(editbar,textvariable=self.radius,from_=1,to=1000,width=5).pack(side='left',padx=4)
        self.edit_buttons=[]
        for label,command in [('完成多边形',self.finish_polygon),('撤销',lambda:self.history(False)),('重做',lambda:self.history(True)),('重置蒙版',self.reset),('保存进度',self.save)]:
            button=ttk.Button(editbar,text=label,command=command);button.pack(side='left',padx=3);self.edit_buttons.append(button)
        body=ttk.Frame(outer);body.pack(fill='both',expand=True)
        sidebar=ttk.Frame(body);sidebar.pack(side='left',fill='y',padx=(0,8))
        self.listbox=tk.Listbox(sidebar,width=29,exportselection=False);self.listbox.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(sidebar,command=self.listbox.yview);scroll.pack(side='right',fill='y');self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind('<<ListboxSelect>>',self.select)
        panels=ttk.Frame(body);panels.pack(side='left',fill='both',expand=True)
        panels.columnconfigure(0,weight=1,uniform='image');panels.columnconfigure(1,weight=1,uniform='image');panels.rowconfigure(1,weight=1)
        self.canvases=[];self.titles=[]
        for index,title in enumerate(['原图','候选白底图']):
            text=ttk.Label(panels,text=title);text.grid(row=0,column=index,pady=5);self.titles.append(text)
            canvas=tk.Canvas(panels,background='#edf0f3',highlightthickness=0)
            canvas.grid(row=1,column=index,sticky='nsew',padx=3)
            canvas.bind('<Configure>',self.schedule_render)
            canvas.bind('<ButtonPress-1>',self.press);canvas.bind('<B1-Motion>',self.motion);canvas.bind('<ButtonRelease-1>',self.release)
            canvas.bind('<Button-3>',lambda e:self.cancel_selection())
            canvas.bind('<MouseWheel>',self.scroll)
            canvas.bind('<Shift-MouseWheel>',lambda e:self.scroll(e,horizontal=True))
            canvas.bind('<Command-MouseWheel>',self.wheel_zoom)
            canvas.bind('<Control-MouseWheel>',self.wheel_zoom)
            canvas.bind('<Button-4>',lambda e:self.pan(0,-35))
            canvas.bind('<Button-5>',lambda e:self.pan(0,35))
            self.canvases.append(canvas)
        ttk.Label(outer,textvariable=self.state).pack(anchor='w',pady=(8,3))
        ttk.Label(outer,textvariable=self.detail,wraplength=950).pack(anchor='w')
        ttk.Label(outer,text='套索：按住拖出轮廓，松手应用。双指捏合缩放；双指滚动平移；⌘+滚动缓慢缩放；方向键平移；空格+拖动临时移动。右键取消选区。',foreground='#687385').pack(anchor='w',pady=5)
        remarks=ttk.Frame(outer);remarks.pack(fill='x',pady=5)
        ttk.Label(remarks,text='人工原因/备注（选填）').pack(side='left');ttk.Entry(remarks,textvariable=self.note).pack(side='left',fill='x',expand=True,padx=8)
        decisions=ttk.Frame(outer);decisions.pack(fill='x')
        self.approve=ttk.Button(decisions,text='确认无问题 → 导出到白底图',command=lambda:self.decide('approved'));self.approve.pack(side='left',padx=4)
        self.redo=ttk.Button(decisions,text='有问题 → 导出到待重制',command=lambda:self.decide('redo'));self.redo.pack(side='left',padx=4)
        self.retry=ttk.Button(decisions,text='重新抠这一张',command=self.retry_one);self.retry.pack(side='left',padx=4)
        self.preview_button=ttk.Button(decisions,text='导出布局预览',command=self.preview_export);self.preview_button.pack(side='left',padx=4)
        retrybar=ttk.Frame(outer);retrybar.pack(fill='x',pady=5)
        self.retry_model=tk.StringVar(value=next(iter(MODEL_LABELS)))
        ttk.Label(retrybar,text='本图重试模型').pack(side='left')
        self.retry_box=ttk.Combobox(retrybar,textvariable=self.retry_model,values=list(MODEL_LABELS),state='readonly',width=27)
        self.retry_box.pack(side='left',padx=8)
        ttk.Label(retrybar,text='BiRefNet 首次需要下载；换模型重试会清空本图编辑。',foreground='#687385').pack(side='left')
        ttk.Label(outer,text='关闭或切换图片前自动保存编辑。确认后再修改，会撤销旧确认和工具生成的旧导出；手工改过的导出不会覆盖。',foreground='#687385',wraplength=950).pack(anchor='w',pady=5)
        self.window.bind('<Escape>',lambda e:self.cancel_selection())
        for key,dx,dy in [('Left',-40,0),('Right',40,0),('Up',0,-40),('Down',0,40)]:
            self.window.bind('<'+key+'>',lambda e,x=dx,y=dy:self.key_pan(e,x,y))
            self.window.bind('<Shift-'+key+'>',lambda e,x=dx,y=dy:self.key_pan(e,x*4,y*4))
        self.window.bind('<KeyPress-space>',lambda e:self.space_key(e,True))
        self.window.bind('<KeyRelease-space>',lambda e:self.space_key(e,False))
        self.window.bind('<FocusOut>',lambda e:setattr(self,'space_pan',False))
        from gestures import PinchBridge
        self.pinch=PinchBridge(self.window,self.canvases,self.pinch_zoom)
        ttk.Label(outer,text=self.pinch.status,foreground='#687385').pack(anchor='w')
        self.refresh();self.window.after(100,self.poll)

    def refresh(self):
        if self.busy:return
        if not self.discard_selection():return
        if not self.save():return
        rows=self.store.entries();mode=self.filter.get()
        self.visible=[r for r in rows if mode=='全部' or (mode=='待审核/编辑中/失败' and r['status'] in {'pending','editing','failed','processing'}) or (mode=='已通过' and r['status']=='approved') or (mode=='待重制' and r['status']=='redo')]
        self.listbox.delete(0,'end')
        for r in self.visible:self.listbox.insert('end',STATUSES[r['status']]+' · '+r['source'])
        if self.visible:self.listbox.selection_set(0);self.select()
        else:
            self.row=None;self.engine=None;self.images={};self.generation+=1;self.detail.set('当前筛选下没有图片；可刷新或切换查看范围。');self.state.set('');self.controls();self.schedule_render()

    def navigate(self,step):
        if self.busy or not self.visible:return
        selected=self.listbox.curselection();index=min(len(self.visible)-1,max(0,(selected[0] if selected else 0)+step))
        self.listbox.selection_clear(0,'end');self.listbox.selection_set(index);self.listbox.see(index);self.select()

    def submit(self,kind,work):
        self.busy=True;self.controls();generation=self.generation
        def task():
            try:self.queue.put((kind,generation,work(),None))
            except Exception as exc:self.queue.put((kind,generation,None,str(exc)))
        self.future=self.jobs.submit(task)

    def select(self,event=None):
        selected=self.listbox.curselection()
        if not selected:return
        if self.busy:
            if self.row:
                old=next((i for i,r in enumerate(self.visible) if r['source']==self.row['source']),None)
                self.listbox.selection_clear(0,'end')
                if old is not None:self.listbox.selection_set(old)
            return
        if not self.discard_selection():return
        if not self.save():return
        row=self.store.get(self.visible[selected[0]]['source'])
        self.row=row;self.generation+=1;self.images={};self.engine=None;self.dirty=False;self.points=[]
        self.note.set(row.get('human',''))
        self.retry_model.set(next((label for label,model in MODEL_LABELS.items() if model==row['options']['model']),next(iter(MODEL_LABELS))))
        self.state.set('正在载入…');self.detail.set(self.routing_text(row)+'自动提示：'+(row.get('automatic') or '未触发规则，仍需人工确认。')+('；错误：'+row['error'] if row.get('error') else ''))
        self.schedule_render()
        def load():
            if row.get('mask'):
                original,white=self.store.preview(row);mask=self.store.load_mask(row)
                return original,white,mask
            try:original=load_image(safe_path(self.store.source,row['source']))
            except Exception:original=None
            return original,None,None
        self.submit('load',load)

    @staticmethod
    def routing_text(row):
        route=row.get("routing")
        if not route:return ""
        return "自动选模："+route["selected"]+" · "+route["reason"]+"\n"

    def poll(self):
        try:
            while True:
                kind,generation,value,error=self.queue.get_nowait()
                if generation!=self.generation:continue
                self.busy=False
                if error:
                    self.state.set('操作未完成，编辑成果保留。');messagebox.showerror('操作未完成',error,parent=self.window)
                elif kind=='load':
                    original,white,mask=value
                    self.images={k:v for k,v in [('original',original),('white',white)] if v is not None}
                    self.engine=MaskEditor(mask) if mask else None
                    self.state.set(STATUSES[self.row['status']]+' · '+self.row['source'])
                    for label,key in zip(self.titles,['original','white']):
                        im=self.images.get(key);label.configure(text=('原图' if key=='original' else '候选白底图')+(f' · {im.width}×{im.height}' if im else ' · 无图片'))
                    self.fit()
                elif kind=='preview':
                    ExportPreview(self.window,value)
                elif kind in {'decide','retry'}:
                    self.row=value;self.state.set(STATUSES[value['status']]+' · '+value['source']);self.refresh()
                self.controls()
        except queue.Empty:pass
        if self.window.winfo_exists():self.window.after(100,self.poll)

    def controls(self):
        enabled=self.engine is not None and not self.busy
        for button in self.edit_buttons:button.configure(state='normal' if enabled else 'disabled')
        self.approve.configure(state='normal' if enabled else 'disabled')
        self.preview_button.configure(state='normal' if enabled else 'disabled')
        self.redo.configure(state='normal' if self.row and not self.busy and self.row['status']!='processing' else 'disabled')
        self.retry.configure(state='normal' if self.row and not self.busy else 'disabled')
        self.retry_box.configure(state='readonly' if not self.busy else 'disabled')

    def tool_changed(self):
        self.cancel_selection()

    def coordinate(self,event):
        return (self.center[0]+(event.x-event.widget.winfo_width()/2)/self.zoom,
                self.center[1]+(event.y-event.widget.winfo_height()/2)/self.zoom)

    def press(self,event):
        if self.busy:return
        event.widget.focus_set()
        self.dragging_pan=self.tool.get()=='移动' or self.space_pan
        if self.dragging_pan:self.drag_origin=(event.x,event.y,self.center);return
        if not self.engine:return
        point=self.coordinate(event)
        if self.shape.get()=='多边形':self.points.append(point);self.schedule_render()
        else:self.points=[point]

    def motion(self,event):
        if self.busy:return
        if self.dragging_pan:
            if hasattr(self,'drag_origin'):
                x,y,center=self.drag_origin;self.center=(center[0]-(event.x-x)/self.zoom,center[1]-(event.y-y)/self.zoom);self.schedule_render()
        elif self.points and self.shape.get()!='多边形':
            point=self.coordinate(event)
            if self.shape.get()=='矩形':self.points=self.points[:1]+[point]
            else:self.points.append(point)
            self.schedule_render()

    def release(self,event):
        if self.dragging_pan:self.dragging_pan=False;return
        if self.busy or self.shape.get()=='多边形' or not self.points:return
        self.points.append(self.coordinate(event));self.apply_selection()

    def finish_polygon(self):
        if self.shape.get()=='多边形' and len(self.points)>=3:self.apply_selection()

    def ensure_editing(self):
        try:
            if self.row['status']!='editing':self.row=self.store.begin_edit(self.row['source'],self.row['revision'])
            return True
        except Exception as exc:messagebox.showerror('不能编辑',str(exc),parent=self.window);return False

    def apply_selection(self):
        if not self.engine or self.busy:return
        try:radius=int(self.radius.get())
        except ValueError:messagebox.showinfo('笔刷半径','请输入 1–1000 的整数',parent=self.window);return
        if not 1<=radius<=1000:return
        changed=self.engine.apply({'笔刷':'brush','矩形':'rectangle','多边形':'polygon','套索':'lasso'}[self.shape.get()],self.points,self.tool.get()=='恢复主体',radius)
        self.points=[]
        if changed:
            if not self.ensure_editing():
                self.engine.undo_one();self.engine.redo.clear();self.schedule_render();return
            self.dirty=True;self.update_white();self.save()
        self.schedule_render()

    def update_white(self):
        original=self.images['original'];cutout=original.copy()
        cutout.putalpha(ImageChops.multiply(self.engine.mask,original.getchannel('A')))
        white=Image.new('RGBA',original.size,'white');white.alpha_composite(cutout)
        self.images['white']=white.convert('RGB');self.state.set('编辑中 · 需要重新确认 · '+self.row['source'])

    def history(self,redo):
        if not self.engine or self.busy:return
        if not (self.engine.redo if redo else self.engine.undo):return
        if not self.ensure_editing():return
        changed=self.engine.redo_one() if redo else self.engine.undo_one()
        if changed:self.dirty=True;self.update_white();self.save();self.schedule_render()

    def reset(self):
        if not self.engine or self.busy:return
        if not messagebox.askyesno('重置蒙版','恢复本次模型生成的初始蒙版？这会清空当前撤销记录，仍需重新确认。',parent=self.window):return
        if not self.ensure_editing():return
        try:self.engine=MaskEditor(self.store.load_mask(self.row,base=True));self.dirty=True;self.update_white();self.save();self.schedule_render()
        except Exception as exc:messagebox.showerror('重置失败',str(exc),parent=self.window)

    def save(self):
        if not self.row or not self.engine or self.busy:return not self.busy
        if not self.dirty and self.note.get()==self.row.get('human',''):return True
        try:
            if not self.ensure_editing():return False
            self.row=self.store.save_edit(self.row['source'],self.row['revision'],self.engine.mask,self.note.get())
            self.dirty=False;return True
        except Exception as exc:messagebox.showerror('保存失败','请不要关闭窗口；'+str(exc),parent=self.window);return False

    def preview_export(self):
        if self.busy or not self.row or not self.engine or not self.save():return
        row=dict(self.row)
        def work():
            from core import Options,render_white
            original=load_image(self.store._validate_source(row));mask=self.store.load_mask(row)
            original.putalpha(ImageChops.multiply(mask,original.getchannel('A')))
            return render_white(original,Options(**row['options']))[0]
        self.submit('preview',work)

    def decide(self,decision):
        if self.busy or not self.row or not self.save():return
        if self.points:messagebox.showinfo('选区尚未完成','请先完成或取消当前选区，再确认。',parent=self.window);return
        row=dict(self.row);human=self.note.get();self.state.set('正在验证并导出…')
        self.submit('decide',lambda:self.store.decide(row['source'],row['revision'],decision,human))

    def retry_one(self):
        if self.busy or not self.row or not self.save():return
        if not messagebox.askyesno('重新抠图',f'使用 {self.retry_model.get()} 重新生成本图；会清空编辑、确认状态并移除工具生成的旧导出。',parent=self.window):return
        row=dict(self.row)
        row['options']=dict(row['options']);row['options']['model']=MODEL_LABELS[self.retry_model.get()]
        if row['options']['model']=='framed-dual':row['options']['alpha_matting']=False
        def work():
            import fcntl
            from core import Options,digest,make_session,remove_background,inspect_cutout,render_white
            lock=(self.store.cache/'batch.lock').open('a+')
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close();raise ValueError('批量候选生成仍在运行，请停止或等待完成后再重试单张')
            try:
                return self._retry_work(row)
            finally:
                fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
        self.submit('retry',work)

    def _retry_work(self,row):
            from core import Options,digest,make_session,remove_background,inspect_cutout,render_white
            path=safe_path(self.store.source,row['source']);source_hash=digest(path)
            current=self.store.prepare(row['source'],source_hash,Options(**row['options']),row['revision'],row.get('override_base') or row['config'])
            try:
                original=load_image(path);options=Options(**row['options']);session=make_session(options.model)
                cutout=remove_background(original,session,options);mask=ImageChops.multiply(cutout.getchannel('A'),original.getchannel('A'))
                cutout=original.copy();cutout.putalpha(mask);white,_=render_white(cutout,options)
                if digest(path)!=source_hash:raise ValueError('原图在处理期间改变')
                return self.store.finish(current,mask,'；'.join(inspect_cutout(original,cutout,white,options)),cutout.info.get('routing'))
            except Exception as exc:return self.store.fail(current,str(exc))

    def discard_selection(self):
        if self.points:
            if not messagebox.askyesno('取消未完成选区','当前选区尚未应用，切换将取消选区。已完成编辑会保存。',parent=self.window):
                if self.row:
                    old=next((i for i,r in enumerate(self.visible) if r['source']==self.row['source']),None)
                    if old is not None:self.listbox.selection_clear(0,'end');self.listbox.selection_set(old)
                return False
            self.points=[]
        return True

    def cancel_selection(self):
        self.points=[];self.schedule_render()

    def fit(self):
        if not self.images:return
        width=max(im.width for im in self.images.values());height=max(im.height for im in self.images.values())
        self.center=(width/2,height/2);self.set_zoom(min(min(max(1,c.winfo_width())/width,max(1,c.winfo_height())/height) for c in self.canvases))

    def pinch_zoom(self,factor,canvas,x,y):
        self.set_zoom(self.zoom*factor,(x,y,canvas.winfo_width(),canvas.winfo_height()))

    def set_zoom(self,zoom,anchor=None):
        new=max(.005,min(8,zoom))
        if anchor is not None:
            sx,sy,width,height=anchor
            offset=((sx-width/2),(sy-height/2))
            self.center=(self.center[0]+offset[0]/self.zoom-offset[0]/new,
                         self.center[1]+offset[1]/self.zoom-offset[1]/new)
        self.zoom=new;self.schedule_render()

    def pan(self,dx,dy):
        self.center=(self.center[0]+dx/self.zoom,self.center[1]+dy/self.zoom);self.schedule_render()
        return 'break'

    def scroll(self,event,horizontal=False):
        distance=scroll_distance(event.delta,sys.platform)
        return self.pan(-distance if horizontal else 0,0 if horizontal else -distance)

    def wheel_zoom(self,event):
        units=event.delta if sys.platform=='darwin' else event.delta/120
        self.wheel_amount+=units
        self.wheel_anchor=(event.x,event.y,event.widget.winfo_width(),event.widget.winfo_height())
        if self.wheel_job is None:self.wheel_job=self.window.after(50,self.flush_wheel)
        return 'break'

    def flush_wheel(self):
        self.wheel_job=None;amount=self.wheel_amount;self.wheel_amount=0
        self.set_zoom(self.zoom*wheel_zoom_factor(amount),self.wheel_anchor)

    def key_pan(self,event,dx,dy):
        if text_input(event.widget):return None
        return self.pan(dx,dy)

    def space_key(self,event,pressed):
        if text_input(event.widget):return None
        self.space_pan=pressed;return 'break'

    def schedule_render(self,event=None):
        if self.render_job is not None:self.window.after_cancel(self.render_job)
        self.render_job=self.window.after(40,self.render)

    def render(self):
        self.render_job=None;self.photos=[]
        for canvas,key in zip(self.canvases,['original','white']):
            canvas.delete('all');im=self.images.get(key)
            if im is None:canvas.create_text(canvas.winfo_width()/2,canvas.winfo_height()/2,text='暂无图片',fill='#687385');continue
            frame=viewport_image(im,(max(1,canvas.winfo_width()),max(1,canvas.winfo_height())),self.zoom,self.center)
            photo=ImageTk.PhotoImage(frame,master=self.window);self.photos.append(photo);canvas.create_image(0,0,anchor='nw',image=photo)
            points=[(canvas.winfo_width()/2+(x-self.center[0])*self.zoom,canvas.winfo_height()/2+(y-self.center[1])*self.zoom) for x,y in self.points]
            if len(points)>1:
                if self.shape.get()=='矩形':canvas.create_rectangle(*points[0],*points[-1],outline='#e97324',width=2)
                else:
                    coords=[v for point in points for v in point]
                    if self.shape.get()=='套索' and len(points)>2:coords.extend(points[0])
                    canvas.create_line(*coords,fill='#e97324',width=2)
        self.position.set(f'{len(self.visible)} 张 · {self.zoom*100:.0f}%')

    def close(self):
        if self.busy:messagebox.showinfo('正在执行操作','请等当前载入、导出或重试完成后再关闭。',parent=self.window);return
        if self.points and not messagebox.askyesno('取消未完成选区','当前选区尚未应用，关闭将取消该选区。已完成的编辑会保存。',parent=self.window):return
        if not self.save():return
        self.jobs.shutdown(wait=False,cancel_futures=True);self.generation+=1
        if self.render_job is not None:self.window.after_cancel(self.render_job)
        if self.wheel_job is not None:self.window.after_cancel(self.wheel_job)
        self.window.destroy()


class ExportPreview:
    def __init__(self,parent,image):
        self.image=image;self.zoom=1;self.center=(image.width/2,image.height/2);self.photo=None
        self.window=tk.Toplevel(parent);self.window.title('导出布局预览（保存前）')
        self.window.geometry('900x750')
        bar=ttk.Frame(self.window,padding=8);bar.pack(fill='x')
        ttk.Label(bar,text=f'最终画布 {image.width}×{image.height}；JPG 保存会产生少量压缩差异').pack(side='left')
        ttk.Button(bar,text='适应窗口',command=self.fit).pack(side='right')
        ttk.Button(bar,text='100%',command=lambda:self.scale(1)).pack(side='right')
        self.canvas=tk.Canvas(self.window,background='#edf0f3',highlightthickness=0);self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Configure>',lambda e:self.render())
        self.canvas.bind('<MouseWheel>',lambda e:self.preview_pan(0,-scroll_distance(e.delta,sys.platform)))
        self.canvas.bind('<Shift-MouseWheel>',lambda e:self.preview_pan(-scroll_distance(e.delta,sys.platform),0))
        self.canvas.bind('<Command-MouseWheel>',lambda e:self.scale(self.zoom*wheel_zoom_factor(e.delta)))
        self.canvas.bind('<Control-MouseWheel>',lambda e:self.scale(self.zoom*wheel_zoom_factor(e.delta/120)))
        for key,dx,dy in [('Left',-40,0),('Right',40,0),('Up',0,-40),('Down',0,40)]:
            self.window.bind('<'+key+'>',lambda e,x=dx,y=dy:self.preview_pan(x,y))
        self.canvas.bind('<ButtonPress-1>',self.press);self.canvas.bind('<B1-Motion>',self.drag)
        from gestures import PinchBridge
        self.pinch=PinchBridge(self.window,[self.canvas],self.pinch_zoom)
        self.window.after(100,self.fit)
    def pinch_zoom(self,factor,canvas,x,y):
        new=max(.005,min(8,self.zoom*factor))
        dx=x-canvas.winfo_width()/2;dy=y-canvas.winfo_height()/2
        self.center=(self.center[0]+dx/self.zoom-dx/new,self.center[1]+dy/self.zoom-dy/new)
        self.scale(new)

    def fit(self):
        self.center=(self.image.width/2,self.image.height/2)
        self.scale(min(max(1,self.canvas.winfo_width())/self.image.width,max(1,self.canvas.winfo_height())/self.image.height))
    def scale(self,zoom):self.zoom=max(.005,min(8,zoom));self.render()
    def preview_pan(self,dx,dy):
        self.center=(self.center[0]+dx/self.zoom,self.center[1]+dy/self.zoom);self.render();return 'break'
    def press(self,e):self.canvas.focus_set();self.origin=(e.x,e.y,self.center)
    def drag(self,e):
        if not hasattr(self,'origin'):return
        x,y,center=self.origin;self.center=(center[0]-(e.x-x)/self.zoom,center[1]-(e.y-y)/self.zoom);self.render()
    def render(self):
        frame=viewport_image(self.image,(max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())),self.zoom,self.center)
        self.photo=ImageTk.PhotoImage(frame,master=self.window);self.canvas.delete('all');self.canvas.create_image(0,0,anchor='nw',image=self.photo)
