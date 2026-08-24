export const MBTI_LABELS_CN: Record<string, string> = {
  INTJ: "幕后操盘手",
  INTP: "逻辑怪才",
  ENTJ: "铁血指挥",
  ENTP: "诡辩大师",
  INFJ: "灵魂导师",
  INFP: "和平使者",
  ENFJ: "聚光灯主角",
  ENFP: "人气王",
  ISTJ: "规则守护者",
  ISFJ: "默默守护者",
  ESTJ: "铁面警长",
  ESFJ: "热心群众",
  ISTP: "独行侠",
  ISFP: "随性艺术家",
  ESTP: "冒险赌徒",
  ESFP: "欢乐喜剧人",
};

export const STYLE_LABELS_CN: Record<string, string> = {
  analytical: "分析型",
  persuasive: "说服型",
  aggressive: "激进型",
  insightful: "洞察型",
  observant: "观察型",
  expressive: "表达型",
  meticulous: "细致型",
  provocative: "挑衅型",
  energetic: "活力型",
  academic: "学院派",
  commander: "指挥型",
  sensitive: "敏感型",
  interrogator: "盘问型",
  gentle: "温和型",
  archivist: "档案型",
  tricky: "狡黠型",
  tactical: "战术型",
  precise: "精准型",
  observer: "旁观型",
  rallier: "拉票型",
  playful: "俏皮型",
  lyrical: "抒情型",
  deconstructive: "拆解型",
  caretaker: "守护型",
  strategist: "策略型",
  curious: "探询型",
  ranger: "游走型",
  matrix: "套路型",
  debater: "辩论型",
  veteran: "老将型",
  theorist: "理论型",
  poetic: "诗性型",
  cosmopolitan: "见多识广型",
  harmonizer: "协调型",
  still_water: "静水型",
  mediator: "调停型",
  anchor: "定海神针型",
};

export const HUMOR_LABELS_CN: Record<string, string> = {
  dry: "冷幽默",
  self_deprecating: "自嘲型",
  sarcastic: "讽刺型",
  warm: "温暖型",
  none: "无幽默",
};

export const GENDER_LABELS_CN: Record<string, string> = {
  male: "男",
  female: "女",
  nonbinary: "非二元",
};

export function labelOrValue(
  map: Record<string, string>,
  value: string,
): string {
  return map[value] || value;
}

export function formatPersonaProfile(mbti?: string, style?: string): string {
  const parts: string[] = [];
  if (mbti) parts.push(labelOrValue(MBTI_LABELS_CN, mbti));
  if (style) parts.push(labelOrValue(STYLE_LABELS_CN, style));
  return parts.join(" · ");
}
