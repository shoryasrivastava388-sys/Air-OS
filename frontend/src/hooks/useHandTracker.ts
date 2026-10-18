import { useEffect, useRef, useState, useCallback } from 'react';
import type { NormalizedLandmark, HandsResults } from '../types/mediapipe';

export type GestureState =
  | 'IDLE'
  | 'MOVE'
  | 'LEFT_CLICK'
  | 'DRAG_HOLD'
  | 'DRAG_RELEASE'
  | 'RIGHT_CLICK'
  | 'SCROLL'
  | 'CLUTCH_PAUSE';

export interface HandAction {
  type: 'MOVE' | 'LEFT_CLICK' | 'DRAG_HOLD' | 'DRAG_RELEASE' | 'RIGHT_CLICK' | 'SCROLL';
  x: number;
  y: number;
  deltaY?: number;
}

export interface UseHandTrackerOptions {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  canvasRef: React.RefObject<HTMLCanvasElement | null>;
  isArmedRef: React.RefObject<boolean>;
  wsRef?: React.RefObject<WebSocket | null>;
  bindingsRef?: React.RefObject<{
    thumbIndexPinch: 'left_click' | 'double_click' | 'play_pause' | 'mute';
    thumbIndexHold: 'drag_hold' | 'volume_up_down';
    thumbMiddlePinch: 'right_click' | 'double_click' | 'desktop_next' | 'desktop_prev';
    thumbRingPinch: 'scroll' | 'volume_up_down';
  }>;
  onGestureChange?: (gesture: GestureState) => void;
  onAction?: (action: HandAction) => void;
}

// MediaPipe 21 Hand Connections
const HAND_CONNECTIONS: [number, number][] = [
  // Thumb
  [0, 1], [1, 2], [2, 3], [3, 4],
  // Index
  [0, 5], [5, 6], [6, 7], [7, 8],
  // Middle
  [9, 10], [10, 11], [11, 12],
  // Ring
  [13, 14], [14, 15], [15, 16],
  // Pinky
  [0, 17], [17, 18], [18, 19], [19, 20],
  // Palm Base
  [5, 9], [9, 13], [13, 17],
];

