import hashlib
import datetime
import os
import re
import sys
import time
import uuid
from typing import Any

import boto3
from azure.cosmos import CosmosClient, PartitionKey, exceptions as cosmos_exceptions
from botocore.exceptions import ClientError, NoCredentialsError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

THRESHOLD = float(os.getenv("CMIS_THRESHOLD", "0.75"))
CONSTITUTION_HASH = os.getenv("CMIS_CONSTITUTION_HASH", "CMIS_CONST_V1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "")
COSMOS_CON_STR = os.getenv("COSMOS_DB_CONNECTION_STRING", "")
COSMOS_DATABASE_NAME = os.getenv("COSMOS_DB_NAME", "cmis-database")
COSMOS_CONTAINER_NAME = os.getenv("COSMOS_CONTAINER_NAME", "traces")
MAX_PROMPT_LENGTH = int(os.getenv("MAX_PROMPT_LENGTH", "4000"))
DEMO_MODE = os.getenv("CMIS_DEMO_MODE", "1").strip().lower() not in {"0", "false", "no", "off"}
CMIS_POLICY_PROFILE = os.getenv("CMIS_POLICY_PROFILE", "DEMO" if DEMO_MODE else "SCHOOL").strip().upper()
STRICT_MODE = os.getenv("CMIS_STRICT_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
ALLOWED_POLICY_PROFILES = {"SCHOOL", "DEMO"}
ALLOWED_DEBUG_MODES = {"STRICT", "STANDARD"}
DEFAULT_POLICY_PROFILE = "SCHOOL"
DEFAULT_DEBUG_MODE = "STRICT"

UNSAFE_SIGNAL_KEYWORDS = {
    "hack",
    "exploit",
    "malware",
    "phishing",
    "ddos",
    "ransomware",
    "intrusion",
    "breach",
    "bypass",
    "steal",
    "credential",
}


def validate_aws_startup() -> None:
    print("[CMIS-NODE] Initializing pre-flight check...")

    required_vars = ["AWS_REGION", "BEDROCK_MODEL_ID"]
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        print(f"CRITICAL ERROR: Missing environment variables: {', '.join(missing)}")
        print("FIX: Set AWS_REGION and BEDROCK_MODEL_ID before starting the server.")
        sys.exit(1)

    try:
        sts = boto3.client("sts", region_name=AWS_REGION)
        identity = sts.get_caller_identity()
        print(f"✅ AWS Identity Verified: {identity['Arn']}")
    except (NoCredentialsError, ClientError):
        print("CRITICAL ERROR: Invalid or missing AWS credentials.")
        print("FIX: Run aws configure or set a valid AWS_PROFILE before starting uvicorn.")
        sys.exit(1)


validate_aws_startup()

bedrock_runtime = boto3.client(service_name="bedrock-runtime", region_name=AWS_REGION)


def init_cosmos_container():
    if not COSMOS_CON_STR:
        print("WARN: COSMOS_DB_CONNECTION_STRING not set. Trace persistence disabled.")
        return None

    try:
        client = CosmosClient.from_connection_string(COSMOS_CON_STR)
        database = client.create_database_if_not_exists(id=COSMOS_DATABASE_NAME)
        container = database.create_container_if_not_exists(
            id=COSMOS_CONTAINER_NAME,
            partition_key=PartitionKey(path="/trace_id"),
            offer_throughput=400,
        )
        print("✅ Cosmos DB 'Memory' Connected")
        return container
    except cosmos_exceptions.CosmosHttpResponseError as exc:
        print(f"WARN: Cosmos DB connection failed: {exc}")
        return None
    except Exception as exc:
        print(f"WARN: Cosmos DB initialization failed: {exc}")
        return None


cosmos_container = init_cosmos_container()


def save_cmis_trace(
    prompt: str,
    response: str,
    logic_path: str,
    trace_id: str,
    status: str,
    decision: str,
    score: float | None = None,
    reason: str | None = None,
) -> None:
    if cosmos_container is None:
        return

    trace_data: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "trace_id": trace_id or str(uuid.uuid4()),
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "prompt": prompt,
        "response": response,
        "logic_path": logic_path,
        "status": status,
        "decision": decision,
    }
    if score is not None:
        trace_data["score"] = score
    if reason:
        trace_data["reason"] = reason

    try:
        cosmos_container.upsert_item(trace_data)
        print(f"Trace saved to Cosmos DB: {trace_data['id']}")
    except Exception as exc:
        print(f"WARN: Failed to save trace to Cosmos DB: {exc}")

app.mount("/static", StaticFiles(directory="static"), name="static")


class PromptRequest(BaseModel):
    prompt: str


class DebugGovernanceRequest(BaseModel):
    prompt: str
    policy_profile: str = DEFAULT_POLICY_PROFILE
    mode: str = DEFAULT_DEBUG_MODE


class ContactRequest(BaseModel):
    name: str
    email: str
    company: str
    topic: str
    message: str


def validate_prompt(prompt: str) -> str:
    normalized = (prompt or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "EmptyPrompt",
                "message": "Prompt must not be empty.",
                "friendly_sentry_message": "Sentry check: please enter a prompt so governance can evaluate it.",
            },
        )

    if len(normalized) > MAX_PROMPT_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "PromptTooLong",
                "message": (
                    f"Prompt length ({len(normalized)}) exceeds the allowed limit "
                    f"({MAX_PROMPT_LENGTH})."
                ),
                "friendly_sentry_message": "Sentry check: your prompt is too long. Please shorten it and try again.",
            },
        )

    return normalized


