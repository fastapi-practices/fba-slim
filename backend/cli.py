import asyncio
import re
import secrets
import subprocess
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

import anyio
import cappa
import granian

from cappa.output import error_format
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from watchfiles import Change, PythonFilter

from backend import __version__
from backend.common.enums import DataBaseType
from backend.common.model import MappedBase
from backend.core.conf import settings
from backend.core.path_conf import (
    BASE_PATH,
    ENV_EXAMPLE_FILE_PATH,
    ENV_FILE_PATH,
    LOCALE_DIR,
    MYSQL_SCRIPT_DIR,
    POSTGRESQL_SCRIPT_DIR,
    RELOAD_LOCK_FILE,
)
from backend.database.db import (
    async_db_session,
    create_database_async_engine,
    create_database_async_session,
    get_database_url,
)
from backend.database.redis import RedisCli, redis_client
from backend.plugin.core import (
    get_plugins,
)
from backend.plugin.sql import build_sql_filename, get_plugin_sql
from backend.utils.console import console
from backend.utils.sql_parser import parse_sql_script

_OUTPUT_HELP: Final = "\n更多信息，尝试 '[cyan]--help[/]'"


class CustomReloadFilter(PythonFilter):
    """自定义重载过滤器"""

    def __init__(self) -> None:
        self.extra_extensions = ('.json', '.yaml', '.yml')
        super().__init__(extra_extensions=self.extra_extensions)

    def __call__(self, change: Change, path: str) -> bool:
        if RELOAD_LOCK_FILE.exists():
            return False

        file_path = Path(path).resolve()
        if file_path.suffix in self.extra_extensions and not file_path.is_relative_to(LOCALE_DIR.resolve()):
            return False

        return super().__call__(change, path)


def setup_env_file() -> bool:
    """交互式配置并生成 .env 环境变量文件"""
    if not ENV_EXAMPLE_FILE_PATH.exists():
        console.caution('.env.example 文件不存在')
        return False

    try:
        env_content = Path(ENV_EXAMPLE_FILE_PATH).read_text(encoding='utf-8')
        console.note('配置数据库连接信息...')
        db_type = Prompt.ask('数据库类型', choices=['mysql', 'postgresql'], default='postgresql')
        db_host = Prompt.ask('数据库主机', default='127.0.0.1')
        db_port = Prompt.ask('数据库端口', default='5432' if db_type == 'postgresql' else '3306')
        db_user = Prompt.ask('数据库用户名', default='postgres' if db_type == 'postgresql' else 'root')
        db_password = Prompt.ask('数据库密码', password=True, default='123456')

        console.note('配置 Redis 连接信息...')
        redis_host = Prompt.ask('Redis 主机', default='127.0.0.1')
        redis_port = Prompt.ask('Redis 端口', default='6379')
        redis_password = Prompt.ask('Redis 密码（留空表示无密码）', password=True, default='')
        redis_db = Prompt.ask('Redis 数据库编号', default='0')

        console.info('生成 Token 密钥...')
        token_secret = secrets.token_urlsafe(32)

        console.info('写入 .env 文件...')
        env_content = env_content.replace("DATABASE_TYPE='postgresql'", f"DATABASE_TYPE='{db_type}'")
        settings.DATABASE_TYPE = db_type
        env_content = env_content.replace("DATABASE_HOST='127.0.0.1'", f"DATABASE_HOST='{db_host}'")
        settings.DATABASE_HOST = db_host
        env_content = env_content.replace('DATABASE_PORT=5432', f'DATABASE_PORT={db_port}')
        settings.DATABASE_PORT = db_port
        env_content = env_content.replace("DATABASE_USER='postgres'", f"DATABASE_USER='{db_user}'")
        settings.DATABASE_USER = db_user
        env_content = env_content.replace("DATABASE_PASSWORD='123456'", f"DATABASE_PASSWORD='{db_password}'")
        settings.DATABASE_PASSWORD = db_password
        env_content = env_content.replace("REDIS_HOST='127.0.0.1'", f"REDIS_HOST='{redis_host}'")
        settings.REDIS_HOST = redis_host
        env_content = env_content.replace('REDIS_PORT=6379', f'REDIS_PORT={redis_port}')
        settings.REDIS_PORT = redis_port
        env_content = env_content.replace("REDIS_PASSWORD=''", f"REDIS_PASSWORD='{redis_password}'")
        settings.REDIS_PASSWORD = redis_password
        env_content = env_content.replace('REDIS_DATABASE=0', f'REDIS_DATABASE={redis_db}')
        settings.REDIS_DATABASE = redis_db
        env_content = re.sub(r"TOKEN_SECRET_KEY='[^']*'", f"TOKEN_SECRET_KEY='{token_secret}'", env_content)
        settings.TOKEN_SECRET_KEY = token_secret

        Path(ENV_FILE_PATH).write_text(env_content, encoding='utf-8')
        console.tip('.env 文件创建成功')
    except Exception as e:
        console.caution(f'.env 文件创建失败: {e}')
        return False
    else:
        return True


