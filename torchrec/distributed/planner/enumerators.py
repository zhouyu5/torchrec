#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import logging
from typing import Dict, List, Optional, Tuple, Union

from torch import nn
from torchrec.distributed.embedding_types import EmbeddingComputeKernel
from torchrec.distributed.planner.constants import POOLING_FACTOR
from torchrec.distributed.planner.shard_estimators import (
    EmbeddingPerfEstimator,
    EmbeddingStorageEstimator,
)
from torchrec.distributed.planner.types import (
    Enumerator,
    ParameterConstraints,
    PartitionByType,
    Shard,
    ShardEstimator,
    ShardingOption,
    Topology,
)
from torchrec.distributed.planner.utils import sharder_name
from torchrec.distributed.sharding_plan import calculate_shard_sizes_and_offsets
from torchrec.distributed.types import (
    BoundsCheckMode,
    CacheParams,
    ModuleSharder,
    ShardingType,
)
from torchrec.modules.embedding_tower import EmbeddingTower, EmbeddingTowerCollection


logger: logging.Logger = logging.getLogger(__name__)


class EmbeddingEnumerator(Enumerator):
    """
    Generates embedding sharding options for given `nn.Module`, considering user provided
    constraints.

    Args:
        topology (Topology): device topology.
        batch_size (int): batch size.
        constraints (Optional[Dict[str, ParameterConstraints]]): dict of parameter names
            to provided ParameterConstraints.
    """

    def __init__(
        self,
        topology: Topology,
        batch_size: int,
        constraints: Optional[Dict[str, ParameterConstraints]] = None,
        estimator: Optional[Union[ShardEstimator, List[ShardEstimator]]] = None,
    ) -> None:
        self._compute_device: str = topology.compute_device
        self._world_size: int = topology.world_size
        self._local_world_size: int = topology.local_world_size
        self._batch_size: int = batch_size
        self._constraints = constraints
        self._sharder_map: Dict[str, ModuleSharder[nn.Module]] = {}

        if estimator:
            self._estimators: List[ShardEstimator] = (
                [estimator] if not isinstance(estimator, list) else estimator
            )
        else:
            self._estimators: List[ShardEstimator] = [
                EmbeddingPerfEstimator(topology=topology, constraints=constraints),
                EmbeddingStorageEstimator(topology=topology, constraints=constraints),
            ]

    def enumerate(
        self,
        module: nn.Module,
        sharders: List[ModuleSharder[nn.Module]],
    ) -> List[ShardingOption]:
        """
        Generates relevant sharding options given module and sharders.

        Args:
            module (nn.Module): module to be sharded.
            sharders (List[ModuleSharder[nn.Module]]): provided sharders for module.

        Returns:
            List[ShardingOption]: valid sharding options with values populated.
        """

        self._sharder_map = {
            sharder_name(sharder.module_type): sharder for sharder in sharders
        }
        sharding_options: List[ShardingOption] = []

        named_modules_queue = [("", module)]
        while named_modules_queue:
            child_path, child_module = named_modules_queue.pop()
            sharder_key = sharder_name(type(child_module))
            sharder = self._sharder_map.get(sharder_key, None)
            if not sharder:
                for n, m in child_module.named_children():
                    if child_path != "":
                        named_modules_queue.append((child_path + "." + n, m))
                    else:
                        named_modules_queue.append((n, m))
                continue

            # Determine the pooling state for all sharding_options using this
            # (child_module, child_path). With this optimization, we change enumerate()
            # from being O(N^2) with respect to the number of tables to O(N). The
            # previous quadratic behavior is because in populate_estimates() invoked below, each
            # sharding_option needs to determine its pooling state, which is does via
            # an expensive O(N) walk through the list of embedding tables. With this
            # change sharding_option.is_pooled becomes O(1).
            is_pooled = ShardingOption.module_pooled(child_module, child_path)

            for name, param in sharder.shardable_parameters(child_module).items():
                (
                    input_lengths,
                    col_wise_shard_dim,
                    cache_params,
                    enforce_hbm,
                    stochastic_rounding,
                    bounds_check_mode,
                    feature_names,
                ) = _extract_constraints_for_param(self._constraints, name)

                sharding_options_per_table: List[ShardingOption] = []

                for sharding_type in self._filter_sharding_types(
                    name, sharder.sharding_types(self._compute_device)
                ):
                    for compute_kernel in self._filter_compute_kernels(
                        name,
                        sharder.compute_kernels(sharding_type, self._compute_device),
                        sharding_type,
                    ):
                        (
                            shard_sizes,
                            shard_offsets,
                        ) = calculate_shard_sizes_and_offsets(
                            tensor=param,
                            world_size=self._world_size,
                            local_world_size=self._local_world_size,
                            sharding_type=sharding_type,
                            col_wise_shard_dim=col_wise_shard_dim,
                        )
                        dependency = None
                        if isinstance(child_module, EmbeddingTower):
                            dependency = child_path
                        elif isinstance(child_module, EmbeddingTowerCollection):
                            tower_index = _get_tower_index(name, child_module)
                            dependency = child_path + ".tower_" + str(tower_index)
                        sharding_options_per_table.append(
                            ShardingOption(
                                name=name,
                                tensor=param,
                                module=(child_path, child_module),
                                input_lengths=input_lengths,
                                batch_size=self._batch_size,
                                compute_kernel=compute_kernel,
                                sharding_type=sharding_type,
                                partition_by=get_partition_by_type(sharding_type),
                                shards=[
                                    Shard(size=size, offset=offset)
                                    for size, offset in zip(shard_sizes, shard_offsets)
                                ],
                                cache_params=cache_params,
                                enforce_hbm=enforce_hbm,
                                stochastic_rounding=stochastic_rounding,
                                bounds_check_mode=bounds_check_mode,
                                dependency=dependency,
                                is_pooled=is_pooled,
                                feature_names=feature_names,
                            )
                        )
                if not sharding_options_per_table:
                    raise RuntimeError(
                        "No available sharding type and compute kernel combination "
                        f"after applying user provided constraints for {name}. "
                        f"Module: {sharder_key}, sharder: {sharder.__class__.__name__}, compute device: {self._compute_device}. "
                        f"To debug, search above for warning logs about no available sharding types/compute kernels for table: {name}"
                    )

                sharding_options.extend(sharding_options_per_table)

        self.populate_estimates(sharding_options)

        return sharding_options

    def populate_estimates(self, sharding_options: List[ShardingOption]) -> None:
        for estimator in self._estimators:
            estimator.estimate(sharding_options, self._sharder_map)

    def _filter_sharding_types(
        self, name: str, allowed_sharding_types: List[str]
    ) -> List[str]:
        if not self._constraints or not self._constraints.get(name):
            return allowed_sharding_types
        constraints: ParameterConstraints = self._constraints[name]
        if not constraints.sharding_types:
            return allowed_sharding_types
        constrained_sharding_types: List[str] = constraints.sharding_types

        filtered_sharding_types = list(
            set(constrained_sharding_types) & set(allowed_sharding_types)
        )

        if not filtered_sharding_types:
            logger.warn(
                "No available sharding types after applying user provided "
                f"constraints for {name}. Constrained sharding types: "
                f"{constrained_sharding_types}, allowed sharding types: "
                f"{allowed_sharding_types}, filtered sharding types: "
                f"{filtered_sharding_types}. Please check if the constrained "
                "sharding types are too restrictive, if the sharder allows the "
                "sharding types, or if non-strings are passed in."
            )
        return filtered_sharding_types

    def _filter_compute_kernels(
        self,
        name: str,
        allowed_compute_kernels: List[str],
        sharding_type: str,
    ) -> List[str]:
        # for the log message only
        constrained_compute_kernels: List[str] = [
            compute_kernel.value for compute_kernel in EmbeddingComputeKernel
        ]
        if not self._constraints or not self._constraints.get(name):
            filtered_compute_kernels = allowed_compute_kernels
        else:
            constraints: ParameterConstraints = self._constraints[name]
            if not constraints.compute_kernels:
                filtered_compute_kernels = allowed_compute_kernels
            else:
                constrained_compute_kernels = constraints.compute_kernels
                filtered_compute_kernels = list(
                    set(constrained_compute_kernels) & set(allowed_compute_kernels)
                )

        if EmbeddingComputeKernel.DENSE.value in filtered_compute_kernels:
            if (
                EmbeddingComputeKernel.FUSED.value in filtered_compute_kernels
            ):  # always false for data_parallel
                filtered_compute_kernels.remove(EmbeddingComputeKernel.DENSE.value)

        if not filtered_compute_kernels:
            logger.warn(
                "No available compute kernels after applying user provided "
                f"constraints for {name}. Constrained compute kernels: "
                f"{constrained_compute_kernels}, allowed compute kernels: "
                f"{allowed_compute_kernels}, filtered compute kernels: "
                f"{filtered_compute_kernels}, sharding type: {sharding_type}. Please check if the constrained "
                "compute kernels are too restrictive, if the sharder allows the "
                "compute kernels, or if non-strings are passed in."
            )
        return filtered_compute_kernels


