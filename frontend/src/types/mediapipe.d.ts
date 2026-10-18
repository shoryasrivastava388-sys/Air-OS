export interface NormalizedLandmark {
  x: number;
  y: number;
  z: number;
  visibility?: number;
}

export type NormalizedLandmarkList = NormalizedLandmark[];

export interface Handedness {
  index: number;
  label: 'Left' | 'Right' | string;
  score: number;
}

export interface HandsResults {
  multiHandLandmarks: NormalizedLandmarkList[];
  multiHandWorldLandmarks?: NormalizedLandmarkList[];
  multiHandedness: Handedness[];
  image: HTMLCanvasElement | HTMLVideoElement | ImageBitmap;
}

export interface HandsOptions {
  maxNumHands?: number;
  modelComplexity?: 0 | 1;
  minDetectionConfidence?: number;
  minTrackingConfidence?: number;
  selfieMode?: boolean;
}

export interface HandsConfig {
  locateFile?: (file: string, prefix?: string) => string;
}

export interface MediaPipeHandsInstance {
  setOptions(options: HandsOptions): void;
  onResults(callback: (results: HandsResults) => void): void;
  send(inputs: { image: HTMLVideoElement | HTMLCanvasElement | HTMLImageElement }): Promise<void>;
  close(): Promise<void>;
  initialize(): Promise<void>;
  reset(): void;
}

export interface CameraOptions {
  onFrame: () => Promise<void> | void;
  width?: number;
  height?: number;
  facingMode?: string;
}

export interface MediaPipeCameraInstance {
  start(): Promise<void>;
  stop(): Promise<void>;
}

declare global {
  interface Window {
    Hands: new (config?: HandsConfig) => MediaPipeHandsInstance;
    Camera: new (
      videoElement: HTMLVideoElement,
      options: CameraOptions
    ) => MediaPipeCameraInstance;
  }
}

export {};