async def create_database(conn: AsyncConnection) -> bool:
    """创建或重建数据库"""
    try:
        terminate_sql = None
        if DataBaseType.mysql == settings.DATABASE_TYPE:
            check_sql = f"SHOW DATABASES LIKE '{settings.DATABASE_SCHEMA}'"
            drop_sql = f'DROP DATABASE IF EXISTS `{settings.DATABASE_SCHEMA}`'
            create_sql = (
                f'CREATE DATABASE `{settings.DATABASE_SCHEMA}` CHARACTER SET {settings.DATABASE_CHARSET} '
                f'COLLATE {settings.DATABASE_CHARSET}_unicode_ci'
            )
        else:
            check_sql = f"SELECT 1 FROM pg_database WHERE datname = '{settings.DATABASE_SCHEMA}'"
            drop_sql = f'DROP DATABASE IF EXISTS {settings.DATABASE_SCHEMA}'
            create_sql = f'CREATE DATABASE {settings.DATABASE_SCHEMA}'
            terminate_sql = (
                f'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                f"WHERE datname = '{settings.DATABASE_SCHEMA}' AND pid <> pg_backend_pid()"
            )

        result = await conn.execute(text(check_sql))
        exists = result.fetchone() is not None
        console.note(f'重建 {settings.DATABASE_SCHEMA} 数据库...')
        if exists:
            if terminate_sql:
                await conn.execute(text(terminate_sql))
            await conn.execute(text(drop_sql))
        await conn.execute(text(create_sql))
        console.tip('数据库创建成功')
    except Exception as e:
        console.caution(f'数据库创建失败: {e}')
        return False
    else:
        return True


def _build_db_config_panel_content() -> Text:
    """构建数据库配置面板内容"""
    panel_content = Text()
    panel_content.append('【数据库配置】', style='bold green')
    panel_content.append('\n\n  • 类型: ')
    panel_content.append(f'{settings.DATABASE_TYPE}', style='yellow')
    panel_content.append('\n  • 主机：')
    panel_content.append(f'{settings.DATABASE_HOST}:{settings.DATABASE_PORT}', style='yellow')
    panel_content.append('\n  • 数据库：')
    panel_content.append(f'{settings.DATABASE_SCHEMA}', style='yellow')
    panel_content.append('\n  • 主键模式：')
    panel_content.append(f'{settings.DATABASE_PK_MODE}', style='yellow')
    return panel_content