def extract_signals(prompt: str) -> list[str]:
    lowered = prompt.lower()
    signals = sorted([word for word in UNSAFE_SIGNAL_KEYWORDS if word in lowered])
    return signals


def unsafe_signals_detected(signals: list[str]) -> bool:
    return len(signals) > 0


def classify_prompt(signals: list[str]) -> str:
    if not signals:
        return "general"

    if any(word in signals for word in {"hack", "exploit", "intrusion", "bypass", "malware", "phishing"}):
        return "ethics"

    return "conflict"


def determine_scope(classification: str) -> str:
    if classification in {"ethics", "conflict"}:
        return "ethics_review"
    return "standard"


def load_active_policy_profile(profile_name: str = DEFAULT_POLICY_PROFILE) -> dict[str, Any]:
    normalized_profile = profile_name.strip().upper()
    if normalized_profile == "SCHOOL":
        return {
            "name": "SCHOOL",
            "domain": "school",
            "rules": ["network_intrusion_block", "student_safety_required"],
        }

    return {
        "name": "DEMO",
        "domain": "demo",
        "rules": ["network_intrusion_block"],
    }


def enforce_constraints(envelope: dict[str, Any]) -> dict[str, Any]:
    violations: list[str] = []
    if unsafe_signals_detected(envelope["signals"]):
        violations.append("UnsafeSignalsDetected")

    if envelope["score"] > THRESHOLD:
        violations.append("ThresholdExceeded")

    envelope["violations"] = violations
    if violations:
        envelope["decision"] = "REFUSE"
        envelope["reason"] = violations[0]

    return envelope


def CMIS_ANALYZE(prompt: str, deterministic: bool = False, policy_profile: str = DEFAULT_POLICY_PROFILE) -> dict[str, Any]:
    score = round(len(prompt) / 500, 3)
    profile_name = policy_profile.strip().upper()
    if deterministic:
        trace_seed = f"{CONSTITUTION_HASH}:{profile_name}:{prompt}:STRICT"
    else:
        trace_seed = f"{CONSTITUTION_HASH}:{prompt}:{time.time()}"
    trace_id = hashlib.sha256(trace_seed.encode()).hexdigest()[:12]

    signals = extract_signals(prompt)
    classification = classify_prompt(signals)
    scope = determine_scope(classification)
    confidence = 0.95 if signals else 0.72

    envelope: dict[str, Any] = {
        "trace_id": trace_id,
        "score": score,
        "decision": "ALLOW",
        "classification": classification,
        "scope": scope,
        "confidence": confidence,
        "signals": signals,
        "violations": [],
        "reason": None,
    }

    return enforce_constraints(envelope)


def CanonicalGovernanceGate(prompt: str) -> dict[str, Any]:
    state = CMIS_ANALYZE(prompt)
    if state["decision"] == "REFUSE":
        return {
            "decision": "REFUSE",
            "trace_id": state["trace_id"],
            "score": state["score"],
            "reason": state.get("reason") or "GovernanceBlocked",
            "classification": state["classification"],
            "scope": state["scope"],
            "confidence": state["confidence"],
            "signals": state["signals"],
            "violations": state["violations"],
        }

    return {
        "decision": "ALLOW",
        "trace_id": state["trace_id"],
        "score": state["score"],
        "classification": state["classification"],
        "scope": state["scope"],
        "confidence": state["confidence"],
        "signals": state["signals"],
        "violations": state["violations"],
    }


