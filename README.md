# Lambda CDK Template

A template for Python Lambda functions on AWS. You run `cdk deploy` once, and that
creates a CodePipeline. After that, every push to the configured branch tests,
builds and deploys the Lambda automatically. The pipeline also updates itself when
you change the pipeline code.

```
GitHub push ──> Source ──> Build (pytest + cdk synth) ──> UpdatePipeline ──> Assets ──> Deploy Lambda + API
```

Each Lambda gets an API Gateway endpoint ending in `/invoke`. Only users signed in
to the configured Cognito user pool can call it. The request body picks a **runmode**,
which is the function inside the Lambda to run.

## Layout

| Path | What it is |
| --- | --- |
| `config.yaml` | **The file you edit:** app name, repo, branch, Lambda settings |
| `lambda/handler.py` | Your function code: add runmodes to the `RUNMODES` table |
| `lambda/requirements.txt` | The function's pip dependencies, bundled automatically |
| `lambda/db.py` | `get_connection()` for the hawkeye database (used when `database.user` is set) |
| `lambda/storage.py` | Presigned upload/view URLs for the imagery bucket (used when `storage.access` is set) |
| `app.py` | CDK entry point, plus the **API Gateway settings** (user pool, app clients, CORS origins, path) |
| `pipeline/pipeline_stack.py` | The CodePipeline definition |
| `pipeline/lambda_stage.py` | The stage the pipeline deploys (add more stacks here) |
| `pipeline/lambda_stack.py` | The Lambda function, its log group and API Gateway |
| `pipeline/config.py` | Loads and validates `config.yaml` |
| `tests/` | Tests that run locally and in the pipeline before every deploy |

## One-time setup (per AWS account)

1. **GitHub connection.** In the AWS console, go to Developer Tools → Settings →
   Connections → Create connection → GitHub. Authorize it and grant it access to your
   repos, or to all repos so new ones work automatically. Copy the connection ARN into
   `github.connection_arn` in `config.yaml`. One connection is shared by every Lambda
   repo you create from this template.
2. **CDK bootstrap**, if the account/region has never been bootstrapped:
   ```bash
   cdk bootstrap aws://ACCOUNT_ID/us-east-2
   ```

## Local tools

- Python 3.12+
- Node.js (for the CDK CLI): `npm install -g aws-cdk`
- Docker, which bundles the Lambda's dependencies during synth
- AWS CLI credentials for the target account (`aws sts get-caller-identity`)

## Creating a new Lambda from this template

1. On GitHub, click **Use this template** → create a repo. Name it the same as your
   app, e.g. `hawkeye-alerts`. Tick **Include all branches** if you deploy from a
   branch other than the default one.
2. Clone it, then set `app_name` in `config.yaml` to the repo name:
   ```yaml
   app_name: hawkeye-alerts
   ```
   If the repo name differs from `app_name`, also set `github.repo`.
3. Set up the environment and check everything synthesizes:
   ```bash
   python3 -m venv .venv          # or: uv venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt -r requirements-dev.txt
   pytest
   cdk synth
   ```
4. Commit and push to the branch in `github.branch`:
   ```bash
   git add -A && git commit -m "Configure hawkeye-alerts" && git push
   ```
5. Deploy the pipeline, once only:
   ```bash
   cdk deploy
   ```
   The pipeline starts right away and deploys the Lambda. Watch it in the
   CodePipeline console as `<app_name>-pipeline`.

From then on, **just push**. You don't need to run `cdk deploy` again, even for
pipeline changes.

> The pipeline builds from what's on GitHub, not your local copy, so push before
> step 5. Otherwise the first pipeline run deploys the template's old `app_name`.

## What gets created

| Resource | Name |
| --- | --- |
| Pipeline stack | `<app_name>-pipeline` |
| CodePipeline | `<app_name>-pipeline` |
| Lambda stack | `<app_name>` |
| Lambda function | `<app_name>` |
| Log group | `/aws/lambda/<app_name>` |
| HTTP API | `<app_name>`, with the URL shown as the `ApiUrl` stack output |

## Calling the Lambda

