"""HTTP integration tests use tiny fake masks; no model downloads."""
from urllib.parse import quote
import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import web_server as web
from task_cards import TaskCards
from core import Options, digest
from workflow import run_batch, BatchStore

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.source.mkdir();self.image=self.source/'sample.png'
        Image.new('RGB',(80,60),'red').save(self.image);self.before=digest(self.image)
        self.patches=[patch.object(web,'SETTINGS',self.root/'settings.json'),patch('task_cards.DATA',self.root/'cards'),patch.dict('os.environ',{'WHITEBG_DATA_HOME':str(self.root/'runtime')})]
        for p in self.patches:p.start()
        self.ws=web.Workspace();self.ws.open({'source':str(self.source),'destination':str(self.root/'out')})
        self.ident=next(iter(self.ws.paths));self.server=web.ThreadingHTTPServer(('127.0.0.1',0),web.handler(self.ws,'test-token'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def request(self,path,data=None,headers=None):
        h={'Cookie':'whiteflow=test-token'}
        if data is not None:h['Content-Type']='application/json'
        h.update(headers or {})
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        conn.request('POST' if data is not None else 'GET',path,json.dumps(data) if data is not None else None,h)
        response=conn.getresponse();body=response.read();code=response.status;conn.close()
        return code,json.loads(body) if body.startswith(b'{') else body
    def generate(self):
        def remove(im,session,opt):im=im.copy();im.putalpha(Image.new('L',im.size,255));return im
        return run_batch(self.source,self.root/'out',Options(model='u2net'),session_factory=lambda _:object(),remover=remove)
    def test_desktop_endpoint_removed(self):
        code, result = self.request('/api/desktop', {})
        self.assertEqual(code, 404)
        self.assertEqual(result['error'], '接口不存在')

    def test_export_feedback_actual_collision_name_and_safe_reveal(self):
        self.generate();row=self.ws.store.get('sample.png')
        foreign=self.root/'out/白底图/sample.jpg';foreign.write_bytes(b'other image')
        code,value=self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})
        self.assertEqual(code,200);info=value['export']
        self.assertNotEqual(info['name'],'sample.jpg');self.assertEqual(info['id'],self.ident)
        self.assertTrue(Path(info['path']).is_file());self.assertEqual(Path(info['path']).name,info['name'])
        self.assertEqual(self.request('/api/item?id='+self.ident)[1]['export'],info)
        self.ws.job['busy']=True
        with patch.object(web.subprocess,'run') as reveal:
            # An arbitrary submitted path must never become the opened path.
            code,_=self.request('/api/reveal-export',{'id':self.ident,'relative':info['relative'],'path':'/etc/passwd'})
            self.assertEqual(code,200);self.assertEqual(reveal.call_args.args[0],['open','-R',info['path']])
            self.assertEqual(self.request('/api/reveal-export',{'id':self.ident,'relative':'../outside'})[0],400)
            self.assertEqual(self.request('/api/reveal-export',{'id':'../outside','relative':info['relative']})[0],400)
            self.assertEqual(reveal.call_count,1)
            Path(info['path']).unlink()
            self.assertEqual(self.request('/api/reveal-export',{'id':self.ident,'relative':info['relative']})[0],400)
        self.assertEqual(foreign.read_bytes(),b'other image');self.assertEqual(digest(self.image),self.before)

    def test_unapproved_restart_confirmation_and_approved_review_access(self):
        self.generate();row=self.ws.store.get('sample.png')
        with patch.object(self.ws,'start') as start:
            self.assertEqual(self.request('/api/start',{'mode':'restart_unapproved'})[0],400)
            self.assertEqual(self.request('/api/start',{'mode':'restart_unapproved','confirmed':True})[0],200)
            self.assertEqual(start.call_args.args[0]['mode'],'restart_unapproved')
        row=self.ws.store.decide('sample.png',row['revision'],'approved','')
        with patch.object(self.ws,'start') as start:
            self.assertEqual(self.request('/api/start',{'mode':'restart_unapproved','confirmed':True})[0],400)
            start.assert_not_called()
        self.ws.active_request={'action':'batch','mode':'restart_unapproved'};self.ws.job.update(busy=True,progress=0)
        self.assertTrue(self.request('/api/item?id='+self.ident)[1]['reviewable'])
        self.assertEqual(self.request('/api/state')[1]['items'][0]['reviewable'],True)

    def test_manual_selection_crop_export_and_reopen(self):
        from PIL import ImageDraw
        self.generate();row=self.ws.store.get('sample.png')
        mask=Image.new('L',(80,60),0);draw=ImageDraw.Draw(mask)
        draw.rectangle((5,10,40,45),fill=255);draw.rectangle((60,20,72,35),fill=255)
        row=self.ws.store.save_edit('sample.png',row['revision'],mask,'')
        self.assertEqual(self.request(f'/api/components?id={self.ident}&revision={row["revision"]}')[0],404)
        self.assertEqual(self.request('/api/edit',{'id':self.ident,'revision':row['revision'],'action':'component','component':1})[0],400)
        code,value=self.request('/api/edit',{'id':self.ident,'revision':row['revision'],'action':'draw','shape':'rectangle','mode':'erase','points':[[59,19],[73,36]],'radius':1})
        self.assertEqual(code,200);row=value['row'];self.assertEqual(self.ws.store.load_mask(row).getpixel((65,25)),0)
        self.assertEqual(self.ws.store.load_mask(row).getpixel((20,20)),255)
        code,value=self.request('/api/edit',{'id':self.ident,'revision':row['revision'],'action':'undo'})
        self.assertEqual(code,200);row=value['row'];self.assertEqual(self.ws.store.load_mask(row).getpixel((65,25)),255)
        code,value=self.request('/api/edit',{'id':self.ident,'revision':row['revision'],'action':'redo'})
        row=value['row'];self.assertEqual(code,200)
        code,value=self.request(f'/api/crop-suggest?id={self.ident}&revision={row["revision"]}')
        self.assertEqual(code,200);self.assertEqual(value['box'],[2,7,44,49])
        self.assertEqual(self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':[0,0,100,60]})[0],400)
        code,value=self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':[2,7,44,49]})
        self.assertEqual(code,200);row=value['row']
        self.assertEqual(BatchStore(self.root/'out').get('sample.png')['crop'],[2,7,44,49])
        for kind in ('original','result'):
            code,data=self.request(f'/api/image?id={self.ident}&kind={kind}&revision={row["revision"]}&crop=1')
            self.assertEqual(code,200)
            with Image.open(io.BytesIO(data)) as preview:self.assertEqual(preview.size,(42,42))
            self.assertEqual(self.request(f'/api/image?id={self.ident}&kind={kind}&revision=0&crop=1')[0],400)
        code,data=self.request(f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}')
        with Image.open(io.BytesIO(data)) as preview:self.assertEqual(preview.size,(80,60))
        self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],200)
        with Image.open(self.root/'out/白底图/sample.jpg') as image:self.assertEqual(image.size,(42,42))
        self.assertEqual(digest(self.image),self.before)

    def test_custom_export_and_filename_only_rework_note(self):
        self.generate();row=self.ws.store.get('sample.png')
        code,_=self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'redo','reason':'去掉色卡','naming':'custom','custom_name':'作品主图.jpg'})
        self.assertEqual(code,200)
        target=self.root/'out/待重制/作品主图_返工-去掉色卡.jpg'
        self.assertTrue(target.exists())
        code,item=self.request('/api/item?id='+self.ident)
        self.assertEqual(code,200);self.assertEqual(item['rework_note'],'去掉色卡')
        self.assertEqual(item['row']['export_name'],'作品主图.jpg')
        for key in ('human','task_context','reason','requirements'):
            self.assertNotIn(key,item['row'])
        for key in ('rules','card'):self.assertNotIn(key,item)
        code,_=self.request('/api/decide',{'id':self.ident,'revision':item['row']['revision'],'decision':'approved','reason':'不应保存','naming':'original'})
        self.assertEqual(code,200);self.assertFalse(target.exists())
        self.assertTrue((self.root/'out/白底图/sample.jpg').exists())
        self.assertEqual(self.request('/api/item?id='+self.ident)[1]['rework_note'],'')
        self.assertEqual(digest(self.image),self.before)

    def test_geometry_preview_save_crop_export_and_retry(self):
        from image_geometry import apply_geometry, on_white
        self.generate();row=self.ws.store.get('sample.png')
        code,value=self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':[5,5,65,45]})
        self.assertEqual(code,200);row=value['row']
        geometry={'turns':1,'flip':True,'angle':5,'horizontal':20,'vertical':-15}
        url=f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}&crop=1&geometry='+quote(json.dumps(geometry))
        code,preview=self.request(url);self.assertEqual(code,200)
        expected=on_white(apply_geometry(Image.new('RGBA',(60,40),'red'),geometry))
        with Image.open(io.BytesIO(preview)) as image:self.assertEqual(image.size,expected.size)
        self.assertNotIn('geometry',self.ws.store.get('sample.png'))
        self.assertEqual(self.ws.store.get('sample.png')['revision'],row['revision'])
        # The original endpoint ignores output geometry, even when explicitly supplied.
        code,original=self.request(url.replace('kind=result','kind=original').replace('&crop=1',''))
        with Image.open(io.BytesIO(original)) as image:self.assertEqual(image.size,(80,60))
        self.assertEqual(self.request('/api/geometry',{'id':self.ident,'revision':row['revision'],'geometry':{'angle':90}})[0],400)
        code,value=self.request('/api/geometry',{'id':self.ident,'revision':row['revision'],'geometry':geometry})
        self.assertEqual(code,200);updated=value['row']
        self.assertEqual(self.request('/api/geometry',{'id':self.ident,'revision':row['revision'],'geometry':{}})[0],400)
        reopened=BatchStore(self.root/'out').get('sample.png')
        self.assertEqual(reopened['geometry'],geometry);self.assertEqual(reopened['crop'],[5,5,65,45])
        self.assertEqual(self.request('/api/item?id='+self.ident)[1]['adjusted_dimensions'],list(expected.size))
        self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':updated['revision'],'decision':'approved'})[0],200)
        with Image.open(self.root/'out/白底图/sample.jpg') as exported:
            self.assertEqual(exported.size,expected.size)
            import numpy as np
            self.assertLess(np.abs(np.array(exported,dtype=float)-np.array(expected,dtype=float)).mean(),2)
        approved=self.ws.store.get('sample.png')
        cleared=self.ws.store.save_geometry('sample.png',approved['revision'],None)
        self.assertEqual(cleared['status'],'editing');self.assertFalse((self.root/'out/白底图/sample.jpg').exists())
        self.assertEqual(cleared['geometry']['angle'],0)
        prepared=self.ws.store.prepare('sample.png',self.before,Options(model='u2net'),cleared['revision'])
        self.assertNotIn('geometry',prepared);self.assertNotIn('crop',prepared)
        self.assertEqual(digest(self.image),self.before)

    def test_live_geometry_base_preserves_alpha_and_completed_crop_history(self):
        from PIL import ImageDraw
        from image_geometry import apply_geometry
        image=Image.new('RGB',(80,60),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((8,10,35,45),fill='red');draw.rectangle((50,20,65,50),fill='blue')
        image.save(self.image);original_hash=digest(self.image)
        self.generate();row=self.ws.store.get('sample.png')
        mask=Image.new('L',image.size,0);ImageDraw.Draw(mask).rectangle((8,10,65,50),fill=255)
        row=self.ws.store.save_edit('sample.png',row['revision'],mask,'')
        row=self.ws.store.save_background('sample.png',row['revision'],'#e9dccb')
        geometry={'turns':1,'flip':True,'angle':10,'horizontal':25,'vertical':-15}
        row=self.ws.store.save_geometry('sample.png',row['revision'],geometry)
        rgba=image.convert('RGBA');rgba.putalpha(mask)
        def check_base(expected):
            before=self.ws.store.get('sample.png')
            code,data=self.request(f'/api/image?id={self.ident}&kind=result&revision={before["revision"]}&geometry_base=1')
            self.assertEqual(code,200)
            with Image.open(io.BytesIO(data)) as base:
                self.assertEqual(base.format,'PNG');self.assertEqual(base.mode,'RGBA')
                self.assertEqual(base.size,expected.size);self.assertEqual(base.tobytes(),expected.tobytes())
                self.assertEqual(base.getchannel('A').getextrema(),(0,255))
            self.assertEqual(self.ws.store.get('sample.png'),before)
        # A live draft replaces the editable tail, so the base excludes that geometry.
        check_base(rgba)
        corrected=apply_geometry(rgba,geometry);box=[2,3,corrected.width-2,corrected.height-3]
        old_revision=row['revision']
        row=self.ws.store.save_crop('sample.png',row['revision'],box,space='adjusted')
        completed=corrected.crop(tuple(box))
        # After a completed crop, a new live adjustment starts from that cropped result.
        check_base(completed)
        row=self.ws.store.save_geometry('sample.png',row['revision'],{'turns':2,'angle':-5})
        check_base(completed)
        self.assertEqual(self.request(f'/api/image?id={self.ident}&kind=result&revision={old_revision}&geometry_base=1')[0],400)
        code,data=self.request(f'/api/image?id={self.ident}&kind=original&geometry_base=1')
        self.assertEqual(code,200)
        with Image.open(io.BytesIO(data)) as original:self.assertEqual(original.size,(80,60))
        self.assertEqual(digest(self.image),original_hash)

    def test_corrected_crop_preview_matches_export_and_reopens(self):
        from PIL import ImageDraw
        from image_geometry import apply_geometry, on_white
        image=Image.new('RGB',(80,60),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((8,8,25,48),fill='red');draw.rectangle((45,20,68,45),fill='blue');image.save(self.image)
        original_hash=digest(self.image)
        self.generate();row=self.ws.store.get('sample.png')
        mask=Image.new('L',(80,60),0);draw=ImageDraw.Draw(mask)
        draw.rectangle((8,8,25,48),fill=255);draw.rectangle((45,20,68,45),fill=255)
        row=self.ws.store.save_edit('sample.png',row['revision'],mask,'')
        for geometry in ({'turns':1,'flip':True},{'turns':3,'angle':-8,'horizontal':25,'vertical':-20}):
            code,value=self.request('/api/geometry',{'id':self.ident,'revision':row['revision'],'geometry':geometry})
            self.assertEqual(code,200);row=value['row']
            rgba=image.convert('RGBA');rgba.putalpha(mask)
            corrected=apply_geometry(rgba,geometry)
            _,item=self.request('/api/item?id='+self.ident)
            self.assertEqual(item['crop_canvas_dimensions'],list(corrected.size))
            _,suggestion=self.request(f'/api/crop-suggest?id={self.ident}&revision={row["revision"]}')
            self.assertEqual(suggestion['dimensions'],list(corrected.size))
            box=suggestion['box'];bbox=corrected.getchannel('A').point(lambda v:255 if v>8 else 0).getbbox()
            self.assertLessEqual(box[0],bbox[0]);self.assertLessEqual(box[1],bbox[1]);self.assertGreaterEqual(box[2],bbox[2]);self.assertGreaterEqual(box[3],bbox[3])
            base=f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}&crop=1'
            code,data=self.request(base+'&frame=1');self.assertEqual(code,200)
            import numpy as np
            with Image.open(io.BytesIO(data)) as frame:
                self.assertEqual(frame.size,corrected.size)
                self.assertLess(np.abs(np.array(frame,dtype=float)-np.array(on_white(corrected),dtype=float)).mean(),5)
            # Exact user-chosen dimensions in the corrected canvas, rather than source dimensions.
            box=[2,3,corrected.width-5,corrected.height-4]
            self.assertEqual(self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':[0,0,corrected.width+1,10],'space':'adjusted'})[0],400)
            code,value=self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':box,'space':'adjusted'})
            self.assertEqual(code,200);row=value['row']
            reopened=BatchStore(self.root/'out').get('sample.png');self.assertEqual(reopened['output_crop'],box)
            expected=on_white(corrected.crop(tuple(box)))
            base=f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}&crop=1'
            _,data=self.request(base)
            with Image.open(io.BytesIO(data)) as preview:
                self.assertEqual(preview.size,expected.size)
                self.assertLess(np.abs(np.array(preview,dtype=float)-np.array(expected,dtype=float)).mean(),5)
            _,data=self.request(base+'&frame=1')
            with Image.open(io.BytesIO(data)) as frame:self.assertEqual(frame.size,corrected.size)
            _,data=self.request(base.replace('kind=result','kind=original').replace('&crop=1',''))
            with Image.open(io.BytesIO(data)) as original:self.assertEqual(original.size,(80,60))
            self.assertEqual(self.request('/api/item?id='+self.ident)[1]['adjusted_dimensions'],list(expected.size))
            self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],200)
            row=self.ws.store.get('sample.png')
            with Image.open(self.root/'out'/row['export']) as exported:
                self.assertEqual(exported.size,expected.size)
                self.assertLess(np.abs(np.array(exported,dtype=float)-np.array(expected,dtype=float)).mean(),2)
            row=self.ws.store.save_crop('sample.png',row['revision'],None,space='adjusted')
        row=self.ws.store.save_geometry('sample.png',row['revision'],{})
        self.assertNotIn('output_crop',row)
        self.assertEqual(digest(self.image),original_hash)

    def test_alternating_crop_and_geometry_preserves_previous_result(self):
        from PIL import ImageDraw
        from image_geometry import apply_geometry, on_white
        image=Image.new('RGB',(80,60),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((5,8,30,50),fill='red');draw.rectangle((42,18,73,44),fill='blue');image.save(self.image)
        before=digest(self.image);self.generate();row=self.ws.store.get('sample.png')
        first={'turns':1,'angle':7,'horizontal':20}
        row=self.ws.store.save_geometry('sample.png',row['revision'],first)
        expected=apply_geometry(image.convert('RGBA'),first)
        box=[3,4,expected.width-4,expected.height-3]
        row=self.ws.store.save_crop('sample.png',row['revision'],box,'adjusted');expected=expected.crop(box)
        next_geometry={'turns':1,'flip':True,'vertical':-12}
        preview_url=f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}&crop=1&frame=1&geometry='+quote(json.dumps(next_geometry))
        code,data=self.request(preview_url);self.assertEqual(code,200)
        expected=apply_geometry(expected,next_geometry)
        import numpy as np
        with Image.open(io.BytesIO(data)) as preview:
            self.assertEqual(preview.size,expected.size)
            self.assertLess(np.abs(np.array(preview,dtype=float)-np.array(on_white(expected),dtype=float)).mean(),5)
        row=self.ws.store.save_geometry('sample.png',row['revision'],next_geometry)
        self.assertTrue(row['image_steps']);self.assertNotIn('output_crop',row)
        suggestion=self.ws.store.suggest_crop(row);self.assertEqual(tuple(suggestion['dimensions']),expected.size)
        second=[2,2,expected.width-2,expected.height-2]
        row=self.ws.store.save_crop('sample.png',row['revision'],second,'adjusted');expected=expected.crop(second)
        reopened=BatchStore(self.root/'out').get('sample.png')
        self.assertEqual(reopened['image_steps'],row['image_steps'])
        self.assertEqual(self.ws.store.result_size(reopened),expected.size)
        self.assertEqual(self.ws.store.preview_result(reopened,crop=True).tobytes(),on_white(expected).tobytes())
        row=self.ws.store.decide('sample.png',row['revision'],'approved')
        with Image.open(self.root/'out'/row['export']) as exported:
            self.assertEqual(exported.size,expected.size)
            self.assertLess(np.abs(np.array(exported,dtype=float)-np.array(on_white(expected),dtype=float)).mean(),2)
        self.assertEqual(digest(self.image),before)

    def test_reset_composition_preserves_mask_and_background(self):
        self.generate();store=self.ws.store;key='sample.png';row=store.get(key)
        row=store.save_crop(key,row['revision'],[5,5,70,50],space='adjusted')
        row=store.save_geometry(key,row['revision'],{'turns':1})
        row=store.save_background(key,row['revision'],'transparent')
        mask_hash=row['mask_hash']
        code,value=self.request('/api/geometry',{'id':self.ident,'revision':row['revision'],'reset_composition':True})
        self.assertEqual(code,200);row=value['row']
        self.assertFalse(any(row.get(k) for k in ('crop','output_crop','geometry','image_steps')))
        self.assertEqual(store.result_size(row),(80,60))
        self.assertEqual(row['mask_hash'],mask_hash);self.assertEqual(row['background'],'transparent')
        self.assertEqual(digest(self.image),self.before)
        self.assertEqual(self.request('/api/geometry',{'id':self.ident,'revision':row['revision']-1,'reset_composition':True})[0],400)

    def test_transparent_square_export_retains_alpha(self):
        from PIL import ImageDraw
        options=Options(model='u2net',layout='square',size=300)
        row=self.ws.store.prepare('sample.png',digest(self.image),options)
        mask=Image.new('L',(80,60),0);ImageDraw.Draw(mask).rectangle((20,10,60,50),fill=255)
        row=self.ws.store.finish(row,mask,'')
        row=self.ws.store.save_background('sample.png',row['revision'],'transparent')
        row=self.ws.store.decide('sample.png',row['revision'],'approved','')
        with Image.open(self.root/'out'/row['export']) as image:
            self.assertEqual(image.size,(300,300));self.assertEqual(image.mode,'RGBA')
            self.assertEqual(image.getpixel((0,0))[3],0);self.assertEqual(image.getpixel((150,150))[3],255)

    def test_background_preview_export_conflict_and_reopen(self):
        self.generate();row=self.ws.store.get('sample.png')
        mask=Image.new('L',(80,60),0)
        from PIL import ImageDraw
        ImageDraw.Draw(mask).rectangle((10,10,60,45),fill=255)
        row=self.ws.store.save_edit('sample.png',row['revision'],mask,'')
        self.assertEqual(self.request('/api/background',{'id':self.ident,'revision':row['revision'],'background':'url(bad)'})[0],400)
        code,value=self.request('/api/background',{'id':self.ident,'revision':row['revision'],'background':'transparent'})
        self.assertEqual(code,200);row=value['row']
        _,data=self.request(f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}&crop=1')
        with Image.open(io.BytesIO(data)) as preview:
            self.assertEqual(preview.format,'PNG');self.assertEqual(preview.getpixel((0,0))[3],0)
        target=self.root/'out/白底图/sample.png';target.write_bytes(b'keep existing')
        row=self.ws.store.decide('sample.png',row['revision'],'approved')
        self.assertEqual(Path(row['export']).name,'sample_2.png');self.assertEqual(target.read_bytes(),b'keep existing')
        with Image.open(self.root/'out'/row['export']) as exported:
            self.assertEqual(exported.mode,'RGBA');self.assertEqual(exported.getpixel((0,0))[3],0)
            self.assertEqual(exported.getpixel((20,20)),(255,0,0,255))
        self.assertEqual(BatchStore(self.root/'out').get('sample.png')['background'],'transparent')
        old=self.root/'out'/row['export']
        row=self.ws.store.save_background('sample.png',row['revision'],'#438edb')
        self.assertFalse(old.exists());self.assertEqual(row['status'],'editing')
        self.assertEqual(self.ws.store.preview_result(row,crop=True).getpixel((0,0)),(67,142,219))
        row=self.ws.store.decide('sample.png',row['revision'],'approved',naming='custom',custom_name='新名称.png')
        self.assertEqual(Path(row['export']).name,'新名称.jpg')
        with Image.open(self.root/'out'/row['export']) as exported:
            self.assertEqual(exported.format,'JPEG')
            self.assertTrue(all(abs(a-b)<=2 for a,b in zip(exported.getpixel((0,0)),(67,142,219))))
        self.assertEqual(digest(self.image),self.before)

    def test_completed_image_editable_while_another_image_is_inferred(self):
        Image.new('RGB',(80,60),'blue').save(self.source/'z.png')
        self.ws.open({'source':str(self.source),'destination':str(self.root/'out')})
        ids={p.name:ident for ident,p in self.ws.paths.items()};self.ident=ids['sample.png']
        waiting=threading.Event();release=threading.Event();outcome=[];errors=[]
        def remove(im,*_):
            if im.getpixel((0,0))[2]>200:
                waiting.set()
                if not release.wait(10):raise RuntimeError('test did not release inference')
            result=im.copy();result.putalpha(Image.new('L',im.size,255));return result
        def run():
            try:outcome.append(run_batch(self.source,self.root/'out',Options(model='u2net'),session_factory=lambda _:object(),remover=remove,continue_existing=True))
            except Exception as exc:errors.append(exc)
        self.ws.job['busy']=True;self.ws.active_request={'action':'batch','mode':'resume'}
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(waiting.wait(10))
            _,state=self.request('/api/state?selected='+self.ident)
            self.assertTrue(state['job']['busy']);self.assertTrue(state['selected_reviewable'])
            self.assertIsNone(state['navigation']['next_review'])
            self.assertEqual(self.request('/api/edit',{'id':ids['z.png'],'revision':self.ws.store.get('z.png')['revision'],'action':'reset'})[0],400)
            row=self.ws.store.get('sample.png')
            code,value=self.request('/api/edit',{'id':self.ident,'revision':row['revision'],'action':'draw','shape':'rectangle','mode':'erase','points':[[0,0],[5,5]],'radius':1})
            self.assertEqual(code,200);row=value['row']
            code,value=self.request('/api/geometry',{'id':self.ident,'revision':row['revision'],'geometry':{'turns':1}})
            self.assertEqual(code,200);row=value['row']
            self.assertEqual(self.request(f'/api/crop-suggest?id={self.ident}&revision={row["revision"]}')[0],200)
            code,value=self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':[5,5,55,75],'space':'adjusted'})
            self.assertEqual(code,200);row=value['row']
            for route in ('/api/retry','/api/start','/api/open'):
                self.assertEqual(self.request(route,{'id':self.ident,'revision':row['revision'],'model':'u2net'})[0],400)
            self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],200)
            revision=self.ws.store.get('sample.png')['revision']
        finally:
            release.set();thread.join(10);self.ws.job['busy']=False
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(outcome[0]['success'],2)
        row=self.ws.store.get('sample.png')
        self.assertEqual(row['status'],'approved');self.assertEqual(row['revision'],revision)
        self.assertEqual(row['output_crop'],[5,5,55,75]);self.assertEqual(self.ws.store.load_mask(row).getpixel((0,0)),0)
        with Image.open(self.root/'out'/row['export']) as exported:self.assertEqual(exported.size,(50,70))
        self.assertEqual(self.ws.store.get('z.png')['status'],'pending')
        self.assertEqual(digest(self.image),self.before)

    def test_legacy_crop_can_be_adjusted_then_cleared_without_losing_geometry(self):
        from image_geometry import apply_geometry, on_white
        self.generate();row=self.ws.store.get('sample.png')
        row=self.ws.store.save_crop('sample.png',row['revision'],[5,5,65,45])
        row=self.ws.store.save_geometry('sample.png',row['revision'],{'turns':1,'angle':5})
        corrected=apply_geometry(Image.new('RGBA',(60,40),'red'),row['geometry'])
        box=[1,2,corrected.width-2,corrected.height-3]
        code,value=self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':box,'space':'adjusted'})
        self.assertEqual(code,200);row=value['row']
        self.assertEqual(row['crop'],[5,5,65,45]);self.assertEqual(row['output_crop'],box)
        expected=on_white(corrected.crop(tuple(box)))
        code,data=self.request(f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}&crop=1')
        self.assertEqual(code,200)
        with Image.open(io.BytesIO(data)) as preview:self.assertEqual(preview.size,expected.size)
        self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],200)
        row=self.ws.store.get('sample.png')
        with Image.open(self.root/'out'/row['export']) as exported:self.assertEqual(exported.size,expected.size)
        code,value=self.request('/api/crop',{'id':self.ident,'revision':row['revision'],'box':None,'space':'adjusted'})
        self.assertEqual(code,200);row=value['row']
        self.assertNotIn('crop',row);self.assertNotIn('output_crop',row)
        self.assertEqual(row['geometry']['turns'],1)
        self.assertEqual(self.request('/api/item?id='+self.ident)[1]['adjusted_dimensions'],list(apply_geometry(Image.new('RGBA',(80,60)),row['geometry']).size))
        self.assertEqual(digest(self.image),self.before)

    def test_restart_queue_blocks_old_candidates_but_not_completed_ones(self):
        Image.new('RGB',(80,60),'blue').save(self.source/'z.png');self.generate()
        self.ws.open({'source':str(self.source),'destination':str(self.root/'out')})
        ids={p.name:ident for ident,p in self.ws.paths.items()}
        self.ws.job.update(busy=True,progress=0);self.ws.active_request={'action':'batch','mode':'restart_all'}
        self.assertEqual(self.request('/api/edit',{'id':ids['sample.png'],'revision':self.ws.store.get('sample.png')['revision'],'action':'reset'})[0],400)
        with self.ws.store.locked() as c:
            c.execute("INSERT OR REPLACE INTO metadata VALUES ('restart',?)",(json.dumps({'pending':['z.png'],'options':vars(Options(model='u2net'))}),))
        self.ws.job['progress']=1
        code,_=self.request('/api/edit',{'id':ids['sample.png'],'revision':self.ws.store.get('sample.png')['revision'],'action':'reset'})
        self.assertEqual(code,200)
        self.assertEqual(self.request('/api/edit',{'id':ids['z.png'],'revision':self.ws.store.get('z.png')['revision'],'action':'reset'})[0],400)
        _,state=self.request('/api/state?selected='+ids['z.png'])
        self.assertFalse(state['selected_reviewable']);self.assertEqual(state['navigation']['next_review'],ids['sample.png'])

    def test_auth_origin_host_and_file_escape(self):
        self.assertEqual(self.request('/api/state',headers={'Cookie':''})[0],403)
        self.assertEqual(self.request('/api/state',headers={'Host':'evil.test'})[0],403)
        self.assertEqual(self.request('/api/stop',{}, {'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('/api/image?id=../../etc/passwd')[0],400)
        self.assertEqual(self.request('/api/state')[0],200)
        self.assertEqual(self.request('/')[0],200)
    def test_review_export_resume_stale_revision_and_context(self):
        self.generate();code,data=self.request('/api/item?id='+self.ident);self.assertEqual(code,200)
        row=data['row'];payload={'id':self.ident,'revision':row['revision'],'decision':'approved','reason':'确认'}
        self.assertEqual(self.request('/api/decide',payload)[0],200)
        self.assertTrue((self.root/'out/白底图/sample.jpg').exists())
        self.assertEqual(self.request('/api/decide',payload)[0],400)
        self.assertEqual(self.generate()['skipped'],1)
        loaded=web.Workspace();self.assertEqual(loaded.store.entries()[0]['status'],'approved')
        self.assertEqual(self.request('/api/card',{'id':self.ident,'rules':'保留吊带'})[0],400)
        self.assertFalse((self.root/'cards').exists())
        self.assertNotIn('human',self.ws.store.get('sample.png'))
        self.assertEqual(self.generate()['skipped'],1)
        self.assertEqual(digest(self.image),self.before)
    def test_image_revision_and_conflict_guard(self):
        self.generate();row=self.ws.store.get('sample.png')
        self.assertEqual(self.request(f'/api/image?id={self.ident}&kind=result&revision={row["revision"]}')[0],200)
        self.assertEqual(self.request(f'/api/image?id={self.ident}&kind=result&revision=999')[0],400)
        target=self.root/'out/白底图/sample.jpg';target.write_bytes(b'keep')
        self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],200)
        self.assertTrue((target.parent/'sample_2.jpg').exists())
        self.assertEqual(target.read_bytes(),b'keep')
    def test_busy_guard_and_original_change(self):
        self.ws.job['busy']=True
        for route,data in [('/api/open',{}),('/api/card',{'id':self.ident}),('/api/start',{}),('/api/decide',{'id':self.ident})]:
            self.assertEqual(self.request(route,data)[0],400)
        self.ws.job['busy']=False
        self.generate();row=self.ws.store.get('sample.png')
        Image.new('RGB',(80,60),'blue').save(self.image)
        self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],400)
    def test_retry_preserves_context_and_clears_approval(self):
        self.generate();row=self.ws.store.get('sample.png')
        row=self.ws.store.decide('sample.png',row['revision'],'approved','ok')
        export=self.root/'out'/row['export']
        def remove(image,*args):
            result=image.copy();result.putalpha(Image.new('L',image.size,255));return result
        with patch('core.make_session',lambda _:object()),patch('core.remove_background',remove):
            retried=web.retry(self.ws.store,'sample.png',row['revision'],'isnet-general-use')
        self.assertEqual(retried['status'],'pending');self.assertFalse(export.exists())
        self.assertNotIn('task_context',retried)
        self.assertEqual(retried['options']['model'],'isnet-general-use')
        with self.assertRaises(ValueError):web.retry(self.ws.store,'sample.png',row['revision'],'u2net')
        self.assertEqual(digest(self.image),self.before)

    def test_web_paint_history_export_and_stale_revision(self):
        self.generate();row=self.ws.store.get('sample.png')
        row=self.ws.store.decide('sample.png',row['revision'],'approved','ok')
        exported=self.root/'out'/row['export']
        payload={'id':self.ident,'revision':row['revision'],'action':'draw','mode':'erase','shape':'brush','radius':3,'points':[[20,20],[25,20]]}
        code,value=self.request('/api/edit',payload);self.assertEqual(code,200)
        self.assertEqual(value['row']['status'],'editing');self.assertFalse(exported.exists())
        self.assertEqual(self.ws.store.load_mask(value['row']).getpixel((22,20)),0)
        self.assertEqual(self.ws.store.load_mask(value['row']).getpixel((60,40)),255)
        self.assertEqual(self.request('/api/edit',payload)[0],400)
        for action,expected in [('undo',255),('redo',0),('reset',255)]:
            code,value=self.request('/api/edit',{'id':self.ident,'revision':value['row']['revision'],'action':action})
            self.assertEqual(code,200);self.assertEqual(self.ws.store.load_mask(value['row']).getpixel((22,20)),expected)
        row=value['row']
        self.assertEqual(self.request('/api/decide',{'id':self.ident,'revision':row['revision'],'decision':'approved'})[0],200)
        self.assertEqual(digest(self.image),self.before)

    def test_web_selection_tools_and_guards(self):
        self.generate()
        for shape,points,pixel in [('rectangle',[[2,2],[8,8]],(5,5)),('lasso',[[30,2],[40,2],[35,12]],(35,5)),('polygon',[[50,20],[60,20],[55,30]],(55,23))]:
            row=self.ws.store.get('sample.png')
            code,value=self.request('/api/edit',{'id':self.ident,'revision':row['revision'],'action':'draw','mode':'erase','shape':shape,'points':points})
            self.assertEqual(code,200);self.assertEqual(self.ws.store.load_mask(value['row']).getpixel(pixel),0)
        row=value['row'];payload={'id':self.ident,'revision':row['revision'],'action':'draw','mode':'restore','shape':'brush','points':[[55,23]],'radius':2}
        code,value=self.request('/api/edit',payload);self.assertEqual(code,200)
        self.assertEqual(self.ws.store.load_mask(value['row']).getpixel((55,23)),255)
        payload['revision']=value['row']['revision'];payload['points']=[[-1,20]]
        self.assertEqual(self.request('/api/edit',payload)[0],400)
        payload['points']=[[20,20]];self.ws.job['busy']=True
        self.assertEqual(self.request('/api/edit',payload)[0],200);self.ws.job['busy']=False
        Image.new('RGB',(80,60),'blue').save(self.image)
        self.assertEqual(self.request('/api/edit',payload)[0],400)

    def test_no_classification_gate_and_explicit_restart_confirmation(self):
        cards=TaskCards(self.source);item=cards.item(self.image)
        item.update(category='包袋',category_confirmed=True);cards.save_item(self.image,item)
        _,state=self.request('/api/state?category='+quote('待确认'))
        self.assertEqual(state['filtered'],1);self.assertNotIn('groups',state)
        self.assertEqual(self.request('/api/classify',{'id':self.ident})[0],400)
        with patch.object(self.ws,'start') as start:
            self.assertEqual(self.request('/api/start',{})[0],200)
            self.assertEqual(start.call_args.args[0]['mode'],'resume')
            self.assertEqual(self.request('/api/start',{'mode':'restart_all'})[0],400)
            self.assertEqual(self.request('/api/start',{'mode':'restart_all','confirmed':True,'model':'isnet-general-use'})[0],200)
            self.assertEqual(start.call_args.args[0]['mode'],'restart_all')

    def test_shared_destination_and_navigation(self):
        self.generate();old=self.ws.store
        other=self.root/'second';other.mkdir();Image.new('RGB',(80,60),'blue').save(other/'sample.png')
        self.assertEqual(self.request('/api/open',{'source':str(other),'destination':str(self.root/'out')})[0],200)
        self.assertEqual(self.ws.store.entries(),[])
        self.assertEqual(old.get('sample.png')['status'],'pending')
        self.assertEqual(self.request('/api/open',{'source':str(self.source),'destination':str(self.root/'out')})[0],200)
        ident=next(iter(self.ws.paths))
        _,state=self.request('/api/state?selected='+ident)
        self.assertEqual(state['navigation'],{'prev':None,'next':None,'position':1,'next_review':None})
        _,item=self.request('/api/item?id='+ident);self.assertEqual(item['name'],'sample.png')

    def test_review_target_survives_approval_sort_and_filter_changes(self):
        for name in ('a','b','c'):
            Image.new('RGB',(80,60),'red').save(self.source/f'{name}.png')
        self.generate()
        self.ws.open({'source':str(self.source),'destination':str(self.root/'out')})
        ids={p.name:ident for ident,p in self.ws.paths.items()}
        for name,status in [('b.png','redo'),('c.png','approved')]:
            row=self.ws.store.get(name)
            self.ws.store.decide(name,row['revision'],status)
        _,state=self.request('/api/state?selected='+ids['sample.png'])
        self.assertEqual(state['navigation']['next_review'],ids['a.png'])
        _,state=self.request('/api/state?filter=pending&selected='+ids['a.png'])
        target=state['navigation']['next_review']
        self.assertEqual(target,ids['sample.png'])
        row=self.ws.store.get('a.png')
        self.assertEqual(self.request('/api/decide',{'id':ids['a.png'],'revision':row['revision'],'decision':'approved'})[0],200)
        _,state=self.request('/api/state?filter=pending&selected='+target)
        self.assertEqual(state['navigation']['position'],1)
        self.assertIsNone(state['navigation']['next_review'])
        self.assertEqual([v['id'] for v in state['items']],[target])

    def test_page_limit_and_nested_paths(self):
        self.assertEqual(self.request('/api/open',{'source':str(self.source),'destination':str(self.source/'nested')})[0],400)
        for i in range(45):Image.new('RGB',(2,2)).save(self.source/f'{i}.png')
        self.ws.open({'source':str(self.source),'destination':str(self.root/'out')})
        _,v=self.request('/api/state');self.assertEqual(len(v['items']),40);self.assertEqual(v['filtered'],46)
        last=v['items'][-1]['id']
        _,v=self.request('/api/state?offset=40');self.assertEqual(len(v['items']),6)
        next_page=v['items'][0]['id']
        _,v=self.request('/api/state?selected='+last)
        self.assertEqual(v['navigation']['next'],next_page)
        _,v=self.request('/api/state?offset=40&selected='+next_page)
        self.assertEqual(v['navigation']['prev'],last)
        self.assertEqual(v['navigation']['position'],41)


