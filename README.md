# 金饰活动话术与预留有效性

金饰门店活动群、实物清单及客服来信。材料中的人、机构及经营记录是合成的。

每份资料为 UTF-8 JSON 数组。id 在同一份资料内唯一，原始文字按收到的形式保留。

- `data/policy.json`。活动群对话。role=经理授权/发布/转发分别表示有权更正、可发布、新增不了授权。时刻为门店当地时间。
- `data/inventory.json`。单位重量 g，gram_price 为基础金料价，fixed_price 为固定含工费总价。holdable=false 是货品目录明确禁止留货，null 表示等待门店确认。
- `data/customers.json`。客服记录。at 为记录时间，request 是归属同一客户组合的多条来信的引用。外部柜台单可已有签字，原承诺来自旧手工操作。

使用 Python 3.11 标准库，无需安装软件包或连接外部服务。

以下命令分别读取一份原始资料、检查资料中的字段与关联。

```sh
python3 -m json.tool data/policy.json
python3 -m unittest discover -s tests -v
```
