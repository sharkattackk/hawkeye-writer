from pathlib import Path

from aws_cdk import CfnOutput, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_python_alpha as python
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from pipeline.config import ApiConfig, Config

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambda"

RUNTIMES = {
    "python3.12": lambda_.Runtime.PYTHON_3_12,
    "python3.13": lambda_.Runtime.PYTHON_3_13,
}

ARCHITECTURES = {
    "x86_64": lambda_.Architecture.X86_64,
    "arm64": lambda_.Architecture.ARM_64,
}


class LambdaStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, config: Config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        log_group = logs.LogGroup(
            self,
            "LogGroup",
            log_group_name=f"/aws/lambda/{config.app_name}",
            retention=_retention(config.log_retention_days),
            removal_policy=RemovalPolicy.DESTROY,
        )

        database = self._database_props(config) if config.database_user else {}
        bucket_name = (
            ssm.StringParameter.value_for_string_parameter(self, f"{config.database_ssm_prefix}/imagery-bucket")
            if config.storage_access != "none"
            else None
        )

        # Installs lambda/requirements.txt into the package; code lives in lambda/handler.py
        self.function = python.PythonFunction(
            self,
            "Function",
            function_name=config.app_name,
            entry=str(LAMBDA_DIR),
            index="handler.py",
            handler="handler",
            runtime=RUNTIMES[config.runtime],
            architecture=ARCHITECTURES[config.architecture],
            memory_size=config.memory_mb,
            timeout=Duration.seconds(config.timeout_seconds),
            environment={
                "APP_NAME": config.app_name,
                **database.get("environment", {}),
                **({"IMAGERY_BUCKET": bucket_name} if bucket_name else {}),
                **config.environment,
            },
            log_group=log_group,
            vpc=database.get("vpc"),
            vpc_subnets=database.get("vpc_subnets"),
            security_groups=database.get("security_groups"),
        )

        if bucket_name:
            self._grant_storage(bucket_name, config.storage_access)

        if config.database_user:
            # IAM database auth: lets the function log in as its database user
            self.function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["rds-db:connect"],
                    resources=[
                        f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:"
                        f"{database['resource_id']}/{config.database_user}"
                    ],
                )
            )

        CfnOutput(self, "FunctionName", value=self.function.function_name)

        if config.function_url:
            url = self.function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
            CfnOutput(self, "FunctionUrl", value=url.url)

        if config.api:
            self._add_api(config.app_name, config.api)

    def _grant_storage(self, bucket_name: str, access: str) -> None:
        objects = f"arn:aws:s3:::{bucket_name}/*"
        self.function.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[objects]))
        if access == "write":
            self.function.add_to_role_policy(
                iam.PolicyStatement(actions=["s3:PutObject", "s3:DeleteObject"], resources=[objects])
            )
            # Lets head_object report a missing upload as 404 rather than 403
            self.function.add_to_role_policy(
                iam.PolicyStatement(actions=["s3:ListBucket"], resources=[f"arn:aws:s3:::{bucket_name}"])
            )

    def _database_props(self, config: Config) -> dict:
        """Look up hawkeye-database's VPC and connection details from SSM at deploy time."""

        def param(name: str) -> str:
            return ssm.StringParameter.value_for_string_parameter(self, f"{config.database_ssm_prefix}/{name}")

        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "DatabaseVpc",
            vpc_id=param("vpc-id"),
            availability_zones=Fn.split(",", param("availability-zones"), 2),
            isolated_subnet_ids=Fn.split(",", param("lambda-subnet-ids"), 2),
        )

        return {
            "vpc": vpc,
            "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            "security_groups": [
                ec2.SecurityGroup.from_security_group_id(
                    self, "DatabaseClientSecurityGroup", param("client-security-group-id")
                )
            ],
            "environment": {
                "DB_HOST": param("host"),
                "DB_PORT": param("port"),
                "DB_NAME": param("name"),
                "DB_USER": config.database_user,
            },
            "resource_id": param("resource-id"),
        }

    def _add_api(self, app_name: str, api_config: ApiConfig) -> None:
        user_pool = cognito.UserPool.from_user_pool_id(self, "UserPool", api_config.user_pool_id)
        clients = [
            cognito.UserPoolClient.from_user_pool_client_id(self, f"UserPoolClient{i}", client_id)
            for i, client_id in enumerate(api_config.user_pool_client_ids)
        ]

        api = apigw.HttpApi(
            self,
            "Api",
            api_name=app_name,
            # API Gateway answers CORS preflight itself, before the authorizer runs
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=api_config.cors_origins,
                allow_methods=[apigw.CorsHttpMethod.POST, apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["authorization", "content-type"],
                max_age=Duration.days(1),
            ),
        )

        api.add_routes(
            path=api_config.path,
            methods=[apigw.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("Integration", self.function),
            authorizer=authorizers.HttpUserPoolAuthorizer(
                "Authorizer",
                user_pool,
                user_pool_clients=clients,
            ),
        )

        CfnOutput(self, "ApiUrl", value=f"{api.api_endpoint}{api_config.path}")


RETENTION = {
    1: logs.RetentionDays.ONE_DAY,
    3: logs.RetentionDays.THREE_DAYS,
    5: logs.RetentionDays.FIVE_DAYS,
    7: logs.RetentionDays.ONE_WEEK,
    14: logs.RetentionDays.TWO_WEEKS,
    30: logs.RetentionDays.ONE_MONTH,
    60: logs.RetentionDays.TWO_MONTHS,
    90: logs.RetentionDays.THREE_MONTHS,
    180: logs.RetentionDays.SIX_MONTHS,
    365: logs.RetentionDays.ONE_YEAR,
    731: logs.RetentionDays.TWO_YEARS,
    1827: logs.RetentionDays.FIVE_YEARS,
    3653: logs.RetentionDays.TEN_YEARS,
}


def _retention(days: int) -> logs.RetentionDays:
    """Round up to the nearest retention period CloudWatch accepts."""
    return next((RETENTION[d] for d in sorted(RETENTION) if d >= days), logs.RetentionDays.TEN_YEARS)
