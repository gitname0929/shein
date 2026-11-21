# 提交前自测

Python 3.10以上，仅标准库。contracts.py与组织方评分器使用同一文件内容；JSON Schema不能单独覆盖材料归属、页码类型、坐标顺序及元数据例外。

```sh
python3 validate_submission.py --inputs ../../03_公开开发集/inputs/audit_inputs.jsonl --predictions ../../03_公开开发集/reference_predictions.jsonl --references ../../03_公开开发集/references/audit_gold.jsonl
```

本地工具只给诊断，不产生正式比赛总分，也不接受学生自报组织方验收记录。若输入路径不同，按本机位置替换。
