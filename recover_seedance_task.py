#!/usr/bin/env python3
"""
Recover a Seedance MP4 by BytePlus ARK task id — no new generation charge.

Polling an existing task only reads status; you are not submitting a new job.
Use this when LiteLLM, your app, or the network failed after ARK accepted the task.

Requires (pick one path):
  Direct ARK (most reliable):  BYTEDANCE_API_KEY
  Via LiteLLM proxy:           LITELLM_MASTER_KEY + LITELLM_API_BASE

Usage:
  python recover_seedance_task.py cgt-20260524222121-27n4l
  python recover_seedance_task.py cgt-... --output recovered.mp4
  python recover_seedance_task.py --list-recent   # needs SEEDANCE_TASK_LEDGER_PATH
  python recover_seedance_task.py --recover-pending  # poll all non-terminal ledger rows

Optional env:
  SEEDANCE_ARK_BASE          default ap-southeast ARK v3 base
  SEEDANCE_TASK_LEDGER_PATH  append-only log written by the proxy handler
  RECOVER_MAX_WAIT_S         default 3600
  RECOVER_POLL_INTERVAL_S    default 10
  LITELLM_API_BASE           default http://localhost:4000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_ARK_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
TASK_PREFIX = "seedance-task://"


def _normalize_task_id(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith(TASK_PREFIX):
        s = s[len(TASK_PREFIX) :]
    return s.strip()


def _read_ledger(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _ark_get_task(
    *, client: httpx.Client, ark_base: str, api_key: str, task_id: str
) -> dict[str, Any]:
    r = client.get(
        f"{ark_base.rstrip('/')}/contents/generations/tasks/{task_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    r.raise_for_status()
    return r.json()


def _proxy_poll_task(
    *,
    client: httpx.Client,
    api_base: str,
    master_key: str,
    model: str,
    task_id: str,
) -> dict[str, Any]:
    r = client.post(
        f"{api_base.rstrip('/')}/v1/images/generations",
        headers={
            "Authorization": f"Bearer {master_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "prompt": f"{TASK_PREFIX}{task_id}",
            "seedance_task_id": task_id,
        },
        timeout=120.0,
    )
    if r.status_code != 200:
        raise RuntimeError(f"proxy poll failed {r.status_code}: {r.text[:500]}")
    return r.json()


def _video_url_from_body(body: dict[str, Any]) -> Optional[str]:
    # Direct ARK
    if body.get("status") == "succeeded":
        return (body.get("content") or {}).get("video_url")
    # LiteLLM OpenAI shape
    data = body.get("data") or []
    if data:
        url = data[0].get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


def _status_label(body: dict[str, Any]) -> str:
    if "status" in body:
        return str(body.get("status") or "unknown")
    data = body.get("data") or []
    if data:
        url = data[0].get("url") or ""
        if url.startswith(TASK_PREFIX):
            return str(data[0].get("revised_prompt") or "running")
        if url.startswith("https://"):
            return "succeeded"
    return "unknown"


def recover_task(
    task_id: str,
    *,
    output: Path,
    via: str,
    model: str,
    max_wait_s: float,
    poll_interval_s: float,
) -> Path:
    task_id = _normalize_task_id(task_id)
    if not task_id:
        raise ValueError("task_id is required")

    ark_base = os.environ.get("SEEDANCE_ARK_BASE", DEFAULT_ARK_BASE)
    ark_key = os.environ.get("BYTEDANCE_API_KEY", "")
    proxy_base = os.environ.get("LITELLM_API_BASE", "http://localhost:4000")
    proxy_key = os.environ.get("LITELLM_MASTER_KEY", "")

    if via == "ark" and not ark_key:
        raise SystemExit("BYTEDANCE_API_KEY is required for --via ark")
    if via == "proxy" and not proxy_key:
        raise SystemExit("LITELLM_MASTER_KEY is required for --via proxy")

    deadline = time.monotonic() + max_wait_s
    print(f"[recover] task_id={task_id} via={via} max_wait={max_wait_s:.0f}s")

    with httpx.Client(timeout=120.0) as client:
        while True:
            if via == "ark":
                body = _ark_get_task(
                    client=client, ark_base=ark_base, api_key=ark_key, task_id=task_id
                )
            else:
                body = _proxy_poll_task(
                    client=client,
                    api_base=proxy_base,
                    master_key=proxy_key,
                    model=model,
                    task_id=task_id,
                )

            status = _status_label(body)
            video_url = _video_url_from_body(body)
            print(f"[recover] status={status}")

            if video_url:
                print(f"[recover] downloading {video_url[:100]}…")
                dl = client.get(video_url)
                dl.raise_for_status()
                output.write_bytes(dl.content)
                print(f"[recover] saved {output} ({len(dl.content) / 1024:.1f} KB)")
                return output

            if status in ("failed", "expired"):
                err = body.get("error") or body
                raise SystemExit(f"task {task_id} terminal status={status}: {err}")

            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"task {task_id} still not ready after {max_wait_s:.0f}s — "
                    "retry later (ARK keeps completed outputs for ~48h)"
                )

            time.sleep(poll_interval_s)


def cmd_list_recent(ledger_path: str, limit: int) -> None:
    rows = _read_ledger(ledger_path)
    if not rows:
        print(f"No ledger at {ledger_path}")
        return
    for row in rows[-limit:]:
        print(
            row.get("logged_at"),
            row.get("task_id"),
            row.get("ark_model"),
            (row.get("prompt_preview") or "")[:60],
        )


def cmd_recover_pending(
    ledger_path: str,
    *,
    via: str,
    model: str,
    max_wait_s: float,
    poll_interval_s: float,
    out_dir: Path,
) -> None:
    rows = _read_ledger(ledger_path)
    if not rows:
        print("Ledger empty")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        task_id = row.get("task_id")
        if not task_id:
            continue
        dest = out_dir / f"{task_id}.mp4"
        if dest.is_file():
            print(f"[skip] {task_id} already at {dest}")
            continue
        try:
            recover_task(
                str(task_id),
                output=dest,
                via=via,
                model=model,
                max_wait_s=max_wait_s,
                poll_interval_s=poll_interval_s,
            )
        except SystemExit as e:
            print(f"[fail] {task_id}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover Seedance video by ARK task id")
    parser.add_argument("task_id", nargs="?", help="ARK task id (cgt-...) or seedance-task://…")
    parser.add_argument("-o", "--output", type=Path, help="output MP4 path")
    parser.add_argument(
        "--via",
        choices=("ark", "proxy"),
        default=os.environ.get("RECOVER_VIA", "ark"),
        help="poll ARK directly (default) or via LiteLLM proxy",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("RECOVER_MODEL", "seedance-2.0-fast"),
        help="LiteLLM model alias when --via proxy",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=float(os.environ.get("RECOVER_MAX_WAIT_S", "3600")),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("RECOVER_POLL_INTERVAL_S", "10")),
    )
    parser.add_argument(
        "--list-recent",
        action="store_true",
        help="print recent rows from SEEDANCE_TASK_LEDGER_PATH",
    )
    parser.add_argument(
        "--recover-pending",
        action="store_true",
        help="attempt recovery for each task id in the ledger",
    )
    parser.add_argument(
        "--pending-dir",
        type=Path,
        default=Path("recovered_seedance"),
        help="output directory for --recover-pending",
    )
    args = parser.parse_args()

    ledger = os.environ.get("SEEDANCE_TASK_LEDGER_PATH", "").strip()

    if args.list_recent:
        if not ledger:
            sys.exit("Set SEEDANCE_TASK_LEDGER_PATH to use --list-recent")
        cmd_list_recent(ledger, limit=50)
        return

    if args.recover_pending:
        if not ledger:
            sys.exit("Set SEEDANCE_TASK_LEDGER_PATH to use --recover-pending")
        cmd_recover_pending(
            ledger,
            via=args.via,
            model=args.model,
            max_wait_s=args.max_wait,
            poll_interval_s=args.interval,
            out_dir=args.pending_dir,
        )
        return

    if not args.task_id:
        parser.error("task_id is required unless using --list-recent or --recover-pending")

    task_id = _normalize_task_id(args.task_id)
    output = args.output or Path(f"recovered_{task_id}.mp4")
    recover_task(
        task_id,
        output=output,
        via=args.via,
        model=args.model,
        max_wait_s=args.max_wait,
        poll_interval_s=args.interval,
    )


if __name__ == "__main__":
    main()
