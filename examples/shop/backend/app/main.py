from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Item
from .settings import get_settings

settings = get_settings()
app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ItemIn(BaseModel):
    name: str
    price: float


class ItemOut(ItemIn):
    id: int


@app.get("/health")
def health() -> dict:
    return {"ok": True, "app": settings.app_name}


@app.get("/api/items", response_model=list[ItemOut])
def list_items(db: Session = Depends(get_db)) -> list[ItemOut]:
    return [ItemOut(id=i.id, name=i.name, price=float(i.price)) for i in db.scalars(select(Item).order_by(Item.id))]


@app.post("/api/items", response_model=ItemOut, status_code=201)
def create_item(body: ItemIn, db: Session = Depends(get_db)) -> ItemOut:
    item = Item(name=body.name, price=body.price)
    db.add(item)
    db.commit()
    return ItemOut(id=item.id, name=item.name, price=float(item.price))
