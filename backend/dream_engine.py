"""
Air-OS Dream State & Self-Improvement Engine (Phase 6+)
======================================================
Autonomous background cognitive loop that:
1. Logs user interactions, workflow state, and timestamps to memory.json.
2. Monitors user presence via a webcam feed watchdog (OpenCV / MediaPipe).
3. Enters a "Dream State" when zero hands/faces/activity are detected for 10+ minutes.
4. Uses local Ollama models (e.g. qwen2.5, llama3.2) to self-reflect on interaction
   memories, extract insights, and rewrite/optimize its core system behavior prompt.
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# Ensure UTF-8 output encoding for Windows command line shells
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import ollama
except ImportError:
    ollama = None

class SafeStreamHandler(logging.StreamHandler):
    """Prevents Windows cp1252 charmap encoding errors when logging unicode or emojis."""
    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream or sys.stdout
            enc = getattr(stream, "encoding", None) or "utf-8"
            safe_msg = msg.encode(enc, errors="replace").decode(enc, errors="replace")
            stream.write(safe_msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

logger = logging.getLogger("dream-engine")
if not logger.handlers:
    handler = SafeStreamHandler(sys.stdout)
    formatter = logging.Formatter("[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "memory.json")
SYSTEM_PROMPT_FILE = os.path.join(BASE_DIR, "system_prompt.txt")
PROMPT_HISTORY_DIR = os.path.join(BASE_DIR, "prompt_history")
DREAM_LOG_FILE = os.path.join(BASE_DIR, "dream_log.json")

DEFAULT_SYSTEM_PROMPT = """You are Air-OS, an intelligent Windows 11 AI assistant powered by local vision, voice, and hand gestures.
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


# =============================================================================
# 1. Memory Store (memory.json)
# =============================================================================
class MemoryStore:
    """Manages the persistent memory.json store for interaction snippets and state."""

    def __init__(self, filepath: str = MEMORY_FILE):
        self.filepath = filepath
        self.lock = threading.RLock()
        self._ensure_storage()

    def _ensure_storage(self):
        if not os.path.exists(self.filepath):
            initial_data = {
                "version": "1.0.0",
                "created_at": datetime.datetime.now().isoformat(),
                "last_updated": datetime.datetime.now().isoformat(),
                "total_interactions": 0,
                "dream_cycles_completed": 0,
                "interactions": [],
            }
            self._write_raw(initial_data)

    def _read_raw(self) -> Dict[str, Any]:
        with self.lock:
            try:
                if os.path.exists(self.filepath):
                    with open(self.filepath, "r", encoding="utf-8") as f:
                        return json.load(f)
            except Exception as e:
                logger.error(f"Error reading memory store at {self.filepath}: {e}")
            return {
                "version": "1.0.0",
                "total_interactions": 0,
                "dream_cycles_completed": 0,
                "interactions": [],
            }

    def _write_raw(self, data: Dict[str, Any]):
        with self.lock:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Error writing memory store to {self.filepath}: {e}")

    def log_interaction(
        self,
        prompt: str,
        response: str,
        source: str = "chat",
        workflow_state: Optional[Dict[str, Any]] = None,
        actions: Optional[List[Dict[str, Any]]] = None,
        model: str = "local",
    ) -> Dict[str, Any]:
        """Logs a single interaction snippet into memory.json."""
        data = self._read_raw()
        now_iso = datetime.datetime.now().isoformat()
        interaction_id = f"mem_{int(time.time() * 1000)}"

        entry = {
            "id": interaction_id,
            "timestamp": now_iso,
            "source": source,  # 'chat', 'vision', 'voice', 'macro'
            "prompt": prompt,
            "response": response,
            "model": model,
            "actions": actions or [],
            "workflow_state": workflow_state or {},
            "analyzed_in_dream": False,
        }

        data.setdefault("interactions", []).append(entry)
        data["total_interactions"] = len(data["interactions"])
        data["last_updated"] = now_iso
        self._write_raw(data)
        logger.info(f"[MEMORY LOGGED] ({source.upper()} | id={interaction_id}) Prompt: {prompt[:40]}...")
        return entry

    def get_unanalyzed_interactions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns interactions that have not yet been processed in a dream cycle."""
        data = self._read_raw()
        interactions = data.get("interactions", [])
        unanalyzed = [i for i in interactions if not i.get("analyzed_in_dream", False)]
        return unanalyzed[-limit:]

    def get_recent_interactions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the most recent interactions."""
        data = self._read_raw()
        return data.get("interactions", [])[-limit:]

    def mark_interactions_analyzed(self, interaction_ids: List[str], dream_id: str):
        """Flags interactions as analyzed by a specific dream cycle."""
        data = self._read_raw()
        id_set = set(interaction_ids)
        for i in data.get("interactions", []):
            if i.get("id") in id_set:
                i["analyzed_in_dream"] = True
                i["dream_cycle_id"] = dream_id
        data["dream_cycles_completed"] = data.get("dream_cycles_completed", 0) + 1
        data["last_dream_at"] = datetime.datetime.now().isoformat()
        self._write_raw(data)

    def get_stats(self) -> Dict[str, Any]:
        data = self._read_raw()
        interactions = data.get("interactions", [])
        unanalyzed_count = sum(1 for i in interactions if not i.get("analyzed_in_dream", False))
        return {
            "total_interactions": len(interactions),
            "unanalyzed_interactions": unanalyzed_count,
            "dream_cycles_completed": data.get("dream_cycles_completed", 0),
            "last_updated": data.get("last_updated"),
            "last_dream_at": data.get("last_dream_at"),
        }


