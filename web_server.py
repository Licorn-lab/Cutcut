"""Loopback-only web UI. Inference lives in one disposable CPU subprocess."""
import io
import math
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[name] = '2'
os.environ.setdefault('WHITEBG_DATA_HOME', str(ROOT/'data/runtime'))
from core import Options, atomic_json, collect_images, digest, load_image, MODEL_LABELS
from task_cards import context_hash
from workflow import BatchStore, safe_path, rework_note

ALLOWED = set(MODEL_LABELS.values())
SETTINGS = ROOT/'data/web-settings.json'


def retry(store, key, revision, model, context=None):
    import fcntl
    from PIL import ImageChops
    from core import make_session, remove_background, render_white, inspect_cutout
    if model not in ALLOWED:raise ValueError('模型未启用')
    with (store.cache/'batch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = store.get(key)
        if not old or old['revision'] != revision:raise ValueError('候选已更新，请刷新后重试')
        options = Options(**{**old['options'], 'model':model, **({'task_context_hash':context_hash(context)} if context is not None else {})})
        path = safe_path(store.source, key)
        checksum = digest(path)
        if checksum != old['source_hash']:raise ValueError('原图已改变，请重新生成批次')
        current = store.prepare(key, checksum, options, revision, old.get('override_base') or old['config'])
        try:
            original = load_image(path)
            session = make_session(model)
            cutout = remove_background(original, session, options)
            if cutout.size != original.size:raise ValueError('模型返回尺寸不匹配')
            mask = ImageChops.multiply(cutout.getchannel('A'), original.getchannel('A'))
            info = cutout.info.copy();cutout = original.copy();cutout.putalpha(mask);cutout.info.update(info)
            white, _ = render_white(cutout, options)
            if digest(path) != checksum:raise ValueError('原图在处理期间改变')
            return store.finish(current, mask, '；'.join(inspect_cutout(original, cutout, white, options)), info.get('routing'))
        except Exception as exc:
            store.fail(current, str(exc));raise


def worker(request):
    from workflow import run_batch
    cancel = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_:cancel.set())
    def emit(event, value):
        if event != 'row':print(json.dumps({'event':event, 'value':value}, ensure_ascii=False), flush=True)
    source, destination = str(Path(request['source']).resolve()), str(Path(request['destination']).resolve())
    if request['action'] == 'batch':
        options = Options(**request['options'])
        run_batch(source, destination, options, emit=emit, cancel=cancel,
                  restart=request.get('mode') in {'restart_all','restart_unapproved'}, continue_existing=True,
                  restart_scope='unapproved' if request.get('mode')=='restart_unapproved' else 'all')
    elif request['action'] == 'retry':
        row = retry(BatchStore(destination, source), request['key'], request['revision'], request['model'])
        emit('done', {'status':row['status']})
    else:raise ValueError('任务类型无效')


