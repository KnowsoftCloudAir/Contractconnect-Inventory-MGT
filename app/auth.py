from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session, select
from dotenv import load_dotenv
import os

from app.database import get_session
from app.models import User, UserRole

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY", "knowsoft-churchgate-change-this-in-production-32chars")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))

def role_val(role) -> str:
    if role is None:
        return ""
    return str(getattr(role, "value", role)).lower().replace("userrole.", "")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    if isinstance(password, str):
        password = password.encode("utf-8")[:72].decode("utf-8", errors="ignore")
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_user_by_email(session: Session, email: str) -> Optional[User]:
    return session.exec(select(User).where(User.email == email)).first()

async def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    session: Session = Depends(get_session)
) -> Optional[User]:
    if not token:
        token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            return None
    except JWTError:
        return None
    user = get_user_by_email(session, email)
    if not user or not user.is_active:
        return None
    rv = role_val(user.role)
    # Staff always see church dashboard; members only if granted or pastor status
    if rv in ("general_admin", "church_admin", "data_officer"):
        user.can_view_church_dashboard = True
    elif rv == "member":
        # Members never see the church operational dashboard
        user.can_view_church_dashboard = False
    else:
        user.can_view_church_dashboard = False
    return user

async def require_user(user: Optional[User] = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

def require_roles(*roles: UserRole):
    async def checker(user: User = Depends(require_user)) -> User:
        rv = role_val(user.role)
        allowed = {role_val(r) for r in roles} | {"general_admin"}
        if rv not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user
    return checker
