from aws_cdk import Stage
from constructs import Construct

from pipeline.config import Config
from pipeline.lambda_stack import LambdaStack


class LambdaStage(Stage):
    """The set of stacks the pipeline deploys. Add more stacks here if the app grows."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        LambdaStack(self, "Lambda", stack_name=config.app_name, config=config)
