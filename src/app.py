import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .nova_client import DEFAULT_INTEGRATOR_MODEL, DEFAULT_MODULE_MODEL, converse_text

app = FastAPI(title="Amazon Nova AI Hackathon Gateway")

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_PATH = BASE_DIR / "prompts" / "system_prompts.json"
STATIC_DIR = BASE_DIR / "static"
SYSTEM_PROMPTS = json.loads(PROMPTS_PATH.read_text(encoding="utf-8-sig"))

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ProblemRequest(BaseModel):
    problem: str


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/evaluate")
def evaluate(req: ProblemRequest):
    problem = req.problem.strip()
    if not problem:
        raise HTTPException(status_code=400, detail="problem must not be empty")

    modules = ["aristotle", "hume", "kahneman", "z_warrior", "schopenhauer"]
    outputs: dict[str, str] = {}

    try:
        for m in modules:
            outputs[m] = converse_text(
                model_id=DEFAULT_MODULE_MODEL,
                user_text=f"Problem: {problem}",
                system_text=SYSTEM_PROMPTS[m],
                temperature=0.3,
            )

        synthesis_input = {
            "problem": problem,
            "module_outputs": outputs,
            "format": {
                "decision": "ALLOW|CAUTION|REFUSE",
                "confidence": "0.0-1.0",
                "rationale": "short explanation",
            },
        }

        final = converse_text(
            model_id=DEFAULT_INTEGRATOR_MODEL,
            user_text=json.dumps(synthesis_input, ensure_ascii=True),
            system_text=SYSTEM_PROMPTS["integrator"],
            temperature=0.2,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Nova invocation failed: {exc}")

    return {
        "problem": problem,
        "modules": outputs,
        "heroic_decision": final,
    }