async def auto_init() -> None:
    """自动化初始化流程"""
    console.print('\n[bold cyan]步骤 1/3:[/] 配置环境变量', style='bold')
    panel_content = Text()
    panel_content.append('【环境变量配置】', style='bold green')
    panel_content.append('\n\n  • 数据库连接信息')
    panel_content.append('\n  • Redis 连接信息')
    panel_content.append('\n  • Token 密钥（自动生成）')

    console.print(Panel(panel_content, title=f'fba (v{__version__}) - 环境变量', border_style='cyan', padding=(1, 2)))
    if not setup_env_file():
        raise cappa.Exit('.env 文件配置失败', code=1)

    console.print('\n[bold cyan]步骤 2/3:[/] 数据库创建', style='bold')
    panel_content = _build_db_config_panel_content()

    console.print(Panel(panel_content, title=f'fba (v{__version__}) - 数据库', border_style='cyan', padding=(1, 2)))
    ok = Prompt.ask('即将[red]新建/重建数据库[/red]，确认继续吗？', choices=['y', 'n'], default='n')

    if ok.lower() == 'y':
        async_init_engine = create_database_async_engine(get_database_url(with_database=False))
        async with async_init_engine.connect() as conn:
            await conn.execution_options(isolation_level='AUTOCOMMIT')
            if not await create_database(conn):
                raise cappa.Exit('数据库创建失败', code=1)
    else:
        console.warning('已取消数据库操作')

    console.print('\n[bold cyan]步骤 3/3:[/] 初始化数据库表和数据', style='bold')
    async_init_engine = create_database_async_engine(get_database_url())
    async_init_db_session = create_database_async_session(async_init_engine)
    redis_init_client = RedisCli(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD,
        db=settings.REDIS_DATABASE,
    )
    await redis_init_client.init()
    async with async_init_db_session.begin() as db:
        await init(db, redis_init_client)


async def init(db: AsyncSession, redis: RedisCli) -> None:
    """交互式初始化数据库表结构和数据"""
    panel_content = _build_db_config_panel_content()
    pk_details = panel_content.from_markup(
        '[link=https://fastapi-practices.github.io/fastapi_best_architecture_docs/backend/reference/pk.html]（了解详情）[/]'
    )
    panel_content.append(pk_details)
    panel_content.append('\n\n【Redis 配置】', style='bold green')
    panel_content.append('\n\n  • 主机：')
    panel_content.append(f'{settings.REDIS_HOST}:{settings.REDIS_PORT}', style='yellow')
    panel_content.append('\n  • 数据库：')
    panel_content.append(f'{settings.REDIS_DATABASE}', style='yellow')
    plugins = get_plugins()
    panel_content.append('\n\n【已安装插件】', style='bold green')
    panel_content.append('\n\n  • ')
    if plugins:
        panel_content.append(f'{", ".join(plugins)}', style='yellow')
    else:
        panel_content.append('无', style='dim')

    console.print(Panel(panel_content, title=f'fba (v{__version__}) - 初始化', border_style='cyan', padding=(1, 2)))
    ok = Prompt.ask(
        '即将[red]新建/重建数据库表[/red]并[red]执行所有数据库脚本[/red]，确认继续吗？', choices=['y', 'n'], default='n'
    )

    if ok.lower() == 'y':
        try:
            console.note('清理 Redis 缓存')
            for prefix in [
                settings.JWT_USER_REDIS_PREFIX,
                settings.TOKEN_EXTRA_INFO_REDIS_PREFIX,
                settings.TOKEN_REDIS_PREFIX,
                settings.TOKEN_REFRESH_REDIS_PREFIX,
            ]:
                await redis.delete_prefix(prefix)

            console.note('重建数据库表')
            conn = await db.connection()
            await conn.run_sync(MappedBase.metadata.drop_all)
            await conn.run_sync(MappedBase.metadata.create_all)

            console.note('执行 SQL 脚本')
            sql_scripts = await get_sql_scripts()
            for sql_script in sql_scripts:
                console.note(f'正在执行：{sql_script}')
                await execute_sql_scripts(db, sql_script, is_init=True)

            console.tip('初始化成功')
            console.print('\n快试试 [bold cyan]fba run[/bold cyan] 启动服务吧~')
        except Exception as e:
            raise cappa.Exit(f'初始化失败：{e}', code=1)
    else:
        console.warning('已取消初始化操作')


