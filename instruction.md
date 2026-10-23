# 多段摆阵幅频复核

试验台刚完成一轮三段扫频。四个接线端各记录两根摆杆的混合响应，交接时接线表、传感器时基和部分非线性参数都没有留下。机械组要从整组轨迹中复原端子与摆杆的双射、时基偏移、cubic stiffness、阻尼和同端子耦合。

程序入口是 `/app/reconstruct.py`，按下面的形式运行：

```text
python3 /app/reconstruct.py --input <输入路径> --output <结果路径> --ledger <账本路径>
```

试验包的根字段固定为：

```text
schema_version, case_id, pendulums, terminals, excitations, limits
```

`schema_version` 必须是整数 `1`。`case_id` 是非空字符串。

摆杆固定为 8 根。每根只有三个字段：

```text
node_id, length_m
```

`node_id` 只能由大写字母、数字和连字符组成。`length_m` 的取值范围是 `0.35` 至 `1.40`。重复摆杆编号会终止整批求解。

`excitations` 保存 3 段已知激励。每段字段固定为：

```text
segment_id, drive_frequency_rad_s, drive_force_m
```

`segment_id` 非空且唯一。`drive_frequency_rad_s` 位于 `2.0` 至 `6.5`，`drive_force_m` 位于 `0.05` 至 `1.20`。同一输入内的段号集合必须与全部端子轨迹逐段一致。

`terminals` 固定为 4 个接线端。每个端子的字段固定为：

```text
terminal_id, traces
```

端子编号要求唯一。每条轨迹在 `traces` 里只有 `segment_id` 和 `samples` 两项：

```text
segment_id, samples
```

每个端子必须恰好包含一段对应每个 `segment_id` 的轨迹。采样对象只允许：

```text
time_s, response_m
```

每段轨迹有 40 至 80 个样本。`time_s` 的首点为 `0.0`，采样间隔恒定，末点不高于 `6.0 s`。同一段号在所有端子中的时间轴必须一致。`response_m` 必须是有限数值。

`limits` 的字段固定为：

```text
max_nodes, max_terminals, max_segments, max_samples
```

四个上限依次是 8、4、3、80。上限本身允许，超界值拒绝。

摆的自然角频率为 `sqrt(9.80665 / length_m)`。对节点 `i` 和激励段 `s`，复响应 `u_i,s` 满足：

```text
u_i,s = F_s /
        (w_i^2 + beta_i * |u_i,s|^2 - Omega_s^2
         + 2*j*d_i*Omega_s)
```

这里 `d_i` 是摆杆阻尼，`beta_i` 是 cubic stiffness。第 `j` 个接线端连接摆杆 `a`、`b`，二者的同端子耦合记为 `c_ab`，采集时基平移记为 `tau_j`：

```text
v_j,s = u_a,s + u_b,s + c_ab*u_a,s*u_b,s
response_m(t) = Re[ v_j,s * exp(j*Omega_s*(t-tau_j)) ]
```

每个端子恰好连接两根不同摆杆，每根摆杆恰好属于一个端子。接线关系、时基偏移和全部物理参数都没有直接给出。判断对应关系时必须联合使用所有激励段。任一单段只含一个驱动频率，单独峰位或单段频谱无法确定两节点组合、传感器时基和非线性参数。

成功结果写入 `--output`。输出对象根字段固定为：

```text
schema_version, case_id, terminal_map, pendulums, couplings, fit
```

`terminal_map` 以 `terminal_id` 升序交付；每条只写 `terminal_id, node_ids, time_offset_s, correlation`。

`node_ids` 内两个节点按字典序排列且互异。传感器的时基字段 `time_offset_s` 仍取 `-0.08` 至 `0.08` 区间，摆动相位反演后与真值的允许偏差为 `3e-4 s`。`correlation` 只接受有限数。

`pendulums` 以 `node_id` 升序交付；每项只含 `node_id, frequency_rad_s, cubic_stiffness, damping_s_inv`。

频率允许偏差 `2e-7 rad/s`。cubic stiffness 允许偏差 `0.08`。阻尼允许偏差 `8e-4 s^-1`。

`couplings` 依首节点、次节点升序交付；每项只含 `node_ids` 与 `coefficient`。

耦合系数允许偏差 `0.35`。

`fit` 字段固定为：

```text
max_abs_response_residual_m, segment_count
```

把全部扫频段一起代入后，`fit.max_abs_response_residual_m` 不得超过 `2e-4 m`；`segment_count` 记录本轮参与联合反演的扫频段总数。

摆阵复原单的字节外观固定：JSON 不带插入空格，键名按升序落盘，文件收口只有一个换行。正常扫频结束后，终端的标准输出和标准错误都保持空。

程序维护 `--ledger` 指定的校验账本。每条成功记录一行，字段固定为：

```text
case_id, event, input_sha256, result_sha256, schema_version
```

`event` 固定为 `completed`。SHA-256 分别作用于输入包与输出文件的原始字节，摘要以小写十六进制保存。同一输入重复执行时不得新增或改写账本行。新的成功结果必须追加一行。试验流水账的每条记录也按无空格 JSON 追加，行末只保留一个换行。

摆阵包读不进来、出现重复键、字段集合对不上或值类型不对时，程序以状态码 `3` 退出。数量和时基先做契约检查，不合格即静默退出 `3`。缺段或物理方程无闭合解时静默退出 `3`。多解歧义和残差超限同样静默退出 `3`。摆阵反演没闭合而退出时，终端两路输出都保持空白。失败不得创建或改写结果文件。失败不得改动账本。发布摆阵复原单时，先在目标目录暂存草稿，再一次性换到正式文件名；换名失败则旧复原单与旧流水字节不动。
