import argparse
import getpass
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import Owner
from app.security import HASHER


def main(argv=None):
    parser = argparse.ArgumentParser(description="ProbeFlow 中文访谈工具")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-admin", help="安全初始化管理员密码")
    start = sub.add_parser("start", help="启动应用")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8765)
    sub.add_parser("check", help="检查配置与运行环境")
    sub.add_parser("backup", help="一致性备份并核验")
    restore = sub.add_parser("restore", help="向独立空目录恢复，过滤已删除资料")
    restore.add_argument("backup_path", type=Path)
    restore.add_argument("target_dir", type=Path)
    sub.add_parser("cleanup", help="按安全条件清理临时文件")
    args = parser.parse_args(argv)
    load_dotenv(".env", override=False)
    settings = Settings()
    if args.command == "start":
        import uvicorn

        uvicorn.run(
            "app.main:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
            proxy_headers=False,
        )
        return 0
    database = Database(settings)
    database.initialize()
    if args.command == "init-admin":
        with database.read() as db:
            if db.scalar(select(Owner)):
                print("管理员已初始化；不会覆盖现有密码。")
                return 1
        first = getpass.getpass("设置管理员密码（至少12个字符）：")
        second = getpass.getpass("再次输入：")
        if len(first) < 12 or first != second:
            print("密码过短或两次输入不一致，未保存。")
            return 1
        with database.transaction() as db:
            if db.scalar(select(Owner)):
                print("管理员已存在，未覆盖。")
                return 1
            db.add(Owner(password_hash=HASHER.hash(first)))
        print("管理员已初始化。运行 ./scripts/probeflow start 后打开 http://localhost:8765")
        return 0
    from app.storage import Storage

    storage = Storage(database, settings)
    storage.reconcile_deletions()
    if args.command == "backup":
        result = storage.backup()
    elif args.command == "restore":
        result = storage.restore(args.backup_path, args.target_dir)
    elif args.command == "cleanup":
        result = storage.cleanup()
    else:
        import os
        import platform
        import shutil
        from dotenv import dotenv_values

        credential_values = {**dotenv_values(".env"), **os.environ}

        result = {
            "version": "1.1.0",
            "python": platform.python_version(),
            "mode": settings.mode,
            "data_dir": str(settings.data_dir),
            "backup_dir": str(settings.backups),
            "retention": settings.retention_policy,
            "ffmpeg_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "roles": {
                role: {
                    "model": getattr(settings, f"{role}_model"),
                    "credential_configured": bool(
                        credential_values.get(getattr(settings, f"{role}_key_env"))
                    ),
                }
                for role in ("asr", "interview", "tts", "report")
            },
            "storage": storage.diagnostics(),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        # Settings validation can contain input values; never print arbitrary exception dumps.
        from app.errors import AppError

        print(
            exc.message if isinstance(exc, AppError) else "操作失败，请检查本机配置、目录权限和运行说明。",
            file=sys.stderr,
        )
        raise SystemExit(1)
