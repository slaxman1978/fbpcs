#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import asyncio

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Type

import dateutil.parser
import pytz
from fbpcs.bolt.bolt_checkpoint import bolt_checkpoint
from fbpcs.bolt.bolt_job import BoltJob, BoltPlayerArgs
from fbpcs.bolt.bolt_runner import BoltRunner
from fbpcs.bolt.bolt_summary import BoltSummary
from fbpcs.bolt.oss_bolt_pcs import BoltPCSClient, BoltPCSCreateInstanceArgs
from fbpcs.common.feature.pcs_feature_gate_utils import get_stage_flow
from fbpcs.common.service.graphapi_trace_logging_service import (
    GraphApiTraceLoggingService,
)
from fbpcs.common.service.input_data_service import InputDataService
from fbpcs.common.service.trace_logging_service import TraceLoggingService
from fbpcs.pl_coordinator.bolt_graphapi_client import (
    BoltGraphAPIClient,
    BoltPAGraphAPICreateInstanceArgs,
)
from fbpcs.pl_coordinator.constants import MAX_NUM_INSTANCES
from fbpcs.pl_coordinator.exceptions import (
    GraphAPIGenericException,
    IncorrectVersionError,
    OneCommandRunnerBaseException,
    OneCommandRunnerExitCode,
    PCAttributionValidationException,
    sys_exit_after,
)
from fbpcs.private_computation.entity.infra_config import (
    PrivateComputationGameType,
    PrivateComputationRole,
)
from fbpcs.private_computation.entity.pcs_feature import PCSFeature
from fbpcs.private_computation.entity.pcs_tier import PCSTier
from fbpcs.private_computation.entity.product_config import (
    AggregationType,
    AttributionRule,
)
from fbpcs.private_computation.stage_flows.private_computation_base_stage_flow import (
    PrivateComputationBaseStageFlow,
)
from fbpcs.private_computation_cli.private_computation_service_wrapper import (
    build_private_computation_service,
    get_tier,
    get_trace_logging_service,
)


# dataset information fields
DATASETS_INFORMATION = "datasets_information"
TARGET_ID = "target_id"
INSTANCES = "instances"
NUM_SHARDS = "num_shards"
NUM_CONTAINERS = "num_containers"

# instance fields
TIMESTAMP = "timestamp"
ATTRIBUTION_RULE = "attribution_rule"
STATUS = "status"
CREATED_TIME = "created_time"
TIER = "tier"
FEATURE_LIST = "feature_list"

TERMINAL_STATUSES = [
    "POST_PROCESSING_HANDLERS_COMPLETED",
    "RESULT_READY",
    "INSTANCE_FAILURE",
]


LOG_COMPONENT = "pc_attribution_runner"

"""
The input to this function will be the input path, the dataset_id as well as the following params to choose
a specific dataset range to create and run a PA instance on
1) timestamp - timestamp of the day(0AM) describing the data uploaded from the Meta side
2) attribution_rule - attribution rule for the selected data
3) result_type - result type for the selected data
"""


@sys_exit_after
def run_attribution(
    config: Dict[str, Any],
    dataset_id: str,
    input_path: str,
    timestamp: str,
    attribution_rule: AttributionRule,
    aggregation_type: AggregationType,
    concurrency: int,
    num_files_per_mpc_container: int,
    k_anonymity_threshold: int,
    stage_flow: Type[PrivateComputationBaseStageFlow],
    logger: logging.Logger,
    num_tries: Optional[int] = None,  # this is number of tries per stage
    final_stage: Optional[PrivateComputationBaseStageFlow] = None,
    run_id: Optional[str] = None,
    graphapi_version: Optional[str] = None,
    graphapi_domain: Optional[str] = None,
) -> None:
    bolt_summary = asyncio.run(
        run_attribution_async(
            config=config,
            dataset_id=dataset_id,
            input_path=input_path,
            timestamp=timestamp,
            attribution_rule=attribution_rule,
            aggregation_type=aggregation_type,
            concurrency=concurrency,
            num_files_per_mpc_container=num_files_per_mpc_container,
            k_anonymity_threshold=k_anonymity_threshold,
            stage_flow=stage_flow,
            logger=logger,
            num_tries=num_tries,
            final_stage=final_stage,
            run_id=run_id,
            graphapi_version=graphapi_version,
            graphapi_domain=graphapi_domain,
        )
    )

    if bolt_summary.is_failure:
        sys.exit(1)


