"use client";

import { useEffect, useRef } from "react";

/**
 * 智能自动滚动 hook：
 * - 内容变化时，如果用户在底部（60px 内）→ 立即跟随滚动
 * - 如果用户向上翻阅历史 → 不打断，等 AFK 5 秒后再恢复自动滚动
 */
export function useAutoScroll(dependency: unknown) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);
  const lastScrollAtRef = useRef(0);
  const lastScrollHeightRef = useRef(0);
  const SCROLL_IDLE_MS = 5000;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 内容高度没变 → 不需要滚动（避免打字机效果中重复触发）
    if (el.scrollHeight === lastScrollHeightRef.current) return;
    lastScrollHeightRef.current = el.scrollHeight;

    const idleFor = Date.now() - lastScrollAtRef.current;
    // 用户刚手动滚动过且不在底部 → 不打断，等 AFK 超时再恢复
    if (!autoScrollRef.current && idleFor < SCROLL_IDLE_MS) return;

    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    autoScrollRef.current = true;
  }, [dependency]);

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    lastScrollAtRef.current = Date.now();
    autoScrollRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }

  return { scrollRef, handleScroll };
}
