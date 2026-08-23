"""The actual data pipeline:

    EventBridge Scheduler (hourly)
        -> Step Functions state machine
              -> Map: fetch current weather for each city in parallel
                       (Open-Meteo public API, no key needed)
              -> Lambda: transform results into JSON-Lines
              -> Step Functions native SDK integration: PUT the file
                 straight into S3 (no Lambda needed for the write)
        -> Glue Data Catalog table over S3, using partition projection
           (no crawler to run/pay for -- Athena computes partitions
           on the fly from the dt=/hour= key layout)
        -> Athena workgroup to query it

Any unhandled failure in the workflow (all cities failed to fetch, the
transform blew up, the S3 write failed, ...) publishes to an SNS topic
so you get notified instead of the run silently vanishing.
"""
from typing import Optional

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_athena as athena
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

# Cities to poll every run. Add/remove freely -- everything downstream
# (Map concurrency, partitioning, Glue schema) is unaffected by count.
CITIES = [
    {"city": "Sydney", "latitude": -33.8688, "longitude": 151.2093},
    {"city": "Melbourne", "latitude": -37.8136, "longitude": 144.9631},
    {"city": "Perth", "latitude": -31.9523, "longitude": 115.8613},
    {"city": "Auckland", "latitude": -36.8509, "longitude": 174.7645},
    {"city": "Singapore", "latitude": 1.3521, "longitude": 103.8198},
]

GLUE_DATABASE_NAME = "weather_pipeline_db"
GLUE_TABLE_NAME = "observations"

GLUE_COLUMNS = [
    ("city", "string"),
    ("latitude", "double"),
    ("longitude", "double"),
    ("temperature_c", "double"),
    ("windspeed_kmh", "double"),
    ("winddirection_deg", "double"),
    ("weathercode", "int"),
    ("observation_time", "string"),
    ("fetched_at", "string"),
]


class WeatherPipelineStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alert_email: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- Storage --------------------------------------------------
        data_bucket = s3.Bucket(
            self,
            "WeatherDataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,  # demo convenience; use RETAIN in production
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="ExpireAthenaQueryResults",
                    prefix="athena-results/",
                    expiration=Duration.days(7),
                )
            ],
        )

        # ---- Alerting ---------------------------------------------------
        alerts_topic = sns.Topic(self, "PipelineAlertsTopic", display_name="Weather pipeline failures")
        if alert_email:
            alerts_topic.add_subscription(subs.EmailSubscription(alert_email))

        # ---- Lambdas ------------------------------------------------------
        fetch_fn = _lambda.Function(
            self,
            "FetchWeatherFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/fetch_weather"),
            timeout=Duration.seconds(10),
            memory_size=128,
            log_group=logs.LogGroup(
                self,
                "FetchWeatherLogGroup",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            description="Calls the Open-Meteo public API for one city's current weather.",
        )

        transform_fn = _lambda.Function(
            self,
            "TransformWeatherFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/transform"),
            timeout=Duration.seconds(10),
            memory_size=128,
            log_group=logs.LogGroup(
                self,
                "TransformWeatherLogGroup",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            description="Normalizes the Map's fetch results into a JSON-Lines S3 object.",
        )

        # ---- Step Functions state machine ------------------------------
        prepare_cities = sfn.Pass(
            self,
            "PrepareCities",
            parameters={
                "cities": CITIES,
                "run_id.$": "$$.Execution.Name",
                "run_started_at.$": "$$.Execution.StartTime",
            },
        )

        # On failure inside the Map, the Catch below merges an "error"
        # field onto the ORIGINAL per-city input (that's how ASL's
        # ResultPath works on a Catch), so this Pass just terminates
        # that iteration cleanly with {city, latitude, longitude, error}.
        fetch_failed = sfn.Pass(self, "FetchFailed")

        fetch_task = tasks.LambdaInvoke(
            self,
            "FetchWeather",
            lambda_function=fetch_fn,
            payload_response_only=True,
        )
        fetch_task.add_retry(errors=["States.ALL"], interval=Duration.seconds(2), max_attempts=2, backoff_rate=2.0)
        fetch_task.add_catch(fetch_failed, errors=["States.ALL"], result_path="$.error")

        for_each_city = sfn.Map(
            self,
            "ForEachCity",
            items_path="$.cities",
            max_concurrency=4,  # be polite to the free public API
            result_path="$.fetch_results",
        )
        for_each_city.item_processor(fetch_task)

        transform_task = tasks.LambdaInvoke(
            self,
            "TransformWeatherData",
            lambda_function=transform_fn,
            payload_response_only=True,
            payload=sfn.TaskInput.from_object(
                {
                    "fetch_results.$": "$.fetch_results",
                    "run_id.$": "$.run_id",
                    "run_started_at.$": "$.run_started_at",
                }
            ),
            result_path="$.transformed",
        )

        load_to_s3 = tasks.CallAwsService(
            self,
            "LoadToS3",
            service="s3",
            action="putObject",
            iam_action="s3:PutObject",
            parameters={
                "Bucket": data_bucket.bucket_name,
                "Key.$": "$.transformed.key",
                "Body.$": "$.transformed.body",
                "ContentType": "application/json",
            },
            iam_resources=[data_bucket.arn_for_objects("processed/*")],
            result_path=sfn.JsonPath.DISCARD,
        )

        notify_failure = tasks.SnsPublish(
            self,
            "NotifyFailure",
            topic=alerts_topic,
            subject="Weather pipeline execution failed",
            message=sfn.TaskInput.from_text(
                "The weather pipeline failed. Check the Step Functions execution history for details."
            ),
        )

        for state in (for_each_city, transform_task, load_to_s3):
            state.add_catch(notify_failure, errors=["States.ALL"], result_path="$.stateMachineError")

        definition = prepare_cities.next(for_each_city).next(transform_task).next(load_to_s3)

        state_machine_log_group = logs.LogGroup(
            self,
            "StateMachineLogGroup",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        state_machine = sfn.StateMachine(
            self,
            "WeatherPipelineStateMachine",
            state_machine_name="weather-ingestion-pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(5),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=state_machine_log_group, level=sfn.LogLevel.ALL),
        )

        # ---- EventBridge Scheduler (hourly trigger) ------------------
        scheduler_role = iam.Role(
            self,
            "SchedulerExecutionRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description="Lets EventBridge Scheduler start executions of the weather pipeline state machine.",
        )
        state_machine.grant_start_execution(scheduler_role)

        scheduler.CfnSchedule(
            self,
            "HourlyWeatherSchedule",
            schedule_expression="rate(1 hour)",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            target=scheduler.CfnSchedule.TargetProperty(
                arn=state_machine.state_machine_arn,
                role_arn=scheduler_role.role_arn,
                retry_policy=scheduler.CfnSchedule.RetryPolicyProperty(maximum_retry_attempts=1),
                input="{}",
            ),
            state="ENABLED",
        )

        # ---- Glue Data Catalog (partition projection, no crawler) ------
        glue.CfnDatabase(
            self,
            "WeatherGlueDatabase",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(name=GLUE_DATABASE_NAME),
        )

        table_parameters = {
            "classification": "json",
            # Partition projection computes partitions from the S3 key
            # layout at query time -- no Glue Crawler to run or pay for.
            # https://docs.aws.amazon.com/athena/latest/ug/partition-projection.html
            "projection.enabled": "true",
            "projection.dt.type": "date",
            "projection.dt.format": "yyyy-MM-dd",
            "projection.dt.range": "2024-01-01,NOW",
            "projection.dt.interval": "1",
            "projection.dt.interval.unit": "DAYS",
            "projection.hour.type": "integer",
            "projection.hour.range": "0,23",
            "projection.hour.digits": "2",
            "storage.location.template": f"s3://{data_bucket.bucket_name}/processed/dt=${{dt}}/hour=${{hour}}/",
        }

        glue_table = glue.CfnTable(
            self,
            "WeatherGlueTable",
            catalog_id=self.account,
            database_name=GLUE_DATABASE_NAME,
            table_input=glue.CfnTable.TableInputProperty(
                name=GLUE_TABLE_NAME,
                table_type="EXTERNAL_TABLE",
                parameters=table_parameters,
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="dt", type="string"),
                    glue.CfnTable.ColumnProperty(name="hour", type="string"),
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{data_bucket.bucket_name}/processed/",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe",
                    ),
                    columns=[glue.CfnTable.ColumnProperty(name=name, type=type_) for name, type_ in GLUE_COLUMNS],
                ),
            ),
        )
        glue_table.node.add_dependency(data_bucket)

        athena_workgroup = athena.CfnWorkGroup(
            self,
            "WeatherAthenaWorkGroup",
            name="weather-pipeline-wg",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{data_bucket.bucket_name}/athena-results/",
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
            ),
        )

        # ---- Observability: alarm on failed executions -----------------
        failure_alarm = cw.Alarm(
            self,
            "PipelineFailureAlarm",
            metric=state_machine.metric_failed(period=Duration.minutes(5), statistic="sum"),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            alarm_description="Fires when the weather ingestion pipeline execution fails.",
        )
        failure_alarm.add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # ---- Outputs -----------------------------------------------------
        CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "GlueDatabaseName", value=GLUE_DATABASE_NAME)
        CfnOutput(self, "AthenaWorkGroupName", value=athena_workgroup.name)
        CfnOutput(self, "AlertsTopicArn", value=alerts_topic.topic_arn)