async def run_attribution_async(
    config: Dict[str, Any],
    dataset_id: str,
    input_path: str,
    timestamp: str,
    attribution_rule: AttributionRule,
    aggregation_type: AggregationType,
    concurrency: int,
    num_files_per_mpc_container: int,
    k_anonymity_threshold: int,
    stage_flow: Type[PrivateComputationBaseStageFlow],
    logger: logging.Logger,
    num_tries: Optional[int] = None,  # this is number of tries per stage
    final_stage: Optional[PrivateComputationBaseStageFlow] = None,
    run_id: Optional[str] = None,
    graphapi_version: Optional[str] = None,
    graphapi_domain: Optional[str] = None,
) -> BoltSummary:

    ## Step 1: Validation. Function arguments and  for private attribution run.
    # obtain the values in the dataset info vector.
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs] = BoltGraphAPIClient(
        config=config,
        logger=logger,
        graphapi_version=graphapi_version,
        graphapi_domain=graphapi_domain,
    )

    # Create a GraphApiTraceLoggingService specific for this study_id
    endpoint_url = f"{client.graphapi_url}/{dataset_id}/checkpoint"
    default_trace_logger = GraphApiTraceLoggingService(
        access_token=client.access_token,
        endpoint_url=endpoint_url,
    )
    # if the user configured a trace logging service via the config.yml file, use that
    # instead of the default trace logger
    trace_logging_svc = get_trace_logging_service(
        config, default_trace_logger=default_trace_logger
    )
    # register the trace_logging_svc as a Bolt global default
    bolt_checkpoint.register_trace_logger(trace_logging_svc)
    # register the run id as a Bolt global default
    # sets a unique default run id if run_id was None
    run_id = bolt_checkpoint.register_run_id(run_id)

    return await _run_attribution_async_helper(
        client=client,
        trace_logging_svc=trace_logging_svc,
        config=config,
        dataset_id=dataset_id,
        input_path=input_path,
        timestamp=timestamp,
        attribution_rule=attribution_rule,
        aggregation_type=aggregation_type,
        concurrency=concurrency,
        num_files_per_mpc_container=num_files_per_mpc_container,
        k_anonymity_threshold=k_anonymity_threshold,
        stage_flow=stage_flow,
        logger=logger,
        num_tries=num_tries,
        final_stage=final_stage,
        run_id=run_id,
        graphapi_version=graphapi_version,
        graphapi_domain=graphapi_domain,
    )


