"""Human review workflow. Candidate masks are lossless; final JPEGs require a decision."""
from __future__ import annotations
import re
import hashlib
from dataclasses import replace
import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager, closing
from pathlib import Path
from PIL import Image, ImageChops
from image_geometry import validate_geometry, apply_geometry, geometry_size, on_white
from core import (Options, VERSION, batch_cache, collect_images, digest, load_image,
                  EXTENSIONS, make_session, remove_background, render_white, inspect_cutout, save_image_atomic)

STATUSES = {'pending':'待审核','editing':'编辑中','approved':'已通过','redo':'待重制','failed':'处理失败','processing':'正在处理'}


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('记录中的路径超出对应文件夹')
    return path


def rework_note(row):
    if not row or row.get('status') != 'redo' or not row.get('export'):return ''
    name = Path(row['export']).stem
    prefix = Path(row.get('export_name') or row['source']).stem + '_返工-'
    return name[len(prefix):] if name.startswith(prefix) else ''


class BatchStore:
    def __init__(self, destination, source=None):
        self.destination = Path(destination).resolve()
        self.shared_cache = batch_cache(self.destination) / 'human-review-v1'
        self.shared_cache.mkdir(parents=True, exist_ok=True)
        self.mutex = self.shared_cache / 'store.lock'
        source = Path(source).resolve() if source is not None else None
        self.destination.mkdir(parents=True, exist_ok=True)
        for folder in ('白底图', '待重制'):
            path = self.destination / folder
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValueError('输出子目录不能是文件或符号链接：' + folder)
            path.mkdir(exist_ok=True)
        # One output lock protects every batch, including naming and export recovery.
        with self.mutex.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            legacy = self.shared_cache / 'review.sqlite3'
            previous = None
            if legacy.exists():
                with closing(sqlite3.connect(legacy)) as c:
                    previous = c.execute("SELECT value FROM metadata WHERE key='source'").fetchone()
            if source is None:
                if not previous:raise ValueError('该位置没有本版审核记录，请先生成候选图')
                source = Path(previous[0])
            self.source = source
            self.batch_id = hashlib.sha256(str(source).encode()).hexdigest()[:20]
            self.cache = (self.shared_cache if not previous or Path(previous[0]) == source
                          else self.shared_cache / 'sources' / self.batch_id)
            self.cache.mkdir(parents=True, exist_ok=True)
            self.masks = self.cache / 'masks'; self.masks.mkdir(exist_ok=True)
            self.db = self.cache / 'review.sqlite3'
            with closing(sqlite3.connect(self.db)) as c, c:
                c.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS items (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS export_intents (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
                c.execute("INSERT OR IGNORE INTO metadata VALUES ('source', ?)", (str(source),))
                saved = c.execute("SELECT value FROM metadata WHERE key='source'").fetchone()[0]
                if saved != str(source):raise ValueError('审核记录与原图目录不匹配')
            fcntl.flock(lock, fcntl.LOCK_UN)
        with self.locked() as c:self._recover(c)
        self._retire_legacy_notes()

    def _retire_legacy_notes(self):
        # Move old rework notes to filenames before removing legacy text fields.
        # Processing settings, masks and crop coordinates remain available for resume.
        for row in self.entries():
            if row.get('status') == 'redo' and row.get('human'):
                try:
                    row = self.decide(row['source'], row['revision'], 'redo', row['human'],
                                      'custom', row.get('export_name') or self._export_filename(row['source']))
                except (OSError, ValueError) as exc:
                    raise ValueError('旧返工备注暂未迁移，已保留原记录：' + str(exc)) from exc
            if 'human' in row or 'task_context' in row:
                with self.locked() as c:
                    current = self._get(c, row['source'])
                    if current:self._put(c, current)
        with self.locked() as c:
            report = c.execute("SELECT value FROM metadata WHERE key='report'").fetchone()
            if report and Path(report[0]).name == report[0] and report[0].startswith('重制原因') and report[0].endswith('.csv'):
                path = safe_path(self.destination/'待重制', report[0])
                if path.is_file() and path.read_text(encoding='utf-8-sig').startswith('原图相对路径,自动提示,人工原因,'):
                    path.unlink()
                c.execute("DELETE FROM metadata WHERE key='report'")

    @contextmanager
    def locked(self):
        with self.mutex.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            connection = sqlite3.connect(self.db, timeout=30)
            connection.execute('ATTACH DATABASE ? AS output_registry', (str(self.shared_cache/'exports.sqlite3'),))
            connection.execute('CREATE TABLE IF NOT EXISTS output_registry.paths (path TEXT PRIMARY KEY, owner TEXT NOT NULL)')
            try:
                with connection:
                    yield connection
            finally:
                connection.close(); fcntl.flock(lock, fcntl.LOCK_UN)

    def _get(self, connection, key):
        if connection.execute('SELECT 1 FROM export_intents LIMIT 1').fetchone():self._recover(connection)
        row = connection.execute('SELECT data FROM items WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, connection, row):
        row.pop('human', None)
        row.pop('task_context', None)
        connection.execute('INSERT OR REPLACE INTO items VALUES (?, ?)',
                           (row['source'], json.dumps(row, ensure_ascii=False)))

    def get(self, key):
        with self.locked() as c:return self._get(c, key)

    def entries(self):
        with self.locked() as c:
            if c.execute('SELECT 1 FROM export_intents LIMIT 1').fetchone():self._recover(c)
            return [json.loads(r[0]) for r in c.execute('SELECT data FROM items ORDER BY key')]

    def _gc(self,connection):
        referenced=set()
        for item in connection.execute('SELECT data FROM items'):
            row=json.loads(item[0])
            referenced.update(name for name in [row.get('mask'),row.get('base_mask')] if name)
        for path in self.masks.glob('*.png'):
            if path.name not in referenced:path.unlink()

    def _finish_intent(self,connection,key,intent):
        row=intent['row'];target=safe_path(self.destination,row['export'])
        staging=safe_path(self.cache,intent['staging'])
        if not target.is_file() or digest(target)!=row['export_hash']:
            if not staging.is_file():
                connection.execute('DELETE FROM export_intents WHERE key=?',(key,))
                connection.commit()
                return
            if target.exists() and not (intent.get('old_export')==row['export'] and digest(target)==intent.get('old_hash')):
                raise ValueError('未完成导出的目标被外部修改，请先移走该文件')
            target.parent.mkdir(parents=True,exist_ok=True);os.replace(staging,target)
        self._put(connection,row);connection.commit()
        self._report(connection)
        old_name=intent.get('old_export')
        if old_name and old_name!=row['export']:
            old=safe_path(self.destination,old_name)
            if old.exists():
                if digest(old)!=intent.get('old_hash'):raise ValueError('旧导出被外部修改，请另存或移走后继续')
                old.unlink()
        connection.execute('DELETE FROM export_intents WHERE key=?',(key,));connection.commit()
        if staging.exists():staging.unlink()

    def _recover(self,connection):
        intents=list(connection.execute('SELECT key,data FROM export_intents'))
        for key,data in intents:self._finish_intent(connection,key,json.loads(data))
        self._report(connection)

    def _assert_revision(self, row, revision):
        if not row or row['revision'] != revision:
            raise ValueError('图片已被重新处理或在别处修改，请刷新后重新打开')

    def _validate_source(self, row):
        path = safe_path(self.source, row['source'])
        if not path.is_file() or digest(path) != row.get('source_hash'):
            raise ValueError('原图已经改变，请重新生成候选图；旧确认不能继续使用')
        return path

    def _owned_export(self, row):
        if not row.get('export'):
            return None
        path = safe_path(self.destination, row['export'])
        if path.exists() and digest(path) != row.get('export_hash'):
            raise ValueError('导出图片被外部修改，请先另存或移走该文件，再继续；工具不会覆盖它')
        return path

    def _retire_export(self, row):
        path = self._owned_export(row)
        if path and path.is_file():path.unlink()
        row['export'] = None; row['export_hash'] = None

    def _write_mask(self, image):
        path = self.masks / (uuid.uuid4().hex + '.png')
        save_image_atomic(image.convert('L'), path, 'PNG')
        return path.name, digest(path)

    def load_mask(self, row, base=False):
        name = row.get('base_mask' if base else 'mask')
        expected = row.get('base_hash' if base else 'mask_hash')
        if not name:raise ValueError('没有候选蒙版，请重试抠图')
        path = safe_path(self.masks, name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('内部蒙版丢失或改变，请重新生成候选图')
        with Image.open(path) as image:return image.convert('L')

    def _report(self, connection):
        # Kept as a recovery checkpoint; notes are carried only by export filenames.
        pass

    def prepare(self, key, source_hash, options, revision=None, override_base=None):
        """Invalidates an old decision before inference; no mask is exposed while processing."""
        with self.locked() as c:
            old = self._get(c, key)
            if revision is not None:self._assert_revision(old,revision)
            if old:self._retire_export(old)
            row = {'source':key,'source_hash':source_hash,'options':vars(options),
                   'config':options.signature(),'status':'processing','revision':(old or {}).get('revision',0)+1,
                   'automatic':'','error':'','mask':None,'base_mask':None,'export':None,'export_hash':None,
                   'override_base':override_base,'export_name':(old or {}).get('export_name')}
            self._put(c,row);c.commit();self._report(c);self._gc(c)
            return row

    def finish(self, row, mask, automatic, routing=None):
        with self.locked() as c:
            current = self._get(c,row['source']); self._assert_revision(current,row['revision'])
            name,checksum=self._write_mask(mask)
            current.update(mask=name,mask_hash=checksum,base_mask=name,base_hash=checksum,
                           status='pending',automatic=automatic,error='',routing=routing)
            self._put(c,current);c.commit();self._report(c);self._gc(c)
            return current

    def fail(self, row, error):
        with self.locked() as c:
            current=self._get(c,row['source']);self._assert_revision(current,row['revision'])
            current.update(status='failed',error=error)
            self._put(c,current);self._report(c)
            return current

    def resumable(self, row, source_hash, options):
        if row and row['status']=='redo' and not row.get('mask'):
            return row.get('source_hash')==source_hash and row.get('config')==options.signature()
        if not row or row['status'] in {'failed','processing'} or row.get('error'):
            return False
        if row.get('source_hash') != source_hash or row.get('config') != options.signature():return False
        try:
            self.load_mask(row);self.load_mask(row,base=True)
            path=self._owned_export(row)
            if row['status'] in {'approved','redo'} and (not path or not path.is_file()):
                with self.locked() as c:
                    current=self._get(c,row['source']);self._assert_revision(current,row['revision'])
                    self._retire_export(current)
                    current.update(status='editing',revision=current['revision']+1,
                                   automatic=(current.get('automatic','')+'；导出文件丢失，编辑成果保留，请重新确认').strip('；'))
                    self._put(c,current);c.commit();self._report(c)
                    row.clear();row.update(current)
                return True
            return True
        except (OSError,ValueError):return False

    def begin_edit(self, key, revision):
        with self.locked() as c:
            row=self._get(c,key);self._assert_revision(row,revision)
            self._validate_source(row);self.load_mask(row)
            if row['status']=='processing':raise ValueError('图片正在重新处理，请稍后刷新')
            self._retire_export(row)
            row.update(status='editing',revision=row['revision']+1,error='')
            self._put(c,row);c.commit();self._report(c);self._gc(c);return row

    def save_edit(self, key, revision, mask, human):
        with self.locked() as c:
            row=self._get(c,key);self._assert_revision(row,revision);self._validate_source(row)
            original=load_image(safe_path(self.source,key))
            if mask.size != original.size:raise ValueError('编辑蒙版必须与原图尺寸相同')
            name,checksum=self._write_mask(mask)
            self._retire_export(row)
            row.update(mask=name,mask_hash=checksum,status='editing',revision=row['revision']+1,error='')
            self._put(c,row);c.commit();self._report(c);self._gc(c);return row

    def crop_frame_size(self, row, size=None):
        size=size or self.load_mask(row).size
        if row.get('crop'):
            x1,y1,x2,y2=row['crop'];size=(x2-x1,y2-y1)
        return geometry_size(size,row.get('geometry'))

    def result_size(self, row, size=None):
        if row.get('output_crop'):
            x1,y1,x2,y2=row['output_crop'];return x2-x1,y2-y1
        return self.crop_frame_size(row,size)

    def save_crop(self, key, revision, box, space='source'):
        if space not in {'source','adjusted'}:raise ValueError('裁剪坐标系无效')
        with self.locked() as c:
            row=self._get(c,key);self._assert_revision(row,revision);self._validate_source(row)
            mask=self.load_mask(row)
            if row['status']=='processing':raise ValueError('图片正在处理')
            width,height=self.crop_frame_size(row,mask.size) if space=='adjusted' else mask.size
            if box is not None:
                if not isinstance(box,list) or len(box)!=4 or any(type(v)!=int for v in box):raise ValueError('裁剪范围无效')
                x1,y1,x2,y2=box
                if not (0<=x1<x2<=width and 0<=y1<y2<=height):raise ValueError('裁剪范围超出图片')
            field='output_crop' if space=='adjusted' else 'crop'
            if row.get(field)==box and not (box is None and (row.get('crop') or row.get('output_crop'))):return row
            self._retire_export(row)
            row[field]=box
            if space=='source':row.pop('output_crop',None)
            if box is None:row.pop('crop',None);row.pop('output_crop',None)
            row.update(status='editing',revision=row['revision']+1,error='')
            self._put(c,row);c.commit();self._report(c);return row

    def save_geometry(self, key, revision, value):
        value=validate_geometry(value)
        with self.locked() as c:
            row=self._get(c,key);self._assert_revision(row,revision);self._validate_source(row)
            self.load_mask(row)
            if row['status']=='processing':raise ValueError('图片正在处理')
            if validate_geometry(row.get('geometry'))==value:return row
            self._retire_export(row)
            # Post-correction crop coordinates belong to this exact geometry.
            row.pop('output_crop',None)
            row.update(geometry=value,status='editing',revision=row['revision']+1,error='')
            self._put(c,row);c.commit();self._report(c);return row

    def preview(self, row, mask=None):
        original=load_image(self._validate_source(row))
        alpha=self.load_mask(row) if mask is None else mask
        if alpha.size != original.size:raise ValueError('蒙版尺寸与原图不一致')
        cutout=original.copy();cutout.putalpha(ImageChops.multiply(alpha,original.getchannel('A')))
        # Review is always in original coordinates, even when exporting a square.
        canvas=Image.new('RGBA',original.size,'white');canvas.alpha_composite(cutout)
        return original,canvas.convert('RGB')

    def preview_result(self, row, crop=False, geometry=None, limit=1800, frame=False):
        # Downsample before compositing/warping; slider previews do not create full-size RGBA copies.
        image=load_image(self._validate_source(row))
        mask=self.load_mask(row)
        if image.size!=mask.size:raise ValueError('蒙版尺寸与原图不一致')
        frame_size=self.crop_frame_size(row,image.size)
        if crop and row.get('crop'):
            image=image.crop(tuple(row['crop']));mask=mask.crop(tuple(row['crop']))
        image.thumbnail((limit,limit))
        if mask.size!=image.size:mask=mask.resize(image.size,Image.Resampling.LANCZOS)
        image.putalpha(ImageChops.multiply(mask,image.getchannel('A')))
        image=apply_geometry(image,row.get('geometry') if crop and geometry is None else geometry)
        if crop and not frame and row.get('output_crop'):
            width,height=frame_size
            x1,y1,x2,y2=row['output_crop']
            box=(round(x1*image.width/width),round(y1*image.height/height),
                 round(x2*image.width/width),round(y2*image.height/height))
            image=image.crop((box[0],box[1],max(box[0]+1,box[2]),max(box[1]+1,box[3])))
        return on_white(image)

    def suggest_crop(self, row):
        self._validate_source(row)
        mask=self.load_mask(row)
        size=self.crop_frame_size(row,mask.size)
        if row.get('crop'):mask=mask.crop(tuple(row['crop']))
        mask.thumbnail((1800,1800))
        image=Image.new('RGBA',mask.size);image.putalpha(mask)
        adjusted=apply_geometry(image,row.get('geometry')).getchannel('A')
        box=adjusted.point(lambda v:255 if v>8 else 0).getbbox()
        if not box:raise ValueError('没有可裁剪的主体，请先修整图片')
        import math
        width,height=size;pad=max(3,round(max(size)*.005))
        return {'box':[max(0,math.floor(box[0]*width/adjusted.width)-pad),
                       max(0,math.floor(box[1]*height/adjusted.height)-pad),
                       min(width,math.ceil(box[2]*width/adjusted.width)+pad),
                       min(height,math.ceil(box[3]*height/adjusted.height)+pad)],'dimensions':size}

    def _export_filename(self, key):
        return Path(key).stem + '.jpg'

    def _allocate_export(self, c, row, folder, naming='original', custom_name='', note=''):
        owner = self.batch_id + ':' + row['source']
        if naming not in {'original', 'custom'}:raise ValueError('导出命名方式无效')
        if naming == 'custom':
            stem = str(custom_name).strip()
            if stem.lower().endswith('.jpg'):stem = stem[:-4]
            if not stem or stem in {'.', '..'} or re.search(r'[\\/:*?"<>|\x00-\x1f\x7f]', stem) or stem.endswith('.'):
                raise ValueError('自定义名称不能为空，也不能包含路径或特殊字符')
        else:stem = Path(row['source']).stem
        # Notes never become a path or a separate persisted field.
        text = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', '_', str(note))
        text = re.sub(r'\s+', ' ', text).strip(' ._')
        if str(note).strip() and not text:raise ValueError('请输入有效的返工备注')
        suffix = '_返工-' + text if text else ''
        index = 1
        while True:
            base = stem + ('' if index == 1 else '_' + str(index))
            name = base + suffix + '.jpg'
            if len(name.encode('utf-8')) > 240:raise ValueError('导出名称过长，请缩短名称或返工备注')
            relative = (Path(folder)/Path(row['source']).parent/name).as_posix()
            target = safe_path(self.destination, relative)
            claim = c.execute('SELECT owner FROM output_registry.paths WHERE path=?', (relative,)).fetchone()
            own = row.get('export') == relative and target.is_file() and digest(target) == row.get('export_hash')
            if (not claim or claim[0] == owner) and (not target.exists() or own):
                c.execute('INSERT OR REPLACE INTO output_registry.paths VALUES (?,?)', (relative,owner))
                return relative, base + '.jpg'
            index += 1

    def restart_state(self):
        with self.locked() as c:
            value = c.execute("SELECT value FROM metadata WHERE key='restart'").fetchone()
            return json.loads(value[0]) if value else None


    def decide(self, key, revision, decision, human='', naming='original', custom_name=''):
        if decision not in {'approved','redo'}:raise ValueError('审核决定无效')
        with self.locked() as c:
            row=self._get(c,key);self._assert_revision(row,revision)
            if row['status']=='processing':raise ValueError('图片正在处理，不能确认')
            if decision=='approved' and not row.get('mask'):raise ValueError('没有候选图，不能确认通过')
            old=self._owned_export(row)
            export=None;checksum=None
            if row.get('mask') or decision == 'redo':
                try:original=load_image(self._validate_source(row))
                except OSError as exc:raise ValueError('原图无法解码，不能导出返工图片；请检查原文件') from exc
                cutout=original.copy()
                if row.get('mask'):
                    alpha=self.load_mask(row)
                    cutout.putalpha(ImageChops.multiply(alpha,original.getchannel('A')))
                if decision=='approved':
                    if row.get('crop'):cutout=cutout.crop(tuple(row['crop']))
                    cutout=apply_geometry(cutout,row.get('geometry'))
                    if row.get('output_crop'):cutout=cutout.crop(tuple(row['output_crop']))
                    if row.get('crop') or row.get('output_crop'):white=on_white(cutout)
                    else:white,_=render_white(cutout,Options(**row['options']))
                else:
                    white=Image.new('RGBA',original.size,'white');white.alpha_composite(cutout);white=white.convert('RGB')
                folder='白底图' if decision=='approved' else '待重制'
                export, export_name = self._allocate_export(c, row, folder, naming, custom_name, human if decision == 'redo' else '')
                target=safe_path(self.destination,export)
                if target.exists() and (row.get('export')!=export or digest(target)!=row.get('export_hash')):
                    raise ValueError('目标位置已有非本次生成或被修改的文件，请移走后再导出')
                # Validation happens on a staged file; a failed write cannot destroy an existing result.
                staging=self.cache/('export-'+uuid.uuid4().hex+'.jpg')
                try:
                    save_image_atomic(white,staging,'JPEG',quality=100,subsampling=0)
                    with Image.open(staging) as saved:
                        saved.load()
                        if saved.size != white.size:raise ValueError('导出文件尺寸异常，已保留候选和编辑成果')
                    checksum=digest(staging)
                    if digest(safe_path(self.source,key))!=row['source_hash']:raise ValueError('导出时原图发生变化，请重新生成')
                    finished=dict(row)
                    finished.pop('human', None);finished.pop('task_context', None)
                    finished.update(status=decision,export=export,export_name=export_name,export_naming=naming,export_hash=checksum,revision=row['revision']+1)
                    intent={'row':finished,'staging':staging.name,'old_export':row.get('export'),'old_hash':row.get('export_hash')}
                    c.execute('INSERT OR REPLACE INTO export_intents VALUES (?,?)',(key,json.dumps(intent,ensure_ascii=False)))
                    c.commit()
                    self._finish_intent(c,key,intent)
                    return finished
                finally:
                    # An interrupted intent owns its staging file until recovery completes.
                    outstanding=c.execute('SELECT 1 FROM export_intents WHERE key=?',(key,)).fetchone()
                    if staging.exists() and not outstanding:staging.unlink()
            elif decision=='redo' and old and old.exists():old.unlink()
            row.update(status=decision,export=export,export_hash=checksum,revision=row['revision']+1)
            self._put(c,row);c.commit();self._report(c);self._gc(c);return row


def run_batch(source,destination,options=None,emit=None,cancel=None,session_factory=make_session,remover=remove_background,task_contexts=None,image_keys=None,restart=False,continue_existing=False):
    options=options or Options();options.validate()
    if options.white_format!='jpg':raise ValueError('人工审核版最终输出固定为 JPG')
    emit=emit or (lambda event,value:None);cancel=cancel or threading.Event()
    source=Path(source).resolve();destination=Path(destination).resolve()
    images=collect_images(source,destination,options.recursive)
    if image_keys is not None:
        selected=set(image_keys)
        images=[path for path in images if path.relative_to(source).as_posix() in selected]
    if not images:raise ValueError('本次分组没有可处理的图片')
    store=BatchStore(destination,source)
    lock=(store.cache/'batch.lock').open('a+')
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ValueError('该批次正在生成候选，请勿重复启动') from exc
        with store.locked() as c:
            saved = c.execute("SELECT value FROM metadata WHERE key='restart'").fetchone()
            restart_plan = json.loads(saved[0]) if saved else None
            if restart:
                restart_plan = {'options':vars(options), 'pending':[p.relative_to(source).as_posix() for p in images]}
                c.execute("INSERT OR REPLACE INTO metadata VALUES ('restart', ?)", (json.dumps(restart_plan),))
            if restart_plan:
                options = Options(**restart_plan['options'])
                pending = set(restart_plan['pending'])
                images = [p for p in images if p.relative_to(source).as_posix() in pending]
        stats={'total':len(images),'success':0,'skipped':0,'failed':0,'review':0,'auto_pass':0,'cancelled':False}
        emit('total',len(images));sessions={}
        for index,path in enumerate(images,1):
            if cancel.is_set():stats['cancelled']=True;break
            key=path.relative_to(source).as_posix();emit('status',f'{index}/{len(images)} · {key}')
            try:source_hash=digest(path)
            except OSError as exc:
                row=store.prepare(key,None,options);row=store.fail(row,f'原图无法读取：{exc}')
                stats['failed']+=1;stats['review']+=1
                emit('row',row);emit('log',f'失败：{key} · {exc}');emit('progress',index)
                continue
            old=store.get(key)
            effective=options
            context=(task_contexts or {}).get(key)
            if context is not None:
                from task_cards import context_hash, MODELS as TASK_MODELS
                if context.get('sha256')!=source_hash:raise ValueError('任务卡原图已改变，请重新打开任务卡')
                chosen=context.get('model') or options.model
                if chosen not in set(TASK_MODELS.values())-{'',}:raise ValueError('任务卡模型未启用')
                effective=replace(options,model=chosen,task_context_hash=context_hash(context))
            # Web “continue” preserves completed candidates, even if controls changed.
            # Only the explicit confirmed restart replaces the whole batch.
            if continue_existing and not restart_plan and old and old.get('options') and (task_contexts is None or old.get('task_context') == context):
                previous_options = Options(**old['options'])
                if store.resumable(old,source_hash,previous_options):effective = previous_options
            base_options=effective
            if options.resume and not restart_plan and old and old.get('override_base')==base_options.signature():
                effective=Options(**old['options'])
            if options.resume and not restart_plan and store.resumable(old,source_hash,effective):
                row=old;stats['skipped']+=1
            else:
                row=store.prepare(key,source_hash,effective,old['revision'] if old else None,old.get('override_base') if old and effective is not base_options else None)
                try:
                    original=load_image(path)
                    session=sessions.get(effective.model)
                    if session is None:
                        emit('status','正在准备本地模型，首次使用需下载；进度显示在终端')
                        try:
                            session=None
                            sessions.clear()  # Keep only one model session in memory.
                            session=session_factory(effective.model);sessions[effective.model]=session
                        except Exception as exc:
                            store.fail(row,f'模型准备失败：{exc}')
                            raise RuntimeError(f'模型准备失败，停止批次：{exc}') from exc
                    if cancel.is_set():
                        row=store.fail(row,'已停止候选生成，下次继续时会重试')
                        stats['cancelled']=True;stats['review']+=1;emit('row',row)
                        break
                    cutout=remover(original,session,effective)
                    if cutout.size!=original.size:raise ValueError('模型返回尺寸与原图不一致')
                    mask=ImageChops.multiply(cutout.getchannel('A'),original.getchannel('A'))
                    inference_info=cutout.info
                    cutout=original.copy();cutout.putalpha(mask)
                    cutout.info.update({k:inference_info[k] for k in ('routing','auto_warnings') if k in inference_info})
                    white,_=render_white(cutout,effective)
                    warnings=inspect_cutout(original,cutout,white,effective)
                    if digest(path)!=source_hash:raise ValueError('原图在处理期间改变，请重试')
                    row=store.finish(row,mask,'；'.join(warnings),cutout.info.get('routing'));stats['success']+=1
                except RuntimeError as exc:
                    if str(exc).startswith('模型准备失败，停止批次'):raise
                    row=store.fail(row,f'{type(exc).__name__}: {exc}');stats['failed']+=1
                except Exception as exc:
                    row=store.fail(row,f'{type(exc).__name__}: {exc}');stats['failed']+=1
                if row['status']=='failed':emit('log',f"失败：{key} · {row['error']}")
            if row['status'] not in {'approved','redo'}:stats['review']+=1
            if restart_plan and row['status'] != 'failed':
                restart_plan['pending'].remove(key)
                with store.locked() as c:
                    if restart_plan['pending']:
                        c.execute("UPDATE metadata SET value=? WHERE key='restart'", (json.dumps(restart_plan),))
                    else:c.execute("DELETE FROM metadata WHERE key='restart'")
            emit('row',row);emit('progress',index)
        with store.locked() as c:store._report(c)
        emit('done',stats);return stats
    finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
