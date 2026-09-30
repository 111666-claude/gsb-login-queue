"""命令行入口：跑登录排队样例。"""

import argparse

from .core import LoginQueue


def build_parser():
    parser = argparse.ArgumentParser(prog="login-queue", description="登录排队")
    parser.add_argument("--sample", default="rate", help="rate / reserve / timeout / scan")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.sample == "rate":
        queue = LoginQueue(rate_per_min=60)
        queue.enqueue("p", 0)
        queue.enqueue("z", 0)
        # 1 秒最多放行 1 个人：同一时刻连问两次只能过第一个。
        first = queue.poll(0)
        second = queue.poll(0)
        print("first=%s second=%s" % (first, second))
    elif args.sample == "reserve":
        queue = LoginQueue()
        queue.enqueue("a", 0)
        queue.enqueue("b", 100)
        queue.keep_reserve("b", 100)
        print("picked=%s" % queue.poll(5000))
    elif args.sample == "timeout":
        queue = LoginQueue(timeout_ms=1000)
        queue.enqueue("a", 0)
        queue.enqueue("b", 5000)
        print("size=%d" % queue.size(6000))
    elif args.sample == "scan":
        queue = LoginQueue()
        for index in range(3000):
            queue.enqueue("p-%d" % index, 0)
        for _ in range(3000):
            queue.position("p-2999", 0)
        print("scanned=%d" % queue.scanned_count())
    else:
        raise SystemExit("需要 --sample rate|reserve|timeout|scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
