/** 后端统一错误格式：{"detail": {"code": "...", "reason": "..."}}。 */
export async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      return `${detail.code ?? "error"}：${detail.reason ?? ""}`.trim();
    }
    return JSON.stringify(detail ?? body);
  } catch {
    return `请求失败（HTTP ${response.status}）`;
  }
}