class WorkerTests(unittest.TestCase):
    def test_stop_finishes_current_image_then_resume_skips_it(self):
        import signal
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            for name in ('a.png','b.png'):Image.new('RGB',(32,24),'red').save(source/name)
            callbacks={}
            real_run=run_batch
            def remove(im,session,options):
                callbacks[signal.SIGTERM](None,None)
                result=im.copy();result.putalpha(Image.new('L',im.size,255));return result
            def run(*args,**kwargs):
                return real_run(*args,**kwargs,session_factory=lambda _:object(),remover=remove)
            with patch.dict('os.environ',{'WHITEBG_DATA_HOME':str(root/'runtime')}),patch('task_cards.DATA',root/'cards'),patch('workflow.run_batch',run),patch('signal.signal',lambda signum,fn:callbacks.update({signum:fn})),redirect_stdout(io.StringIO()):
                web.worker({'action':'batch','source':str(source),'destination':str(root/'out'),'options':vars(Options(model='u2net'))})
                store=BatchStore(root/'out');self.assertEqual(len(store.entries()),1)
                def complete(im,*_):result=im.copy();result.putalpha(Image.new('L',im.size,255));return result
                stats=real_run(source,root/'out',Options(model='u2net'),session_factory=lambda _:object(),remover=complete)
                self.assertEqual(stats['skipped'],1);self.assertEqual(stats['success'],1)

