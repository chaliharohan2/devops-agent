from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    price: Mapped[float] = mapped_column(Numeric(10, 2))
