# Production Deployment Guide — AgentCore Optimization Loop (API-driven, no UI)

This guide stands up the **entire optimization loop in a fresh AWS account using only AWS
APIs** — no Streamlit UI, no `demo` CLI. It's written for an operator or an
infrastructure-as-code pipeline that wants to run the loop (deploy → evaluate → recommend →
bundle → A/B test → promote) as production automation.

The demo's Python code (`src/agentcore_demo/`) automates exactly these calls; this document
is the vendor-neutral, API-level contract, with request shapes verified against a live
account. Every command uses the AWS CLI or a small `boto3` snippet.

> All steps create **real, billable** Amazon Bedrock AgentCore resources. See
> [§13 Cleanup](#13-cleanup). Replace `ACCOUNT_ID`, ARNs, and IDs with your own.

**Planes:** data-plane API = `bedrock-agentcore`; control-plane API =
`bedrock-agentcore-control`.

**Conventions**
```bash
export AWS_REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export APP=PatientSupport                    # your app/runtime name prefix
export MODEL_ID=us.anthropic.claude-sonnet-5 # Claude Sonnet 5 (US cross-region profile)
```

---

## 0. Fresh-account bootstrap & enablement

Before any resource creation, confirm the account is ready:

1. **Credentials & identity**
   ```bash
   aws sts get-caller-identity          # must succeed; note the Account
   ```
   Use a role/user with permission to create IAM roles, S3 buckets, Lambda functions, and
   Bedrock AgentCore resources. For a locked-down pipeline, see [§12](#12-production-hardening)
   for least-privilege guidance.

2. **Amazon Bedrock AgentCore availability** — confirm the service is available/enabled in
   your target region (it is a newer service; not all regions have it). A quick probe:
   ```bash
   aws bedrock-agentcore-control list-agent-runtimes --region "$AWS_REGION" >/dev/null && echo "AgentCore reachable"
   ```

3. **Model access** — enable **Claude Sonnet 5** in Bedrock for the region (Console →
   Bedrock → Model access), then verify:
   ```bash
   aws bedrock-runtime converse --region "$AWS_REGION" \
     --model-id "$MODEL_ID" \
     --messages '[{"role":"user","content":[{"text":"say ok"}]}]' \
     --inference-config '{"maxTokens":10}' \
     --query 'output.message.content[0].text' --output text          # → ok
   ```

4. **Build tooling** — a machine with Python 3.13 and `pip` able to build
   `manylinux2014_aarch64` (runtime package) and `manylinux2014_x86_64` (Lambda) wheels,
   plus `zip`.

5. **Your agent source** — the runtime entry point is a single `main.py`. This repo ships
   one at `src/agentcore_demo/agent/patient_support_agent.py`; substitute your own agent as
   needed (it must be a `BedrockAgentCoreApp` that reads the config-bundle hook — see §6).

---

## 1. IAM: runtime execution role

The AgentCore Runtime and the online-evaluation service assume this role. It needs
AgentCore, Bedrock model invocation, CloudWatch Logs/X-Ray (OTel traces), S3 read (the
deployment package), and — because online evaluation invokes a custom code-based evaluator
Lambda (§10) — **`lambda:GetFunction` + `lambda:InvokeFunction`**.

> The broad `Resource:"*"` policy below is for a quick stand-up. For production, scope it —
> see [§12](#12-production-hardening).

```bash
# Trust policy: only AgentCore in this account/partition can assume the role.
cat > /tmp/trust.json <<JSON
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow","Principal":{"Service":"bedrock-agentcore.amazonaws.com"},
  "Action":"sts:AssumeRole",
  "Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT_ID"},
               "ArnLike":{"aws:SourceArn":"arn:aws:bedrock-agentcore:*:$ACCOUNT_ID:*"}}}]}
JSON
aws iam create-role --role-name ${APP}Role \
  --assume-role-policy-document file:///tmp/trust.json

cat > /tmp/perms.json <<'JSON'
{"Version":"2012-10-17","Statement":[{
  "Effect":"Allow",
  "Action":["bedrock-agentcore:*","bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream",
            "logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogGroups",
            "logs:DescribeIndexPolicies","logs:PutIndexPolicy","logs:FilterLogEvents","logs:GetLogEvents",
            "logs:StartQuery","logs:GetQueryResults","logs:StopQuery","cloudwatch:*",
            "xray:PutTraceSegments","xray:PutTelemetryRecords","sts:AssumeRole",
            "s3:GetObject","s3:ListBucket","lambda:GetFunction","lambda:InvokeFunction"],
  "Resource":"*"}]}
JSON
aws iam put-role-policy --role-name ${APP}Role \
  --policy-name ${APP}Policy --policy-document file:///tmp/perms.json

export ROLE_ARN=$(aws iam get-role --role-name ${APP}Role --query 'Role.Arn' --output text)
sleep 10   # allow IAM propagation
```

---

## 2. Deploy the agent to AgentCore Runtime

### 2a. Build the ARM64 deployment package
The runtime runs `main.py` under `opentelemetry-instrument`. Bundle the agent + deps for
`manylinux2014_aarch64`, Python 3.13.
```bash
BUILD=/tmp/${APP}_build && rm -rf "$BUILD" && mkdir -p "$BUILD/pkg"
python -m pip install strands-agents[otel] bedrock-agentcore aws-opentelemetry-distro \
  -t "$BUILD/pkg" --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.13 --quiet
cp src/agentcore_demo/agent/patient_support_agent.py "$BUILD/pkg/main.py"   # your agent → main.py
( cd "$BUILD/pkg" && zip -qr ../deployment_package.zip . -x '*.pyc' '*__pycache__*' )
```

### 2b. Upload to S3
```bash
export S3_BUCKET=bedrock-agentcore-code-${ACCOUNT_ID}-${AWS_REGION}
export S3_KEY=${APP}/deployment_package.zip
aws s3api create-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" \
  $( [ "$AWS_REGION" != us-east-1 ] && echo --create-bucket-configuration LocationConstraint=$AWS_REGION ) 2>/dev/null || true
aws s3 cp "$BUILD/deployment_package.zip" "s3://$S3_BUCKET/$S3_KEY"
```

### 2c. Create the runtime and poll to READY
```bash
aws bedrock-agentcore-control create-agent-runtime --region "$AWS_REGION" \
  --agent-runtime-name "$APP" \
  --agent-runtime-artifact "{\"codeConfiguration\":{\"code\":{\"s3\":{\"bucket\":\"$S3_BUCKET\",\"prefix\":\"$S3_KEY\"}},\"runtime\":\"PYTHON_3_13\",\"entryPoint\":[\"opentelemetry-instrument\",\"main.py\"]}}" \
  --network-configuration '{"networkMode":"PUBLIC"}' \
  --role-arn "$ROLE_ARN"

export RUNTIME_ID=<agentRuntimeId>          # from the response
aws bedrock-agentcore-control get-agent-runtime --region "$AWS_REGION" \
  --agent-runtime-id "$RUNTIME_ID" --query '{status:status,arn:agentRuntimeArn}'   # wait ACTIVE/READY

export AGENT_ARN=arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:runtime/${RUNTIME_ID}
export LOG_GROUP=/aws/bedrock-agentcore/runtimes/${RUNTIME_ID}-DEFAULT
export SERVICE_NAME=${APP}.DEFAULT
```

### 2d. Smoke-test (multi-turn)
`runtimeSessionId` **must be ≥ 33 characters** (a UUID works).
```bash
SID=$(python -c "import uuid;print(uuid.uuid4())")
aws bedrock-agentcore invoke-agent-runtime --region "$AWS_REGION" \
  --agent-runtime-arn "$AGENT_ARN" --runtime-session-id "$SID" \
  --payload '{"prompt":"I'\''m patient PT-1001. What medications am I on?"}' /tmp/resp.txt
cat /tmp/resp.txt
```

---

## 3. Offline (batch) evaluation

### 3a. Generate sessions to score
Send your curated cases (see `datasets/offline_multiturn.jsonl` for the format) as sessions
and keep the session IDs. Wait 2–3 min for CloudWatch ingestion.

### 3b. Start a batch evaluation and poll
```bash
aws bedrock-agentcore start-batch-evaluation --region "$AWS_REGION" \
  --batch-evaluation-name ${APP}Baseline \
  --evaluators '[{"evaluatorId":"Builtin.GoalSuccessRate"},{"evaluatorId":"Builtin.Helpfulness"},{"evaluatorId":"Builtin.Correctness"}]' \
  --data-source-config "{\"cloudWatchLogs\":{\"serviceNames\":[\"$SERVICE_NAME\"],\"logGroupNames\":[\"aws/spans\",\"$LOG_GROUP\"],\"filterConfig\":{\"sessionIds\":[\"$SID\"]}}}" \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
# poll until COMPLETED, then read evaluationResults.evaluatorSummaries[].statistics.averageScore
aws bedrock-agentcore get-batch-evaluation --region "$AWS_REGION" --batch-evaluation-id <id>
```

> **Tool-trajectory reconstruction (optional, for custom metrics offline):** filter
> `aws/spans` by `attributes.session.id = '<SID>'` → collect `traceId`s → order
> `execute_tool` spans (`attributes.gen_ai.tool.name`) by start time. This is how you score
> tool-call / trajectory quality outside the built-in LLM judges.

---

## 4. Online evaluation

```bash
aws bedrock-agentcore-control create-online-evaluation-config --region "$AWS_REGION" \
  --online-evaluation-config-name ${APP}OnlineEval \
  --rule '{"samplingConfig":{"samplingPercentage":100.0},"sessionConfig":{"sessionTimeoutMinutes":2}}' \
  --data-source-config "{\"cloudWatchLogs\":{\"logGroupNames\":[\"$LOG_GROUP\"],\"serviceNames\":[\"$SERVICE_NAME\"]}}" \
  --evaluators '[{"evaluatorId":"Builtin.GoalSuccessRate"},{"evaluatorId":"Builtin.Helpfulness"}]' \
  --evaluation-execution-role-arn "$ROLE_ARN" --enable-on-create \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
```
Then drive real traffic via `invoke-agent-runtime` (fresh UUID session id each). Scoring is
**asynchronous** (~10–15 min to aggregate). In production, set `samplingPercentage` below 100
to control cost. Disable later:
```bash
aws bedrock-agentcore-control update-online-evaluation-config --region "$AWS_REGION" \
  --online-evaluation-config-id <id> --execution-status DISABLED
```

---

## 5. Recommendation

Ask AgentCore to improve the system prompt for a target evaluator, from production traces.
(Use the prompt currently in production as `CURRENT_PROMPT`.)
```bash
python - <<PY
import boto3, uuid, datetime as dt, os
dp = boto3.client("bedrock-agentcore", region_name=os.environ["AWS_REGION"])
now = dt.datetime.now(dt.timezone.utc); start = now - dt.timedelta(days=7)
CURRENT_PROMPT = open("current_prompt.txt").read()
r = dp.start_recommendation(
  name="RecSp", type="SYSTEM_PROMPT_RECOMMENDATION",
  recommendationConfig={"systemPromptRecommendationConfig":{
    "systemPrompt":{"text":CURRENT_PROMPT},
    "agentTraces":{"cloudwatchLogs":{
      "logGroupArns":[f"arn:aws:logs:{os.environ['AWS_REGION']}:{os.environ['ACCOUNT_ID']}:log-group:{os.environ['LOG_GROUP']}"],
      "serviceNames":[os.environ["SERVICE_NAME"]], "startTime":start, "endTime":now}},
    "evaluationConfig":{"evaluators":[{"evaluatorArn":"arn:aws:bedrock-agentcore:::evaluator/Builtin.GoalSuccessRate"}]}}},
  clientToken=str(uuid.uuid4()))
print("recommendationId:", r["recommendationId"])
# poll get_recommendation until COMPLETED; read
# result["recommendationResult"]["systemPromptRecommendationResult"]["recommendedSystemPrompt"]
PY
```
> The recommendation job is a server-side LLM analysis over the trace window; it can take
> several minutes. A shorter `startTime` window (e.g. 1 day) narrows what it analyzes.

---

## 6. Configuration bundles: control vs treatment

A **configuration bundle** is a versioned, immutable `{system_prompt, model_id,
tool_descriptions}` snapshot injected into the runtime at request time — no code redeploy.
Create two: the **control** (current/baseline config) and the **treatment** (the change you
want to test, e.g. the recommended prompt or a different `model_id`).

```bash
# Control (current baseline)
aws bedrock-agentcore-control create-configuration-bundle --region "$AWS_REGION" \
  --bundle-name ${APP}Control \
  --components "{\"$AGENT_ARN\":{\"configuration\":{\"system_prompt\":\"<CONTROL PROMPT>\",\"model_id\":\"$MODEL_ID\",\"tool_descriptions\":{}}}}" \
  --commit-message "control: current baseline" \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
# → CONTROL_BUNDLE_ARN, CONTROL_BUNDLE_ID, CONTROL_VER

# Treatment (the candidate change)
aws bedrock-agentcore-control create-configuration-bundle --region "$AWS_REGION" \
  --bundle-name ${APP}Treatment \
  --components "{\"$AGENT_ARN\":{\"configuration\":{\"system_prompt\":\"<TREATMENT PROMPT>\",\"model_id\":\"$MODEL_ID\",\"tool_descriptions\":{}}}}" \
  --commit-message "treatment: candidate change" \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
# → TREATMENT_BUNDLE_ARN, TREATMENT_VER
```

Your agent reads the injected bundle via `BedrockAgentCoreContext.get_config_bundle()` and
overrides its prompt/tool descriptions accordingly. To invoke a specific bundle directly
(outside the gateway), pass a **baggage** header on `invoke_agent_runtime`:
```
baggage = aws.agentcore.configbundle_arn=<BUNDLE_ARN>,aws.agentcore.configbundle_version=<VERSION>
```

> **Editable configs in this repo:** the loop splits this into "draft config → build bundle"
> so the two configs can be edited (model id + system prompt) before the bundle is created.
> At the API level that's just: write your config values, then call
> `create-configuration-bundle` with them.

---

## 7. Offline check of the treatment (regression guard)

Repeat §3 twice — once with the **control** bundle injected (baggage header) and once with
the **treatment** bundle — batch-evaluating each set of sessions. Compare the aggregate
scores; confirm the treatment holds or improves before spending live A/B traffic. This is
the apples-to-apples "control vs candidate" check.

---

## 8. Gateway + target (traffic routing for A/B)

```bash
# Gateway (IAM-authorized), poll to READY
aws bedrock-agentcore-control create-gateway --region "$AWS_REGION" \
  --name ${APP}Gateway --description "A/B gateway" \
  --authorizer-type AWS_IAM --role-arn "$ROLE_ARN" \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
export GATEWAY_ID=<gatewayId>
aws bedrock-agentcore-control get-gateway --region "$AWS_REGION" --gateway-identifier "$GATEWAY_ID" \
  --query '{status:status,arn:gatewayArn,url:gatewayUrl}'
export GATEWAY_ARN=<gatewayArn>; export GATEWAY_URL=<gatewayUrl>

# Target → the runtime, poll to READY
aws bedrock-agentcore-control create-gateway-target --region "$AWS_REGION" \
  --gateway-identifier "$GATEWAY_ID" --name ${APP}Target \
  --target-configuration "{\"http\":{\"agentcoreRuntime\":{\"arn\":\"$AGENT_ARN\",\"qualifier\":\"DEFAULT\"}}}" \
  --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
```

---

## 9. A/B test

Create an online-eval config for the A/B (built-ins + the optional custom evaluator from
§10), then the A/B test. **Variant names must be exactly `C` and `T1`.**

```bash
aws bedrock-agentcore-control create-online-evaluation-config --region "$AWS_REGION" \
  --online-evaluation-config-name ${APP}ABEval \
  --rule '{"samplingConfig":{"samplingPercentage":100.0},"sessionConfig":{"sessionTimeoutMinutes":2}}' \
  --data-source-config "{\"cloudWatchLogs\":{\"logGroupNames\":[\"$LOG_GROUP\"],\"serviceNames\":[\"$SERVICE_NAME\"]}}" \
  --evaluators '[{"evaluatorId":"Builtin.GoalSuccessRate"},{"evaluatorId":"Builtin.Helpfulness"}]' \
  --evaluation-execution-role-arn "$ROLE_ARN" --enable-on-create \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
export ONLINE_EVAL_ARN=<onlineEvaluationConfigArn>

aws bedrock-agentcore create-ab-test --region "$AWS_REGION" \
  --name ${APP}AB --description "control vs treatment" \
  --gateway-arn "$GATEWAY_ARN" --role-arn "$ROLE_ARN" --enable-on-create \
  --evaluation-config "{\"onlineEvaluationConfigArn\":\"$ONLINE_EVAL_ARN\"}" \
  --variants "[{\"name\":\"C\",\"weight\":50,\"variantConfiguration\":{\"configurationBundle\":{\"bundleArn\":\"$CONTROL_BUNDLE_ARN\",\"bundleVersion\":\"$CONTROL_VER\"}}},{\"name\":\"T1\",\"weight\":50,\"variantConfiguration\":{\"configurationBundle\":{\"bundleArn\":\"$TREATMENT_BUNDLE_ARN\",\"bundleVersion\":\"$TREATMENT_VER\"}}}]" \
  --client-token $(python -c "import uuid;print(uuid.uuid4())")
export AB_TEST_ID=<abTestId>
```

### 9a. Drive gateway traffic (SigV4-signed)
```bash
python - <<PY
import boto3, json, uuid, os, requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
sess = boto3.Session(); creds = sess.get_credentials().get_frozen_credentials()
region = os.environ["AWS_REGION"]; url = f"{os.environ['GATEWAY_URL']}/{os.environ['APP']}Target/invocations"
for prompt in ["I'm PT-1001. What meds am I on?", "I'm PT-1002. Is an MRI covered?"]:
    sid = str(uuid.uuid4()); body = json.dumps({"prompt":prompt,"sessionId":sid})
    req = AWSRequest(method="POST", url=url, data=body,
        headers={"Content-Type":"application/json","X-Amzn-Bedrock-AgentCore-Runtime-Session-Id":sid})
    SigV4Auth(creds,"bedrock-agentcore",region).add_auth(req)
    print(requests.post(url, data=body, headers=dict(req.headers), timeout=120).status_code)
PY
```

### 9b. Poll results (allow ~10–15 min to aggregate)
```bash
aws bedrock-agentcore get-ab-test --region "$AWS_REGION" --ab-test-id "$AB_TEST_ID" \
  --query 'results.{ts:analysisTimestamp,metrics:evaluatorMetrics}'
```
Each `evaluatorMetrics[]` has `controlStats.mean` and `variantResults[].{mean,pValue,isSignificant}`.
A variant with `pValue < 0.05` and a higher mean is a significant winner.

> **Timing note:** significance appears only once enough sessions are scored — typically
> ~10-15 min after the traffic is driven. Automate a poll loop rather than a single check.

---

## 10. Custom code-based trajectory evaluator (optional)

Lets **online evaluation** score a dimension the built-in LLM judges don't — here, tool
trajectory — via your own Lambda.

### 10a. Lambda execution role
```bash
aws iam create-role --role-name AgentCoreLambdaEvaluatorRole \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name AgentCoreLambdaEvaluatorRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
export LAMBDA_ROLE_ARN=$(aws iam get-role --role-name AgentCoreLambdaEvaluatorRole --query 'Role.Arn' --output text)
sleep 15
```

### 10b. Package + deploy the Lambda
Source: `lambdas/trajectory_evaluator/lambda_function.py`. Bundle with the
`bedrock-agentcore` SDK + deps for the Lambda platform (x86_64, py3.12):
```bash
LB=/tmp/traj && rm -rf "$LB" && mkdir -p "$LB/pkg"
python -m pip install "bedrock-agentcore>=1.6.0" --no-deps -t "$LB/pkg" --quiet
python -m pip install "pydantic>=2.0.0" -t "$LB/pkg" --platform manylinux2014_x86_64 \
  --implementation cp --python-version 312 --only-binary=:all: --quiet
python -m pip install starlette uvicorn websockets typing-extensions requests -t "$LB/pkg" --quiet
cp lambdas/trajectory_evaluator/lambda_function.py "$LB/pkg/"
( cd "$LB/pkg" && zip -qr ../traj.zip . )

aws lambda create-function --region "$AWS_REGION" \
  --function-name ${APP,,}-trajectory-eval --runtime python3.12 \
  --role "$LAMBDA_ROLE_ARN" --handler lambda_function.lambda_handler \
  --zip-file fileb://"$LB/traj.zip" --timeout 70 --memory-size 256
export LAMBDA_ARN=$(aws lambda get-function --region "$AWS_REGION" \
  --function-name ${APP,,}-trajectory-eval --query 'Configuration.FunctionArn' --output text)

aws lambda add-permission --region "$AWS_REGION" \
  --function-name ${APP,,}-trajectory-eval --statement-id AllowAgentCoreEvaluateInvoke \
  --action lambda:InvokeFunction --principal bedrock-agentcore.amazonaws.com --source-account "$ACCOUNT_ID"
```

### 10c. Register the evaluator + include it in the A/B online eval
```bash
aws bedrock-agentcore-control create-evaluator --region "$AWS_REGION" \
  --evaluator-name TrajectoryQuality --level SESSION \
  --evaluator-config "{\"codeBased\":{\"lambdaConfig\":{\"lambdaArn\":\"$LAMBDA_ARN\",\"lambdaTimeoutInSeconds\":60}}}"
# → evaluatorId ; add {"evaluatorId":"<evaluatorId>"} to §9's create-online-evaluation-config
```
> **Requirement:** the online-eval execution role (`${APP}Role`) needs `lambda:GetFunction`
> + `lambda:InvokeFunction` (granted in §1). Without it, `create-online-evaluation-config`
> fails with a permission error.

---

## 11. Promotion (roll out the winner)

Promotion is an **operational decision**, not a single AgentCore API. In production, gate it
behind whatever approval/change-management your org requires. The mechanical steps:

1. **Decide** from §9b: a winner exists if the treatment is significantly better (p < 0.05)
   on ≥1 metric with no significant regression.
2. **Approve** — your out-of-band sign-off / change ticket. (This repo implements an explicit
   approval record + append-only audit log so promotions are attributable — replicate that in
   your automation.)
3. **Promote** the winning config into the baseline (control) bundle as a new version,
   preserving lineage:
   ```bash
   CUR=$(aws bedrock-agentcore-control get-configuration-bundle --region "$AWS_REGION" \
     --bundle-id "$CONTROL_BUNDLE_ID" --query versionId --output text)
   aws bedrock-agentcore-control update-configuration-bundle --region "$AWS_REGION" \
     --bundle-id "$CONTROL_BUNDLE_ID" \
     --components "{\"$AGENT_ARN\":{\"configuration\":{\"system_prompt\":\"<TREATMENT PROMPT>\",\"model_id\":\"$MODEL_ID\",\"tool_descriptions\":{}}}}" \
     --parent-version-ids "[\"$CUR\"]" \
     --commit-message "Promote treatment (A/B validated)" \
     --client-token $(python -c "import uuid;print(uuid.uuid4())")
   ```
4. **Stop the A/B test:**
   ```bash
   aws bedrock-agentcore update-ab-test --region "$AWS_REGION" --ab-test-id "$AB_TEST_ID" --execution-status STOPPED
   ```
5. **Audit** — record who approved, the metrics at decision time, and the outcome (append to
   your audit store).

The control bundle now serves the promoted config — the new baseline for the next iteration.

---

## 12. Production hardening

Recommendations for running this as real production automation, beyond the quick stand-up
above:

- **Least-privilege IAM.** Replace the broad `${APP}Policy` `Resource:"*"` with scoped ARNs:
  `bedrock:InvokeModel` limited to the specific model, `s3:GetObject` to the deployment
  bucket/prefix, `lambda:*Function` to the evaluator function ARN, `logs`/`xray` to the
  runtime's log groups, and the specific AgentCore resource ARNs your automation creates.
  Grant the *creating* principal (your pipeline role) only the AgentCore/IAM/S3/Lambda
  actions it needs.
- **Idempotency.** Every create call takes a `clientToken` — generate a stable token per
  logical operation so retries don't create duplicates. Treat resource names as unique;
  add a deterministic suffix per environment to avoid `ConflictException`.
- **Poll with timeouts.** Runtime/gateway/target reach READY quickly; batch/recommendation
  jobs and online-eval aggregation take minutes. Poll with a bounded loop + backoff, and
  surface a clear timeout rather than hanging.
- **Cost controls.** Online evaluation scores sampled sessions continuously — set
  `samplingPercentage` to what you need (not always 100). Stop/disable A/B tests and
  online-eval configs when a test concludes. Bundles, gateways, and the Lambda are cheap at
  rest but the evaluations and model invocations are the cost drivers.
- **Monitoring.** Alarm on runtime errors and evaluation job failures via CloudWatch; the
  OTel spans in `aws/spans` are your source of truth for per-session behavior and tool
  trajectories.
- **Change management.** Keep the promotion decision human-gated and audited (§11). Store the
  A/B evidence (per-variant means, p-values) alongside the approval record.
- **Region/model.** Confirm both AgentCore *and* the chosen model are available in the target
  region before promoting infra to it.

---

## 13. Cleanup

Delete in roughly reverse order to stop costs:
```bash
# A/B test
aws bedrock-agentcore update-ab-test --region "$AWS_REGION" --ab-test-id "$AB_TEST_ID" --execution-status STOPPED
aws bedrock-agentcore delete-ab-test --region "$AWS_REGION" --ab-test-id "$AB_TEST_ID"
# Online eval configs
aws bedrock-agentcore-control update-online-evaluation-config --region "$AWS_REGION" --online-evaluation-config-id <id> --execution-status DISABLED
aws bedrock-agentcore-control delete-online-evaluation-config --region "$AWS_REGION" --online-evaluation-config-id <id>
# Configuration bundles
aws bedrock-agentcore-control delete-configuration-bundle --region "$AWS_REGION" --bundle-id "$CONTROL_BUNDLE_ID"
aws bedrock-agentcore-control delete-configuration-bundle --region "$AWS_REGION" --bundle-id "$TREATMENT_BUNDLE_ID"
# Gateway target + gateway
aws bedrock-agentcore-control delete-gateway-target --region "$AWS_REGION" --gateway-identifier "$GATEWAY_ID" --target-id <targetId>
aws bedrock-agentcore-control delete-gateway --region "$AWS_REGION" --gateway-identifier "$GATEWAY_ID"
# Custom evaluator + Lambda
aws bedrock-agentcore-control delete-evaluator --region "$AWS_REGION" --evaluator-id <evaluatorId>
aws lambda delete-function --region "$AWS_REGION" --function-name ${APP,,}-trajectory-eval
# Runtime
aws bedrock-agentcore-control delete-agent-runtime --region "$AWS_REGION" --agent-runtime-id "$RUNTIME_ID"
# IAM roles + S3
aws iam delete-role-policy --role-name ${APP}Role --policy-name ${APP}Policy
aws iam delete-role --role-name ${APP}Role
aws iam detach-role-policy --role-name AgentCoreLambdaEvaluatorRole --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name AgentCoreLambdaEvaluatorRole
aws s3 rb "s3://$S3_BUCKET" --force
```

---

## Appendix — API map (loop stage ↔ AWS operation)

| Loop step | AWS operation(s) | Plane |
|---|---|---|
| Deploy runtime | `create-agent-runtime`, `get-agent-runtime` | control |
| Invoke agent | `invoke-agent-runtime` | data |
| Offline / batch eval | `start-batch-evaluation`, `get-batch-evaluation` | data |
| Online eval | `create/update-online-evaluation-config` | control |
| Recommendation | `start-recommendation`, `get-recommendation` | data |
| Configuration bundles | `create/update/get-configuration-bundle` | control |
| Gateway + target | `create-gateway`, `create-gateway-target` | control |
| A/B test | `create-ab-test`, `get-ab-test`, `update-ab-test` | data |
| Custom evaluator | `create-evaluator` (+ Lambda `create-function`, `add-permission`) | control / lambda |
| Trace queries | CloudWatch Logs `start-query` / `get-query-results` over `aws/spans` | logs |

> This repo's `src/agentcore_demo/agentcore_client.py` implements every call above with
> retries, timing waits, and response parsing, if you'd rather drive it from Python than the
> raw CLI.
