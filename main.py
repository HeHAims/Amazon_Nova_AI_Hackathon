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


def CanonicalGovernanceGate(prompt: str) -> dict[str, Any]:
    score = len(prompt) / 500
    trace_seed = f"{CONSTITUTION_HASH}:{prompt}:{time.time()}"
    trace_id = hashlib.sha256(trace_seed.encode()).hexdigest()[:12]

    if score > THRESHOLD:
        return {
            "decision": "REFUSE",
            "trace_id": trace_id,
            "score": round(score, 3),
            "reason": "ThresholdExceeded",
        }

    return {
        "decision": "ALLOW",
        "trace_id": trace_id,
        "score": round(score, 3),
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

    if DEMO_MODE:
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

    if DEMO_MODE:
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
