# app/core/config.py
import os

class Settings:
    API_V1_STR: str = "/api/v1"
    PROJECT_NAME: str = "TuStockYa Backend"
    VERSION: str = "1.0.0"
    
    # JWT
    SECRET_KEY: str = "super-secret-key-cambia-en-produccion"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080
    
    # Files
    UPLOAD_DIR: str = "data/uploads"
    MAX_FILE_SIZE: int = 10485760
    
    # Environment
    DEBUG: bool = True
    ENVIRONMENT: str = "development"

settings = Settings()