class Workspace:
    def __init__(self):
        self.edit_session = None
        self.lock = threading.RLock()
        self.process = None;self.external = None
        self.job = {'busy':False, 'message':'准备就绪', 'progress':0, 'total':0, 'error':''}
        self.source = None;self.destination = None;self.paths = {};self.store = None
        self.settings = {}
        if SETTINGS.exists():
            try:self.settings = json.loads(SETTINGS.read_text())
            except (ValueError, OSError):pass
        if self.settings.get('source') and self.settings.get('destination'):
            try:self.open(self.settings)
            except (ValueError, OSError):pass

    def idle(self):
        if self.job['busy']:raise ValueError('任务正在运行，请停止并等待完成')
        if self.external and self.external.poll() is None:raise ValueError('桌面修整窗口仍在运行，请先关闭')

    def open(self, data):
        self.idle()
        source = Path(data['source']).expanduser().resolve()
        destination = Path(data['destination']).expanduser().resolve()
        if not source.is_dir():raise ValueError('请选择有效的原图文件夹')
        if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
            raise ValueError('原图和输出文件夹不能相同或互相包含')
        paths = collect_images(source, destination, True)
        if not paths:raise ValueError('原图目录没有支持的图片')
        store = BatchStore(destination, source)
        self.edit_session = None
        self.source, self.destination, self.store = source, destination, store
        self.paths = {secrets.token_hex(12):p for p in paths}
        self.settings = {'source':str(source), 'destination':str(destination)}
        atomic_json(SETTINGS, self.settings)

    def path(self, ident):
        if ident not in self.paths:raise ValueError('图片不存在，请刷新批次')
        path = self.paths[ident]
        safe_path(self.source, path.relative_to(self.source).as_posix())
        return path

    def start(self, request):
        self.idle()
        if not self.store:raise ValueError('请先打开批次')
        self.edit_session=None
        self.active_request=dict(request)
        request.update(source=str(self.source), destination=str(self.destination))
        self.job = {'busy':True,'message':'准备任务…','progress':0,'total':len(self.paths),'error':''}
        self.process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.process.stdin.write(json.dumps(request));self.process.stdin.close()
        threading.Thread(target=self.monitor, args=(self.process,), daemon=True).start()

    def monitor(self, process):
        tail = []
        for line in process.stdout:
            with self.lock:
                try:
                    event = json.loads(line)
                    if event['event'] == 'status':self.job['message'] = str(event['value'])
                    elif event['event'] in ('total','progress'):self.job[event['event']] = event['value']
                    elif event['event'] == 'done':self.job['message'] = '任务已结束，请复核结果';self.job['summary'] = event['value']
                except (ValueError, KeyError, TypeError):tail = (tail+[line.strip()])[-8:]
        code = process.wait()
        with self.lock:
            self.job['busy'] = False
            if code:self.job.update(error='\n'.join(tail)[-1800:] or '任务未完成', message='任务失败，可修正后重试')
            self.process = None

    def export_info(self, key, row):
        if not row or not row.get('export'):return None
        path=safe_path(self.destination,row['export'])
        if not path.is_file():return None
        return {'id':next((ident for ident,p in self.paths.items() if p.relative_to(self.source).as_posix()==key),None),
                'name':path.name,'path':str(path),'relative':row['export']}

    def review_blocked(self):
        if not self.job['busy']:return set()
        request=getattr(self,'active_request',{})
        plan=self.store.restart_state() if self.store else None
        blocked=set(plan['pending']) if plan else set()
        if request.get('action')=='retry':blocked.add(request['key'])
        # Before the restart worker writes its checkpoint, all old candidates are queued.
        if request.get('mode') in {'restart_all','restart_unapproved'} and not plan and not self.job.get('progress'):
            rows={row['source']:row for row in self.store.entries()}
            blocked.update(p.relative_to(self.source).as_posix() for p in self.paths.values()
                           if request['mode']=='restart_all' or rows.get(p.relative_to(self.source).as_posix(),{}).get('status')!='approved')
        return blocked

    def reviewable(self, key, row, blocked=None, allow_unmasked=False):
        if self.external and self.external.poll() is None:return False
        if not row or row['status']=='processing' or (not row.get('mask') and not allow_unmasked):return False
        return key not in (self.review_blocked() if blocked is None else blocked)

    def require_reviewable(self, key, allow_unmasked=False):
        row=self.store.get(key)
        if not self.reviewable(key,row,allow_unmasked=allow_unmasked):raise ValueError('本张尚未完成处理或仍在重做队列，请等待本张处理完成')

    def edit(self, key, data):
        from editor import MaskEditor
        row=self.store.get(key)
        revision=int(data['revision'])
        if not row or row['revision']!=revision:raise ValueError('候选已更新，请刷新后修整')
        if row['status']=='processing':raise ValueError('图片正在处理')
        self.store._validate_source(row)
        session=self.edit_session
        if not session or session['key']!=key or session['revision']!=revision:
            session={'key':key,'revision':revision,'engine':MaskEditor(self.store.load_mask(row),max_bytes=16*1024*1024)}
            self.edit_session=session
        engine=session['engine'];action=data.get('action','draw')
        try:
            if action=='undo':changed=engine.undo_one()
            elif action=='redo':changed=engine.redo_one()
            elif action=='reset':
                engine=MaskEditor(self.store.load_mask(row,base=True),max_bytes=16*1024*1024)
                session['engine']=engine;changed=True
            elif action=='draw':
                points=data.get('points');shape=data.get('shape');mode=data.get('mode')
                if shape not in {'brush','lasso','rectangle','polygon'} or mode not in {'restore','erase'}:raise ValueError('修整工具无效')
                if not isinstance(points,list) or not 1<=len(points)<=1800:raise ValueError('选区点数量无效')
                valid=[]
                for pt in points:
                    if not isinstance(pt,list) or len(pt)!=2:raise ValueError('选区坐标无效')
                    x,y=map(float,pt)
                    if not math.isfinite(x) or not math.isfinite(y) or not 0<=x<=engine.mask.width or not 0<=y<=engine.mask.height:raise ValueError('选区超出原图')
                    valid.append((x,y))
                radius=float(data.get('radius',10))
                if not math.isfinite(radius) or not 1<=radius<=1000:raise ValueError('笔刷半径无效')
                changed=engine.apply(shape,valid,mode=='restore',radius)
            else:raise ValueError('修整操作无效')
            if changed:
                row=self.store.save_edit(key,revision,engine.mask,'')
                session['revision']=row['revision']
            # A single large region can exceed the budget: retain the edit, release undo pixels.
            if sum(b.width*b.height*2 for _,b,_ in engine.undo+engine.redo)>engine.max_bytes:
                engine.undo.clear();engine.redo.clear()
            return {'row':row,'undo':bool(engine.undo),'redo':bool(engine.redo),'changed':changed}
        except Exception:
            self.edit_session=None
            raise

    def state(self, query):
        rows = {r['source']:r for r in self.store.entries()} if self.store else {}
        items = []
        counts = {}
        blocked=self.review_blocked()
        for ident, path in self.paths.items():
            key = path.relative_to(self.source).as_posix();row = rows.get(key, {})
            status = row.get('status','new');warning = bool(row.get('automatic'))
            counts[status] = counts.get(status,0)+1
            items.append({'id':ident,'name':key,'status':status,'warning':warning,'revision':row.get('revision',0),'reviewable':self.reviewable(key,row,blocked)})
        items.sort(key=lambda v:(0 if v['status']=='failed' else 1 if v['warning'] and v['status'] not in ('approved','redo') else 2, v['name']))
        mode = query.get('filter',['all'])[0]
        if mode == 'warning':items = [v for v in items if v['warning']]
        elif mode == 'pending':items = [v for v in items if v['status'] in ('pending','editing','new')]
        elif mode != 'all':items = [v for v in items if v['status']==mode]
        selected = query.get('selected',[''])[0]
        position = next((i for i,v in enumerate(items) if v['id']==selected),None)
        navigation = None
        if position is not None:
            # Capture the next review target before approval changes sorting/filter membership.
            remaining = items[position+1:] + items[:position]
            navigation = {
                'prev':items[position-1]['id'] if position else None,
                'next':items[position+1]['id'] if position+1<len(items) else None,
                'position':position+1,
                'next_review':next((v['id'] for v in remaining if v['reviewable'] and v['status'] not in ('approved','redo')),None)}
        offset = max(0,int(query.get('offset',['0'])[0]));limit = 40
        return {'settings':self.settings,'job':dict(self.job),'count':len(self.paths),'counts':counts,
                'selected':selected,'selected_reviewable':next((v['reviewable'] for v in items if v['id']==selected),None),'navigation':navigation,'filtered':len(items),'items':items[offset:offset+limit], 'restart':self.store.restart_state() if self.store else None}


