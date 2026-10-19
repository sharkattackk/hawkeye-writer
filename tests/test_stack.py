import dataclasses

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


def test_database_access():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, database_user="hawkeye_reader"))
    template = Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "VpcConfig": Match.object_like({"SubnetIds": Match.any_value(), "SecurityGroupIds": Match.any_value()}),
            "Environment": {
                "Variables": Match.object_like({"DB_USER": "hawkeye_reader", "DB_HOST": Match.any_value()}),
            },
        },
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({"Action": "rds-db:connect"}),
                ]),
            },
        },
    )


def test_no_database_by_default():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, database_user=""))
    function = next(iter(Template.from_stack(stack).find_resources("AWS::Lambda::Function").values()))
    assert "VpcConfig" not in function["Properties"]


def _policy_actions(template):
    actions = set()
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement["Action"]
            actions.update(action if isinstance(action, list) else [action])
    return actions


def test_storage_read():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, storage_access="read"))
    template = Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": Match.object_like({"IMAGERY_BUCKET": Match.any_value()})}},
    )
    actions = _policy_actions(template)
    assert "s3:GetObject" in actions
    assert "s3:PutObject" not in actions


def test_storage_write():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, storage_access="write"))
    assert {"s3:GetObject", "s3:PutObject", "s3:ListBucket"} <= _policy_actions(Template.from_stack(stack))


def test_no_storage_by_default():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, storage_access="none"))
    template = Template.from_stack(stack)
    assert not any(a.startswith("s3:") for a in _policy_actions(template))


def test_no_api_by_default():
    stack = LambdaStack(_app(), "Lambda", config=dataclasses.replace(config, api=None))
    Template.from_stack(stack).resource_count_is("AWS::ApiGatewayV2::Api", 0)
