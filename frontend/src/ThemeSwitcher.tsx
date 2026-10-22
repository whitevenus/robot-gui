/** 左下角的外观切换：浅色 / 深色 / 跟随系统。

偏好存在 localStorage 里；`<html data-theme>` 上永远是解析后的实际主题
（light / dark），这样 CSS 只需要一套浅色变量。页面第一帧之前由
index.html 里的内联脚本先定好主题，刷新时不会闪。
*/
import { useEffect, useRef, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";

export const THEME_STORAGE_KEY = "robot-gui-theme";

const OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "浅色" },
  { value: "dark", label: "深色" },
  { value: "system", label: "跟随系统" },
];

function readStored(): ThemePreference {
  try {
    const value = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (value === "light" || value === "dark" || value === "system") return value;
  } catch {
    // 隐私模式下读不到，按跟随系统处理。
  }
  return "system";
}

/** 把偏好落到 `<html data-theme>`：跟随系统时当场按系统偏好定。 */
export function applyTheme(preference: ThemePreference): void {
  const systemLight = window.matchMedia("(prefers-color-scheme: light)").matches;
  const effective = preference === "system" ? (systemLight ? "light" : "dark") : preference;
  document.documentElement.dataset.theme = effective;
}

function ThemeIcon({ light }: { light: boolean }) {
  return light ? (
    <svg
      viewBox="0 0 24 24"
      width="15"
      height="15"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  ) : (
    <svg
      viewBox="0 0 24 24"
      width="15"
      height="15"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5Z" />
    </svg>
  );
}

export function ThemeSwitcher() {
  const [preference, setPreference] = useState<ThemePreference>(readStored);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    applyTheme(preference);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    } catch {
      // 存不下就算了：这次会话内切换照样有效。
    }
  }, [preference]);

  // 跟随系统时，系统主题变了要跟着变。
  useEffect(() => {
    if (preference !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => applyTheme("system");
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [preference]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onPointerDown);
    return () => window.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const isLight = document.documentElement.dataset.theme === "light";

  return (
    <div className="theme-switcher" ref={rootRef}>
      {open && (
        <div className="theme-menu" role="menu" aria-label="外观">
          <p className="theme-menu-title">外观</p>
          {OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              role="menuitemradio"
              aria-checked={preference === option.value}
              className="theme-menu-item"
              onClick={() => {
                setPreference(option.value);
                setOpen(false);
              }}
            >
              <span className="theme-menu-dot" aria-hidden="true" />
              {option.label}
            </button>
          ))}
        </div>
      )}
      <button
        type="button"
        className="theme-button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <ThemeIcon light={isLight} />
        <span>外观</span>
      </button>
    </div>
  );
}
