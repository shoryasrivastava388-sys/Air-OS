"""
Air-OS Mobile Wi-Fi Real-Time Companion Bridge
==============================================
FastAPI WebSocket & HTTP bridge connecting desktop Air-OS to a mobile phone browser over local Wi-Fi.

Core Capabilities:
1. Serves the standalone mobile web UI at GET / and GET /mobile.
2. Real-Time State Transfer:
   - Monitors PC webcam presence via dream_watchdog.
   - When the user walks away (idle >= threshold), pushes "TRANSFER_SOUL" event to phone.
   - When user returns to PC, pushes "RETURN_SOUL" event back to desktop.
   - Supports manual transfer toggle from phone or PC.
3. Mobile Voice & Text Input:
   - Ingests text or transcribed voice messages directly into PC's memory.json instantly.
   - Queries local Ollama model using the self-optimized system prompt.
   - Executes remote PC macro actions ([ACTION:LOCK_PC], [ACTION:SHOW_DESKTOP], etc.).
   - Pushes real-time assistant responses and memory status back to the phone.
"""

import asyncio
import datetime
import json
import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.responses import FileResponse, HTMLResponse
import httpx

from dream_engine import memory_store, prompt_manager, dream_watchdog

logger = logging.getLogger("mobile-bridge")
logger.setLevel(logging.INFO)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MOBILE_HTML_PATH = os.path.join(STATIC_DIR, "mobile.html")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")