def InvokeModel(prompt: str) -> tuple[str, str]:
    response = bedrock_runtime.converse(
        modelId=MODEL_ID,
        messages=[
            {
                "role": "user",
                "content": [{"text": prompt}],
            }
        ],
        inferenceConfig={
            "maxTokens": 100,
            "temperature": 0.2,
        },
    )

    content = (((response or {}).get("output") or {}).get("message") or {}).get("content", [])
    output_text = next((item.get("text") for item in content if item.get("text")), None)
    if not output_text:
        raise RuntimeError("No text in model response")

    trace_id = ((response or {}).get("ResponseMetadata") or {}).get("RequestId", "")
    return output_text, trace_id


def build_demo_allow_response(prompt: str, trace_id: str) -> dict[str, Any]:
    return {
        "status": "success",
        "decision": "ALLOW",
        "cmis_governance": "PASS",
        "trace_id": f"demo-{trace_id}",
        "score": round(len(prompt) / 500, 3),
        "model_response": (
            "[DEMO MODE] Nova Pro: This prompt passed the CMIS Sentry. "
            "Under normal quota, the model would now generate your response."
        ),
        "demo_mode": True,
    }


def classify_bedrock_failure(exc: Exception) -> tuple[int, str, str, str]:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "ClientError")
        message = error.get("Message", str(exc))

        if code in {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}:
            return (
                503,
                "BedrockThrottled",
                "Bedrock is temporarily throttled or out of quota. Please retry later.",
                f"Upstream Bedrock throttle: {message}",
            )

        if code in {"ValidationException", "ResourceNotFoundException"}:
            return (
                502,
                "InvalidModelConfiguration",
                "The configured Bedrock model identifier is invalid or unavailable.",
                f"Upstream Bedrock configuration error: {message}",
            )

        if code == "AccessDeniedException":
            return (
                403,
                "BedrockAccessDenied",
                "AWS denied access to the configured Bedrock model.",
                f"Upstream Bedrock access denied: {message}",
            )

        return (
            502,
            code,
            "Bedrock returned an upstream error.",
            f"Upstream Bedrock error {code}: {message}",
        )

    message = str(exc)
    lowered = message.lower()

    if "throttl" in lowered or "quota" in lowered:
        return (
            503,
            "BedrockThrottled",
            "Bedrock is temporarily throttled or out of quota. Please retry later.",
            message,
        )

    if "invalid" in lowered and "model" in lowered:
        return (
            502,
            "InvalidModelConfiguration",
            "The configured Bedrock model identifier is invalid or unavailable.",
            message,
        )

    if "access denied" in lowered:
        return (
            403,
            "BedrockAccessDenied",
            "AWS denied access to the configured Bedrock model.",
            message,
        )

    return 502, "BedrockInvocationFailure", "Bedrock returned an upstream error.", message


@app.get("/")
def index() -> FileResponse:
    return FileResponse("index.html")


@app.get("/styles.css")
def stylesheet() -> FileResponse:
    return FileResponse("styles.css")


@app.get("/favicon.svg")
def favicon() -> FileResponse:
    return FileResponse("favicon.svg")


@app.get("/benchmarks.html")
def benchmarks_page() -> FileResponse:
    return FileResponse("benchmarks.html")


@app.get("/contact.html")
def contact_page() -> FileResponse:
    return FileResponse("contact.html")


@app.get("/demo.html")
def demo_page() -> FileResponse:
    return FileResponse("demo.html")


@app.get("/demo-live.html")
def demo_live_page() -> FileResponse:
    return FileResponse("demo-live.html")


