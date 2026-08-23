"""sidecar CLI 入口：``python -m vocalis.voice.sidecar``。

参数与 :class:`vocalis.config.SidecarConfig` 的默认值保持一致
（host 127.0.0.1 / port 8765），这样主框架的默认 ``base_url`` 开箱即用。

鉴权：``--token`` 优先，缺省读环境变量 ``VOCALIS_SIDECAR_TOKEN``；
两者都未设置时无鉴权运行（仅建议本机回环监听场景）。

日志走 basicConfig：sidecar 是独立进程，主框架的日志配置管不到这里。
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from vocalis.voice.sidecar.server import create_app

logger = logging.getLogger("vocalis.voice.sidecar.__main__")

#: token 的环境变量来源（CLI --token 优先于此）。
TOKEN_ENV_VAR = "VOCALIS_SIDECAR_TOKEN"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vocalis.voice.sidecar",
        description=(
            "Vocalis IndexTTS sidecar 服务（进程隔离的克隆合成服务）；"
            "无 GPU 时以 engine='none' 降级模式启动"
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认 127.0.0.1；局域网部署改 0.0.0.0）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="监听端口（默认 8765，与 SidecarConfig.base_url 一致）",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="IndexTTS 权重目录（含 config.yaml；默认 checkpoints/）",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="注册表数据目录（默认 ~/.vocalis/sidecar）",
    )
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "Bearer token 鉴权（所有端点要求 Authorization: Bearer <token> "
            f"或 X-Sidecar-Token 头）；默认读环境变量 {TOKEN_ENV_VAR}，"
            "未设置则无鉴权（仅建议本机回环监听）"
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 INFO）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """解析参数并启动 uvicorn 服务（阻塞直至进程退出）。"""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = args.token if args.token is not None else os.environ.get(TOKEN_ENV_VAR)
    if not token:
        token = None
    app = create_app(
        state=None,  # None -> 启动时自动探测（无 GPU 则 engine="none" 降级）
        registry_path=args.data_dir,
        model_dir=args.model_dir,
        token=token,
    )
    logger.info("sidecar 启动: http://%s:%s", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
