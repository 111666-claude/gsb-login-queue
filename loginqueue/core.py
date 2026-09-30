"""登录排队：令牌桶限速、断线重连保留位、超时清理。

口径见 README：
- 令牌桶容量 1、初始 1 个令牌，按 elapsed_ms * rate_per_min / 60000 补充；
- keep_reserve 的玩家在 timeout_ms 内重连时插到队首放行，保留位不超 reserve_slots；
- 入队超过 timeout_ms 的条目清掉，size 只数还在等的人；
- 同一玩家重复入队不重复占位；
- position 不随队列长度线性扫描，scanned_count 每次查询只 +1。
"""


class QueueError(ValueError):
    """排队操作不合法。"""


class _Fenwick:
    """只存删除标记的树状数组：前缀和 O(log n)，仅保留位插队产生洞时才用。"""

    def __init__(self, size):
        self.tree = [0] * (size + 1)

    def add(self, index, delta):
        tree = self.tree
        pos = index + 1
        while pos < len(tree):
            tree[pos] += delta
            pos += pos & -pos

    def prefix_sum(self, index):
        """[0, index) 上的和；index 超出容量时按全部容量算（超出部分没有洞）。"""
        tree = self.tree
        total = 0
        pos = min(index, len(tree) - 1)
        while pos > 0:
            total += tree[pos]
            pos -= pos & -pos
        return total


class LoginQueue:
    """登录队列。"""

    def __init__(self, rate_per_min=60, reserve_slots=2, timeout_ms=60000):
        self.rate_per_min = rate_per_min
        self.reserve_slots = reserve_slots
        self.timeout_ms = timeout_ms
        # 槽位：None 表示已被放行/清理留下的洞；正常入队/队首放行永远走 append/head。
        self.slots = []
        self.head = 0
        self.live = 0
        self.index = {}
        self.gaps = None
        # 令牌桶：容量 1、初始 1，余量以分子形式累计，整除才补、余数结转。
        self.tokens = 1
        self._carry_ms = 0
        self._last_ms = 0
        # 保留位：player -> 失效时刻；按首次 keep_reserve 先后排队（dict 插入序），续期不改先后。
        self.reserves = {}
        self.scanned = 0

    def _ensure_gap_tree(self):
        """洞计数树容量倍增：倍增时从槽位重建，整体摊还 O(1)。"""
        if self.gaps is not None:
            if len(self.gaps.tree) > len(self.slots):
                return
            new_cap = max(1, (len(self.gaps.tree) - 1) * 2)
        else:
            new_cap = max(8, len(self.slots) * 2)
        rebuilt = _Fenwick(new_cap)
        for index in range(self.head, len(self.slots)):
            if self.slots[index] is None:
                rebuilt.add(index, 1)
        self.gaps = rebuilt

    def _add_gap(self, index):
        self._ensure_gap_tree()
        self.gaps.add(index, 1)

    def _gap_count(self, begin, end):
        """[begin, end) 之间的洞数。"""
        if self.gaps is None:
            return 0
        return self.gaps.prefix_sum(end) - self.gaps.prefix_sum(begin)

    def _swallow_head(self):
        """从队首吞掉连续的洞；槽位全部耗尽时整体复位。"""
        slots = self.slots
        while self.head < len(slots) and slots[self.head] is None:
            self.head += 1
        if self.head == len(slots):
            self.slots = []
            self.head = 0
            self.gaps = None

    def _expire(self, now_ms):
        """清理队首入队超过 timeout_ms 的条目（入队时间单调，只可能在队首）。"""
        while self.head < len(self.slots):
            player, enqueue_ms = self.slots[self.head]
            if now_ms - enqueue_ms <= self.timeout_ms:
                break
            self.slots[self.head] = None
            self.live -= 1
            self.index.pop(player, None)
            self._swallow_head()

    def _purge_expired_reserves(self, now_ms):
        expired = [p for p, deadline in self.reserves.items() if deadline < now_ms]
        for player in expired:
            del self.reserves[player]

    def _next_reserve(self, now_ms):
        """返回当前可插队放行的保留位玩家（已重连入队、未过期），没有则 None。"""
        for player, deadline in self.reserves.items():
            if deadline >= now_ms and player in self.index:
                return player
        return None

    def _admit(self, player):
        index = self.index.pop(player)
        self.live -= 1
        self.reserves.pop(player, None)
        self.slots[index] = None
        if index == self.head:
            self._swallow_head()
        else:
            self._add_gap(index)
        return player

    def _refill(self, now_ms):
        if now_ms < self._last_ms:
            now_ms = self._last_ms
        elapsed = now_ms - self._last_ms
        self._last_ms = now_ms
        self._carry_ms += elapsed * self.rate_per_min
        added = self._carry_ms // 60000
        self._carry_ms %= 60000
        if added:
            self.tokens = min(1, self.tokens + added)

    def enqueue(self, player, now_ms):
        """入队；同一玩家重复入队不重复占位，返回其队列位置。"""
        self._expire(now_ms)
        if player in self.index:
            return self._rank(player)
        self.index[player] = len(self.slots)
        self.slots.append((player, now_ms))
        self._ensure_gap_tree()
        self.live += 1
        return self._rank(player)

    def keep_reserve(self, player, now_ms):
        """保留位子：timeout_ms 内重连插到队首；超出 reserve_slots 返回 False。"""
        self._purge_expired_reserves(now_ms)
        deadline = now_ms + self.timeout_ms
        if player in self.reserves:
            self.reserves[player] = deadline
            return True
        if len(self.reserves) >= self.reserve_slots:
            return False
        self.reserves[player] = deadline
        return True

    def poll(self, now_ms):
        """有令牌才放行一个：保留位优先，否则队首；没令牌返回 None。"""
        self._refill(now_ms)
        if self.tokens < 1:
            return None
        self._purge_expired_reserves(now_ms)
        self._expire(now_ms)
        reserved = self._next_reserve(now_ms)
        if reserved is not None:
            self.tokens -= 1
            return self._admit(reserved)
        if self.head >= len(self.slots):
            return None
        entry = self.slots[self.head]
        self.tokens -= 1
        return self._admit(entry[0])

    def size(self, now_ms):
        """还在等的人数（超时的先清掉）。"""
        self._expire(now_ms)
        return self.live

    def _rank(self, player):
        index = self.index.get(player)
        if index is None:
            return 0
        return index - self.head + 1 - self._gap_count(self.head, index)

    def position(self, player, now_ms):
        """队列位置（1 起），不在队列为 0；单次代价 O(1) 摊还，不扫全表。"""
        self.scanned += 1
        self._expire(now_ms)
        return self._rank(player)

    def scanned_count(self):
        return self.scanned
