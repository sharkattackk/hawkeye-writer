#!/usr/bin/env python3
import aws_cdk as cdk

from pipeline.config import load_config
from pipeline.pipeline_stack import PipelineStack

config = load_config()

app = cdk.App()

PipelineStack(
    app,
    f"{config.app_name}-pipeline",
    config=config,
    env=cdk.Environment(account=config.account, region=config.region),
)

app.synth()
