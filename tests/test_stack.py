import dataclasses
import importlib.util
import json

import aws_cdk as cdk
from aws_cdk.assertions import Match, Template

from pipeline.config import ApiConfig, load_config
from pipeline.lambda_stack import LAMBDA_DIR, LambdaStack
from pipeline.pipeline_stack import PipelineStack

config = load_config(require_connection=False)
if not config.connection_arn.startswith("arn:aws:"):
    # Lets tests run before the real connection ARN is filled in
    config = dataclasses.replace(
        config,
        connection_arn=f"arn:aws:codeconnections:{config.region}:{config.account}:connection/test",
    )


def _app():
    # Skip Docker bundling so tests run anywhere
    return cdk.App(context={"aws:cdk:bundling-stacks": []})


def test_lambda_stack():
    stack = LambdaStack(_app(), "Lambda", config=config)
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "FunctionName": config.app_name,
            "Handler": "handler.handler",
            "Runtime": config.runtime,
            "MemorySize": config.memory_mb,
            "Timeout": config.timeout_seconds,
        },
    )
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"LogGroupName": f"/aws/lambda/{config.app_name}"},
    )


def test_pipeline_stack():
    stack = PipelineStack(
        _app(),
        "Pipeline",
        config=config,
        env=cdk.Environment(account=config.account, region=config.region),
    )
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::CodePipeline::Pipeline",
        {"Name": f"{config.app_name}-pipeline"},
    )
    template.has_resource_properties(
        "AWS::CodePipeline::Pipeline",
        {
            "Stages": Match.array_with([
                Match.object_like({
                    "Name": "Source",
                    "Actions": [
                        Match.object_like({
                            "Configuration": Match.object_like({
                                "FullRepositoryId": config.repo_path,
                                "BranchName": config.github_branch,
                            }),
                        }),
                    ],
                }),
            ]),
        },
    )


def test_api():
    api_config = ApiConfig(
        user_pool_id="us-east-2_example",
        user_pool_client_ids=["exampleclient"],
        cors_origins=["http://localhost:5173"],
    )
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, api=api_config))
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::ApiGatewayV2::Api",
        {
            "ProtocolType": "HTTP",
            "CorsConfiguration": Match.object_like({"AllowOrigins": ["http://localhost:5173"]}),
        },
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route",
        {"RouteKey": "POST /invoke", "AuthorizationType": "JWT"},
    )
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Authorizer",
        {
            "AuthorizerType": "JWT",
            "JwtConfiguration": {
                "Audience": ["exampleclient"],
                "Issuer": Match.any_value(),
            },
        },
    )


def test_no_api_by_default():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, api=None))
    Template.from_stack(stack).resource_count_is("AWS::ApiGatewayV2::Api", 0)


def _load_handler():
    # "lambda" is a Python keyword, so load the module by path
    spec = importlib.util.spec_from_file_location("handler", LAMBDA_DIR / "handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.handler


def test_write_runmode():
    response = _load_handler()({"body": json.dumps({"runmode": "write"})}, None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"message": "writer written"}


def test_direct_invoke():
    response = _load_handler()({"runmode": "write"}, None)
    assert json.loads(response["body"]) == {"message": "writer written"}


def test_unknown_runmode():
    response = _load_handler()({"body": json.dumps({"runmode": "nope"})}, None)
    assert response["statusCode"] == 400


def test_invalid_body():
    response = _load_handler()({"body": "not json"}, None)
    assert response["statusCode"] == 400
