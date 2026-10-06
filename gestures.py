"""Mac app-local NSEvent magnify adapter; no global input monitoring."""
import math
import sys


def pinch_factor(amount):
    return math.exp(max(-math.log(1.06),min(math.log(1.06),amount)))


class PinchBridge:
    def __init__(self,window,canvases,callback):
        self.window=window;self.canvases=canvases;self.callback=callback
        self.monitor=None;self.job=None;self.amount=0.;self.anchor=None;self.closed=False
        self.status='双指捏合：仅Mac支持；⌘+滚动也可缩放。'
        window.bind('<Destroy>',self.destroy,add='+')
        if sys.platform!='darwin':return
        try:
            from AppKit import NSEvent,NSEventMaskMagnify
            self.NSEvent=NSEvent
            self.handler=self.handle
            self.monitor=NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskMagnify,self.handler)
            if self.monitor is None:raise RuntimeError('无法建立本应用手势监听')
            self.status='双指捏合已启用：张开放大、合拢缩小；双指滚动仍用于平移。'
        except Exception as error:
            self.status='双指捏合未启用：请运行 升级组件.command；暂用⌘+滚动。'
            print('Mac捏合组件未启用：'+str(error),flush=True)
    def handle(self,event):
        if self.closed:return event
        try:
            px,py=self.window.winfo_pointerxy()
            canvas=self.window.winfo_containing(px,py)
            if canvas not in self.canvases:return event
            delta=float(event.magnification())
            if not math.isfinite(delta):return event
            # Native values are relative changes, not scroll deltas.
            self.amount+=math.log(max(.1,1+delta))
            self.anchor=(canvas,px-canvas.winfo_rootx(),py-canvas.winfo_rooty())
            if self.job is None:self.job=self.window.after(40,self.flush)
            return None
        except Exception as error:
            print('捏合事件未处理：'+str(error),flush=True)
            return event
    def flush(self):
        self.job=None;amount=self.amount;self.amount=0
        if not self.closed and self.anchor:
            self.callback(pinch_factor(amount),*self.anchor)
    def destroy(self,event):
        if event.widget is not self.window:return
        self.closed=True
        if self.job is not None:
            self.window.after_cancel(self.job);self.job=None
        if self.monitor is not None:
            self.NSEvent.removeMonitor_(self.monitor);self.monitor=None
