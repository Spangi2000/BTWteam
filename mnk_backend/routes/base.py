from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_sqlalchemy import DBSessionMiddleware
from logger_middleware import LoggerMiddleware

from mnk_backend import __version__
from mnk_backend.routes.event import event
from mnk_backend.routes.item import item
from mnk_backend.routes.item_type import item_type
from mnk_backend.routes.mnk_session import mnk_session
from mnk_backend.routes.strike import strike
from mnk_backend.settings import get_settings


settings = get_settings()
app = FastAPI(
    title='Сервис цифрового проката',
    description='Краткое описание',
    version=__version__,
    # Отключаем нелокальную документацию
    root_path=settings.ROOT_PATH if __version__ != 'dev' else '',
    docs_url=None if __version__ != 'dev' else '/docs',
    redoc_url=None,
)


app.add_middleware(
    DBSessionMiddleware,
    db_url=str(settings.DB_DSN),
    engine_args={"pool_pre_ping": True, "isolation_level": "AUTOCOMMIT"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

app.add_middleware(LoggerMiddleware, service_id=settings.SERVICE_ID)

app.include_router(event)
app.include_router(item)
app.include_router(mnk_session)
app.include_router(item_type)
app.include_router(strike)