@bolt_checkpoint(
    dump_params=True,
    include=[
        "dataset_id",
        "input_path",
        "timestamp",
        "attribution_rule",
        "aggregation_type",
        "concurrency",
        "num_files_per_mpc_container",
        "k_anonymity_threshold",
    ],
    dump_return_val=True,
    checkpoint_name="RUN_ATTRIBUTION",
    component=LOG_COMPONENT,
)
async def _run_attribution_async_helper(
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs],
    trace_logging_svc: TraceLoggingService,
    *,
    config: Dict[str, Any],
    dataset_id: str,
    input_path: str,
    timestamp: str,
    attribution_rule: AttributionRule,
    aggregation_type: AggregationType,
    concurrency: int,
    num_files_per_mpc_container: int,
    k_anonymity_threshold: int,
    stage_flow: Type[PrivateComputationBaseStageFlow],
    logger: logging.Logger,
    num_tries: Optional[int],
    final_stage: Optional[PrivateComputationBaseStageFlow],
    run_id: Optional[str],
    graphapi_version: Optional[str],
    graphapi_domain: Optional[str],
) -> BoltSummary:

    try:
        datasets_info = _get_attribution_dataset_info(client, dataset_id, logger)
    except GraphAPIGenericException as err:
        logger.error(err)
        raise PCAttributionValidationException(
            cause=f"Read attribution dataset {dataset_id} data failed.",
            remediation=f"Check access token has permission to read dataset {dataset_id}",
            exit_code=OneCommandRunnerExitCode.ERROR_READ_DATASET,
        )

    datasets = datasets_info[DATASETS_INFORMATION]
    target_id = datasets_info[TARGET_ID]
    # Verify adspixel
    _verify_adspixel(target_id, client)
    matched_data = {}
    attribution_rule_str = attribution_rule.name
    attribution_rule_val = attribution_rule.value
    instance_id = None

    dt = timestamp_to_dt(timestamp)

    # Compute the argument after the timestamp has been input
    dt_arg = int(datetime.timestamp(dt))

    # Verify that input has matching dataset info:
    # a. attribution rule
    # b. timestamp
    if len(datasets) == 0:
        raise ValueError("Dataset for given parameters and dataset invalid")
    for data in datasets:
        if data["key"] == attribution_rule_str:
            matched_attr = data["value"]

    for m_data in matched_attr:
        m_time = dateutil.parser.parse(m_data[TIMESTAMP])
        if m_time == dt:
            matched_data = m_data
            break
    if len(matched_data) == 0:
        raise ValueError("No dataset matching to the information provided")
    # Step 2: Validate what instances need to be created vs what already exist
    # Conditions for retry:
    # 1. Not in a terminal status
    # 2. Instance has been created > 1d ago
    try:
        dataset_instance_data = _get_existing_pa_instances(client, dataset_id)
    except GraphAPIGenericException as err:
        logger.error(err)
        raise PCAttributionValidationException(
            cause=f"Read dataset instance {dataset_id} failed.",
            remediation=f"Check access token has permission to read dataset instance {dataset_id}",
            exit_code=OneCommandRunnerExitCode.ERROR_READ_PA_INSTANCE,
        )

    existing_instances = dataset_instance_data["data"]
    for inst in existing_instances:
        if _should_resume_instance(inst, dt, attribution_rule):
            instance_id = inst["id"]
            break

    if instance_id is None:
        try:
            instance_id = await _create_new_instance(
                dataset_id,
                int(dt_arg),
                attribution_rule_val,
                run_id,
                client,
                logger,
            )
        except GraphAPIGenericException as err:
            logger.error(err)
            raise PCAttributionValidationException(
                cause=f"Create dataset instance {dataset_id} failed.",
                remediation=f"Check access token has permission to create dataset instance {dataset_id}",
                exit_code=OneCommandRunnerExitCode.ERROR_CREATE_PA_INSTANCE,
            )

    instance_data = await _get_pa_instance_info(client, instance_id, logger)
    _check_version(instance_data, config)
    # override stage flow based on pcs feature gate. Please contact PSI team to have a similar adoption
    stage_flow_override = stage_flow
    # get the enabled features
    pcs_features = _get_pcs_features(instance_data)
    pcs_feature_enums = []
    if pcs_features:
        logger.info(f"Enabled features: {pcs_features}")
        pcs_feature_enums = [PCSFeature.from_str(feature) for feature in pcs_features]
        stage_flow_override = get_stage_flow(
            game_type=PrivateComputationGameType.ATTRIBUTION,
            pcs_feature_enums=set(pcs_feature_enums),
            stage_flow_cls=stage_flow,
        )
    num_pid_containers = instance_data[NUM_SHARDS]
    num_mpc_containers = instance_data[NUM_CONTAINERS]

    ## Step 3. Populate instance args and create Bolt jobs
    publisher_args = BoltPlayerArgs(
        create_instance_args=BoltPAGraphAPICreateInstanceArgs(
            instance_id=instance_id,
            dataset_id=dataset_id,
            timestamp=str(dt_arg),
            attribution_rule=attribution_rule.name,
            run_id=run_id,
        )
    )

    data_ts = matched_data[TIMESTAMP]
    timestamps = InputDataService.get_attribution_timestamps(data_ts)
    partner_args = BoltPlayerArgs(
        create_instance_args=BoltPCSCreateInstanceArgs(
            instance_id=instance_id,
            role=PrivateComputationRole.PARTNER,
            game_type=PrivateComputationGameType.ATTRIBUTION,
            input_path=input_path,
            num_pid_containers=num_pid_containers,
            num_mpc_containers=num_mpc_containers,
            stage_flow_cls=stage_flow_override,
            concurrency=concurrency,
            attribution_rule=attribution_rule,
            aggregation_type=aggregation_type,
            num_files_per_mpc_container=num_files_per_mpc_container,
            k_anonymity_threshold=k_anonymity_threshold,
            pcs_features=pcs_features,
            pid_configs=config["pid"],
            run_id=run_id,
            input_path_start_ts=timestamps.start_timestamp,
            input_path_end_ts=timestamps.end_timestamp,
        )
    )
    job = BoltJob(
        job_name=f"Job [dataset_id: {dataset_id}][timestamp: {dt_arg}]",
        publisher_bolt_args=publisher_args,
        partner_bolt_args=partner_args,
        num_tries=num_tries,
        final_stage=stage_flow_override.get_last_stage().previous_stage,
        poll_interval=60,
    )
    # Step 4. Run instances async

    logger.info(f"Started running instance {instance_id}.")
    bolt_summary = await run_bolt(
        publisher_client=client,
        trace_logging_svc=trace_logging_svc,
        config=config,
        logger=logger,
        job_list=[job],
        graphapi_version=graphapi_version,
        graphapi_domain=graphapi_domain,
    )
    logger.info(f"Finished running instance {instance_id}.")
    logger.info(bolt_summary)
    return bolt_summary


