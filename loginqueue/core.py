"""登录排队：令牌桶限速、断线重连保留位、超时清理。

口径（README 为准）：

- 令牌桶容量 1、初始 1 个令牌，之后按 ``elapsed_ms * rate_per_min / 60000``
  整除补充、封顶 1；``poll`` 只在 ``tokens >= 1`` 时放行并扣一个，否则 ``None``。
- ``keep_reserve`` 让该玩家在 ``timeout_ms`` 内重连时插到队首（保留位通道）
  放行；保留位占用（通道内 + 悬空位）不超过 ``reserve_slots``，超出返回 ``False``。
- 入队超过 ``timeout_ms`` 的条目会被惰性清掉，``size`` 只数还在等的人。
- 同一玩家重复入队不重复占位；``position`` O(1) 摊还，扫描计数不随队列长度增长。
"""

from collections import deque


class QueueError(ValueError):
    """排队操作不合法。"""


class _Fenwick:
    """按票号记录存活条目数的 Fenwick 树，单点更新与前缀和均为 O(log n)。

    普通通道的条目既会从队首离开，也会从中间转入保留位，无法只靠偏移
    算位置；用票号上的 Fenwick 树记录哪些条目还活着。容量按 2 倍扩张，
    扩张时按位表重建，重建总代价摊还 O(1)/次入队。
    """

    __slots__ = ("tree", "bits", "size")

    def __init__(self):
        self.tree = [0]
        self.bits = [0]
        self.size = 0

    def ensure(self, index):
        """容量按 2 倍扩张到能容纳 index（1-based）。"""
        if self.size >= index:
            return
        new_size = max(1, self.size)
        while new_size < index:
            new_size *= 2
        self.bits.extend([0] * (new_size - self.size))
        rebuilt = self.bits[:]
        for i in range(1, new_size + 1):
            parent = i + (i & -i)
            if parent <= new_size:
                rebuilt[parent] += rebuilt[i]
        self.tree = rebuilt
        self.size = new_size

    def add(self, index, delta):
        self.bits[index] += delta
        tree = self.tree
        while index < len(tree):
            tree[index] += delta
            index += index & -index

    def prefix_sum(self, index):
        tree = self.tree
        total = 0
        while index > 0:
            total += tree[index]
            index -= index & -index
        return total


