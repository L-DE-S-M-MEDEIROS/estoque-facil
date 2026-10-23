import gc
from types import SimpleNamespace
import unittest
import weakref
from unittest.mock import Mock, patch

import app
from test_sync_ui import SyncAppHarness
from premium_widgets import SmoothScrollableFrame


class GuiLifetimeTests(unittest.TestCase):
    def test_gui_cycles_are_collected_on_the_event_loop(self):
        initial=gc.isenabled()
        states=[]
        class Window:
            def __init__(self):
                states.append(gc.isenabled())
                self.callback=None
            def after(self,delay,callback):
                self.callback=callback
            def mainloop(self):
                states.append(gc.isenabled())
                self.callback()
        with patch.object(app,'EstoqueApp',Window):
            app.run_desktop_app()
        self.assertEqual(states,[False,False])
        self.assertEqual(gc.isenabled(),initial)

    def test_gc_state_is_restored_when_initialization_fails(self):
        initial=gc.isenabled()
        with patch.object(app,'EstoqueApp',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):app.run_desktop_app()
        self.assertEqual(gc.isenabled(),initial)

    def test_pending_cloud_worker_does_not_retain_the_window(self):
        gui=SyncAppHarness(SimpleNamespace(path='unused',db=Mock()))
        targets=[]
        with patch.object(app,'has_pending_sync',return_value=False), patch.object(app.threading,'Thread',side_effect=lambda **kw:SimpleNamespace(start=lambda:targets.append(kw['target']))):
            gui.start_cloud_sync()
        reference=weakref.ref(gui)
        del gui
        gc.collect()
        self.assertIsNone(reference(),'network thread retained the GUI')
        self.assertEqual(len(targets),1)

    def test_scroll_stops_when_canvas_rounding_prevents_progress(self):
        frame=SimpleNamespace(_smooth_scroll_job='old',_scroll_axis='y',_scroll_target_pixels=10.0,
            winfo_exists=lambda:True,_scroll_metrics=lambda _axis:(9.0,100.0,200.0),
            _parent_canvas=SimpleNamespace(yview_moveto=Mock()),after=Mock())
        SmoothScrollableFrame._animate_scroll(frame)
        self.assertIsNone(frame._scroll_target_pixels)
        frame.after.assert_not_called()


if __name__=='__main__':unittest.main()
