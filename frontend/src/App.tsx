import { useState, useEffect, useRef, useCallback } from 'react';
import {
  ShieldCheck,
  ShieldAlert,
  Camera,
  CameraOff,
  PictureInPicture2,
  Lock,
  Monitor,
  Volume2,
  VolumeX,
  Smartphone,
  Sparkles,
  Brain,
  Moon,
  Mic,
  Send,
  Eye,
  RefreshCw,
  Play,
  Download,
  Zap,
} from 'lucide-react';

export interface DreamStatus {
  watchdog: {
    watchdog_active: boolean;
    presence_detected: boolean;
    idle_seconds: number;
    idle_threshold_seconds: number;
    seconds_until_dream: number;
    is_dreaming: boolean;
    last_presence_time: string;
    latest_dream_summary?: {
      summary?: string;
      insights?: string[];
      duration_seconds?: number;
      new_prompt_version?: number;
      timestamp?: string;
    };
  };
  stats: {
    total_interactions: number;
    unanalyzed_interactions: number;
    dream_cycles_completed: number;
    last_updated: string;
    last_dream_at?: string;
  };
  current_prompt_version: number;
  prompt_versions_count: number;
  active_model: string;
}

export interface OllamaModelInfo {
  name: string;
  model: string;
  size: number;
  parameter_size: string;
  family: string;
  has_vision: boolean;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  image?: string;
  actions?: Array<{ type: string; payload: string; raw?: string; executed?: boolean }>;
  source?: 'desktop_text' | 'desktop_voice' | 'mobile_voice' | 'mobile_text' | 'vision';
}

export interface CustomGesture {
  id: string;
  name: string;
  action_type: 'url' | 'shell' | 'hotkey';
  action_payload: string;
  tolerance?: number;
  trigger_count: number;
  last_triggered?: string | null;
}

// Dynamic API base for both Vite dev server (5173) and production FastAPI (8000)
const getApiBase = () => {
  if (window.location.port === '8000') {
    return '';
  }
  const protocol = window.location.protocol === 'https:' ? 'https:' : 'https:';
  const host = window.location.hostname || 'localhost';
  return `${protocol}//${host}:8000`;
};
const API_BASE = getApiBase();

