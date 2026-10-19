# Lambda CDK Template

A template for Python Lambda functions on AWS. You run `cdk deploy` once, and that
creates a CodePipeline. After that, every push to the configured branch tests,
builds and deploys the Lambda automatically. The pipeline also updates itself when
you change the pipeline code.

```
GitHub push ──> Source ──> Build (pytest + cdk synth) ──> UpdatePipeline ──> Assets ──> Deploy Lambda
```

## Layout

| Path | What it is |
| --- | --- |
| `config.yaml` | **The file you edit:** app name, repo, branch, Lambda settings |
| `lambda/handler.py` | Your function code (`handler(event, context)`) |
| `lambda/requirements.txt` | The function's pip dependencies, bundled automatically |
| `app.py` | CDK entry point |
| `pipeline/pipeline_stack.py` | The CodePipeline definition |
| `pipeline/lambda_stage.py` | The stage the pipeline deploys (add more stacks here) |
| `pipeline/lambda_stack.py` | The Lambda function, its log group and an optional function URL |
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

## Everyday changes

- **Function code:** edit `lambda/handler.py`, then push.
- **Function dependencies:** add them to `lambda/requirements.txt`, then push.
- **Memory, timeout, env vars, public URL:** edit the `lambda:` section of
  `config.yaml`, then push.
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
