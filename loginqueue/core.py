"""登录排队：令牌桶限速、断线重连保留位、超时清理。

缺陷：令牌按调用次数补而不是按时间、保留位不生效、超时条目不清、入队不查重、位置查询全扫。
"""


class QueueError(ValueError):
    """排队操作不合法。"""


class LoginQueue:
    """登录队列。"""

    def __init__(self, rate_per_min=60, reserve_slots=2, timeout_ms=60000):
        self.rate_per_min = rate_per_min
        self.reserve_slots = reserve_slots
        self.timeout_ms = timeout_ms
        self.waiting = []
        self.tokens = 0.0
        self.reserve = {}
        self.scanned = 0

    def enqueue(self, player, now_ms):
        """入队。缺陷：同一玩家可以重复占位，超时条目也不清。"""
        self.waiting.append((player, now_ms))
        return len(self.waiting)

    def poll(self, now_ms):
        """放行一个人。缺陷：每调用一次就补一个令牌，按调用次数而不是时间。"""
        self.tokens += 1
        if not self.waiting:
            return None
        player, _ = self.waiting.pop(0)
        return player

    def keep_reserve(self, player, now_ms):
        """给断线重连留位子。缺陷：只是记一下，放行时完全不看。"""
        self.reserve[player] = now_ms + self.timeout_ms

    def size(self, now_ms):
        """队列长度。缺陷：超时的也算在里面。"""
        self.scanned += len(self.waiting)
        return len(self.waiting)

    def position(self, player, now_ms):
        """队列位置。缺陷：全表扫描，超时条目也占位。"""
        self.scanned += len(self.waiting)
        for index, (name, _) in enumerate(self.waiting):
            if name == player:
                return index + 1
        return 0

    def scanned_count(self):
        return self.scanned
