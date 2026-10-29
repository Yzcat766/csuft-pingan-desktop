"""Previewed log retention; deletes only complete, unreferenced app batch folders."""

import json
import shutil
import re
import os
from datetime import timedelta
from pathlib import Path
from storage import now_shanghai


def log_plan(directory, data, days=90, active_id=None, moment=None):
    if type(days) is not int or not 7 <= days <= 3650:
        raise ValueError("日志保留天数应为 7～3650。")
    moment = moment or now_shanghai()
    cutoff = (moment - timedelta(days=days)).isoformat()
    protected = {r.get("batch_id") for r in data.get("results", [])}
    protected.add(active_id)
    protected.add((data.get("automation") or {}).get("attempt_id"))
    entries, total = [], 0
    root = Path(directory) / "运行日志"
    if (
        root.is_symlink()
        or (hasattr(root, "is_junction") and root.is_junction())
        or root.resolve().parent != Path(directory).resolve()
    ):
        raise ValueError("日志根目录不在当前数据目录内，不能清理。")
    from data_management import check_tree

    for name in ("运行日志", "总日志"):
        log_root = Path(directory) / name
        check_tree(log_root)
        for current, _, files in os.walk(log_root, followlinks=False):
            total += sum(
                (Path(current) / f).stat().st_size
                for f in files
                if Path(f).suffix in (".json", ".log")
            )
    for folder in root.iterdir() if root.exists() else []:
        if (
            not folder.is_dir()
            or folder.is_symlink()
            or (hasattr(folder, "is_junction") and folder.is_junction())
        ):
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            if (
                not re.fullmatch(r"\d{20}-[A-Za-z0-9_-]{1,100}", folder.name)
                or meta.get("key") != folder.name
                or meta.get("kind") != "batch"
                or meta.get("version") != 1
            ):
                continue
            files = list(folder.iterdir())
            if any(
                p.is_dir()
                or p.is_symlink()
                or not re.fullmatch(r"(?:meta|\d{6})\.json", p.name)
                for p in files
            ):
                continue
            size = sum(p.stat().st_size for p in files)
            if (
                meta.get("status")
                not in ("finished", "cancelled", "skipped", "not_started")
                or meta.get("batch_id") in protected
                or not meta.get("started_at")
                or meta["started_at"] >= cutoff
            ):
                continue
            entries.append(
                {
                    "name": folder.name,
                    "bytes": size,
                    "date": meta["started_at"][:10],
                    "batch_id": meta.get("batch_id"),
                    "meta_digest": (folder / "meta.json").read_bytes(),
                }
            )
        except (OSError, ValueError, TypeError):
            continue
    return {
        "entries": entries,
        "total_bytes": total,
        "delete_bytes": sum(e["bytes"] for e in entries),
        "cutoff": cutoff[:10],
        "days": days,
    }


def clear_logs(directory, plan, data, active_id=None):
    current = {
        e["name"]: e
        for e in log_plan(directory, data, plan["days"], active_id)["entries"]
    }
    deleted = 0
    root = (Path(directory) / "运行日志").resolve()
    for entry in plan["entries"]:
        checked = current.get(entry["name"])
        if not checked or checked["meta_digest"] != entry["meta_digest"]:
            continue
        path = (root / entry["name"]).resolve()
        if path.parent != root:
            raise ValueError("清理路径无效。")
        shutil.rmtree(path)
        deleted += 1
    return deleted
