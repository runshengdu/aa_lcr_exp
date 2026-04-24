from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import tiktoken
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.aa_lcr import (
    DATASET_CSV_PATH,
    DEFAULT_CONTEXT_LENGTH_TOKENS,
    get_task_prompt_for_row_or_skip,
    load_models_yaml,
    load_questions,
    read_jsonl_data_line_strings,
    read_results_jsonl_state,
    safe_filename_component,
    build_knowledge_completion_body,
    write_jsonl_atomic,
    MODELS_YAML_PATH,
)

TERMINAL_BATCH_STATES = {"completed", "failed", "expired", "cancelled"}


def _sort_qid_keys(keys: set[str] | list[str]) -> list[str]:
    def _k(x: str) -> tuple[int, int | str]:
        s = str(x)
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return (0, int(s))
        return (1, s)

    return sorted(keys, key=_k)


def _qid_to_custom_id(qid: str) -> str:
    b = base64.urlsafe_b64encode(qid.encode("utf-8")).decode("ascii").rstrip("=")
    return f"qid-{b}"


def _parse_custom_id_to_qid(s: str) -> str | None:
    if not isinstance(s, str) or not s.startswith("qid-"):
        return None
    tail = s[4:]
    pad = "=" * ((4 - len(tail) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(tail + pad).decode("utf-8")
    except Exception:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AA-LCR: batch 推理 + collect 只下载合并；请用 main.py --evaluation-file 做判分。"
    )
    parser.add_argument(
        "--step",
        type=str,
        default="all",
        choices=["all", "prepare", "upload", "create", "wait", "collect", "submit", "poll"],
        help="Pipeline step. Recommended: prepare / upload / create / wait / collect, or all.",
    )
    parser.add_argument(
        "--needle",
        type=str,
        default="aa_lcr",
        help="Artifact subdir name under --artifacts-dir (default: aa_lcr).",
    )
    parser.add_argument("--model-id", type=str, default="qwen3.6-flash", help="Model name in models.yaml")
    parser.add_argument(
        "--save-to",
        type=str,
        default=None,
        help="Path to results jsonl (model-only rows, judge_result 空；与 main 一致可续跑).",
    )
    parser.add_argument(
        "--max-context-window",
        type=int,
        default=DEFAULT_CONTEXT_LENGTH_TOKENS,
        help="Max context (cl100k_base, same as main.py). Rows above are skipped in prepare.",
    )
    parser.add_argument(
        "--completion-window",
        type=str,
        default="24h",
        help="Batch completion window.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=10,
        help="Batch status polling interval in seconds.",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=str,
        default="batch_api/qwen/artifacts",
        help="Directory for run_dir artifacts (jsonl, meta).",
    )
    parser.add_argument(
        "--run-dir",
        type=str,
        default=None,
        help="Existing run directory, required for upload/create/wait/collect.",
    )
    parser.add_argument(
        "--batch-id",
        type=str,
        default=None,
        help="Batch ID override for wait/collect.",
    )
    return parser.parse_args()


def make_client(model_name: str) -> OpenAI:
    models = load_models_yaml(MODELS_YAML_PATH)
    if model_name not in models:
        raise SystemExit(f"model {model_name!r} not found in {MODELS_YAML_PATH}")
    cfg = models[model_name]
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_run_dir(artifacts_dir: Path, model_name: str, needle: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = safe_filename_component(model_name)
    safe_needle = safe_filename_component(needle)
    run_dir = artifacts_dir / safe_needle / safe_model / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def resolve_results_jsonl_path(args: argparse.Namespace, model_name: str) -> Path:
    if args.save_to:
        p = Path(args.save_to)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    return Path("results") / model_name.replace("/", "__") / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"


def append_skip_records_to_jsonl(
    save_path: Path,
    skip_records: list[dict[str, Any]],
) -> None:
    if not skip_records:
        return
    h, data_lines = read_jsonl_data_line_strings(save_path)
    for rec in skip_records:
        data_lines.append(json.dumps(rec, ensure_ascii=False))
    # prepare/collect 不写新首行；若该文件已存在 main 评估后写入的 _meta_stats 则保留
    write_jsonl_atomic(save_path, header_line=h, data_line_strings=data_lines)


def build_batch_input_file(
    pending_rows: list[dict[str, Any]],
    model_name: str,
    max_context_window: int,
    input_path: Path,
    encoder: Any,
) -> tuple[dict[str, dict[str, Any]], set[str], list[dict[str, Any]]]:
    models = load_models_yaml(MODELS_YAML_PATH)
    if model_name not in models:
        raise SystemExit(f"model {model_name!r} not found in {MODELS_YAML_PATH}")
    model_cfg = models[model_name]

    row_payloads: dict[str, dict[str, Any]] = {}
    skipped_token_qids: set[str] = set()
    prebatch_skip_records: list[dict[str, Any]] = []

    with open(input_path, "w", encoding="utf-8", newline="\n") as f:
        for row in pending_rows:
            qid = str(row.get("question_id", "")).strip()
            prep = get_task_prompt_for_row_or_skip(
                row=row,
                encoder=encoder,
                context_length=max_context_window,
                model_cfg=model_cfg,
            )
            if not prep.get("ok"):
                rec = prep["record"]
                if rec.get("judge_result") == "ERROR" and not rec.get("skipped_reason"):
                    print(f"[SKIP prepare] pre-batch error for qid {qid!r}, not writing to jsonl (like main).")
                elif "skipped_reason" in rec:
                    prebatch_skip_records.append(rec)
                    skipped_token_qids.add(qid)
                else:
                    print(f"[WARN] unexpected pre-batch record for {qid}: {rec!r}")
                continue

            task_prompt = prep["task_prompt"]
            max_tok = int(prep["max_completion_tokens"])
            custom_id = _qid_to_custom_id(qid)
            body = build_knowledge_completion_body(model_cfg, task_prompt, max_tok)
            request_obj: dict[str, Any] = {
                "custom_id": custom_id,
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": body,
            }
            f.write(json.dumps(request_obj, ensure_ascii=False) + "\n")
            row_payloads[qid] = {
                "question": str(row.get("question", "")).strip(),
                "gold_answer": str(row.get("answer", "")).strip(),
                "question_id": qid,
            }

    return row_payloads, skipped_token_qids, prebatch_skip_records


def extract_text_from_file_content(content_obj: Any) -> str:
    text_attr = getattr(content_obj, "text", None)
    if isinstance(text_attr, str):
        return text_attr
    if callable(text_attr):
        return str(text_attr())
    read_method = getattr(content_obj, "read", None)
    if callable(read_method):
        raw = read_method()
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)
    return str(content_obj)


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def upload_input_file(client: OpenAI, input_path: Path) -> str:
    with open(input_path, "rb") as f:
        file_obj = client.files.create(file=f, purpose="batch")
    print(f"输入文件已上传: {file_obj.id}")
    return file_obj.id


def create_batch(client: OpenAI, input_file_id: str, completion_window: str) -> str:
    batch = client.batches.create(
        input_file_id=input_file_id,
        endpoint="/v1/chat/completions",
        completion_window=completion_window,
    )
    print(f"Batch 任务已创建: {batch.id}")
    return batch.id


def poll_batch(client: OpenAI, batch_id: str, poll_interval_seconds: int) -> Any:
    while True:
        batch = client.batches.retrieve(batch_id)
        counts = getattr(batch, "request_counts", None)
        completed = getattr(counts, "completed", 0) if counts else 0
        total = getattr(counts, "total", 0) if counts else 0
        print(f"状态: {batch.status} ({completed}/{total})")

        if batch.status in TERMINAL_BATCH_STATES:
            return batch
        time.sleep(max(1, int(poll_interval_seconds)))


def parse_batch_output(
    output_text: str,
    row_payloads: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    row_results: dict[str, dict[str, Any]] = {}
    failed_qids: set[str] = set()

    for raw_line in output_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue

        custom_id = data.get("custom_id")
        qid = _parse_custom_id_to_qid(str(custom_id)) if custom_id is not None else None
        if qid is None or qid not in row_payloads:
            continue

        error = data.get("error")
        response = data.get("response") or {}
        status_code = response.get("status_code")
        body = response.get("body") or {}

        if error is not None or status_code not in {0, 200}:
            failed_qids.add(qid)
            continue

        choices = body.get("choices") or []
        if not choices:
            failed_qids.add(qid)
            continue

        message = choices[0].get("message") or {}
        content = message.get("content")
        usage = body.get("usage") or {}
        p_tok = usage.get("prompt_tokens")
        c_tok = usage.get("completion_tokens")
        if c_tok is None:
            c_tok = 0

        if not isinstance(content, str) or p_tok is None:
            failed_qids.add(qid)
            continue

        row_results[qid] = {
            "llm_answer": content,
            "prompt_token": int(p_tok),
            "completion_token": int(c_tok),
        }

    return row_results, failed_qids


def stage_prepare(args: argparse.Namespace) -> Path:
    models = load_models_yaml(MODELS_YAML_PATH)
    model_name = str(args.model_id)
    if model_name not in models:
        raise SystemExit(f"--model-id not found: {model_name!r} in {MODELS_YAML_PATH}")

    max_context_window = int(args.max_context_window) if args.max_context_window is not None else int(DEFAULT_CONTEXT_LENGTH_TOKENS)
    results_path = resolve_results_jsonl_path(args, model_name)

    done_ids, _existing, _c, _t = read_results_jsonl_state(results_path)
    all_rows = sorted(
        load_questions(DATASET_CSV_PATH), key=lambda r: int(str(r.get("question_id", "0") or "0"))
    )
    pending_full = [r for r in all_rows if str(r.get("question_id", "")).strip() not in done_ids]

    if not pending_full:
        print("results jsonl 已覆盖全部题号，无需续测。")
        raise SystemExit(0)

    encoder = tiktoken.get_encoding("cl100k_base")
    artifacts_root = Path(args.artifacts_dir)
    run_dir = build_run_dir(artifacts_root, model_name, str(args.needle))
    input_jsonl = run_dir / "batch_input.jsonl"
    output_jsonl = run_dir / "batch_output.jsonl"
    error_jsonl = run_dir / "batch_error.jsonl"
    row_payloads_json = run_dir / "row_payloads.json"
    meta_json = run_dir / "meta.json"

    row_payloads, skipped_context_qids, prebatch_skips = build_batch_input_file(
        pending_full,
        model_name,
        max_context_window,
        input_jsonl,
        encoder,
    )

    append_skip_records_to_jsonl(results_path, prebatch_skips)

    if not row_payloads:
        print(
            f"没有可提交的 batch 行（可能全部因上下文被跳过）。"
            f"已写入跳过的行: {len(prebatch_skips)}，上下文相关 qid: {sorted(skipped_context_qids)}"
        )
        raise SystemExit(0)

    print(
        f"准备提交 batch 行数: {len(row_payloads)}，"
        f"prepare 阶段因上下文跳过并写入 jsonl: {len(prebatch_skips)}，"
        f"results: {results_path}"
    )

    save_json(
        row_payloads_json,
        {k: v for k, v in row_payloads.items()},
    )

    metadata: dict[str, Any] = {
        "version": 3,
        "needle": str(args.needle),
        "model": model_name,
        "run_dir": str(run_dir),
        "results_jsonl": str(results_path.resolve()),
        "csv_path": str(results_path.resolve()),
        "input_jsonl": str(input_jsonl),
        "output_jsonl": str(output_jsonl),
        "error_jsonl": str(error_jsonl),
        "row_payloads_json": str(row_payloads_json),
        "input_file_id": None,
        "batch_id": None,
        "batch_status": None,
        "completion_window": str(args.completion_window),
        "poll_interval_seconds": int(args.poll_interval_seconds),
        "submitted_rows": len(row_payloads),
        "submitted_qids": _sort_qid_keys(list(row_payloads.keys())),
        "skipped_context_qids": sorted(skipped_context_qids),
        "prebatch_skips_written": len(prebatch_skips),
        "max_context_window": max_context_window,
    }
    save_json(meta_json, metadata)
    print(f"prepare 完成，run_dir: {run_dir}")
    return run_dir


def load_meta_or_fail(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    meta_json = run_dir / "meta.json"
    if not meta_json.exists():
        raise FileNotFoundError(f"meta.json 不存在: {meta_json}")
    return meta_json, load_json(meta_json)


def stage_upload(args: argparse.Namespace, run_dir: Path) -> None:
    meta_json, metadata = load_meta_or_fail(run_dir)
    model_name = str(metadata["model"])
    input_jsonl = Path(metadata["input_jsonl"])

    if not input_jsonl.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_jsonl}")

    client = make_client(model_name)
    input_file_id = upload_input_file(client, input_jsonl)

    metadata["input_file_id"] = input_file_id
    metadata["batch_id"] = None
    metadata["batch_status"] = None
    save_json(meta_json, metadata)
    print(f"upload 完成，input_file_id: {input_file_id}")


def stage_create(args: argparse.Namespace, run_dir: Path) -> str:
    meta_json, metadata = load_meta_or_fail(run_dir)
    model_name = str(metadata["model"])
    input_file_id = metadata.get("input_file_id")
    if not input_file_id:
        raise ValueError("未找到 input_file_id。请先执行 upload。")

    completion_window = str(args.completion_window or metadata.get("completion_window") or "24h")
    client = make_client(model_name)
    batch_id = create_batch(client, str(input_file_id), completion_window=completion_window)
    metadata["batch_id"] = batch_id
    metadata["batch_status"] = "validating"
    metadata["completion_window"] = completion_window
    save_json(meta_json, metadata)
    print(f"create 完成，batch_id: {batch_id}")
    return batch_id


def stage_wait(args: argparse.Namespace, run_dir: Path) -> Any:
    meta_json, metadata = load_meta_or_fail(run_dir)
    model_name = str(metadata["model"])
    batch_id = args.batch_id or metadata.get("batch_id")
    if not batch_id:
        raise ValueError("未找到 batch_id。请先执行 create，或通过 --batch-id 指定。")

    poll_interval = int(args.poll_interval_seconds or metadata.get("poll_interval_seconds") or 10)
    client = make_client(model_name)
    batch = poll_batch(client, str(batch_id), poll_interval_seconds=poll_interval)
    metadata["batch_id"] = str(batch_id)
    metadata["batch_status"] = batch.status
    save_json(meta_json, metadata)
    print(f"wait 完成，最终状态: {batch.status}")
    return batch


def stage_collect(args: argparse.Namespace, run_dir: Path, batch_obj: Any | None = None) -> None:
    meta_json, metadata = load_meta_or_fail(run_dir)
    model_name = str(metadata["model"])
    batch_id = args.batch_id or metadata.get("batch_id")
    if not batch_id:
        raise ValueError("未找到 batch_id。请先执行 create，或通过 --batch-id 指定。")

    client = make_client(model_name)
    batch = batch_obj if batch_obj is not None else client.batches.retrieve(str(batch_id))
    metadata["batch_id"] = str(batch_id)
    metadata["batch_status"] = batch.status

    if batch.status != "completed":
        save_json(meta_json, metadata)
        raise RuntimeError(f"batch 状态不是 completed，当前为: {batch.status}")

    output_file_id = getattr(batch, "output_file_id", None)
    if not output_file_id:
        save_json(meta_json, metadata)
        raise RuntimeError("Batch 已完成但 output_file_id 为空")

    output_jsonl = Path(metadata["output_jsonl"])
    error_jsonl = Path(metadata["error_jsonl"])
    results_path = Path(metadata["results_jsonl"])
    row_payloads_json = Path(metadata["row_payloads_json"])
    if not row_payloads_json.exists():
        raise FileNotFoundError(f"row_payloads.json 不存在: {row_payloads_json}")

    row_payloads_raw = load_json(row_payloads_json)
    row_payloads: dict[str, dict[str, Any]] = {str(k): v for k, v in row_payloads_raw.items()}

    output_content = client.files.content(output_file_id)
    output_text = extract_text_from_file_content(output_content)
    save_text(output_jsonl, output_text)
    metadata["output_file_id"] = output_file_id

    error_file_id = getattr(batch, "error_file_id", None)
    if error_file_id:
        error_content = client.files.content(error_file_id)
        error_text = extract_text_from_file_content(error_content)
        save_text(error_jsonl, error_text)
        metadata["error_file_id"] = error_file_id
    else:
        metadata["error_file_id"] = None

    row_results, failed_from_output = parse_batch_output(output_text, row_payloads)
    submitted = set(str(q) for q in metadata.get("submitted_qids", []))
    if not submitted:
        submitted = set(row_payloads.keys())
    failed_qids: set[str] = set(failed_from_output) | (submitted - set(row_results.keys()))
    success_qids = _sort_qid_keys(set(row_results.keys()) - failed_qids)

    if failed_qids:
        print(f"[SKIP] 以下行 batch 未成功，不写入 results: {sorted(failed_qids)}")

    if not success_qids:
        save_json(meta_json, metadata)
        print("无成功行可写入；请检查 output / error jsonl。")
        return

    h, data_lines = read_jsonl_data_line_strings(results_path)
    already: set[str] = set()
    for raw in data_lines:
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if isinstance(o, dict) and "question_id" in o:
            already.add(str(o["question_id"]))

    n_added = 0
    for qid in success_qids:
        if str(qid) in already:
            print(f"[skip] results 中已存在 question_id={qid!r}，不重复追加")
            continue
        pl = row_payloads[qid]
        pr = row_results[qid]
        rec: dict[str, Any] = {
            "question_id": pl["question_id"],
            "question": pl["question"],
            "gold_answer": pl["gold_answer"],
            "llm_answer": pr["llm_answer"],
            "judge_result": "",
            "prompt_token": int(pr.get("prompt_token", 0)),
            "completion_token": int(pr.get("completion_token", 0)),
        }
        data_lines.append(json.dumps(rec, ensure_ascii=False))
        n_added += 1

    if n_added:
        # collect 不写入首行 _meta_stats；若该文件已存在 main 写过的元数据行则原样保留
        write_jsonl_atomic(results_path, header_line=h, data_line_strings=data_lines)

    metadata["success_qids"] = success_qids
    metadata["failed_qids"] = sorted(failed_qids)
    metadata["collected_to_jsonl"] = str(results_path)
    save_json(meta_json, metadata)
    print(
        f"已更新 jsonl: {results_path}（新追加 {n_added} 行，batch 失败 {len(failed_qids)}）。"
        f" 判分: python main.py --evaluation-file {results_path!s}"
    )
    print(f"中间产物: {run_dir}")


def main() -> None:
    args = parse_args()
    step = str(args.step)
    if step == "submit":
        step = "upload"
    elif step == "poll":
        step = "wait"

    if step in {"upload", "create", "wait", "collect"} and not args.run_dir:
        raise ValueError(f"--step {step} 需要传入 --run-dir")

    if step == "prepare":
        stage_prepare(args)
        return
    if step == "upload":
        stage_upload(args, Path(args.run_dir))
        return
    if step == "create":
        stage_create(args, Path(args.run_dir))
        return
    if step == "wait":
        stage_wait(args, Path(args.run_dir))
        return
    if step == "collect":
        stage_collect(args, Path(args.run_dir))
        return

    run_dir = stage_prepare(args)
    stage_upload(args, run_dir)
    stage_create(args, run_dir)
    b = stage_wait(args, run_dir)
    stage_collect(args, run_dir, batch_obj=b)


if __name__ == "__main__":
    main()