@bolt_checkpoint(component=LOG_COMPONENT)
async def run_bolt(
    publisher_client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs],
    trace_logging_svc: TraceLoggingService,
    config: Dict[str, Any],
    logger: logging.Logger,
    job_list: List[
        BoltJob[BoltPAGraphAPICreateInstanceArgs, BoltPCSCreateInstanceArgs]
    ],
    graphapi_version: Optional[str] = None,
    graphapi_domain: Optional[str] = None,
) -> BoltSummary:
    """Run private attribution with the BoltRunner in a dedicated function to ensure that
    the BoltRunner semaphore and runner.run_async share the same event loop.

    Arguments:
        config: The dict representation of a config.yml file
        logger: logger client
        job_list: The BoltJobs to execute
    """
    if not job_list:
        raise OneCommandRunnerBaseException(
            "Expected at least one job",
            "len(job_list) == 0",
            "Submit at least one job to call this API",
        )

    runner = BoltRunner(
        publisher_client=publisher_client,
        partner_client=BoltPCSClient(
            build_private_computation_service(
                pc_config=config["private_computation"],
                mpc_config=config["mpc"],
                pid_config=config["pid"],
                pph_config=config.get("post_processing_handlers", {}),
                pid_pph_config=config.get("pid_post_processing_handlers", {}),
                trace_logging_svc=trace_logging_svc,
            ),
        ),
        logger=logger,
        max_parallel_runs=MAX_NUM_INSTANCES,
    )

    # run all jobs
    return await runner.run_async(job_list)


@bolt_checkpoint(
    dump_params=True,
    include=["dataset_id", "timestamp", "attribution_rule"],
    dump_return_val=True,
    component=LOG_COMPONENT,
)
async def _create_new_instance(
    dataset_id: str,
    timestamp: int,
    attribution_rule: str,
    run_id: Optional[str],
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs],
    logger: logging.Logger,
) -> str:
    instance_id = await client.create_instance(
        BoltPAGraphAPICreateInstanceArgs(
            instance_id="",
            dataset_id=dataset_id,
            timestamp=str(timestamp),
            attribution_rule=attribution_rule,
            run_id=run_id,
        )
    )
    logger.info(
        f"Created instance {instance_id} for dataset {dataset_id} and attribution rule {attribution_rule}"
    )
    return instance_id


@bolt_checkpoint(component=LOG_COMPONENT)
def _check_version(
    instance: Dict[str, Any],
    config: Dict[str, Any],
) -> None:
    """Checks that the publisher version (graph api) and the partner version (config.yml) are the same

    Arguments:
        instances: theoretically is dict representing the PA instance fields.
        config: The dict representation of a config.yml file

    Raises:
        IncorrectVersionError: the publisher and partner are running with different versions
    """

    instance_tier_str = instance.get(TIER)
    # if there is no tier for some reason, let's just assume
    # the tier is correct
    if not instance_tier_str:
        return

    config_tier = get_tier(config)
    expected_tier = PCSTier.from_str(instance_tier_str)
    if expected_tier is not config_tier:
        raise IncorrectVersionError.make_error(
            instance["id"], expected_tier, config_tier
        )


def _is_unix_ts(timestamp: str) -> bool:
    try:
        int(timestamp)
        return True
    except ValueError:
        return False


def timestamp_to_dt(timestamp: str) -> datetime:
    pacific_timezone = pytz.timezone("US/Pacific")
    # Validate if input is datetime or timestamp
    is_date_format = _iso_date_validator(timestamp)
    if is_date_format:
        return pacific_timezone.localize(datetime.strptime(timestamp, "%Y-%m-%d"))
    elif _is_unix_ts(timestamp):
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    else:
        return dateutil.parser.parse(timestamp)


