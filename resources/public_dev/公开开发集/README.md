# 公开开发集

64个独立教学单元：B001 20个；进阶规则F01—F10每个规则族取1条代表规则（R005、R010、R014、R021、R026、R035、R041、R048、R050、R060）各4个单元，其中R050另补4个矩阵教学单元。其余50条进阶规则没有公开样例，请按01_规则包原文实现；隐藏集覆盖全部60条。矩阵读法和逐例索引见矩阵材料说明.md、矩阵教学索引.json。inputs/audit_inputs.jsonl和同目录assets为任务；references/audit_gold.jsonl为公开标签与事实；reference_predictions.jsonl为完整参考输出。逐例讲解.md解释边界。

在本目录运行：

```sh
python3 metric_reference.py --inputs inputs/audit_inputs.jsonl --references references/audit_gold.jsonl --predictions reference_predictions.jsonl
```

参考输出应Q=1，只证明接口和算术一致，不是参赛系统成绩。正式隐藏集不使用开发集的源图或任务ID。开发与隐藏集共享公开规则语义，但对象、材料布局及组合由各自设计生成。公开标签可用于开发，隐藏标签不得作为调参输入。

本目录不提供伪装成业务能力的满分实现。学生应通过真实build/infer/annotate/iterate/evaluate流程完成任务。

ID及装饰信息均为随机生成。开发参考输出按协议1.3.1检查。

公开样例的难度为教学设计标记，不参与正式难度统计。