class RestartWorkerTests(unittest.TestCase):
    def test_force_model_clears_overrides_crop_and_resume_only_remaining(self):
        import signal
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();dest=root/'out'
            for name in ('a.png','b.png'):Image.new('RGB',(32,24),'red').save(source/name)
            seen=[];callbacks={};stop=[False]
            def remove(im,session,options):
                seen.append(options.model)
                if stop[0]:callbacks[signal.SIGTERM](None,None)
                result=im.copy();result.putalpha(Image.new('L',im.size,255));return result
            def run(*args,**kwargs):return run_batch(*args,**kwargs,session_factory=lambda _:object(),remover=remove)
            with patch.dict('os.environ',{'WHITEBG_DATA_HOME':str(root/'runtime')}),patch('task_cards.DATA',root/'cards'),patch('workflow.run_batch',run),patch('signal.signal',lambda signum,fn:callbacks.update({signum:fn})),redirect_stdout(io.StringIO()):
                cards=TaskCards(source);item=cards.item(source/'a.png');item.update(model='birefnet-general',requirements='保留细带');cards.save_item(source/'a.png',item)
                request={'action':'batch','source':str(source),'destination':str(dest),'options':vars(Options(model='u2net'))}
                web.worker(request);store=BatchStore(dest,source);row=store.get('a.png')
                row=store.save_crop('a.png',row['revision'],[2,2,20,20])
                row=store.save_geometry('a.png',row['revision'],{'turns':1})
                row=store.save_crop('a.png',row['revision'],[1,1,17,17],space='adjusted')
                row=store.decide('a.png',row['revision'],'approved','ok')
                oldexport=dest/row['export'];seen.clear()
                # Ordinary continue must not silently clear approved edits when the UI model changes.
                approved_revision=row['revision']
                web.worker({**request,'options':vars(Options(model='isnet-general-use'))})
                self.assertEqual(seen,[]);self.assertEqual(store.get('a.png')['revision'],approved_revision)
                self.assertTrue(oldexport.exists());stop[0]=True
                web.worker({**request,'mode':'restart_all'})
                self.assertEqual(seen,['u2net']);self.assertFalse(oldexport.exists())
                row=store.get('a.png');self.assertEqual(row['status'],'pending');self.assertFalse(row.get('crop'))
                self.assertNotIn('output_crop',row);self.assertNotIn('geometry',row)
                self.assertNotIn('task_context',row);self.assertEqual(TaskCards(source).item(source/'a.png')['model'],'birefnet-general')
                self.assertEqual(store.restart_state()['pending'],['b.png'])
                seen.clear();stop[0]=False;revision=row['revision'];web.worker(request)
                self.assertEqual(seen,['u2net']);self.assertEqual(store.get('a.png')['revision'],revision);self.assertIsNone(store.restart_state())

