#!/usr/bin/env python3
"""Local operator audit. No model path, default DB, or automatic human verdicts."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.travel_agent.uncertainty_store import UncertaintyStore
from packages.travel_agent.uncertainty_audit import UncertaintyAudit


def human_confirmation():
    if not sys.stdin.isatty():
        raise ValueError('人工判定只接受本机操作员交互输入，不接受模型/管道自动标注')
    if input('请人工阅读原始快照；确认身份与判定责任，输入「我已核对原始证据」：').strip() != '我已核对原始证据':
        raise ValueError('未提交人工判定')
    return {'id': f'local-operator:{getpass.getuser()}', 'kind': 'human', 'authenticated': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Explicit private uncertainty/trip SQLite path')
    parser.add_argument('--output', type=Path, help='Private JSON artifact (mode 0600; refuses overwrite)')
    parser.add_argument('command', choices=['sample-week', 'sample-recheck', 'report', 'export', 'annotate', 'repair'])
    parser.add_argument('--batch')
    parser.add_argument('--entry')
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error('数据库必须已存在；不会自动创建空生产数据库')
    audit = UncertaintyAudit(UncertaintyStore(args.db))
    if args.command == 'sample-week':
        value = audit.sample_week()
    elif args.command == 'sample-recheck':
        value = audit.sample_recheck()
    elif args.command == 'report':
        value = audit.weekly_report()
    elif args.command == 'export':
        if not args.batch or not args.output:
            parser.error('export需要--batch和私密--output，避免快照直接输出到日志')
        value = audit.get_batch(args.batch)
    elif args.command == 'annotate':
        if not args.batch or not args.entry:
            parser.error('annotate需要--batch和--entry')
        actor = human_confirmation()
        answer = input('证据是否准确支持判定时点的断言？输入 yes / no：').strip()
        if answer not in ('yes', 'no'):
            raise ValueError('未知不能当正确；保留待判')
        reason = input('写出支持或反驳的具体原文/条件：').strip()
        value = audit.annotate(args.batch, args.entry, correct=answer == 'yes', reason=reason, actor=actor)
    else:
        actor = human_confirmation()
        reference = input('修复证据文件/版本引用：').strip()
        version = input('新核验器版本：').strip()
        value = audit.record_repair(repair_ref=reference, verifier_version=version, actor=actor)
    if args.output:
        encoded = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(encoded)
    else:
        # Summary logs must not contain evidence snapshots across private trips.
        print(json.dumps({k: v for k, v in value.items() if k != 'items'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