def run(host: str, port: int, reload: bool, workers: int) -> None:  # noqa: FBT001
    """启动 API 服务"""
    url = f'http://{host}:{port}'
    docs_url = url + settings.FASTAPI_DOCS_URL
    redoc_url = url + settings.FASTAPI_REDOC_URL
    openapi_url = url + (settings.FASTAPI_OPENAPI_URL or '')

    panel_content = Text()
    panel_content.append('Python 版本：', style='bold cyan')
    panel_content.append(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}', style='white')

    panel_content.append('\nAPI 请求地址: ', style='bold cyan')
    panel_content.append(f'{url}{settings.FASTAPI_API_V1_PATH}', style='blue')

    panel_content.append('\n\n环境模式：', style='bold green')
    env_style = 'yellow' if settings.ENVIRONMENT == 'dev' else 'green'
    panel_content.append(f'{settings.ENVIRONMENT.upper()}', style=env_style)

    plugins = get_plugins()
    panel_content.append('\n已安装插件：', style='bold green')
    if plugins:
        panel_content.append(f'{", ".join(plugins)}', style='yellow')
    else:
        panel_content.append('无', style='white')

    if settings.ENVIRONMENT == 'dev':
        panel_content.append(f'\n\n📖 Swagger 文档: {docs_url}', style='bold magenta')
        panel_content.append(f'\n📚 Redoc   文档: {redoc_url}', style='bold magenta')
        panel_content.append(f'\n📡 OpenAPI JSON: {openapi_url}', style='bold magenta')

    panel_content.append('\n🌐 架构官方文档: ', style='bold magenta')
    panel_content.append('https://fastapi-practices.github.io/fastapi_best_architecture_docs/')

    console.print(Panel(panel_content, title=f'fba (v{__version__})', border_style='purple', padding=(1, 2)))
    granian.Granian(
        target='backend.main:app',
        interface='asgi',
        address=host,
        port=port,
        reload=not reload,
        reload_filter=CustomReloadFilter,
        workers=workers,
    ).serve()


async def get_sql_scripts() -> list[str]:
    """获取所有待执行的 SQL 脚本路径列表"""
    sql_scripts: list[str] = []
    db_script_dir = MYSQL_SCRIPT_DIR if DataBaseType.mysql == settings.DATABASE_TYPE else POSTGRESQL_SCRIPT_DIR
    main_sql_file = db_script_dir / build_sql_filename(
        'init',
        settings.DATABASE_PK_MODE,
        suffix='test_data',
    )

    if await anyio.Path(main_sql_file).exists():
        sql_scripts.append(str(main_sql_file))

    for plugin in get_plugins():
        plugin_sql = await get_plugin_sql(plugin, settings.DATABASE_TYPE, settings.DATABASE_PK_MODE)
        if plugin_sql:
            sql_scripts.append(plugin_sql)

    return sql_scripts


async def execute_sql_scripts(db: AsyncSession, sql_scripts: str, *, is_init: bool = False) -> None:
    """解析并执行 SQL 脚本"""
    try:
        stmts = await parse_sql_script(sql_scripts)
        conn = await db.connection()
        for stmt in stmts:
            await conn.exec_driver_sql(stmt)
    except Exception as e:
        raise cappa.Exit(f'SQL 脚本执行失败：{e}', code=1)

    if not is_init:
        console.tip('SQL 脚本已执行完成')


def run_alembic(*args: str) -> None:
    """执行 alembic 命令"""
    try:
        subprocess.run(['alembic', *args], cwd=BASE_PATH.parent, check=True)
    except subprocess.CalledProcessError as e:
        raise cappa.Exit('Alembic 命令执行失败', code=e.returncode)


@cappa.command(help='初始化 fba 项目', default_long=True)
@dataclass
class Init:
    auto: Annotated[
        bool,
        cappa.Arg(default=False, help='自动化初始化模式：自动创建 .env、安装依赖、创建数据库并初始化表结构'),
    ]

    async def __call__(self) -> None:
        if self.auto:
            await auto_init()
        else:
            async with async_db_session.begin() as db:
                await init(db, redis_client)


@cappa.command(help='运行 API 服务', default_long=True)
@dataclass
class Run:
    host: Annotated[
        str,
        cappa.Arg(
            default='127.0.0.1',
            help='提供服务的主机 IP 地址，对于本地开发，请使用 `127.0.0.1`。'
            '要启用公共访问，例如在局域网中，请使用 `0.0.0.0`',
        ),
    ]
    port: Annotated[
        int,
        cappa.Arg(default=8000, help='提供服务的主机端口号'),
    ]
    no_reload: Annotated[
        bool,
        cappa.Arg(default=False, help='禁用在（代码）文件更改时自动重新加载服务器'),
    ]
    workers: Annotated[
        int,
        cappa.Arg(default=1, help='使用多个工作进程，必须与 `--no-reload` 同时使用'),
    ]

    def __call__(self) -> None:
        run(host=self.host, port=self.port, reload=self.no_reload, workers=self.workers)


