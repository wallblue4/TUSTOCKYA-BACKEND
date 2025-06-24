# Dockerfile - Optimizado para Railway
FROM python:3.11-slim

# Variables de entorno
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    libgl1-mesa-glx \
    libglib2.0-0 \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Copiar requirements primero para cache
COPY requirements.txt .

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo el código
COPY . .

# Crear directorios necesarios
RUN mkdir -p data/uploads \
    && chmod 755 data \
    && chmod 755 data/uploads

# Exponer puerto (Railway usa $PORT)
EXPOSE $PORT

# Comando de inicio
CMD ["python", "-m", "uvicorn", "main_standalone:app", "--host", "0.0.0.0", "--port", "$PORT"]