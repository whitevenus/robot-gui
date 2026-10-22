/** 统一的确认弹窗。

浏览器原生的 `window.confirm` 长什么样由浏览器决定，深色工作台里那块灰白
对话框很突兀。这里的弹窗跟页面同一套配色，并且回车/ESC 都能操作。

用法：

```tsx
const confirm = useConfirm();
if (!(await confirm({ title: "回放整套课程", message: "……" }))) return;
```
*/
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

export interface ConfirmRequest {
  /** 标题：说清要确认的是什么。 */
  title: string;
  /** 正文：可以多行。 */
  message: string;
  /** 危险操作（删除等）：确认按钮染红。 */
  danger?: boolean;
}

type ConfirmFn = (request: ConfirmRequest) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

/** 拿到确认弹窗；必须在 ConfirmProvider 里面用。 */
export function useConfirm(): ConfirmFn {
  const confirm = useContext(ConfirmContext);
  if (confirm === null) {
    throw new Error("useConfirm 必须在 ConfirmProvider 里面使用。");
  }
  return confirm;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  const resolver = useRef<((value: boolean) => void) | null>(null);
  const acceptRef = useRef<HTMLButtonElement | null>(null);

  const confirm = useCallback((next: ConfirmRequest) => {
    setRequest(next);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const settle = useCallback((value: boolean) => {
    resolver.current?.(value);
    resolver.current = null;
    setRequest(null);
  }, []);

  useEffect(() => {
    if (request === null) return;
    acceptRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      const tag = (event.target as HTMLElement | null)?.tagName ?? "";
      if (event.key === "Escape") {
        settle(false);
        return;
      }
      // 回车等于确认；但焦点在输入框里时不能抢走输入。
      if (event.key === "Enter" && tag !== "INPUT" && tag !== "TEXTAREA") {
        settle(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request, settle]);

  const value = useMemo(() => confirm, [confirm]);

  return (
    <ConfirmContext.Provider value={value}>
      {children}
      {request !== null && (
        <div className="confirm-backdrop" onClick={() => settle(false)}>
          <div
            className="confirm-panel"
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirm-title"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 className="confirm-title" id="confirm-title">
              {request.title}
            </h2>
            <p className="confirm-message">{request.message}</p>
            <div className="confirm-actions">
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => settle(false)}
              >
                取消
              </button>
              <button
                ref={acceptRef}
                type="button"
                className={`btn confirm-accept${request.danger ? " is-danger" : ""}`}
                onClick={() => settle(true)}
              >
                确认
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}
