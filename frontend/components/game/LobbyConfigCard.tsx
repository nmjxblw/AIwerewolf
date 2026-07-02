"use client";

import { useMemo } from "react";
import { CustomRolesConfig, Language, Role } from "@/types";
import { t, tRole } from "@/lib/i18n";
import { Button } from "@/components/ui/Button";

// ── 默认角色配置（与后端 WOLFCHA_ROLE_CONFIGS 保持一致） ──
const DEFAULT_ROLE_CONFIGS: Record<number, Role[]> = {
  7: [Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.VILLAGER],
  8: [Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.VILLAGER, Role.VILLAGER],
  9: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.SEER, Role.WITCH, Role.HUNTER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
  10: [Role.WEREWOLF, Role.WEREWOLF, Role.WHITE_WOLF_KING, Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
  11: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WHITE_WOLF_KING, Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.IDIOT, Role.VILLAGER, Role.VILLAGER],
  12: [Role.WEREWOLF, Role.WEREWOLF, Role.WEREWOLF, Role.WHITE_WOLF_KING, Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.IDIOT, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
};

/** 可在 UI 中切换的角色（不包括必须有的基础狼人和村民） */
const TOGGLEABLE_ROLES: Role[] = [
  Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.IDIOT,
  Role.WHITE_WOLF_KING,
];

/** 角色分类标签 */
const GOD_ROLES: Role[] = [Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.IDIOT];
const WOLF_ROLES: Role[] = [Role.WHITE_WOLF_KING];

// ── Props ──

interface LobbyConfigCardProps {
  language: Language; playerCount: number; mode: "ai" | "human";
  humanSeat: number; isCreating: boolean; error: string;
  customRoles: CustomRolesConfig | null;
  onPlayerCountChange: (value: number) => void;
  onModeChange: (mode: "ai" | "human") => void;
  onHumanSeatChange: (seat: number) => void;
  onCustomRolesChange: (config: CustomRolesConfig | null) => void;
  onCreateRoom: () => void;
}

function countInList(list: Role[], role: Role): number {
  return list.filter((r) => r === role).length;
}

function roleLabel(role: Role, language: Language): string {
  const translated = tRole(role, language);
  return translated;
}

export function LobbyConfigCard(props: LobbyConfigCardProps) {
  const { language, playerCount, mode, humanSeat, isCreating, error,
    customRoles, onPlayerCountChange, onModeChange, onHumanSeatChange,
    onCustomRolesChange, onCreateRoom } = props;

  const isAi = mode === "ai";

  // 计算当前有效角色列表：默认 + include - exclude
  const effectiveRoles = useMemo(() => {
    const base = [...DEFAULT_ROLE_CONFIGS[playerCount] ?? DEFAULT_ROLE_CONFIGS[7]];
    if (!customRoles) return base;
    // exclude: 替换为村民
    for (const ex of customRoles.exclude) {
      const idx = base.indexOf(ex);
      if (idx !== -1) base[idx] = Role.VILLAGER;
    }
    // include: 替换村民
    const villagerIdxs = base.map((r, i) => (r === Role.VILLAGER ? i : -1)).filter((i) => i !== -1);
    for (let j = 0; j < customRoles.include.length && j < villagerIdxs.length; j++) {
      base[villagerIdxs[j]] = customRoles.include[j];
    }
    return base;
  }, [playerCount, customRoles]);

  // 判断某个角色当前是否激活
  const isRoleActive = (role: Role): boolean => {
    if (!customRoles) return countInList(DEFAULT_ROLE_CONFIGS[playerCount] ?? [], role) > 0;
    const defaultCount = countInList(DEFAULT_ROLE_CONFIGS[playerCount] ?? [], role);
    const excluded = customRoles.exclude.filter((r) => r === role).length;
    const included = customRoles.include.filter((r) => r === role).length;
    return defaultCount - excluded + included > 0;
  };

  const handleToggleRole = (role: Role) => {
    if (!customRoles) {
      // 首次操作：基于默认配置创建 customRoles
      const active = isRoleActive(role);
      onCustomRolesChange({
        exclude: active ? [role] : [],
        include: active ? [] : [role],
      });
    } else {
      const exclude = [...customRoles.exclude];
      const include = [...customRoles.include];
      const defaultCount = countInList(DEFAULT_ROLE_CONFIGS[playerCount] ?? [], role);
      const excludedCount = exclude.filter((r) => r === role).length;
      const includedCount = include.filter((r) => r === role).length;
      const active = defaultCount - excludedCount + includedCount > 0;

      if (active) {
        // 关闭：优先减少 include，否则增加 exclude
        if (includedCount > 0) {
          const idx = include.lastIndexOf(role);
          if (idx !== -1) include.splice(idx, 1);
        } else {
          exclude.push(role);
        }
      } else {
        // 开启：优先减少 exclude，否则增加 include
        if (excludedCount > 0) {
          const idx = exclude.lastIndexOf(role);
          if (idx !== -1) exclude.splice(idx, 1);
        } else {
          include.push(role);
        }
      }

      // 如果 exclude 和 include 都为空，恢复为 null（无自定义）
      if (exclude.length === 0 && include.length === 0) {
        onCustomRolesChange(null);
      } else {
        onCustomRolesChange({ exclude, include });
      }
    }
  };

  // 是否有自定义修改
  const hasCustom = customRoles && (customRoles.exclude.length > 0 || customRoles.include.length > 0);

  const modeDesc = isAi
    ? (language === "zh" ? "所有玩家由 AI 控制，你可以观战完整对局。" : "All players controlled by AI. Spectate the full match.")
    : (language === "zh" ? "你选择一个座位加入对局，其余玩家由 AI 扮演。" : "Pick a seat to join. Other players are AI-controlled.");

  const buttonText = isAi
    ? (language === "zh" ? "开始 AI 对局" : "Start AI Match")
    : (language === "zh" ? "开始真人参与对局" : "Start Human Match");

  return (
    <div className="w-full space-y-5 rounded-xl border border-border/40 bg-cardBackground/80 backdrop-blur-sm p-6 shadow-[0_8px_40px_rgba(0,0,0,0.3)]">
      {/* Mode toggle */}
      <div>
        <label className="block text-xs font-medium text-text-sub/60 mb-2.5 uppercase tracking-wider min-h-[1em]">{t("gameMode", language)}</label>
        <div className="flex rounded-lg border border-border/40 p-0.5 bg-border/5">
          {(["ai", "human"] as const).map((m) => (
            <button key={m} data-testid={`mode-${m}-button`} onClick={() => onModeChange(m)}
              className={`flex-1 py-2.5 text-sm font-medium rounded-md transition-all duration-200 ${
                mode === m ? "bg-primary text-white shadow-[0_2px_8px_rgba(183,131,63,0.3)]" : "text-text-sub/60 hover:text-textPrimary"
              }`}>
              {m === "ai" ? t("aiVsAi", language) : t("humanPlay", language)}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-text-sub/50 mt-2 leading-relaxed min-h-[2.25em]">{modeDesc}</p>
      </div>

      {/* Player count */}
      <div>
        <label className="block text-xs font-medium text-text-sub/60 mb-2.5 uppercase tracking-wider">{t("playerCount", language)}</label>
        <div className="flex gap-1.5">
          {[7, 8, 9, 10, 11, 12].map((count) => (
            <button key={count} onClick={() => onPlayerCountChange(count)}
              className={`flex-1 py-2.5 rounded-lg text-sm font-medium transition-all duration-200 ${
                playerCount === count ? "bg-primary/15 text-primary border border-primary/30 ring-1 ring-primary/20"
                : "text-text-sub/50 border border-transparent hover:text-textPrimary hover:bg-primary/5"
              }`}>
              {count}
            </button>
          ))}
        </div>
      </div>

      {/* ── 角色配置 ── */}
      <div>
        <div className="flex items-center justify-between mb-2.5">
          <label className="text-xs font-medium text-text-sub/60 uppercase tracking-wider">
            {language === "zh" ? "角色配置" : "Role Config"}
          </label>
          {hasCustom && (
            <button
              onClick={() => onCustomRolesChange(null)}
              className="text-[11px] text-primary/70 hover:text-primary transition-colors"
            >
              {language === "zh" ? "恢复默认" : "Reset"}
            </button>
          )}
        </div>

        {/* 神职 */}
        <div className="mb-2">
          <span className="text-[10px] text-text-sub/40 uppercase tracking-wider">
            {language === "zh" ? "神职" : "Gods"}
          </span>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {GOD_ROLES.map((role) => {
              const active = isRoleActive(role);
              const count = effectiveRoles.filter((r) => r === role).length;
              return (
                <button
                  key={role}
                  data-testid={`role-toggle-${role}`}
                  onClick={() => handleToggleRole(role)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-200 border ${
                    active
                      ? "bg-primary/15 text-primary border-primary/30 ring-1 ring-primary/20"
                      : "text-text-sub/40 border-border/30 hover:text-text-sub/60 hover:border-border/50"
                  }`}
                >
                  {roleLabel(role, language)}
                  {count > 1 && <span className="ml-0.5 opacity-60">×{count}</span>}
                </button>
              );
            })}
          </div>
        </div>

        {/* 狼人阵营附加角色 */}
        <div>
          <span className="text-[10px] text-text-sub/40 uppercase tracking-wider">
            {language === "zh" ? "狼人阵营" : "Wolf Camp"}
          </span>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {WOLF_ROLES.map((role) => {
              const active = isRoleActive(role);
              return (
                <button
                  key={role}
                  data-testid={`role-toggle-${role}`}
                  onClick={() => handleToggleRole(role)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all duration-200 border ${
                    active
                      ? "bg-primary/15 text-primary border-primary/30 ring-1 ring-primary/20"
                      : "text-text-sub/40 border-border/30 hover:text-text-sub/60 hover:border-border/50"
                  }`}
                >
                  {roleLabel(role, language)}
                </button>
              );
            })}
          </div>
        </div>

        {/* 角色预览摘要 */}
        <div className="mt-2 flex flex-wrap gap-1">
          {TOGGLEABLE_ROLES.map((role) => {
            const count = effectiveRoles.filter((r) => r === role).length;
            if (count === 0) return null;
            return (
              <span key={role} className="inline-flex items-center gap-0.5 text-[10px] text-text-sub/50 bg-border/10 rounded px-1.5 py-0.5">
                {roleLabel(role, language)}
                {count > 1 && <>×{count}</>}
              </span>
            );
          })}
          <span className="text-[10px] text-text-sub/30 bg-transparent rounded px-1.5 py-0.5">
            +{effectiveRoles.filter((r) => r === Role.VILLAGER).length} {language === "zh" ? "村民" : "Villager"}
          </span>
          <span className="text-[10px] text-text-sub/30 bg-transparent rounded px-1.5 py-0.5">
            {effectiveRoles.filter((r) => r === Role.WEREWOLF).length} {language === "zh" ? "狼人" : "Wolf"}
          </span>
        </div>
      </div>

      {/* Seat selector — human mode only, max-height transition (stable cross-browser) */}
      <div className={`transition-all duration-300 overflow-hidden ${!isAi ? "max-h-60 opacity-100 mt-1" : "max-h-0 opacity-0 mt-0"}`}>
        <label className="block text-xs font-medium text-text-sub/60 mb-2.5 uppercase tracking-wider">{t("yourSeat", language)}</label>
        <div className="grid grid-cols-6 gap-1.5">
          {Array.from({ length: playerCount }, (_, i) => i + 1).map((seat) => (
            <button key={seat} onClick={() => onHumanSeatChange(seat)}
              data-testid={`human-seat-${seat}-button`}
              className={`py-2.5 rounded-lg text-sm font-medium transition-all duration-200 ${
                humanSeat === seat ? "bg-primary/15 text-primary border border-primary/30 ring-1 ring-primary/20"
                : "text-text-sub/50 border border-transparent hover:text-textPrimary hover:bg-primary/5"
              }`}>
              {seat}
            </button>
          ))}
        </div>
      </div>

      {/* Error */}
      {error && <div className="rounded-lg bg-danger/10 border border-danger/20 px-4 py-2.5 text-sm text-danger text-center">{error}</div>}

      {/* Submit */}
      <Button onClick={onCreateRoom} disabled={isCreating} className="w-full h-12 text-base font-semibold tracking-wide">
        {isCreating ? (
          <span className="flex items-center justify-center gap-2">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
            {t("creating", language)}
          </span>
        ) : buttonText}
      </Button>
    </div>
  );
}
