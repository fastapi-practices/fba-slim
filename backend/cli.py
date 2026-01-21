import asyncio
import re
import secrets
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import anyio
import cappa
import granian

from cappa.output import error_format
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from watchfiles import PythonFilter

from backend import __version__
from backend.common.enums import DataBaseType, PrimaryKeyType
from backend.common.exception.errors import BaseExceptionError
from backend.common.model import MappedBase
from backend.core.conf import settings
from backend.core.path_conf import (
    ENV_EXAMPLE_FILE_PATH,
    ENV_FILE_PATH,
    MYSQL_SCRIPT_DIR,
    POSTGRESQL_SCRIPT_DIR,
)
from backend.database.db import (
    async_db_session,
    create_database_async_engine,
    create_database_async_session,
    create_database_url,
)
from backend.database.redis import RedisCli, redis_client
from backend.plugin.core import get_plugin_sql, get_plugins
from backend.plugin.installer import install_git_plugin, install_zip_plugin
from backend.utils.console import console
from backend.utils.sql_parser import parse_sql_script

output_help = '\n更多信息，尝试 "[cyan]--help[/]"'


class CustomReloadFilter(PythonFilter):
    """自定义重载过滤器"""

    def __init__(self) -> None:
        super().__init__(extra_extensions=['.json', '.yaml', '.yml'])


def setup_env_file() -> bool:
    if not ENV_EXAMPLE_FILE_PATH.exists():
        console.print('.env.example 文件不存在', style='red')
        return False

    try:
        env_content = Path(ENV_EXAMPLE_FILE_PATH).read_text(encoding='utf-8')
        console.print('配置数据库连接信息...', style='white')
        db_type = Prompt.ask('数据库类型', choices=['mysql', 'postgresql'], default='postgresql')
        db_host = Prompt.ask('数据库主机', default='127.0.0.1')
        db_port = Prompt.ask('数据库端口', default='5432' if db_type == 'postgresql' else '3306')
        db_user = Prompt.ask('数据库用户名', default='postgres' if db_type == 'postgresql' else 'root')
        db_password = Prompt.ask('数据库密码', password=True, default='123456')

        console.print('配置 Redis 连接信息...', style='white')
        redis_host = Prompt.ask('Redis 主机', default='127.0.0.1')
        redis_port = Prompt.ask('Redis 端口', default='6379')
        redis_password = Prompt.ask('Redis 密码（留空表示无密码）', password=True, default='')
        redis_db = Prompt.ask('Redis 数据库编号', default='0')

        console.print('生成 Token 密钥...', style='white')
        token_secret = secrets.token_urlsafe(32)

        console.print('写入 .env 文件...', style='white')
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
        console.print('.env 文件创建成功', style='green')
    except Exception as e:
        console.print(f'.env 文件创建失败: {e}', style='red')
        return False
    else:
        return True


async def create_database(conn: AsyncConnection) -> bool:
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
        console.print(f'重建 {settings.DATABASE_SCHEMA} 数据库...', style='white')
        if exists:
            if terminate_sql:
                await conn.execute(text(terminate_sql))
            await conn.execute(text(drop_sql))
        await conn.execute(text(create_sql))
        console.print('数据库创建成功', style='green')
    except Exception as e:
        console.print(f'数据库创建失败: {e}', style='red')
        return False
    else:
        return True


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

    console.print(Panel(panel_content, title=f'fba (v{__version__}) - 数据库', border_style='cyan', padding=(1, 2)))
    ok = Prompt.ask('即将[red]新建/重建数据库[/red]，确认继续吗？', choices=['y', 'n'], default='n')

    if ok.lower() == 'y':
        async_init_engine = create_database_async_engine(create_database_url(with_database=False))
        async with async_init_engine.connect() as conn:
            await conn.execution_options(isolation_level='AUTOCOMMIT')
            if not await create_database(conn):
                raise cappa.Exit('数据库创建失败', code=1)
    else:
        console.print('已取消数据库操作', style='yellow')

    console.print('\n[bold cyan]步骤 3/3:[/] 初始化数据库表和数据', style='bold')
    async_init_engine = create_database_async_engine(create_database_url())
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
        console.print('开始初始化...', style='white')
        try:
            console.print('清理 Redis 缓存', style='white')
            for prefix in [
                settings.JWT_USER_REDIS_PREFIX,
                settings.TOKEN_EXTRA_INFO_REDIS_PREFIX,
                settings.TOKEN_REDIS_PREFIX,
                settings.TOKEN_REFRESH_REDIS_PREFIX,
            ]:
                await redis.delete_prefix(prefix)

            console.print('重建数据库表', style='white')
            conn = await db.connection()
            await conn.run_sync(MappedBase.metadata.drop_all)
            await conn.run_sync(MappedBase.metadata.create_all)

            console.print('执行 SQL 脚本', style='white')
            sql_scripts = await get_sql_scripts()
            for sql_script in sql_scripts:
                console.print(f'正在执行：{sql_script}', style='white')
                await execute_sql_scripts(db, sql_script, is_init=True)

            console.print('初始化成功', style='green')
            console.print('\n快试试 [bold cyan]fba run[/bold cyan] 启动服务吧~')
        except Exception as e:
            raise cappa.Exit(f'初始化失败：{e}', code=1)
    else:
        console.print('已取消初始化操作', style='yellow')