# =============================================================================
# 2. System Prompt Manager
# =============================================================================
class SystemPromptManager:
    """Manages reading, writing, and versioning of the self-optimized system prompt."""

    def __init__(self, prompt_file: str = SYSTEM_PROMPT_FILE, history_dir: str = PROMPT_HISTORY_DIR):
        self.prompt_file = prompt_file
        self.history_dir = history_dir
        self.lock = threading.RLock()
        self._ensure_storage()

    def _ensure_storage(self):
        os.makedirs(self.history_dir, exist_ok=True)
        if not os.path.exists(self.prompt_file):
            with open(self.prompt_file, "w", encoding="utf-8") as f:
                f.write(DEFAULT_SYSTEM_PROMPT.strip())

    def get_current_prompt(self) -> str:
        with self.lock:
            if os.path.exists(self.prompt_file):
                try:
                    with open(self.prompt_file, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            return content
                except Exception as e:
                    logger.error(f"Error reading system prompt from {self.prompt_file}: {e}")
            return DEFAULT_SYSTEM_PROMPT.strip()

    def update_prompt(self, new_prompt: str, dream_id: str, insights: List[str]) -> int:
        """Archives current prompt and writes the newly optimized prompt."""
        with self.lock:
            current = self.get_current_prompt()
            # Determine next version
            existing_history = [f for f in os.listdir(self.history_dir) if f.startswith("prompt_v")]
            next_version = len(existing_history) + 1

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_filename = f"prompt_v{next_version - 1}_{timestamp}.txt"
            archive_path = os.path.join(self.history_dir, archive_filename)

            # Archive old prompt
            try:
                with open(archive_path, "w", encoding="utf-8") as f:
                    f.write(f"# ARCHIVED PROMPT v{next_version - 1} | Dream ID: {dream_id}\n")
                    f.write(f"# Timestamp: {timestamp}\n")
                    f.write(f"# Pre-Dream Insights: {json.dumps(insights)}\n\n")
                    f.write(current)
            except Exception as e:
                logger.error(f"Error archiving old prompt to {archive_path}: {e}")

            # Write new prompt (ensure core action tags are always preserved)
            if "[ACTION:" not in new_prompt:
                bullets = "\n".join([f"- {ins}" for ins in insights])
                final_prompt = f"""{DEFAULT_SYSTEM_PROMPT.strip()}

### LEARNED USER BEHAVIORS & PREFERENCES (Self-Optimized via Dream State):
{bullets}
Adaptive context: {new_prompt.strip()}"""
            else:
                final_prompt = new_prompt.strip()

            try:
                with open(self.prompt_file, "w", encoding="utf-8") as f:
                    f.write(final_prompt)
                logger.info(f"[PROMPT UPDATED] Self-optimized prompt saved as Version {next_version}")
                return next_version
            except Exception as e:
                logger.error(f"Error saving new prompt to {self.prompt_file}: {e}")
                return next_version

    def get_current_version(self) -> int:
        with self.lock:
            try:
                if os.path.exists(self.history_dir):
                    existing = [f for f in os.listdir(self.history_dir) if f.startswith("prompt_v")]
                    return len(existing) + 1
            except Exception:
                pass
            return 1

    def get_history(self) -> List[Dict[str, Any]]:
        with self.lock:
            history = []
            if os.path.exists(self.history_dir):
                for fname in sorted(os.listdir(self.history_dir), reverse=True):
                    fpath = os.path.join(self.history_dir, fname)
                    if os.path.isfile(fpath) and fname.endswith(".txt"):
                        history.append({
                            "filename": fname,
                            "modified_at": datetime.datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat(),
                            "size": os.path.getsize(fpath),
                        })
            return history


# =============================================================================
# 3. Dream State & Self-Improvement Engine
# =============================================================================
class DreamStateEngine:
    """Invokes local Ollama models during the dream cycle to optimize core prompt."""

    def __init__(
        self,
        memory_store: MemoryStore,
        prompt_manager: SystemPromptManager,
        dream_log_file: str = DREAM_LOG_FILE,
        default_model: str = "qwen2.5:0.5b",
    ):
        self.memory_store = memory_store
        self.prompt_manager = prompt_manager
        self.dream_log_file = dream_log_file
        self.default_model = default_model
        self.is_dreaming = False
        self.last_dream_summary: Optional[Dict[str, Any]] = None

    def _select_best_ollama_model(self) -> str:
        """Finds the best available local Ollama text model for self-reflection."""
        if not ollama:
            return self.default_model
        try:
            client = ollama.Client(host="http://127.0.0.1:11434")
            models_info = client.list()
            installed = [m.model for m in models_info.models]

            # Preferred order of capable reasoning/text models (fast models first for interactive speed)
            candidates = [
                "qwen2.5:0.5b",
                "moondream:1.8b",
                "qwen2.5-coder:7b",
                "deepseek-r1:8b",
                "gemma4:e4b",
                "north-mini-code:fast",
            ]
            for c in candidates:
                for inst in installed:
                    if inst.startswith(c) or c in inst:
                        return inst
            if installed:
                return installed[0]
        except Exception as e:
            logger.warning(f"Could not auto-detect Ollama model, using default: {e}")
        return self.default_model

    def run_dream_cycle(self, model: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """Runs one Dream State cycle to analyze memory and self-optimize system behavior."""
        if self.is_dreaming:
            return {"status": "in_progress", "message": "A dream cycle is already active."}

        unanalyzed = self.memory_store.get_unanalyzed_interactions(limit=40)
        if not unanalyzed and not force:
            logger.info("[DREAM STATE] No new unanalyzed memories found. Skipping cycle.")
            return {"status": "skipped", "reason": "No unanalyzed memories in store."}

        self.is_dreaming = True
        dream_id = f"dream_{int(time.time())}"
        start_time = time.time()
        logger.info(f"[DREAM] >>> ENTERING DREAM STATE ({dream_id}) <<<")
        logger.info(f"[DREAM STATE] Analyzing {len(unanalyzed)} interaction memory records...")

        chosen_model = model or self._select_best_ollama_model()
        current_prompt = self.prompt_manager.get_current_prompt()

        # Format memories for the LLM
        memories_text = []
        for idx, mem in enumerate(unanalyzed, 1):
            actions_summary = ", ".join([f"{a.get('type')}:{a.get('payload')}" for a in mem.get("actions", [])])
            memories_text.append(
                f"[{idx}] Source: {mem.get('source')} | Time: {mem.get('timestamp')}\n"
                f"    User: {mem.get('prompt')}\n"
                f"    Assistant: {mem.get('response')}\n"
                f"    Actions Executed: {actions_summary or 'None'}"
            )
        formatted_memories = "\n".join(memories_text) if memories_text else "No recent interaction logs available."

        reflection_prompt = f"""Analyze recent Air-OS interactions:
{formatted_memories}

Generate JSON with keys:
- insights: list of 2-3 strings
- summary: string
- optimized_system_prompt: string preserving [ACTION:OPEN_URL:...], [ACTION:OPEN_APP:...], [ACTION:HOTKEY:...]

JSON:"""

        insights: List[str] = []
        summary: str = "Prompt optimized based on interaction memories."
        optimized_prompt: str = current_prompt

        try:
            if not ollama:
                raise RuntimeError("The 'ollama' Python library is not installed.")

            client = ollama.Client(host="http://127.0.0.1:11434")
            logger.info(f"[DREAM STATE] Invoking Ollama model '{chosen_model}' for cognitive synthesis...")
            response = client.chat(
                model=chosen_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a JSON-only assistant.",
                    },
                    {"role": "user", "content": reflection_prompt},
                ],
                options={"temperature": 0.3, "num_predict": 220},
            )

            if hasattr(response, "message") and hasattr(response.message, "content"):
                raw_reply = response.message.content.strip()
            elif isinstance(response, dict):
                raw_reply = response.get("message", {}).get("content", "").strip()
            else:
                raw_reply = str(response).strip()

            # Clean JSON markdown fences if present
            cleaned_reply = re.sub(r"^```(?:json)?\s*", "", raw_reply, flags=re.MULTILINE)
            cleaned_reply = re.sub(r"\s*```$", "", cleaned_reply, flags=re.MULTILINE).strip()

            parsed = {}
            try:
                parsed = json.loads(cleaned_reply)
            except Exception:
                # Fallback: extract json block via regex
                json_match = re.search(r"\{.*\}", raw_reply, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group(0))

            if parsed and isinstance(parsed, dict):
                insights = parsed.get("insights", [])
                summary = parsed.get("summary", summary)
                new_prompt_candidate = parsed.get("optimized_system_prompt", "").strip()
                if new_prompt_candidate and len(new_prompt_candidate) > 40:
                    optimized_prompt = new_prompt_candidate

        except Exception as e:
            logger.error(f"[DREAM STATE] Error during Ollama self-improvement inference: {e}")
            insights = [f"Dream inference notice: {str(e)}"]
            summary = "Dream cycle completed with heuristic memory synthesis."

        # If insights were empty, extract simple heuristics
        if not insights:
            insights = [
                f"Processed {len(unanalyzed)} interactions across chat, vision, and gesture channels.",
                "Ensured prompt retains zero-latency Windows action tag execution.",
            ]

        # Update prompt and mark memories
        new_version = self.prompt_manager.update_prompt(optimized_prompt, dream_id, insights)
        interaction_ids = [m["id"] for m in unanalyzed]
        self.memory_store.mark_interactions_analyzed(interaction_ids, dream_id)

        duration = round(time.time() - start_time, 2)
        dream_record = {
            "dream_id": dream_id,
            "timestamp": datetime.datetime.now().isoformat(),
            "duration_seconds": duration,
            "model_used": chosen_model,
            "memories_analyzed": len(unanalyzed),
            "new_prompt_version": new_version,
            "summary": summary,
            "insights": insights,
            "prompt_length": len(optimized_prompt),
        }

        self._append_dream_log(dream_record)
        self.last_dream_summary = dream_record
        self.is_dreaming = False

        logger.info(f"[DREAM] >>> DREAM STATE COMPLETED in {duration}s! (Prompt v{new_version}) <<<")
        logger.info(f"[DREAM INSIGHTS]: {json.dumps(insights, indent=2, ensure_ascii=True)}")
        return {
            "status": "success",
            "dream_id": dream_id,
            "duration_seconds": duration,
            "model": chosen_model,
            "new_version": new_version,
            "summary": summary,
            "insights": insights,
        }

    def _append_dream_log(self, record: Dict[str, Any]):
        try:
            logs = []
            if os.path.exists(self.dream_log_file):
                with open(self.dream_log_file, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            logs.append(record)
            with open(self.dream_log_file, "w", encoding="utf-8") as f:
                json.dump(logs[-50:], f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error appending to dream log: {e}")

    def get_dream_history(self) -> List[Dict[str, Any]]:
        if os.path.exists(self.dream_log_file):
            try:
                with open(self.dream_log_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []


# =============================================================================
# 4. Background Webcam Watchdog
# =============================================================================
class DreamWatchdog:
    """Monitors webcam presence (hands/faces/activity).

    If zero presence is detected for >= idle_threshold_seconds, triggers Dream Cycle.
    """

    def __init__(
        self,
        dream_engine: DreamStateEngine,
        idle_threshold_seconds: float = 600.0,  # 10 minutes default
        poll_interval: float = 2.0,
    ):
        self.dream_engine = dream_engine
        self.idle_threshold_seconds = idle_threshold_seconds
        self.poll_interval = poll_interval
        self.last_presence_time = time.time()
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.presence_detected = False
        self.lock = threading.RLock()

    def notify_presence(self, detected: bool = True):
        """Called by camera/vision loop when user hands, faces, or motion are detected."""
        with self.lock:
            self.presence_detected = detected
            if detected:
                self.last_presence_time = time.time()

    def get_idle_seconds(self) -> float:
        with self.lock:
            return round(time.time() - self.last_presence_time, 1)

    def get_status(self) -> Dict[str, Any]:
        idle_sec = self.get_idle_seconds()
        threshold = self.idle_threshold_seconds
        remaining = max(0.0, round(threshold - idle_sec, 1))
        return {
            "watchdog_active": self.running,
            "presence_detected": self.presence_detected,
            "idle_seconds": idle_sec,
            "idle_threshold_seconds": threshold,
            "seconds_until_dream": remaining,
            "is_dreaming": self.dream_engine.is_dreaming,
            "last_presence_time": datetime.datetime.fromtimestamp(self.last_presence_time).isoformat(),
            "latest_dream_summary": self.dream_engine.last_dream_summary,
        }

    def start(self):
        """Starts the background watchdog loop thread."""
        if self.running:
            return
        self.running = True
        self.last_presence_time = time.time()
        self.thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="DreamWatchdog")
        self.thread.start()
        logger.info(f"[WATCHDOG STARTED] Monitoring user presence (Idle threshold: {self.idle_threshold_seconds}s)")

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        logger.info("[WATCHDOG STOPPED]")

    def _watchdog_loop(self):
        while self.running:
            time.sleep(self.poll_interval)
            idle_sec = self.get_idle_seconds()

            if idle_sec >= self.idle_threshold_seconds and not self.dream_engine.is_dreaming:
                logger.info(
                    f"[WATCHDOG TRIGGER] Zero presence detected for {idle_sec:.1f}s "
                    f"(>= {self.idle_threshold_seconds}s threshold). Initiating Dream State!"
                )
                try:
                    self.dream_engine.run_dream_cycle()
                except Exception as e:
                    logger.error(f"Watchdog dream cycle error: {e}")
                # Reset presence timer to avoid rapid repeated dreams without new activity
                with self.lock:
                    self.last_presence_time = time.time()


# Singleton Instances
memory_store = MemoryStore()
prompt_manager = SystemPromptManager()
dream_engine = DreamStateEngine(memory_store=memory_store, prompt_manager=prompt_manager)
dream_watchdog = DreamWatchdog(dream_engine=dream_engine, idle_threshold_seconds=600.0)


# =============================================================================
# Standalone CLI Interface
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Air-OS Dream State & Self-Improvement Cognitive Engine")
    parser.add_argument("--idle-seconds", type=float, default=10.0, help="Idle timeout before dream triggers (default: 10s for testing)")
    parser.add_argument("--trigger-now", action="store_true", help="Manually trigger an immediate Dream cycle")
    parser.add_argument("--add-sample", action="store_true", help="Add mock user interaction memories to test reflection")
    parser.add_argument("--model", type=str, default="qwen2.5:0.5b", help="Ollama model to use")
    parser.add_argument("--show-prompt", action="store_true", help="Print current active system prompt")
    parser.add_argument("--show-stats", action="store_true", help="Print memory & dream stats")
    parser.add_argument("--watchdog", action="store_true", help="Start the interactive presence watchdog monitoring loop")
    args = parser.parse_args()

    print("=" * 70)
    print("[*] Air-OS Dream State & Self-Improvement Cognitive Subsystem")
    print("=" * 70)

    if args.add_sample:
        print("[+] Adding sample interaction memories to memory.json...")
        memory_store.log_interaction(
            prompt="Can you open Spotify and play my study playlist?",
            response="Opening Spotify for you! [ACTION:OPEN_URL:https://open.spotify.com]",
            source="chat",
            actions=[{"type": "url", "payload": "https://open.spotify.com"}],
            workflow_state={"hands_count": 1, "active_gesture": "MOVE"},
        )
        memory_store.log_interaction(
            prompt="Mute the audio please, I am taking a phone call.",
            response="Muted your Windows audio output! [ACTION:HOTKEY:MUTE]",
            source="voice",
            actions=[{"type": "hotkey", "payload": "MUTE"}],
            workflow_state={"hands_count": 0, "active_gesture": "IDLE"},
        )
        memory_store.log_interaction(
            prompt="What is on my screen right now?",
            response="I see a code editor and terminal running python uvicorn on port 8000.",
            source="vision",
            workflow_state={"camera_enabled": True},
        )
        print("[+] Sample interactions logged successfully.")

    if args.show_stats:
        stats = memory_store.get_stats()
        print(f"Memory Stats: {json.dumps(stats, indent=2, ensure_ascii=True)}")

    if args.show_prompt:
        prompt = prompt_manager.get_current_prompt()
        print(f"\n--- ACTIVE SYSTEM BEHAVIOR PROMPT ---\n{prompt}\n--------------------------------------")

    if args.trigger_now:
        print(f"\n[+] Triggering immediate Dream State using model '{args.model}'...")
        res = dream_engine.run_dream_cycle(model=args.model, force=True)
        print(f"\n[+] Dream Result:\n{json.dumps(res, indent=2, ensure_ascii=True)}")
        return

    if not args.watchdog and (args.show_prompt or args.show_stats or args.add_sample):
        return

    # Run Watchdog Test Loop
    print(f"\n[+] Starting Watchdog Loop (Idle Timeout: {args.idle_seconds}s)...")
    print("[+] Press Ctrl+C to stop.\n")
    watchdog = DreamWatchdog(dream_engine=dream_engine, idle_threshold_seconds=args.idle_seconds, poll_interval=1.0)
    watchdog.start()

    try:
        while True:
            status = watchdog.get_status()
            print(
                f"\r[WATCHDOG STATUS] Idle: {status['idle_seconds']}s / {status['idle_threshold_seconds']}s "
                f"| Until Dream: {status['seconds_until_dream']}s | Dreaming: {status['is_dreaming']}",
                end="",
                flush=True,
            )
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[!] Stopping Watchdog...")
        watchdog.stop()


if __name__ == "__main__":
    main()
