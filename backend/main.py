import asyncio
import base64
from contextlib import asynccontextmanager
import ctypes
import io
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set
import webbrowser

import cv2
import httpx
try:
    import pyttsx3
except Exception:
    pyttsx3 = None
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse, HTMLResponse, StreamingResponse
import mediapipe as mp
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
import numpy as np
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Controller as MouseController, Button

# Air-OS Cognitive Subsystem & Self-Improvement Engine
from dream_engine import memory_store, prompt_manager, dream_engine, dream_watchdog
from mobile_bridge import mobile_bridge, mobile_router

# 1. Enable Process DPI Awareness & Multi-thread OpenCV
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

cv2.setNumThreads(4)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("air-os-backend")


# 2. Attach thread to Windows interactive "default" desktop
def ensure_interactive_desktop():
    try:
        user32 = ctypes.windll.user32
        hdesk = user32.OpenDesktopW("default", 0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception as e:
        logger.warning(f"Could not attach to default desktop: {e}")


ensure_interactive_desktop()

# Screen metrics and Input Controllers
mouse = MouseController()
keyboard = KeyboardController()


def get_screen_resolution():
    try:
        ensure_interactive_desktop()
        user32 = ctypes.windll.user32
        user32.SetProcessDPIAware()
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        if w > 0 and h > 0:
            return w, h
    except Exception as e:
        logger.warning(f"Error fetching screen resolution: {e}")
    return 1920, 1080


# MediaPipe 21 Hand Landmark Connections
HAND_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),        # Thumb
    (0, 5), (5, 6), (6, 7), (7, 8),        # Index
    (9, 10), (10, 11), (11, 12),           # Middle
    (13, 14), (14, 15), (15, 16),          # Ring
    (0, 17), (17, 18), (18, 19), (19, 20), # Pinky
    (5, 9), (9, 13), (13, 17),             # Palm Base
]

# Custom Gestures Persistence
GESTURES_FILE = os.path.join(os.path.dirname(__file__), "custom_gestures.json")


