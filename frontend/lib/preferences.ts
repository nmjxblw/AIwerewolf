/**
 * 对局偏好持久化 — localStorage 存取。
 * 返回大厅时自动恢复上次的对局配置。
 */
import { CustomRolesConfig } from "@/types";

const PREFERENCES_KEY = "gamePreferences";

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
  hasBadge: true,
  sharePersona: true,
  enableStrategy: true,
  hasLastWords: true,
  parallelSpeech: true,
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
