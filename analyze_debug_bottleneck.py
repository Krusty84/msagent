#!/usr/bin/env python3
"""
分析 msmodelslim --debug 模式下 _record_debug_info 的拷贝开销。
不依赖运行量化，只静态分析 RotatePair 数量和 R 矩阵大小。
"""
# MiniMax-M3 配置（从日志和 config 推断）
HIDDEN = 24576  # merged_hidden_size
HEAD_DIM = 256  # 单头维度（用于 uv 旋转）
NUM_LAYERS = 60  # 日志显示 60 层
NUM_EXPERTS = 256  # MoE 专家数（参考 MiniMax-M2，M3 类似量级）


def fmt_size(n_bytes):
    if n_bytes >= 1024 ** 3:
        return f"{n_bytes / 1024 ** 3:.2f} GB"
    if n_bytes >= 1024 ** 2:
        return f"{n_bytes / 1024 ** 2:.2f} MB"
    return f"{n_bytes / 1024:.2f} KB"


def analyze():
    # 每个 R 矩阵：Hadamard，shape (n, n)，float32
    rot_size = HIDDEN * HIDDEN * 4  # 主旋转矩阵
    rot_uv_size = HEAD_DIM * HEAD_DIM * 4  # UV 旋转矩阵

    # pre_run_pairs: 1 个 RotatePair
    #   right_rot: {"model.embed_tokens": rot}  → 1 个
    pre_run_count = 1

    # rotate_pairs[0]: 1 个 RotatePair
    #   每层 right_rot: q_proj, k_proj, v_proj, gate = 4 个（dense 层）
    #                    + 每个 expert w1, w3 = 2 * num_experts
    #   每层 left_rot: o_proj = 1 个
    #                  + 每个 expert w2 = 1 * num_experts
    #   另加 lm_head right_rot: 1 个
    rotate_pair_0_right_per_layer = 4 + 2 * NUM_EXPERTS
    rotate_pair_0_left_per_layer = 1 + 1 * NUM_EXPERTS

    # rotate_pairs[1]: 1 个 RotatePair (uv)
    #   每层 left_rot_uv: v_proj = 1 个
    #   每层 right_rot_uv: o_proj = 1 个
    rotate_pair_1_left_per_layer = 1
    rotate_pair_1_right_per_layer = 1

    total_rot = pre_run_count
    total_rot += NUM_LAYERS * (rotate_pair_0_right_per_layer_layer := rotate_pair_0_right_per_layer)
    total_rot += NUM_LAYERS * (rotate_pair_0_left_per_layer_layer := rotate_pair_0_left_per_layer)
    total_rot += 1  # lm_head
    total_rot += NUM_LAYERS * rotate_pair_1_left_per_layer
    total_rot += NUM_LAYERS * rotate_pair_1_right_per_layer

    # 大多数是 rot (hidden×hidden)，UV 是 (head_dim×head_dim)
    uv_count = NUM_LAYERS * (rotate_pair_1_left_per_layer + rotate_pair_1_right_per_layer)
    rot_count = total_rot - uv_count

    rot_bytes = rot_count * rot_size
    uv_bytes = uv_count * rot_uv_size
    total_bytes = rot_bytes + uv_bytes

    print(f"=== MiniMax-M3 --debug 拷贝开销分析 ===")
    print(f"模型规模: {NUM_LAYERS} 层, hidden={HIDDEN}, head_dim={HEAD_DIM}, experts={NUM_EXPERTS}")
    print()
    print(f"单个 R 矩阵 (hidden×hidden): {fmt_size(rot_size)}")
    print(f"单个 R_uv 矩阵 (head_dim×head_dim): {fmt_size(rot_uv_size)}")
    print()
    print(f"pre_run_pairs: 1 个 R ({fmt_size(rot_size)})")
    print(f"rotate_pairs[0] 每层 right_rot: {rotate_pair_0_right_per_layer} 个 "
          f"({fmt_size(rotate_pair_0_right_per_layer * rot_size)})")
    print(f"rotate_pairs[0] 每层 left_rot:  {rotate_pair_0_left_per_layer} 个 "
          f"({fmt_size(rotate_pair_0_left_per_layer * rot_size)})")
    print(f"rotate_pairs[0] lm_head: 1 个 ({fmt_size(rot_size)})")
    print(f"rotate_pairs[1] 每层 left_rot_uv: {rotate_pair_1_left_per_layer} 个 "
          f"({fmt_size(rotate_pair_1_left_per_layer * rot_uv_size)})")
    print(f"rotate_pairs[1] 每层 right_rot_uv: {rotate_pair_1_right_per_layer} 个 "
          f"({fmt_size(rotate_pair_1_right_per_layer * rot_uv_size)})")
    print()
    print(f"R 矩阵总数: {rot_count} 个, 总大小 {fmt_size(rot_bytes)}")
    print(f"R_uv 矩阵总数: {uv_count} 个, 总大小 {fmt_size(uv_bytes)}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"总拷贝量: {total_bytes / 1024 ** 3:.2f} GB ({total_rot} 个矩阵)")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print()
    print("每次 .cpu() 拷贝一个矩阵，按 PCIe Gen5 x16 ≈ 32 GB/s 估算:")
    print(f"  理想最快: {total_bytes / (32 * 1024**3):.1f} 秒")
    print(f"  实际（同步开销 + 多次 ioctl）通常 5-10x: "
          f"{total_bytes / (32 * 1024**3) * 5:.0f}-{total_bytes / (32 * 1024**3) * 10:.0f} 秒")
    print(f"  即 {total_bytes / (32 * 1024**3) * 5 / 3600:.1f}-"
          f"{total_bytes / (32 * 1024**3) * 10 / 3600:.1f} 小时")


if __name__ == "__main__":
    analyze()
