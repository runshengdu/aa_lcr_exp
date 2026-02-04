import argparse
import asyncio
import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any
import yaml
import tiktoken
from tqdm import tqdm
from utils import ModelConfig, expand_env_vars, call_chat_completion
from grader import grade_answer

# Configuration
BASE_DIR = Path(__file__).parent
DATASET_CSV_PATH = BASE_DIR / "dataset/AA-LCR_Dataset.csv"
EXTRACTED_TEXT_ROOT = BASE_DIR / "dataset/AA-LCR_extracted-text"
MODELS_YAML_PATH = BASE_DIR / "models.yaml"

DEFAULT_CONTEXT_LENGTH_TOKENS = int(128000*0.9)
DEFAULT_JUDGE_MODEL_ID = "doubao-seed-1-8-251228"
DEFAULT_MAX_CONCURRENCY = 20
DEFAULT_RETRIES = 3


def load_models_yaml(path: Path) -> dict[str, ModelConfig]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")
        
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = data.get("models", [])
    if not isinstance(models, list):
        raise RuntimeError(f"Invalid models list in {path}.")

    out = {}
    for m in models:
        if not isinstance(m, dict) or not (name := str(m.get("name", "")).strip()):
            continue

        if not (api_key := str(m.get("api_key", "")).strip()):
            raise RuntimeError(f"Missing api_key for model {name} in {path}.")
            
        if not (base_url := str(m.get("base_url", "")).strip()):
            raise RuntimeError(f"Missing base_url for model {name} in {path}.")

        out[name] = ModelConfig(
            model_id=name,
            temperature=float(m.get("temperature", 1.0)),
            base_url=base_url,
            api_key=expand_env_vars(api_key),
            extra_body=m.get("extra_body") if isinstance(m.get("extra_body"), dict) else None,
            max_tokens=int(m.get("max_tokens", 2048)),
        )
    return out


