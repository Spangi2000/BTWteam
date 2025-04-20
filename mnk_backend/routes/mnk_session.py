import asyncio
import datetime

from auth_lib.fastapi import UnionAuth
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi_sqlalchemy import db

from mnk_backend.exceptions import InactiveSession, NoneAvailable, ObjectNotFound
from mnk_backend.models.db import Item, ItemType, mnkSession
from mnk_backend.routes.strike import create_strike
from mnk_backend.schemas.models import mnkSessionGet, mnkSessionPatch, RentStatus, StrikePost
from mnk_backend.utils.action import ActionLogger


mnk_session = APIRouter(prefix="/mnk-sessions", tags=["mnkSession"])

mnk_SESSION_EXPIRY = datetime.timedelta(minutes=10)


async def check_session_expiration(session_id: int):
    """
    Фоновая задача для проверки и истечения срока аренды.

    :param session_id: Идентификатор сессии аренды.
    """
    await asyncio.sleep(mnk_SESSION_EXPIRY.total_seconds())
    session = mnkSession.query(session=db.session).filter(mnkSession.id == session_id).one_or_none()
    if session and session.status == RentStatus.RESERVED:
        mnkSession.update(
            session=db.session,
            id=session_id,
            status=RentStatus.CANCELED,
        )
        Item.update(session=db.session, id=session.item_id, is_available=True)
        ActionLogger.log_event(
            user_id=session.user_id,
            admin_id=None,
            session_id=session.id,
            action_type="EXPIRE_SESSION",
            details={"status": RentStatus.CANCELED},
        )


@mnk_session.post("/{item_type_id}", response_model=mnkSessionGet)
async def create_mnk_session(item_type_id, background_tasks: BackgroundTasks, user=Depends(UnionAuth())):
    """
    Создает новую сессию аренды для указанного типа предмета.

    :param item_type_id: Идентификатор типа предмета.
    :param background_tasks: Фоновые задачи для выполнения.
    :return: Объект mnkSessionGet с информацией о созданной сессии аренды.
    :raises NoneAvailable: Если нет доступных предметов указанного типа.
    """
    available_items = (
        Item.query(session=db.session).filter(Item.type_id == item_type_id, Item.is_available == True).all()
    )
    if not available_items:
        raise NoneAvailable(ItemType, item_type_id)
    session = mnkSession.create(
        session=db.session,
        user_id=user.get("id"),
        item_id=available_items[0].id,
        reservation_ts=datetime.datetime.now(tz=datetime.timezone.utc),
        status=RentStatus.RESERVED,
    )
    Item.update(session=db.session, id=available_items[0].id, is_available=False)

    background_tasks.add_task(check_session_expiration, session.id)

    ActionLogger.log_event(
        user_id=user.get("id"),
        admin_id=None,
        session_id=session.id,
        action_type="CREATE_SESSION",
        details={"item_id": session.item_id, "status": RentStatus.RESERVED},
    )

    return mnkSessionGet.model_validate(session)


@mnk_session.patch("/{session_id}/start", response_model=mnkSessionGet)
async def start_mnk_session(session_id, user=Depends(UnionAuth(scopes=["mnk.session.admin"]))):
    """
    Начинает сессию аренды, изменяя её статус на ACTIVE.

    :param session_id: Идентификатор сессии аренды.

    :return: Объект mnkSessionGet с обновленной информацией о сессии аренды.
    :raises ObjectNotFound: Если сессия с указанным идентификатором не найдена.
    """
    session = mnkSession.get(id=session_id, session=db.session)
    if not session:
        raise ObjectNotFound
    updated_session = mnkSession.update(
        session=db.session,
        id=session_id,
        status=RentStatus.ACTIVE,
        start_ts=datetime.datetime.now(tz=datetime.timezone.utc),
        admin_open_id=user.get("id"),
    )

    ActionLogger.log_event(
        user_id=session.user_id,
        admin_id=user.get("id"),
        session_id=session.id,
        action_type="START_SESSION",
        details={"status": RentStatus.ACTIVE},
    )

    return mnkSessionGet.model_validate(updated_session)