@bolt_checkpoint(dump_params=True, dump_return_val=True, component=LOG_COMPONENT)
def _should_resume_instance(
    inst: Dict[str, Any], dt: datetime, attribution_rule: AttributionRule
) -> bool:
    inst_time = dateutil.parser.parse(inst[TIMESTAMP])
    creation_time = dateutil.parser.parse(inst[CREATED_TIME])
    exp_time = datetime.now(tz=timezone.utc) - timedelta(days=1)
    expired = exp_time > creation_time
    return (
        inst[ATTRIBUTION_RULE] == attribution_rule.value
        and inst_time == dt
        and inst[STATUS] not in TERMINAL_STATUSES
        and not expired
    )


@bolt_checkpoint(dump_params=True, dump_return_val=True, component=LOG_COMPONENT)
def _get_pcs_features(instance: Dict[str, Any]) -> Optional[List[str]]:
    return instance.get(FEATURE_LIST)


@bolt_checkpoint(dump_params=True, include=["adpixels_id"], component=LOG_COMPONENT)
def _verify_adspixel(
    adspixels_id: str, client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs]
) -> None:
    try:
        client.get_adspixels(adspixels_id=adspixels_id, fields=["id"])
    except GraphAPIGenericException:
        raise PCAttributionValidationException(
            cause=f"Read adspixel {adspixels_id} failed.",
            remediation="Check access token has permission to read adspixel",
            exit_code=OneCommandRunnerExitCode.ERROR_READ_ADSPIXELS,
        )


# TODO: remove unused method
def get_attribution_dataset_info(
    config: Dict[str, Any],
    dataset_id: str,
    logger: logging.Logger,
    graphapi_version: Optional[str] = None,
    graphapi_domain: Optional[str] = None,
) -> str:
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs] = BoltGraphAPIClient(
        config=config,
        logger=logger,
        graphapi_version=graphapi_version,
        graphapi_domain=graphapi_domain,
    )

    return json.loads(
        client.get_attribution_dataset_info(
            dataset_id,
            [DATASETS_INFORMATION, TARGET_ID],
        ).text
    )


def get_runnable_timestamps(
    config: Dict[str, Any],
    dataset_id: str,
    logger: logging.Logger,
    attribution_rule: AttributionRule,
    graphapi_version: Optional[str] = None,
    graphapi_domain: Optional[str] = None,
) -> Iterable[str]:

    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs] = BoltGraphAPIClient(
        config=config,
        logger=logger,
        graphapi_version=graphapi_version,
        graphapi_domain=graphapi_domain,
    )

    datasets_info = _get_attribution_dataset_info(client, dataset_id, logger)
    datasets = datasets_info[DATASETS_INFORMATION]
    matching_datasets = [
        data["value"] for data in datasets if data["key"] == attribution_rule.name
    ]

    if not matching_datasets:
        return []

    possible_timestamps = {d[TIMESTAMP] for d in matching_datasets[0]}

    dataset_instance_data = _get_existing_pa_instances(client, dataset_id)
    existing_instances = dataset_instance_data["data"]

    timestamps_to_exclude = set()
    for inst in existing_instances:
        timestamp = inst[TIMESTAMP]
        dt = dateutil.parser.parse(timestamp)
        if _should_resume_instance(inst, dt, attribution_rule):
            timestamps_to_exclude.add(timestamp)

    runnable_timestamps = possible_timestamps.difference(timestamps_to_exclude)

    logger.info(f"Non-runnable timestamps: {timestamps_to_exclude}")
    logger.info(f"Runnable timestamps: {runnable_timestamps}")

    return runnable_timestamps


@bolt_checkpoint(component=LOG_COMPONENT)
async def _get_pa_instance_info(
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs],
    instance_id: str,
    logger: logging.Logger,
) -> Any:
    return json.loads((await client.get_instance(instance_id)).text)


def _iso_date_validator(timestamp: str) -> Any:
    try:
        datetime.strptime(timestamp, "%Y-%m-%d")
        return True
    except Exception:
        pass
    else:
        return False


@bolt_checkpoint(
    dump_params=True,
    include=["dataset_id"],
    component=LOG_COMPONENT,
)
def _get_attribution_dataset_info(
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs],
    dataset_id: str,
    logger: logging.Logger,
) -> Any:
    return json.loads(
        client.get_attribution_dataset_info(
            dataset_id,
            [DATASETS_INFORMATION, TARGET_ID],
        ).text
    )


@bolt_checkpoint(
    dump_params=True,
    include=["dataset_id"],
    component=LOG_COMPONENT,
)
def _get_existing_pa_instances(
    client: BoltGraphAPIClient[BoltPAGraphAPICreateInstanceArgs], dataset_id: str
) -> Any:
    return json.loads(client.get_existing_pa_instances(dataset_id).text)
