"""
Shared dataset loading, prompt building, and jsonl layout for AA-LCR.
Used by main.py and batch_api/qwen/qwen.py.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any, Tuple

import yaml

from src.utils import ModelConfig, expand_env_vars

# Repository root: parent of `src/`
REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_CSV_PATH = REPO_ROOT / "dataset/AA-LCR_Dataset.csv"
EXTRACTED_TEXT_ROOT = REPO_ROOT / "dataset/AA-LCR_extracted-text"
MODELS_YAML_PATH = REPO_ROOT / "models.yaml"

DEFAULT_CONTEXT_LENGTH_TOKENS = int(256000 * 0.9)
DEFAULT_RETRIES = 3
DEFAULT_MAX_CONCURRENCY = 20
DEFAULT_EVAL_WORKERS = 50

_JSONL_HEADER_PAD = 200


def load_models_yaml(path: Path) -> dict[str, ModelConfig]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found.")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = data.get("models", [])
    if not isinstance(models, list):
        raise RuntimeError(f"Invalid models list in {path}.")

    out: dict[str, ModelConfig] = {}
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
    texts: list[str] = []
    for filename in data_source_filenames:
        doc_path = doc_dir / filename
        if doc_path.exists():
            texts.append(doc_path.read_text(encoding="utf-8", errors="replace"))
    return texts


def result_base_for_row(row: dict) -> dict[str, Any]:
    qid = str(row.get("question_id", "")).strip()
    question = str(row.get("question", "")).strip()
    gold_answer = str(row.get("answer", "")).strip()
    return {
        "question_id": qid,
        "question": question,
        "gold_answer": gold_answer,
        "llm_answer": "",
        "judge_result": "SKIPPED",
        "prompt_token": 0,
        "completion_token": 0,
    }


def get_task_prompt_for_row_or_skip(
    row: dict,
    encoder: Any,
    context_length: int,
    model_cfg: ModelConfig,
) -> dict[str, Any]:
    """
    If the row can be sent to the model, returns
      { 'ok': True, 'task_prompt', 'max_completion_tokens', 'result_base', ... }.
    Otherwise returns { 'ok': False, 'record': full result dict to return } with skipped_reason.
    """
    result_base = result_base_for_row(row)
    try:
        filenames = row.get("data_source_filenames", [])
        if not isinstance(filenames, list):
            filenames = []

        docs = load_document_set(
            str(row.get("document_category", "")).strip(),
            str(row.get("document_set_id", "")).strip(),
            filenames,
        )

        doc_tokens_sum = sum(len(encoder.encode(d)) for d in docs)
        if doc_tokens_sum > context_length:
            return {
                "ok": False,
                "record": {
                    **result_base,
                    "skipped_reason": f"docs_tokens_sum({doc_tokens_sum}) > context_length({context_length})",
                },
            }

        documents_text = "\n\n".join(
            f"BEGIN DOCUMENT {i+1}:\n{d}\nEND DOCUMENT {i+1}" for i, d in enumerate(docs)
        )
        task_prompt = (
            f"BEGIN INPUT DOCUMENTS\n\n{documents_text}\n\nEND INPUT DOCUMENTS\n\n"
            f"Answer the following question using the input documents provided above.\n\n"
            f"START QUESTION\n\n{result_base['question']}\n\nEND QUESTION\n"
        )

        prompt_tokens_est = len(encoder.encode(task_prompt))
        max_completion_tokens = min(model_cfg.max_tokens, max(0, context_length - prompt_tokens_est))

        if max_completion_tokens <= 0:
            return {
                "ok": False,
                "record": {
                    **result_base,
                    "skipped_reason": f"prompt_tokens({prompt_tokens_est}) >= context_length({context_length})",
                },
            }

        return {
            "ok": True,
            "task_prompt": task_prompt,
            "max_completion_tokens": int(max_completion_tokens),
            "prompt_tokens_est": int(prompt_tokens_est),
            "result_base": result_base,
        }
    except Exception as e:
        return {
            "ok": False,
            "record": {
                **result_base,
                "judge_result": "ERROR",
                "error": f"{type(e).__name__}: {e}",
            },
        }


def make_jsonl_stats_line(correct: int, total: int) -> str:
    acc = (correct / total * 100) if total > 0 else 0.0
    data = {
        "_meta_stats": True,
        "accuracy": f"{acc:.2f}%",
        "correct": correct,
        "total": total,
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    json_str = json.dumps(data, ensure_ascii=False)
    return f"{json_str:<{_JSONL_HEADER_PAD}}\n"


def is_judge_result_empty(obj: dict[str, Any]) -> bool:
    jr = obj.get("judge_result")
    return jr is None or (isinstance(jr, str) and jr.strip() == "")


def need_evaluation(obj: dict[str, Any]) -> bool:
    """可判分、且尚未有 judge 结论的样本（judge_result 空）。SKIPPED/已有结论不评。"""
    if obj.get("judge_result") == "SKIPPED" or "skipped_reason" in obj:
        return False
    if not is_judge_result_empty(obj):
        return False
    if not str(obj.get("llm_answer", "")).strip():
        return False
    return True


def count_stats_4a(data_objects: list[dict[str, Any]]) -> tuple[int, int]:
    """
    决策 4A: total 仅统计已有最终 judge 的题（CORRECT/INCORRECT/UNKNOWN）；
    correct 为其中 CORRECT 数。未评估（空）与 ERROR、SKIPPED 等不计入 total。
    """
    scorable = {"CORRECT", "INCORRECT", "UNKNOWN"}
    tot = 0
    cor = 0
    for o in data_objects:
        jr = o.get("judge_result")
        if not isinstance(jr, str) or jr not in scorable:
            continue
        tot += 1
        if jr == "CORRECT":
            cor += 1
    return cor, tot


def read_jsonl_data_line_strings(path: Path) -> tuple[str | None, list[str]]:
    """
    返回 (首行元数据行, 数据行)。若无首行或首行非 _meta_stats，则 (None, 全部非空行)。
    行内容不含换行符。
    """
    if not path.exists():
        return None, []
    lines_raw: list[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            t = line.rstrip("\n\r")
            if t == "":
                continue
            lines_raw.append(t)
    if not lines_raw:
        return None, []
    first_obj: Any = None
    try:
        first_obj = json.loads(lines_raw[0])
    except Exception:
        return None, lines_raw
    if isinstance(first_obj, dict) and first_obj.get("_meta_stats"):
        return lines_raw[0], lines_raw[1:]
    return None, lines_raw


def read_results_jsonl_state(
    save_path: Path,
) -> Tuple[set[str], list[str], int, int]:
    if not save_path.exists():
        return set(), [], 0, 0
    _header_line, data_line_strings = read_jsonl_data_line_strings(save_path)
    done_ids: set[str] = set()
    data_objs: list[dict[str, Any]] = []
    for line in data_line_strings:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("_meta_stats"):
            continue
        if "question_id" in obj:
            done_ids.add(str(obj["question_id"]))
        data_objs.append(obj)
    correct_count, total_count = count_stats_4a(data_objs)
    return done_ids, data_line_strings, correct_count, total_count


def write_jsonl_atomic(
    path: Path,
    *,
    header_line: str | None,
    data_line_strings: list[str],
) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    out_lines: list[str] = []
    if header_line is not None:
        h = header_line
        if not h.endswith("\n"):
            h = h + "\n"
        out_lines.append(h)
    for s in data_line_strings:
        if s.endswith("\n"):
            s = s.rstrip("\n")
        if s:
            out_lines.append(s + "\n")
    tmp = path.parent / f".{path.name}.tmp"
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write("".join(out_lines))
    os.replace(str(tmp), str(path))


def safe_filename_component(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip())
    s = s.strip("._-") or "model"
    if len(s) > 120:
        s = s[:120]
    return s


def build_knowledge_completion_body(
    model_cfg: ModelConfig,
    task_prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model_cfg.model_id,
        "messages": [{"role": "user", "content": task_prompt}],
        "temperature": model_cfg.temperature,
        "max_tokens": int(max_tokens),
    }
    if model_cfg.extra_body and isinstance(model_cfg.extra_body, dict):
        body.update(model_cfg.extra_body)
    return body