def _extract_constraints_for_param(
    constraints: Optional[Dict[str, ParameterConstraints]], name: str
) -> Tuple[
    List[float],
    Optional[int],
    Optional[CacheParams],
    Optional[bool],
    Optional[bool],
    Optional[BoundsCheckMode],
    Optional[List[str]],
]:
    input_lengths = [POOLING_FACTOR]
    col_wise_shard_dim = None
    cache_params = None
    enforce_hbm = None
    stochastic_rounding = None
    bounds_check_mode = None
    feature_names = None

    if constraints and constraints.get(name):
        input_lengths = constraints[name].pooling_factors
        col_wise_shard_dim = constraints[name].min_partition
        cache_params = constraints[name].cache_params
        enforce_hbm = constraints[name].enforce_hbm
        stochastic_rounding = constraints[name].stochastic_rounding
        bounds_check_mode = constraints[name].bounds_check_mode
        feature_names = constraints[name].feature_names

    return (
        input_lengths,
        col_wise_shard_dim,
        cache_params,
        enforce_hbm,
        stochastic_rounding,
        bounds_check_mode,
        feature_names,
    )


def get_partition_by_type(sharding_type: str) -> str:
    """
    Gets corresponding partition by type for provided sharding type.

    Args:
        sharding_type (str): sharding type string.

    Returns:
        str: the corresponding `PartitionByType` value.
    """

    device_sharding_types = {
        ShardingType.TABLE_WISE.value,
        ShardingType.COLUMN_WISE.value,
    }
    host_sharding_types = {
        ShardingType.TABLE_ROW_WISE.value,
        ShardingType.TABLE_COLUMN_WISE.value,
    }
    uniform_sharding_types = {
        ShardingType.ROW_WISE.value,
        ShardingType.DATA_PARALLEL.value,
    }

    if sharding_type in device_sharding_types:
        return PartitionByType.DEVICE.value
    elif sharding_type in host_sharding_types:
        return PartitionByType.HOST.value
    elif sharding_type in uniform_sharding_types:
        return PartitionByType.UNIFORM.value

    raise ValueError(
        f"Unrecognized or unsupported sharding type provided: {sharding_type}"
    )


def _get_tower_index(name: str, child_module: EmbeddingTowerCollection) -> int:
    for i, tower in enumerate(child_module.towers):
        for n, m in tower.named_modules():
            if isinstance(m, nn.Embedding) or isinstance(m, nn.EmbeddingBag):
                table_name = n.split(".")[-1]
                if name == table_name:
                    return i
    raise RuntimeError(
        f"couldn't get the tower index for table {name}, tower collection: {child_module}"
    )
