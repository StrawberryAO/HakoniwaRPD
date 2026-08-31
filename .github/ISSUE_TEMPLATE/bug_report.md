name: Bug 报告
description: 提交一个 bug 反馈
title: "[Bug] "
labels: ["bug"]
body:
  - type: markdown
    attributes:
      value: |
        感谢反馈！请尽量填写以下信息，便于定位问题。
  - type: input
    id: version
    attributes:
      label: 版本 / 运行方式
      description: 是源码运行（python main.py）还是 exe？版本号或大致时间。
      placeholder: "e.g. 源码 / exe，2026-08"
    validations:
      required: true
  - type: textarea
    id: what
    attributes:
      label: 发生了什么？
      description: 描述问题现象。
      placeholder: "点 X 之后，Y 没有出现，并且 / 报错……"
    validations:
      required: true
  - type: textarea
    id: expected
    attributes:
      label: 期望的行为
      description: 你希望发生什么？
  - type: textarea
    id: reproduce
    attributes:
      label: 复现步骤
      placeholder: "1. 打开设置… 2. 点击自动生成… 3. …"
  - type: textarea
    id: logs
    attributes:
      label: 日志 / 报错信息
      description: 粘贴控制台输出或 `logs/app.log` 末尾的内容。
      render: text
  - type: textarea
    id: extra
    attributes:
      label: 其他信息
      description: 操作系统、Python 版本、相关截图等。