`POST` to the `ApiUrl` (find it in the CloudFormation console under the `<app_name>`
stack's Outputs) with the signed-in user's Cognito ID token:

```js
import { fetchAuthSession } from 'aws-amplify/auth'

const { tokens } = await fetchAuthSession()
const res = await fetch(API_URL, {
  method: 'POST',
  headers: { Authorization: `Bearer ${tokens.idToken}`, 'Content-Type': 'application/json' },
  body: JSON.stringify({ runmode: 'ping' }),
})
const data = await res.json()  // { message: 'pong from <app_name>' }
```

| Response | Meaning |
| --- | --- |
| `200` | The runmode's return value, as JSON |
| `400` | Unknown runmode or invalid JSON; the body lists the valid runmodes |
| `401` | Missing, expired or wrong-pool token |

**API settings** live at the top of `app.py`:
```python
API = ApiConfig(
    user_pool_id="us-east-2_...",            # whose users can call it
    user_pool_client_ids=["..."],            # which app clients' tokens are accepted
    cors_origins=["https://your.site", "http://localhost:5173"],
    path="/invoke",
)
```
Set `API = None` for a Lambda with no API, e.g. one triggered by S3 or a schedule.

**Adding a runmode:** write a function in `lambda/handler.py` and add it to `RUNMODES`:
```python
def write(payload, event):
    if not payload.get("content"):
        raise RunmodeError(400, "content is required")
    return {"message": "written", "user": user_id(event)}

RUNMODES = {"ping": ping, "write": write}
```
`payload` is the parsed request body. Helpers available in `handler.py`:

- `user_id(event)` returns the signed-in user's Cognito ID (`sub`). Use it for
  ownership; never trust a user ID sent in the request body.
- `raise RunmodeError(status, message)` returns an error response, e.g.
  `raise RunmodeError(404, "Item not found")` → `404 {"error": "Item not found"}`.
- Return values can contain `datetime` and `Decimal` (as MySQL returns them).
  Datetimes become ISO-8601 UTC strings like `2026-10-19T02:30:00Z`.

## Using the database

Set the database user in `config.yaml` and push:
```yaml
database:
  user: hawkeye_reader      # or hawkeye_writer
```
The Lambda then runs inside the `hawkeye-database` VPC, with access to the database
and permission to log in as that user using an IAM token, so no password is stored.
In code:
```python
from db import get_connection

def list_items(payload, event):
    with get_connection().cursor() as cursor:
        cursor.execute("SELECT id, name FROM items ORDER BY id")
        return {"items": cursor.fetchall()}
```
Rows come back as dicts. Always pass values as parameters (`%s`), never with
f-strings, to avoid SQL injection.

Database users and their permissions are defined in `hawkeye-database`
(`sql/users.sql`).

> **No internet inside the VPC.** A Lambda with `database.user` set can reach the
> database, but not the internet or other AWS APIs such as S3 or Secrets Manager.
> If one needs both, it needs a NAT gateway or VPC endpoint added to
> `hawkeye-database`.

## Using the imagery bucket

Set `storage.access` in `config.yaml` (`read` or `write`) and push. The bucket name
comes from `hawkeye-database`. In code:
```python
import storage

storage.presigned_view_url("marinas/1/images/abc.jpg")          # read: URL a browser can load
storage.presigned_upload(key, "image/jpeg", max_bytes=50_000_000)  # write: {"url", "fields"} for a browser POST
storage.head(key)                                               # write: {"size", "content_type"} or None
```
Browsers upload straight to S3 with the presigned form, so large files never pass
through the Lambda.

## Everyday changes

- **Function code:** edit `lambda/handler.py`, then push.
- **Function dependencies:** add them to `lambda/requirements.txt`, then push.
- **Memory, timeout, env vars:** edit the `lambda:` section of
  `config.yaml`, then push.
- **Who can call the API, or CORS origins:** edit `API` in `app.py`, then push.
- **Permissions or other AWS resources** (S3, DynamoDB, SQS triggers, …): add them in
  `pipeline/lambda_stack.py`, then push. For example:
  ```python
  table.grant_read_write_data(self.function)
  ```

## Tearing down

```bash
cdk destroy                                   # removes the pipeline
aws cloudformation delete-stack --stack-name <app_name>   # removes the Lambda
```
The pipeline's artifact bucket is retained; delete it from S3 if you want it gone.

## Cost

The pipeline is a V2 pipeline, which has no monthly fee and bills per
action-minute while it runs, so it costs cents per push. CodeBuild minutes for the
build steps are billed separately. Both are idle-free, and the Lambda itself is
pay-per-invocation.
