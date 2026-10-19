from aws_cdk import Environment, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import pipelines
from constructs import Construct

from pipeline.config import Config
from pipeline.lambda_stage import LambdaStage


class PipelineStack(Stack):
    """Self-updating pipeline: GitHub push -> synth -> update pipeline -> deploy Lambda."""

    def __init__(self, scope: Construct, construct_id: str, *, config: Config, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        source = pipelines.CodePipelineSource.connection(
            config.repo_path,
            config.github_branch,
            connection_arn=config.connection_arn,
        )

        pipeline = pipelines.CodePipeline(
            self,
            "Pipeline",
            pipeline_name=f"{config.app_name}-pipeline",
            synth=pipelines.ShellStep(
                "Synth",
                input=source,
                install_commands=[
                    "npm install -g aws-cdk",
                    "pip install -r requirements.txt -r requirements-dev.txt",
                ],
                commands=[
                    "pytest -q",
                    "cdk synth",
                ],
            ),
            # PythonFunction bundles lambda/requirements.txt inside a Docker container
            docker_enabled_for_synth=True,
            code_build_defaults=pipelines.CodeBuildOptions(
                build_environment=codebuild.BuildEnvironment(
                    build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                ),
            ),
        )

        pipeline.add_stage(
            LambdaStage(
                self,
                "Deploy",
                config=config,
                env=Environment(account=config.account, region=config.region),
            )
        )

