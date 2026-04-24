import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any
import tiktoken
from tqdm import tqdm

from src.aa_lcr import (
    DATASET_CSV_PATH,
    MODELS_YAML_PATH,
    DEFAULT_CONTEXT_LENGTH_TOKENS,
    DEFAULT_RETRIES,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_EVAL_WORKERS,
    get_task_prompt_for_row_or_skip,
    load_models_yaml,
    load_questions,
    make_jsonl_stats_line,
    read_jsonl_data_line_strings,
    read_results_jsonl_state,
    need_evaluation,
    count_stats_4a,
    write_jsonl_atomic,
)
from src.utils import ModelConfig, call_chat_completion
from src.grader import grade_answer


async def process_one_question_generate(
    *,
    row: dict,
    model_cfg: ModelConfig,
    encoder,
    context_length: int,
    retries: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """仅生成，不含 judge；成功时 judge_result 为空串。"""
    prep = get_task_prompt_for_row_or_skip(
        row=row, encoder=encoder, context_length=context_length, model_cfg=model_cfg
    )
    if not prep.get("ok"):
        return prep["record"]

    result_base = prep["result_base"]
    task_prompt = prep["task_prompt"]
    max_completion_tokens = prep["max_completion_tokens"]

    try:
        async with semaphore:
            llm_answer, usage = await call_chat_completion(
                model_cfg=model_cfg, prompt=task_prompt, max_tokens=max_completion_tokens, retries=retries
            )

        return {
            **result_base,
            "llm_answer": llm_answer,
            "judge_result": "",
            "prompt_token": usage["prompt_tokens"],
            "completion_token": usage["completion_tokens"],
        }
    except Exception as e:
        return {**result_base, "judge_result": "ERROR", "error": f"{type(e).__name__}: {e}"}


async def run_generate(args: argparse.Namespace) -> int:
    models = load_models_yaml(MODELS_YAML_PATH)
    if not args.model_id or args.model_id not in models:
        raise SystemExit(f"--model-id 必填，且在 {MODELS_YAML_PATH} 中存在: {getattr(args, 'model_id', None)!r}")
    if args.evaluation_file is not None:
        raise SystemExit("生成模式请不要传 --evaluation-file")

    model_cfg = models[args.model_id]

    save_path = (
        Path(args.save_to)
        if args.save_to
        else Path("results") / args.model_id.replace("/", "__") / f"{dt.datetime.now():%Y%m%d_%H%M%S}.jsonl"
    )
    save_path.parent.mkdir(parents=True, exist_ok=True)

    done_ids, existing_data_lines, _c, _t = read_results_jsonl_state(save_path)
    all_data_lines: list[str] = list(existing_data_lines)

    rows = sorted(load_questions(DATASET_CSV_PATH), key=lambda r: int(str(r.get("question_id", "0") or "0")))
    pending_rows = [r for r in rows if str(r.get("question_id", "")).strip() not in done_ids]
    if args.num_tasks is not None:
        pending_rows = pending_rows[: args.num_tasks]

    if not pending_rows:
        return 0

    encoder = tiktoken.get_encoding("cl100k_base")
    sem = asyncio.Semaphore(args.gen_workers)
    pbar = tqdm(total=len(pending_rows), desc="Generate", unit="task")
    failed = 0

    tasks = [
        asyncio.create_task(
            process_one_question_generate(
                row=r,
                model_cfg=model_cfg,
                encoder=encoder,
                context_length=DEFAULT_CONTEXT_LENGTH_TOKENS,
                retries=DEFAULT_RETRIES,
                semaphore=sem,
            )
        )
        for r in pending_rows
    ]
    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result.get("judge_result") == "ERROR":
            if "error" in result and result.get("error") is not None:
                failed += 1
                pbar.write(f"FAILED [QID: {result.get('question_id')}]: {result.get('error', '')}")
            pbar.set_postfix(failed=failed, refresh=False)
            pbar.update(1)
            continue
        all_data_lines.append(json.dumps(result, ensure_ascii=False))
        write_jsonl_atomic(save_path, header_line=None, data_line_strings=all_data_lines)
        pbar.set_postfix(failed=failed, refresh=False)
        pbar.update(1)
    pbar.close()
    return 0


async def run_evaluate(args: argparse.Namespace) -> int:
    path = Path(args.evaluation_file)
    if not path.is_file():
        raise SystemExit(f"--evaluation-file 不存在: {path}")

    models = load_models_yaml(MODELS_YAML_PATH)
    if args.judge_id not in models:
        raise SystemExit(f"Judge {args.judge_id!r} not found in {MODELS_YAML_PATH}")
    judge_cfg = models[args.judge_id]

    _h, data_line_strings = read_jsonl_data_line_strings(path)
    data_objs: list[dict[str, Any]] = []
    for line in data_line_strings:
        try:
            o = json.loads(line)
        except Exception as e:
            raise SystemExit(f"非合法 JSON 的数据行: {e}") from e
        if not isinstance(o, dict):
            raise SystemExit("仅支持每行为一个 JSON 对象。")
        data_objs.append(o)

    idx_work = [i for i, o in enumerate(data_objs) if need_evaluation(o)]
    encoder = tiktoken.get_encoding("cl100k_base")
    sem = asyncio.Semaphore(int(args.eval_workers))
    pbar = tqdm(total=len(idx_work), desc="Evaluate", unit="row")

    async def eval_one(i: int) -> None:
        o = data_objs[i]
        try:
            jr, _u = await grade_answer(
                question=str(o.get("question", "")),
                gold_answer=str(o.get("gold_answer", "")),
                llm_answer=str(o.get("llm_answer", "")),
                judge_cfg=judge_cfg,
                encoder=encoder,
                context_length=DEFAULT_CONTEXT_LENGTH_TOKENS,
                retries=int(args.retries),
                semaphore=sem,
            )
        except Exception as e:
            o["judge_result"] = "ERROR"
            o["error"] = f"{type(e).__name__}: {e}"
        else:
            o["judge_result"] = jr
        finally:
            pbar.update(1)

    try:
        if idx_work:
            await asyncio.gather(*[eval_one(i) for i in idx_work])
    finally:
        pbar.close()

    c, t = count_stats_4a(data_objs)
    hdr = make_jsonl_stats_line(c, t)
    new_lines = [json.dumps(o, ensure_ascii=False) for o in data_objs]
    write_jsonl_atomic(path, header_line=hdr, data_line_strings=new_lines)
    print(f"已写入首行元数据并原子替换。correct={c} total(判分)={t} path={path}")
    return 0


def _async_run(coro) -> int:
    return int(asyncio.run(coro) or 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evaluation-file",
        type=str,
        default=None,
        metavar="PATH",
        help="对 jsonl 中 judge_result 为空、且可判分（非 SKIPPED）的样本做判分，并写入首行 _meta_stats。与生成模式二选一。",
    )
    parser.add_argument(
        "--eval-workers",
        type=int,
        default=DEFAULT_EVAL_WORKERS,
        help="评估阶段并发度（仅 --evaluation-file 时有效）。",
    )
    parser.add_argument("--gen-workers", type=int, default=DEFAULT_MAX_CONCURRENCY, help="生成阶段并发度。")
    parser.add_argument(
        "--model-id",
        type=str,
        default=None,
        help="生成用模型（--evaluation-file 时不需要）。",
    )
    parser.add_argument(
        "--judge-id",
        type=str,
        default="qwen3.5-plus",
        help="仅 --evaluation-file 时使用。",
    )
    parser.add_argument("--num-tasks", type=int)
    parser.add_argument("--save-to")
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="与 judge 的 chat 重试次（主生成与 --evaluation-file 下 judge 均适用）。",
    )
    args = parser.parse_args()

    if args.evaluation_file is not None:
        return _async_run(run_evaluate(args))
    if not args.model_id:
        raise SystemExit("未指定 --evaluation-file 时，需要 --model-id 做生成。")
    return _async_run(run_generate(args))


if __name__ == "__main__":
    try:
        raise SystemExit(main() or 0)
    except KeyboardInterrupt:
        raise SystemExit(130)
