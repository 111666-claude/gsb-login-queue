# login-queue

登录排队：令牌桶限速、断线重连保留位、超时清理，只用 Python 标准库。

```
python3 -m loginqueue.cli --sample rate
python3 -m loginqueue.cli --sample reserve
python3 -m loginqueue.cli --sample timeout
python3 -m unittest discover -s tests -v
```

## 口径（README 为准）

- **令牌桶按时间补**：桶容量为 1、初始 1 个令牌，之后按
  `elapsed_ms * rate_per_min / 60000` 增量补充（封顶 1）；`poll` 只有在 `tokens >= 1` 时才放行
  并扣掉一个，否则返回 `None`。
- **保留位**：`keep_reserve(player, now)` 让该玩家在 `timeout_ms` 内重连时插到队首放行，
  同时保留位数量不超过 `reserve_slots`，超出返回 `False`。
- **超时清理**：入队超过 `timeout_ms` 的条目必须从队列里清掉，`size(now)` 只数还在等的人。
- **入队幂等**：同一玩家重复入队不重复占位（位置不变）。
- **查询代价**：`position` 不许每次全扫队列，`scanned_count()` 不随队列长度线性增长。
- **规模**：单区 50 万排队玩家、每秒 10 万次位置查询，单次 O(1) 摊还。

## 输出契约（不改格式）

```
first=p second=None
picked=b
size=1
scanned<=3000
```

## 目录

```
loginqueue/core.py   令牌桶、保留位与超时
loginqueue/cli.py    命令行入口
tests/               unittest 用例
```
