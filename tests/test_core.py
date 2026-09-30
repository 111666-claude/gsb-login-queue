import random
import unittest

from loginqueue.core import LoginQueue


class EnqueueTest(unittest.TestCase):
    def test_first_enqueue_is_first(self):
        self.assertEqual(LoginQueue().enqueue("a", 0), 1)

    def test_position_of_head(self):
        queue = LoginQueue()
        queue.enqueue("a", 0)
        self.assertEqual(queue.position("a", 0), 1)

    def test_position_of_missing_is_zero(self):
        self.assertEqual(LoginQueue().position("z", 0), 0)


class PollTest(unittest.TestCase):
    def test_poll_empty_returns_none(self):
        self.assertIsNone(LoginQueue().poll(0))

    def test_poll_returns_head(self):
        queue = LoginQueue()
        queue.enqueue("a", 0)
        self.assertEqual(queue.poll(0), "a")


class TokenBucketTest(unittest.TestCase):
    def test_same_instant_only_one_passes(self):
        queue = LoginQueue(rate_per_min=60)
        queue.enqueue("p", 0)
        queue.enqueue("z", 0)
        self.assertEqual(queue.poll(0), "p")
        self.assertIsNone(queue.poll(0))

    def test_token_refills_by_elapsed_time(self):
        queue = LoginQueue(rate_per_min=60)
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        self.assertEqual(queue.poll(0), "a")
        self.assertIsNone(queue.poll(999))
        # 失败的 poll 也会刷新基准时刻：从 999 再走 1000ms 才整除出 1 个。
        self.assertEqual(queue.poll(2000), "b")

    def test_refill_uses_floor_division(self):
        # 60/分 => 每 1000ms 才整除出 1 个令牌，999ms 不够。
        queue = LoginQueue(rate_per_min=60)
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        queue.enqueue("c", 0)
        self.assertEqual(queue.poll(0), "a")
        self.assertIsNone(queue.poll(999))
        self.assertEqual(queue.poll(1999), "b")
        self.assertIsNone(queue.poll(2000))

    def test_bucket_capacity_is_one(self):
        queue = LoginQueue(rate_per_min=60, timeout_ms=10 ** 12)
        for index in range(3):
            queue.enqueue("p%d" % index, 0)
        self.assertEqual(queue.poll(0), "p0")
        # 空闲再久也只攒 1 个令牌。
        long_idle = 10 ** 9
        self.assertEqual(queue.poll(long_idle), "p1")
        self.assertIsNone(queue.poll(long_idle))

    def test_release_never_exceeds_token_budget(self):
        rate = 120
        queue = LoginQueue(rate_per_min=rate)
        for index in range(500):
            queue.enqueue("p%d" % index, 0)
        admitted = []  # (now, player)
        for now in range(0, 60000 * 3, 137):
            player = queue.poll(now)
            if player is not None:
                admitted.append((now, player))
        # 任意 60 秒窗口放行数不超过 rate + 初始令牌。
        for start, _ in admitted:
            window = [t for t, _ in admitted if start <= t < start + 60000]
            self.assertLessEqual(len(window), rate + 1)


class ReserveTest(unittest.TestCase):
    def test_reserved_waiting_player_jumps_queue(self):
        queue = LoginQueue()
        queue.enqueue("a", 0)
        queue.enqueue("b", 100)
        self.assertTrue(queue.keep_reserve("b", 100))
        self.assertEqual(queue.poll(5000), "b")

    def test_held_player_reconnects_to_front(self):
        queue = LoginQueue()
        self.assertTrue(queue.keep_reserve("x", 0))
        queue.enqueue("a", 10)
        queue.enqueue("b", 20)
        self.assertEqual(queue.enqueue("x", 30), 1)
        self.assertEqual(queue.poll(40), "x")
        self.assertEqual(queue.poll(50000), "a")

    def test_reserve_slots_cap(self):
        queue = LoginQueue(reserve_slots=1)
        self.assertTrue(queue.keep_reserve("a", 0))
        self.assertFalse(queue.keep_reserve("b", 0))

    def test_expired_reserve_frees_slot(self):
        queue = LoginQueue(reserve_slots=1, timeout_ms=1000)
        self.assertTrue(queue.keep_reserve("a", 0))
        self.assertFalse(queue.keep_reserve("b", 500))
        self.assertTrue(queue.keep_reserve("b", 2000))

    def test_expired_reserved_player_is_dropped(self):
        queue = LoginQueue(timeout_ms=2000)
        queue.enqueue("a", 0)
        queue.keep_reserve("a", 0)
        queue.enqueue("b", 1000)
        # a 的保留位已失效，轮到 b。
        self.assertEqual(queue.poll(2500), "b")

    def test_reserve_full_does_not_eject_waiting_player(self):
        queue = LoginQueue(reserve_slots=1)
        queue.enqueue("a", 0)
        self.assertTrue(queue.keep_reserve("x", 0))
        self.assertFalse(queue.keep_reserve("a", 0))
        self.assertEqual(queue.position("a", 0), 2)


