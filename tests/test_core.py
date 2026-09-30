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


if __name__ == "__main__":
    unittest.main()
