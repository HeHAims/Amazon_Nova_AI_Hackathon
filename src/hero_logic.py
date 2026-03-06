import json
from pathlib import Path

from nova_client import DEFAULT_INTEGRATOR_MODEL, DEFAULT_MODULE_MODEL, converse_with_meta
from trace_logger import write_trace

PROMPTS_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompts.json"
SYSTEM_PROMPTS = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))


def _score_from_text(text: str) -> float:
    # Lightweight heuristic score placeholder for demo tracing.
    return round(min(1.0, max(0.0, len(text) / 500.0)), 3)


def run_hero_council(user_prompt: str):
    module_order = ["aristotle", "hume", "kahneman", "schopenhauer", "z_warrior"]

    try:
        module_outputs: dict[str, str] = {}
        lite_request_ids: list[str] = []

        for module in module_order:
            text, req_id, _raw = converse_with_meta(
                model_id=DEFAULT_MODULE_MODEL,
                user_text=f"Problem: {user_prompt}",
                system_text=SYSTEM_PROMPTS[module],
                temperature=0.3,
            )
            module_outputs[module] = text
            if req_id:
                lite_request_ids.append(req_id)

        logic_score = _score_from_text(module_outputs["aristotle"])
        bias_score = _score_from_text(module_outputs["kahneman"])
        suffering_score = _score_from_text(module_outputs["schopenhauer"])
        z_score = _score_from_text(module_outputs["z_warrior"])
        R = round((logic_score + (1 - bias_score) + (1 - suffering_score) + z_score) / 4, 3)

        synthesis_payload = {
            "problem": user_prompt,
            "modules": module_outputs,
            "scores": {
                "logic": logic_score,
                "bias_penalty": bias_score,
                "suffering_factor": suffering_score,
                "resilience": z_score,
                "R_final": R,
            },
        }

        final_text, pro_request_id, _pro_raw = converse_with_meta(
            model_id=DEFAULT_INTEGRATOR_MODEL,
            user_text=json.dumps(synthesis_payload, ensure_ascii=True),
            system_text=SYSTEM_PROMPTS["integrator"],
            temperature=0.2,
        )

        write_trace(
            prompt=user_prompt,
            modules_executed=[
                "Aristotle",
                "Hume",
                "Kahneman",
                "Schopenhauer",
                "Z_Warrior",
            ],
            nova_request_ids={
                "pro_request_ids": [pro_request_id] if pro_request_id else [],
                "lite_request_ids": lite_request_ids,
            },
            scores={
                "logic": logic_score,
                "bias_penalty": bias_score,
                "suffering_factor": suffering_score,
                "resilience": z_score,
                "R_final": R,
            },
            decision="Resilient Action Recommended",
            error=None,
        )

        return {
            "modules": module_outputs,
            "decision": final_text,
            "scores": {
                "logic": logic_score,
                "bias_penalty": bias_score,
                "suffering_factor": suffering_score,
                "resilience": z_score,
                "R_final": R,
            },
        }

    except Exception as e:
        write_trace(
            prompt=user_prompt,
            modules_executed=["Partial"],
            nova_request_ids={},
            scores={},
            decision="Failed",
            error={
                "type": type(e).__name__,
                "message": str(e),
            },
        )
        raise


if __name__ == "__main__":
    prompt = "A patient needs a 5% success-rate surgery that causes extreme pain."
    result = run_hero_council(prompt)
    print("Hero Council Result:\n", json.dumps(result, indent=2))
