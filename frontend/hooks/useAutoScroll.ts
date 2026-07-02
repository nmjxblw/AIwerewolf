"use client";

import { useEffect, useRef } from "react";

export function useAutoScroll(dependency: unknown) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);
  const lastScrollAtRef = useRef(0);
  const SCROLL_IDLE_MS = 5000;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const idleFor = Date.now() - lastScrollAtRef.current;
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
