import "@testing-library/jest-dom/vitest";

// jsdom 没有实现 matchMedia；外观切换要用它读系统主题偏好，补一个最小实现。
// G1Viewer 的用例跑在 node 环境里（没有 window），所以先判一下。
if (typeof window !== "undefined" && typeof window.matchMedia !== "function") {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}
