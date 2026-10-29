"""Credential-free, merged notifications, with matching in-app destinations."""


def batch_notice(results, preferences, cycle=None, batch_id=None):
    if not preferences.get("enabled", True):
        return None
    failures = [r for r in results if r.get("status") == "failure"]
    waiting = set((cycle or {}).get("pending_ids", []))
    final = [r for r in failures if r["account_id"] not in waiting]
    if failures:
        urgent = any(not r.get("retryable") for r in failures)
        title = (
            "需要人工处理"
            if urgent
            else "批次失败" if final else "临时失败，等待自动重试"
        )
        message = f"本批 {len(failures)} 个账号失败；{len(final)} 个需要查看，{len(failures)-len(final)} 个等待重试。请在软件中查看结果。"
        if not final and not urgent:
            return None  # UI shows the waiting state; notify on final failure only.
    elif preferences.get("success", False) and any(
        r.get("status") == "success" for r in results
    ):
        title, message = "批次完成", "服务端已确认本批成功结果，可在软件中查看。"
    else:
        return None
    return {
        "title": title,
        "message": message,
        "batch_id": batch_id or next((r.get("batch_id") for r in results), None),
        "account_id": final[0]["account_id"] if len(final) == 1 else None,
    }
