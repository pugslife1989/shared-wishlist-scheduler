FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN rm -rf /usr/local/lib/python3.13/site-packages/setuptools/_vendor/jaraco.context-5.3.0.dist-info /usr/local/lib/python3.13/site-packages/setuptools/_vendor/wheel-0.45.1.dist-info
COPY app.py .
COPY templates/ ./templates/
COPY static/ ./static/
RUN mkdir -p /data /data/photos
ENV DB_PATH=/data/places.db PHOTOS_DIR=/data/photos PORT=5000
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