class TimeoutTest(unittest.TestCase):
    def test_size_counts_only_waiting(self):
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 5000)
        self.assertEqual(queue.size(6000), 1)

    def test_expired_player_has_no_position(self):
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        self.assertEqual(queue.position("a", 2000), 0)

    def test_poll_skips_expired_head(self):
        queue = LoginQueue(timeout_ms=2000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 500)
        self.assertEqual(queue.poll(2001), "b")

    def test_expired_player_can_reenqueue(self):
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 5000)
        self.assertEqual(queue.enqueue("a", 5000), 2)
        self.assertEqual(queue.position("a", 5000), 2)


class IdempotenceTest(unittest.TestCase):
    def test_duplicate_enqueue_keeps_position(self):
        queue = LoginQueue()
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        self.assertEqual(queue.enqueue("a", 0), 1)
        self.assertEqual(queue.size(0), 2)
        self.assertEqual(queue.position("a", 0), 1)
        self.assertEqual(queue.position("b", 0), 2)

    def test_same_sequence_same_result(self):
        script = [("enqueue", "a", 0), ("enqueue", "b", 10),
                  ("keep_reserve", "b", 20), ("poll", None, 30),
                  ("enqueue", "c", 40), ("poll", None, 50),
                  ("size", None, 60)]

        def run():
            queue = LoginQueue(rate_per_min=60000)
            out = []
            for op, player, now in script:
                if op == "enqueue":
                    out.append(queue.enqueue(player, now))
                elif op == "keep_reserve":
                    out.append(queue.keep_reserve(player, now))
                elif op == "poll":
                    out.append(queue.poll(now))
                else:
                    out.append(queue.size(now))
            return out

        self.assertEqual(run(), run())


class PositionCostTest(unittest.TestCase):
    def test_position_query_is_constant_cost(self):
        for length in (100, 10000):
            queue = LoginQueue()
            for index in range(length):
                queue.enqueue("p%d" % index, 0)
            before = queue.scanned_count()
            queue.position("p%d" % (length - 1), 0)
            self.assertLessEqual(queue.scanned_count() - before, 1)

    def test_scanned_grows_with_queries_not_queue_length(self):
        queue = LoginQueue()
        for index in range(3000):
            queue.enqueue("p-%d" % index, 0)
        for _ in range(3000):
            queue.position("p-2999", 0)
        self.assertLessEqual(queue.scanned_count(), 3000)

    def test_large_queue_stays_fast(self):
        queue = LoginQueue()
        total = 100000
        for index in range(total):
            queue.enqueue("p%d" % index, 0)
        before = queue.scanned_count()
        for _ in range(50000):
            self.assertEqual(queue.position("p%d" % (total - 1), 0), total)
        self.assertEqual(queue.scanned_count() - before, 50000)
        self.assertEqual(queue.size(0), total)


