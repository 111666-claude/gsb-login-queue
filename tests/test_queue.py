import random
import time
import unittest

from loginqueue.core import LoginQueue


class RateLimitTest(unittest.TestCase):
    def test_initial_token_only_one(self):
        queue = LoginQueue(rate_per_min=60)
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        self.assertEqual(queue.poll(0), "a")
        self.assertIsNone(queue.poll(0))

    def test_refill_needs_full_interval(self):
        queue = LoginQueue(rate_per_min=60)
        for name in "abcd":
            queue.enqueue(name, 0)
        self.assertEqual(queue.poll(0), "a")
        self.assertIsNone(queue.poll(999))
        self.assertEqual(queue.poll(1000), "b")

    def test_refill_scales_with_rate(self):
        queue = LoginQueue(rate_per_min=120)
        for name in "abcd":
            queue.enqueue(name, 0)
        self.assertEqual(queue.poll(0), "a")
        self.assertIsNone(queue.poll(499))
        self.assertEqual(queue.poll(500), "b")
        self.assertIsNone(queue.poll(999))
        self.assertEqual(queue.poll(1000), "c")

    def test_remainder_carries_over(self):
        queue = LoginQueue(rate_per_min=1)
        queue.enqueue("a", 0)
        self.assertEqual(queue.poll(0), "a")
        queue.enqueue("b", 1)
        # 半分钟余数结转，再来半分钟才凑够一个令牌。
        self.assertIsNone(queue.poll(30000))
        self.assertEqual(queue.poll(60000), "b")

    def test_bucket_cap_is_one(self):
        queue = LoginQueue(rate_per_min=60, timeout_ms=10**9)
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        # 等再久也只攒 1 个令牌。
        self.assertEqual(queue.poll(600000), "a")
        self.assertIsNone(queue.poll(600000))

    def test_poll_without_token_does_not_touch_queue(self):
        queue = LoginQueue(rate_per_min=60)
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        self.assertEqual(queue.poll(0), "a")
        self.assertIsNone(queue.poll(0))
        self.assertEqual(queue.size(1000), 1)
        self.assertEqual(queue.position("b", 1000), 1)


class ReserveTest(unittest.TestCase):
    def test_reserved_reconnect_goes_first(self):
        queue = LoginQueue(reserve_slots=2, timeout_ms=60000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 100)
        self.assertTrue(queue.keep_reserve("b", 100))
        self.assertEqual(queue.poll(5000), "b")
        self.assertEqual(queue.poll(6000), "a")

    def test_reserve_slots_are_capped(self):
        queue = LoginQueue(reserve_slots=1)
        self.assertTrue(queue.keep_reserve("a", 0))
        self.assertFalse(queue.keep_reserve("b", 1))
        # a 的保留位过期后，名额释放，b 才能占。
        self.assertTrue(queue.keep_reserve("b", 60001))

    def test_waiting_for_reconnect_does_not_block_head(self):
        queue = LoginQueue(timeout_ms=60000)
        queue.enqueue("a", 0)
        self.assertTrue(queue.keep_reserve("b", 100))
        # b 还没重连，不该卡在队首前面。
        self.assertEqual(queue.poll(5000), "a")
        queue.enqueue("b", 6000)
        self.assertEqual(queue.poll(7000), "b")

    def test_expired_reserve_loses_priority(self):
        queue = LoginQueue(timeout_ms=2000, rate_per_min=60000)
        # b 断线先占保留位，超时窗口过了才重连：保留位已失效，不再插队。
        self.assertTrue(queue.keep_reserve("b", 0))
        queue.enqueue("a", 1600)
        queue.enqueue("b", 1700)
        self.assertEqual(queue.poll(2001), "a")
        self.assertEqual(queue.poll(2002), "b")

    def test_reserve_priority_follows_first_keep_order(self):
        queue = LoginQueue(reserve_slots=2, rate_per_min=60000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        queue.enqueue("c", 0)
        queue.keep_reserve("b", 0)
        queue.keep_reserve("c", 1)
        queue.keep_reserve("b", 2)  # 续期不改变先后
        self.assertEqual(queue.poll(3), "b")
        self.assertEqual(queue.poll(4), "c")
        self.assertEqual(queue.poll(5), "a")


class TimeoutTest(unittest.TestCase):
    def test_size_counts_only_waiting(self):
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 5000)
        self.assertEqual(queue.size(6000), 1)

    def test_timed_out_entry_leaves_the_queue(self):
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 500)
        self.assertEqual(queue.size(1001), 1)
        self.assertEqual(queue.position("a", 1001), 0)
        self.assertEqual(queue.position("b", 1001), 1)

    def test_timed_out_player_can_enqueue_again(self):
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        self.assertEqual(queue.size(1001), 0)
        queue.enqueue("a", 2000)
        self.assertEqual(queue.size(2000), 1)
        self.assertEqual(queue.position("a", 2000), 1)


class IdempotentEnqueueTest(unittest.TestCase):
    def test_duplicate_enqueue_keeps_one_slot(self):
        queue = LoginQueue()
        queue.enqueue("a", 0)
        queue.enqueue("b", 0)
        self.assertEqual(queue.enqueue("a", 0), 1)
        self.assertEqual(queue.size(0), 2)
        self.assertEqual(queue.poll(0), "a")
        self.assertEqual(queue.poll(1000), "b")


