#!/usr/bin/env python3
import dataclasses

import aws_cdk as cdk

from pipeline.config import ApiConfig, load_config
from pipeline.pipeline_stack import PipelineStack

# API Gateway in front of the Lambda. Set to None for a Lambda with no API.
# Callers POST to https://<api-id>.execute-api.<region>.amazonaws.com/invoke
# with an "Authorization: Bearer <Cognito token>" header.
API = ApiConfig(
    user_pool_id="us-east-2_mPtSngfn0",  # hawkeye-users
    user_pool_client_ids=["71jeepon9opiv4gg2fletq8508"],  # hawkeye-web
    cors_origins=[
        "https://production.d24ktnparigmd2.amplifyapp.com",
        "http://localhost:5173",
    ],
    path="/invoke",
)

config = dataclasses.replace(load_config(), api=API)

app = cdk.App()

PipelineStack(
    app,
    f"{config.app_name}-pipeline",
    config=config,
    env=cdk.Environment(account=config.account, region=config.region),
)

app.synth()