@mnk_session.patch("/{session_id}/return", response_model=mnkSessionGet)
async def accept_end_mnk_session(
    session_id,
    with_strike: bool = Query(False, description="Флаг, определяющий выдачу страйка"),
    strike_reason: str = Query("", description="Описание причины страйка"),
    user=Depends(UnionAuth(scopes=["mnk.session.admin"])),
):
    """
    Завершает сессию аренды, изменяя её статус на RETURNED. При необходимости выдает страйк.
    :param session_id: Идентификатор сессии аренды.
    :param with_strike: Флаг, указывающий, нужно ли выдать страйк.
    :param strike_reason: Причина выдачи страйка.
    :return: Объект mnkSessionGet с обновленной информацией о сессии аренды.
    :raises ObjectNotFound: Если сессия с указанным идентификатором не найдена.
    :raises InactiveSession: Если сессия не активна.
    """
    rent_session = mnkSession.get(id=session_id, session=db.session)
    if not rent_session:
        raise ObjectNotFound
    if rent_session.status != RentStatus.ACTIVE:
        raise InactiveSession
    ended_session = mnkSession.update(
        session=db.session,
        id=session_id,
        status=RentStatus.RETURNED,
        end_ts=datetime.datetime.now(tz=datetime.timezone.utc) if not rent_session.end_ts else rent_session.end_ts,
        actual_return_ts=datetime.datetime.now(tz=datetime.timezone.utc),
        admin_close_id=user.get("id"),
    )

    ActionLogger.log_event(
        user_id=rent_session.user_id,
        admin_id=user.get("id"),
        session_id=rent_session.id,
        action_type="RETURN_SESSION",
        details={"status": RentStatus.RETURNED},
    )

    if with_strike:
        strike_info = StrikePost(
            user_id=ended_session.user_id, admin_id=user.get("id"), reason=strike_reason, session_id=rent_session.id
        )
        create_strike(strike_info, user=user)

    return mnkSessionGet.model_validate(ended_session)


@mnk_session.get("/user/{user_id}", response_model=list[mnkSessionGet])
async def get_user_sessions(user_id, user=Depends(UnionAuth())):
    """
    Получает список сессий аренды для указанного пользователя.

    :param user_id: id пользователя.
    :return: Список объектов mnkSessionGet с информацией о сессиях аренды.
    """
    user_sessions = mnkSession.query(session=db.session).filter(mnkSession.user_id == user_id).all()
    return [mnkSessionGet.model_validate(user_session) for user_session in user_sessions]


@mnk_session.get("/{session_id}", response_model=mnkSessionGet)
async def get_mnk_session(session_id: int, user=Depends(UnionAuth())):
    session = mnkSession.get(id=session_id, session=db.session)

    return mnkSessionGet.model_validate(session)


@mnk_session.get("", response_model=list[mnkSessionGet])
async def get_mnk_sessions(
    is_reserved: bool = Query(False, description="флаг, показывать заявки"),
    is_canceled: bool = Query(False, description="Флаг, показывать отмененные"),
    is_dismissed: bool = Query(False, description="Флаг, показывать отклоненные"),
    is_overdue: bool = Query(False, description="Флаг, показывать просроченные"),
    is_returned: bool = Query(False, description="Флаг, показывать вернутые"),
    is_active: bool = Query(False, description="Флаг, показывать активные"),
    user=Depends(UnionAuth(scopes=["mnk.session.admin"])),
):
    """
    Получает список сессий аренды с возможностью фильтрации по статусу.

    :param is_reserved: Флаг, показывать зарезервированные сессии.
    :param is_canceled: Флаг, показывать отмененные сессии.
    :param is_dismissed: Флаг, показывать отклоненные сессии.
    :param is_overdue: Флаг, показывать просроченные сессии.
    :param is_returned: Флаг, показывать возвращенные сессии.
    :param is_active: Флаг, показывать активные сессии.
    :return: Список объектов mnkSessionGet с информацией о сессиях аренды.
    """
    to_show = []
    if is_reserved:
        to_show.append(RentStatus.RESERVED)
    if is_canceled:
        to_show.append(RentStatus.CANCELED)
    if is_dismissed:
        to_show.append(RentStatus.DISMISSED)
    if is_overdue:
        to_show.append(RentStatus.OVERDUE)
    if is_returned:
        to_show.append(RentStatus.RETURNED)
    if is_active:
        to_show.append(RentStatus.ACTIVE)

    rent_sessions = mnkSession.query(session=db.session).filter(mnkSession.status.in_(to_show)).all()
    return [mnkSessionGet.model_validate(rent_session) for rent_session in rent_sessions]


@mnk_session.get("/{session_id}", response_model=mnkSessionGet)
async def get_mnk_session(session_id: int, user=Depends(UnionAuth())):
    session = mnkSession.get(id=session_id, session=db.session)
    return mnkSessionGet.model_validate(session)


@mnk_session.patch("/{session_id}", response_model=mnkSessionGet)
async def update_mnk_session(
    session_id: int, update_data: mnkSessionPatch, user=Depends(UnionAuth(scopes=["mnk.session.admin"]))
):
    """
    Обновляет информацию о сессии аренды.

    :param session_id: Идентификатор сессии аренды.
    :param update_data: Данные для обновления сессии.
    :return: Объект mnkSessionGet с обновленной информацией о сессии аренды.
    :raises ObjectNotFound: Если сессия с указанным идентификатором не найдена.
    """
    session = mnkSession.get(id=session_id, session=db.session)
    if not session:
        raise ObjectNotFound
    upd_data = update_data.model_dump(exclude_unset=True)

    updated_session = mnkSession.update(session=db.session, id=session_id, **upd_data)

    ActionLogger.log_event(
        user_id=session.user_id,
        admin_id=user.get("id"),
        session_id=session.id,
        action_type="UPDATE_SESSION",
        details={"status": session.status, "end_ts": session.end_ts, "actual_return_ts": session.actual_return_ts},
    )

    return mnkSessionGet.model_validate(updated_session)