class ScopedRestartTests(unittest.TestCase):
    def test_unapproved_restart_preserves_approved_export_crop_and_resume_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();dest=root/'out'
            for name in ('a.png','b.png','c.png'):Image.new('RGB',(32,24),'red').save(source/name)
            cancel=threading.Event();seen=[]
            def remove(im,session,opt):
                seen.append(opt.model)
                result=im.copy();result.putalpha(Image.new('L',im.size,255));return result
            def run(options,**kwargs):return run_batch(source,dest,options,session_factory=lambda _:object(),remover=remove,**kwargs)
            with patch.dict('os.environ',{'WHITEBG_DATA_HOME':str(root/'runtime')}):
                run(Options(model='u2net'));store=BatchStore(dest,source);row=store.get('a.png')
                row=store.save_crop('a.png',row['revision'],[1,1,20,20]);approved=store.decide('a.png',row['revision'],'approved','')
                output=dest/approved['export'];checksum=digest(output);originals={p.name:digest(p) for p in source.iterdir()}
                seen.clear()
                def stop(event,value):
                    if event=='progress':cancel.set()
                stats=run(Options(model='isnet-general-use'),restart=True,restart_scope='unapproved',cancel=cancel,emit=stop)
                self.assertEqual(stats['total'],2);self.assertEqual(seen,['isnet-general-use'])
                self.assertEqual(store.get('a.png'),approved);self.assertEqual(digest(output),checksum)
                self.assertEqual(store.restart_state()['scope'],'unapproved');self.assertEqual(store.restart_state()['pending'],['c.png'])
                revision=store.get('b.png')['revision'];seen.clear()
                # Resume uses the checkpoint model and scope, regardless of current controls.
                run(Options(model='u2net'),continue_existing=True)
                self.assertEqual(seen,['isnet-general-use']);self.assertEqual(store.get('b.png')['revision'],revision)
                self.assertIsNone(store.restart_state());self.assertEqual(store.get('a.png'),approved)
                self.assertEqual({p.name:digest(p) for p in source.iterdir()},originals)
                # A deliberate all-images restart still replaces the approved candidate.
                seen.clear();run(Options(model='u2net'),restart=True)
                self.assertEqual(len(seen),3);self.assertEqual(store.get('a.png')['status'],'pending');self.assertFalse(output.exists())

    def test_failed_restart_stays_pending_and_empty_scope_leaves_export_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir();Image.new('RGB',(20,20),'red').save(source/'a.png')
            def good(im,*_):result=im.copy();result.putalpha(Image.new('L',im.size,255));return result
            with patch.dict('os.environ',{'WHITEBG_DATA_HOME':str(root/'runtime')}):
                def run(**kwargs):return run_batch(source,root/'out',Options(model='u2net'),session_factory=lambda _:object(),**kwargs)
                run(remover=good)
                with patch('workflow.inspect_cutout',side_effect=ValueError('temporary failure')):
                    stats=run(remover=good,restart=True,restart_scope='unapproved')
                store=BatchStore(root/'out',source);self.assertEqual(stats['failed'],1)
                self.assertEqual(store.restart_state()['pending'],['a.png'])
                run(remover=good);row=store.get('a.png');row=store.decide('a.png',row['revision'],'approved','')
                output=root/'out'/row['export'];checksum=digest(output)
                with self.assertRaisesRegex(ValueError,'没有需要重做'):run(remover=good,restart=True,restart_scope='unapproved')
                self.assertEqual(store.get('a.png'),row);self.assertEqual(digest(output),checksum);self.assertIsNone(store.restart_state())

if __name__=='__main__':unittest.main()