class MobileBridgeManager:
    """Manages mobile client WebSockets, state transfer, and voice/text interactions."""

    def __init__(
        self,
        memory_store_ref=memory_store,
        prompt_manager_ref=prompt_manager,
        dream_watchdog_ref=dream_watchdog,
        macro_executor: Optional[Callable[[str, str], Any]] = None,
        tracker_state_ref: Any = None,
        away_threshold_seconds: float = 8.0,
    ):
        self.memory_store = memory_store_ref
        self.prompt_manager = prompt_manager_ref
        self.dream_watchdog = dream_watchdog_ref
        self.macro_executor = macro_executor
        self.tracker_state = tracker_state_ref
        self.away_threshold_seconds = away_threshold_seconds

        self.soul_location: str = "desktop"  # "desktop" or "mobile"
        self.active_websockets: Set[WebSocket] = set()
        self.lock = threading.RLock()
        self.running: bool = False
        self.monitor_thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the background presence transition watcher thread."""
        if self.running:
            return
        self.running = True
        self.monitor_thread = threading.Thread(
            target=self._presence_transition_loop,
            daemon=True,
            name="MobileBridgePresenceWatcher",
        )
        self.monitor_thread.start()
        logger.info(
            f"[MOBILE BRIDGE STARTED] State Transfer Watcher active (Away threshold: {self.away_threshold_seconds}s)"
        )

    def stop(self):
        self.running = False
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=1.5)
        logger.info("[MOBILE BRIDGE STOPPED]")

    # -------------------------------------------------------------------------
    # WebSocket Registration & Broadcast
    # -------------------------------------------------------------------------
    def register_client(self, websocket: WebSocket):
        with self.lock:
            self.active_websockets.add(websocket)
        logger.info(f"[MOBILE WS JOIN] Mobile client connected. Total mobile devices: {len(self.active_websockets)}")

    def unregister_client(self, websocket: WebSocket):
        with self.lock:
            self.active_websockets.discard(websocket)
        logger.info(f"[MOBILE WS LEFT] Mobile client disconnected. Total mobile devices: {len(self.active_websockets)}")

    def broadcast_sync(self, payload: Dict[str, Any]):
        """Thread-safe synchronous broadcast to all active mobile WebSockets."""
        with self.lock:
            clients = list(self.active_websockets)

        if not clients:
            return

        text_data = json.dumps(payload, ensure_ascii=False)
        dead: Set[WebSocket] = set()

        for ws in clients:
            try:
                loop = getattr(ws, "_loop", None)
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(ws.send_text(text_data), loop)
                else:
                    dead.add(ws)
            except Exception:
                dead.add(ws)

        if dead:
            with self.lock:
                self.active_websockets -= dead

    async def broadcast_async(self, payload: Dict[str, Any]):
        """Asynchronous broadcast to all active mobile WebSockets."""
        with self.lock:
            clients = list(self.active_websockets)

        if not clients:
            return

        text_data = json.dumps(payload, ensure_ascii=False)
        dead: Set[WebSocket] = set()

        for ws in clients:
            try:
                await ws.send_text(text_data)
            except Exception:
                dead.add(ws)

        if dead:
            with self.lock:
                self.active_websockets -= dead

    # -------------------------------------------------------------------------
    # State Transfer Watchdog Loop (PC Webcam -> Phone)
    # -------------------------------------------------------------------------
    def _presence_transition_loop(self):
        """Monitors webcam presence and triggers TRANSFER_SOUL / RETURN_SOUL."""
        last_telemetry_time = 0.0

        while self.running:
            time.sleep(1.0)
            now = time.time()

            idle_sec = self.dream_watchdog.get_idle_seconds()
            user_present = self.dream_watchdog.presence_detected

            # Condition 1: User walked away from PC -> TRANSFER_SOUL
            if not user_present and idle_sec >= self.away_threshold_seconds:
                if self.soul_location != "mobile":
                    with self.lock:
                        self.soul_location = "mobile"
                    logger.info(
                        f"[SOUL TRANSFER EVENT] User absent for {idle_sec:.1f}s. "
                        f"Transferring companion soul to MOBILE!"
                    )
                    evt = self._build_transfer_event("TRANSFER_SOUL", "user_walked_away", idle_sec)
                    self.broadcast_sync(evt)
                    self._sync_desktop_websockets(evt)

            # Condition 2: User returned to PC -> RETURN_SOUL
            elif user_present and self.soul_location == "mobile":
                with self.lock:
                    self.soul_location = "desktop"
                logger.info("[SOUL RETURN EVENT] User returned to PC webcam! Docking soul back to DESKTOP.")
                evt = self._build_transfer_event("RETURN_SOUL", "user_returned", 0.0)
                self.broadcast_sync(evt)
                self._sync_desktop_websockets(evt)

            # Periodic Telemetry Heartbeat (every 2.5s)
            if now - last_telemetry_time >= 2.5 and self.active_websockets:
                last_telemetry_time = now
                telemetry = {
                    "type": "telemetry",
                    "soul_state": self.soul_location,
                    "presence_detected": user_present,
                    "idle_seconds": round(idle_sec, 1),
                    "total_interactions": self.memory_store.get_stats().get("total_interactions", 0),
                    "prompt_version": self.prompt_manager.get_current_version(),
                }
                self.broadcast_sync(telemetry)

    def _build_transfer_event(self, event_type: str, reason: str, idle_sec: float) -> Dict[str, Any]:
        stats = self.memory_store.get_stats()
        recent = self.memory_store.get_recent_interactions(1)
        last_mem = recent[-1] if recent else None

        mood = "Roaming on Mobile" if event_type == "TRANSFER_SOUL" else "Docked at Desktop"
        msg = (
            "User walked away from desktop. Air-OS Soul transferred to Mobile Phone!"
            if event_type == "TRANSFER_SOUL"
            else "User returned to desktop. Air-OS Soul docked back to PC!"
        )

        return {
            "type": event_type,
            "state": "mobile" if event_type == "TRANSFER_SOUL" else "desktop",
            "reason": reason,
            "timestamp": datetime.datetime.now().isoformat(),
            "companion": {
                "name": "Air-OS Soul",
                "mood": mood,
                "presence": event_type == "RETURN_SOUL",
                "idle_seconds": round(idle_sec, 1),
                "prompt_version": self.prompt_manager.get_current_version(),
                "total_interactions": stats.get("total_interactions", 0),
                "dream_cycles": stats.get("dream_cycles_completed", 0),
                "last_interaction": last_mem,
            },
            "message": msg,
        }

    def _sync_desktop_websockets(self, payload: Dict[str, Any]):
        """Notifies desktop HUD clients of the soul transfer if connected."""
        if not self.tracker_state:
            return
        try:
            with self.tracker_state.lock:
                desktop_clients = list(getattr(self.tracker_state, "active_websockets", set()))
            if not desktop_clients:
                return

            text_data = json.dumps(payload, ensure_ascii=False)
            dead = set()
            for ws in desktop_clients:
                try:
                    loop = getattr(ws, "_loop", None)
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(ws.send_text(text_data), loop)
                    else:
                        dead.add(ws)
                except Exception:
                    dead.add(ws)
            if dead:
                with self.tracker_state.lock:
                    self.tracker_state.active_websockets -= dead
        except Exception as e:
            logger.warning(f"Error notifying desktop clients: {e}")

    # -------------------------------------------------------------------------
    # Manual State Transfer
    # -------------------------------------------------------------------------
    def set_soul_location(self, target_state: str, reason: str = "manual_toggle") -> Dict[str, Any]:
        target = "mobile" if target_state.lower() == "mobile" else "desktop"
        with self.lock:
            self.soul_location = target

        event_type = "TRANSFER_SOUL" if target == "mobile" else "RETURN_SOUL"
        idle_sec = self.dream_watchdog.get_idle_seconds()
        evt = self._build_transfer_event(event_type, reason, idle_sec)
        self.broadcast_sync(evt)
        self._sync_desktop_websockets(evt)
        logger.info(f"[SOUL MANUAL TOGGLE] Set soul location to {target.upper()} (reason={reason})")
        return evt

    # -------------------------------------------------------------------------
    # Mobile Voice & Text Ingestion + Memory Logging
    # -------------------------------------------------------------------------
    async def process_mobile_input(
        self,
        text: str,
        source: str = "mobile_text",
        auto_execute: bool = True,
        model: Optional[str] = None,
        client_ip: str = "mobile_client",
    ) -> Dict[str, Any]:
        """
        Receives text or voice input from phone, logs to PC memory.json,
        invokes local Ollama with system prompt, executes macros on PC,
        and saves complete response to memory.json.
        """
        prompt_text = text.strip()
        if not prompt_text:
            return {"status": "error", "message": "Empty prompt received"}

        chosen_model = model or "qwen2.5:0.5b"
        active_system_prompt = self.prompt_manager.get_current_prompt()

        # Step 1: Immediately log incoming user input to memory.json
        interaction_entry = self.memory_store.log_interaction(
            prompt=prompt_text,
            response="[Processing Ollama response...]",
            source=source,
            model=chosen_model,
            workflow_state={
                "soul_location": self.soul_location,
                "user_present_at_pc": self.dream_watchdog.presence_detected,
                "idle_seconds": self.dream_watchdog.get_idle_seconds(),
                "client_ip": client_ip,
                "channel": "wifi_mobile_bridge",
            },
        )

        reply_text = ""
        actions: List[Dict[str, Any]] = []
        executed: List[Dict[str, Any]] = []

        # Step 2: Query Local Ollama with current optimized prompt
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                res = await client.post(
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": chosen_model,
                        "messages": [
                            {"role": "system", "content": active_system_prompt},
                            {"role": "user", "content": prompt_text},
                        ],
                        "stream": False,
                    },
                )
                if res.status_code == 200:
                    res_json = res.json()
                    reply_text = res_json.get("message", {}).get("content", "").strip()
                else:
                    reply_text = (
                        f"Companion received your message, but local Ollama returned HTTP {res.status_code}. "
                        f"Stored in memory."
                    )
        except Exception as ollama_err:
            logger.warning(f"[MOBILE INPUT] Ollama query failed: {ollama_err}")
            reply_text = f"Companion heard: '{prompt_text}'. Memory saved on PC!"

        # Step 3: Extract & execute PC action tags if present
        actions = self._extract_actions(reply_text)
        if auto_execute and actions and self.macro_executor:
            for act in actions:
                try:
                    self.macro_executor(act["type"], act["payload"])
                    executed.append(act)
                    logger.info(f"[MOBILE PC ACTION EXEC] Executed: {act['raw']}")
                except Exception as exec_err:
                    logger.error(f"Error executing macro from mobile: {exec_err}")

        # Step 4: Update memory.json with completed assistant response
        try:
            raw_mem = self.memory_store._read_raw()
            for item in raw_mem.get("interactions", []):
                if item.get("id") == interaction_entry.get("id"):
                    item["response"] = reply_text
                    item["actions"] = actions
                    item["executed"] = executed
                    break
            self.memory_store._write_raw(raw_mem)
        except Exception as update_err:
            logger.warning(f"Error updating memory.json response: {update_err}")

        result_payload = {
            "type": "chat_reply",
            "id": interaction_entry.get("id"),
            "user_prompt": prompt_text,
            "response": reply_text,
            "source": source,
            "model": chosen_model,
            "actions": actions,
            "executed": executed,
            "total_interactions": self.memory_store.get_stats().get("total_interactions", 0),
            "timestamp": datetime.datetime.now().isoformat(),
        }

        # Broadcast to all connected phones and PC
        self.broadcast_sync(result_payload)
        self._sync_desktop_websockets(result_payload)

        return result_payload

    def _extract_actions(self, text: str) -> List[Dict[str, Any]]:
        import re

        actions = []
        pattern = re.compile(r"\[ACTION:([A-Za-z0-9_]+)(?::([^\]]+))?\]", re.IGNORECASE)
        for tag, payload in pattern.findall(text):
            tag = tag.upper().strip()
            payload = (payload or "").strip()
            if tag in ["URL", "OPEN_URL"]:
                actions.append({"type": "url", "payload": payload, "raw": f"[ACTION:{tag}:{payload}]"})
            elif tag in ["SHELL", "OPEN_APP", "APP"]:
                actions.append({"type": "shell", "payload": payload, "raw": f"[ACTION:{tag}:{payload}]"})
            elif tag in ["HOTKEY", "KEY"]:
                actions.append({"type": "hotkey", "payload": payload.upper(), "raw": f"[ACTION:{tag}:{payload}]"})
            elif tag in [
                "SHOW_DESKTOP",
                "TASK_VIEW",
                "VOLUME_UP",
                "VOLUME_DOWN",
                "MUTE",
                "LOCK_PC",
                "CLOSE_WINDOW",
                "PLAY_PAUSE",
            ]:
                actions.append({"type": "hotkey", "payload": tag, "raw": f"[ACTION:{tag}]"})
        return actions


# Singleton Instance
mobile_bridge = MobileBridgeManager()


# -----------------------------------------------------------------------------
# FastAPI Mobile Router & Endpoints
# -----------------------------------------------------------------------------
mobile_router = APIRouter(tags=["Mobile Wi-Fi Bridge"])


@mobile_router.get("/")
@mobile_router.get("/mobile")
async def get_mobile_ui():
    """Serves the standalone mobile web client."""
    if os.path.exists(MOBILE_HTML_PATH):
        return FileResponse(MOBILE_HTML_PATH, media_type="text/html")
    return HTMLResponse(
        "<h1>Air-OS Mobile Bridge</h1><p>mobile.html file not found in static folder.</p>",
        status_code=404,
    )


@mobile_router.get("/manifest.json")
async def get_manifest():
    p = os.path.join(STATIC_DIR, "manifest.json")
    if os.path.exists(p):
        return FileResponse(p, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest.json not found")


@mobile_router.get("/sw.js")
async def get_sw():
    p = os.path.join(STATIC_DIR, "sw.js")
    if os.path.exists(p):
        return FileResponse(p, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="sw.js not found")


@mobile_router.get("/static/{file_name}")
async def get_static_asset(file_name: str):
    p = os.path.join(STATIC_DIR, file_name)
    if os.path.exists(p):
        return FileResponse(p)
    raise HTTPException(status_code=404, detail="Static asset not found")



@mobile_router.get("/api/mobile/status")
async def get_mobile_status():
    """Returns current status of mobile bridge, soul state, and presence."""
    return {
        "soul_location": mobile_bridge.soul_location,
        "presence_detected": mobile_bridge.dream_watchdog.presence_detected,
        "idle_seconds": mobile_bridge.dream_watchdog.get_idle_seconds(),
        "away_threshold_seconds": mobile_bridge.away_threshold_seconds,
        "system_prompt_version": mobile_bridge.prompt_manager.get_current_version(),
        "total_interactions": mobile_bridge.memory_store.get_stats().get("total_interactions", 0),
        "connected_mobile_clients": len(mobile_bridge.active_websockets),
    }


@mobile_router.post("/api/mobile/input")
async def post_mobile_input(data: dict):
    """REST endpoint for phone to push text or voice messages directly into memory.json."""
    text = data.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text' field")
    source = data.get("source", "mobile_text")
    auto_execute = data.get("auto_execute", True)
    model = data.get("model", None)

    res = await mobile_bridge.process_mobile_input(
        text=text,
        source=source,
        auto_execute=auto_execute,
        model=model,
    )
    return res


@mobile_router.post("/api/mobile/transfer")
async def post_transfer_soul(data: dict):
    """Manually toggles the soul transfer between mobile and desktop."""
    target = data.get("target", "mobile")
    return mobile_bridge.set_soul_location(target, reason="api_request")


@mobile_router.post("/api/mobile/macro")
async def post_mobile_macro(data: dict):
    """Executes a Windows macro action from phone."""
    action = data.get("action", "").strip().upper()
    if not action:
        raise HTTPException(status_code=400, detail="Missing 'action'")
    if mobile_bridge.macro_executor:
        mobile_bridge.macro_executor("hotkey", action)
        return {"status": "success", "executed": action}
    raise HTTPException(status_code=500, detail="Macro executor not bound")


@mobile_router.websocket("/ws/mobile")
async def websocket_mobile_endpoint(websocket: WebSocket):
    """Real-time bi-directional WebSocket connection for mobile phone clients."""
    await websocket.accept()
    websocket._loop = asyncio.get_running_loop()  # type: ignore
    mobile_bridge.register_client(websocket)

    client_ip = getattr(getattr(websocket, "client", None), "host", "mobile_device")

    try:
        # Send initial state sync to phone
        init_payload = {
            "type": "mobile_init",
            "soul_state": mobile_bridge.soul_location,
            "user_present": mobile_bridge.dream_watchdog.presence_detected,
            "idle_seconds": mobile_bridge.dream_watchdog.get_idle_seconds(),
            "system_prompt_version": mobile_bridge.prompt_manager.get_current_version(),
            "stats": mobile_bridge.memory_store.get_stats(),
            "recent_memories": mobile_bridge.memory_store.get_recent_interactions(5),
            "message": "Connected to Air-OS Companion Bridge over local Wi-Fi",
        }
        await websocket.send_text(json.dumps(init_payload, ensure_ascii=False))

        while True:
            raw_text = await websocket.receive_text()
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")

            if msg_type == "chat":
                text = data.get("text", "").strip()
                source = data.get("source", "mobile_text")
                auto_exec = data.get("auto_execute", True)
                model = data.get("model", None)
                if text:
                    await mobile_bridge.process_mobile_input(
                        text=text,
                        source=source,
                        auto_execute=auto_exec,
                        model=model,
                        client_ip=client_ip,
                    )

            elif msg_type == "transfer_soul":
                target = data.get("target", "mobile")
                mobile_bridge.set_soul_location(target, reason="mobile_ws_request")

            elif msg_type == "pc_action":
                act_name = data.get("action", "").strip().upper()
                if act_name and mobile_bridge.macro_executor:
                    mobile_bridge.macro_executor("hotkey", act_name)
                    logger.info(f"[MOBILE WS ACTION] Triggered PC macro: {act_name}")

            elif msg_type == "get_memories":
                recent = mobile_bridge.memory_store.get_recent_interactions(15)
                await websocket.send_text(
                    json.dumps(
                        {"type": "memory_store_data", "interactions": recent},
                        ensure_ascii=False,
                    )
                )

    except WebSocketDisconnect:
        mobile_bridge.unregister_client(websocket)
    except Exception as e:
        logger.error(f"Mobile WebSocket error: {e}")
        mobile_bridge.unregister_client(websocket)


# Standalone App Mode (Can be run directly via uvicorn or python)
app = FastAPI(title="Air-OS Mobile Wi-Fi Companion Bridge")
app.include_router(mobile_router)

if __name__ == "__main__":
    import uvicorn
    mobile_bridge.start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    finally:
        mobile_bridge.stop()