class LoginQueue:
    """登录队列。"""

    def __init__(self, rate_per_min=60, reserve_slots=2, timeout_ms=60000):
        self.rate_per_min = rate_per_min
        self.reserve_slots = reserve_slots
        self.timeout_ms = timeout_ms

        # 普通通道：票号单调递增的 FIFO，中间可能留“已转入保留位”的空洞，
        # 空洞条目滑到队首时被跳过（每条目入/出各一次，摊还 O(1)）。
        self._regular = deque()          # deque[ticket]
        self._owner = {}                 # ticket -> player（空洞条目已删除）
        self._enqueued_at = {}           # ticket -> 入队时刻
        self._player_ticket = {}         # player -> ticket 的反查表
        self._live = _Fenwick()
        self._next_ticket = 1
        self._regular_count = 0

        # 保留位通道：队首是最新保留位、最先放行；失效条目聚在队尾。
        self._reserve_q = deque()        # deque[player]
        self._reserve_meta = {}          # player -> (deadline, enqueued_at)

        # 断线未归的悬空保留位：player -> 失效时刻。
        self._held = {}

        # 玩家当前所在通道，保证重复入队不重复占位。
        self._where = {}                 # player -> "regular"/"reserve"/"held"

        # 令牌桶：容量 1，初始 1 个令牌。
        self.tokens = 1
        self._last_ms = 0

        self.scanned = 0

    # ------------------------------------------------------------------
    # 内部维护
    # ------------------------------------------------------------------

    def _reserve_used(self):
        return len(self._reserve_q) + len(self._held)

    def _purge_held(self, now_ms):
        stale = [p for p, deadline in self._held.items() if deadline < now_ms]
        for player in stale:
            del self._held[player]
            self._where.pop(player, None)

    def _drop_regular_ticket(self, ticket, dead):
        """把普通通道条目从记账结构里删掉。dead=True 表示它是转入保留位的空洞。"""
        self._regular.popleft()
        self.scanned += 1
        if dead:
            return
        player = self._owner.pop(ticket)
        del self._enqueued_at[ticket]
        del self._player_ticket[player]
        self._where.pop(player, None)
        self._live.add(ticket, -1)
        self._regular_count -= 1

    def _purge_regular(self, now_ms):
        """跳过队首空洞并清掉超时条目；入队时刻非降，超时条目必成前缀。"""
        regular = self._regular
        while regular:
            ticket = regular[0]
            enqueued_at = self._enqueued_at.get(ticket)
            if enqueued_at is None:
                # 已转入保留位的空洞。
                self._drop_regular_ticket(ticket, dead=True)
                continue
            if now_ms - enqueued_at <= self.timeout_ms:
                break
            self._drop_regular_ticket(ticket, dead=False)

    def _purge_reserve(self, now_ms):
        """清掉保留位通道队尾的失效条目（deadline 从队首到队尾递减）。"""
        queue = self._reserve_q
        while queue:
            player = queue[-1]
            deadline, enqueued_at = self._reserve_meta[player]
            if deadline >= now_ms and now_ms - enqueued_at <= self.timeout_ms:
                break
            queue.pop()
            self.scanned += 1
            del self._reserve_meta[player]
            self._where.pop(player, None)

    def _purge_all(self, now_ms):
        self._purge_held(now_ms)
        self._purge_regular(now_ms)
        self._purge_reserve(now_ms)

    def _reserve_position(self, player):
        position = 1
        for name in self._reserve_q:
            if name == player:
                return position
            position += 1
        return 0

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def enqueue(self, player, now_ms):
        """入队；已在等的玩家不重复占位（位置不变），返回入队后的位置。

        持有悬空保留位的玩家视作重连：插到保留位通道队首。
        """
        self._purge_all(now_ms)
        where = self._where.get(player)
        if where == "reserve":
            return self._reserve_position(player)
        if where == "regular":
            ticket = self._player_ticket[player]
            return len(self._reserve_q) + self._live.prefix_sum(ticket)
        if where == "held":
            deadline = self._held.pop(player)
            self._where[player] = "reserve"
            self._reserve_meta[player] = (deadline, now_ms)
            self._reserve_q.appendleft(player)
            return 1

        ticket = self._next_ticket
        self._next_ticket += 1
        self._live.ensure(ticket)
        self._regular.append(ticket)
        self._owner[ticket] = player
        self._enqueued_at[ticket] = now_ms
        self._player_ticket[player] = ticket
        self._live.add(ticket, 1)
        self._regular_count += 1
        self._where[player] = "regular"
        return len(self._reserve_q) + self._regular_count

    def keep_reserve(self, player, now_ms):
        """给玩家留保留位：在队玩家立即提到保留位通道队首。

        不在队列的玩家占一个悬空位，等其 ``enqueue`` 重连。超出
        ``reserve_slots`` 或重复保留无效时返回 ``False``。
        """
        self._purge_all(now_ms)
        where = self._where.get(player)
        if where in ("held", "reserve"):
            return True
        if self._reserve_used() >= self.reserve_slots:
            return False
        deadline = now_ms + self.timeout_ms
        if where == "regular":
            ticket = self._player_ticket[player]
            del self._owner[ticket]
            del self._enqueued_at[ticket]
            del self._player_ticket[player]
            self._live.add(ticket, -1)
            self._regular_count -= 1
        self._where[player] = "reserve"
        self._reserve_meta[player] = (deadline, now_ms)
        self._reserve_q.appendleft(player)
        return True

    def _refill(self, now_ms):
        """按 elapsed_ms * rate_per_min / 60000 整除补充，封顶 1。"""
        elapsed = now_ms - self._last_ms
        self._last_ms = now_ms
        if elapsed > 0:
            gained = elapsed * self.rate_per_min // 60000
            if gained:
                self.tokens = min(1, self.tokens + gained)

    def poll(self, now_ms):
        """令牌够 1 个才放行并扣一个；保留位通道优先，否则普通通道队首。"""
        self._purge_all(now_ms)
        self._refill(now_ms)
        if self.tokens < 1:
            return None

        if self._reserve_q:
            player = self._reserve_q.popleft()
            del self._reserve_meta[player]
            self._where.pop(player, None)
            self.tokens -= 1
            return player

        regular = self._regular
        while regular:
            ticket = regular.popleft()
            self.scanned += 1
            if ticket not in self._owner:
                continue  # 防御：理论上已被 _purge_regular 清掉的空洞。
            player = self._owner.pop(ticket)
            del self._enqueued_at[ticket]
            del self._player_ticket[player]
            self._where.pop(player, None)
            self._live.add(ticket, -1)
            self._regular_count -= 1
            self.tokens -= 1
            return player
        return None

    def size(self, now_ms):
        """还在等的人数（超时的不算）。"""
        self._purge_all(now_ms)
        return len(self._reserve_q) + self._regular_count

    def position(self, player, now_ms):
        """从队首算起的位置（1 起），不在等返回 0；不做全表扫描。"""
        self._purge_all(now_ms)
        where = self._where.get(player)
        if where == "reserve":
            self.scanned += 1
            return self._reserve_position(player)
        if where == "regular":
            ticket = self._player_ticket[player]
            self.scanned += 1
            return len(self._reserve_q) + self._live.prefix_sum(ticket)
        return 0

    def scanned_count(self):
        return self.scanned
