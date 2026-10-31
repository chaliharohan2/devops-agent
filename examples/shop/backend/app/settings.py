from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://shop:shop@localhost:5432/shop"
    cors_origins: str = "*"
    app_name: str = "Shop API"


@lru_cache
def get_settings() -> Settings:
    return Settings()