class ReferenceQueue:
    """参考实现：按时间补令牌 + 保留位优先 + 超时清理，用列表直白模拟。"""

    def __init__(self, rate_per_min=60, reserve_slots=2, timeout_ms=60000):
        self.rate_per_min = rate_per_min
        self.reserve_slots = reserve_slots
        self.timeout_ms = timeout_ms
        self.regular = []   # [(player, enqueued_at)]，队首在前
        self.reserve = []   # [(player, deadline, enqueued_at)]，最新保留位在前
        self.held = {}      # player -> deadline
        self.tokens = 1
        self.last = 0

    def _purge(self, now):
        self.held = {p: d for p, d in self.held.items() if d >= now}
        self.regular = [(p, t) for p, t in self.regular
                        if now - t <= self.timeout_ms]
        self.reserve = [(p, d, t) for p, d, t in self.reserve
                        if d >= now and now - t <= self.timeout_ms]

    def enqueue(self, player, now):
        self._purge(now)
        for index, (p, _d, _t) in enumerate(self.reserve):
            if p == player:
                return index + 1
        for index, (p, _t) in enumerate(self.regular):
            if p == player:
                return len(self.reserve) + index + 1
        if player in self.held:
            deadline = self.held.pop(player)
            self.reserve.insert(0, (player, deadline, now))
            return 1
        self.regular.append((player, now))
        return len(self.reserve) + len(self.regular)

    def keep_reserve(self, player, now):
        self._purge(now)
        if player in self.held:
            return True
        if any(p == player for p, _d, _t in self.reserve):
            return True
        if len(self.reserve) + len(self.held) >= self.reserve_slots:
            return False
        deadline = now + self.timeout_ms
        for index, (p, _t) in enumerate(self.regular):
            if p == player:
                del self.regular[index]
                break
        self.reserve.insert(0, (player, deadline, now))
        return True

    def poll(self, now):
        self._purge(now)
        elapsed = now - self.last
        self.last = now
        if elapsed > 0:
            gained = elapsed * self.rate_per_min // 60000
            if gained:
                self.tokens = min(1, self.tokens + gained)
        if self.tokens < 1:
            return None
        if self.reserve:
            player, _d, _t = self.reserve.pop(0)
            self.tokens -= 1
            return player
        if self.regular:
            player, _t = self.regular.pop(0)
            self.tokens -= 1
            return player
        return None

    def size(self, now):
        self._purge(now)
        return len(self.reserve) + len(self.regular)

    def position(self, player, now):
        self._purge(now)
        for index, (p, _d, _t) in enumerate(self.reserve):
            if p == player:
                return index + 1
        for index, (p, _t) in enumerate(self.regular):
            if p == player:
                return len(self.reserve) + index + 1
        return 0


class DifferentialTest(unittest.TestCase):
    """随机序列对照：实现与参考每次放行结果与队列长度必须一致。"""

    def test_matches_reference_on_random_sequences(self):
        rng = random.Random(20261001)
        players = ["p%d" % index for index in range(8)]
        for trial in range(200):
            rate = rng.choice([30, 60, 120, 600])
            slots = rng.choice([1, 2, 3])
            timeout = rng.choice([100, 1000, 5000])
            actual = LoginQueue(rate_per_min=rate, reserve_slots=slots,
                                timeout_ms=timeout)
            expect = ReferenceQueue(rate_per_min=rate, reserve_slots=slots,
                                    timeout_ms=timeout)
            now = 0
            for step in range(rng.randint(20, 60)):
                now += rng.randint(0, 3000)
                op = rng.choice(["enqueue", "enqueue", "keep_reserve",
                                 "poll", "poll", "size", "position"])
                if op == "enqueue":
                    player = rng.choice(players)
                    got = actual.enqueue(player, now)
                    want = expect.enqueue(player, now)
                elif op == "keep_reserve":
                    player = rng.choice(players)
                    got = actual.keep_reserve(player, now)
                    want = expect.keep_reserve(player, now)
                elif op == "poll":
                    got = actual.poll(now)
                    want = expect.poll(now)
                elif op == "size":
                    got = actual.size(now)
                    want = expect.size(now)
                else:
                    player = rng.choice(players)
                    got = actual.position(player, now)
                    want = expect.position(player, now)
                self.assertEqual(got, want,
                                 "trial=%d step=%d op=%s now=%d" %
                                 (trial, step, op, now))
                self.assertEqual(actual.size(now), expect.size(now),
                                 "size drift trial=%d step=%d" % (trial, step))


if __name__ == "__main__":
    unittest.main()
