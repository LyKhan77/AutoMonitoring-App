import os
import time
import threading
import base64
from typing import Optional, Tuple, Dict, Any

import cv2

# Reuse the existing Flask app, routes, and Socket.IO instance
from app import app, socketio, load_cameras
from flask_socketio import emit

# ---- Parameters ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARAM_PATH = os.path.join(BASE_DIR, 'config', 'parameter_config.json')


def _load_param() -> Dict[str, Any]:
    import json
    try:
        with open(PARAM_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {
            'fps_target': 15,
            'stream_max_width': 720,
            'jpeg_quality': 60,
            'annotation_stride': 3,
        }


def _resize_keep_w(frame, max_w: int):
    if not isinstance(max_w, int) or max_w <= 0:
        return frame
    h, w = frame.shape[:2]
    if w <= max_w:
        return frame
    scale = max_w / float(w)
    nh = max(1, int(h * scale))
    return cv2.resize(frame, (max_w, nh), interpolation=cv2.INTER_AREA)


class RTSPFrameSource:
    """
    Simple source that reads frames from an RTSP URL or a numeric webcam index.
    Uses OpenCV VideoCapture for both RTSP and webcam.
    """
    def __init__(self, url: str):
        self.url = url
        self._cap = None

    def open(self):
        # Numeric webcam source (e.g., "0", "1") => use integer index
        src = self.url
        try:
            s = str(src).strip()
            if s.isdigit():
                src = int(s)
        except Exception:
            pass
        self._cap = cv2.VideoCapture(src)
        if not self._cap or not self._cap.isOpened():
            raise RuntimeError('Failed to open video source')

    def read(self):
        if not self._cap:
            return None
        ok, frame = self._cap.read()
        if not ok:
            return None
        return frame

    def close(self):
        try:
            if self._cap:
                self._cap.release()
        except Exception:
            pass
        self._cap = None


class StreamWorker:
    """Raw streaming worker: reads frames from source, rescales and emits JPEGs to client."""
    def __init__(self, sid: str, cam_id: int, url: str, emit_cb, params: Optional[Dict[str, Any]] = None):
        self.sid = sid
        self.cam_id = cam_id
        self.url = url
        self.emit_cb = emit_cb  # function(event_name, payload)
        self.params = params or _load_param()
        self._thr: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        fps_target = float(self.params.get('fps_target', 15))
        stream_max_width = int(self.params.get('stream_max_width', 720))
        jpeg_quality = int(self.params.get('jpeg_quality', 60))
        stride = max(1, int(self.params.get('annotation_stride', 3)))

        src = RTSPFrameSource(self.url)
        try:
            src.open()
        except Exception as e:
            self.emit_cb('stream_error', {'message': f'open_error: {e}'})
            return

        try:
            last_emit = 0.0
            frame_idx = 0
            period = 1.0 / max(1.0, fps_target)
            while not self._stop.is_set():
                frame = src.read()
                if frame is None:
                    time.sleep(0.01)
                    continue
                frame_idx += 1
                if (frame_idx % stride) != 0:
                    continue
                frame = _resize_keep_w(frame, stream_max_width)
                now = time.time()
                if now - last_emit < period:
                    continue
                last_emit = now
                ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
                if not ok:
                    continue
                b64 = base64.b64encode(buf.tobytes()).decode('ascii')
                self.emit_cb('frame', {'cam_id': self.cam_id, 'image': b64})
        except Exception as e:
            self.emit_cb('stream_error', {'message': f'rtsp_error: {e}'})
        finally:
            src.close()
            self.emit_cb('stream_stopped', {'cam_id': self.cam_id})


# --- Socket handlers (raw RTSP streaming) ---
_workers_by_sid: Dict[str, StreamWorker] = {}


def _get_cam_url(cam_id: int) -> Tuple[str, bool]:
    cams = load_cameras()
    cam = cams.get(int(cam_id)) if cams else None
    if not cam:
        raise RuntimeError('camera_not_found')
    url = (cam.get('rtsp_url') or '').strip()
    if not url:
        raise RuntimeError('rtsp_url_empty')
    stream_enabled = bool(cam.get('stream_enabled', True))
    return url, stream_enabled


@socketio.on('start_stream')
def on_start_stream(payload):
    try:
        cam_id = int(payload.get('cam_id'))
    except Exception:
        emit('stream_error', {'message': 'invalid_cam_id'})
        return
    try:
        from flask import request
        sid = request.sid
    except Exception:
        sid = None

    # Stop any previous worker for this sid
    old = _workers_by_sid.pop(sid, None)
    if old:
        try:
            old.stop()
        except Exception:
            pass

    # Enforce stream_enabled and fetch URL
    try:
        url, enabled = _get_cam_url(cam_id)
        if not enabled:
            emit('stream_stopped', {'cam_id': cam_id})
            return
    except Exception as e:
        emit('stream_error', {'message': str(e)})
        return

    def _emit(event_name: str, data: Dict[str, Any]):
        socketio.emit(event_name, data, to=sid)

    w = StreamWorker(sid=sid or '', cam_id=cam_id, url=url, emit_cb=_emit)
    _workers_by_sid[sid] = w
    w.start()


@socketio.on('stop_stream')
def on_stop_stream(payload=None):
    try:
        from flask import request
        sid = request.sid
    except Exception:
        sid = None
    w = _workers_by_sid.pop(sid, None)
    if w:
        try:
            w.stop()
        except Exception:
            pass
        emit('stream_stopped', {'cam_id': getattr(w, 'cam_id', None)})


if __name__ == '__main__':
    # Run the RTSP-enabled app
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '5000')))
