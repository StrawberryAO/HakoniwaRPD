name: 功能建议
description: 提出一个新功能或改进想法
title: "[Feature] "
labels: ["enhancement"]
body:
  - type: markdown
    attributes:
      value: |
        欢迎提出想法！描述越具体越好。
  - type: textarea
    id: problem
    attributes:
      label: 解决了什么问题 / 想改善什么
      placeholder: "例如：希望角色能主动分享自己的日常……"
    validations:
      required: true
  - type: textarea
    id: solution
    attributes:
      label: 期望的方案
      placeholder: "描述你设想的功能或交互方式"
    validations:
      required: true
  - type: textarea
    id: alternative
    attributes:
      label: 备选方案（可选）
  - type: textarea
    id: extra
    attributes:
      label: 其他 / 参考
      description: "如参考截图、其他软件的做法等"
