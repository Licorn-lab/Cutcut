"""Human review workflow. Candidate masks are lossless; final JPEGs require a decision."""
from __future__ import annotations
import csv
import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import uuid
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from PIL import Image, ImageChops
from core import (Options, VERSION, batch_cache, collect_images, digest, load_image,
                  EXTENSIONS, make_session, remove_background, render_white, inspect_cutout, save_image_atomic)

STATUSES = {'pending':'待审核','editing':'编辑中','approved':'已通过','redo':'待重制','failed':'处理失败','processing':'正在处理'}


def safe_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('记录中的路径超出对应文件夹')
    return path


class BatchStore:
    def __init__(self, destination, source=None):
        self.destination = Path(destination).resolve()
        self.cache = batch_cache(self.destination) / 'human-review-v1'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.masks = self.cache / 'masks'; self.masks.mkdir(exist_ok=True)
        self.db = self.cache / 'review.sqlite3'
        self.mutex = self.cache / 'store.lock'
        if source is not None:
            source = Path(source).resolve()
            self.destination.mkdir(parents=True, exist_ok=True)
            bad = [p.name for p in self.destination.iterdir() if p.name != '.DS_Store' and
                   (p.name not in {'白底图','待重制'} or not p.is_dir() or p.is_symlink())]
            if bad:
                raise ValueError('新版需要新的空输出文件夹或本版已有批次；不会整理或删除旧版结果。')
        with self.locked() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS items (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS export_intents (key TEXT PRIMARY KEY, data TEXT NOT NULL)')
            previous = connection.execute("SELECT value FROM metadata WHERE key='source'").fetchone()
            if previous is None:
                if source is None:
                    raise ValueError('该位置没有本版审核记录，请先生成候选图')
                if any(p.suffix.lower() in {'.jpg','.jpeg','.png'} for folder in ['白底图','待重制']
                       if (self.destination / folder).exists() for p in (self.destination / folder).rglob('*')):
                    raise ValueError('输出已有图片但审核记录不存在，请选择新的空输出位置')
                connection.execute("INSERT INTO metadata VALUES ('source', ?)", (str(source),))
                self.source = source
            else:
                self.source = Path(previous[0])
                if source is not None and source != self.source:
                    raise ValueError('这个输出位置属于另一批原图，请选择新的空输出位置')
        for folder in ['白底图','待重制']:
            (self.destination / folder).mkdir(exist_ok=True)
        with self.locked() as c:self._recover(c)

    @contextmanager
    def locked(self):
        with self.mutex.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            connection = sqlite3.connect(self.db, timeout=30)
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
        rows = [json.loads(r[0]) for r in connection.execute('SELECT data FROM items ORDER BY key')]
        folder = self.destination / '待重制'
        fd, tmp = tempfile.mkstemp(prefix='.report-', dir=folder)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(['原图相对路径','自动提示','人工原因','重制图片路径（相对输出位置）','处理错误'])
                for row in rows:
                    if row['status'] == 'redo':
                        writer.writerow([row['source'],row.get('automatic',''),row.get('human',''),row.get('export') or '',row.get('error','')])
            os.replace(tmp, folder / '重制原因.csv')
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

    def prepare(self, key, source_hash, options, revision=None, override_base=None):
        """Invalidates an old decision before inference; no mask is exposed while processing."""
        with self.locked() as c:
            old = self._get(c, key)
            if revision is not None:self._assert_revision(old,revision)
            if old:self._retire_export(old)
            row = {'source':key,'source_hash':source_hash,'options':vars(options),
                   'config':options.signature(),'status':'processing','revision':(old or {}).get('revision',0)+1,
                   'automatic':'','human':'','error':'','mask':None,'base_mask':None,'export':None,'export_hash':None,
                   'override_base':override_base}
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
            row.update(mask=name,mask_hash=checksum,human=human,status='editing',revision=row['revision']+1,error='')
            self._put(c,row);c.commit();self._report(c);self._gc(c);return row

    def preview(self, row, mask=None):
        original=load_image(self._validate_source(row))
        alpha=self.load_mask(row) if mask is None else mask
        if alpha.size != original.size:raise ValueError('蒙版尺寸与原图不一致')
        cutout=original.copy();cutout.putalpha(ImageChops.multiply(alpha,original.getchannel('A')))
        # Review is always in original coordinates, even when exporting a square.
        canvas=Image.new('RGBA',original.size,'white');canvas.alpha_composite(cutout)
        return original,canvas.convert('RGB')

    def _export_filename(self,key):
        path=Path(key)
        folded=lambda text:unicodedata.normalize('NFC',text).casefold()
        parent=safe_path(self.source,key).parent
        conflicts=[other.name for other in parent.iterdir() if other.is_file()
                   and other.suffix.lower() in EXTENSIONS and other.name!=path.name
                   and folded(other.stem)==folded(path.stem)]
        if conflicts:raise ValueError('导出命名冲突：'+path.name+' 与 '+ '、'.join(conflicts)+' 同名。请先为原图设置不同文件名，再生成并审核；不会覆盖。')
        return path.stem+'w.jpg'

    def decide(self, key, revision, decision, human):
        if decision not in {'approved','redo'}:raise ValueError('审核决定无效')
        with self.locked() as c:
            row=self._get(c,key);self._assert_revision(row,revision)
            if row['status']=='processing':raise ValueError('图片正在处理，不能确认')
            if decision=='approved' and not row.get('mask'):raise ValueError('没有候选图，不能确认通过')
            old=self._owned_export(row)
            export=None;checksum=None
            if row.get('mask'):
                original=load_image(self._validate_source(row));alpha=self.load_mask(row)
                cutout=original.copy();cutout.putalpha(ImageChops.multiply(alpha,original.getchannel('A')))
                if decision=='approved':white,_=render_white(cutout,Options(**row['options']))
                else:
                    white=Image.new('RGBA',original.size,'white');white.alpha_composite(cutout);white=white.convert('RGB')
                folder='白底图' if decision=='approved' else '待重制'
                export=(Path(folder)/Path(key).parent/self._export_filename(key)).as_posix()
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
                    finished.update(status=decision,human=human,export=export,export_hash=checksum,revision=row['revision']+1)
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
            row.update(status=decision,human=human,export=export,export_hash=checksum,revision=row['revision']+1)
            self._put(c,row);c.commit();self._report(c);self._gc(c);return row


def run_batch(source,destination,options=None,emit=None,cancel=None,session_factory=make_session,remover=remove_background):
    options=options or Options();options.validate()
    if options.white_format!='jpg':raise ValueError('人工审核版最终输出固定为 JPG')
    emit=emit or (lambda event,value:None);cancel=cancel or threading.Event()
    source=Path(source).resolve();destination=Path(destination).resolve()
    images=collect_images(source,destination,options.recursive)
    if not images:raise ValueError('原图位置没有支持的静态图片')
    store=BatchStore(destination,source)
    lock=(store.cache/'batch.lock').open('a+')
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ValueError('该批次正在生成候选，请勿重复启动') from exc
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
            if options.resume and old and old.get('override_base')==options.signature():
                effective=Options(**old['options'])
            if options.resume and store.resumable(old,source_hash,effective):
                row=old;stats['skipped']+=1
            else:
                row=store.prepare(key,source_hash,effective,old['revision'] if old else None,old.get('override_base') if old and effective is not options else None)
                try:
                    original=load_image(path)
                    session=sessions.get(effective.model)
                    if session is None:
                        emit('status','正在准备本地模型，首次使用需下载；进度显示在终端')
                        try:
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
            emit('row',row);emit('progress',index)
        with store.locked() as c:store._report(c)
        emit('done',stats);return stats
    finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
