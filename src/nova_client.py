import boto3
from typing import Any, Tuple

DEFAULT_REGION = "us-east-1"
DEFAULT_MODULE_MODEL = "amazon.nova-lite-v1:0"
DEFAULT_INTEGRATOR_MODEL = "amazon.nova-pro-v1:0"


def converse_with_meta(
    model_id: str,
    user_text: str,
    system_text: str | None = None,
    temperature: float = 0.3,
    region: str = DEFAULT_REGION,
) -> Tuple[str, str, dict[str, Any]]:
    client = boto3.client("bedrock-runtime", region_name=region)

    messages = [{"role": "user", "content": [{"text": user_text}]}]
    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {"temperature": temperature, "maxTokens": 400},
    }
    if system_text:
        kwargs["system"] = [{"text": system_text}]

    response = client.converse(**kwargs)
    content = response.get("output", {}).get("message", {}).get("content", [])
    request_id = response.get("ResponseMetadata", {}).get("RequestId", "")

    for part in content:
        text = part.get("text")
        if text:
            return text, request_id, response
    raise RuntimeError("No text returned from Nova.")


def converse_text(model_id: str, user_text: str, system_text: str | None = None, temperature: float = 0.3, region: str = DEFAULT_REGION) -> str:
    text, _request_id, _response = converse_with_meta(
        model_id=model_id,
        user_text=user_text,
        system_text=system_text,
        temperature=temperature,
        region=region,
    )
    return text
