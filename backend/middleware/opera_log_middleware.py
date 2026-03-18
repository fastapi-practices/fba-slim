import time

from typing import Any

from fastapi import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from backend.common.context import ctx
from backend.common.log import log
from backend.common.response.response_code import StandardResponseCode
from backend.core.conf import settings


class OperaLogMiddleware(BaseHTTPMiddleware):
    """操作日志中间件"""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        """
        处理请求并记录操作日志

        :param request: FastAPI 请求对象
        :param call_next: 下一个中间件或路由处理函数
        :return:
        """
        path = request.url.path
        method = request.method
        code = 200
        elapsed = 0

        try:
            response = await call_next(request)
        except Exception as e:
            elapsed = round((time.perf_counter() - ctx.perf_time) * 1000, 3)
            code = getattr(e, 'code', StandardResponseCode.HTTP_500)
            log.error(f'请求异常: {e!s}')
            raise
        else:
            elapsed = round((time.perf_counter() - ctx.perf_time) * 1000, 3)

            for exception_key in [
                '__request_http_exception__',
                '__request_validation_exception__',
                '__request_assertion_error__',
                '__request_custom_exception__',
            ]:
                exception = ctx.get(exception_key)
                if exception:
                    code = exception.get('code')
                    log.error(f'请求异常: {exception.get("msg")}')
                    break
        finally:
            route = request.scope.get('route')
            summary = route.summary or '' if route else ''

            log.debug(f'接口摘要：[{summary}]')
            log.debug(f'请求地址：[{ctx.ip}]')

            if request.method != 'OPTIONS':
                log.debug('<-- 请求结束')

            if path.startswith(settings.FASTAPI_API_V1_PATH):
                log.info(f'{ctx.ip: <15} | {method: <8} | {code!s: <6} | {path} | {elapsed:.3f}ms')

        return response
