from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import services
from ..db import get_db
from ..deps import current_user
from ..schemas import SettingsIn, SettingsOut

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingsOut, summary="读取当前用户的活动设置")
def read_settings(
    db: Session = Depends(get_db), user_id: str = Depends(current_user)
) -> SettingsOut:
    return services.settings_out(services.get_or_create_settings(db, user_id))


@router.put("", response_model=SettingsOut, summary="保存活动设置")
def write_settings(
    payload: SettingsIn,
    db: Session = Depends(get_db),
    user_id: str = Depends(current_user),
) -> SettingsOut:
    return services.settings_out(services.save_settings(db, user_id, payload))
