set -x

# 指定只使用 NPU 1
export ASCEND_RT_VISIBLE_DEVICES=1

MODEL_ID=${MODEL_ID:-Qwen/Qwen3-0.6B}
MODEL_PATH=/workspace/models/Qwen3-0.6B
NNODES=${NNODES:-1}
NDEVICES_PER_NODE=${NDEVICES_PER_NODE:-1}

TRAIN_BATCH_SIZE=${TRAIN_BATCH_SIZE:-4}
MAX_PROMPT_LENGTH=${MAX_PROMPT_LENGTH:-512}
# 按照 refer.txt 4.1/4.2 节，使用 max_response_length=1 简化训推对比
MAX_RESPONSE_LENGTH=${MAX_RESPONSE_LENGTH:-1}

TOTAL_EPOCHS=${TOTAL_EPOCHS:-1}
TOTAL_TRAINING_STEPS=${TOTAL_TRAINING_STEPS:-1}
SAVE_FREQ=${SAVE_FREQ:-20}
TEST_FREQ=${TEST_FREQ:-20}

PROJECT_NAME=${PROJECT_NAME:-verl_grpo_gsm8k}
EXPERIMENT_NAME=${EXPERIMENT_NAME:-qwen3_0.6b_grpo_megatron_vllm_$(date +%Y%m%d_%H%M)}

TRAIN_FILE=/workspace/data/gsm8k/train.parquet
TEST_FILE=/workspace/data/gsm8k/test.parquet

########################### derived defaults ###########################
n_devices_per_node=${NDEVICES_PER_NODE:-1}

export HCCL_CONNECT_TIMEOUT=1500
export HCCL_HOST_SOCKET_PORT_RANGE=60000-60050
export HCCL_NPU_SOCKET_PORT_RANGE=61000-61050


########################### parameter arrays ###########################

DATA=(
    algorithm.adv_estimator=grpo
    algorithm.use_kl_in_reward=False
    data.train_files=${TRAIN_FILE}
    data.val_files=${TEST_FILE}
    data.train_batch_size=${TRAIN_BATCH_SIZE}
    data.max_prompt_length=${MAX_PROMPT_LENGTH}
    data.max_response_length=${MAX_RESPONSE_LENGTH}
    data.filter_overlong_prompts=True
    data.truncation='error'
    data.val_max_samples=16
)

MODEL=(
    actor_rollout_ref.model.path="$MODEL_PATH"
)

ACTOR=(
    actor_rollout_ref.actor.optim.lr=5e-7
    actor_rollout_ref.actor.ppo_mini_batch_size=4
    actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=1
    actor_rollout_ref.actor.strategy=megatron
    actor_rollout_ref.actor.megatron.pipeline_model_parallel_size=1
    actor_rollout_ref.actor.megatron.tensor_model_parallel_size=1
    actor_rollout_ref.actor.megatron.expert_model_parallel_size=1
    actor_rollout_ref.actor.megatron.use_mbridge=True
    actor_rollout_ref.actor.megatron.vanilla_mbridge=True
    actor_rollout_ref.actor.use_kl_loss=True
    actor_rollout_ref.actor.kl_loss_coef=0.001
    actor_rollout_ref.actor.kl_loss_type=low_var_kl
    actor_rollout_ref.actor.use_torch_compile=False
)

ROLLOUT=(
    actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=1
    actor_rollout_ref.rollout.enable_chunked_prefill=False
    actor_rollout_ref.rollout.tensor_model_parallel_size=1
    actor_rollout_ref.rollout.name=vllm
    actor_rollout_ref.rollout.gpu_memory_utilization=0.6
    actor_rollout_ref.rollout.enforce_eager=True
    actor_rollout_ref.rollout.n=2
    actor_rollout_ref.rollout.calculate_log_probs=True
)

REF=(
    actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=1
    actor_rollout_ref.ref.strategy=megatron
    actor_rollout_ref.ref.megatron.pipeline_model_parallel_size=1
    actor_rollout_ref.ref.megatron.tensor_model_parallel_size=1
    actor_rollout_ref.ref.megatron.expert_model_parallel_size=1
    actor_rollout_ref.ref.use_torch_compile=False
)

TRAINER=(
    trainer.balance_batch=True
    trainer.logger='["console"]'
    trainer.val_before_train=False
    trainer.project_name=${PROJECT_NAME}
    trainer.experiment_name=${EXPERIMENT_NAME}
    trainer.n_gpus_per_node=${n_devices_per_node}
    trainer.nnodes=${NNODES}
    trainer.save_freq=${SAVE_FREQ}
    trainer.test_freq=${TEST_FREQ}
    trainer.total_epochs=${TOTAL_EPOCHS}
    trainer.total_training_steps=${TOTAL_TRAINING_STEPS}
)

PROFILER_OUTPUT_DIR=${PROFILER_OUTPUT_DIR:-/verl/tests/special_npu/profiler_output}
rm -rf ${PROFILER_OUTPUT_DIR} 2>/dev/null || true
mkdir -p ${PROFILER_OUTPUT_DIR}

# Enable NPU profiler for both training and rollout paths
# Add discrete=True for rollout to capture vLLM ops in separate DB
PROFILER=(
    global_profiler.tool=npu
    global_profiler.steps=[1]
    global_profiler.save_path=${PROFILER_OUTPUT_DIR}
    actor_rollout_ref.actor.profiler.enable=True
    actor_rollout_ref.actor.profiler.tool_config.npu.contents="['npu','cpu','shapes']"
    actor_rollout_ref.actor.profiler.tool_config.npu.level=level1
    actor_rollout_ref.ref.profiler.enable=True
    actor_rollout_ref.ref.profiler.tool_config.npu.contents="['npu','cpu','shapes']"
    actor_rollout_ref.ref.profiler.tool_config.npu.level=level1
    actor_rollout_ref.rollout.profiler.enable=True
    actor_rollout_ref.rollout.profiler.tool=npu
    actor_rollout_ref.rollout.profiler.tool_config.npu.contents="['npu','cpu','shapes']"
    actor_rollout_ref.rollout.profiler.tool_config.npu.level=level1
    actor_rollout_ref.rollout.profiler.tool_config.npu.discrete=True
)

########################### launch ###########################
ray stop --force 2>/dev/null || true

# 通过 msprof profiling 采集算子的 CANN 级数据
# e2e 目录：训练路径（Megatron actor+ref）
# agent_loop_rollout_replica_0 目录：推理路径（vLLM rollout）
# discrete=True 确保 rollout 与训练分开采集

python3 -m verl.trainer.main_ppo --config-path=config \
    --config-name='ppo_megatron_trainer.yaml' \
    "${DATA[@]}" \
    "${MODEL[@]}" \
    "${ACTOR[@]}" \
    "${ROLLOUT[@]}" \
    "${REF[@]}" \
    "${TRAINER[@]}" \
    "${PROFILER[@]}" \
    "$@"

