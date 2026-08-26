/**
 * 对局偏好持久化 — localStorage 存取。
 * 返回大厅时自动恢复上次的对局配置。
 */
import { CustomRolesConfig } from "@/types";

// Bump the key when aligned defaults change so legacy badge/parallel-speech
// preferences do not silently override the current research rules.
const PREFERENCES_KEY = "gamePreferences.v2";

export interface GamePreferences {
  playerCount: number;
  mode: "ai" | "human";
  humanSeat: number;
  customRoles: CustomRolesConfig | null;
  hasBadge: boolean;
  sharePersona: boolean;
  enableStrategy: boolean;
  hasLastWords: boolean;
  parallelSpeech: boolean;
}

const DEFAULTS: GamePreferences = {
  playerCount: 7,
  mode: "ai",
  humanSeat: 1,
  customRoles: null,
  hasBadge: false,
  sharePersona: true,
  enableStrategy: true,
  hasLastWords: false,
  parallelSpeech: false,
};

export function loadGamePreferences(): GamePreferences {
  try {
    const raw = localStorage.getItem(PREFERENCES_KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw);
    return {
      playerCount:
        typeof parsed.playerCount === "number"
          ? parsed.playerCount
          : DEFAULTS.playerCount,
      mode: parsed.mode === "human" ? "human" : "ai",
      humanSeat:
        typeof parsed.humanSeat === "number"
          ? parsed.humanSeat
          : DEFAULTS.humanSeat,
      customRoles: parsed.customRoles || null,
      hasBadge:
        typeof parsed.hasBadge === "boolean"
          ? parsed.hasBadge
          : DEFAULTS.hasBadge,
      sharePersona:
        typeof parsed.sharePersona === "boolean"
          ? parsed.sharePersona
          : DEFAULTS.sharePersona,
      enableStrategy:
        typeof parsed.enableStrategy === "boolean"
          ? parsed.enableStrategy
          : DEFAULTS.enableStrategy,
      hasLastWords:
        typeof parsed.hasLastWords === "boolean"
          ? parsed.hasLastWords
          : DEFAULTS.hasLastWords,
      parallelSpeech:
        typeof parsed.parallelSpeech === "boolean"
          ? parsed.parallelSpeech
          : DEFAULTS.parallelSpeech,
    };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveGamePreferences(prefs: GamePreferences): void {
  try {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify(prefs));
  } catch {
    // localStorage 不可用时不崩溃
  }
}
