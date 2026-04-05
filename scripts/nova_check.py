import boto3

region = "us-east-1"
bedrock = boto3.client("bedrock", region_name=region)
runtime = boto3.client("bedrock-runtime", region_name=region)

print("=== Amazon Nova model IDs in us-east-1 ===")
models = bedrock.list_foundation_models(byProvider="Amazon").get("modelSummaries", [])
for model_id in sorted(m.get("modelId") for m in models if m.get("modelId")):
    if "nova" in model_id:
        print(model_id)

print("=== Converse probes ===")
messages = [{"role": "user", "content": [{"text": "Health check"}]}]
inference_config = {"maxTokens": 30, "temperature": 0.2}

for model_id in ["amazon.nova-pro", "amazon.nova-pro-v1:0", "amazon.nova-2-pro-v1:0"]:
    print(f"--- {model_id} ---")
    try:
        result = runtime.converse(
            modelId=model_id,
            messages=messages,
            inferenceConfig=inference_config,
        )
        content = ((result.get("output") or {}).get("message") or {}).get("content") or []
        text = next((item.get("text") for item in content if isinstance(item, dict) and item.get("text")), "")
        print("OK", text[:120])
    except Exception as exc:
        print(type(exc).__name__, str(exc))
