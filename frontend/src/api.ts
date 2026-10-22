/** 后端统一错误格式：{"detail": {"code": "...", "reason": "..."}}。 */
export async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      return `${detail.code ?? "error"}：${detail.reason ?? ""}`.trim();
    }
    // FastAPI 路由不存在时返回 {"detail": "Not Found"}：多半是后端没跟上部署
    // （前后端是分开发布的），直接把这句英文甩给操作者没有用。
    if (response.status === 404 && detail === "Not Found") {
      return "后端没有这个接口：机器人上的 robot-gui 后端需要同步最新代码并重启。";
    }
    return JSON.stringify(detail ?? body);
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}