class PositionCostTest(unittest.TestCase):
    def test_sample_scan_budget(self):
        queue = LoginQueue()
        for index in range(3000):
            queue.enqueue("p-%d" % index, 0)
        for _ in range(3000):
            queue.position("p-2999", 0)
        self.assertLessEqual(queue.scanned_count(), 3000)

    def test_scanned_does_not_grow_with_queue_length(self):
        counts = []
        for size in (1000, 10000):
            queue = LoginQueue()
            for index in range(size):
                queue.enqueue("p-%d" % index, 0)
            before = queue.scanned_count()
            for _ in range(100):
                queue.position("p-%d" % (size - 1), 0)
            counts.append(queue.scanned_count() - before)
        self.assertEqual(counts[0], counts[1])

    def test_scale_500k_enqueue_100k_lookups(self):
        queue = LoginQueue(timeout_ms=10**12)
        n = 500_000
        for index in range(n):
            queue.enqueue("p-%d" % index, index)
        start = time.perf_counter()
        for index in range(100_000):
            self.assertEqual(queue.position("p-%d" % (n - 1), n + index), n)
        elapsed = time.perf_counter() - start
        self.assertEqual(queue.scanned_count(), 100_000)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(queue.size(n + 100_000), n)


class DeterminismTest(unittest.TestCase):
    def test_same_sequence_same_result(self):
        def run():
            queue = LoginQueue(rate_per_min=60000, reserve_slots=2, timeout_ms=5000)
            out = []
            for now, action in [
                (0, ("enqueue", "a")), (10, ("enqueue", "b")),
                (20, ("enqueue", "c")), (30, ("keep", "c")),
                (40, ("poll",)), (1000, ("poll",)),
                (2000, ("enqueue", "a")), (3000, ("enqueue", "d")),
                (3000, ("keep", "d")), (4000, ("poll",)),
                (6000, ("poll",)), (6001, ("size",)),
                (7000, ("poll",)), (12000, ("poll",)),
            ]:
                kind = action[0]
                if kind == "enqueue":
                    out.append(queue.enqueue(action[1], now))
                elif kind == "keep":
                    out.append(queue.keep_reserve(action[1], now))
                elif kind == "poll":
                    out.append(queue.poll(now))
                else:
                    out.append(queue.size(now))
            return out
        self.assertEqual(run(), run())


class ReferenceQueue:
    """参考实现：朴素 list + dict，口径与 README 一致。"""

    def __init__(self, rate_per_min, reserve_slots, timeout_ms):
        self.rate_per_min = rate_per_min
        self.reserve_slots = reserve_slots
        self.timeout_ms = timeout_ms
        self.waiting = []
        self.reserves = {}
        self.tokens = 1
        self._carry_num = 0
        self._last_ms = 0

    def _purge(self, now):
        self.waiting = [
            (p, t) for p, t in self.waiting if now - t <= self.timeout_ms
        ]
        for player in [p for p, d in self.reserves.items() if d < now]:
            del self.reserves[player]

    def enqueue(self, player, now):
        self._purge(now)
        if not any(p == player for p, _ in self.waiting):
            self.waiting.append((player, now))
        return self.position(player, now)

    def keep_reserve(self, player, now):
        self._purge(now)
        deadline = now + self.timeout_ms
        if player in self.reserves:
            self.reserves[player] = deadline
            return True
        if len(self.reserves) >= self.reserve_slots:
            return False
        self.reserves[player] = deadline
        return True

    def _refill(self, now):
        elapsed = max(0, now - self._last_ms)
        self._last_ms = max(self._last_ms, now)
        self._carry_num += elapsed * self.rate_per_min
        added = self._carry_num // 60000
        self._carry_num %= 60000
        if added:
            self.tokens = min(1, self.tokens + added)

    def poll(self, now):
        self._refill(now)
        if self.tokens < 1:
            return None
        self._purge(now)
        picked = None
        for player in self.reserves:
            if any(p == player for p, _ in self.waiting):
                picked = player
                break
        if picked is None and self.waiting:
            picked = self.waiting[0][0]
        if picked is None:
            return None
        self.tokens -= 1
        self.reserves.pop(picked, None)
        self.waiting = [(p, t) for p, t in self.waiting if p != picked]
        return picked

    def size(self, now):
        self._purge(now)
        return len(self.waiting)

    def position(self, player, now):
        self._purge(now)
        for index, (p, _) in enumerate(self.waiting):
            if p == player:
                return index + 1
        return 0


class DifferentialTest(unittest.TestCase):
    def test_random_sequences_match_reference(self):
        rng = random.Random(20261001)
        players = ["a", "b", "c", "d", "e"]
        for _ in range(200):
            rate = rng.choice([1, 30, 60, 120, 600])
            slots = rng.randint(1, 3)
            timeout = rng.choice([500, 1000, 5000])
            actual = LoginQueue(rate, slots, timeout)
            reference = ReferenceQueue(rate, slots, timeout)
            now = 0
            for _ in range(rng.randint(100, 400)):
                now += rng.choice([0, 1, 7, 100, 499, 500, 1000, 6000])
                kind = rng.choice(
                    ["enqueue"] * 5
                    + ["keep"] * 2
                    + ["poll"] * 5
                    + ["size"] * 2
                    + ["position"] * 2
                )
                player = rng.choice(players)
                if kind == "enqueue":
                    self.assertEqual(
                        actual.enqueue(player, now),
                        reference.enqueue(player, now),
                    )
                elif kind == "keep":
                    self.assertEqual(
                        actual.keep_reserve(player, now),
                        reference.keep_reserve(player, now),
                    )
                elif kind == "poll":
                    self.assertEqual(actual.poll(now), reference.poll(now))
                elif kind == "size":
                    self.assertEqual(actual.size(now), reference.size(now))
                else:
                    self.assertEqual(
                        actual.position(player, now),
                        reference.position(player, now),
                    )
                self.assertEqual(actual.size(now), reference.size(now))


if __name__ == "__main__":
    unittest.main()
