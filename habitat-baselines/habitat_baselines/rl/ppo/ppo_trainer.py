#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import contextlib
import os
import random
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

import numpy as np
import torch
import tqdm
from gym import spaces
from torch import nn
from torch.optim.lr_scheduler import LambdaLR

from habitat import Config, VectorEnv, logger
from habitat.config import read_write
from habitat.tasks.rearrange.rearrange_sensors import GfxReplayMeasure
from habitat.tasks.rearrange.utils import write_gfx_replay
from habitat.utils import profiling_wrapper
from habitat.utils.render_wrapper import overlay_frame
from habitat.utils.visualizations.utils import observations_to_image
from habitat_baselines.common.base_trainer import BaseRLTrainer
from habitat_baselines.common.baseline_registry import baseline_registry
from habitat_baselines.common.construct_vector_env import construct_envs
from habitat_baselines.common.obs_transformers import (
    apply_obs_transforms_batch,
    apply_obs_transforms_obs_space,
    get_active_obs_transforms,
)
from habitat_baselines.common.rollout_storage import RolloutStorage
from habitat_baselines.common.tensorboard_utils import (
    TensorboardWriter,
    get_writer,
)
from habitat_baselines.rl.ddppo.algo import DDPPO
from habitat_baselines.rl.ddppo.ddp_utils import (
    EXIT,
    get_distrib_size,
    init_distrib_slurm,
    is_slurm_batch_job,
    load_resume_state,
    rank0_only,
    requeue_job,
    save_resume_state,
)
from habitat_baselines.rl.ddppo.policy import (  # noqa: F401.
    PointNavResNetNet,
    PointNavResNetPolicy,
)
from habitat_baselines.rl.hrl.hierarchical_policy import (  # noqa: F401.
    HierarchicalPolicy,
)
from habitat_baselines.rl.ppo import PPO
from habitat_baselines.rl.ppo.policy import NetPolicy
from habitat_baselines.utils.common import (
    batch_obs,
    generate_video,
    get_num_actions,
    inference_mode,
    is_continuous_action_space,
)


