#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成测试阶段动态访问码（卖家 / 管理员用）。

用法：
  python gentoken.py --days 7 --note "测试用户A"
  python gentoken.py --hours 24
  python gentoken.py                      # 默认 7 天

注意：本脚本与 proxy.py 必须在同一目录，且共用同一个 ACCESS_SECRET
（默认一致；生产环境请用环境变量 ACCESS_SECRET 覆盖），否则签发的码服务端校验不通过。
"""
import argparse
import time
from access import make_access_token


def main():
    ap = argparse.ArgumentParser(description="生成测试阶段动态访问码（基于时间，有时效）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--hours", type=int, default=None, help="有效时长（小时）")
    g.add_argument("--days", type=float, default=None, help="有效时长（天）")
    ap.add_argument("--note", default="", help="备注，如 测试用户A / 渠道X / 有效期至X")
    args = ap.parse_args()

    if args.days is not None:
        hours = int(round(args.days * 24))
    elif args.hours is not None:
        hours = args.hours
    else:
        hours = 168  # 默认 7 天

    code = make_access_token(exp_hours=hours, note=args.note)
    exp_ts = int(time.time()) + hours * 3600
    print("动态访问码：", code)
    print("有效期至：  ", time.ctime(exp_ts))
    print("剩余时长：  %.1f 小时（约 %.1f 天）" % (hours, hours / 24))
    if args.note:
        print("备注：      ", args.note)
    print("（把这段码发给受邀用户，他们在 /tool 页面输入即可解锁；过期联系你重新生成）")


if __name__ == "__main__":
    main()