export default function App() {
  // --------------------------------------------------------------------------
  // Core Tracking & State
  // --------------------------------------------------------------------------
  const [isArmed, setIsArmed] = useState<boolean>(false);
  const [cameraEnabled, setCameraEnabled] = useState<boolean>(true);
  const [fps, setFps] = useState<number>(0);
  const [currentGesture, setCurrentGesture] = useState<string>('IDLE');
  const [handDetected, setHandDetected] = useState<boolean>(false);
  const [handsCount, setHandsCount] = useState<number>(0);
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number }>({ x: 0.5, y: 0.5 });
  const [activeCombo, setActiveCombo] = useState<string>('NONE');
  const [activeMacro, setActiveMacro] = useState<string>('NONE');
  const [backendStatus, setBackendStatus] = useState<'online' | 'offline' | 'connecting'>('connecting');

  // Mobile Wi-Fi Bridge Sync
  const [soulState, setSoulState] = useState<'desktop' | 'mobile'>('desktop');
  const [userPresent, setUserPresent] = useState<boolean>(true);
  const [idleSeconds, setIdleSeconds] = useState<number>(0);
  const [phoneClientsCount, setPhoneClientsCount] = useState<number>(0);

  // AI & Ollama State
  const [ollamaStatus, setOllamaStatus] = useState<'online' | 'offline' | 'checking'>('checking');
  const [ollamaModels, setOllamaModels] = useState<OllamaModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('qwen2.5:0.5b');
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([
    {
      id: 'init-1',
      role: 'assistant',
      content:
        'Air-OS Autonomous Spatial Engine online. Vision, local Ollama intelligence, and mobile Wi-Fi soul bridge are active.',
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    },
  ]);
  const [inputPrompt, setInputPrompt] = useState<string>('');
  const [isAiLoading, setIsAiLoading] = useState<boolean>(false);
  const [ttsEnabled, setTtsEnabled] = useState<boolean>(true);
  const [isListeningMic, setIsListeningMic] = useState<boolean>(false);

  // Dream State & Memory
  const [dreamStatus, setDreamStatus] = useState<DreamStatus | null>(null);
  const [isDreaming, setIsDreaming] = useState<boolean>(false);
  const [recentMemories, setRecentMemories] = useState<any[]>([]);

  // Custom Gestures & PWA
  const [customGestures, setCustomGestures] = useState<CustomGesture[]>([]);
  const [isPiPActive, setIsPiPActive] = useState<boolean>(false);
  const [deferredPrompt, setDeferredPrompt] = useState<any>(null);

  // Refs
  const wsRef = useRef<WebSocket | null>(null);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const speechRecognitionRef = useRef<any>(null);
  const pipCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const pipVideoRef = useRef<HTMLVideoElement | null>(null);

  // --------------------------------------------------------------------------
  // 1. WebSocket Telemetry & Control Bridge
  // --------------------------------------------------------------------------
  useEffect(() => {
    let reconnectTimeout: any = null;

    const connectWS = () => {
      let wsUrl = '';
      if (window.location.port === '8000') {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        wsUrl = `${protocol}//${window.location.host}/ws/control`;
      } else {
        const host = window.location.hostname || 'localhost';
        wsUrl = `wss://${host}:8000/ws/control`;
      }

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setBackendStatus('online');
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'telemetry') {
            setFps(Math.round(data.fps || 0));
            setCurrentGesture(data.gesture || 'IDLE');
            setHandDetected(!!data.hand_detected);
            setHandsCount(data.hands_count || 0);
            if (data.cursor) {
              setCursorPos({ x: data.cursor.x, y: data.cursor.y });
            }
            if (data.is_armed !== undefined) setIsArmed(data.is_armed);
            if (data.camera_enabled !== undefined) setCameraEnabled(data.camera_enabled);
            if (data.active_combo) setActiveCombo(data.active_combo);
            if (data.active_macro) setActiveMacro(data.active_macro);
          } else if (data.type === 'ready') {
            setFps(Math.round(data.fps || 0));
            setIsArmed(!!data.is_armed);
            setCameraEnabled(data.camera_enabled !== false);
            if (data.gestures) setCustomGestures(data.gestures);
          } else if (data.type === 'TRANSFER_SOUL') {
            setSoulState('mobile');
            setUserPresent(false);
          } else if (data.type === 'RETURN_SOUL') {
            setSoulState('desktop');
            setUserPresent(true);
          } else if (data.type === 'chat_reply') {
            // Live interaction synced from mobile or desktop
            setChatMessages((prev) => [
              ...prev,
              {
                id: data.id || `msg-${Date.now()}`,
                role: 'assistant',
                content: data.response,
                timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
                actions: data.actions || [],
                source: data.source,
              },
            ]);
            if (ttsEnabled && data.response) {
              speakText(data.response);
            }
          }
        } catch {}
      };

      ws.onclose = () => {
        setBackendStatus('offline');
        reconnectTimeout = setTimeout(connectWS, 2500);
      };

      ws.onerror = () => {
        ws.close();
      };
    };

    connectWS();

    // Listen for PWA Install Prompt
    const handleBeforeInstall = (e: any) => {
      e.preventDefault();
      setDeferredPrompt(e);
    };
    window.addEventListener('beforeinstallprompt', handleBeforeInstall);

    return () => {
      clearTimeout(reconnectTimeout);
      if (wsRef.current) wsRef.current.close();
      window.removeEventListener('beforeinstallprompt', handleBeforeInstall);
    };
  }, [ttsEnabled]);

  // --------------------------------------------------------------------------
  // 2. Periodic Polling for Dream State, Memories & Mobile Status
  // --------------------------------------------------------------------------
  const pollData = useCallback(async () => {
    try {
      // 1. Dream State
      const dreamRes = await fetch(`${API_BASE}/api/dream/status`);
      if (dreamRes.ok) {
        const d = await dreamRes.json();
        setDreamStatus(d);
        setIsDreaming(!!d.watchdog?.is_dreaming);
        setUserPresent(!!d.watchdog?.presence_detected);
        setIdleSeconds(d.watchdog?.idle_seconds || 0);
      }

      // 2. Mobile Bridge Status
      const mobRes = await fetch(`${API_BASE}/api/mobile/status`);
      if (mobRes.ok) {
        const m = await mobRes.json();
        setSoulState(m.soul_location || 'desktop');
        setPhoneClientsCount(m.connected_mobile_clients || 0);
      }

      // 3. Recent Memories
      const memRes = await fetch(`${API_BASE}/api/dream/memory`);
      if (memRes.ok) {
        const mem = await memRes.json();
        if (mem.interactions) {
          setRecentMemories(mem.interactions.slice(-10).reverse());
        }
      }
    } catch {}
  }, []);

  useEffect(() => {
    pollData();
    const interval = setInterval(pollData, 3000);
    return () => clearInterval(interval);
  }, [pollData]);

  // --------------------------------------------------------------------------
  // 3. Ollama Models Discovery
  // --------------------------------------------------------------------------
  useEffect(() => {
    const fetchOllama = async () => {
      setOllamaStatus('checking');
      try {
        const res = await fetch(`${API_BASE}/api/ollama/models`);
        if (res.ok) {
          const data = await res.json();
          if (data.status === 'online' && data.models?.length > 0) {
            setOllamaStatus('online');
            setOllamaModels(data.models);
            if (!data.models.some((m: OllamaModelInfo) => m.name === selectedModel)) {
              setSelectedModel(data.models[0].name);
            }
            return;
          }
        }
        setOllamaStatus('offline');
      } catch {
        setOllamaStatus('offline');
      }
    };
    fetchOllama();
  }, []);

  // Auto-scroll Chat
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chatMessages]);

  // --------------------------------------------------------------------------
  // 4. User Actions & Hardware Controls
  // --------------------------------------------------------------------------
  const sendWs = (msg: any) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  };

  const toggleArm = () => {
    sendWs({ type: 'toggle_arm' });
    setIsArmed(!isArmed);
  };

  const toggleCamera = () => {
    sendWs({ type: 'toggle_camera' });
    setCameraEnabled(!cameraEnabled);
  };

  const toggleSoulTransfer = async () => {
    const target = soulState === 'desktop' ? 'mobile' : 'desktop';
    setSoulState(target);
    try {
      await fetch(`${API_BASE}/api/mobile/transfer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target }),
      });
    } catch {}
  };

  const triggerMacroAction = (key: string) => {
    sendWs({ type: 'hotkey', key });
  };

  const triggerDreamStateNow = async () => {
    setIsDreaming(true);
    try {
      await fetch(`${API_BASE}/api/dream/trigger`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: selectedModel }),
      });
      pollData();
    } catch {}
  };

  // Text-to-Speech
  const speakText = (text: string) => {
    if (!ttsEnabled || !('speechSynthesis' in window)) return;
    try {
      window.speechSynthesis.cancel();
      const clean = text.replace(/\[ACTION:[^\]]+\]/gi, '').trim();
      if (!clean) return;
      const utt = new SpeechSynthesisUtterance(clean);
      utt.rate = 1.05;
      window.speechSynthesis.speak(utt);
    } catch {}
  };

  // Send Text Query to AI
  const handleSendChat = async (promptOverride?: string) => {
    const prompt = (promptOverride || inputPrompt).trim();
    if (!prompt || isAiLoading) return;
    setInputPrompt('');

    const userMsg: ChatMessage = {
      id: `usr-${Date.now()}`,
      role: 'user',
      content: prompt,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      source: 'desktop_text',
    };
    setChatMessages((prev) => [...prev, userMsg]);
    setIsAiLoading(true);

    try {
      const res = await fetch(`${API_BASE}/api/ollama/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: selectedModel,
          prompt,
          auto_execute: true,
        }),
      });

      if (res.ok) {
        const data = await res.json();
        const assistantText = data.message?.content || 'Acknowledged.';
        const assistantMsg: ChatMessage = {
          id: `asst-${Date.now()}`,
          role: 'assistant',
          content: assistantText,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          actions: data.actions || [],
        };
        setChatMessages((prev) => [...prev, assistantMsg]);
        if (ttsEnabled) speakText(assistantText);
      } else {
        setChatMessages((prev) => [
          ...prev,
          {
            id: `err-${Date.now()}`,
            role: 'assistant',
            content: `Local Ollama error (HTTP ${res.status}). Model ${selectedModel} might be offline or busy.`,
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          },
        ]);
      }
    } catch (err: any) {
      setChatMessages((prev) => [
        ...prev,
        {
          id: `err-${Date.now()}`,
          role: 'assistant',
          content: `Failed to connect to backend: ${err.message}`,
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ]);
    } finally {
      setIsAiLoading(false);
      pollData();
    }
  };

  // Snap & Ask Webcam Vision
  const handleSnapAndAsk = async () => {
    if (isAiLoading) return;
    setIsAiLoading(true);

    const userMsg: ChatMessage = {
      id: `usr-vision-${Date.now()}`,
      role: 'user',
      content: '📸 [Webcam Snapshot Analysis Requested]',
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      source: 'vision',
    };
    setChatMessages((prev) => [...prev, userMsg]);

    try {
      const res = await fetch(`${API_BASE}/api/vision/capture`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prompt: 'Analyze what you see in front of my webcam. Describe hands, posture, and items.',
          model: selectedModel,
        }),
      });

      if (res.ok) {
        const data = await res.json();
        setChatMessages((prev) => [
          ...prev,
          {
            id: `asst-vision-${Date.now()}`,
            role: 'assistant',
            content: data.analysis || 'Vision analysis complete.',
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
            image: data.image_base64,
          },
        ]);
        if (ttsEnabled && data.analysis) speakText(data.analysis);
      }
    } catch {}
    setIsAiLoading(false);
    pollData();
  };

  // Voice Push-to-Talk (Web Speech API)
  const toggleVoiceInput = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SpeechRecognition) {
      alert('Speech recognition is not supported in this browser. Please type your command.');
      return;
    }

    if (isListeningMic) {
      if (speechRecognitionRef.current) speechRecognitionRef.current.stop();
      setIsListeningMic(false);
      return;
    }

    try {
      const recognition = new SpeechRecognition();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = 'en-US';

      recognition.onstart = () => setIsListeningMic(true);
      recognition.onresult = (event: any) => {
        const transcript = event.results[0][0].transcript;
        if (transcript) {
          handleSendChat(transcript);
        }
      };
      recognition.onerror = () => setIsListeningMic(false);
      recognition.onend = () => setIsListeningMic(false);

      speechRecognitionRef.current = recognition;
      recognition.start();
    } catch {
      setIsListeningMic(false);
    }
  };

  // Picture-in-Picture Popout HUD
  const togglePiP = async () => {
    try {
      if (document.pictureInPictureElement) {
        await document.exitPictureInPicture();
        setIsPiPActive(false);
        return;
      }

      if (pipCanvasRef.current && pipVideoRef.current) {
        const stream = pipCanvasRef.current.captureStream(30);
        pipVideoRef.current.srcObject = stream;
        await pipVideoRef.current.play();
        await pipVideoRef.current.requestPictureInPicture();
        setIsPiPActive(true);
      }
    } catch {}
  };

  // Install PWA
  const handleInstallApp = async () => {
    if (!deferredPrompt) return;
    deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    if (outcome === 'accepted') setDeferredPrompt(null);
  };

  // --------------------------------------------------------------------------
  // Render: Unified Luxury Cockpit
  // --------------------------------------------------------------------------
  return (
    <div className="flex flex-col h-screen w-screen bg-[#08090d] text-slate-100 overflow-hidden font-sans select-none">
      {/* Hidden elements for Picture-in-Picture */}
      <canvas ref={pipCanvasRef} width={320} height={180} className="hidden" />
      <video ref={pipVideoRef} muted className="hidden" />

      {/* TOP COMMAND DECK BAR */}
      <header className="h-14 border-b border-white/[0.08] bg-[#0c0e14]/90 backdrop-blur-md px-4 flex items-center justify-between shrink-0 z-30">
        {/* Brand & System Status */}
        <div className="flex items-center gap-3">
          <div className="relative flex items-center justify-center w-8 h-8 rounded-lg overflow-hidden border border-white/10 shadow-lg">
            <img src="/airos_icon.jpg" alt="Air-OS" className="w-full h-full object-cover" />
            <div className="absolute inset-0 bg-cyan-400/10 pointer-events-none" />
          </div>

          <div className="flex flex-col">
            <div className="flex items-center gap-2">
              <span className="font-display font-bold text-sm tracking-wide text-white">AIR-OS COCKPIT</span>
              <span className="text-[10px] font-code font-semibold px-1.5 py-0.5 rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                v5.5 HYBRID
              </span>
            </div>
            <div className="flex items-center gap-2 text-[11px] text-slate-400 font-code">
              <span className="flex items-center gap-1">
                <span
                  className={`w-1.5 h-1.5 rounded-full ${backendStatus === 'online' ? 'bg-emerald-400 shadow-[0_0_6px_#34d399]' : 'bg-rose-400'}`}
                />
                {backendStatus === 'online' ? `${fps} FPS` : 'DISCONNECTED'}
              </span>
              <span>•</span>
              <span className="text-slate-300">
                {handsCount > 0 ? `${handsCount} Hand${handsCount > 1 ? 's' : ''}` : 'No Hands'}
              </span>
              <span>•</span>
              <span className="text-cyan-400 uppercase font-semibold">{currentGesture}</span>
              {activeCombo !== 'NONE' && (
                <>
                  <span>•</span>
                  <span className="text-purple-300 font-bold">{activeCombo}</span>
                </>
              )}
              {activeMacro !== 'NONE' && (
                <>
                  <span>•</span>
                  <span className="text-emerald-300 font-bold">{activeMacro}</span>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Center: Soul Location & State Transfer Hub */}
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-slate-900/60 border border-white/[0.06]">
          <div className="flex items-center gap-2">
            <div
              className={`w-2.5 h-2.5 rounded-full ${soulState === 'mobile' ? 'bg-purple-400 shadow-[0_0_10px_#c084fc] animate-pulse' : 'bg-cyan-400 shadow-[0_0_8px_#38bdf8]'}`}
            />
            <span className="text-xs font-semibold text-slate-200">
              {soulState === 'mobile' ? 'Soul Active on Mobile' : 'Soul Docked at PC'}
            </span>
          </div>

          <span className="text-[11px] font-code text-slate-400">
            {userPresent ? '• User at Desk' : `• User Away (${Math.round(idleSeconds)}s)`}
          </span>

          <button
            onClick={toggleSoulTransfer}
            className="ml-1 px-2.5 py-1 rounded-lg text-[11px] font-medium transition-all bg-white/[0.06] hover:bg-white/[0.12] text-slate-200 border border-white/10 active:scale-95"
            title="Toggle companion presence manually"
          >
            {soulState === 'desktop' ? 'Summon to Phone' : 'Dock to PC'}
          </button>
        </div>

        {/* Right Controls & Quick Hardware Toggles */}
        <div className="flex items-center gap-2">
          {/* Ollama Model Selector */}
          <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-900/80 border border-white/10 text-xs text-slate-300">
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                ollamaStatus === 'online' ? 'bg-emerald-400 shadow-[0_0_6px_#34d399]' : 'bg-rose-400'
              }`}
            />
            <Brain className="w-3.5 h-3.5 text-purple-400" />
            <select
              value={selectedModel}
              onChange={(e) => setSelectedModel(e.target.value)}
              className="bg-transparent text-xs font-code text-slate-200 outline-none cursor-pointer"
            >
              {ollamaModels.map((m) => (
                <option key={m.name} value={m.name} className="bg-slate-900 text-slate-200">
                  {m.name} {m.has_vision ? '👁️' : ''}
                </option>
              ))}
            </select>
          </div>

          {/* Wi-Fi Phone Bridge Pill */}
          <a
            href="https://192.168.31.18:8000"
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-code text-cyan-300 bg-cyan-500/10 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors"
            title="Open Mobile Companion on Wi-Fi"
          >
            <Smartphone className="w-3.5 h-3.5" />
            <span>:8000</span>
            {phoneClientsCount > 0 && (
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_#34d399]" />
            )}
          </a>

          {/* Camera Privacy Toggle */}
          <button
            onClick={toggleCamera}
            className={`p-1.5 rounded-lg border text-xs transition-colors ${
              cameraEnabled
                ? 'bg-slate-900/80 text-slate-300 border-white/10 hover:bg-slate-800'
                : 'bg-rose-500/10 text-rose-400 border-rose-500/30 shadow-[0_0_10px_rgba(244,63,94,0.2)]'
            }`}
            title={cameraEnabled ? 'Disable Camera Privacy' : 'Enable Camera'}
          >
            {cameraEnabled ? <Camera className="w-4 h-4" /> : <CameraOff className="w-4 h-4" />}
          </button>

          {/* Master Arm Control */}
          <button
            onClick={toggleArm}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold tracking-wide border transition-all ${
              isArmed
                ? 'bg-emerald-500/15 text-emerald-400 border-emerald-500/40 shadow-[0_0_12px_rgba(52,211,153,0.25)]'
                : 'bg-slate-900/80 text-slate-400 border-white/10 hover:text-slate-200'
            }`}
          >
            {isArmed ? <ShieldCheck className="w-4 h-4" /> : <ShieldAlert className="w-4 h-4" />}
            <span>{isArmed ? 'ARMED' : 'PAUSED'}</span>
          </button>

          {/* Speech Audio Toggle */}
          <button
            onClick={() => setTtsEnabled(!ttsEnabled)}
            className="p-1.5 rounded-lg border border-white/10 bg-slate-900/80 text-slate-300 hover:bg-slate-800 transition-colors"
            title={ttsEnabled ? 'Speech Synthesis Enabled' : 'Speech Synthesis Muted'}
          >
            {ttsEnabled ? <Volume2 className="w-4 h-4 text-cyan-400" /> : <VolumeX className="w-4 h-4 text-slate-500" />}
          </button>

          {/* PWA Install Button */}
          {deferredPrompt && (
            <button
              onClick={handleInstallApp}
              className="p-1.5 rounded-lg border border-cyan-500/30 bg-cyan-500/15 text-cyan-300 text-xs hover:bg-cyan-500/25 transition-all"
              title="Install Desktop App"
            >
              <Download className="w-4 h-4" />
            </button>
          )}

          {/* Picture-in-Picture Mini HUD */}
          <button
            onClick={togglePiP}
            className={`p-1.5 rounded-lg border transition-colors ${
              isPiPActive
                ? 'bg-cyan-500/20 text-cyan-300 border-cyan-500/40 shadow-[0_0_10px_rgba(0,240,255,0.2)]'
                : 'border-white/10 bg-slate-900/80 text-slate-400 hover:text-slate-200'
            }`}
            title="Pop out Mini Floating HUD"
          >
            <PictureInPicture2 className="w-4 h-4" />
          </button>
        </div>
      </header>

      {/* MAIN 3-ZONE COCKPIT (Zero clutter, everything on one page) */}
      <main className="flex-1 grid grid-cols-12 gap-3 p-3 overflow-hidden">
        {/* ================================================================== */}
        {/* DECK 1 (LEFT 3 COLS): Vision Feed, Spatial Tracking & Quick Macros */}
        {/* ================================================================== */}
        <section className="col-span-3 flex flex-col gap-3 h-full overflow-hidden">
          {/* Live Video Stream Card */}
          <div className="cyber-card rounded-2xl overflow-hidden flex flex-col shrink-0 border border-white/[0.08]">
            <div className="px-3 py-2 border-b border-white/[0.06] flex items-center justify-between text-xs text-slate-400 font-medium">
              <span className="flex items-center gap-1.5">
                <Camera className="w-3.5 h-3.5 text-cyan-400" />
                <span>Spatial DirectShow Feed</span>
              </span>
              <span className="font-code text-[11px] text-cyan-400">{cameraEnabled ? '640x360 @ 60' : 'MUTED'}</span>
            </div>

            <div className="relative aspect-video bg-black/60 overflow-hidden flex items-center justify-center">
              {cameraEnabled ? (
                <img
                  src={`${API_BASE}/api/video_feed`}
                  alt="Live Vision Stream"
                  className="w-full h-full object-cover"
                />
              ) : (
                <div className="flex flex-col items-center gap-2 text-slate-500">
                  <CameraOff className="w-8 h-8 stroke-[1.5]" />
                  <span className="text-xs">Camera Hardware Muted</span>
                </div>
              )}

              {/* Hand Knuckle Radar Reticle */}
              {handDetected && (
                <div
                  className="absolute w-3.5 h-3.5 -ml-1.5 -mt-1.5 rounded-full border-2 border-cyan-400 bg-cyan-400/40 pointer-events-none transition-all duration-75 shadow-[0_0_8px_#00f0ff]"
                  style={{
                    left: `${cursorPos.x * 100}%`,
                    top: `${cursorPos.y * 100}%`,
                  }}
                />
              )}
            </div>

            {/* Live Gesture Readout */}
            <div className="p-3 bg-[#0a0c12]/80 flex items-center justify-between border-t border-white/[0.04]">
              <div>
                <div className="text-[10px] uppercase font-semibold text-slate-400 tracking-wider">Detected Action</div>
                <div className="text-sm font-code font-bold text-white flex items-center gap-1.5 mt-0.5">
                  <Zap className="w-3.5 h-3.5 text-cyan-400" />
                  <span>{currentGesture}</span>
                </div>
              </div>
              <div className="text-right">
                <div className="text-[10px] uppercase font-semibold text-slate-400 tracking-wider">Knuckle Pos</div>
                <div className="text-xs font-code text-slate-300 mt-0.5">
                  {cursorPos.x.toFixed(2)} / {cursorPos.y.toFixed(2)}
                </div>
              </div>
            </div>
          </div>

          {/* Quick Hardware Actions Bar */}
          <div className="cyber-card rounded-2xl p-3 flex flex-col gap-2 shrink-0 border border-white/[0.08]">
            <div className="text-[11px] uppercase font-bold text-slate-400 tracking-wider flex items-center justify-between">
              <span>Instant PC Remote Actions</span>
              <span className="text-cyan-400 font-code font-normal">Direct</span>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <button
                onClick={() => triggerMacroAction('LOCK_PC')}
                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-slate-900/90 hover:bg-slate-800/90 text-xs font-medium text-slate-200 border border-white/[0.06] transition-all active:scale-95"
              >
                <Lock className="w-3.5 h-3.5 text-cyan-400" />
                <span>Lock PC</span>
              </button>

              <button
                onClick={() => triggerMacroAction('SHOW_DESKTOP')}
                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-slate-900/90 hover:bg-slate-800/90 text-xs font-medium text-slate-200 border border-white/[0.06] transition-all active:scale-95"
              >
                <Monitor className="w-3.5 h-3.5 text-purple-400" />
                <span>Desktop</span>
              </button>

              <button
                onClick={() => triggerMacroAction('MUTE')}
                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-slate-900/90 hover:bg-slate-800/90 text-xs font-medium text-slate-200 border border-white/[0.06] transition-all active:scale-95"
              >
                <VolumeX className="w-3.5 h-3.5 text-rose-400" />
                <span>Mute Audio</span>
              </button>

              <button
                onClick={() => triggerMacroAction('PLAY_PAUSE')}
                className="flex items-center gap-2 px-3 py-2 rounded-xl bg-slate-900/90 hover:bg-slate-800/90 text-xs font-medium text-slate-200 border border-white/[0.06] transition-all active:scale-95"
              >
                <Play className="w-3.5 h-3.5 text-emerald-400" />
                <span>Play / Pause</span>
              </button>
            </div>
          </div>

          {/* Spatial Gesture Guide Card */}
          <div className="cyber-card rounded-2xl p-3 flex-1 overflow-y-auto border border-white/[0.08] flex flex-col gap-2">
            <div className="text-[11px] uppercase font-bold text-slate-400 tracking-wider">Active Gesture Map</div>

            <div className="flex flex-col gap-1.5 text-xs">
              <div className="p-2 rounded-lg bg-slate-900/60 border border-white/[0.04] flex items-center justify-between">
                <span className="text-slate-300">👌 Index Pinch</span>
                <span className="font-code text-cyan-400 font-semibold">Left Click</span>
              </div>
              <div className="p-2 rounded-lg bg-slate-900/60 border border-white/[0.04] flex items-center justify-between">
                <span className="text-slate-300">✊ Closed Fist</span>
                <span className="font-code text-rose-400 font-semibold">Clutch Pause</span>
              </div>
              <div className="p-2 rounded-lg bg-slate-900/60 border border-white/[0.04] flex items-center justify-between">
                <span className="text-slate-300">✌️ Middle Pinch</span>
                <span className="font-code text-purple-400 font-semibold">Right Click</span>
              </div>
              <div className="p-2 rounded-lg bg-slate-900/60 border border-white/[0.04] flex items-center justify-between">
                <span className="text-slate-300">🤌 Ring Pinch</span>
                <span className="font-code text-emerald-400 font-semibold">Scroll Mode</span>
              </div>
              <div className="p-2 rounded-lg bg-slate-900/60 border border-white/[0.04] flex items-center justify-between">
                <span className="text-slate-300">🌀 Index Tips Touch</span>
                <span className="font-code text-cyan-300 font-semibold">Portal Magic</span>
              </div>
              <div className="p-2 rounded-lg bg-slate-900/60 border border-white/[0.04] flex items-center justify-between">
                <span className="text-slate-300">⚔️ Wrists Crossed</span>
                <span className="font-code text-indigo-300 font-semibold">X-Shield Desktop</span>
              </div>

              {customGestures.length > 0 && (
                <div className="pt-2 border-t border-white/[0.06] flex flex-col gap-1.5">
                  <div className="text-[10px] uppercase font-bold text-slate-500 tracking-wider">Custom Macros</div>
                  {customGestures.map((g) => (
                    <div
                      key={g.id}
                      className="p-2 rounded-lg bg-slate-900/40 border border-white/[0.03] flex items-center justify-between text-xs"
                    >
                      <span className="text-slate-300 truncate max-w-[120px]">✨ {g.name}</span>
                      <span className="font-code text-cyan-400 font-medium truncate max-w-[110px]">{g.action_payload}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </section>

        {/* ================================================================== */}
        {/* DECK 2 (CENTER 5 COLS): Companion Soul Core & Cognitive Dialogue   */}
        {/* ================================================================== */}
        <section className="col-span-5 flex flex-col gap-3 h-full overflow-hidden">
          {/* Hero Companion Core Banner */}
          <div className="cyber-card-glow rounded-2xl p-4 flex items-center gap-4 border border-cyan-500/20 shrink-0 relative overflow-hidden">
            <div className="relative w-14 h-14 rounded-2xl overflow-hidden shrink-0 border border-white/20 shadow-xl">
              <img src="/airos_icon.jpg" alt="Air-OS Core" className="w-full h-full object-cover" />
              <div className="absolute inset-0 bg-gradient-to-tr from-cyan-500/20 to-purple-500/20 pointer-events-none" />
            </div>

            <div className="flex-1">
              <div className="flex items-center gap-2">
                <h2 className="font-display font-bold text-base text-white tracking-wide">
                  Air-OS Cognitive Companion
                </h2>
                <span
                  className={`text-[10px] font-code font-bold px-2 py-0.5 rounded-full ${
                    soulState === 'mobile'
                      ? 'bg-purple-500/20 text-purple-300 border border-purple-500/30'
                      : 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/30'
                  }`}
                >
                  {soulState === 'mobile' ? 'ROAMING MOBILE' : 'PC DESKTOP'}
                </span>
              </div>
              <p className="text-xs text-slate-400 mt-0.5">
                {soulState === 'mobile'
                  ? 'User walked away from desk. Soul transferred over local Wi-Fi.'
                  : 'Webcam tracking user presence. Ready for voice & spatial interaction.'}
              </p>
            </div>

            <div className="flex flex-col items-end gap-1 text-right shrink-0">
              <span className="text-[10px] font-code text-slate-400 uppercase">Self-Prompt</span>
              <span className="text-xs font-code font-bold text-purple-400">
                v{dreamStatus?.current_prompt_version || 4}
              </span>
            </div>
          </div>

          {/* Dialogue Conversation Stream */}
          <div
            ref={chatScrollRef}
            className="cyber-card rounded-2xl p-3 flex-1 overflow-y-auto border border-white/[0.08] flex flex-col gap-3 scroll-smooth"
          >
            {chatMessages.map((msg) => (
              <div
                key={msg.id}
                className={`flex flex-col max-w-[85%] rounded-2xl p-3 text-xs leading-relaxed ${
                  msg.role === 'user'
                    ? 'self-end bg-gradient-to-br from-cyan-600/90 to-blue-700/90 text-white rounded-tr-sm shadow-md'
                    : 'self-start bg-[#121520]/95 border border-white/[0.08] text-slate-200 rounded-tl-sm shadow-md'
                }`}
              >
                <div className="flex items-center justify-between text-[10px] text-slate-300/80 mb-1 font-code">
                  <span className="font-semibold uppercase">
                    {msg.role === 'user'
                      ? msg.source === 'desktop_voice' || msg.source === 'mobile_voice'
                        ? '🎙️ Voice'
                        : '💬 You'
                      : '⚡ Air-OS'}
                  </span>
                  <span>{msg.timestamp}</span>
                </div>

                {msg.image && (
                  <div className="mb-2 rounded-xl overflow-hidden border border-white/10">
                    <img src={msg.image} alt="Webcam Capture" className="w-full object-cover max-h-48" />
                  </div>
                )}

                <div className="whitespace-pre-wrap">{msg.content}</div>

                {msg.actions && msg.actions.length > 0 && (
                  <div className="mt-2 pt-2 border-t border-white/10 flex flex-wrap gap-1">
                    {msg.actions.map((act, idx) => (
                      <span
                        key={idx}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-code font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30"
                      >
                        ✓ Executed: {act.raw || act.payload || act.type}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}

            {isAiLoading && (
              <div className="self-start bg-[#121520]/90 border border-white/[0.08] rounded-2xl p-3 text-xs text-slate-400 flex items-center gap-2">
                <RefreshCw className="w-3.5 h-3.5 animate-spin text-cyan-400" />
                <span>Ollama {selectedModel} analyzing...</span>
              </div>
            )}
          </div>

          {/* Unified Input Dock */}
          <div className="cyber-card rounded-2xl p-2.5 border border-white/[0.08] flex flex-col gap-2 shrink-0">
            {/* Quick Prompts */}
            <div className="flex items-center gap-1.5 overflow-x-auto pb-1 text-[11px] text-slate-300">
              <button
                onClick={() => handleSendChat('What can you do?')}
                className="px-2.5 py-1 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.06] shrink-0"
              >
                Capabilities
              </button>
              <button
                onClick={() => handleSendChat('Lock my computer')}
                className="px-2.5 py-1 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.06] shrink-0"
              >
                Lock PC
              </button>
              <button
                onClick={() => handleSendChat('Show my desktop')}
                className="px-2.5 py-1 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] border border-white/[0.06] shrink-0"
              >
                Show Desktop
              </button>
              <button
                onClick={handleSnapAndAsk}
                className="px-2.5 py-1 rounded-lg bg-cyan-500/10 hover:bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 shrink-0 flex items-center gap-1"
              >
                <Eye className="w-3 h-3" />
                <span>Snap Webcam</span>
              </button>
            </div>

            {/* Input Row */}
            <div className="flex items-center gap-2">
              <button
                onClick={toggleVoiceInput}
                className={`p-2.5 rounded-xl border transition-all ${
                  isListeningMic
                    ? 'bg-rose-500 text-white border-rose-400 shadow-[0_0_12px_rgba(244,63,94,0.5)] animate-pulse'
                    : 'bg-slate-900/80 text-cyan-400 border-white/10 hover:bg-slate-800'
                }`}
                title="Voice Input (Push-to-Talk)"
              >
                <Mic className="w-4 h-4" />
              </button>

              <input
                type="text"
                value={inputPrompt}
                onChange={(e) => setInputPrompt(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSendChat()}
                placeholder="Ask Air-OS or command Windows..."
                className="flex-1 bg-[#0a0c12]/90 border border-white/10 rounded-xl px-3 py-2 text-xs text-white placeholder-slate-500 outline-none focus:border-cyan-500/50"
              />

              <button
                onClick={() => handleSendChat()}
                disabled={isAiLoading || !inputPrompt.trim()}
                className="p-2.5 rounded-xl bg-gradient-to-r from-cyan-500 to-blue-600 text-white disabled:opacity-40 hover:opacity-90 active:scale-95 transition-all shadow-md"
              >
                <Send className="w-4 h-4" />
              </button>
            </div>
          </div>
        </section>

        {/* ================================================================== */}
        {/* DECK 3 (RIGHT 4 COLS): Memory Store, Dream State & Self-Improvement */}
        {/* ================================================================== */}
        <section className="col-span-4 flex flex-col gap-3 h-full overflow-hidden">
          {/* Self-Improvement Dream State Card */}
          <div className="cyber-card rounded-2xl p-4 flex flex-col gap-3 shrink-0 border border-white/[0.08]">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Moon className="w-4 h-4 text-purple-400" />
                <span className="font-display font-bold text-xs text-white uppercase tracking-wider">
                  Dream State Engine
                </span>
              </div>
              <span
                className={`text-[10px] font-code px-2 py-0.5 rounded-full font-semibold ${
                  isDreaming
                    ? 'bg-purple-500/20 text-purple-300 border border-purple-500/40 animate-pulse'
                    : 'bg-slate-800 text-slate-400'
                }`}
              >
                {isDreaming ? 'DREAMING...' : 'MONITORING'}
              </span>
            </div>

            {/* Metrics */}
            <div className="grid grid-cols-3 gap-2">
              <div className="p-2 rounded-xl bg-[#0a0c12]/60 border border-white/[0.04] text-center">
                <span className="text-[10px] text-slate-400 uppercase font-semibold">Memories</span>
                <div className="text-sm font-code font-bold text-white mt-0.5">
                  {dreamStatus?.stats.total_interactions || recentMemories.length}
                </div>
              </div>

              <div className="p-2 rounded-xl bg-[#0a0c12]/60 border border-white/[0.04] text-center">
                <span className="text-[10px] text-slate-400 uppercase font-semibold">Cycles</span>
                <div className="text-sm font-code font-bold text-purple-400 mt-0.5">
                  {dreamStatus?.stats.dream_cycles_completed || 0}
                </div>
              </div>

              <div className="p-2 rounded-xl bg-[#0a0c12]/60 border border-white/[0.04] text-center">
                <span className="text-[10px] text-slate-400 uppercase font-semibold">Prompt</span>
                <div className="text-sm font-code font-bold text-cyan-400 mt-0.5">
                  v{dreamStatus?.current_prompt_version || 4}
                </div>
              </div>
            </div>

            {/* Trigger Button */}
            <button
              onClick={triggerDreamStateNow}
              disabled={isDreaming}
              className="w-full py-2 px-3 rounded-xl bg-purple-600/20 hover:bg-purple-600/30 text-purple-300 border border-purple-500/30 text-xs font-semibold flex items-center justify-center gap-2 transition-all active:scale-95 disabled:opacity-50"
            >
              <Sparkles className="w-3.5 h-3.5 text-purple-400" />
              <span>Trigger Autonomous Dream Reflection</span>
            </button>
          </div>

          {/* Recent Cognitive Memory Log Feed */}
          <div className="cyber-card rounded-2xl p-3 flex-1 overflow-y-auto border border-white/[0.08] flex flex-col gap-2">
            <div className="flex items-center justify-between text-[11px] uppercase font-bold text-slate-400 tracking-wider">
              <span>Memory.json Store</span>
              <button onClick={pollData} className="hover:text-cyan-400 transition-colors">
                <RefreshCw className="w-3 h-3" />
              </button>
            </div>

            <div className="flex flex-col gap-2">
              {recentMemories.map((mem) => (
                <div
                  key={mem.id}
                  className="p-2.5 rounded-xl bg-[#0a0c12]/70 border border-white/[0.04] flex flex-col gap-1 text-xs"
                >
                  <div className="flex items-center justify-between text-[10px] font-code text-slate-400">
                    <span className="text-cyan-400 font-semibold uppercase">[{mem.source || 'chat'}]</span>
                    <span>{mem.timestamp ? new Date(mem.timestamp).toLocaleTimeString() : ''}</span>
                  </div>
                  <div className="text-white font-medium truncate">User: {mem.prompt}</div>
                  <div className="text-slate-400 text-[11px] truncate">AI: {mem.response}</div>
                  <div className="flex items-center justify-between text-[10px] mt-1 pt-1 border-t border-white/[0.03] text-slate-500">
                    <span>Model: {mem.model || 'qwen2.5:0.5b'}</span>
                    <span className={mem.analyzed_in_dream ? 'text-purple-400' : 'text-emerald-400'}>
                      {mem.analyzed_in_dream ? '🌙 Analyzed' : '⭐ New Memory'}
                    </span>
                  </div>
                </div>
              ))}

              {recentMemories.length === 0 && (
                <div className="text-center py-8 text-xs text-slate-500">No memories recorded yet.</div>
              )}
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
