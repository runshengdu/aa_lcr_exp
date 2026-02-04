import asyncio
from typing import Tuple
from utils import ModelConfig, call_chat_completion

def normalize_judge_result(text: str) -> str:
    t = (text or "").strip().upper()
    if "CORRECT" in t and "INCORRECT" not in t:
        return "CORRECT"
    if "INCORRECT" in t:
        return "INCORRECT"
    return "UNKNOWN"


async def grade_answer(
    *,
    question: str,
    gold_answer: str,
    llm_answer: str,
    judge_cfg: ModelConfig,
    encoder,
    context_length: int,
    retries: int,
    semaphore: asyncio.Semaphore = None,
) -> Tuple[str, dict[str, int]]:
    """
    Evaluates the LLM answer against the gold answer using the judge model.
    Returns (judge_result_normalized, usage_dict)
    """
    judge_prompt = (
        "Assess whether the following CANDIDATE ANSWER is CORRECT or INCORRECT.\n"
        "For the CANDIDATE ANSWER to be correct, it must be consistent with the OFFICIAL ANSWER.\n\n"
        f"The question, for reference only: {question}\n"
        f"The OFFICIAL ANSWER: {gold_answer}\n"
        f"CANDIDATE ANSWER TO ASSESS: {llm_answer}\n\n"
        "Reply only with CORRECT or INCORRECT.\n"
    )
    
    judge_max_tokens = min(judge_cfg.max_tokens, max(1, context_length - len(encoder.encode(judge_prompt))))
    
    if semaphore:
        async with semaphore:
            judge_text, usage = await call_chat_completion(
                model_cfg=judge_cfg, prompt=judge_prompt, max_tokens=judge_max_tokens, retries=retries
            )
    else:
        judge_text, usage = await call_chat_completion(
            model_cfg=judge_cfg, prompt=judge_prompt, max_tokens=judge_max_tokens, retries=retries
        )
        
    return normalize_judge_result(judge_text), usage