@cappa.command(help='格式化代码')
@dataclass
class Format:
    def __call__(self) -> None:
        try:
            subprocess.run(['prek', 'run', '--all-files'], cwd=BASE_PATH.parent, check=False)
        except FileNotFoundError:
            raise cappa.Exit('prek 未安装，请先安装项目依赖', code=1)
        except KeyboardInterrupt:
            pass


@cappa.command(help='生成数据库迁移文件', default_long=True)
@dataclass
class Revision:
    autogenerate: Annotated[
        bool,
        cappa.Arg(default=True, help='自动检测模型变更并生成迁移脚本'),
    ]
    message: Annotated[
        str,
        cappa.Arg(short='-m', default='', help='迁移文件的描述信息'),
    ]

    def __call__(self) -> None:
        args = ['revision']
        if self.autogenerate:
            args.append('--autogenerate')
        if self.message:
            args.extend(['-m', self.message])
        run_alembic(*args)
        console.tip('迁移文件生成成功')


@cappa.command(help='升级数据库到指定版本', default_long=True)
@dataclass
class Upgrade:
    revision: Annotated[
        str,
        cappa.Arg(default='head', help='目标版本，默认为最新版本'),
    ]

    def __call__(self) -> None:
        run_alembic('upgrade', self.revision)
        console.tip(f'数据库已升级到: {self.revision}')


@cappa.command(help='降级数据库到指定版本', default_long=True)
@dataclass
class Downgrade:
    revision: Annotated[
        str,
        cappa.Arg(default='-1', help='目标版本，默认回退一个版本'),
    ]

    def __call__(self) -> None:
        run_alembic('downgrade', self.revision)
        console.tip(f'数据库已降级到: {self.revision}')


@cappa.command(help='显示数据库当前迁移版本')
@dataclass
class Current:
    verbose: Annotated[
        bool,
        cappa.Arg(short='-v', default=False, help='显示详细信息'),
    ]

    def __call__(self) -> None:
        args = ['current']
        if self.verbose:
            args.append('-v')
        run_alembic(*args)


@cappa.command(help='显示迁移历史记录', default_long=True)
@dataclass
class History:
    verbose: Annotated[
        bool,
        cappa.Arg(short='-v', default=False, help='显示详细信息'),
    ]
    range: Annotated[
        str,
        cappa.Arg(short='-r', default='', help='显示指定范围的历史，例如 -r base:head'),
    ]

    def __call__(self) -> None:
        args = ['history']
        if self.verbose:
            args.append('-v')
        if self.range:
            args.extend(['-r', self.range])
        run_alembic(*args)


@cappa.command(help='显示所有头版本')
@dataclass
class Heads:
    verbose: Annotated[
        bool,
        cappa.Arg(short='-v', default=False, help='显示详细信息'),
    ]

    def __call__(self) -> None:
        args = ['heads']
        if self.verbose:
            args.append('-v')
        run_alembic(*args)


@cappa.command(help='数据库迁移管理')
@dataclass
class Alembic:
    subcmd: cappa.Subcommands[Revision | Upgrade | Downgrade | Current | History | Heads]


@cappa.command(help='一个高效的 fba 命令行界面', default_long=True)
@dataclass
class FbaCli:
    sql: Annotated[
        str,
        cappa.Arg(value_name='PATH', default='', show_default=False, help='在事务中执行 SQL 脚本'),
    ]
    subcmd: cappa.Subcommands[Init | Run | Format | Alembic | None] = None

    async def __call__(self) -> None:
        if self.sql:
            async with async_db_session.begin() as db:
                await execute_sql_scripts(db, self.sql)


def main() -> None:
    output = cappa.Output(error_format=f'{error_format}\n{_OUTPUT_HELP}')
    asyncio.run(cappa.invoke_async(FbaCli, version=__version__, output=output))
