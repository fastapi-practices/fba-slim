from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.conf import settings
from backend.database.db import async_engine
from backend.utils.serializers import select_list_serialize

_sys_config_table_exists: bool | None = None


async def check_sys_config_table_exists() -> bool:
    """
    检查 sys_config 表是否存在

    :return:
    """
    global _sys_config_table_exists
    if _sys_config_table_exists is None:
        async with async_engine.begin() as conn:
            _sys_config_table_exists = await conn.run_sync(lambda c: inspect(c).has_table('sys_config', schema=None))
    return _sys_config_table_exists


async def load_login_config(db: AsyncSession) -> None:
    """
    获取登录配置

    :param db: 数据库会话
    :return:
    """
    if not await check_sys_config_table_exists():
        return

    from backend.plugin.config.crud.crud_config import config_dao
    from backend.plugin.config.enums import ConfigType

    dynamic_config = await config_dao.get_all(db, ConfigType.login)

    if dynamic_config:
        login_config_status_key = 'LOGIN_CONFIG_STATUS'
        login_captcha_enabled_key = 'LOGIN_CAPTCHA_ENABLED'

        configs = {dc['key']: dc['value'] for dc in select_list_serialize(dynamic_config)}
        if int(configs.get(login_config_status_key)) and login_captcha_enabled_key in configs:
            settings.LOGIN_CAPTCHA_ENABLED = configs[login_captcha_enabled_key] == 'true'