def run(host: str, port: int, reload: bool, workers: int) -> None:  # noqa: FBT001
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


async def install_plugin(
    path: str,
    repo_url: str,
    no_sql: bool,  # noqa: FBT001
    db_type: DataBaseType,
    pk_type: PrimaryKeyType,
) -> None:
    if not path and not repo_url:
        raise cappa.Exit('path 或 repo_url 必须指定其中一项', code=1)
    if path and repo_url:
        raise cappa.Exit('path 和 repo_url 不能同时指定', code=1)

    plugin_name = None
    console.print('开始安装插件...', style='bold cyan')

    try:
        if path:
            plugin_name = await install_zip_plugin(file=path)
        if repo_url:
            plugin_name = await install_git_plugin(repo_url=repo_url)

        console.print(f'插件 {plugin_name} 安装成功', style='bold green')

        sql_file = await get_plugin_sql(plugin_name, db_type, pk_type)
        if sql_file and not no_sql:
            console.print('开始自动执行插件 SQL 脚本...', style='bold cyan')
            async with async_db_session.begin() as db:
                await execute_sql_scripts(db, sql_file)

    except Exception as e:
        raise cappa.Exit(e.msg if isinstance(e, BaseExceptionError) else str(e), code=1)


async def get_sql_scripts() -> list[str]:
    sql_scripts = []
    db_script_dir = MYSQL_SCRIPT_DIR if DataBaseType.mysql == settings.DATABASE_TYPE else POSTGRESQL_SCRIPT_DIR
    main_sql_file = (
        db_script_dir / 'init_test_data.sql'
        if PrimaryKeyType.autoincrement == settings.DATABASE_PK_MODE
        else db_script_dir / 'init_snowflake_test_data.sql'
    )

    main_sql_path = anyio.Path(main_sql_file)
    if await main_sql_path.exists():
        sql_scripts.append(str(main_sql_file))

    plugins = get_plugins()
    for plugin in plugins:
        plugin_sql = await get_plugin_sql(plugin, settings.DATABASE_TYPE, settings.DATABASE_PK_MODE)
        if plugin_sql:
            sql_scripts.append(str(plugin_sql))

    return sql_scripts


async def execute_sql_scripts(db: AsyncSession, sql_scripts: str, *, is_init: bool = False) -> None:
    try:
        stmts = await parse_sql_script(sql_scripts)
        for stmt in stmts:
            await db.execute(text(stmt))
    except Exception as e:
        raise cappa.Exit(f'SQL 脚本执行失败：{e}', code=1)

    if not is_init:
        console.print('SQL 脚本已执行完成', style='bold green')


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


@cappa.command(help='新增插件', default_long=True)
@dataclass
class Add:
    path: Annotated[
        str | None,
        cappa.Arg(help='ZIP 插件的本地完整路径'),
    ]
    repo_url: Annotated[
        str | None,
        cappa.Arg(help='Git 插件的仓库地址'),
    ]
    no_sql: Annotated[
        bool,
        cappa.Arg(default=False, help='禁用插件 SQL 脚本自动执行'),
    ]
    db_type: Annotated[
        DataBaseType,
        cappa.Arg(default='postgresql', help='执行插件 SQL 脚本的数据库类型'),
    ]
    pk_type: Annotated[
        PrimaryKeyType,
        cappa.Arg(default='autoincrement', help='执行插件 SQL 脚本数据库主键类型'),
    ]

    async def __call__(self) -> None:
        await install_plugin(self.path, self.repo_url, self.no_sql, self.db_type, self.pk_type)


@cappa.command(help='一个高效的 fba 命令行界面', default_long=True)
@dataclass
class FbaCli:
    sql: Annotated[
        str,
        cappa.Arg(value_name='PATH', default='', show_default=False, help='在事务中执行 SQL 脚本'),
    ]
    subcmd: cappa.Subcommands[Init | Run | Add | None] = None

    async def __call__(self) -> None:
        if self.sql:
            async with async_db_session.begin() as db:
                await execute_sql_scripts(db, self.sql)


def main() -> None:
    output = cappa.Output(error_format=f'{error_format}\n{output_help}')
    asyncio.run(cappa.invoke_async(FbaCli, version=__version__, output=output))