@app.post("/generate")
async def governed_generation(request: PromptRequest):
    prompt = validate_prompt(request.prompt)
    state = CanonicalGovernanceGate(prompt)
    if state["decision"] == "REFUSE":
        save_cmis_trace(
            prompt=prompt,
            response="",
            logic_path="GovernanceGate",
            trace_id=state["trace_id"],
            status="Blocked",
            decision="REFUSE",
            score=state.get("score"),
            reason=state.get("reason", "GovernanceBlocked"),
        )
        return {
            "status": "blocked",
            "decision": "REFUSE",
            "trace_id": state["trace_id"],
            "score": state["score"],
            "reason": state.get("reason", "GovernanceBlocked"),
            "message": "Request denied under constitutional constraints.",
        }

    if DEMO_MODE and not STRICT_MODE:
        demo_response = build_demo_allow_response(prompt, state["trace_id"])
        save_cmis_trace(
            prompt=prompt,
            response=demo_response["model_response"],
            logic_path="GovernanceGate->DemoBypass",
            trace_id=demo_response["trace_id"],
            status="Governed",
            decision="ALLOW",
            score=state.get("score"),
            reason="DemoMode",
        )
        return demo_response

    try:
        output_text, req_id = InvokeModel(prompt)
        final_trace_id = req_id or state["trace_id"]
        save_cmis_trace(
            prompt=prompt,
            response=output_text,
            logic_path="GovernanceGate->ModelInvoke",
            trace_id=final_trace_id,
            status="Governed",
            decision="ALLOW",
            score=state.get("score"),
        )
        return {
            "status": "success",
            "decision": "ALLOW",
            "cmis_governance": "PASS",
            "trace_id": final_trace_id,
            "score": state["score"],
            "model_response": output_text,
        }
    except ClientError as exc:
        status_code, reason, user_message, log_message = classify_bedrock_failure(exc)
        save_cmis_trace(
            prompt=prompt,
            response=log_message,
            logic_path="GovernanceGate->ModelInvoke",
            trace_id=state["trace_id"],
            status="Error",
            decision="REFUSE",
            score=state.get("score"),
            reason=reason,
        )
        return JSONResponse(
            status_code=status_code,
            content={
            "status": "degraded" if status_code == 503 else "blocked",
            "decision": "REFUSE",
            "cmis_governance": "PASS",
            "trace_id": state["trace_id"],
            "score": state.get("score"),
            "reason": reason,
            "message": user_message,
            "upstream_status": status_code,
            },
        )
    except Exception as exc:
        save_cmis_trace(
            prompt=prompt,
            response=str(exc),
            logic_path="GovernanceGate->ModelInvoke",
            trace_id=state["trace_id"],
            status="Error",
            decision="REFUSE",
            score=state.get("score"),
            reason="ModelInvocationFailure",
        )
        return JSONResponse(
            status_code=502,
            content={
            "status": "blocked",
            "decision": "REFUSE",
            "cmis_governance": "PASS",
            "trace_id": state["trace_id"],
            "score": state.get("score"),
            "reason": "ModelInvocationFailure",
            "message": "Model invocation failed before completion.",
            },
        )


@app.post("/govern")
def govern(request: PromptRequest):
    prompt = validate_prompt(request.prompt)
    state = CanonicalGovernanceGate(prompt)

    if state["decision"] == "REFUSE":
        save_cmis_trace(
            prompt=prompt,
            response="",
            logic_path="GovernanceGate",
            trace_id=state["trace_id"],
            status="Blocked",
            decision="REFUSE",
            score=state.get("score"),
            reason=state.get("reason", "GovernanceBlocked"),
        )
        return {
            "decision": "REFUSE",
            "trace_id": state["trace_id"],
            "score": state["score"],
            "message": "Request denied under constitutional constraints.",
        }

    if DEMO_MODE and not STRICT_MODE:
        demo_response = build_demo_allow_response(prompt, state["trace_id"])
        save_cmis_trace(
            prompt=prompt,
            response=demo_response["model_response"],
            logic_path="GovernanceGate->DemoBypass",
            trace_id=demo_response["trace_id"],
            status="Governed",
            decision="ALLOW",
            score=state.get("score"),
            reason="DemoMode",
        )
        return {
            "decision": "ALLOW",
            "trace_id": demo_response["trace_id"],
            "score": demo_response["score"],
            "response": demo_response["model_response"],
            "demo_mode": True,
        }

    # STRICT FAIL-CLOSED INVOCATION
    try:
        response, req_id = InvokeModel(prompt)
    except ClientError as exc:
        status_code, reason, user_message, log_message = classify_bedrock_failure(exc)
        save_cmis_trace(
            prompt=prompt,
            response=log_message,
            logic_path="GovernanceGate->ModelInvoke",
            trace_id=state["trace_id"],
            status="Error",
            decision="REFUSE",
            score=state.get("score"),
            reason=reason,
        )
        return JSONResponse(
            status_code=status_code,
            content={
            "decision": "REFUSE",
            "trace_id": state["trace_id"],
            "score": state["score"],
            "reason": reason,
            "message": user_message,
            "upstream_status": status_code,
            },
        )
    except Exception as exc:
        save_cmis_trace(
            prompt=prompt,
            response=str(exc),
            logic_path="GovernanceGate->ModelInvoke",
            trace_id=state["trace_id"],
            status="Error",
            decision="REFUSE",
            score=state.get("score"),
            reason="ModelInvocationFailure",
        )
        return JSONResponse(
            status_code=502,
            content={
            "decision": "REFUSE",
            "trace_id": state["trace_id"],
            "score": state["score"],
            "reason": "ModelInvocationFailure",
            "message": "Model invocation failure. Request denied.",
            },
        )

    final_trace_id = req_id or state["trace_id"]
    save_cmis_trace(
        prompt=prompt,
        response=response,
        logic_path="GovernanceGate->ModelInvoke",
        trace_id=final_trace_id,
        status="Governed",
        decision="ALLOW",
        score=state.get("score"),
    )

    return {
        "decision": "ALLOW",
        "trace_id": final_trace_id,
        "score": state["score"],
        "response": response,
    }