@baseline_registry.register_trainer(name="ddppo")
@baseline_registry.register_trainer(name="ppo")
class PPOTrainer(BaseRLTrainer):
    r"""Trainer class for PPO algorithm
    Paper: https://arxiv.org/abs/1707.06347.
    """
    supported_tasks = ["Nav-v0"]

    SHORT_ROLLOUT_THRESHOLD: float = 0.25
    _is_distributed: bool
    envs: VectorEnv
    agent: PPO
    actor_critic: NetPolicy

    def __init__(self, config=None):
        super().__init__(config)
        self.actor_critic = None
        self.agent = None
        self.envs = None
        self.obs_transforms = []

        self._static_encoder = False
        self._encoder = None
        self._obs_space = None

        # Distributed if the world size would be
        # greater than 1
        self._is_distributed = get_distrib_size()[2] > 1

    @property
    def obs_space(self):
        if self._obs_space is None and self.envs is not None:
            self._obs_space = self.envs.observation_spaces[0]

        return self._obs_space

    @obs_space.setter
    def obs_space(self, new_obs_space):
        self._obs_space = new_obs_space

    def _all_reduce(self, t: torch.Tensor) -> torch.Tensor:
        r"""All reduce helper method that moves things to the correct
        device and only runs if distributed
        """
        if not self._is_distributed:
            return t

        orig_device = t.device
        t = t.to(device=self.device)
        torch.distributed.all_reduce(t)

        return t.to(device=orig_device)

    def _setup_actor_critic_agent(self, ppo_cfg: Config) -> None:
        r"""Sets up actor critic and agent for PPO.

        Args:
            ppo_cfg: config node with relevant params

        Returns:
            None
        """
        logger.add_filehandler(self.config.habitat_baselines.log_file)

        policy = baseline_registry.get_policy(
            self.config.habitat_baselines.rl.policy.name
        )
        observation_space = self.obs_space
        self.obs_transforms = get_active_obs_transforms(self.config)
        observation_space = apply_obs_transforms_obs_space(
            observation_space, self.obs_transforms
        )

        self.actor_critic = policy.from_config(
            self.config,
            observation_space,
            self.policy_action_space,
            orig_action_space=self.orig_policy_action_space,
        )
        self.obs_space = observation_space
        print("device:", self.device)
        self.actor_critic.to(self.device)

        if (
            self.config.habitat_baselines.rl.ddppo.pretrained_encoder
            or self.config.habitat_baselines.rl.ddppo.pretrained
        ):
            pretrained_state = torch.load(
                self.config.habitat_baselines.rl.ddppo.pretrained_weights,
                map_location="cpu",
            )

        if self.config.habitat_baselines.rl.ddppo.pretrained:
            self.actor_critic.load_state_dict(
                {  # type: ignore
                    k[len("actor_critic.") :]: v
                    for k, v in pretrained_state["state_dict"].items()
                }
            )
        elif self.config.habitat_baselines.rl.ddppo.pretrained_encoder:
            prefix = "actor_critic.net.visual_encoder."
            self.actor_critic.net.visual_encoder.load_state_dict(
                {
                    k[len(prefix) :]: v
                    for k, v in pretrained_state["state_dict"].items()
                    if k.startswith(prefix)
                }
            )

        if not self.config.habitat_baselines.rl.ddppo.train_encoder:
            self._static_encoder = True
            for param in self.actor_critic.net.visual_encoder.parameters():
                param.requires_grad_(False)

        if self.config.habitat_baselines.rl.ddppo.reset_critic:
            nn.init.orthogonal_(self.actor_critic.critic.fc.weight)
            nn.init.constant_(self.actor_critic.critic.fc.bias, 0)

        self.agent = (DDPPO if self._is_distributed else PPO).from_config(
            self.actor_critic, ppo_cfg
        )

    def _init_envs(self, config=None, is_eval: bool = False):
        if config is None:
            config = self.config

        self.envs = construct_envs(
            config,
            workers_ignore_signals=is_slurm_batch_job(),
            enforce_scenes_greater_eq_environments=is_eval,
        )

    def _init_train(self, resume_state=None):
        if resume_state is None:
            resume_state = load_resume_state(self.config)

        if resume_state is not None:
            self.config: Config = resume_state["config"]

        if self.config.habitat_baselines.rl.ddppo.force_distributed:
            self._is_distributed = True

        self._add_preemption_signal_handlers()

        if self._is_distributed:
            local_rank, tcp_store = init_distrib_slurm(
                self.config.habitat_baselines.rl.ddppo.distrib_backend
            )
            if rank0_only():
                logger.info(
                    "Initialized DD-PPO with {} workers".format(
                        torch.distributed.get_world_size()
                    )
                )

            with read_write(self.config):
                self.config.habitat_baselines.torch_gpu_id = local_rank
                self.config.habitat_baselines.simulator_gpu_id = local_rank
                # Multiply by the number of simulators to make sure they also get unique seeds
                self.config.habitat.seed += (
                    torch.distributed.get_rank()
                    * self.config.habitat_baselines.num_environments
                )

            random.seed(self.config.habitat.seed)
            np.random.seed(self.config.habitat.seed)
            torch.manual_seed(self.config.habitat.seed)
            self.num_rollouts_done_store = torch.distributed.PrefixStore(
                "rollout_tracker", tcp_store
            )
            self.num_rollouts_done_store.set("num_done", "0")

        if rank0_only() and self.config.habitat_baselines.verbose:
            logger.info(f"config: {self.config}")

        profiling_wrapper.configure(
            capture_start_step=self.config.habitat_baselines.profiling.capture_start_step,
            num_steps_to_capture=self.config.habitat_baselines.profiling.num_steps_to_capture,
        )

        self._init_envs()

        action_space = self.envs.action_spaces[0]
        self.policy_action_space = action_space
        self.orig_policy_action_space = self.envs.orig_action_spaces[0]
        if is_continuous_action_space(action_space):
            # Assume ALL actions are NOT discrete
            action_shape = (get_num_actions(action_space),)
            discrete_actions = False
        else:
            # For discrete pointnav
            action_shape = (1,)
            discrete_actions = True

        ppo_cfg = self.config.habitat_baselines.rl.ppo
        if torch.cuda.is_available():
            self.device = torch.device(
                "cuda", self.config.habitat_baselines.torch_gpu_id
            )
            torch.cuda.set_device(self.device)
        else:
            self.device = torch.device("cpu")

        if rank0_only() and not os.path.isdir(
            self.config.habitat_baselines.checkpoint_folder
        ):
            os.makedirs(self.config.habitat_baselines.checkpoint_folder)

        self._setup_actor_critic_agent(ppo_cfg)
        if resume_state is not None:
            self.agent.load_state_dict(resume_state["state_dict"])
            self.agent.optimizer.load_state_dict(resume_state["optim_state"])
        if self._is_distributed:
            self.agent.init_distributed(find_unused_params=False)  # type: ignore

        logger.info(
            "agent number of parameters: {}".format(
                sum(param.numel() for param in self.agent.parameters())
            )
        )

        obs_space = self.obs_space
        if self._static_encoder:
            self._encoder = self.actor_critic.net.visual_encoder
            obs_space = spaces.Dict(
                {
                    PointNavResNetNet.PRETRAINED_VISUAL_FEATURES_KEY: spaces.Box(
                        low=np.finfo(np.float32).min,
                        high=np.finfo(np.float32).max,
                        shape=self._encoder.output_shape,
                        dtype=np.float32,
                    ),
                    **obs_space.spaces,
                }
            )

        self._nbuffers = 2 if ppo_cfg.use_double_buffered_sampler else 1

        self.rollouts = RolloutStorage(
            ppo_cfg.num_steps,
            self.envs.num_envs,
            obs_space,
            self.policy_action_space,
            ppo_cfg.hidden_size,
            num_recurrent_layers=self.actor_critic.net.num_recurrent_layers,
            is_double_buffered=ppo_cfg.use_double_buffered_sampler,
            action_shape=action_shape,
            discrete_actions=discrete_actions,
        )
        self.rollouts.to(self.device)

        observations = self.envs.reset()
        batch = batch_obs(observations, device=self.device)
        batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

        if self._static_encoder:
            with inference_mode():
                batch[
                    PointNavResNetNet.PRETRAINED_VISUAL_FEATURES_KEY
                ] = self._encoder(batch)

        self.rollouts.buffers["observations"][0] = batch  # type: ignore

        self.current_episode_reward = torch.zeros(self.envs.num_envs, 1)
        self.running_episode_stats = dict(
            count=torch.zeros(self.envs.num_envs, 1),
            reward=torch.zeros(self.envs.num_envs, 1),
        )
        self.window_episode_stats = defaultdict(
            lambda: deque(maxlen=ppo_cfg.reward_window_size)
        )

        self.env_time = 0.0
        self.pth_time = 0.0
        self.t_start = time.time()

    @rank0_only
    @profiling_wrapper.RangeContext("save_checkpoint")
    def save_checkpoint(
        self, file_name: str, extra_state: Optional[Dict] = None
    ) -> None:
        r"""Save checkpoint with specified name.

        Args:
            file_name: file name for checkpoint

        Returns:
            None
        """
        checkpoint = {
            "state_dict": self.agent.state_dict(),
            "config": self.config,
        }
        if extra_state is not None:
            checkpoint["extra_state"] = extra_state

        torch.save(
            checkpoint,
            os.path.join(
                self.config.habitat_baselines.checkpoint_folder, file_name
            ),
        )
        torch.save(
            checkpoint,
            os.path.join(
                self.config.habitat_baselines.checkpoint_folder, "latest.pth"
            ),
        )

    def load_checkpoint(self, checkpoint_path: str, *args, **kwargs) -> Dict:
        r"""Load checkpoint of specified path as a dict.

        Args:
            checkpoint_path: path of target checkpoint
            *args: additional positional args
            **kwargs: additional keyword args

        Returns:
            dict containing checkpoint info
        """
        return torch.load(checkpoint_path, *args, **kwargs)

    METRICS_BLACKLIST = {"top_down_map", "collisions.is_collision"}

    @classmethod
    def _extract_scalars_from_info(
        cls, info: Dict[str, Any]
    ) -> Dict[str, float]:
        result = {}
        for k, v in info.items():
            if not isinstance(k, str) or k in cls.METRICS_BLACKLIST:
                continue

            if isinstance(v, dict):
                result.update(
                    {
                        k + "." + subk: subv
                        for subk, subv in cls._extract_scalars_from_info(
                            v
                        ).items()
                        if isinstance(subk, str)
                        and k + "." + subk not in cls.METRICS_BLACKLIST
                    }
                )
            # Things that are scalar-like will have an np.size of 1.
            # Strings also have an np.size of 1, so explicitly ban those
            elif np.size(v) == 1 and not isinstance(v, str):
                result[k] = float(v)

        return result

    @classmethod
    def _extract_scalars_from_infos(
        cls, infos: List[Dict[str, Any]]
    ) -> Dict[str, List[float]]:

        results = defaultdict(list)
        for i in range(len(infos)):
            for k, v in cls._extract_scalars_from_info(infos[i]).items():
                results[k].append(v)

        return results

    def _compute_actions_and_step_envs(self, buffer_index: int = 0):
        num_envs = self.envs.num_envs
        env_slice = slice(
            int(buffer_index * num_envs / self._nbuffers),
            int((buffer_index + 1) * num_envs / self._nbuffers),
        )

        t_sample_action = time.time()

        # sample actions
        with inference_mode():
            step_batch = self.rollouts.buffers[
                self.rollouts.current_rollout_step_idxs[buffer_index],
                env_slice,
            ]

            profiling_wrapper.range_push("compute actions")
            (
                values,
                actions,
                actions_log_probs,
                recurrent_hidden_states,
            ) = self.actor_critic.act(
                step_batch["observations"],
                step_batch["recurrent_hidden_states"],
                step_batch["prev_actions"],
                step_batch["masks"],
            )

        self.pth_time += time.time() - t_sample_action

        profiling_wrapper.range_pop()  # compute actions

        t_step_env = time.time()

        for index_env, act in zip(
            range(env_slice.start, env_slice.stop), actions.cpu().unbind(0)
        ):
            if is_continuous_action_space(self.policy_action_space):
                # Clipping actions to the specified limits
                act = np.clip(
                    act.numpy(),
                    self.policy_action_space.low,
                    self.policy_action_space.high,
                )
            else:
                act = act.item()
            self.envs.async_step_at(index_env, act)

        self.env_time += time.time() - t_step_env

        self.rollouts.insert(
            next_recurrent_hidden_states=recurrent_hidden_states,
            actions=actions,
            action_log_probs=actions_log_probs,
            value_preds=values,
            buffer_index=buffer_index,
        )

    def _collect_environment_result(self, buffer_index: int = 0):
        num_envs = self.envs.num_envs
        env_slice = slice(
            int(buffer_index * num_envs / self._nbuffers),
            int((buffer_index + 1) * num_envs / self._nbuffers),
        )

        t_step_env = time.time()
        outputs = [
            self.envs.wait_step_at(index_env)
            for index_env in range(env_slice.start, env_slice.stop)
        ]

        observations, rewards_l, dones, infos = [
            list(x) for x in zip(*outputs)
        ]

        self.env_time += time.time() - t_step_env

        t_update_stats = time.time()
        batch = batch_obs(observations, device=self.device)
        batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

        rewards = torch.tensor(
            rewards_l,
            dtype=torch.float,
            device=self.current_episode_reward.device,
        )
        rewards = rewards.unsqueeze(1)

        not_done_masks = torch.tensor(
            [[not done] for done in dones],
            dtype=torch.bool,
            device=self.current_episode_reward.device,
        )
        done_masks = torch.logical_not(not_done_masks)

        self.current_episode_reward[env_slice] += rewards
        current_ep_reward = self.current_episode_reward[env_slice]
        self.running_episode_stats["reward"][env_slice] += current_ep_reward.where(done_masks, current_ep_reward.new_zeros(()))  # type: ignore
        self.running_episode_stats["count"][env_slice] += done_masks.float()  # type: ignore
        for k, v_k in self._extract_scalars_from_infos(infos).items():
            v = torch.tensor(
                v_k,
                dtype=torch.float,
                device=self.current_episode_reward.device,
            ).unsqueeze(1)
            if k not in self.running_episode_stats:
                self.running_episode_stats[k] = torch.zeros_like(
                    self.running_episode_stats["count"]
                )
            self.running_episode_stats[k][env_slice] += v.where(done_masks, v.new_zeros(()))  # type: ignore

        self.current_episode_reward[env_slice].masked_fill_(done_masks, 0.0)

        if self._static_encoder:
            with inference_mode():
                batch[
                    PointNavResNetNet.PRETRAINED_VISUAL_FEATURES_KEY
                ] = self._encoder(batch)

        self.rollouts.insert(
            next_observations=batch,
            rewards=rewards,
            next_masks=not_done_masks,
            buffer_index=buffer_index,
        )

        self.rollouts.advance_rollout(buffer_index)

        self.pth_time += time.time() - t_update_stats

        return env_slice.stop - env_slice.start

    @profiling_wrapper.RangeContext("_collect_rollout_step")
    def _collect_rollout_step(self):
        self._compute_actions_and_step_envs()
        return self._collect_environment_result()

    @profiling_wrapper.RangeContext("_update_agent")
    def _update_agent(self):
        ppo_cfg = self.config.habitat_baselines.rl.ppo
        t_update_model = time.time()
        with inference_mode():
            step_batch = self.rollouts.buffers[
                self.rollouts.current_rollout_step_idx
            ]

            next_value = self.actor_critic.get_value(
                step_batch["observations"],
                step_batch["recurrent_hidden_states"],
                step_batch["prev_actions"],
                step_batch["masks"],
            )

        self.rollouts.compute_returns(
            next_value, ppo_cfg.use_gae, ppo_cfg.gamma, ppo_cfg.tau
        )

        self.agent.train()

        losses = self.agent.update(self.rollouts)

        self.rollouts.after_update()
        self.pth_time += time.time() - t_update_model
        return losses

    def _coalesce_post_step(
        self, losses: Dict[str, float], count_steps_delta: int
    ) -> Dict[str, float]:
        stats_ordering = sorted(self.running_episode_stats.keys())
        stats = torch.stack(
            [self.running_episode_stats[k] for k in stats_ordering], 0
        )

        stats = self._all_reduce(stats)

        for i, k in enumerate(stats_ordering):
            self.window_episode_stats[k].append(stats[i])

        if self._is_distributed:
            loss_name_ordering = sorted(losses.keys())
            stats = torch.tensor(
                [losses[k] for k in loss_name_ordering] + [count_steps_delta],
                device="cpu",
                dtype=torch.float32,
            )
            stats = self._all_reduce(stats)
            count_steps_delta = int(stats[-1].item())
            stats /= torch.distributed.get_world_size()

            losses = {
                k: stats[i].item() for i, k in enumerate(loss_name_ordering)
            }

        if self._is_distributed and rank0_only():
            self.num_rollouts_done_store.set("num_done", "0")

        self.num_steps_done += count_steps_delta

        return losses

    @rank0_only
    def _training_log(
        self, writer, losses: Dict[str, float], prev_time: int = 0
    ):
        deltas = {
            k: (
                (v[-1] - v[0]).sum().item()
                if len(v) > 1
                else v[0].sum().item()
            )
            for k, v in self.window_episode_stats.items()
        }
        deltas["count"] = max(deltas["count"], 1.0)

        writer.add_scalar(
            "reward",
            deltas["reward"] / deltas["count"],
            self.num_steps_done,
        )

        # Check to see if there are any metrics
        # that haven't been logged yet
        metrics = {
            k: v / deltas["count"]
            for k, v in deltas.items()
            if k not in {"reward", "count"}
        }

        for k, v in metrics.items():
            writer.add_scalar(f"metrics/{k}", v, self.num_steps_done)
        for k, v in losses.items():
            writer.add_scalar(f"learner/{k}", v, self.num_steps_done)

        fps = self.num_steps_done / ((time.time() - self.t_start) + prev_time)
        writer.add_scalar("perf/fps", fps, self.num_steps_done)

        # log stats
        if (
            self.num_updates_done % self.config.habitat_baselines.log_interval
            == 0
        ):
            logger.info(
                "update: {}\tfps: {:.3f}\t".format(
                    self.num_updates_done,
                    fps,
                )
            )

            logger.info(
                "update: {}\tenv-time: {:.3f}s\tpth-time: {:.3f}s\t"
                "frames: {}".format(
                    self.num_updates_done,
                    self.env_time,
                    self.pth_time,
                    self.num_steps_done,
                )
            )

            logger.info(
                "Average window size: {}  {}".format(
                    len(self.window_episode_stats["count"]),
                    "  ".join(
                        "{}: {:.3f}".format(k, v / deltas["count"])
                        for k, v in deltas.items()
                        if k != "count"
                    ),
                )
            )

    def should_end_early(self, rollout_step) -> bool:
        if not self._is_distributed:
            return False
        # This is where the preemption of workers happens.  If a
        # worker detects it will be a straggler, it preempts itself!
        return (
            rollout_step
            >= self.config.habitat_baselines.rl.ppo.num_steps
            * self.SHORT_ROLLOUT_THRESHOLD
        ) and int(self.num_rollouts_done_store.get("num_done")) >= (
            self.config.habitat_baselines.rl.ddppo.sync_frac
            * torch.distributed.get_world_size()
        )

    @profiling_wrapper.RangeContext("train")
    def train(self) -> None:
        r"""Main method for training DD/PPO.

        Returns:
            None
        """

        resume_state = load_resume_state(self.config)
        self._init_train(resume_state)

        count_checkpoints = 0
        prev_time = 0

        lr_scheduler = LambdaLR(
            optimizer=self.agent.optimizer,
            lr_lambda=lambda x: 1 - self.percent_done(),
        )

        if self._is_distributed:
            torch.distributed.barrier()

        if resume_state is not None:
            self.agent.load_state_dict(resume_state["state_dict"])
            self.agent.optimizer.load_state_dict(resume_state["optim_state"])
            lr_scheduler.load_state_dict(resume_state["lr_sched_state"])

            requeue_stats = resume_state["requeue_stats"]
            self.env_time = requeue_stats["env_time"]
            self.pth_time = requeue_stats["pth_time"]
            self.num_steps_done = requeue_stats["num_steps_done"]
            self.num_updates_done = requeue_stats["num_updates_done"]
            self._last_checkpoint_percent = requeue_stats[
                "_last_checkpoint_percent"
            ]
            count_checkpoints = requeue_stats["count_checkpoints"]
            prev_time = requeue_stats["prev_time"]

            self.running_episode_stats = requeue_stats["running_episode_stats"]
            self.window_episode_stats.update(
                requeue_stats["window_episode_stats"]
            )

        ppo_cfg = self.config.habitat_baselines.rl.ppo

        with (
            get_writer(
                self.config,
                flush_secs=self.flush_secs,
                purge_step=int(self.num_steps_done),
            )
            if rank0_only()
            else contextlib.suppress()
        ) as writer:
            while not self.is_done():
                profiling_wrapper.on_start_step()
                profiling_wrapper.range_push("train update")

                if ppo_cfg.use_linear_clip_decay:
                    self.agent.clip_param = ppo_cfg.clip_param * (
                        1 - self.percent_done()
                    )

                if rank0_only() and self._should_save_resume_state():
                    requeue_stats = dict(
                        env_time=self.env_time,
                        pth_time=self.pth_time,
                        count_checkpoints=count_checkpoints,
                        num_steps_done=self.num_steps_done,
                        num_updates_done=self.num_updates_done,
                        _last_checkpoint_percent=self._last_checkpoint_percent,
                        prev_time=(time.time() - self.t_start) + prev_time,
                        running_episode_stats=self.running_episode_stats,
                        window_episode_stats=dict(self.window_episode_stats),
                    )

                    save_resume_state(
                        dict(
                            state_dict=self.agent.state_dict(),
                            optim_state=self.agent.optimizer.state_dict(),
                            lr_sched_state=lr_scheduler.state_dict(),
                            config=self.config,
                            requeue_stats=requeue_stats,
                        ),
                        self.config,
                    )

                if EXIT.is_set():
                    profiling_wrapper.range_pop()  # train update

                    self.envs.close()

                    requeue_job()

                    return

                self.agent.eval()
                count_steps_delta = 0
                profiling_wrapper.range_push("rollouts loop")

                profiling_wrapper.range_push("_collect_rollout_step")
                for buffer_index in range(self._nbuffers):
                    self._compute_actions_and_step_envs(buffer_index)

                for step in range(ppo_cfg.num_steps):
                    is_last_step = (
                        self.should_end_early(step + 1)
                        or (step + 1) == ppo_cfg.num_steps
                    )

                    for buffer_index in range(self._nbuffers):
                        count_steps_delta += self._collect_environment_result(
                            buffer_index
                        )

                        if (buffer_index + 1) == self._nbuffers:
                            profiling_wrapper.range_pop()  # _collect_rollout_step

                        if not is_last_step:
                            if (buffer_index + 1) == self._nbuffers:
                                profiling_wrapper.range_push(
                                    "_collect_rollout_step"
                                )

                            self._compute_actions_and_step_envs(buffer_index)

                    if is_last_step:
                        break

                profiling_wrapper.range_pop()  # rollouts loop

                if self._is_distributed:
                    self.num_rollouts_done_store.add("num_done", 1)

                losses = self._update_agent()

                if ppo_cfg.use_linear_lr_decay:
                    lr_scheduler.step()  # type: ignore

                self.num_updates_done += 1
                losses = self._coalesce_post_step(
                    losses,
                    count_steps_delta,
                )

                self._training_log(writer, losses, prev_time)

                # checkpoint model
                if rank0_only() and self.should_checkpoint():
                    self.save_checkpoint(
                        f"ckpt.{count_checkpoints}.pth",
                        dict(
                            step=self.num_steps_done,
                            wall_time=(time.time() - self.t_start) + prev_time,
                        ),
                    )
                    count_checkpoints += 1

                profiling_wrapper.range_pop()  # train update

            self.envs.close()

    def _eval_checkpoint(
        self,
        checkpoint_path: str,
        writer: TensorboardWriter,
        checkpoint_index: int = 0,
    ) -> None:
        r"""Evaluates a single checkpoint.

        Args:
            checkpoint_path: path of checkpoint
            writer: tensorboard writer object for logging to tensorboard
            checkpoint_index: index of cur checkpoint for logging

        Returns:
            None
        """
        if self._is_distributed:
            raise RuntimeError("Evaluation does not support distributed mode")

        # Map location CPU is almost always better than mapping to a CUDA device.
        if self.config.habitat_baselines.eval.should_load_ckpt:
            ckpt_dict = self.load_checkpoint(
                checkpoint_path, map_location="cpu"
            )
            step_id = ckpt_dict["extra_state"]["step"]
            print(step_id)
        else:
            ckpt_dict = {}

        if self.config.habitat_baselines.eval.use_ckpt_config:
            config = self._setup_eval_config(ckpt_dict["config"])
        else:
            config = self.config.clone()

        ppo_cfg = config.habitat_baselines.rl.ppo

        with read_write(config):
            config.habitat.dataset.split = config.habitat_baselines.eval.split

        if (
            len(self.config.habitat_baselines.video_option) > 0
            and self.config.habitat_baselines.video_render_top_down
        ):
            with read_write(config):
                config.habitat.task.measurements.append("top_down_map")
                config.habitat.task.measurements.append("collisions")

        if (
            len(config.habitat_baselines.video_render_views) > 0
            and len(self.config.habitat_baselines.video_option) > 0
        ):
            with read_write(config):
                for render_view in config.habitat_baselines.video_render_views:
                    uuid = config.habitat.simulator[render_view].uuid
                    config.habitat.gym.obs_keys.append(uuid)
                    config.habitat_baselines.sensors.append(render_view)

        # whether we print the config
        print(config.habitat_baselines.verbose)
        if config.habitat_baselines.verbose:
            logger.info(f"env config: {config}")
        else:
            print("we do not print all the config, if you want to print it,",
                  "please set config.habitat_baselines.verbose == True")

        self._init_envs(config, is_eval=True)
        print('#'*5, 'task config path:',
              config.habitat_baselines.base_task_config_path, '#'*5)

        action_space = self.envs.action_spaces[0]
        print('#'*10, 'action space is', action_space, '#'*10)
        self.policy_action_space = action_space
        self.orig_policy_action_space = self.envs.orig_action_spaces[0]
        if is_continuous_action_space(action_space):
            # Assume NONE of the actions are discrete
            action_shape = (get_num_actions(action_space),)
            discrete_actions = False
            print("we use continuous action space")
        else:
            # For discrete pointnav
            action_shape = (1,)
            discrete_actions = True
            print("we use discrete action space")

        self._setup_actor_critic_agent(ppo_cfg)

        if self.agent.actor_critic.should_load_agent_state:
            self.agent.load_state_dict(ckpt_dict["state_dict"])
        self.actor_critic = self.agent.actor_critic

        observations = self.envs.reset()
        batch = batch_obs(observations, device=self.device)
        batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

        current_episode_reward = torch.zeros(
            self.envs.num_envs, 1, device="cpu"
        )

        test_recurrent_hidden_states = torch.zeros(
            self.config.habitat_baselines.num_environments,
            self.actor_critic.num_recurrent_layers,
            ppo_cfg.hidden_size,
            device=self.device,
        )
        prev_actions = torch.zeros(
            self.config.habitat_baselines.num_environments,
            *action_shape,
            device=self.device,
            dtype=torch.long if discrete_actions else torch.float,
        )
        not_done_masks = torch.zeros(
            self.config.habitat_baselines.num_environments,
            1,
            device=self.device,
            dtype=torch.bool,
        )
        stats_episodes: Dict[
            Any, Any
        ] = {}  # dict of dicts that stores stats per episode
        ep_eval_count: Dict[Any, int] = defaultdict(lambda: 0)

        rgb_frames = [
            [] for _ in range(self.config.habitat_baselines.num_environments)
        ]  # type: List[List[np.ndarray]]
        if len(self.config.habitat_baselines.video_option) > 0:
            os.makedirs(self.config.habitat_baselines.video_dir, exist_ok=True)

        # number of episodes we run
        number_of_eval_episodes = (
            self.config.habitat_baselines.test_episode_count
        )
        # default: 1
        evals_per_ep = self.config.habitat_baselines.eval.evals_per_ep
        if number_of_eval_episodes == -1:
            number_of_eval_episodes = sum(self.envs.number_of_episodes)
        else:
            total_num_eps = sum(self.envs.number_of_episodes)
            # if total_num_eps is negative,
            # it means the number of evaluation episodes is unknown
            if total_num_eps < number_of_eval_episodes and total_num_eps > 1:
                logger.warn(
                    f"Config specified {number_of_eval_episodes} eval episodes"
                    ", dataset only has {total_num_eps}."
                )
                logger.warn(f"Evaluating with {total_num_eps} instead.")
                number_of_eval_episodes = total_num_eps
            else:
                assert evals_per_ep == 1
        assert (
            number_of_eval_episodes > 0
        ), "You must specify a number of evaluation episodes " \
           "with test_episode_count"

        print("#"*5, "Now we complete all prior work and start to evaluate",
              "#"*5)
        print("#"*5, "We will run", number_of_eval_episodes, "trajectories",
              "#"*5)

        # pbar = tqdm.tqdm(total=number_of_eval_episodes * evals_per_ep)
        self.actor_critic.eval()
        while (
            len(stats_episodes) < (number_of_eval_episodes * evals_per_ep)
            and self.envs.num_envs > 0
        ):
            current_episodes_info = self.envs.current_episodes()

            # a function like torch.no_grad in torch>=1.10
            with inference_mode():
                (
                    _,
                    actions,
                    _,
                    test_recurrent_hidden_states,
                ) = self.actor_critic.act(
                    batch,
                    test_recurrent_hidden_states,
                    prev_actions,
                    not_done_masks,
                    deterministic=False,
                )

                prev_actions.copy_(actions)  # type: ignore
            # NB: Move actions to CPU.  If CUDA tensors are
            # sent in to env.step(), that will create CUDA contexts
            # in the subprocesses.
            if is_continuous_action_space(self.policy_action_space):
                # Clipping actions to the specified limits
                step_data = [
                    np.clip(
                        a.numpy(),
                        self.policy_action_space.low,
                        self.policy_action_space.high,
                    )
                    for a in actions.cpu()
                ]
            else:
                step_data = [a.item() for a in actions.cpu()]

            outputs = self.envs.step(step_data)

            observations, rewards_l, dones, infos = [
                list(x) for x in zip(*outputs)
            ]
            batch = batch_obs(  # type: ignore
                observations,
                device=self.device,
            )
            batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

            not_done_masks = torch.tensor(
                [[not done] for done in dones],
                dtype=torch.bool,
                device="cpu",
            )

            rewards = torch.tensor(
                rewards_l, dtype=torch.float, device="cpu"
            ).unsqueeze(1)
            current_episode_reward += rewards
            next_episodes_info = self.envs.current_episodes()
            envs_to_pause = []
            n_envs = self.envs.num_envs
            for i in range(n_envs):
                if (
                    ep_eval_count[
                        (
                            next_episodes_info[i].scene_id,
                            next_episodes_info[i].episode_id,
                        )
                    ]
                    == evals_per_ep
                ):
                    envs_to_pause.append(i)

                if len(self.config.habitat_baselines.video_option) > 0:
                    # TODO move normalization / channel changing out of the
                    #  policy and undo it here
                    frame = observations_to_image(
                        {k: v[i] for k, v in batch.items()}, infos[i]
                    )
                    if not not_done_masks[i].item():
                        # The last frame corresponds to the first frame of the next episode
                        # but the info is correct. So we use a black frame
                        frame = observations_to_image(
                            {k: v[i] * 0.0 for k, v in batch.items()}, infos[i]
                        )
                    if self.config.habitat_baselines.video_render_all_info:
                        frame = overlay_frame(frame, infos[i])
                    rgb_frames[i].append(frame)

                # episode ended
                if not not_done_masks[i].item():
                    print("*"*5, "Now we have run episode:",
                          len(stats_episodes), "*"*5)
                    # pbar.update()
                    episode_stats = {
                        "reward": current_episode_reward[i].item()
                    }
                    episode_stats.update(
                        self._extract_scalars_from_info(infos[i])
                    )
                    current_episode_reward[i] = 0
                    k = (
                        current_episodes_info[i].scene_id,
                        current_episodes_info[i].episode_id,
                    )
                    ep_eval_count[k] += 1
                    # use scene_id + episode_id as unique id for storing stats
                    stats_episodes[(k, ep_eval_count[k])] = episode_stats

                    if len(self.config.habitat_baselines.video_option) > 0:
                        print("Now we generate videos")
                        generate_video(
                            video_option=self.config.habitat_baselines.video_option,
                            video_dir=self.config.habitat_baselines.video_dir,
                            images=rgb_frames[i],
                            episode_id=current_episodes_info[i].episode_id,
                            checkpoint_idx=checkpoint_index,
                            metrics=self._extract_scalars_from_info(infos[i]),
                            fps=self.config.habitat_baselines.video_fps,
                            tb_writer=writer,
                            keys_to_include_in_name=self.config.habitat_baselines.eval_keys_to_include_in_name,
                        )

                        rgb_frames[i] = []

                    gfx_str = infos[i].get(GfxReplayMeasure.cls_uuid, "")
                    if gfx_str != "":
                        write_gfx_replay(
                            gfx_str,
                            self.config.habitat.task,
                            current_episodes_info[i].episode_id,
                        )

            not_done_masks = not_done_masks.to(device=self.device)
            (
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            ) = self._pause_envs(
                envs_to_pause,
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            )

        # pbar.close()
        assert (
            len(ep_eval_count) >= number_of_eval_episodes
        ), f"Expected {number_of_eval_episodes} episodes, got {len(ep_eval_count)}."

        print("#"*5, "Current we will print the results", "#"*5)
        aggregated_stats = {}
        for stat_key in next(iter(stats_episodes.values())).keys():
            aggregated_stats[stat_key] = np.mean(
                [v[stat_key] for v in stats_episodes.values()]
            )

        for k, v in aggregated_stats.items():
            logger.info(f"Average episode {k}: {v:.4f}")
        print("#" * 5, "There are all the results", "#" * 5)

        step_id = checkpoint_index
        if "extra_state" in ckpt_dict and "step" in ckpt_dict["extra_state"]:
            step_id = ckpt_dict["extra_state"]["step"]

        writer.add_scalar(
            "eval_reward/average_reward", aggregated_stats["reward"], step_id
        )

        metrics = {k: v for k, v in aggregated_stats.items() if k != "reward"}
        for k, v in metrics.items():
            writer.add_scalar(f"eval_metrics/{k}", v, step_id)

        self.envs.close()

    def _eval_attacked_checkpoint(
        self,
        checkpoint_path: str,
        writer: TensorboardWriter,
        checkpoint_index: int = 0,
        evaluate_strategy: int = 1,
    ) -> None:
        if self._is_distributed:
            raise RuntimeError("Evaluation does not support distributed mode")

        # Map location CPU is almost always better
        # than mapping to a CUDA device.
        if self.config.habitat_baselines.eval.should_load_ckpt:
            ckpt_dict = self.load_checkpoint(
                checkpoint_path, map_location="cpu"
            )
            step_id = ckpt_dict["extra_state"]["step"]
            print(step_id)
        else:
            ckpt_dict = {}

        if self.config.habitat_baselines.eval.use_ckpt_config:
            config = self._setup_eval_config(ckpt_dict["config"])
        else:
            config = self.config.clone()

        ppo_cfg = config.habitat_baselines.rl.ppo

        with read_write(config):
            config.habitat.dataset.split = config.habitat_baselines.eval.split

        if (
            len(self.config.habitat_baselines.video_option) > 0
            and self.config.habitat_baselines.video_render_top_down
        ):
            with read_write(config):
                config.habitat.task.measurements.append("top_down_map")
                config.habitat.task.measurements.append("collisions")

        if (
            len(config.habitat_baselines.video_render_views) > 0
            and len(self.config.habitat_baselines.video_option) > 0
        ):
            with read_write(config):
                for render_view in config.habitat_baselines.video_render_views:
                    uuid = config.habitat.simulator[render_view].uuid
                    config.habitat.gym.obs_keys.append(uuid)
                    config.habitat_baselines.sensors.append(render_view)

        # whether we print the config
        print(config.habitat_baselines.verbose)
        if config.habitat_baselines.verbose:
            logger.info(f"env config: {config}")
        else:
            print("we do not print all the config, if you want to print it,",
                  "please set config.habitat_baselines.verbose == True")

        self._init_envs(config, is_eval=True)
        print('#'*5, 'task config path:',
              config.habitat_baselines.base_task_config_path, '#'*5)

        action_space = self.envs.action_spaces[0]
        print('#'*5, 'action space is', action_space, '#'*5)
        self.policy_action_space = action_space
        self.orig_policy_action_space = self.envs.orig_action_spaces[0]
        if is_continuous_action_space(action_space):
            # Assume NONE of the actions are discrete
            action_shape = (get_num_actions(action_space),)
            discrete_actions = False
            print("we use continuous action space")
        else:
            # For discrete pointnav
            action_shape = (1,)
            discrete_actions = True
            print("we use discrete action space")

        self._setup_actor_critic_agent(ppo_cfg)

        if self.agent.actor_critic.should_load_agent_state:
            self.agent.load_state_dict(ckpt_dict["state_dict"])
        self.actor_critic = self.agent.actor_critic

        observations = self.envs.reset()
        batch = batch_obs(observations, device=self.device)
        batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

        current_episode_reward = torch.zeros(
            self.envs.num_envs, 1, device="cpu"
        )

        test_recurrent_hidden_states = torch.zeros(
            self.config.habitat_baselines.num_environments,
            self.actor_critic.num_recurrent_layers,
            ppo_cfg.hidden_size,
            device=self.device,
        )
        prev_actions = torch.zeros(
            self.config.habitat_baselines.num_environments,
            *action_shape,
            device=self.device,
            dtype=torch.long if discrete_actions else torch.float,
        )
        not_done_masks = torch.zeros(
            self.config.habitat_baselines.num_environments,
            1,
            device=self.device,
            dtype=torch.bool,
        )
        stats_episodes: Dict[
            Any, Any
        ] = {}  # dict of dicts that stores stats per episode
        ep_eval_count: Dict[Any, int] = defaultdict(lambda: 0)

        rgb_frames = [
            [] for _ in range(self.config.habitat_baselines.num_environments)
        ]  # type: List[List[np.ndarray]]
        if len(self.config.habitat_baselines.video_option) > 0:
            os.makedirs(self.config.habitat_baselines.video_dir, exist_ok=True)

        # number of episodes we run
        number_of_eval_episodes = (
            self.config.habitat_baselines.test_episode_count
        )
        # default: 1
        evals_per_ep = self.config.habitat_baselines.eval.evals_per_ep
        if number_of_eval_episodes == -1:
            number_of_eval_episodes = sum(self.envs.number_of_episodes)
        else:
            total_num_eps = sum(self.envs.number_of_episodes)
            # if total_num_eps is negative,
            # it means the number of evaluation episodes is unknown
            if total_num_eps < number_of_eval_episodes and total_num_eps > 1:
                logger.warn(
                    f"Config specified {number_of_eval_episodes} eval episodes"
                    ", dataset only has {total_num_eps}."
                )
                logger.warn(f"Evaluating with {total_num_eps} instead.")
                number_of_eval_episodes = total_num_eps
            else:
                assert evals_per_ep == 1
        assert (
            number_of_eval_episodes > 0
        ), "You must specify a number of evaluation episodes " \
           "with test_episode_count"

        print("#" * 5, "Now we complete all prior work and start to evaluate",
              "#" * 5)

        print("#" * 10, "Now we calculate the noise", "#" * 10)
        update_num = self.config.habitat_baselines.eval.update_num
        traj_num_each = self.config.habitat_baselines.eval.traj_num_each
        attack_trajectory_number = update_num * traj_num_each
        print("#" * 5, "We sample", attack_trajectory_number,
              "trajectories for attacking", "#" * 5)
        # the class RunningMeanAndVar in /ddppo/policy/running_mean_and_var
        # may change the parameters when forward
        # thus we need to set the attacked to be True for calculating its
        # gradient
        self.actor_critic.net.visual_encoder.running_mean_and_var.attacked \
            = True

        print("#"*5, "we first initialize consistent noises", "#"*5)
        CA_noises = {}
        CA_grad = {}
        for key, values in batch.items():
            # CA_noises[key] = torch.rand(values.shape, dtype=torch.float,
            #                             device=values.device)
            # CA_noises[key] = CA_noises[key] * 10000
            CA_noises[key] = torch.zeros(values.shape, dtype=torch.float,
                                         device=values.device)
            CA_grad[key] = torch.zeros(values.shape, dtype=torch.float,
                                       device=values.device)

        eta = self.config.habitat_baselines.eval.eta
        gamma_ = 0.99
        print("our eta is:", eta)
        if evaluate_strategy == 0:
            print("We donot apply noises")
        elif evaluate_strategy == 1:
            print("We use UAP attack")
            # pbar = tqdm.tqdm(total=number_of_eval_episodes * evals_per_ep)
            # set model to eval, and we can not calculate the grad when in eval
            # self.actor_critic.eval()
            while (
                len(stats_episodes) < (attack_trajectory_number * evals_per_ep)
                and self.envs.num_envs > 0
            ):
                current_episodes_info = self.envs.current_episodes()

                for key, value in batch.items():
                    # print("key:", key)
                    # if key == "pointgoal_with_gps_compass":
                    # if key == "depth" or key == "pointgoal_with_gps_compass":
                    #     print("lalala")
                    # print(key, ":", value.float())
                    batch[key] = value.float().clone().\
                        detach().requires_grad_()
                test_recurrent_hidden_states = test_recurrent_hidden_states.\
                    clone().detach().requires_grad_()
                self.actor_critic.zero_grad()
                (
                    current_value_,
                    actions,
                    current_log_prob_,
                    test_recurrent_hidden_states,
                ) = self.actor_critic.act(
                    batch,
                    test_recurrent_hidden_states,
                    prev_actions,
                    not_done_masks,
                    deterministic=False,
                )

                prev_actions.copy_(actions)  # type: ignore

                # NB: Move actions to CPU.  If CUDA tensors are
                # sent in to env.step(), that will create CUDA contexts
                # in the subprocesses.
                if is_continuous_action_space(self.policy_action_space):
                    # Clipping actions to the specified limits
                    step_data = [
                        np.clip(
                            a.numpy(),
                            self.policy_action_space.low,
                            self.policy_action_space.high,
                        )
                        for a in actions.cpu()
                    ]
                else:
                    step_data = [a.item() for a in actions.cpu()]

                # in Gibson, action space is discrete(4), i.e., 0,1,2,3
                # print("action:", step_data)
                # print("value:", current_value_)
                # print("log prob:", current_log_prob_)
                # print("prob:", torch.exp(current_log_prob_))
                loss_ = torch.exp(current_log_prob_)
                # loss_ = current_log_prob_
                loss_.backward()
                # print("a", batch["pointgoal_with_gps_compass"].grad)
                # print("b", batch["depth"].grad)
                for key, value in batch.items():
                    CA_noises[key] = CA_noises[key] - value.grad
                #     print(key, value.grad)

                outputs = self.envs.step(step_data)

                observations, rewards_l, dones, infos = [
                    list(x) for x in zip(*outputs)
                ]
                batch = batch_obs(  # type: ignore
                    observations,
                    device=self.device,
                )
                batch = apply_obs_transforms_batch(batch, self.obs_transforms)

                not_done_masks = torch.tensor(
                    [[not done] for done in dones],
                    dtype=torch.bool,
                    device="cpu",
                )

                rewards = torch.tensor(
                    rewards_l, dtype=torch.float, device="cpu"
                ).unsqueeze(1)
                current_episode_reward += rewards
                next_episodes_info = self.envs.current_episodes()
                envs_to_pause = []
                n_envs = self.envs.num_envs
                for i in range(n_envs):
                    if (
                        ep_eval_count[
                            (
                                next_episodes_info[i].scene_id,
                                next_episodes_info[i].episode_id,
                            )
                        ]
                        == evals_per_ep
                    ):
                        envs_to_pause.append(i)

                    # episode ended
                    if not not_done_masks[i].item():
                        print("*"*5, "Now we have run episode:",
                              len(stats_episodes), "*"*5)
                        # pbar.update()
                        episode_stats = {
                            "reward": current_episode_reward[i].item()
                        }
                        episode_stats.update(
                            self._extract_scalars_from_info(infos[i])
                        )
                        current_episode_reward[i] = 0
                        k = (
                            current_episodes_info[i].scene_id,
                            current_episodes_info[i].episode_id,
                        )
                        ep_eval_count[k] += 1
                        # use scene_id + episode_id as unique id for storing stats
                        stats_episodes[(k, ep_eval_count[k])] = episode_stats
                        print(episode_stats)

                not_done_masks = not_done_masks.to(device=self.device)
                (
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                ) = self._pause_envs(
                    envs_to_pause,
                    self.envs,
                    test_recurrent_hidden_states,
                    not_done_masks,
                    current_episode_reward,
                    prev_actions,
                    batch,
                    rgb_frames,
                )

            for key, value in CA_noises.items():
                print("norm in consistent attack", key, ":", torch.norm(value))
                print(CA_noises[key].shape)
                print(len(CA_noises[key].shape))
                print(value)
                if torch.norm(value) == 0:
                    continue
                if key == "rgb" or key == "rgbd":
                    print("shape of", key, ":", value.shape)
                    CA_noises[key] = value / torch.norm(value) * \
                                     eta * 255 * value.shape[3] * \
                                     value.shape[2]
                elif key == "depth":
                    print("shape of depth:", value.shape)
                    CA_noises[key] = value / torch.norm(value) * eta * \
                                     value.shape[3] * value.shape[2]
                else:
                    CA_noises[key] = value / torch.norm(value) * eta
        elif evaluate_strategy == 2:
            print("We use Reward UAP attack")
            # pbar = tqdm.tqdm(total=number_of_eval_episodes * evals_per_ep)
            # set model to eval, and we can not calculate the grad when in eval
            # self.actor_critic.eval()

            alpha_ = eta / update_num
            for attack_index in range(update_num):
                print("#"*5, "This is update time:", attack_index+1, "#"*5)

                for key, values in CA_grad.items():
                    CA_grad[key] = torch.zeros(values.shape, dtype=torch.float,
                                               device=values.device)

                batch = batch_obs(observations, device=self.device)
                batch = apply_obs_transforms_batch(batch,
                                                   self.obs_transforms)  # type: ignore

                current_episode_reward = torch.zeros(
                    self.envs.num_envs, 1, device="cpu"
                )

                test_recurrent_hidden_states = torch.zeros(
                    self.config.habitat_baselines.num_environments,
                    self.actor_critic.num_recurrent_layers,
                    ppo_cfg.hidden_size,
                    device=self.device,
                )
                prev_actions = torch.zeros(
                    self.config.habitat_baselines.num_environments,
                    *action_shape,
                    device=self.device,
                    dtype=torch.long if discrete_actions else torch.float,
                )
                not_done_masks = torch.zeros(
                    self.config.habitat_baselines.num_environments,
                    1,
                    device=self.device,
                    dtype=torch.bool,
                )
                stats_episodes: Dict[
                    Any, Any
                ] = {}  # dict of dicts that stores stats per episode
                ep_eval_count: Dict[Any, int] = defaultdict(lambda: 0)

                rgb_frames = [
                    [] for _ in
                    range(self.config.habitat_baselines.num_environments)
                ]  # type: List[List[np.ndarray]]

                while (
                    len(stats_episodes) < (traj_num_each * evals_per_ep)
                    and self.envs.num_envs > 0
                ):
                    current_episodes_info = self.envs.current_episodes()

                    for key, value in batch.items():
                        batch[key] = batch[key] + CA_noises[key]

                    for key, value in batch.items():
                        # print("key:", key)
                        # if key == "pointgoal_with_gps_compass":
                        # if key == "depth" or key == "pointgoal_with_gps_compass":
                        #     print("lalala")
                        # print(key, ":", value.float())
                        batch[key] = value.float().clone().\
                            detach().requires_grad_()
                    test_recurrent_hidden_states = \
                        test_recurrent_hidden_states.\
                            clone().detach().requires_grad_()
                    self.actor_critic.zero_grad()
                    (
                        current_value_,
                        actions,
                        current_log_prob_,
                        test_recurrent_hidden_states,
                    ) = self.actor_critic.act(
                        batch,
                        test_recurrent_hidden_states,
                        prev_actions,
                        not_done_masks,
                        deterministic=False,
                    )

                    prev_actions.copy_(actions)  # type: ignore

                    # NB: Move actions to CPU.  If CUDA tensors are
                    # sent in to env.step(), that will create CUDA contexts
                    # in the subprocesses.
                    if is_continuous_action_space(self.policy_action_space):
                        # Clipping actions to the specified limits
                        step_data = [
                            np.clip(
                                a.numpy(),
                                self.policy_action_space.low,
                                self.policy_action_space.high,
                            )
                            for a in actions.cpu()
                        ]
                    else:
                        step_data = [a.item() for a in actions.cpu()]

                    # in Gibson, action space is discrete(4), i.e., 0,1,2,3
                    # print("action:", step_data)
                    # print("value:", current_value_)
                    # print("log prob:", current_log_prob_)
                    # print("prob:", torch.exp(current_log_prob_))
                    # loss_ = torch.exp(current_log_prob_)
                    loss_ = current_log_prob_
                    loss_.backward()
                    # print("a", batch["pointgoal_with_gps_compass"].grad)
                    # print("b", batch["depth"].grad)
                    with torch.no_grad():
                        for key, value in batch.items():
                            # CA_noises[key] = CA_noises[key] - \
                            #                  value.grad * current_value_
                            CA_grad[key] = CA_grad[key] - \
                                           value.grad * current_value_
                            # del value.grad
                    #     print(key, value.grad)

                    outputs = self.envs.step(step_data)

                    observations, rewards_l, dones, infos = [
                        list(x) for x in zip(*outputs)
                    ]
                    batch = batch_obs(  # type: ignore
                        observations,
                        device=self.device,
                    )
                    batch = apply_obs_transforms_batch(batch, self.obs_transforms)

                    not_done_masks = torch.tensor(
                        [[not done] for done in dones],
                        dtype=torch.bool,
                        device="cpu",
                    )

                    rewards = torch.tensor(
                        rewards_l, dtype=torch.float, device="cpu"
                    ).unsqueeze(1)
                    current_episode_reward += rewards
                    next_episodes_info = self.envs.current_episodes()
                    envs_to_pause = []
                    n_envs = self.envs.num_envs
                    for i in range(n_envs):
                        if (
                            ep_eval_count[
                                (
                                    next_episodes_info[i].scene_id,
                                    next_episodes_info[i].episode_id,
                                )
                            ]
                            == evals_per_ep
                        ):
                            envs_to_pause.append(i)

                        # episode ended
                        if not not_done_masks[i].item():
                            print("*" * 5, "Now we have run episode:",
                                  len(stats_episodes), "*" * 5)
                            # pbar.update()
                            episode_stats = {
                                "reward": current_episode_reward[i].item()
                            }
                            episode_stats.update(
                                self._extract_scalars_from_info(infos[i])
                            )
                            current_episode_reward[i] = 0
                            k = (
                                current_episodes_info[i].scene_id,
                                current_episodes_info[i].episode_id,
                            )
                            ep_eval_count[k] += 1
                            # use scene_id + episode_id as unique id for storing stats
                            stats_episodes[(k, ep_eval_count[k])] = episode_stats
                            print(episode_stats)

                    not_done_masks = not_done_masks.to(device=self.device)
                    (
                        self.envs,
                        test_recurrent_hidden_states,
                        not_done_masks,
                        current_episode_reward,
                        prev_actions,
                        batch,
                        rgb_frames,
                    ) = self._pause_envs(
                        envs_to_pause,
                        self.envs,
                        test_recurrent_hidden_states,
                        not_done_masks,
                        current_episode_reward,
                        prev_actions,
                        batch,
                        rgb_frames,
                    )

                for key, value in CA_grad.items():
                    print("norm in consistent attack", key, ":",
                          torch.norm(value))
                    print(CA_grad[key].shape)
                    print(len(CA_grad[key].shape))
                    # print(value)
                    if torch.norm(value) == 0:
                        continue
                    if key == "rgb":
                        print("shape of", key, ":", value.shape)
                        CA_noises[key] = CA_noises[key] +\
                                         value / torch.norm(value) * \
                                         alpha_ * 255 * value.shape[3] *\
                                         value.shape[2]
                    elif key == "depth":
                        print("shape of depth:", value.shape)
                        CA_noises[key] = CA_noises[key] +\
                                         value / torch.norm(value) * \
                                         alpha_ * value.shape[3] *\
                                         value.shape[2]
                    else:
                        CA_noises[key] = CA_noises[key] +\
                                         value / torch.norm(value) * \
                                         alpha_

            for key, value in CA_noises.items():
                print("norm in consistent attack", key, ":", torch.norm(value))
                print(CA_noises[key].shape)
                print(len(CA_noises[key].shape))
                print(value)
                if torch.norm(value) == 0:
                    continue
                if key == "rgb":
                    print("shape of", key, ":", value.shape)
                    CA_noises[key] = value / torch.norm(value) * \
                                     eta * 255 * value.shape[3] * \
                                     value.shape[2]
                elif key == "depth":
                    print("shape of depth:", value.shape)
                    CA_noises[key] = value / torch.norm(value) * eta * \
                                     value.shape[3] * value.shape[2]
                else:
                    CA_noises[key] = value / torch.norm(value) * eta
        elif evaluate_strategy == 3:
            print("We use Reward Trajectory attack")
            # pbar = tqdm.tqdm(total=number_of_eval_episodes * evals_per_ep)
            # set model to eval, and we can not calculate the grad when in eval
            # self.actor_critic.eval()

            alpha_ = eta / update_num
            for attack_index in range(update_num):
                print("#"*5, "This is update time:", attack_index+1, "#"*5)

                for key, values in CA_noises.items():
                    CA_grad[key] = torch.zeros(values.shape, dtype=torch.float,
                                               device=values.device)

                batch = batch_obs(observations, device=self.device)
                batch = apply_obs_transforms_batch(batch,
                                                   self.obs_transforms)  # type: ignore

                current_episode_reward = torch.zeros(
                    self.envs.num_envs, 1, device="cpu"
                )

                test_recurrent_hidden_states = torch.zeros(
                    self.config.habitat_baselines.num_environments,
                    self.actor_critic.num_recurrent_layers,
                    ppo_cfg.hidden_size,
                    device=self.device,
                )
                prev_actions = torch.zeros(
                    self.config.habitat_baselines.num_environments,
                    *action_shape,
                    device=self.device,
                    dtype=torch.long if discrete_actions else torch.float,
                )
                not_done_masks = torch.zeros(
                    self.config.habitat_baselines.num_environments,
                    1,
                    device=self.device,
                    dtype=torch.bool,
                )
                stats_episodes: Dict[
                    Any, Any
                ] = {}  # dict of dicts that stores stats per episode
                ep_eval_count: Dict[Any, int] = defaultdict(lambda: 0)

                rgb_frames = [
                    [] for _ in
                    range(self.config.habitat_baselines.num_environments)
                ]  # type: List[List[np.ndarray]]

                current_CA_grad = {}
                for key, values in CA_noises.items():
                    current_CA_grad[key] = torch.zeros(values.shape,
                                                       dtype=torch.float,
                                                       device=values.device)
                while (
                    len(stats_episodes) < (traj_num_each * evals_per_ep)
                    and self.envs.num_envs > 0
                ):
                    current_episodes_info = self.envs.current_episodes()

                    for key, value in batch.items():
                        batch[key] = batch[key] + CA_noises[key]

                    for key, value in batch.items():
                        # print("key:", key)
                        # if key == "pointgoal_with_gps_compass":
                        # if key == "depth" or key == "pointgoal_with_gps_compass":
                        #     print("lalala")
                        # print(key, ":", value.float())
                        batch[key] = value.float().clone().\
                            detach().requires_grad_()
                    test_recurrent_hidden_states = \
                        test_recurrent_hidden_states.\
                            clone().detach().requires_grad_()
                    self.actor_critic.zero_grad()
                    (
                        current_value_,
                        actions,
                        current_log_prob_,
                        test_recurrent_hidden_states,
                    ) = self.actor_critic.act(
                        batch,
                        test_recurrent_hidden_states,
                        prev_actions,
                        not_done_masks,
                        deterministic=False,
                    )

                    prev_actions.copy_(actions)  # type: ignore

                    # NB: Move actions to CPU.  If CUDA tensors are
                    # sent in to env.step(), that will create CUDA contexts
                    # in the subprocesses.
                    if is_continuous_action_space(self.policy_action_space):
                        # Clipping actions to the specified limits
                        step_data = [
                            np.clip(
                                a.numpy(),
                                self.policy_action_space.low,
                                self.policy_action_space.high,
                            )
                            for a in actions.cpu()
                        ]
                    else:
                        step_data = [a.item() for a in actions.cpu()]

                    # in Gibson, action space is discrete(4), i.e., 0,1,2,3
                    # print("action:", step_data)
                    # print("value:", current_value_)
                    # print("log prob:", current_log_prob_)
                    # print("prob:", torch.exp(current_log_prob_))
                    # loss_ = torch.exp(current_log_prob_)
                    loss_ = current_log_prob_
                    loss_.backward()
                    # print("a", batch["pointgoal_with_gps_compass"].grad)
                    # print("b", batch["depth"].grad)
                    with torch.no_grad():
                        for key, value in batch.items():
                            # CA_noises[key] = CA_noises[key] - \
                            #                  value.grad * current_value_
                            current_CA_grad[key] = current_CA_grad[key] * gamma_ \
                                                   - value.grad
                    #     print(key, value.grad)

                    outputs = self.envs.step(step_data)

                    observations, rewards_l, dones, infos = [
                        list(x) for x in zip(*outputs)
                    ]
                    batch = batch_obs(  # type: ignore
                        observations,
                        device=self.device,
                    )
                    batch = apply_obs_transforms_batch(batch, self.obs_transforms)

                    not_done_masks = torch.tensor(
                        [[not done] for done in dones],
                        dtype=torch.bool,
                        device="cpu",
                    )

                    rewards = torch.tensor(
                        rewards_l, dtype=torch.float, device="cpu"
                    ).unsqueeze(1)
                    current_episode_reward += rewards
                    next_episodes_info = self.envs.current_episodes()
                    envs_to_pause = []
                    n_envs = self.envs.num_envs
                    for i in range(n_envs):
                        if (
                            ep_eval_count[
                                (
                                    next_episodes_info[i].scene_id,
                                    next_episodes_info[i].episode_id,
                                )
                            ]
                            == evals_per_ep
                        ):
                            envs_to_pause.append(i)

                        # episode ended
                        if not not_done_masks[i].item():
                            print("*" * 5, "Now we have run episode:",
                                  len(stats_episodes), "*" * 5)
                            # pbar.update()
                            episode_stats = {
                                "reward": current_episode_reward[i].item()
                            }
                            episode_stats.update(
                                self._extract_scalars_from_info(infos[i])
                            )
                            current_episode_reward[i] = 0
                            k = (
                                current_episodes_info[i].scene_id,
                                current_episodes_info[i].episode_id,
                            )
                            ep_eval_count[k] += 1
                            # use scene_id + episode_id as unique id for storing stats
                            stats_episodes[(k, ep_eval_count[k])] = episode_stats
                            print(episode_stats)

                            for key, value in CA_grad.items():
                                CA_grad[key] = CA_grad[key] + \
                                               current_CA_grad[key] * \
                                               episode_stats['success']

                            for key, values in CA_noises.items():
                                current_CA_grad[key] = torch.zeros(
                                    values.shape,
                                    dtype=torch.float,
                                    device=values.device)

                    not_done_masks = not_done_masks.to(device=self.device)
                    (
                        self.envs,
                        test_recurrent_hidden_states,
                        not_done_masks,
                        current_episode_reward,
                        prev_actions,
                        batch,
                        rgb_frames,
                    ) = self._pause_envs(
                        envs_to_pause,
                        self.envs,
                        test_recurrent_hidden_states,
                        not_done_masks,
                        current_episode_reward,
                        prev_actions,
                        batch,
                        rgb_frames,
                    )

                for key, value in CA_grad.items():
                    print("norm in consistent attack", key, ":",
                          torch.norm(value))
                    print(CA_grad[key].shape)
                    print(len(CA_grad[key].shape))
                    # print(value)
                    if torch.norm(value) == 0:
                        continue
                    if key == "rgb":
                        print("shape of", key, ":", value.shape)
                        CA_noises[key] = CA_noises[key] +\
                                         value / torch.norm(value) * \
                                         alpha_ * 255 * value.shape[3] *\
                                         value.shape[2]
                    elif key == "depth":
                        print("shape of depth:", value.shape)
                        CA_noises[key] = CA_noises[key] +\
                                         value / torch.norm(value) * \
                                         alpha_ * value.shape[3] *\
                                         value.shape[2]
                    else:
                        CA_noises[key] = CA_noises[key] +\
                                         value / torch.norm(value) * \
                                         alpha_

            for key, value in CA_noises.items():
                print("norm in consistent attack", key, ":", torch.norm(value))
                print(CA_noises[key].shape)
                print(len(CA_noises[key].shape))
                print(value)
                if torch.norm(value) == 0:
                    continue
                if key == "rgb":
                    print("shape of", key, ":", value.shape)
                    CA_noises[key] = value / torch.norm(value) * \
                                     eta * 255 * value.shape[3] * \
                                     value.shape[2]
                elif key == "depth":
                    print("shape of depth:", value.shape)
                    CA_noises[key] = value / torch.norm(value) * eta * \
                                     value.shape[3] * value.shape[2]
                else:
                    CA_noises[key] = value / torch.norm(value) * eta

        print("#" * 10, "Now we evaluate the victim policy", "#" * 10)
        print("#" * 5, "We will run", number_of_eval_episodes, "trajectories",
              "#" * 5)
        self.envs.close()

        self._init_envs(config, is_eval=True)
        observations = self.envs.reset()
        batch = batch_obs(observations, device=self.device)
        batch = apply_obs_transforms_batch(batch,
                                           self.obs_transforms)  # type: ignore

        current_episode_reward = torch.zeros(
            self.envs.num_envs, 1, device="cpu"
        )

        test_recurrent_hidden_states = torch.zeros(
            self.config.habitat_baselines.num_environments,
            self.actor_critic.num_recurrent_layers,
            ppo_cfg.hidden_size,
            device=self.device,
        )
        prev_actions = torch.zeros(
            self.config.habitat_baselines.num_environments,
            *action_shape,
            device=self.device,
            dtype=torch.long if discrete_actions else torch.float,
        )
        not_done_masks = torch.zeros(
            self.config.habitat_baselines.num_environments,
            1,
            device=self.device,
            dtype=torch.bool,
        )
        stats_episodes: Dict[
            Any, Any
        ] = {}  # dict of dicts that stores stats per episode
        ep_eval_count: Dict[Any, int] = defaultdict(lambda: 0)

        rgb_frames = [
            [] for _ in range(self.config.habitat_baselines.num_environments)
        ]  # type: List[List[np.ndarray]]

        self.actor_critic.eval()

        result_dir = self.config.habitat_baselines.video_dir + "/"
        if evaluate_strategy == 0:
            result_dir = result_dir + "victim"
        elif evaluate_strategy == 1:
            result_dir = result_dir + "UAP"
        elif evaluate_strategy == 2:
            result_dir = result_dir + "Reward_UAP"
        elif evaluate_strategy == 3:
            result_dir = result_dir + "Trajectory_UAP"
        else:
            result_dir = result_dir + str(evaluate_strategy)

        if evaluate_strategy >= 1:
            result_dir = result_dir + "/" + str(eta) + "_" + \
                         str(attack_trajectory_number) + "_" + \
                         str(update_num) + "_" + str(traj_num_each)
        save_video_dir = result_dir + "/video"
        if len(self.config.habitat_baselines.video_option) > 0:
            os.makedirs(save_video_dir, exist_ok=True)
        np.save(result_dir + "/adversarial_noises.npy", CA_noises)

        start_time = time.time()
        while (
            len(stats_episodes) < (number_of_eval_episodes * evals_per_ep)
            and self.envs.num_envs > 0
        ):
            current_episodes_info = self.envs.current_episodes()

            # a function like torch.no_grad in torch>=1.10
            with inference_mode():
                # add consistent attack
                # print("Now we add the consistent noises")
                for key, values in batch.items():
                    batch[key] = batch[key] + CA_noises[key]

                (
                    current_value_,
                    actions,
                    current_log_prob_,
                    test_recurrent_hidden_states,
                ) = self.actor_critic.act(
                    batch,
                    test_recurrent_hidden_states,
                    prev_actions,
                    not_done_masks,
                    deterministic=False,
                )

                prev_actions.copy_(actions)  # type: ignore

            # NB: Move actions to CPU.  If CUDA tensors are
            # sent in to env.step(), that will create CUDA contexts
            # in the subprocesses.
            if is_continuous_action_space(self.policy_action_space):
                # Clipping actions to the specified limits
                step_data = [
                    np.clip(
                        a.numpy(),
                        self.policy_action_space.low,
                        self.policy_action_space.high,
                    )
                    for a in actions.cpu()
                ]
            else:
                step_data = [a.item() for a in actions.cpu()]

            outputs = self.envs.step(step_data)

            observations, rewards_l, dones, infos = [
                list(x) for x in zip(*outputs)
            ]

            # print("infos:", infos)

            batch = batch_obs(  # type: ignore
                observations,
                device=self.device,
            )
            batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

            not_done_masks = torch.tensor(
                [[not done] for done in dones],
                dtype=torch.bool,
                device="cpu",
            )

            rewards = torch.tensor(
                rewards_l, dtype=torch.float, device="cpu"
            ).unsqueeze(1)
            current_episode_reward += rewards
            next_episodes_info = self.envs.current_episodes()
            envs_to_pause = []
            n_envs = self.envs.num_envs
            for i in range(n_envs):
                if (
                    ep_eval_count[
                        (
                            next_episodes_info[i].scene_id,
                            next_episodes_info[i].episode_id,
                        )
                    ]
                    == evals_per_ep
                ):
                    envs_to_pause.append(i)

                if len(self.config.habitat_baselines.video_option) > 0:
                    # TODO move normalization / channel changing out of the
                    #  policy and undo it here
                    # print("raw infos:", infos[i])
                    # np.set_printoptions(threshold=np.inf)
                    processed_info = {
                        "collisions":
                            {"is_collision":
                                 infos[i]["collisions.is_collision"]},
                        "top_down_map": {
                            "map": infos[i]["top_down_map.map"],
                            "fog_of_war_mask":
                                infos[i]["top_down_map.fog_of_war_mask"],
                            "agent_map_coord":
                                infos[i]["top_down_map.agent_map_coord"],
                            "agent_angle":
                                infos[i]["top_down_map.agent_angle"],
                        }
                    }
                    # frame = observations_to_image(
                    #     {k: v[i] for k, v in batch.items()}, infos[i]
                    # )
                    frame = observations_to_image(
                        {k: v[i] for k, v in batch.items()}, processed_info
                    )
                    if not not_done_masks[i].item():
                        # The last frame corresponds to the first frame of
                        # the next episode
                        # but the info is correct. So we use a black frame
                        # frame = observations_to_image(
                        #     {k: v[i] * 0.0 for k, v in batch.items()}, infos[i]
                        # )
                        frame = observations_to_image(
                            {k: v[i] * 0.0 for k, v in batch.items()}, processed_info
                        )
                    if self.config.habitat_baselines.video_render_all_info:
                        # frame = overlay_frame(frame, infos[i])
                        frame = overlay_frame(frame, processed_info)
                    rgb_frames[i].append(frame)

                # episode ended
                if not not_done_masks[i].item():
                    print("*"*5, "Now we have run episode:",
                          len(stats_episodes), "*"*5)
                    print("result dir:", result_dir)
                    # pbar.update()
                    episode_stats = {
                        "reward": current_episode_reward[i].item()
                    }
                    episode_stats.update(
                        self._extract_scalars_from_info(infos[i])
                    )
                    current_episode_reward[i] = 0
                    k = (
                        current_episodes_info[i].scene_id,
                        current_episodes_info[i].episode_id,
                    )
                    ep_eval_count[k] += 1
                    # use scene_id + episode_id as unique id for storing stats
                    stats_episodes[(k, ep_eval_count[k])] = episode_stats
                    print("#"*5, "Performance of this episode", "#"*5)
                    print(episode_stats)
                    print("#" * 5, "Current average performance", "#" * 5)
                    current_average_info = {}
                    for key_ in next(iter(stats_episodes.values())).keys():
                        current_average_info[key_] = np.mean(
                            [v[key_] for v in stats_episodes.values()]
                        )
                        # print(key_, ":", np.mean(
                        #     [v[key_] for v in stats_episodes.values()]
                        # ))
                    print(current_average_info)

                    if len(self.config.habitat_baselines.video_option) > 0 \
                        and self.config.habitat_baselines.video_save:
                        print("#"*5, "Now we generate videos", "#"*5)
                        generate_video(
                            video_option=self.config.habitat_baselines.video_option,
                            # video_dir=self.config.habitat_baselines.video_dir,
                            video_dir=save_video_dir,
                            images=rgb_frames[i],
                            episode_id=current_episodes_info[i].episode_id,
                            checkpoint_idx=checkpoint_index,
                            metrics=self._extract_scalars_from_info(infos[i]),
                            fps=self.config.habitat_baselines.video_fps,
                            tb_writer=writer,
                            keys_to_include_in_name=self.config.
                            habitat_baselines.eval_keys_to_include_in_name,
                        )

                        rgb_frames[i] = []

                    gfx_str = infos[i].get(GfxReplayMeasure.cls_uuid, "")
                    if gfx_str != "":
                        write_gfx_replay(
                            gfx_str,
                            self.config.habitat.task,
                            current_episodes_info[i].episode_id,
                        )

                    np.save(result_dir + "/stats_episodes.npy", stats_episodes)
                    print("Time cost:", time.time() - start_time)
                    start_time = time.time()

            not_done_masks = not_done_masks.to(device=self.device)
            (
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            ) = self._pause_envs(
                envs_to_pause,
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            )

        # pbar.close()
        assert (
            len(ep_eval_count) >= number_of_eval_episodes
        ), f"Expected {number_of_eval_episodes} episodes, got {len(ep_eval_count)}."

        print("#"*5, "Current we will print the results", "#"*5)
        print("eta:", eta)
        print("update number:", update_num)
        print("trajectory number in each update:", traj_num_each)
        aggregated_stats = {}
        for stat_key in next(iter(stats_episodes.values())).keys():
            aggregated_stats[stat_key] = np.mean(
                [v[stat_key] for v in stats_episodes.values()]
            )

        np.save(result_dir + "/stats_episodes.npy", stats_episodes)
        np.save(result_dir + "/adversarial_noises.npy", CA_noises)

        for k, v in aggregated_stats.items():
            logger.info(f"Average episode {k}: {v:.4f}")
        print("#" * 5, "There are all the results", "#" * 5)

        step_id = checkpoint_index
        if "extra_state" in ckpt_dict and "step" in ckpt_dict["extra_state"]:
            step_id = ckpt_dict["extra_state"]["step"]

        writer.add_scalar(
            "eval_reward/average_reward", aggregated_stats["reward"], step_id
        )

        metrics = {k: v for k, v in aggregated_stats.items() if k != "reward"}
        for k, v in metrics.items():
            writer.add_scalar(f"eval_metrics/{k}", v, step_id)

        print("#"*5, "The config we use:",
              self.config.habitat_baselines.eval_ckpt_path_dir, "#"*5)

        self.envs.close()