def load_custom_gestures() -> List[dict]:
    if os.path.exists(GESTURES_FILE):
        try:
            with open(GESTURES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading {GESTURES_FILE}: {e}")
    return []


def save_custom_gestures(gestures: List[dict]):
    try:
        with open(GESTURES_FILE, "w", encoding="utf-8") as f:
            json.dump(gestures, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {GESTURES_FILE}: {e}")


# Custom Configuration Persistence (User-Configured Combos & Bindings)
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "combos": {
        "portal_magic_circle": {
            "enabled": True,
            "name": "Portal Magic Circle",
            "action_type": "url",
            "action_payload": "https://www.google.com",
        },
        "x_shield": {
            "enabled": True,
            "name": "X-Shield Barrier",
            "action_type": "hotkey",
            "action_payload": "SHOW_DESKTOP",
        },
    },
    "bindings": {
        "thumb_index_pinch": "left_click",
        "thumb_index_hold": "drag_hold",
        "thumb_middle_pinch": "right_click",
        "thumb_ring_pinch": "scroll",
    },
}


def load_app_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                cfg = DEFAULT_CONFIG.copy()
                if "combos" in data:
                    cfg["combos"].update(data["combos"])
                if "bindings" in data:
                    cfg["bindings"].update(data["bindings"])
                return cfg
        except Exception as e:
            logger.error(f"Error loading {CONFIG_FILE}: {e}")
    return DEFAULT_CONFIG.copy()


def save_app_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving {CONFIG_FILE}: {e}")


# Helper: Euclidean distance between two landmarks
def dist_2d(p1, p2):
    return math.hypot(p1.x - p2.x, p1.y - p2.y)


# Helper: Extract 70-element normalized pose vector from 21 hand landmarks
def extract_pose_vector(landmarks, mirror_x: bool = False) -> np.ndarray:
    w = landmarks[0]
    scale = math.hypot(landmarks[9].x - w.x, landmarks[9].y - w.y, landmarks[9].z - w.z)
    if scale < 0.01:
        scale = 0.01

    vec = []
    x_mult = -1.0 if mirror_x else 1.0
    for i in range(1, 21):
        vec.extend([
            (landmarks[i].x - w.x) * x_mult / scale,
            (landmarks[i].y - w.y) / scale,
            (landmarks[i].z - w.z) / scale,
        ])

    tips = [4, 8, 12, 16, 20]
    knuckles = [2, 5, 9, 13, 17]
    for t in tips:
        vec.append(math.hypot(landmarks[t].x - w.x, landmarks[t].y - w.y, landmarks[t].z - w.z) / scale)
    for t, k in zip(tips, knuckles):
        vec.append(math.hypot(landmarks[t].x - landmarks[k].x, landmarks[t].y - landmarks[k].y, landmarks[t].z - landmarks[k].z) / scale)

    arr = np.array(vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 1e-6:
        arr /= norm
    return arr


# Global Tracker State
class NativeTrackerState:
    def __init__(self):
        self.is_armed: bool = False
        self.camera_enabled: bool = True  # Camera Privacy & Hardware Toggle
        self.fps: float = 0.0
        self.gesture: str = "IDLE"
        self.hand_detected: bool = False
        self.hands_count: int = 0
        self.cursor_x: float = 0.5
        self.cursor_y: float = 0.5
        self.screen_w, self.screen_h = get_screen_resolution()
        self.latest_jpeg: bytes = b""
        self.latest_clean_jpeg: bytes = b""
        self.active_websockets: Set[WebSocket] = set()
        self.lock = threading.Lock()
        self.running: bool = False

        # Configurable Combos & Bindings
        self.config: dict = load_app_config()

        # Custom Gestures & Learning
        self.custom_gestures: List[dict] = load_custom_gestures()
        self.recording_target: Optional[dict] = None
        self.recording_frames_left: int = 0
        self.needs_save_gestures: bool = False
        self.last_save_time: float = 0.0

        # Combos & Macros Tracking
        self.active_combo: str = "NONE"
        self.active_macro: str = "NONE"
        self.macro_flash_until: float = 0.0
        self.last_combo_time: Dict[str, float] = {"portal": 0.0, "x_shield": 0.0}
        self.event_queue: List[dict] = []


state = NativeTrackerState()


def trigger_macro_action(action_type: str, action_payload: str):
    if not action_payload or action_type == "none":
        return
    try:
        if action_type == "url":
            url = action_payload
            if not url.startswith("http://") and not url.startswith("https://"):
                url = "https://" + url
            webbrowser.open(url)
            logger.info(f"[ACTION EXEC] Opened URL: {url}")
        elif action_type == "shell":
            subprocess.Popen(action_payload, shell=True)
            logger.info(f"[ACTION EXEC] Spawned shell process: {action_payload}")
        elif action_type == "hotkey":
            cmd = action_payload.upper()
            if cmd == "VOLUME_UP":
                keyboard.tap(Key.media_volume_up)
            elif cmd == "VOLUME_DOWN":
                keyboard.tap(Key.media_volume_down)
            elif cmd == "MUTE":
                keyboard.tap(Key.media_volume_mute)
            elif cmd == "PLAY_PAUSE":
                keyboard.tap(Key.media_play_pause)
            elif cmd == "TASK_VIEW":
                with keyboard.pressed(Key.cmd):
                    keyboard.tap(Key.tab)
            elif cmd == "SHOW_DESKTOP":
                with keyboard.pressed(Key.cmd):
                    keyboard.tap('d')
            elif cmd == "LOCK_PC":
                ctypes.windll.user32.LockWorkStation()
            elif cmd == "CLOSE_WINDOW":
                with keyboard.pressed(Key.alt):
                    keyboard.tap(Key.f4)
            logger.info(f"[ACTION EXEC] Fired hotkey: {cmd}")
    except Exception as e:
        logger.warning(f"Error triggering action {action_type} ({action_payload}): {e}")


# Helper: Open camera via DirectShow
def open_webcam_directshow():
    c = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    c.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    c.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
    c.set(cv2.CAP_PROP_FPS, 60)
    c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return c


# Native Vision & Hand Tracking Background Thread
def native_vision_worker():
    ensure_interactive_desktop()
    user32 = ctypes.windll.user32
    logger.info(">>> Starting Native Vision & OS Mouse Control Thread...")

    # Locate hand landmarker model asset
    model_path = os.path.join(os.path.dirname(__file__), "hand_landmarker.task")
    if not os.path.exists(model_path):
        logger.error(f"MediaPipe model asset not found at {model_path}!")
        return

    base_options = python.BaseOptions(model_asset_path=model_path)
    options = vision.HandLandmarkerOptions(
        base_options=base_options,
        running_mode=vision.RunningMode.VIDEO,
        num_hands=2,
        min_hand_detection_confidence=0.55,
        min_hand_presence_confidence=0.55,
        min_tracking_confidence=0.55,
    )
    detector = vision.HandLandmarker.create_from_options(options)

    # Webcam handle (dynamically opened/closed when camera is toggled)
    cap = None
    if state.camera_enabled:
        cap = open_webcam_directshow()
        if cap.isOpened():
            logger.info("Webcam DirectShow stream initialized at 640x360 @ 60 FPS (VIDEO mode, 2 Hands)")
        else:
            logger.error("Failed to open webcam with DirectShow!")
            cap = None

    # Tracking & Smoothing state (for primary cursor hand)
    prev_raw_x, prev_raw_y = 0.5, 0.5
    smooth_x, smooth_y = 0.5, 0.5
    last_cursor_x, last_cursor_y = -1, -1
    last_alpha = 0.3

    # Gesture state timers & hysteresis flags
    is_thumb_index_pinching = False
    is_thumb_middle_pinching = False
    is_thumb_ring_pinching = False
    is_drag_down = False
    pinch_start_time = None
    last_right_click_time = 0.0
    prev_scroll_y = 0.5
    left_click_flash_until = 0.0
    right_click_flash_until = 0.0
    missing_hand_frames = 0
    last_timestamp_ms = 0

    # Telemetry and FPS calculation
    last_telemetry_time = 0.0
    frame_count = 0
    fps_start_time = time.perf_counter()
    measured_fps = 0.0

    while state.running:
        # =====================================================================
        # CAMERA DISABLED / PRIVACY MODE HANDLING
        # =====================================================================
        if not state.camera_enabled:
            # 1. Release hardware camera so physical light turns completely OFF!
            if cap is not None and cap.isOpened():
                cap.release()
                cap = None
                logger.info("[CAMERA PRIVACY] Webcam released. Hardware light turned OFF.")

            # 2. Render stylish Privacy Screen for MJPEG stream
            privacy_frame = np.zeros((360, 640, 3), dtype=np.uint8)
            privacy_frame[:] = (15, 23, 42)  # Dark slate

            # Red badge card
            cv2.rectangle(privacy_frame, (110, 100), (530, 260), (30, 41, 59), -1)
            cv2.rectangle(privacy_frame, (110, 100), (530, 260), (239, 68, 68), 3)

            # Camera off symbol & text
            cv2.circle(privacy_frame, (320, 150), 22, (239, 68, 68), -1)
            cv2.putText(privacy_frame, "X", (312, 158), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(privacy_frame, "CAMERA IS TURNED OFF", (195, 205), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(privacy_frame, "PRIVACY MODE ACTIVE | HARDWARE LIGHT OFF", (150, 235), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (148, 163, 184), 1, cv2.LINE_AA)

            _, jpeg = cv2.imencode(".jpg", privacy_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
            with state.lock:
                state.latest_jpeg = jpeg.tobytes()
                state.hand_detected = False
                state.hands_count = 0
                state.gesture = "CAMERA_OFF"
                state.fps = 0.0

            # Broadcast privacy telemetry (~10Hz)
            now = time.perf_counter()
            if now - last_telemetry_time >= 0.1:
                last_telemetry_time = now
                telemetry_data = json.dumps({
                    "type": "telemetry",
                    "fps": 0.0,
                    "gesture": "CAMERA_OFF",
                    "x": 0.5,
                    "y": 0.5,
                    "is_armed": state.is_armed,
                    "camera_enabled": False,
                    "hand_detected": False,
                    "hands_count": 0,
                    "active_combo": "NONE",
                    "active_macro": "NONE",
                    "screen": {"width": state.screen_w, "height": state.screen_h},
                })
                with state.lock:
                    dead = set()
                    for ws in list(state.active_websockets):
                        try:
                            loop = getattr(ws, "_loop", None)
                            if loop and loop.is_running():
                                asyncio.run_coroutine_threadsafe(ws.send_text(telemetry_data), loop)
                            else:
                                dead.add(ws)
                        except Exception:
                            dead.add(ws)
                    state.active_websockets -= dead

            time.sleep(0.04)
            continue

        # =====================================================================
        # CAMERA ENABLED: Reopen if needed and process frames
        # =====================================================================
        if cap is None or not cap.isOpened():
            cap = open_webcam_directshow()
            if not cap.isOpened():
                time.sleep(0.5)
                continue
            logger.info("[CAMERA] Webcam re-opened and streaming live.")

        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.005)
            continue

        # Flip horizontally for natural mirror behavior
        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape
        now = time.perf_counter()

        # Save clean frame for AI Vision (without HUD overlays)
        if frame_count % 3 == 0:
            _, clean_jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            with state.lock:
                state.latest_clean_jpeg = clean_jpg.tobytes()

        # Measure FPS
        frame_count += 1
        if now - fps_start_time >= 1.0:
            measured_fps = frame_count / (now - fps_start_time)
            frame_count = 0
            fps_start_time = now
            state.fps = measured_fps

        # Optimized Downscale for MediaPipe Inference
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        infer_w, infer_h = 480, 270
        rgb_resized = cv2.resize(rgb, (infer_w, infer_h), interpolation=cv2.INTER_LINEAR)
        rgb_resized.flags.writeable = False
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_resized)

        timestamp_ms = int(time.time() * 1000)
        if timestamp_ms <= last_timestamp_ms:
            timestamp_ms = last_timestamp_ms + 1
        last_timestamp_ms = timestamp_ms

        detection_result = detector.detect_for_video(mp_image, timestamp_ms)

        num_hands = len(detection_result.hand_landmarks) if detection_result.hand_landmarks else 0
        state.hands_count = num_hands
        hand_detected = num_hands > 0
        evaluated_gesture = "MOVE"

        # Active Control Zone (middle 60%: x 0.2 to 0.8, y 0.2 to 0.8)
        box_x1 = int(w * 0.2)
        box_y1 = int(h * 0.2)
        box_x2 = int(w * 0.8)
        box_y2 = int(h * 0.8)
        box_color = (60, 200, 65) if state.is_armed else (11, 158, 245)

        # Draw Active Zone bounding box and reticle corners
        cv2.rectangle(frame, (box_x1, box_y1), (box_x2, box_y2), box_color, 1)
        corner = 14
        cv2.line(frame, (box_x1, box_y1), (box_x1 + corner, box_y1), box_color, 2)
        cv2.line(frame, (box_x1, box_y1), (box_x1, box_y1 + corner), box_color, 2)
        cv2.line(frame, (box_x2, box_y1), (box_x2 - corner, box_y1), box_color, 2)
        cv2.line(frame, (box_x2, box_y1), (box_x2, box_y1 + corner), box_color, 2)
        cv2.line(frame, (box_x1, box_y2), (box_x1 + corner, box_y2), box_color, 2)
        cv2.line(frame, (box_x1, box_y2), (box_x1, box_y2 - corner), box_color, 2)
        cv2.line(frame, (box_x2, box_y2), (box_x2 - corner, box_y2), box_color, 2)
        cv2.line(frame, (box_x2, box_y2), (box_x2, box_y2 - corner), box_color, 2)
        cv2.putText(
            frame,
            f"ACTIVE ZONE (60%) | HANDS: {num_hands}",
            (box_x1 + 8, box_y1 + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            box_color,
            1,
            cv2.LINE_AA,
        )

        # =====================================================================
        # 1. USER-CONFIGURABLE TWO-HAND COMBO ENGINE
        # =====================================================================
        current_active_combo = "NONE"
        combos_cfg = state.config.get("combos", {})

        if num_hands >= 2:
            h1 = detection_result.hand_landmarks[0]
            h2 = detection_result.hand_landmarks[1]

            # A. "Portal / Magic Circle" (Both Index Fingertips Touch: Landmark 8)
            tip1 = (int(h1[8].x * w), int(h1[8].y * h))
            tip2 = (int(h2[8].x * w), int(h2[8].y * h))
            dist_index_tips = math.hypot(tip1[0] - tip2[0], tip1[1] - tip2[1])

            portal_cfg = combos_cfg.get("portal_magic_circle", {})
            if portal_cfg.get("enabled", True) and dist_index_tips <= 36:
                current_active_combo = "PORTAL_MAGIC_CIRCLE"
                mid_x = (tip1[0] + tip2[0]) // 2
                mid_y = (tip1[1] + tip2[1]) // 2

                pulse = int(math.sin(now * 8) * 4)
                cv2.circle(frame, (mid_x, mid_y), 28 + pulse, (255, 0, 255), 2, cv2.LINE_AA)
                cv2.circle(frame, (mid_x, mid_y), 16, (0, 255, 255), 2, cv2.LINE_AA)
                cv2.line(frame, tip1, tip2, (0, 255, 255), 2, cv2.LINE_AA)
                combo_title = portal_cfg.get("name", "PORTAL MAGIC CIRCLE")
                cv2.putText(
                    frame,
                    combo_title,
                    (mid_x - 70, mid_y - 35),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 255, 255),
                    1,
                    cv2.LINE_AA,
                )

                if now - state.last_combo_time["portal"] > 2.0:
                    state.last_combo_time["portal"] = now
                    act_type = portal_cfg.get("action_type", "url")
                    act_payload = portal_cfg.get("action_payload", "https://www.google.com")
                    state.active_macro = f"{combo_title} ({act_payload})"
                    state.macro_flash_until = now + 2.5
                    trigger_macro_action(act_type, act_payload)
                    with state.lock:
                        state.event_queue.append({
                            "type": "combo_fired",
                            "combo": combo_title,
                            "action": f"{act_type.upper()}: {act_payload}",
                            "timestamp": time.strftime("%H:%M:%S")
                        })

            # B. "X-Shield" (Both Wrists Crossed: Landmark 0)
            w1 = (int(h1[0].x * w), int(h1[0].y * h))
            w2 = (int(h2[0].x * w), int(h2[0].y * h))
            k1 = (int(h1[9].x * w), int(h1[9].y * h))
            k2 = (int(h2[9].x * w), int(h2[9].y * h))
            dist_wrists = math.hypot(w1[0] - w2[0], w1[1] - w2[1])

            shield_cfg = combos_cfg.get("x_shield", {})
            is_crossed = (w1[0] - w2[0]) * (k1[0] - k2[0]) < 0 or dist_wrists < 45
            if shield_cfg.get("enabled", True) and dist_wrists <= 70 and is_crossed:
                current_active_combo = "X_SHIELD"
                shield_cx = (w1[0] + w2[0]) // 2
                shield_cy = (w1[1] + w2[1]) // 2

                cv2.line(frame, w1, k2, (0, 215, 255), 3, cv2.LINE_AA)
                cv2.line(frame, w2, k1, (0, 215, 255), 3, cv2.LINE_AA)
                shield_title = shield_cfg.get("name", "X-SHIELD BARRIER")
                cv2.putText(
                    frame,
                    shield_title,
                    (shield_cx - 85, shield_cy - 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (0, 215, 255),
                    1,
                    cv2.LINE_AA,
                )

                if now - state.last_combo_time["x_shield"] > 2.0:
                    state.last_combo_time["x_shield"] = now
                    act_type = shield_cfg.get("action_type", "hotkey")
                    act_payload = shield_cfg.get("action_payload", "SHOW_DESKTOP")
                    state.active_macro = f"{shield_title} ({act_payload})"
                    state.macro_flash_until = now + 2.5
                    trigger_macro_action(act_type, act_payload)
                    with state.lock:
                        state.event_queue.append({
                            "type": "combo_fired",
                            "combo": shield_title,
                            "action": f"{act_type.upper()}: {act_payload}",
                            "timestamp": time.strftime("%H:%M:%S")
                        })

        state.active_combo = current_active_combo

        # =====================================================================
        # 2. HAND PROCESSING & SKELETON DRAWING (Multi-Hand)
        # =====================================================================
        if hand_detected:
            missing_hand_frames = 0
            hand_colors = [
                ((0, 200, 255), (81, 185, 16)),
                ((255, 105, 180), (220, 50, 240)),
            ]

            for h_idx, lms in enumerate(detection_result.hand_landmarks):
                line_col, accent_col = hand_colors[h_idx % 2]

                for p1_idx, p2_idx in HAND_CONNECTIONS:
                    pt1 = (int(lms[p1_idx].x * w), int(lms[p1_idx].y * h))
                    pt2 = (int(lms[p2_idx].x * w), int(lms[p2_idx].y * h))
                    cv2.line(frame, pt1, pt2, line_col, 2, cv2.LINE_AA)

                for idx, lm in enumerate(lms):
                    px = int(lm.x * w)
                    py = int(lm.y * h)
                    if idx == 5:
                        cv2.circle(frame, (px, py), 6, accent_col, -1, cv2.LINE_AA)
                        cv2.circle(frame, (px, py), 12, accent_col, 1, cv2.LINE_AA)
                    elif idx in (4, 8):
                        cv2.circle(frame, (px, py), 4, (238, 211, 34), -1, cv2.LINE_AA)
                    else:
                        cv2.circle(frame, (px, py), 3, (238, 211, 34), -1, cv2.LINE_AA)

            primary_lms = detection_result.hand_landmarks[0]
            wrist = primary_lms[0]
            thumb_tip = primary_lms[4]
            index_knuckle = primary_lms[5]
            index_tip = primary_lms[8]
            middle_mcp = primary_lms[9]
            middle_tip = primary_lms[12]
            ring_tip = primary_lms[16]
            pinky_tip = primary_lms[20]

            palm_dist = max(0.015, dist_2d(wrist, middle_mcp))

            def norm_d(p1, p2):
                return dist_2d(p1, p2) / palm_dist

            raw_ti_dist = norm_d(thumb_tip, index_tip)
            raw_tm_dist = norm_d(thumb_tip, middle_tip)
            raw_tr_dist = norm_d(thumb_tip, ring_tip)

            is_thumb_index_pinching = (
                raw_ti_dist < 0.32 if is_thumb_index_pinching else raw_ti_dist < 0.25
            )
            is_thumb_middle_pinching = (
                raw_tm_dist < 0.32 if is_thumb_middle_pinching else raw_tm_dist < 0.26
            )
            is_thumb_ring_pinching = (
                raw_tr_dist < 0.32 if is_thumb_ring_pinching else raw_tr_dist < 0.26
            )

            is_index_curled = dist_2d(index_tip, wrist) < dist_2d(primary_lms[6], wrist) or norm_d(index_tip, index_knuckle) < 0.65
            is_middle_curled = dist_2d(middle_tip, wrist) < dist_2d(primary_lms[10], wrist) or norm_d(middle_tip, middle_mcp) < 0.65
            is_ring_curled = dist_2d(ring_tip, wrist) < dist_2d(primary_lms[14], wrist) or norm_d(ring_tip, primary_lms[13]) < 0.65
            is_pinky_curled = dist_2d(pinky_tip, wrist) < dist_2d(primary_lms[18], wrist) or norm_d(pinky_tip, primary_lms[17]) < 0.65
            is_closed_fist = is_index_curled and is_middle_curled and is_ring_curled and is_pinky_curled

            clamped_x = max(0.0, min(1.0, (index_knuckle.x - 0.2) / 0.6))
            clamped_y = max(0.0, min(1.0, (index_knuckle.y - 0.2) / 0.6))

            speed = math.hypot(clamped_x - prev_raw_x, clamped_y - prev_raw_y)
            if speed > 0.0001:
                prev_raw_x, prev_raw_y = clamped_x, clamped_y
                last_alpha = min(0.85, max(0.20, speed * 12))

            alpha = last_alpha
            if not is_closed_fist:
                smooth_x += alpha * (clamped_x - smooth_x)
                smooth_y += alpha * (clamped_y - smooth_y)

            bindings = state.config.get("bindings", {})

            if is_closed_fist:
                evaluated_gesture = "CLUTCH_PAUSE"
                pinch_start_time = None
                if is_drag_down and state.is_armed:
                    mouse.release(Button.left)
                    is_drag_down = False
            elif is_thumb_ring_pinching:
                evaluated_gesture = "SCROLL"
                pinch_start_time = None
                if is_drag_down and state.is_armed:
                    mouse.release(Button.left)
                    is_drag_down = False
                scroll_delta = (smooth_y - prev_scroll_y) * 100
                prev_scroll_y = smooth_y
                if state.is_armed:
                    ring_action = bindings.get("thumb_ring_pinch", "scroll")
                    if ring_action == "scroll":
                        sdy = -int(round(scroll_delta * 0.35))
                        if sdy != 0:
                            mouse.scroll(0, sdy)
                    elif ring_action == "volume_up_down":
                        if scroll_delta > 0.05:
                            keyboard.tap(Key.media_volume_down)
                        elif scroll_delta < -0.05:
                            keyboard.tap(Key.media_volume_up)
            elif is_thumb_middle_pinching:
                pinch_start_time = None
                if is_drag_down and state.is_armed:
                    mouse.release(Button.left)
                    is_drag_down = False
                if now - last_right_click_time > 0.5:
                    last_right_click_time = now
                    right_click_flash_until = now + 0.35
                    if state.is_armed:
                        mid_action = bindings.get("thumb_middle_pinch", "right_click")
                        if mid_action == "right_click":
                            mouse.click(Button.right, 1)
                        elif mid_action == "double_click":
                            mouse.click(Button.left, 2)
                        elif mid_action == "desktop_next":
                            with keyboard.pressed(Key.ctrl, Key.cmd):
                                keyboard.tap(Key.right)
                        elif mid_action == "desktop_prev":
                            with keyboard.pressed(Key.ctrl, Key.cmd):
                                keyboard.tap(Key.left)
                evaluated_gesture = "RIGHT_CLICK"
            elif is_thumb_index_pinching:
                if pinch_start_time is None:
                    pinch_start_time = now
                hold_duration = now - pinch_start_time
                if hold_duration >= 0.25:
                    evaluated_gesture = "DRAG_HOLD"
                    idx_hold_action = bindings.get("thumb_index_hold", "drag_hold")
                    if idx_hold_action == "drag_hold":
                        if state.is_armed and not is_drag_down:
                            mouse.press(Button.left)
                            is_drag_down = True
                else:
                    evaluated_gesture = "MOVE"
            else:
                if pinch_start_time is not None:
                    hold_duration = now - pinch_start_time
                    if is_drag_down:
                        if state.is_armed:
                            mouse.release(Button.left)
                        is_drag_down = False
                        evaluated_gesture = "DRAG_RELEASE"
                    elif hold_duration < 0.25:
                        evaluated_gesture = "LEFT_CLICK"
                        left_click_flash_until = now + 0.35
                        if state.is_armed:
                            idx_pinch_action = bindings.get("thumb_index_pinch", "left_click")
                            if idx_pinch_action == "left_click":
                                mouse.click(Button.left, 1)
                            elif idx_pinch_action == "double_click":
                                mouse.click(Button.left, 2)
                            elif idx_pinch_action == "play_pause":
                                keyboard.tap(Key.media_play_pause)
                            elif idx_pinch_action == "mute":
                                keyboard.tap(Key.media_volume_mute)
                    pinch_start_time = None
                else:
                    if now < left_click_flash_until:
                        evaluated_gesture = "LEFT_CLICK"
                    elif now < right_click_flash_until:
                        evaluated_gesture = "RIGHT_CLICK"
                    else:
                        evaluated_gesture = "MOVE"

            # Direct Zero-Lag Win32 OS Mouse Movement
            if state.is_armed and not is_closed_fist and current_active_combo == "NONE":
                screen_w, screen_h = state.screen_w, state.screen_h
                target_x = int(smooth_x * (screen_w - 1))
                target_y = int(smooth_y * (screen_h - 1))
                target_x = max(0, min(screen_w - 1, target_x))
                target_y = max(0, min(screen_h - 1, target_y))

                if target_x != last_cursor_x or target_y != last_cursor_y:
                    user32.SetCursorPos(target_x, target_y)
                    last_cursor_x, last_cursor_y = target_x, target_y

            # Draw Gesture Badge near Primary Hand
            knuckle_px = int(index_knuckle.x * w)
            knuckle_py = int(index_knuckle.y * h)
            badge_x = min(w - 130, max(10, knuckle_px + 20))
            badge_y = min(h - 10, max(25, knuckle_py - 10))

            badge_text = evaluated_gesture
            cv2.rectangle(frame, (badge_x - 4, badge_y - 14), (badge_x + len(badge_text) * 8 + 8, badge_y + 4), (15, 23, 42), -1)
            cv2.rectangle(frame, (badge_x - 4, badge_y - 14), (badge_x + len(badge_text) * 8 + 8, badge_y + 4), box_color, 1)
            cv2.putText(frame, badge_text, (badge_x, badge_y), cv2.FONT_HERSHEY_SIMPLEX, 0.4, box_color, 1, cv2.LINE_AA)

            # =================================================================
            # 3. CUSTOM GESTURE RECORDING & MATCHING (Phase 5)
            # =================================================================
            pose_vec = extract_pose_vector(primary_lms, mirror_x=False)
            pose_vec_mirrored = extract_pose_vector(primary_lms, mirror_x=True)

            if state.recording_target is not None:
                state.recording_target["samples"].append(pose_vec)
                state.recording_frames_left -= 1
                frames_done = 30 - state.recording_frames_left

                cv2.rectangle(frame, (20, h - 45), (w - 20, h - 15), (20, 20, 80), -1)
                cv2.rectangle(frame, (20, h - 45), (w - 20, h - 15), (0, 0, 255), 2)
                progress_w = int((w - 44) * (frames_done / 30))
                cv2.rectangle(frame, (22, h - 43), (22 + progress_w, h - 17), (0, 50, 200), -1)
                cv2.putText(
                    frame,
                    f"RECORDING POSE: {state.recording_target['name']} ({frames_done}/30 FRAMES)",
                    (28, h - 25),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,
                    (255, 255, 255),
                    1,
                    cv2.LINE_AA,
                )

                if state.recording_frames_left <= 0:
                    avg_vec = np.mean(state.recording_target["samples"], axis=0)
                    norm = np.linalg.norm(avg_vec)
                    if norm > 1e-6:
                        avg_vec = avg_vec / norm

                    new_gesture = {
                        "id": f"g_{int(time.time() * 1000)}",
                        "name": state.recording_target["name"],
                        "action_type": state.recording_target["action_type"],
                        "action_payload": state.recording_target["action_payload"],
                        "vector": avg_vec.tolist(),
                        "tolerance": 0.86,
                        "trigger_count": 0,
                        "last_triggered": None,
                    }

                    with state.lock:
                        state.custom_gestures.append(new_gesture)
                        save_custom_gestures(state.custom_gestures)
                        target_name = state.recording_target["name"]
                        state.recording_target = None
                        state.active_macro = f"SAVED: {target_name}"
                        state.macro_flash_until = now + 2.5
                        state.event_queue.append({
                            "type": "recording_complete",
                            "gesture": new_gesture,
                            "timestamp": time.strftime("%H:%M:%S")
                        })
                    logger.info(f"[GESTURE RECORDER] Successfully recorded and saved '{target_name}'!")

            elif len(state.custom_gestures) > 0 and not is_closed_fist:
                best_sim = -1.0
                best_g = None
                best_is_mirrored = False

                for g in state.custom_gestures:
                    if "vector" not in g or not g["vector"]:
                        continue
                    tpl = np.array(g["vector"], dtype=np.float32)
                    sim_normal = float(np.dot(pose_vec, tpl))
                    sim_mirrored = float(np.dot(pose_vec_mirrored, tpl))
                    sim = max(sim_normal, sim_mirrored)

                    if sim > best_sim:
                        best_sim = sim
                        best_g = g
                        best_is_mirrored = sim_mirrored > sim_normal

                tol = best_g.get("tolerance", 0.86) if best_g else 0.86
                if best_g and best_sim >= tol:
                    last_fired = best_g.get("last_fired_time", 0.0)
                    if now - last_fired > 2.0:
                        best_g["last_fired_time"] = now
                        best_g["trigger_count"] = best_g.get("trigger_count", 0) + 1
                        best_g["last_triggered"] = time.strftime("%H:%M:%S")

                        state.active_macro = f"{best_g['name']} ({int(best_sim * 100)}%)"
                        state.macro_flash_until = now + 2.0

                        if best_sim >= 0.88:
                            active_pvec = pose_vec_mirrored if best_is_mirrored else pose_vec
                            tpl_arr = np.array(best_g["vector"], dtype=np.float32)
                            adapted = 0.95 * tpl_arr + 0.05 * active_pvec
                            norm_ad = np.linalg.norm(adapted)
                            if norm_ad > 1e-6:
                                adapted /= norm_ad
                            best_g["vector"] = adapted.tolist()
                            state.needs_save_gestures = True

                        trigger_macro_action(best_g["action_type"], best_g["action_payload"])

                        with state.lock:
                            state.event_queue.append({
                                "type": "macro_fired",
                                "id": best_g.get("id"),
                                "name": best_g.get("name"),
                                "action_type": best_g.get("action_type"),
                                "action_payload": best_g.get("action_payload"),
                                "confidence": round(best_sim, 3),
                                "trigger_count": best_g["trigger_count"],
                                "timestamp": time.strftime("%H:%M:%S")
                            })

        else:
            missing_hand_frames += 1
            if missing_hand_frames > 5:
                evaluated_gesture = "IDLE"
                if is_drag_down and state.is_armed:
                    mouse.release(Button.left)
                    is_drag_down = False

        state.gesture = evaluated_gesture
        state.hand_detected = hand_detected
        state.cursor_x = smooth_x
        state.cursor_y = smooth_y

        # Feed activity into the Dream State watchdog (monitors user presence)
        dream_watchdog.notify_presence(hand_detected and state.camera_enabled)

        # Top HUD Status Banner
        status_text = (
            f"NATIVE 60FPS: {measured_fps:.0f} FPS | "
            f"HANDS: {num_hands} | "
            f"GESTURE: {evaluated_gesture} | "
            f"OS: {'ARMED' if state.is_armed else 'PAUSED'}"
        )
        cv2.rectangle(frame, (10, 8), (w - 10, 30), (15, 23, 42), -1)
        cv2.rectangle(frame, (10, 8), (w - 10, 30), box_color, 1)
        cv2.putText(
            frame,
            status_text,
            (16, 23),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

        # Macro / Combo Alert Banner
        if state.active_macro != "NONE" and now < state.macro_flash_until:
            cv2.rectangle(frame, (10, 34), (w - 10, 56), (15, 45, 25), -1)
            cv2.rectangle(frame, (10, 34), (w - 10, 56), (65, 200, 60), 1)
            cv2.putText(
                frame,
                f"ACTION FIRED: {state.active_macro}",
                (18, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (220, 255, 220),
                1,
                cv2.LINE_AA,
            )

        # Debounced Gesture Persistence
        if state.needs_save_gestures and now - state.last_save_time > 4.0:
            state.last_save_time = now
            state.needs_save_gestures = False
            with state.lock:
                save_custom_gestures(state.custom_gestures)

        # Compress to JPEG for MJPEG Streaming
        _, jpeg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
        with state.lock:
            state.latest_jpeg = jpeg.tobytes()

        # Telemetry & Event Broadcast (~25Hz)
        if now - last_telemetry_time >= 0.04:
            last_telemetry_time = now
            pending_events = []
            with state.lock:
                if state.event_queue:
                    pending_events = list(state.event_queue)
                    state.event_queue.clear()

            telemetry_data = json.dumps({
                "type": "telemetry",
                "fps": round(measured_fps, 1),
                "gesture": evaluated_gesture,
                "x": round(smooth_x, 4),
                "y": round(smooth_y, 4),
                "is_armed": state.is_armed,
                "camera_enabled": state.camera_enabled,
                "hand_detected": hand_detected,
                "hands_count": num_hands,
                "active_combo": state.active_combo,
                "active_macro": state.active_macro if now < state.macro_flash_until else "NONE",
                "screen": {"width": state.screen_w, "height": state.screen_h},
            })

            with state.lock:
                dead_sockets = set()
                for ws in list(state.active_websockets):
                    try:
                        loop = getattr(ws, "_loop", None)
                        if loop and loop.is_running():
                            asyncio.run_coroutine_threadsafe(ws.send_text(telemetry_data), loop)
                            for evt in pending_events:
                                asyncio.run_coroutine_threadsafe(ws.send_text(json.dumps(evt)), loop)
                        else:
                            dead_sockets.add(ws)
                    except Exception:
                        dead_sockets.add(ws)
                state.active_websockets -= dead_sockets

    if cap is not None:
        cap.release()
    logger.info("Native vision thread stopped cleanly")


# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    state.running = True
    vision_thread = threading.Thread(target=native_vision_worker, daemon=True)
    vision_thread.start()
    dream_watchdog.start()
    mobile_bridge.macro_executor = trigger_macro_action
    mobile_bridge.tracker_state = state
    mobile_bridge.start()
    yield
    mobile_bridge.stop()
    dream_watchdog.stop()
    state.running = False


# FastAPI Application
app = FastAPI(title="Air-OS Native Hand Controller Backend", version="5.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mobile_router)

FRONTEND_DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "dist")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MOBILE_HTML_PATH = os.path.join(STATIC_DIR, "mobile.html")

assets_dir = os.path.join(FRONTEND_DIST, "assets")
if os.path.exists(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/desktop")
async def get_desktop_ui():
    """Serves the full luxury desktop cockpit."""
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse("<h1>Air-OS Desktop Cockpit</h1><p>frontend/dist not found. Please build frontend.</p>", status_code=404)


@app.get("/")
async def root_router(request: Request):
    """
    Intelligent Root Router:
    - If accessed on mobile or with ?view=mobile -> serves mobile.html
    - If accessed on desktop/laptop or with ?view=desktop -> serves full desktop cockpit!
    """
    view = request.query_params.get("view", "")
    if view == "mobile":
        if os.path.exists(MOBILE_HTML_PATH):
            return FileResponse(MOBILE_HTML_PATH, media_type="text/html")
    if view == "desktop":
        index_path = os.path.join(FRONTEND_DIST, "index.html")
        if os.path.exists(index_path):
            return FileResponse(index_path, media_type="text/html")

    ua = request.headers.get("user-agent", "").lower()
    is_mobile = any(k in ua for k in ["iphone", "android", "mobile", "ipad"])
    if is_mobile:
        if os.path.exists(MOBILE_HTML_PATH):
            return FileResponse(MOBILE_HTML_PATH, media_type="text/html")

    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, media_type="text/html")

    if os.path.exists(MOBILE_HTML_PATH):
        return FileResponse(MOBILE_HTML_PATH, media_type="text/html")
    return HTMLResponse("<h1>Air-OS</h1><p>No UI file found.</p>", status_code=404)


@app.get("/favicon.svg")
async def get_fav_svg():
    p = os.path.join(FRONTEND_DIST, "favicon.svg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/svg+xml")
    raise HTTPException(status_code=404)


@app.get("/icons.svg")
async def get_icons_svg():
    p = os.path.join(FRONTEND_DIST, "icons.svg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/svg+xml")
    raise HTTPException(status_code=404)


@app.get("/airos_icon.jpg")
async def get_airos_icon():
    p = os.path.join(FRONTEND_DIST, "airos_icon.jpg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/jpeg")
    raise HTTPException(status_code=404)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "Air-OS Native Hand Controller Backend (Phase 5 Supercharged)",
        "fps": state.fps,
        "is_armed": state.is_armed,
        "camera_enabled": state.camera_enabled,
        "gesture": state.gesture,
        "hands_count": state.hands_count,
        "custom_gestures_count": len(state.custom_gestures),
        "screen": {"width": state.screen_w, "height": state.screen_h},
    }


@app.get("/api/state")
async def get_state():
    return {
        "is_armed": state.is_armed,
        "camera_enabled": state.camera_enabled,
        "fps": state.fps,
        "gesture": state.gesture,
        "hand_detected": state.hand_detected,
        "hands_count": state.hands_count,
        "active_combo": state.active_combo,
        "active_macro": state.active_macro,
        "screen": {"width": state.screen_w, "height": state.screen_h},
    }


@app.post("/api/toggle_arm")
async def toggle_arm():
    state.is_armed = not state.is_armed
    logger.info(f"[OS CONTROL] Toggled is_armed -> {state.is_armed}")
    return {"is_armed": state.is_armed}


@app.post("/api/arm")
async def set_arm(data: dict):
    state.is_armed = bool(data.get("armed", False))
    logger.info(f"[OS CONTROL] Set is_armed -> {state.is_armed}")
    return {"is_armed": state.is_armed}


# =============================================================================
# Camera Hardware & Privacy Control APIs (Turn Camera Off / On)
# =============================================================================
@app.post("/api/camera/toggle")
async def toggle_camera():
    with state.lock:
        state.camera_enabled = not state.camera_enabled
        if not state.camera_enabled:
            state.is_armed = False  # automatically pause control when camera turns off
    logger.info(f"[CAMERA] Toggled camera_enabled -> {state.camera_enabled}")
    return {"camera_enabled": state.camera_enabled, "is_armed": state.is_armed}


@app.post("/api/camera")
async def set_camera(data: dict):
    with state.lock:
        state.camera_enabled = bool(data.get("enabled", True))
        if not state.camera_enabled:
            state.is_armed = False
    logger.info(f"[CAMERA] Set camera_enabled -> {state.camera_enabled}")
    return {"camera_enabled": state.camera_enabled, "is_armed": state.is_armed}


# =============================================================================
# Custom Configuration REST APIs (Combos & Bindings Customizer)
# =============================================================================
@app.get("/api/config")
async def get_config():
    with state.lock:
        return state.config


@app.post("/api/config")
async def update_config(data: dict):
    with state.lock:
        if "combos" in data:
            state.config.setdefault("combos", {}).update(data["combos"])
        if "bindings" in data:
            state.config.setdefault("bindings", {}).update(data["bindings"])
        save_app_config(state.config)
        logger.info(f"[CONFIG] Updated user customization: {state.config}")
    return {"status": "success", "config": state.config}


# =============================================================================
# Custom Gesture Studio REST APIs (Phase 5)
# =============================================================================
@app.get("/api/gestures")
async def get_custom_gestures():
    sanitized = []
    with state.lock:
        for g in state.custom_gestures:
            sanitized.append({
                "id": g.get("id"),
                "name": g.get("name"),
                "action_type": g.get("action_type"),
                "action_payload": g.get("action_payload"),
                "tolerance": g.get("tolerance", 0.86),
                "trigger_count": g.get("trigger_count", 0),
                "last_triggered": g.get("last_triggered"),
            })
    return {"gestures": sanitized}


@app.post("/api/gestures/record")
async def start_gesture_record(data: dict):
    name = data.get("name", "").strip()
    action_type = data.get("action_type", "url")
    action_payload = data.get("action_payload", "").strip()

    if not name or not action_payload:
        raise HTTPException(status_code=400, detail="Name and action_payload are required.")

    with state.lock:
        state.recording_target = {
            "name": name,
            "action_type": action_type,
            "action_payload": action_payload,
            "samples": []
        }
        state.recording_frames_left = 30

    logger.info(f"[GESTURE RECORDER] Started recording session for '{name}' (30 frames)")
    return {"status": "recording_started", "name": name, "frames": 30}


@app.delete("/api/gestures/{gesture_id}")
async def delete_custom_gesture(gesture_id: str):
    with state.lock:
        orig_len = len(state.custom_gestures)
        state.custom_gestures = [g for g in state.custom_gestures if g.get("id") != gesture_id]
        if len(state.custom_gestures) != orig_len:
            save_custom_gestures(state.custom_gestures)
            logger.info(f"[GESTURE RECORDER] Deleted gesture {gesture_id}")
            return {"status": "deleted", "id": gesture_id}
    raise HTTPException(status_code=404, detail="Gesture not found")


@app.post("/api/gestures/test/{gesture_id}")
async def test_custom_gesture(gesture_id: str):
    target = None
    with state.lock:
        for g in state.custom_gestures:
            if g.get("id") == gesture_id:
                target = g
                break
    if not target:
        raise HTTPException(status_code=404, detail="Gesture not found")

    trigger_macro_action(target["action_type"], target["action_payload"])
    return {"status": "triggered", "gesture": target["name"]}


# =============================================================================
# Phase 6: Local Ollama AI, Multimodal Vision & Speech Assistant
# =============================================================================
OLLAMA_BASE_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

SYSTEM_PROMPT_TEMPLATE = """You are Air-OS, an intelligent Windows 11 AI assistant powered by local vision, voice, and hand gestures.
Be concise, punchy, helpful, and friendly.
If the user explicitly asks to control their computer, open an app, navigate, or trigger a hotkey, append an action tag to your answer:
- [ACTION:OPEN_URL:https://...]
- [ACTION:OPEN_APP:notepad.exe] or [ACTION:OPEN_APP:calc.exe]
- [ACTION:HOTKEY:VOLUME_UP]
- [ACTION:HOTKEY:VOLUME_DOWN]
- [ACTION:HOTKEY:MUTE]
- [ACTION:HOTKEY:PLAY_PAUSE]
- [ACTION:HOTKEY:SHOW_DESKTOP]
- [ACTION:HOTKEY:TASK_VIEW]
- [ACTION:HOTKEY:LOCK_PC]
- [ACTION:HOTKEY:CLOSE_WINDOW]
Only output actions when requested by the user. Keep normal answers conversational and clear."""


def extract_actions_from_text(text: str) -> List[dict]:
    actions = []
    pattern = re.compile(r'\[ACTION:([A-Za-z0-9_]+)(?::([^\]]+))?\]', re.IGNORECASE)
    for tag, payload in pattern.findall(text):
        tag = tag.upper().strip()
        payload = (payload or "").strip()
        if tag in ["URL", "OPEN_URL"]:
            actions.append({"type": "url", "payload": payload, "raw": f"[ACTION:{tag}:{payload}]"})
        elif tag in ["SHELL", "OPEN_APP", "APP"]:
            actions.append({"type": "shell", "payload": payload, "raw": f"[ACTION:{tag}:{payload}]"})
        elif tag in ["HOTKEY", "KEY"]:
            actions.append({"type": "hotkey", "payload": payload.upper(), "raw": f"[ACTION:{tag}:{payload}]"})
        elif tag in ["SHOW_DESKTOP", "TASK_VIEW", "VOLUME_UP", "VOLUME_DOWN", "MUTE", "LOCK_PC", "CLOSE_WINDOW", "PLAY_PAUSE"]:
            actions.append({"type": "hotkey", "payload": tag, "raw": f"[ACTION:{tag}]"})
    return actions


def speak_text_worker(text: str, rate: int = 185):
    try:
        ensure_interactive_desktop()
        if pyttsx3:
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", rate)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
                del engine
                return
            except Exception as e:
                logger.warning(f"pyttsx3 error, falling back to PowerShell SAPI: {e}")

        # Windows PowerShell SAPI fallback
        safe_text = text.replace("'", "''").replace('"', '`"')
        cmd = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{safe_text}')"
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", cmd])
    except Exception as e:
        logger.warning(f"TTS execution error: {e}")


@app.get("/api/ollama/models")
async def get_ollama_models():
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                models = []
                for m in data.get("models", []):
                    details = m.get("details", {})
                    caps = m.get("capabilities", [])
                    name = m.get("name", "")
                    has_vision = (
                        "vision" in caps
                        or any(x in name.lower() for x in ["llava", "moondream", "vision", "gemma4", "minicpm", "bakllava"])
                    )
                    models.append({
                        "name": name,
                        "model": m.get("model", name),
                        "size": m.get("size", 0),
                        "parameter_size": details.get("parameter_size", ""),
                        "family": details.get("family", ""),
                        "has_vision": has_vision,
                    })
                return {"status": "online", "models": models}
            return {"status": "error", "models": [], "error": f"Ollama HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "offline", "models": [], "error": f"Ollama unreachable on {OLLAMA_BASE_URL}: {e}"}


@app.post("/api/ollama/chat")
async def ollama_chat(data: dict):
    model = data.get("model", "moondream:1.8b")
    messages = data.get("messages", [])
    prompt = data.get("prompt", "")
    auto_execute = data.get("auto_execute", False)

    active_prompt = prompt_manager.get_current_prompt()
    ollama_messages = [{"role": "system", "content": active_prompt}]
    if messages:
        ollama_messages.extend(messages)
    elif prompt:
        ollama_messages.append({"role": "user", "content": prompt})

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": ollama_messages,
                    "stream": False,
                }
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=f"Ollama returned: {resp.text}")

            res_data = resp.json()
            reply_text = res_data.get("message", {}).get("content", "")
            actions = extract_actions_from_text(reply_text)

            executed = []
            if auto_execute and actions:
                for a in actions:
                    trigger_macro_action(a["type"], a["payload"])
                    executed.append(a)

            # Log interaction to persistent memory.json for autonomous Dream State learning
            try:
                user_prompt_str = prompt or (messages[-1].get("content", "") if messages else "")
                memory_store.log_interaction(
                    prompt=user_prompt_str,
                    response=reply_text,
                    source="chat",
                    actions=actions,
                    model=model,
                    workflow_state={
                        "hands_count": state.hands_count,
                        "active_gesture": state.gesture,
                        "camera_enabled": state.camera_enabled,
                    }
                )
            except Exception as log_err:
                logger.warning(f"Failed to log chat interaction to memory.json: {log_err}")

            return {
                "status": "success",
                "model": model,
                "message": {"role": "assistant", "content": reply_text},
                "actions": actions,
                "executed": executed,
            }
    except Exception as e:
        logger.error(f"Error calling Ollama chat: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/vision/capture")
async def vision_capture(data: dict):
    prompt = data.get("prompt", "Analyze what you see in front of my webcam. Describe any objects, people, hands, or text.").strip()
    model = data.get("model", "moondream:1.8b").strip()
    auto_execute = data.get("auto_execute", False)

    with state.lock:
        if not state.camera_enabled:
            raise HTTPException(status_code=400, detail="Webcam is currently turned OFF. Turn camera on first.")
        jpeg_bytes = state.latest_clean_jpeg or state.latest_jpeg

    if not jpeg_bytes:
        raise HTTPException(status_code=503, detail="No video frame available from camera yet.")

    b64_img = base64.b64encode(jpeg_bytes).decode("utf-8")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "images": [b64_img],
                    "system": "You are Air-OS Vision AI analyzing the live user webcam view. Provide direct, concise, and helpful answers. If asked to trigger an action based on what you see, append [ACTION:OPEN_URL:...] or [ACTION:HOTKEY:...].",
                    "stream": False,
                }
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=f"Ollama vision inference failed: {resp.text}")

            res_data = resp.json()
            analysis_text = res_data.get("response", "").strip()
            actions = extract_actions_from_text(analysis_text)

            executed = []
            if auto_execute and actions:
                for a in actions:
                    trigger_macro_action(a["type"], a["payload"])
                    executed.append(a)

            # Log vision interaction to memory.json
            try:
                memory_store.log_interaction(
                    prompt=prompt,
                    response=analysis_text,
                    source="vision",
                    actions=actions,
                    model=model,
                    workflow_state={
                        "camera_enabled": state.camera_enabled,
                        "hands_count": state.hands_count,
                    }
                )
            except Exception as log_err:
                logger.warning(f"Failed to log vision interaction to memory.json: {log_err}")

            return {
                "status": "success",
                "model": model,
                "response": analysis_text,
                "snapshot_base64": f"data:image/jpeg;base64,{b64_img}",
                "actions": actions,
                "executed": executed,
            }
    except Exception as e:
        logger.error(f"Vision capture error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tts")
async def tts_endpoint(data: dict):
    text = data.get("text", "").strip()
    rate = int(data.get("rate", 185))
    if not text:
        raise HTTPException(status_code=400, detail="Text parameter is required.")

    clean_text = re.sub(r'\[ACTION:[^\]]+\]', '', text).strip()
    if clean_text:
        threading.Thread(target=speak_text_worker, args=(clean_text, rate), daemon=True).start()

    return {"status": "speaking", "text": clean_text}


@app.post("/api/action/execute")
async def execute_action(data: dict):
    act_type = data.get("type") or data.get("action_type") or "hotkey"
    payload = data.get("payload") or data.get("action_payload") or ""
    trigger_macro_action(act_type, payload)
    return {"status": "executed", "action_type": act_type, "payload": payload}


@app.get("/api/vision/snapshot")
def get_snapshot_image():
    with state.lock:
        data = state.latest_clean_jpeg or state.latest_jpeg
    if not data:
        raise HTTPException(status_code=404, detail="No snapshot available")
    return StreamingResponse(io.BytesIO(data), media_type="image/jpeg")


# =============================================================================
# DREAM STATE & AUTONOMOUS COGNITIVE ENDPOINTS (Phase 6+)
# =============================================================================
@app.get("/api/dream/status")
def get_dream_status():
    status = dream_watchdog.get_status()
    stats = memory_store.get_stats()
    prompt_history = prompt_manager.get_history()
    return {
        "watchdog": status,
        "stats": stats,
        "current_prompt_version": len(prompt_history) + 1,
        "prompt_versions_count": len(prompt_history),
        "active_model": dream_engine.default_model,
    }


@app.post("/api/dream/trigger")
def trigger_dream_state(data: dict = None):
    data = data or {}
    model = data.get("model")
    force = data.get("force", True)
    try:
        res = dream_engine.run_dream_cycle(model=model, force=force)
        return res
    except Exception as e:
        logger.error(f"Dream trigger error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dream/memories")
def get_dream_memories(limit: int = 30):
    return memory_store.get_recent_interactions(limit=limit)


@app.get("/api/dream/prompt")
def get_dream_prompt_info():
    return {
        "current_prompt": prompt_manager.get_current_prompt(),
        "history": prompt_manager.get_history(),
        "dream_log": dream_engine.get_dream_history(),
    }


@app.post("/api/dream/prompt")
def update_dream_prompt(data: dict):
    new_p = data.get("prompt", "").strip()
    if not new_p:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty.")
    v = prompt_manager.update_prompt(new_p, dream_id="manual_edit", insights=["Manual user edit from UI"])
    return {"status": "updated", "version": v}


# MJPEG Video Stream
def generate_mjpeg_stream():
    while state.running:
        with state.lock:
            jpeg_bytes = state.latest_jpeg
        if jpeg_bytes:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpeg_bytes + b"\r\n"
            )
        time.sleep(0.02)


@app.get("/api/video_feed")
def video_feed():
    return StreamingResponse(
        generate_mjpeg_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# WebSocket Telemetry & Control Endpoint
@app.websocket("/ws/control")
async def websocket_control_endpoint(websocket: WebSocket):
    await websocket.accept()
    websocket._loop = asyncio.get_running_loop()  # type: ignore

    with state.lock:
        state.active_websockets.add(websocket)

    logger.info(f">>> [WS CONNECTED] Client joined /ws/control. Total clients: {len(state.active_websockets)}")

    try:
        sanitized_gestures = [
            {
                "id": g.get("id"),
                "name": g.get("name"),
                "action_type": g.get("action_type"),
                "action_payload": g.get("action_payload"),
                "tolerance": g.get("tolerance", 0.86),
                "trigger_count": g.get("trigger_count", 0),
                "last_triggered": g.get("last_triggered"),
            }
            for g in state.custom_gestures
        ]

        await websocket.send_json({
            "type": "ready",
            "message": "Air-OS Native Multi-Hand & Macro Engine Active",
            "is_armed": state.is_armed,
            "camera_enabled": state.camera_enabled,
            "fps": state.fps,
            "hands_count": state.hands_count,
            "gestures": sanitized_gestures,
            "config": state.config,
            "screen": {"width": state.screen_w, "height": state.screen_h},
        })

        while True:
            text = await websocket.receive_text()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type") or data.get("action")
            if msg_type == "toggle_arm":
                state.is_armed = not state.is_armed
                logger.info(f"[WS] Toggled is_armed -> {state.is_armed}")
            elif msg_type == "set_arm":
                state.is_armed = bool(data.get("armed", False))
                logger.info(f"[WS] Set is_armed -> {state.is_armed}")
            elif msg_type == "toggle_camera":
                state.camera_enabled = not state.camera_enabled
                if not state.camera_enabled:
                    state.is_armed = False
                logger.info(f"[WS] Toggled camera_enabled -> {state.camera_enabled}")
            elif msg_type == "set_camera":
                state.camera_enabled = bool(data.get("enabled", True))
                if not state.camera_enabled:
                    state.is_armed = False
                logger.info(f"[WS] Set camera_enabled -> {state.camera_enabled}")
            elif msg_type == "update_config":
                cfg = data.get("config", {})
                with state.lock:
                    if "combos" in cfg:
                        state.config.setdefault("combos", {}).update(cfg["combos"])
                    if "bindings" in cfg:
                        state.config.setdefault("bindings", {}).update(cfg["bindings"])
                    save_app_config(state.config)
                logger.info(f"[WS] Updated user config from WebSocket")
            elif msg_type == "start_record":
                name = data.get("name", "").strip()
                action_type = data.get("action_type", "url")
                action_payload = data.get("action_payload", "").strip()
                if name and action_payload:
                    with state.lock:
                        state.recording_target = {
                            "name": name,
                            "action_type": action_type,
                            "action_payload": action_payload,
                            "samples": []
                        }
                        state.recording_frames_left = 30
                    logger.info(f"[WS RECORDER] Started recording for '{name}'")
            elif msg_type == "delete_gesture":
                gid = data.get("id")
                with state.lock:
                    state.custom_gestures = [g for g in state.custom_gestures if g.get("id") != gid]
                    save_custom_gestures(state.custom_gestures)
                logger.info(f"[WS RECORDER] Deleted gesture {gid}")
            elif msg_type == "hotkey":
                key_name = data.get("key") or data.get("name")
                trigger_macro_action("hotkey", key_name)

    except WebSocketDisconnect:
        with state.lock:
            state.active_websockets.discard(websocket)
        logger.info("<<< [WS DISCONNECT] Client left /ws/control")
    except Exception as e:
        logger.error(f"WebSocket exception: {e}")
        with state.lock:
            state.active_websockets.discard(websocket)
