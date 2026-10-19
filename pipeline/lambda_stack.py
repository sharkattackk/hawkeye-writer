from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_python_alpha as python
from aws_cdk import aws_logs as logs
from constructs import Construct

from pipeline.config import Config

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
            environment={"APP_NAME": config.app_name, **config.environment},
            log_group=log_group,
        )

        CfnOutput(self, "FunctionName", value=self.function.function_name)

        if config.function_url:
            url = self.function.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)
            CfnOutput(self, "FunctionUrl", value=url.url)


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
