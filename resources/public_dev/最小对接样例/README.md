# 可运行最小对接样例

Python3.10+，仅标准库。展示五类命令、共享哈希、模型网关、标注和插件契约；不是完整参赛方案，没有隐藏答案。rules.jsonl、config.json来自公开规则，infer实际读取build产物中的规则和模型配置。

在学生包根目录运行python3 04_最小对接样例/smoke.py，可离线验证build、显式模型未配置错误、no_change迭代、插件、公开标注算术和哈希。离线模式不会伪造模型判断。

使用模型时，在自己的config.json中添加model字段并设置OPENAI_BASE_URL和OPENAI_API_KEY，再执行：

```sh
python3 04_最小对接样例/app.py build --rules 04_最小对接样例/rules.jsonl --config 04_最小对接样例/config.json --out /tmp/audit-demo-build
python3 04_最小对接样例/app.py infer --input 03_公开开发集/标注样例/inputs/tasks.jsonl --artifacts /tmp/audit-demo-build --out /tmp/audit-demo-predictions.jsonl
python3 04_最小对接样例/app.py annotate --input 03_公开开发集/标注样例/inputs/tasks.jsonl --targets 03_公开开发集/标注样例/inputs/targets.jsonl --artifacts /tmp/audit-demo-build --out /tmp/audit-demo-annotations.jsonl
```

目录须为未使用的新目录；重复运行请换目录。模型原始响应留在输出旁的.trace.jsonl，标注解析错误另存.errors.json。仅公开格式校验失败时最多重输出1次，不读取答案、不择优；推理失败使用execution_status=error，标注失败保留缺失和错误记录。

Docker示例：docker build -t audit-demo:local 04_最小对接样例。组织方固定构建后的实际镜像sha256，再调用/app/run.sh；镜像标签仅用于本地构建展示。命令与可选挂载接入见02_协议与评分/容器与模型接入.md。
