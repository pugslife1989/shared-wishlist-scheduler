FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/
RUN mkdir -p /data /data/photos
ENV DB_PATH=/data/places.db PHOTOS_DIR=/data/photos PORT=5000
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