from image_geometry import validate_geometry


def handler(workspace, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):pass
        def send(self, value, status=200, mime='application/json; charset=utf-8'):
            data = json.dumps(value, ensure_ascii=False).encode() if mime.startswith('application/json') else value
            self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(data)

        def authorized(self):
            host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != host:return False
            if self.headers.get('Origin') not in (None, 'http://'+host):return False
            cookies = self.headers.get('Cookie','').split(';')
            return any(secrets.compare_digest(c.strip(), 'whiteflow='+token) for c in cookies)

        def do_GET(self):
            parsed = urlparse(self.path);query = parse_qs(parsed.query)
            if parsed.path == '/' and secrets.compare_digest(query.get('token',[''])[0], token) and self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}':
                self.send_response(303);self.send_header('Set-Cookie',f'whiteflow={token}; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Location','/');self.send_header('Referrer-Policy','no-referrer');self.end_headers();return
            if not self.authorized():return self.send({'error':'本机会话无效，请用启动入口打开'},403)
            try:
                with workspace.lock:
                    if parsed.path in ('/','/app.js','/edit.js','/geometry.js','/style.css'):
                        name = {'/':'index.html','/app.js':'app.js','/edit.js':'edit.js','/geometry.js':'geometry.js','/style.css':'style.css'}[parsed.path]
                        mime = 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/html; charset=utf-8' if name.endswith('.html') else 'text/css; charset=utf-8'
                        return self.send((ROOT/'web'/name).read_bytes(), mime=mime)
                    if parsed.path == '/api/state':return self.send(workspace.state(query))
                    path = workspace.path(query.get('id',[''])[0]);key = path.relative_to(workspace.source).as_posix()
                    row = workspace.store.get(key)
                    if parsed.path == '/api/item':
                        if workspace.edit_session and (workspace.edit_session['key']!=key or not row or workspace.edit_session['revision']!=row['revision']):workspace.edit_session=None
                        size=list(workspace.store.load_mask(row).size) if row and row.get('mask') else list(load_image(path).size)
                        return self.send({'export':workspace.export_info(key,row),'name':key,'rework_note':rework_note(row),'row':row,'dimensions':size,'reviewable':workspace.reviewable(key,row),'crop_canvas_dimensions':workspace.store.crop_frame_size(row or {},size),'adjusted_dimensions':workspace.store.result_size(row or {},size)})
                    if parsed.path == '/api/crop-suggest':
                        workspace.require_reviewable(key)
                        if not row or row['revision']!=int(query.get('revision',['-1'])[0]):raise ValueError('候选已更新，请刷新后重试')
                        return self.send(workspace.store.suggest_crop(row))
                    if parsed.path == '/api/image':
                        geometry_base=query.get('geometry_base')==['1'] and query.get('kind')==['result']
                        size = 1000 if geometry_base else 240 if query.get('thumb') else 1800
                        if query.get('kind',['original'])[0] == 'result':
                            if not row or not row.get('mask'):raise ValueError('尚无候选图')
                            if row['revision'] != int(query.get('revision',['-1'])[0]):raise ValueError('候选已更新')
                            value=validate_geometry(json.loads(query['geometry'][0])) if query.get('geometry') else None
                            image=workspace.store.preview_result(row,query.get('crop')==['1'],value,size,query.get('frame')==['1'],geometry_base=geometry_base)
                        else:image = load_image(path)
                        if query.get('crop') == ['1'] and query.get('kind') != ['result']:
                            if not row or row['revision'] != int(query.get('revision',['-1'])[0]):raise ValueError('裁剪已更新，请刷新后重试')
                            workspace.store._validate_source(row)
                            if row.get('crop'):image = image.crop(tuple(row['crop']))
                        image.thumbnail((size,size));buffer = io.BytesIO()
                        transparent=geometry_base or (query.get('kind')==['result'] and row.get('background')=='transparent')
                        image.save(buffer,'PNG') if transparent else image.convert('RGB').save(buffer,'JPEG',quality=88)
                        return self.send(buffer.getvalue(),mime='image/png' if transparent else 'image/jpeg')
                    return self.send({'error':'路径不存在'},404)
            except (ValueError, OSError, KeyError) as exc:self.send({'error':str(exc)},400)

        def do_POST(self):
            if not self.authorized() or self.headers.get('Content-Type','').split(';')[0] != 'application/json':return self.send({'error':'本机会话无效'},403)
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 65536:raise ValueError('请求大小无效')
                data = json.loads(self.rfile.read(length));route = urlparse(self.path).path
                with workspace.lock:
                    if route == '/api/stop':
                        if workspace.process:workspace.process.terminate()
                        workspace.job['message'] = '正在停止，等待当前图片完成'
                        return self.send({'ok':True})
                    if route == '/api/reveal-export':
                        path=workspace.path(data['id']);key=path.relative_to(workspace.source).as_posix()
                        info=workspace.export_info(key,workspace.store.get(key))
                        if not info:raise ValueError('导出文件不存在，请重新确认导出')
                        if data.get('relative')!=info['relative']:raise ValueError('导出位置已改变，请查看最新导出结果')
                        # Resolve only a recorded export; client-supplied paths are never opened.
                        subprocess.run(['open','-R',info['path']],check=True,capture_output=True)
                        return self.send({'ok':True})
                    if route in {'/api/edit','/api/crop','/api/geometry','/api/background','/api/decide'}:
                        path=workspace.path(data['id']);workspace.require_reviewable(path.relative_to(workspace.source).as_posix(),allow_unmasked=route=='/api/decide' and data.get('decision')=='redo')
                    else:workspace.idle()
                    if route == '/api/pick':
                        result = subprocess.run(['osascript','-e','POSIX path of (choose folder with prompt "选择图片或输出文件夹")'],capture_output=True,text=True)
                        return self.send({'path':result.stdout.strip() if result.returncode==0 else ''})
                    if route == '/api/open':workspace.open(data)
                    elif route == '/api/start':
                        options = Options(model=data.get('model','u2net'),layout=data.get('layout','original'),size=int(data.get('size',1500)),occupancy=float(data.get('occupancy',.85)))
                        options.validate()
                        if options.model not in ALLOWED:raise ValueError('模型未启用')
                        mode = data.get('mode','resume')
                        if mode not in {'resume','restart_all','restart_unapproved'}:raise ValueError('批次操作无效')
                        if mode in {'restart_all','restart_unapproved'} and data.get('confirmed') is not True:raise ValueError('整批重做需要确认')
                        if mode=='restart_unapproved' and workspace.store:
                            rows={row['source']:row for row in workspace.store.entries()}
                            if not any(rows.get(p.relative_to(workspace.source).as_posix(),{}).get('status')!='approved' for p in workspace.paths.values()):raise ValueError('没有需要重做的未通过图片')
                        pending = workspace.store.restart_state() if workspace.store else None
                        if pending and mode == 'resume':options = Options(**pending['options'])
                        workspace.edit_session=None
                        workspace.start({'action':'batch','options':vars(options),'mode':mode})
                    elif route == '/api/desktop':
                        if not workspace.store:raise ValueError('请先打开批次')
                        workspace.external = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--desktop',str(workspace.destination),str(workspace.source)])
                    else:
                        path = workspace.path(data['id']);key = path.relative_to(workspace.source).as_posix()
                        if route == '/api/card':raise ValueError('已取消独立备注保存；请在标记返工时填写文件名备注')
                        elif route == '/api/edit':return self.send(workspace.edit(key,data))
                        elif route == '/api/crop':
                            row=workspace.store.save_crop(key,int(data['revision']),data.get('box'),data.get('space','source'))
                            if workspace.edit_session and workspace.edit_session['key']==key:
                                workspace.edit_session['revision']=row['revision']
                            return self.send({'row':row})
                        elif route == '/api/background':
                            row=workspace.store.save_background(key,int(data['revision']),data.get('background'))
                            if workspace.edit_session and workspace.edit_session['key']==key:workspace.edit_session['revision']=row['revision']
                            return self.send({'row':row})
                        elif route == '/api/geometry':
                            row=(workspace.store.reset_composition(key,int(data['revision'])) if data.get('reset_composition') is True else workspace.store.save_geometry(key,int(data['revision']),data.get('geometry')))
                            if workspace.edit_session and workspace.edit_session['key']==key:workspace.edit_session['revision']=row['revision']
                            return self.send({'row':row})
                        elif route == '/api/classify':raise ValueError('网页版已移除预分类，请直接开始抠图')
                        elif route == '/api/retry':workspace.start({'action':'retry','key':key,'revision':int(data['revision']),'model':data['model']})
                        elif route == '/api/decide':
                            row = workspace.store.get(key)
                            if not row:raise ValueError('尚无候选记录，请先生成候选')
                            row=workspace.store.decide(key,int(data['revision']),data['decision'],str(data.get('reason','')),data.get('naming','original'),str(data.get('custom_name','')))
                            return self.send({'ok':True,'export':workspace.export_info(key,row)})
                        else:raise ValueError('接口不存在')
                    self.send({'ok':True})
            except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:self.send({'error':str(exc)},400)
    return Handler


def main():
    token = secrets.token_urlsafe(32);workspace = Workspace()
    server = ThreadingHTTPServer(('127.0.0.1',0), handler(workspace,token))
    url = f'http://127.0.0.1:{server.server_port}/?token={token}'
    print('CutCut 已启动。关闭终端或按 Control+C 停止服务。',flush=True)
    webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        if workspace.process:
            workspace.process.terminate();workspace.process.wait()
        server.server_close()

if __name__ == '__main__':
    if sys.argv[1:] == ['--worker']:
        try:worker(json.load(sys.stdin))
        except Exception as exc:print(str(exc),flush=True);sys.exit(1)
    elif sys.argv[1:2] == ['--desktop']:
        import tkinter as tk
        from editor import ReviewWindow
        root = tk.Tk();root.withdraw();window = ReviewWindow(root,sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None);root.wait_window(window.window);root.destroy()
    else:main()
