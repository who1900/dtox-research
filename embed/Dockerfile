FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn onnxruntime "transformers>=4.57" numpy

COPY model /app/model
# The cross-encoder used by /rerank. It lived in the container's writable layer
# for a day, which meant any recreation of the container would have removed it
# silently: the API would have kept answering, just without reranking.
COPY models/ce /models/ce
COPY app.py /app/app.py

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