export function useHandTracker({
  videoRef,
  canvasRef,
  isArmedRef,
  wsRef,
  bindingsRef,
  onGestureChange,
  onAction,
}: UseHandTrackerOptions) {
  const [isCameraActive, setIsCameraActive] = useState<boolean>(false);
  const [currentGesture, setCurrentGesture] = useState<GestureState>('IDLE');
  const [fps, setFps] = useState<number>(0);
  const [cameraError, setCameraError] = useState<string | null>(null);

  // References for zero-lag calculations outside of React state
  const streamRef = useRef<MediaStream | null>(null);
  const workerRef = useRef<Worker | null>(null);
  const handsInstanceRef = useRef<any>(null);
  const isSendingFrame = useRef<boolean>(false);
  const animationFrameIdRef = useRef<number | null>(null);
  const latestLandmarksRef = useRef<NormalizedLandmark[] | null>(null);

  // Smoothed Screen Coordinates & 1-Euro Adaptive Velocity Smoothing
  const smoothedScreenX = useRef<number>(0.5);
  const smoothedScreenY = useRef<number>(0.5);
  const prevRawX = useRef<number>(0.5);
  const prevRawY = useRef<number>(0.5);
  const lastSentX = useRef<number>(0);
  const lastSentY = useRef<number>(0);
  const lastSendTime = useRef<number>(0);
  const lostHandFramesRef = useRef<number>(0);
  const lastAlpha = useRef<number>(0.3);

  // Hysteresis refs to eliminate gesture state flickering and React re-render churn
  const isThumbIndexPinchingRef = useRef<boolean>(false);
  const isThumbMiddlePinchingRef = useRef<boolean>(false);
  const isThumbRingPinchingRef = useRef<boolean>(false);
  const isClosedFistRef = useRef<boolean>(false);

  // State machine timers and flags
  const pinchStartTime = useRef<number | null>(null);
  const isDragHolding = useRef<boolean>(false);
  const dragHoldTriggered = useRef<boolean>(false);
  const lastRightClickTime = useRef<number>(0);
  const leftClickFlashUntil = useRef<number>(0);
  const rightClickFlashUntil = useRef<number>(0);
  const lastActiveGesture = useRef<GestureState>('IDLE');
  const prevScrollY = useRef<number>(0.5);

  // FPS calculation refs
  const frameCountRef = useRef<number>(0);
  const lastFpsTimestampRef = useRef<number>(performance.now());

  // 2D Distance helper
  const dist2D = (p1: NormalizedLandmark, p2: NormalizedLandmark) => {
    const dx = p1.x - p2.x;
    const dy = p1.y - p2.y;
    return Math.sqrt(dx * dx + dy * dy);
  };

  // Convert raw normalized landmark to canvas coordinates with horizontal mirroring
  const toCanvasX = (rawX: number, width: number) => (1 - rawX) * width;
  const toCanvasY = (rawY: number, height: number) => rawY * height;

  // Process Landmarks and classify gesture
  const processFrame = useCallback(() => {
    const landmarks = latestLandmarksRef.current;
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Ensure canvas dimensions match client pixel dimensions
    if (canvas.width !== canvas.clientWidth || canvas.height !== canvas.clientHeight) {
      canvas.width = canvas.clientWidth;
      canvas.height = canvas.clientHeight;
    }

    const width = canvas.width;
    const height = canvas.height;
    const now = performance.now();
    const isArmed = !!isArmedRef.current;

    // Clear Canvas
    ctx.clearRect(0, 0, width, height);

    // 1. Draw Active Control Box (Middle 60%: x 0.2 to 0.8, y 0.2 to 0.8)
    const boxX = width * 0.2;
    const boxY = height * 0.2;
    const boxW = width * 0.6;
    const boxH = height * 0.6;

    ctx.save();
    ctx.strokeStyle = isArmed ? 'rgba(6, 182, 212, 0.45)' : 'rgba(245, 158, 11, 0.3)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([8, 6]);
    ctx.strokeRect(boxX, boxY, boxW, boxH);
    ctx.setLineDash([]);

    // Corner reticles for Active Control Box
    const cornerSize = 14;
    ctx.strokeStyle = isArmed ? '#22d3ee' : '#f59e0b';
    ctx.lineWidth = 2.5;

    // Top-Left
    ctx.beginPath();
    ctx.moveTo(boxX, boxY + cornerSize);
    ctx.lineTo(boxX, boxY);
    ctx.lineTo(boxX + cornerSize, boxY);
    ctx.stroke();

    // Top-Right
    ctx.beginPath();
    ctx.moveTo(boxX + boxW - cornerSize, boxY);
    ctx.lineTo(boxX + boxW, boxY);
    ctx.lineTo(boxX + boxW, boxY + cornerSize);
    ctx.stroke();

    // Bottom-Left
    ctx.beginPath();
    ctx.moveTo(boxX, boxY + boxH - cornerSize);
    ctx.lineTo(boxX, boxY + boxH);
    ctx.lineTo(boxX + cornerSize, boxY + boxH);
    ctx.stroke();

    // Bottom-Right
    ctx.beginPath();
    ctx.moveTo(boxX + boxW - cornerSize, boxY + boxH);
    ctx.lineTo(boxX + boxW, boxY + boxH);
    ctx.lineTo(boxX + boxW, boxY + boxH - cornerSize);
    ctx.stroke();

    // Active Control Box Label
    ctx.fillStyle = isArmed ? 'rgba(34, 211, 238, 0.7)' : 'rgba(245, 158, 11, 0.7)';
    ctx.font = '10px "JetBrains Mono", monospace';
    ctx.fillText('ACTIVE CONTROL ZONE (60%)', boxX + 8, boxY + 16);
    ctx.restore();

    if (!landmarks || landmarks.length === 0) {
      lostHandFramesRef.current++;
      // Require 3 consecutive missing frames before transitioning to IDLE, preventing rapid state flickering
      if (lostHandFramesRef.current > 3 && lastActiveGesture.current !== 'IDLE') {
        lastActiveGesture.current = 'IDLE';
        setCurrentGesture('IDLE');
        onGestureChange?.('IDLE');
      }
      return;
    }
    lostHandFramesRef.current = 0;

    // 2. Gesture Classification & Normalization relative to Palm Size
    const wrist = landmarks[0];
    const middleMcp = landmarks[9];
    const palmDist = Math.max(0.015, dist2D(wrist, middleMcp));

    const normDist = (p1: NormalizedLandmark, p2: NormalizedLandmark) => dist2D(p1, p2) / palmDist;

    // Landmarks:
    const thumbTip = landmarks[4];
    const indexTip = landmarks[8];
    const indexKnuckle = landmarks[5]; // Cursor anchor
    const middleTip = landmarks[12];
    const ringTip = landmarks[16];
    const pinkyTip = landmarks[20];

    // Pinches with Hysteresis (prevents rapid on/off state flicker)
    const rawThumbIndexDist = normDist(thumbTip, indexTip);
    const rawThumbMiddleDist = normDist(thumbTip, middleTip);
    const rawThumbRingDist = normDist(thumbTip, ringTip);

    const isThumbIndexPinching = isThumbIndexPinchingRef.current
      ? rawThumbIndexDist < 0.32
      : rawThumbIndexDist < 0.25;
    isThumbIndexPinchingRef.current = isThumbIndexPinching;

    const isThumbMiddlePinching = isThumbMiddlePinchingRef.current
      ? rawThumbMiddleDist < 0.32
      : rawThumbMiddleDist < 0.26;
    isThumbMiddlePinchingRef.current = isThumbMiddlePinching;

    const isThumbRingPinching = isThumbRingPinchingRef.current
      ? rawThumbRingDist < 0.32
      : rawThumbRingDist < 0.26;
    isThumbRingPinchingRef.current = isThumbRingPinching;

    // Closed Fist (CLUTCH_PAUSE) with Hysteresis
    const isIndexCurled = dist2D(indexTip, wrist) < dist2D(landmarks[6], wrist) || normDist(indexTip, indexKnuckle) < 0.65;
    const isMiddleCurled = dist2D(middleTip, wrist) < dist2D(landmarks[10], wrist) || normDist(middleTip, middleMcp) < 0.65;
    const isRingCurled = dist2D(ringTip, wrist) < dist2D(landmarks[14], wrist) || normDist(ringTip, landmarks[13]) < 0.65;
    const isPinkyCurled = dist2D(pinkyTip, wrist) < dist2D(landmarks[18], wrist) || normDist(pinkyTip, landmarks[17]) < 0.65;
    const fistEnter = isIndexCurled && isMiddleCurled && isRingCurled && isPinkyCurled;
    const isClosedFist = isClosedFistRef.current
      ? (dist2D(indexTip, wrist) < dist2D(landmarks[5], wrist) * 1.25)
      : fistEnter;
    isClosedFistRef.current = isClosedFist;

    // 3. Target Screen Mapping from Landmark 5 (Index Knuckle) inside 60% box
    const mirroredX = 1 - indexKnuckle.x;
    const rawY = indexKnuckle.y;

    // Map 0.2 - 0.8 box to full 0.0 - 1.0 screen
    const clampedNormX = Math.max(0, Math.min(1, (mirroredX - 0.2) / 0.6));
    const clampedNormY = Math.max(0, Math.min(1, (rawY - 0.2) / 0.6));

    // Adaptive Lerp (1-Euro Style Smoothing): dynamically scales with velocity
    const rawDx = clampedNormX - prevRawX.current;
    const rawDy = clampedNormY - prevRawY.current;
    const speed = Math.hypot(rawDx, rawDy);

    if (speed > 0.0001) {
      prevRawX.current = clampedNormX;
      prevRawY.current = clampedNormY;
      // When hand moves fast across screen, alpha jumps to 0.85 for instant zero-lag response;
      // when hand slows down to click, alpha drops to 0.18 for rock-steady precision!
      lastAlpha.current = Math.min(0.85, Math.max(0.18, speed * 12));
    }

    const alpha = lastAlpha.current;

    if (!isClosedFist) {
      smoothedScreenX.current += alpha * (clampedNormX - smoothedScreenX.current);
      smoothedScreenY.current += alpha * (clampedNormY - smoothedScreenY.current);
    }

    // Direct WebSocket streaming with Zero Backpressure (60 FPS rate)
    const ws = wsRef?.current;
    const isWsOpen = ws && ws.readyState === WebSocket.OPEN;

    const dx = smoothedScreenX.current - lastSentX.current;
    const dy = smoothedScreenY.current - lastSentY.current;
    const hasMoved = Math.abs(dx) > 0.0005 || Math.abs(dy) > 0.0005; // Moved by at least 1 pixel
    const canSendMove = (now - lastSendTime.current) >= 16; // 60 FPS rate limit

    if (isArmed && isWsOpen && !isClosedFist && hasMoved && canSendMove && ws.bufferedAmount === 0) {
      lastSendTime.current = now;
      lastSentX.current = smoothedScreenX.current;
      lastSentY.current = smoothedScreenY.current;
      ws.send(
        JSON.stringify({
          type: 'move',
          action: 'move',
          x: Number(smoothedScreenX.current.toFixed(4)),
          y: Number(smoothedScreenY.current.toFixed(4)),
        })
      );
    }

    // 4. State Machine Evaluation & Real-time Action Emission
    let evaluatedGesture: GestureState = 'MOVE';

    if (isClosedFist) {
      evaluatedGesture = 'CLUTCH_PAUSE';
      pinchStartTime.current = null;
      isDragHolding.current = false;
      dragHoldTriggered.current = false;
    } else if (isThumbRingPinching) {
      evaluatedGesture = 'SCROLL';
      pinchStartTime.current = null;
      isDragHolding.current = false;
      dragHoldTriggered.current = false;
      const scrollDelta = (smoothedScreenY.current - prevScrollY.current) * 100;
      prevScrollY.current = smoothedScreenY.current;

      onAction?.({
        type: 'SCROLL',
        x: smoothedScreenX.current,
        y: smoothedScreenY.current,
        deltaY: scrollDelta,
      });

      if (isArmed && isWsOpen && ws.bufferedAmount === 0) {
        const ringBinding = bindingsRef?.current?.thumbRingPinch || 'scroll';
        if (ringBinding === 'scroll') {
          const sdy = -Math.round(scrollDelta * 0.35);
          if (sdy !== 0) {
            ws.send(JSON.stringify({ type: 'scroll', action: 'scroll', dy: sdy }));
          }
        } else if (ringBinding === 'volume_up_down') {
          if (scrollDelta < -1.5) {
            ws.send(JSON.stringify({ type: 'hotkey', action: 'hotkey', key: 'VOLUME_UP' }));
          } else if (scrollDelta > 1.5) {
            ws.send(JSON.stringify({ type: 'hotkey', action: 'hotkey', key: 'VOLUME_DOWN' }));
          }
        }
      }
    } else if (isThumbMiddlePinching) {
      pinchStartTime.current = null;
      isDragHolding.current = false;
      dragHoldTriggered.current = false;
      if (now - lastRightClickTime.current > 500) {
        lastRightClickTime.current = now;
        rightClickFlashUntil.current = now + 400;

        onAction?.({
          type: 'RIGHT_CLICK',
          x: smoothedScreenX.current,
          y: smoothedScreenY.current,
        });

        if (isArmed && isWsOpen) {
          const midBinding = bindingsRef?.current?.thumbMiddlePinch || 'right_click';
          if (midBinding === 'right_click') {
            ws.send(JSON.stringify({ type: 'right_click', action: 'right_click' }));
          } else if (midBinding === 'double_click') {
            ws.send(JSON.stringify({ type: 'double_click', action: 'double_click' }));
          } else if (midBinding === 'desktop_next') {
            ws.send(JSON.stringify({ type: 'hotkey', action: 'hotkey', key: 'DESKTOP_NEXT' }));
          } else if (midBinding === 'desktop_prev') {
            ws.send(JSON.stringify({ type: 'hotkey', action: 'hotkey', key: 'DESKTOP_PREV' }));
          }
        }
      }
      evaluatedGesture = 'RIGHT_CLICK';
    } else if (isThumbIndexPinching) {
      if (pinchStartTime.current === null) {
        pinchStartTime.current = now;
        isDragHolding.current = false;
        dragHoldTriggered.current = false;
      }
      const holdDuration = now - pinchStartTime.current;
      if (holdDuration >= 250) {
        isDragHolding.current = true;
        evaluatedGesture = 'DRAG_HOLD';
        if (!dragHoldTriggered.current) {
          dragHoldTriggered.current = true;
          onAction?.({
            type: 'DRAG_HOLD',
            x: smoothedScreenX.current,
            y: smoothedScreenY.current,
          });

          if (isArmed && isWsOpen) {
            if (bindingsRef?.current?.thumbIndexHold === 'drag_hold') {
              ws.send(JSON.stringify({ type: 'down', action: 'down' }));
            }
          }
        }
      } else {
        evaluatedGesture = 'MOVE';
      }
    } else {
      // Released thumb-index pinch
      if (pinchStartTime.current !== null) {
        const holdDuration = now - pinchStartTime.current;
        if (isDragHolding.current || dragHoldTriggered.current) {
          evaluatedGesture = 'DRAG_RELEASE';
          isDragHolding.current = false;
          dragHoldTriggered.current = false;
          onAction?.({
            type: 'DRAG_RELEASE',
            x: smoothedScreenX.current,
            y: smoothedScreenY.current,
          });

          if (isArmed && isWsOpen) {
            if (bindingsRef?.current?.thumbIndexHold === 'drag_hold') {
              ws.send(JSON.stringify({ type: 'up', action: 'up' }));
            }
          }
        } else if (holdDuration < 250) {
          evaluatedGesture = 'LEFT_CLICK';
          leftClickFlashUntil.current = now + 350;
          onAction?.({
            type: 'LEFT_CLICK',
            x: smoothedScreenX.current,
            y: smoothedScreenY.current,
          });

          if (isArmed && isWsOpen) {
            const indexBinding = bindingsRef?.current?.thumbIndexPinch || 'left_click';
            if (indexBinding === 'left_click') {
              ws.send(JSON.stringify({ type: 'click', action: 'click' }));
            } else if (indexBinding === 'double_click') {
              ws.send(JSON.stringify({ type: 'double_click', action: 'double_click' }));
            } else if (indexBinding === 'play_pause') {
              ws.send(JSON.stringify({ type: 'hotkey', action: 'hotkey', key: 'PLAY_PAUSE' }));
            } else if (indexBinding === 'mute') {
              ws.send(JSON.stringify({ type: 'hotkey', action: 'hotkey', key: 'MUTE' }));
            }
          }
        }
        pinchStartTime.current = null;
      } else {
        if (now < leftClickFlashUntil.current) {
          evaluatedGesture = 'LEFT_CLICK';
        } else if (now < rightClickFlashUntil.current) {
          evaluatedGesture = 'RIGHT_CLICK';
        } else {
          evaluatedGesture = 'MOVE';
        }
      }
    }

    // Only trigger React state update if discrete gesture changed
    if (evaluatedGesture !== lastActiveGesture.current) {
      lastActiveGesture.current = evaluatedGesture;
      setCurrentGesture(evaluatedGesture);
      onGestureChange?.(evaluatedGesture);
    }

    // 5. Draw Hand Skeleton on Canvas
    ctx.save();
    ctx.strokeStyle = isDragHolding.current
      ? 'rgba(245, 158, 11, 0.8)'
      : isClosedFist
      ? 'rgba(244, 63, 94, 0.7)'
      : 'rgba(6, 182, 212, 0.75)';
    ctx.lineWidth = 2.5;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    HAND_CONNECTIONS.forEach(([startIdx, endIdx]) => {
      const p1 = landmarks[startIdx];
      const p2 = landmarks[endIdx];
      ctx.beginPath();
      ctx.moveTo(toCanvasX(p1.x, width), toCanvasY(p1.y, height));
      ctx.lineTo(toCanvasX(p2.x, width), toCanvasY(p2.y, height));
      ctx.stroke();
    });

    // Draw Landmark Dots
    landmarks.forEach((p, idx) => {
      const px = toCanvasX(p.x, width);
      const py = toCanvasY(p.y, height);

      ctx.beginPath();
      if (idx === 5) {
        // Landmark 5 (Index Knuckle): Cursor Tracker Target
        ctx.arc(px, py, 6, 0, Math.PI * 2);
        ctx.fillStyle = '#10b981';
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#34d399';
        ctx.stroke();

        // Target reticle ring
        ctx.beginPath();
        ctx.arc(px, py, 14, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(16, 185, 129, 0.6)';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      } else if (idx === 4 || idx === 8) {
        // Pinch points
        ctx.arc(px, py, isThumbIndexPinching ? 5 : 3.5, 0, Math.PI * 2);
        ctx.fillStyle = isThumbIndexPinching ? '#f59e0b' : '#38bdf8';
        ctx.fill();
      } else {
        ctx.arc(px, py, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#22d3ee';
        ctx.fill();
      }
    });

    // If pinching Thumb + Index: Draw pinch chord
    if (isThumbIndexPinching) {
      const tX = toCanvasX(thumbTip.x, width);
      const tY = toCanvasY(thumbTip.y, height);
      const iX = toCanvasX(indexTip.x, width);
      const iY = toCanvasY(indexTip.y, height);

      ctx.beginPath();
      ctx.moveTo(tX, tY);
      ctx.lineTo(iX, iY);
      ctx.strokeStyle = '#f59e0b';
      ctx.lineWidth = 3;
      ctx.stroke();

      // Pinch hold progress ring (250ms threshold)
      if (pinchStartTime.current && !isDragHolding.current) {
        const holdProgress = Math.min(1, (now - pinchStartTime.current) / 250);
        const midX = (tX + iX) / 2;
        const midY = (tY + iY) / 2;
        ctx.beginPath();
        ctx.arc(midX, midY, 16, -Math.PI / 2, -Math.PI / 2 + holdProgress * Math.PI * 2);
        ctx.strokeStyle = '#f59e0b';
        ctx.lineWidth = 3;
        ctx.stroke();
      }
    }

    // 6. DRAG_HOLD Visual Indicator: Glowing Lock Ring
    const knuckleX = toCanvasX(indexKnuckle.x, width);
    const knuckleY = toCanvasY(indexKnuckle.y, height);

    if (isDragHolding.current) {
      ctx.save();
      ctx.beginPath();
      ctx.arc(knuckleX, knuckleY, 26, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(245, 158, 11, 0.15)';
      ctx.fill();
      ctx.strokeStyle = '#f59e0b';
      ctx.lineWidth = 2.5;
      ctx.stroke();

      // Outer dashed spinning glow ring
      ctx.beginPath();
      ctx.arc(knuckleX, knuckleY, 34, 0, Math.PI * 2);
      ctx.setLineDash([6, 6]);
      ctx.strokeStyle = 'rgba(245, 158, 11, 0.6)';
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.restore();
    }

    // 7. Live Floating Badge next to hand
    let badgeText = 'MOVE';
    let badgeColor = '#06b6d4';
    let badgeBg = 'rgba(15, 23, 42, 0.85)';

    switch (evaluatedGesture) {
      case 'LEFT_CLICK':
        badgeText = 'CLICK!';
        badgeColor = '#10b981';
        break;
      case 'DRAG_HOLD':
        badgeText = 'DRAGGING';
        badgeColor = '#f59e0b';
        break;
      case 'RIGHT_CLICK':
        badgeText = 'RIGHT CLICK';
        badgeColor = '#a855f7';
        break;
      case 'SCROLL':
        badgeText = 'SCROLLING';
        badgeColor = '#0ea5e9';
        break;
      case 'CLUTCH_PAUSE':
        badgeText = 'CLUTCH PAUSED';
        badgeColor = '#f43f5e';
        break;
      default:
        badgeText = `MOVE (${smoothedScreenX.current.toFixed(2)}, ${smoothedScreenY.current.toFixed(2)})`;
        badgeColor = '#06b6d4';
    }

    const badgeX = Math.min(width - 130, Math.max(10, knuckleX + 24));
    const badgeY = Math.min(height - 20, Math.max(30, knuckleY - 24));

    // Badge background box
    ctx.font = 'bold 11px "JetBrains Mono", monospace';
    const textWidth = ctx.measureText(badgeText).width;
    const badgePadX = 8;
    const badgePadY = 5;
    const badgeW = textWidth + badgePadX * 2;
    const badgeH = 12 + badgePadY * 2;

    ctx.fillStyle = badgeBg;
    ctx.strokeStyle = badgeColor;
    ctx.lineWidth = 1.5;

    // Rounded badge
    ctx.beginPath();
    ctx.roundRect(badgeX, badgeY - 14, badgeW, badgeH, 6);
    ctx.fill();
    ctx.stroke();

    // Badge text
    ctx.fillStyle = badgeColor;
    ctx.fillText(badgeText, badgeX + badgePadX, badgeY + 1);

    ctx.restore();
  }, [canvasRef, isArmedRef, wsRef, bindingsRef, onAction, onGestureChange]);

  // RequestAnimationFrame Loop for silky smooth rendering when visible
  useEffect(() => {
    let isRunning = true;

    const renderLoop = () => {
      if (!isRunning) return;
      processFrame();

      // FPS tracking
      frameCountRef.current++;
      const now = performance.now();
      if (now - lastFpsTimestampRef.current >= 1000) {
        setFps(frameCountRef.current);
        frameCountRef.current = 0;
        lastFpsTimestampRef.current = now;
      }

      animationFrameIdRef.current = requestAnimationFrame(renderLoop);
    };

    animationFrameIdRef.current = requestAnimationFrame(renderLoop);

    return () => {
      isRunning = false;
      if (animationFrameIdRef.current !== null) {
        cancelAnimationFrame(animationFrameIdRef.current);
      }
    };
  }, [processFrame]);

  // Helper to construct unthrottled Web Worker timer
  const createTimerWorker = () => {
    const workerScript = `
      let timer = null;
      self.onmessage = function(e) {
        if (e.data === 'start') {
          if (!timer) {
            timer = setInterval(function() {
              self.postMessage('tick');
            }, 16);
          }
        } else if (e.data === 'stop') {
          if (timer) {
            clearInterval(timer);
            timer = null;
          }
        }
      };
    `;
    const blob = new Blob([workerScript], { type: 'application/javascript' });
    return new Worker(URL.createObjectURL(blob));
  };

  // Start MediaPipe Hand Tracking with Unthrottled Web Worker Interval
  const startTracking = useCallback(async () => {
    try {
      setCameraError(null);
      const videoElement = videoRef.current;
      if (!videoElement) {
        throw new Error('Video element reference unavailable');
      }

      // Check MediaPipe CDN global availability
      if (typeof window.Hands === 'undefined') {
        throw new Error('MediaPipe Hands script not loaded from CDN yet');
      }

      // 1. Initialize MediaPipe Hands
      const hands = new window.Hands({
        locateFile: (file: string) => `https://cdn.jsdelivr.net/npm/@mediapipe/hands/${file}`,
      });

      hands.setOptions({
        maxNumHands: 1,
        modelComplexity: 0, // Zero-lag lightweight model
        minDetectionConfidence: 0.6,
        minTrackingConfidence: 0.6,
        selfieMode: false,
      });

      hands.onResults((results: HandsResults) => {
        if (results.multiHandLandmarks && results.multiHandLandmarks.length > 0) {
          latestLandmarksRef.current = results.multiHandLandmarks[0];
          processFrame();
        } else {
          latestLandmarksRef.current = null;
        }
      });

      await hands.initialize();
      handsInstanceRef.current = hands;

      // 2. Direct Camera Stream (640x360 @ 60 FPS)
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 640 },
          height: { ideal: 360 },
          frameRate: { ideal: 60 },
          facingMode: 'user',
        },
        audio: false,
      });

      streamRef.current = stream;
      videoElement.srcObject = stream;
      await videoElement.play();

      // 3. Unthrottled Background Web Worker Timer (Ticks every 16ms even when minimized)
      const worker = createTimerWorker();
      workerRef.current = worker;

      worker.onmessage = async (e) => {
        if (e.data === 'tick') {
          const video = videoRef.current;
          const handsInstance = handsInstanceRef.current;

          if (video && handsInstance && video.readyState >= 2 && !isSendingFrame.current) {
            isSendingFrame.current = true;
            try {
              await handsInstance.send({ image: video });
            } catch {
              // Ignore frame drop
            } finally {
              isSendingFrame.current = false;
            }
          }

          // If document is hidden/minimized, requestAnimationFrame stops firing,
          // so the worker tick explicitly drives processFrame and coordinates streaming!
          if (document.hidden) {
            processFrame();
          }
        }
      };

      worker.postMessage('start');
      setIsCameraActive(true);
    } catch (err: unknown) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to initialize MediaPipe hand tracker';
      setCameraError(errorMsg);
      setIsCameraActive(false);
    }
  }, [videoRef, processFrame]);

  // Stop MediaPipe Hand Tracking and Camera
  const stopTracking = useCallback(async () => {
    try {
      if (workerRef.current) {
        workerRef.current.postMessage('stop');
        workerRef.current.terminate();
        workerRef.current = null;
      }
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((track) => track.stop());
        streamRef.current = null;
      }
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
      if (handsInstanceRef.current) {
        await handsInstanceRef.current.close();
        handsInstanceRef.current = null;
      }
      latestLandmarksRef.current = null;
      setIsCameraActive(false);

      const canvas = canvasRef.current;
      if (canvas) {
        const ctx = canvas.getContext('2d');
        if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      }
    } catch (err) {
      console.error('Error stopping tracking:', err);
    }
  }, [canvasRef, videoRef]);

  const toggleTracking = useCallback(() => {
    if (isCameraActive) {
      stopTracking();
    } else {
      startTracking();
    }
  }, [isCameraActive, startTracking, stopTracking]);

  return {
    isCameraActive,
    currentGesture,
    fps,
    cameraError,
    startTracking,
    stopTracking,
    toggleTracking,
    smoothedScreenX,
    smoothedScreenY,
  };
}