@app.post("/contact")
def submit_contact(request: ContactRequest):
    errors: dict[str, str] = {}

    name = request.name.strip()
    email = request.email.strip()
    company = request.company.strip()
    topic = request.topic.strip()
    message = request.message.strip()

    if len(name) < 2:
        errors["name"] = "Name must be at least 2 characters."
    if len(company) < 2:
        errors["company"] = "Company must be at least 2 characters."
    if len(topic) < 3:
        errors["topic"] = "Topic must be at least 3 characters."
    if len(message) < 20:
        errors["message"] = "Message must be at least 20 characters."
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        errors["email"] = "Enter a valid email address."

    if errors:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "ValidationError",
                "message": "One or more contact fields are invalid.",
                "friendly_sentry_message": "Sentry check: we found a few fields to fix before sending your request.",
                "fields": errors,
            },
        )

    print(f"[CONTACT] {name} <{email}> | {company} | {topic}")
    return {
        "status": "received",
        "message": "Contact request accepted.",
    }


@app.post("/debug/governance")
def debug_governance_decision(request: DebugGovernanceRequest):
    prompt = validate_prompt(request.prompt)
    mode = (request.mode or DEFAULT_DEBUG_MODE).strip().upper()
    if mode not in ALLOWED_DEBUG_MODES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "InvalidDebugMode",
                "message": f"mode must be one of {sorted(ALLOWED_DEBUG_MODES)}",
            },
        )

    strict_mode = mode == "STRICT"
    requested_profile = (request.policy_profile or DEFAULT_POLICY_PROFILE).strip().upper()
    if requested_profile not in ALLOWED_POLICY_PROFILES:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "InvalidPolicyProfile",
                "message": f"policy_profile must be one of {sorted(ALLOWED_POLICY_PROFILES)}",
            },
        )

    envelope = CMIS_ANALYZE(
        prompt,
        deterministic=strict_mode,
        policy_profile=requested_profile,
    )
    active_policy = load_active_policy_profile(requested_profile)

    debug_messages: list[str] = []

    if "hack" in prompt.lower() and "hack" not in envelope["signals"]:
        debug_messages.append("ISSUE: Keyword 'hack' not detected in signals.")

    if not unsafe_signals_detected(envelope["signals"]):
        debug_messages.append("ISSUE: unsafe_signals_detected() returned FALSE.")

    if envelope["classification"] is None:
        debug_messages.append("ISSUE: classification returned NULL.")

    if envelope["classification"] not in ["conflict", "ethics"]:
        debug_messages.append("ISSUE: Prompt not classified as safety-sensitive.")

    if envelope["scope"] != "ethics_review":
        debug_messages.append("ISSUE: Scope not escalated to ethics_review.")

    if len(envelope["violations"]) == 0:
        debug_messages.append("ISSUE: No violations triggered during enforce_constraints.")

    if active_policy["name"] == "DEMO":
        debug_messages.append("ISSUE: Demo mode may bypass refusal rules.")

    if active_policy["domain"] == "school" and "network_intrusion_block" not in active_policy["rules"]:
        debug_messages.append("ISSUE: School policy missing network intrusion rule.")

    if len(envelope["violations"]) == 0:
        debug_messages.append("ALLOW default path: violations list is empty.")

    return {
        "decision": envelope["decision"],
        "classification": envelope["classification"],
        "scope": envelope["scope"],
        "confidence": envelope["confidence"],
        "signals": envelope["signals"],
        "violations": envelope["violations"],
        "trace": envelope["trace_id"],
        "mode": mode,
        "strict_enforced": strict_mode,
        "policy_profile": requested_profile,
        "active_policy": active_policy,
        "debug_messages": debug_messages,
    }