def load_questions(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        return [
            {**row, "data_source_filenames": row["data_source_filenames"].split(";")}
            if "data_source_filenames" in row and isinstance(row["data_source_filenames"], str)
            else row
            for row in csv.DictReader(f)
        ]


def load_document_set(document_category: str, document_set_id: str, data_source_filenames: list[str]) -> list[str]:
    doc_dir = EXTRACTED_TEXT_ROOT / document_category / document_set_id
    texts = []
    for filename in data_source_filenames:
        doc_path = doc_dir / filename
        if doc_path.exists():
            texts.append(doc_path.read_text(encoding="utf-8", errors="replace"))
    return texts


async def process_one_question(
    *,
    row: dict,
    model_cfg: ModelConfig,
    judge_cfg: ModelConfig,
    encoder,
    context_length: int,
    retries: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    qid = str(row.get("question_id", "")).strip()
    question = str(row.get("question", "")).strip()
    gold_answer = str(row.get("answer", "")).strip()
    
    result_base = {
        "question_id": qid,
        "question": question,
        "gold_answer": gold_answer,
        "llm_answer": "",
        "judge_result": "SKIPPED",
        "prompt_token": 0,
        "completion_token": 0,
    }

    try:
        filenames = row.get("data_source_filenames", [])
        if not isinstance(filenames, list):
            filenames = []
            
        docs = load_document_set(
            str(row.get("document_category", "")).strip(),
            str(row.get("document_set_id", "")).strip(),
            filenames
        )
        
        doc_tokens_sum = sum(len(encoder.encode(d)) for d in docs)
        if doc_tokens_sum > context_length:
            return {**result_base, "skipped_reason": f"docs_tokens_sum({doc_tokens_sum}) > context_length({context_length})"}

        documents_text = "\n\n".join(f"BEGIN DOCUMENT {i+1}:\n{d}\nEND DOCUMENT {i+1}" for i, d in enumerate(docs))
        task_prompt = (
            f"BEGIN INPUT DOCUMENTS\n\n{documents_text}\n\nEND INPUT DOCUMENTS\n\n"
            f"Answer the following question using the input documents provided above.\n\n"
            f"START QUESTION\n\n{question}\n\nEND QUESTION\n"
        )
        
        prompt_tokens_est = len(encoder.encode(task_prompt))
        max_completion_tokens = min(model_cfg.max_tokens, max(0, context_length - prompt_tokens_est))
        
        if max_completion_tokens <= 0:
            return {**result_base, "skipped_reason": f"prompt_tokens({prompt_tokens_est}) >= context_length({context_length})"}

        async with semaphore:
            llm_answer, usage = await call_chat_completion(
                model_cfg=model_cfg, prompt=task_prompt, max_tokens=max_completion_tokens, retries=retries
            )

        judge_result, judge_usage = await grade_answer(
            question=question,
            gold_answer=gold_answer,
            llm_answer=llm_answer,
            judge_cfg=judge_cfg,
            encoder=encoder,
            context_length=context_length,
            retries=retries,
            semaphore=semaphore,
        )

        return {
            **result_base,
            "llm_answer": llm_answer,
            "judge_result": judge_result,
            "prompt_token": usage["prompt_tokens"],
            "completion_token": usage["completion_tokens"],
        }
        
    except Exception as e:
        return {**result_base, "judge_result": "ERROR", "error": f"{type(e).__name__}: {e}"}


async def run(args: argparse.Namespace) -> int:
    models = load_models_yaml(MODELS_YAML_PATH)
    if args.model_id not in models:
        raise SystemExit(f"--model-id not found in {MODELS_YAML_PATH}: {args.model_id}")
    if DEFAULT_JUDGE_MODEL_ID not in models:
        raise SystemExit(f"Judge model {DEFAULT_JUDGE_MODEL_ID} not found in {MODELS_YAML_PATH}")

    model_cfg = models[args.model_id]
    judge_cfg = models[DEFAULT_JUDGE_MODEL_ID]
    
    save_path = Path(args.save_to) if args.save_to else Path("results") / args.model_id.replace("/", "__") / f"{dt.datetime.now():%Y%m%d_%H%M%S}.jsonl"
    save_path.parent.mkdir(parents=True, exist_ok=True)

    done_ids = set()
    if save_path.exists():
        with save_path.open("r", encoding="utf-8") as f:
            for line in f:
                if (s := line.strip()) and (obj := json.loads(s)) and "question_id" in obj:
                    done_ids.add(str(obj["question_id"]))

    rows = sorted(load_questions(DATASET_CSV_PATH), key=lambda r: int(str(r.get("question_id", "0") or "0")))
    pending_rows = [r for r in rows if str(r.get("question_id", "")).strip() not in done_ids]
    if args.num_tasks is not None:
        pending_rows = pending_rows[: args.num_tasks]

    if not pending_rows and not save_path.exists():
        return 0

    encoder = tiktoken.get_encoding("cl100k_base")
    semaphore = asyncio.Semaphore(args.max_concurrency)

    # Stats initialization
    correct_count = 0
    total_count = 0
    
    # Check existing file content
    existing_lines = []
    if save_path.exists():
        with save_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    # Check if it is a stats header
                    if "_meta_stats" in obj:
                        continue
                    existing_lines.append(line)
                    total_count += 1
                    if obj.get("judge_result") == "CORRECT":
                        correct_count += 1
                except Exception:
                    pass

    # Function to create padded header
    def make_header(correct, total):
        acc = (correct / total * 100) if total > 0 else 0.0
        data = {
            "_meta_stats": True,
            "accuracy": f"{acc:.2f}%",
            "correct": correct,
            "total": total,
            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        json_str = json.dumps(data)
        # Pad to 200 chars to ensure enough space for future updates without shifting content
        return f"{json_str:<200}\n"

    # Rewrite file with header + existing content
    with save_path.open("w", encoding="utf-8") as f:
        f.write(make_header(correct_count, total_count))
        for line in existing_lines:
            f.write(line + "\n")

    if not pending_rows:
        return 0

    async def worker(row):
        return await process_one_question(
            row=row,
            model_cfg=model_cfg,
            judge_cfg=judge_cfg,
            encoder=encoder,
            context_length=DEFAULT_CONTEXT_LENGTH_TOKENS,
            retries=DEFAULT_RETRIES,
            semaphore=semaphore,
        )

    tasks = [asyncio.create_task(worker(r)) for r in pending_rows]
    
    # Progress bar setup
    pbar = tqdm(total=len(tasks), desc="Processing", unit="task")
    failed_count = 0

    # Open in r+ mode to allow seeking to beginning for header updates
    with save_path.open("r+", encoding="utf-8") as out_f:
        # Move to end for appending new results
        out_f.seek(0, 2)
        
        for coro in asyncio.as_completed(tasks):
            result = await coro
            
            # Check for failure
            if result.get("judge_result") == "ERROR":
                failed_count += 1
                qid = result.get("question_id", "Unknown")
                err_msg = result.get("error", "Unknown error")
                pbar.write(f"FAILED [QID: {qid}]: {err_msg}")
                pbar.set_postfix(failed=failed_count, refresh=False)
                pbar.update(1)
                continue  # Skip writing to file

            # Update stats
            total_count += 1
            if result.get("judge_result") == "CORRECT":
                correct_count += 1
            
            # Write result at the end
            out_f.write(json.dumps(result, ensure_ascii=False) + "\n")
            
            # Update header at the beginning
            current_pos = out_f.tell()
            out_f.seek(0)
            out_f.write(make_header(correct_count, total_count))
            out_f.seek(current_pos)
            out_f.flush()
            
            pbar.set_postfix(failed=failed_count, refresh=False)
            pbar.update(1)
            
    pbar.close()

    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", type=str, default=DEFAULT_JUDGE_MODEL_ID)
    parser.add_argument("--num-tasks", type=int)
    parser.add_argument("--save-to")
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
